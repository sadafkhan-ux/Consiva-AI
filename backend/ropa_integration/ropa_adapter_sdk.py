"""Reusable ROPA integration-adapter SDK.

This module is designed to be COPIED INTO A CUSTOMER'S OWN BACKEND and run
there. It has no dependency on any Consiva internal module -- only stdlib plus
`requests` (or `httpx`) -- so it can be dropped into a foreign FastAPI/Django/
Flask project without dragging this repository along.

The pattern it implements, in the order the pipeline requires:

    CONNECT (their own DB session)
      -> READ      schema metadata via SQLAlchemy's Inspector, read-only
      -> VALIDATE  against an explicit allow-list of approved tables/columns
      -> TRANSFORM into curated DiscoveryEvidence JSON
      -> SEND      over authenticated HTTPS with retries

Guarantees this SDK makes, by construction:

* It issues NO SQL of its own. Everything comes from SQLAlchemy's `Inspector`,
  which reads the system catalog. There is no INSERT/UPDATE/DELETE/DDL path in
  this file at all.
* It NEVER reads row data. Only table names, column names, types, nullability
  and foreign keys leave the customer's system.
* It sends only what the allow-list explicitly approves. A table added to their
  database later is invisible to Consiva until someone approves it.
* It never transmits credentials or encryption keys.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 3
# Retry only on transient conditions. A 4xx means the request itself is wrong
# and retrying would just repeat the same rejection.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


SCHEMA_VERSION = "1.0"
ADAPTER_VERSION = "1.0.0"


@dataclass(frozen=True)
class FieldDeclaration:
    """Per-field ROPA metadata the SOURCE OWNER declares about one column.

    This is how a customer states what they already know about their own data,
    instead of Consiva inferring it. Anything left None stays genuinely unknown
    -- it is NEVER filled in with a plausible guess, which is exactly what the
    ROPA rules forbid.

    Declared values are trusted downstream: they arrive as
    ColumnRecord.existing_classification / existing_data_subject /
    existing_purpose, which classification_service treats as rule-order #1 and
    never overwrites automatically.
    """

    column: str
    is_personal_data: bool | None = None
    personal_data_category: str | None = None
    data_subject: str | None = None
    purpose: str | None = None
    processing_activity: str | None = None
    retention: str | None = None
    processor: str | None = None
    confidence: float | None = None
    notes: str | None = None


@dataclass(frozen=True)
class TableAllowList:
    """Which tables and columns an operator has explicitly approved for ROPA.

    Three levels of strictness, from loosest to tightest:

      TableAllowList("attendees")
        -> every column's NAME and TYPE, no declared metadata
      TableAllowList("attendees", columns=("id", "email"))
        -> only those columns, no declared metadata
      TableAllowList("attendees", fields=(FieldDeclaration("email", ...),))
        -> only those columns, WITH the owner's own ROPA metadata attached

    `fields` also acts as a column allow-list, so the tightest form needs only
    one declaration per approved column.
    """

    table: str
    columns: tuple[str, ...] | None = None
    fields: tuple[FieldDeclaration, ...] = ()
    schema: str | None = None
    # Table-level context, used when it is a property of the whole table rather
    # than of one column.
    business_owner: str | None = None
    retention: str | None = None

    def approved_columns(self) -> tuple[str, ...] | None:
        """The effective column allow-list; None means "all columns"."""
        if self.fields:
            return tuple(f.column for f in self.fields)
        return self.columns

    def declaration_for(self, column: str) -> FieldDeclaration | None:
        return next((f for f in self.fields if f.column == column), None)


@dataclass
class AdapterConfig:
    source_name: str                      # logical name Consiva shows, e.g. 'prepmyevent.com'
    consiva_base_url: str                 # e.g. 'https://api.consiva.ai'
    integration_key: str                  # 'csv_<prefix>_<secret>' -- from POST /api/v1/ropa/integration-keys
    allow_list: tuple[TableAllowList, ...]
    org_id: str = "external"              # informational; the server derives the real org from the key
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    verify_tls: bool = True
    extra_headers: dict = field(default_factory=dict)


class AdapterError(Exception):
    """Any failure collecting or delivering evidence. Callers should treat this
    as non-fatal: if Consiva is unreachable, the host application must keep
    working normally."""


# ── READ + VALIDATE + TRANSFORM ─────────────────────────────────────────────────


def collect_evidence(engine, config: AdapterConfig, *, discovery_run_id: str | None = None) -> dict:
    """Build curated DiscoveryEvidence from a live SQLAlchemy engine.

    Uses `sqlalchemy.inspect()` only -- catalog reads, never row reads.
    """
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(engine)
    approved = {(a.schema, a.table): a for a in config.allow_list}

    tables: list[dict] = []
    columns: list[dict] = []
    relationships: list[dict] = []
    business_metadata: list[dict] = []
    table_local_ids: dict[tuple[str | None, str], str] = {}
    column_counter = 0

    for index, ((schema, table_name), rule) in enumerate(sorted(approved.items(), key=lambda kv: kv[0][1]), start=1):
        if not inspector.has_table(table_name, schema=schema):
            logger.warning("ROPA adapter: approved table %r does not exist; skipping", table_name)
            continue

        table_local_id = f"table-{index}"
        table_local_ids[(schema, table_name)] = table_local_id
        tables.append({
            "local_id": table_local_id,
            "source_local_id": "source-1",
            "schema_name": schema,
            "table_name": table_name,
        })

        approved_columns = rule.approved_columns()
        for column in inspector.get_columns(table_name, schema=schema):
            if approved_columns is not None and column["name"] not in approved_columns:
                continue  # not approved -- never leaves the customer's system
            column_counter += 1
            declaration = rule.declaration_for(column["name"])
            record = {
                "local_id": f"column-{column_counter}",
                "table_local_id": table_local_id,
                "column_name": column["name"],
                "data_type": str(column["type"]),
                "nullable": bool(column.get("nullable", True)),
                # Deliberately never populated by this adapter: sampling real
                # values, even redacted, is not needed for a ROPA and is the one
                # place customer PII could leak outward.
                "sample_pattern": None,
            }
            if declaration is not None:
                # Only send what was actually declared. A None stays absent, so
                # the agent treats it as unknown rather than as an assertion.
                if declaration.personal_data_category:
                    record["existing_classification"] = declaration.personal_data_category
                elif declaration.is_personal_data is False:
                    # An explicit "this is not personal data" is itself a
                    # decision worth carrying, not silence.
                    record["existing_classification"] = "Not Personal Data"
                if declaration.data_subject:
                    record["existing_data_subject"] = declaration.data_subject
                if declaration.purpose:
                    record["existing_purpose"] = declaration.purpose
            columns.append(record)

        if rule.business_owner or rule.retention:
            business_metadata.append({
                "local_id": f"meta-{len(business_metadata) + 1}",
                "subject_local_id": table_local_id,
                "business_owner": rule.business_owner,
                "retention_policy": rule.retention,
                "department": None,
            })

    relationship_counter = 0
    for (schema, table_name), _rule in sorted(approved.items(), key=lambda kv: kv[0][1]):
        from_id = table_local_ids.get((schema, table_name))
        if from_id is None:
            continue
        try:
            foreign_keys = inspector.get_foreign_keys(table_name, schema=schema)
        except Exception as exc:  # noqa: BLE001 -- FK introspection is best-effort
            logger.warning("ROPA adapter: could not read foreign keys for %r: %s", table_name, exc)
            continue
        for fk in foreign_keys:
            referred = (fk.get("referred_schema"), fk.get("referred_table"))
            to_id = table_local_ids.get(referred)
            if to_id is None:
                continue  # points at a table that isn't approved -- don't disclose it
            relationship_counter += 1
            relationships.append({
                "local_id": f"rel-{relationship_counter}",
                "from_table_local_id": from_id,
                "from_column": (fk.get("constrained_columns") or [""])[0],
                "to_table_local_id": to_id,
                "to_column": (fk.get("referred_columns") or [""])[0],
                "constraint_name": fk.get("name"),
            })

    return {
        "org_id": config.org_id,
        "discovery_run_id": discovery_run_id or f"adapter-{int(time.time())}",
        "sources": [{
            "local_id": "source-1",
            "name": config.source_name,
            "source_type": "database",
            "connector": "integration_adapter",
            "location": None,  # never disclose internal host/IP
        }],
        "tables": tables,
        "columns": columns,
        "relationships": relationships,
        "business_metadata": business_metadata,
    }


# ── SEND ────────────────────────────────────────────────────────────────────────


def build_payload(
    evidence: dict,
    config: AdapterConfig,
    *,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Wrap curated evidence in the versioned wire contract.

    Mirrors app/agents/ropa/schemas/payload.py's SourcePayload. Kept as a plain
    dict (not a pydantic import) so this SDK stays copy-pasteable into a foreign
    codebase with no Consiva dependency.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "source_name": config.source_name,
        "correlation_id": correlation_id or new_correlation_id(),
        "generated_at": _utc_now_iso(),
        "adapter_version": ADAPTER_VERSION,
        "evidence": evidence,
        "idempotency_key": idempotency_key,
    }


def new_correlation_id() -> str:
    """One id threaded through adapter logs, the receiving API, the audit trail
    and the resulting run -- so a push can be traced across all three systems."""
    return f"pme-{uuid.uuid4().hex[:16]}"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def push_evidence(
    evidence: dict,
    config: AdapterConfig,
    *,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """POST the versioned payload to Consiva over authenticated HTTPS, with retries.

    `idempotency_key` makes a retry safe: the server returns the SAME run rather
    than starting a duplicate one.
    """
    if config.verify_tls and not config.consiva_base_url.startswith("https://"):
        raise AdapterError("consiva_base_url must be https:// (set verify_tls=False only for local testing)")

    payload = build_payload(
        evidence, config, correlation_id=correlation_id, idempotency_key=idempotency_key
    )
    url = config.consiva_base_url.rstrip("/") + "/api/v1/ropa/evidence"
    body = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.integration_key}",
        "User-Agent": f"consiva-ropa-adapter/{ADAPTER_VERSION}",
        # Echoed in Consiva's logs and audit trail for cross-system tracing.
        "X-Correlation-Id": payload["correlation_id"],
        **config.extra_headers,
    }

    last_error: str | None = None
    for attempt in range(1, config.max_retries + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code not in _RETRYABLE_STATUS:
                # The key/payload is wrong; retrying repeats the same rejection.
                raise AdapterError(f"Consiva rejected the push (HTTP {exc.code}): {detail}") from exc
            last_error = f"HTTP {exc.code}: {detail}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < config.max_retries:
            backoff = 2 ** (attempt - 1)
            logger.warning("ROPA adapter push attempt %d/%d failed (%s); retrying in %ds",
                           attempt, config.max_retries, last_error, backoff)
            time.sleep(backoff)

    raise AdapterError(f"push failed after {config.max_retries} attempts: {last_error}")


def run_adapter(
    engine,
    config: AdapterConfig,
    *,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict | None:
    """Collect and push in one call.

    Returns the run summary on success, or None on failure -- it never raises,
    so a ROPA outage can never take down the host application. The requirement
    is explicit: if Consiva is unavailable, the customer's app keeps working.
    """
    correlation_id = correlation_id or new_correlation_id()
    try:
        evidence = collect_evidence(engine, config)
        result = push_evidence(
            evidence, config, correlation_id=correlation_id, idempotency_key=idempotency_key
        )
        logger.info(
            "ROPA adapter [%s]: pushed %d tables / %d columns for %s",
            correlation_id, len(evidence["tables"]), len(evidence["columns"]), config.source_name,
        )
        return result
    except AdapterError as exc:
        logger.error("ROPA adapter [%s] failed (host application unaffected): %s", correlation_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001 -- must never propagate into the host app
        logger.exception(
            "ROPA adapter [%s] crashed unexpectedly (host application unaffected): %s",
            correlation_id, exc,
        )
        return None
