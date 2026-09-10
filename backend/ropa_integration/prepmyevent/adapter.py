"""PrepMyEvent ROPA integration adapter -- the first implementation of the
generic SDK in ../ropa_adapter_sdk.py.

DEPLOYMENT: this file plus `ropa_adapter_sdk.py` are copied into PrepMyEvent's
own backend and run THERE, on the VM that already has database access. Nothing
about their infrastructure is exposed:

  * PostgreSQL stays on localhost:5432 -- no external listener, no firewall
    change, no new database user
  * Consiva never receives their DATABASE_URL, their DB password, or their PII
    encryption keys
  * only the curated metadata in ALLOW_LIST leaves the VM, over outbound HTTPS
    (which their environment already permits)

Known target environment (confirmed): PostgreSQL 16, database `event_flow`,
Python + FastAPI backend under systemd on port 8007, SQLAlchemy access via
`DATABASE_URL`, existing scripts run inside `backend/` and reuse
`backend.database`.

USAGE (from inside PrepMyEvent's backend directory):

    CONSIVA_INTEGRATION_KEY=csv_xxx_yyy \
    CONSIVA_BASE_URL=https://<the host Consiva gave you> \
    python -m ropa_integration.prepmyevent.adapter

Add `--dry-run` to print exactly what WOULD be sent without sending anything --
run that first, and have their team read the output before enabling delivery.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# When deployed inside PrepMyEvent's backend these live alongside each other;
# this import works both there and in this repository.
try:
    from ropa_integration.ropa_adapter_sdk import (
        AdapterConfig,
        FieldDeclaration,
        TableAllowList,
        collect_evidence,
        run_adapter,
    )
except ImportError:  # pragma: no cover - deployment layout fallback
    from ropa_adapter_sdk import (
        AdapterConfig,
        FieldDeclaration,
        TableAllowList,
        collect_evidence,
        run_adapter,
    )

logger = logging.getLogger(__name__)

SOURCE_NAME = "prepmyevent.com"

# ── The approved surface ─────────────────────────────────────────────────────────
# Every table PrepMyEvent's research identified as ROPA-relevant. Columns are
# NOT enumerated here (columns=None) because this adapter only ever transmits
# column NAMES and TYPES, never values -- and a ROPA needs to know that an
# `email` column exists in order to record it as Contact Data.
#
# To tighten further, replace `columns=None` with an explicit tuple; anything
# not listed then stays invisible to Consiva even if it exists.
#
# Deliberately EXCLUDED and worth stating plainly:
#   * anything holding encrypted PII blobs or key material
#   * OAuth/SMTP tokens on connected_inboxes (credentials, not ROPA evidence)
#   * message bodies -- a ROPA records that email content is processed, not the
#     content itself
ALLOW_LIST: tuple[TableAllowList, ...] = (
    TableAllowList(table="leads"),
    TableAllowList(table="events"),
    TableAllowList(table="attendees"),
    TableAllowList(table="campaigns"),
    TableAllowList(table="emails"),
    TableAllowList(table="email_events"),
    TableAllowList(table="users"),
    TableAllowList(table="connected_inboxes"),
    TableAllowList(table="transactions"),
    TableAllowList(table="audit_logs"),
    # Alternate names the research also mentioned. A name that doesn't exist in
    # their schema is logged and skipped, never fatal -- so listing both
    # candidates is safe and avoids a round-trip to confirm naming.
    TableAllowList(table="event_attendees"),
    TableAllowList(table="outreach_campaigns"),
    TableAllowList(table="campaign_leads"),
    TableAllowList(table="generated_emails"),
    TableAllowList(table="pending_signups"),
    TableAllowList(table="credit_transactions"),
)

# ── Optional: PrepMyEvent's own ROPA declarations ────────────────────────────────
# Once their team confirms real column names (run `--dry-run` to list them), they
# can move a table from ALLOW_LIST into here to declare what THEY already know.
# Declared values arrive as ColumnRecord.existing_* and are treated by Consiva as
# authoritative -- rule order #1, never overwritten by inference.
#
# Left empty deliberately: inventing declarations for columns nobody has
# confirmed would be exactly the fabricated ROPA data the agent exists to
# prevent. This is a structure to fill in, not a guess to ship.
#
# Example of the intended shape (commented, not active):
#
#   TableAllowList(
#       table="attendees",
#       business_owner="Events Team",
#       retention="24 months",
#       fields=(
#           FieldDeclaration("email", is_personal_data=True,
#                            personal_data_category="Contact Data",
#                            data_subject="Attendee",
#                            purpose="Event Attendee Management",
#                            confidence=1.0),
#           FieldDeclaration("internal_score", is_personal_data=False),
#       ),
#   )
DECLARED_TABLES: tuple[TableAllowList, ...] = ()


def effective_allow_list() -> tuple[TableAllowList, ...]:
    """DECLARED_TABLES take precedence over the plain ALLOW_LIST entry for the
    same table, so filling one in never means remembering to delete the other."""
    declared_names = {t.table for t in DECLARED_TABLES}
    return (*DECLARED_TABLES, *(t for t in ALLOW_LIST if t.table not in declared_names))


def _engine():
    """Reuse PrepMyEvent's OWN database access rather than opening a second
    connection with separate credentials.

    Tries their existing `backend.database` module first (their scripts already
    do this), and falls back to building a read-only engine from DATABASE_URL.
    """
    try:
        from backend.database import engine  # type: ignore[import-not-found]

        logger.info("Using PrepMyEvent's existing backend.database engine")
        return engine
    except ImportError:
        pass

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "Could not import backend.database and DATABASE_URL is not set. "
            "Run this from inside the PrepMyEvent backend directory."
        )

    from sqlalchemy import create_engine

    logger.info("Using DATABASE_URL (read-only catalog access)")
    return create_engine(_sync_url(database_url), pool_pre_ping=True, pool_size=1, max_overflow=0)


def _sync_url(database_url: str) -> str:
    """Normalize a DATABASE_URL to a SYNC driver this environment actually has.

    SQLAlchemy's Inspector is synchronous, but a FastAPI project's DATABASE_URL
    is usually an async one (`postgresql+asyncpg://`). A bare `postgresql://`
    would then default to psycopg2, which many modern deployments no longer
    install -- so pick whichever sync driver is importable instead of assuming.
    """
    base = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if "+" in base.split("://", 1)[0]:
        return base  # an explicit sync driver was already specified

    for module, scheme in (("psycopg", "postgresql+psycopg://"), ("psycopg2", "postgresql+psycopg2://")):
        try:
            __import__(module)
            return base.replace("postgresql://", scheme, 1)
        except ImportError:
            continue
    return base  # let SQLAlchemy raise a clear driver error


def _require_base_url() -> str:
    """No default on purpose. A plausible-looking default that points at the wrong
    host is worse than none: it looks configured and then 404s on the first real
    push, which is a confusing failure to debug from the customer's side."""
    base_url = os.getenv("CONSIVA_BASE_URL")
    if not base_url:
        raise SystemExit(
            "CONSIVA_BASE_URL is not set. Consiva will tell you the exact host to use "
            "-- it is deployment-specific, so there is no safe default."
        )
    return base_url


