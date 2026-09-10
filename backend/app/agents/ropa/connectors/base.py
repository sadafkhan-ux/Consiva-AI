"""Generic connector framework: the contract every ROPA data source implements,
plus the registry that maps a source-type string to its connector.

The point is that the ROPA pipeline never imports a specific connector. It asks
the registry for one by name and gets back something that returns
DiscoveryEvidence, so adding PrepMyEvent (or any SaaS/API/file source) is a new
module plus a `register()` call, not a change to the agent.

Every connector is READ-ONLY by contract -- see `SourceConnector.discover`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.agents.ropa.schemas.evidence import DiscoveryEvidence


class ConnectorError(Exception):
    """Base for any failure that stops a connector before it can return evidence:
    bad credentials, an over-privileged account, an unreachable host, a timeout,
    or a source whose response doesn't match its declared shape."""


class ConnectorTimeout(ConnectorError):
    """The source did not answer inside its configured budget."""


class ConnectorAuthError(ConnectorError):
    """Credentials were rejected, or the credential is too privileged to be used
    for read-only discovery."""


@runtime_checkable
class SourceConnector(Protocol):
    """A connector reads metadata from ONE authorized source and returns evidence.

    Implementations must never write to, modify, or delete anything in the source
    -- discovery is strictly READ -> VALIDATE -> TRANSFORM -> SEND, and the
    credential handed to a connector is expected to be least-privilege.
    """

    source_type: str  # "database" | "api" | "file" | "application"
    connector_name: str  # "postgres" | "rest_api" | ...

    async def discover(self, *, org_id: str, source_name: str) -> DiscoveryEvidence: ...


_REGISTRY: dict[str, type] = {}


def register(connector_name: str, connector_cls: type) -> None:
    """Register a connector class under the name used in source configuration."""
    _REGISTRY[connector_name] = connector_cls


def get_connector_class(connector_name: str) -> type:
    try:
        return _REGISTRY[connector_name]
    except KeyError:
        raise ConnectorError(
            f"unknown connector {connector_name!r}; registered: {sorted(_REGISTRY)}"
        ) from None


def registered_connectors() -> list[str]:
    return sorted(_REGISTRY)
