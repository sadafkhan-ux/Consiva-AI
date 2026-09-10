"""Generic authenticated read-only REST connector.

This is the plug-in point for SaaS/application sources (PrepMyEvent being the
motivating example): a source is described by CONFIGURATION -- base URL, auth,
and which endpoints to introspect -- rather than by a bespoke module per vendor.

What it deliberately does NOT do:

- It never sends anything but GET. A source cannot be modified through this
  connector even by misconfiguration (see `_ALLOWED_METHOD`).
- It never stores raw field VALUES as evidence. A response is reduced to field
  names, inferred types, and (optionally) redacted shape-only patterns, the same
  privacy model the Postgres connector uses. Sending real personal data into the
  ROPA agent would mean copying PII into a second system in order to do privacy
  compliance -- exactly what the agent exists to flag.

Configure an endpoint with `sample_path` pointing at a LIST endpoint; the
connector reads one page, infers the field shape, and discards the payload.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agents.ropa.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorTimeout,
    register,
)
from app.agents.ropa.schemas.evidence import (
    ApiEndpointRecord,
    ColumnRecord,
    DiscoveryEvidence,
    SourceRecord,
    TableRecord,
)

_ALLOWED_METHOD = "GET"
# How many records to look at when inferring a collection's field shape. More
# than one because the first record may have null/absent optional fields.
_SHAPE_SAMPLE_RECORDS = 5


@dataclass(frozen=True)
class ApiEndpointConfig:
    """One collection to introspect. `name` becomes the logical 'table'."""

    name: str
    path: str
    # Dotted path to the list inside the response body, e.g. "data.items".
    # None means the body itself is the list.
    records_key: str | None = None


@dataclass(frozen=True)
class ApiConnectionConfig:
    base_url: str
    endpoints: tuple[ApiEndpointConfig, ...]
    # Sent as `Authorization: <auth_scheme> <api_key>` when api_key is set.
    api_key: str | None = None
    auth_scheme: str = "Bearer"
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 15.0
    collect_sample_patterns: bool = False
    verify_tls: bool = True


def _redact(value: Any) -> str | None:
    """Reduce a value to a shape-only pattern: digits -> '#', letters -> 'x'.
    Identical policy to the Postgres connector's `_sample_pattern`."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value)[:64]
    text = re.sub(r"[0-9]", "#", text)
    return re.sub(r"[A-Za-z]", "x", text)


def _infer_type(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _extract_records(body: Any, records_key: str | None) -> list[dict]:
    node = body
    if records_key:
        for part in records_key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ConnectorError(f"records_key {records_key!r} not found in response")
            node = node[part]
    if isinstance(node, dict):
        node = [node]
    if not isinstance(node, list):
        raise ConnectorError("expected a list of records in the response body")
    return [r for r in node if isinstance(r, dict)]


class ApiConnector:
    """SourceConnector over a read-only authenticated REST API."""

    source_type = "api"
    connector_name = "rest_api"

    def __init__(self, config: ApiConnectionConfig) -> None:
        self._config = config

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", **self._config.extra_headers}
        if self._config.api_key:
            headers["Authorization"] = f"{self._config.auth_scheme} {self._config.api_key}".strip()
        return headers

    async def discover(self, *, org_id: str, source_name: str) -> DiscoveryEvidence:
        config = self._config
        source_local_id = "source-1"
        tables: list[TableRecord] = []
        columns: list[ColumnRecord] = []
        endpoints: list[ApiEndpointRecord] = []
        column_counter = 0

        async with httpx.AsyncClient(
            base_url=config.base_url,
            headers=self._headers(),
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            follow_redirects=True,
        ) as client:
            for i, endpoint in enumerate(config.endpoints, start=1):
                records = await self._fetch(client, endpoint)

                table_local_id = f"table-{i}"
                tables.append(
                    TableRecord(
                        local_id=table_local_id,
                        source_local_id=source_local_id,
                        schema_name=None,
                        table_name=endpoint.name,
                    )
                )

                # Union the keys across a few records so optional fields absent
                # from the first record are still discovered.
                shape: dict[str, Any] = {}
                for record in records[:_SHAPE_SAMPLE_RECORDS]:
                    for key, value in record.items():
                        if key not in shape or shape[key] is None:
                            shape[key] = value

                for field_name, sample_value in shape.items():
                    column_counter += 1
                    columns.append(
                        ColumnRecord(
                            local_id=f"column-{column_counter}",
                            table_local_id=table_local_id,
                            column_name=field_name,
                            data_type=_infer_type(sample_value),
                            nullable=sample_value is None,
                            sample_pattern=_redact(sample_value) if config.collect_sample_patterns else None,
                        )
                    )

                endpoints.append(
                    ApiEndpointRecord(
                        local_id=f"endpoint-{i}",
                        source_local_id=source_local_id,
                        method=_ALLOWED_METHOD,
                        route=endpoint.path,
                        request_fields=[],
                        response_fields=sorted(shape),
                    )
                )

        return DiscoveryEvidence(
            org_id=org_id,
            discovery_run_id=str(uuid.uuid4()),
            sources=[
                SourceRecord(
                    local_id=source_local_id,
                    name=source_name,
                    source_type="api",
                    connector=self.connector_name,
                    location=config.base_url,
                )
            ],
            tables=tables,
            columns=columns,
            api_endpoints=endpoints,
        )

    async def _fetch(self, client: httpx.AsyncClient, endpoint: ApiEndpointConfig) -> list[dict]:
        try:
            response = await client.request(_ALLOWED_METHOD, endpoint.path)
        except httpx.TimeoutException as exc:
            raise ConnectorTimeout(f"{endpoint.path} timed out after {self._config.timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise ConnectorError(f"{endpoint.path} request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise ConnectorAuthError(
                f"{endpoint.path} rejected the credential (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise ConnectorError(f"{endpoint.path} returned HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ConnectorError(f"{endpoint.path} did not return JSON") from exc

        return _extract_records(body, endpoint.records_key)


register("rest_api", ApiConnector)