def build_config() -> AdapterConfig:
    key = os.getenv("CONSIVA_INTEGRATION_KEY")
    if not key:
        raise SystemExit(
            "CONSIVA_INTEGRATION_KEY is not set. Obtain one from Consiva "
            "(POST /api/v1/ropa/integration-keys) and provide it via environment "
            "or your secret manager -- never commit it."
        )
    return AdapterConfig(
        source_name=SOURCE_NAME,
        consiva_base_url=_require_base_url(),
        integration_key=key,
        allow_list=effective_allow_list(),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the exact payload that would be sent, and send nothing",
    )
    parser.add_argument("--idempotency-key", default=None, help="makes a retried run return the same run")
    args = parser.parse_args()

    engine = _engine()

    if args.dry_run:
        # Deliberately does NOT require CONSIVA_INTEGRATION_KEY: their team must
        # be able to audit exactly what would leave the VM before any credential
        # is issued or any data is sent.
        config = AdapterConfig(
            source_name=SOURCE_NAME, consiva_base_url="https://example.invalid",
            integration_key="dry-run", allow_list=effective_allow_list(),
        )
        evidence = collect_evidence(engine, config)
        print(json.dumps(evidence, indent=2))
        print(
            f"\nDRY RUN -- nothing was sent. "
            f"{len(evidence['tables'])} tables, {len(evidence['columns'])} columns, "
            f"{len(evidence['relationships'])} relationships would be transmitted.\n"
            "No row values are included in the payload above.",
            file=sys.stderr,
        )
        return 0

    result = run_adapter(engine, build_config(), idempotency_key=args.idempotency_key)
    if result is None:
        # Non-zero so a scheduler notices, but the process never raised into the
        # host application.
        print("ROPA push failed -- see logs. PrepMyEvent itself is unaffected.", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
