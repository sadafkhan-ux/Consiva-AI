"""Reads the SHAPE of a REST API. Never its records.

WHY THIS READS A SPEC AND NOT AN ENDPOINT

The obvious way to learn what an API holds is to call it and look at what comes back.
This connector refuses to, for the same reason `structured.py` reads
`information_schema` instead of selecting rows: a response body from a customer's API
is their users' personal data, and fetching it would mean this agent holding personal
data in order to decide whether personal data is being held properly. That trade buys a
marginal gain in field detection and costs the one property that makes the agent
trustworthy.

An OpenAPI document is the honest analogue of `information_schema`. It is pure shape --
schema names and property names, written by the organisation itself, containing no
records by construction. If a source has no spec, this connector says so and reads
nothing; it does not fall back to calling endpoints.

WHAT MAPS TO WHAT

An OpenAPI schema becomes a `Table` and its properties become `Column`s, so everything
downstream -- the personal-data heuristic, the purpose rules, the comparison layer --
works unchanged on a REST source. A schema called `Customer` with an `email` property
is treated exactly like a `customers` table with an `email` column, because for the
question this agent asks they are the same claim.

SECURITY POSTURE

The URL passes the same SSRF guard every other outbound fetch in this platform uses, so
a spec URL cannot be pointed at cloud metadata or a private network. Any credential is
supplied per run, sent once, never stored on the run record, never logged, and stripped
from every error this module raises.
"""

from __future__ import annotations

import json

import httpx

from app.agents.purpose.connectors.structured import (
    _MAX_COLUMNS,
    _MAX_TABLES,
    Column,
    ConnectorError,
    StructuredSchema,
    Table,
    _safe,
)
from app.core.exceptions import ConsivaError
from app.scanner.url_safety import assert_safe_url

# A spec is a document, not a download. 8 MiB is far beyond any real OpenAPI file and
# still small enough that a misconfigured URL pointing at something huge fails fast
# rather than filling the worker's memory.
_MAX_SPEC_BYTES = 8 * 1024 * 1024
_TIMEOUT_SECONDS = 20.0

# How deep to follow `$ref` when resolving a property's type. One hop covers the
# ordinary `#/components/schemas/X` case; beyond that the gain is nil for this agent,
# which only ever needs the NAME of a field, and the cost is a cycle-detection problem.
_REF_PREFIXES = ("#/components/schemas/", "#/definitions/")


def _schemas_from(spec: dict) -> dict:
    """The schema dictionary, from either OpenAPI 3 or Swagger 2.

    Both are still in the wild and the only difference that matters here is where the
    schemas live, so supporting both costs one line and avoids telling a customer their
    perfectly valid spec is unreadable.
    """
    if not isinstance(spec, dict):
        return {}
    components = spec.get("components")
    if isinstance(components, dict) and isinstance(components.get("schemas"), dict):
        return components["schemas"]
    if isinstance(spec.get("definitions"), dict):  # Swagger 2
        return spec["definitions"]
    return {}


def _resolve(node: dict, schemas: dict) -> dict:
    """Follow a single `$ref` into the schema dictionary, or return the node as-is."""
    if not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    for prefix in _REF_PREFIXES:
        if ref.startswith(prefix):
            target = schemas.get(ref[len(prefix):])
            # Only one hop: a resolved node that is itself a $ref is left alone rather
            # than chased, so a spec with a reference cycle cannot hang the worker.
            return target if isinstance(target, dict) else {}
    return {}


def _type_of(node: dict) -> str:
    """A readable type name, used only for display and never for a judgement.

    The purpose rules read NAMES. This exists so a reviewer looking at the evidence can
    see what kind of field it was, which is why an unresolvable type is reported as
    "unknown" rather than guessed at.
    """
    node = node if isinstance(node, dict) else {}
    declared = node.get("type")
    if isinstance(declared, str):
        if declared == "array":
            inner = node.get("items")
            inner_type = _type_of(inner) if isinstance(inner, dict) else "unknown"
            return f"array[{inner_type}]"
        return declared
    if node.get("$ref"):
        return "object"
    # `anyOf`/`oneOf`/`allOf` describe a union this module has no need to flatten.
    for keyword in ("anyOf", "oneOf", "allOf"):
        if isinstance(node.get(keyword), list):
            return "union"
    return "unknown"


