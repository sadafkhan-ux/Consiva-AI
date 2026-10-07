"""Unit tests for the Postgres connector's error handling, isolated from any
live database via monkeypatching -- these must never need real infra.
"""

from __future__ import annotations

import asyncpg
import pytest

from app.agents.ropa.connectors import postgres
from app.agents.ropa.connectors.base import ConnectorError


class _FakeConnection:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_discover_wraps_a_mid_read_postgres_error():
    """A connection drop or server error occurring AFTER connect_source()
    succeeds -- i.e. while discover_metadata() is reading tables/columns/
    relationships -- must still surface as a ConnectorError, not a raw
    asyncpg/OSError. Otherwise it escapes
    ropa_run_service.execute_queued_discovery's `except (ConnectorError,
    ValueError)` uncaught, and the run is never marked failed -- it stays at
    status="discovering" forever, indistinguishable from one still genuinely
    in progress. (Same bug class _connect() was already fixed for; this is
    the schema-read call site.)
    """
    fake_conn = _FakeConnection()

    async def _fake_connect_source(config):
        return fake_conn

    async def _fake_discover_metadata(conn, config):
        raise asyncpg.PostgresError("connection reset by peer mid-read")

    from unittest import mock

    with (
        mock.patch.object(postgres, "connect_source", _fake_connect_source),
        mock.patch.object(postgres, "discover_metadata", _fake_discover_metadata),
    ):
        config = postgres.PostgresConnectionConfig(
            host="db.internal", port=5432, dbname="app", user="ro_user", password="x",
        )
        with pytest.raises(ConnectorError):
            await postgres.discover(org_id="org-1", source_name="app-db", config=config)

    assert fake_conn.closed, "connection must still be closed even when discovery fails"


@pytest.mark.asyncio
async def test_discover_wraps_a_mid_read_os_error():
    fake_conn = _FakeConnection()

    async def _fake_connect_source(config):
        return fake_conn

    async def _fake_discover_metadata(conn, config):
        raise OSError("network unreachable")

    from unittest import mock

    with (
        mock.patch.object(postgres, "connect_source", _fake_connect_source),
        mock.patch.object(postgres, "discover_metadata", _fake_discover_metadata),
    ):
        config = postgres.PostgresConnectionConfig(
            host="db.internal", port=5432, dbname="app", user="ro_user", password="x",
        )
        with pytest.raises(ConnectorError):
            await postgres.discover(org_id="org-1", source_name="app-db", config=config)

    assert fake_conn.closed