def parse_openapi(document: str | dict, *, source_name: str) -> StructuredSchema:
    """Turn an OpenAPI/Swagger document into the same shape a database read produces.

    Separated from the fetch so it can be tested, and called directly when a spec is
    pasted in rather than fetched.
    """
    if isinstance(document, str):
        try:
            spec = json.loads(document)
        except (ValueError, TypeError) as exc:
            raise ConnectorError(
                "The document could not be parsed as JSON. YAML specs are not read "
                f"here -- convert it to JSON first. ({_safe(exc)})"
            ) from exc
    else:
        spec = document

    schemas = _schemas_from(spec)
    if not schemas:
        raise ConnectorError(
            "The document parsed, but contains no schema definitions "
            "(components.schemas for OpenAPI 3, definitions for Swagger 2). There is "
            "nothing to assess -- a spec describing only paths says what the API does, "
            "not what data it holds."
        )

    result = StructuredSchema(source_name=source_name)
    columns_seen = 0

    for schema_name, node in schemas.items():
        if len(result.tables) >= _MAX_TABLES:
            result.truncated = True
            break

        node = _resolve(node, schemas) if isinstance(node, dict) and "$ref" in node else node
        properties = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(properties, dict) or not properties:
            # A schema with no properties -- an enum, a bare string alias, an empty
            # object -- describes no fields, so there is nothing to classify. Skipped
            # rather than recorded as a table with no columns, which would read as
            # "we looked and found nothing personal" when we never looked at all.
            continue

        columns: list[Column] = []
        required = node.get("required")
        required = set(required) if isinstance(required, list) else set()

        for prop_name, prop_node in properties.items():
            if columns_seen >= _MAX_COLUMNS:
                result.truncated = True
                break
            resolved = _resolve(prop_node, schemas) if isinstance(prop_node, dict) else {}
            columns.append(Column(
                name=str(prop_name),
                data_type=_type_of(resolved or prop_node),
                nullable=str(prop_name) not in required,
            ))
            columns_seen += 1

        if columns:
            # `schema` is "openapi" rather than "public" so `Table.qualified` keeps the
            # source visible: a REST schema named `Customer` and a database table named
            # `customer` are different subjects, and an assessment that silently merged
            # them would attribute one system's declared purpose to another's data.
            result.tables.append(Table(schema="openapi", name=str(schema_name), columns=columns))

        if result.truncated:
            break

    if not result.tables:
        raise ConnectorError(
            "No schema in the document defines any properties, so no fields could be "
            "read. Nothing was assessed."
        )
    return result


async def read_openapi(
    url: str, *, source_name: str, auth_header: str | None = None
) -> StructuredSchema:
    """Fetch an OpenAPI document and read its shape.

    Raises ConnectorError with a credential-safe message on any failure. The header
    value is never included in an error, a log line, or the run record -- it is read
    once, sent once, and discarded with this function's frame.
    """
    if not url or not url.strip():
        raise ConnectorError("No specification URL was supplied.")

    # The same guard every other outbound fetch uses: rejects private, loopback,
    # link-local and cloud-metadata addresses, and re-checks after DNS resolution.
    # A spec URL is caller-supplied and therefore exactly the SSRF shape this exists
    # for -- an internal admin API would otherwise be one request away.
    try:
        await assert_safe_url(url.strip())
    except ConsivaError as exc:
        raise ConnectorError(
            f"That specification URL cannot be fetched: {_safe(exc)}"
        ) from exc

    headers = {"Accept": "application/json", "User-Agent": "Consiva-PurposeClassifier/1.0"}
    if auth_header and auth_header.strip():
        headers["Authorization"] = auth_header.strip()

    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS,
            # Redirects are NOT followed. A redirect would land on a URL that never
            # passed the guard above, which is the standard way an SSRF check is walked
            # around; a moved spec is a thing the caller can point at directly.
            follow_redirects=False,
        ) as client:
            response = await client.get(url.strip(), headers=headers)
    except httpx.HTTPError as exc:
        raise ConnectorError(f"The specification could not be fetched: {_safe(exc)}") from exc

    if response.status_code >= 400:
        # The body is deliberately not echoed. An error page from an authenticated
        # endpoint can contain anything, and this string is stored on a run record.
        raise ConnectorError(
            f"The specification URL returned HTTP {response.status_code}."
            + (" The credential supplied was not accepted."
               if response.status_code in (401, 403) else "")
        )

    if len(response.content) > _MAX_SPEC_BYTES:
        raise ConnectorError(
            f"The specification is larger than {_MAX_SPEC_BYTES // (1024 * 1024)} MiB "
            "and was not read."
        )

    return parse_openapi(response.text, source_name=source_name)
