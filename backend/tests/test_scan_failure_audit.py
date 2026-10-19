"""A scan that fails is recorded as failed in the audit trail, with the stage and the reason.

The stage row already said where a scan stopped, but the Audit Timeline -- the place a reviewer looks --
showed nothing for a scan that had timed out. These drive the real `execute_scan_and_persist` with the
engine stubbed (no browser, no database).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ScanTimeoutError
from app.services import scan_service
from tests.test_scan_profiles import _Row, _wire_worker

TIMEOUT = "Scan of https://smartians.ai/ exceeded its 1800s budget and was aborted so the worker could continue with other jobs."


def _row_with_org(org_id):
    row = _Row({}, status="queued")
    row.org_id = org_id
    return row


@pytest.fixture
def audit(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(scan_service.audit_service, "record", spy)
    failed = AsyncMock()
    monkeypatch.setattr(scan_service.scan_repository, "mark_scan_failed", failed)
    return SimpleNamespace(record=spy, mark_failed=failed)


async def test_a_timed_out_website_scan_is_audited_with_its_stage_and_reason(monkeypatch, audit):
    org, scan_id = uuid.uuid4(), uuid.uuid4()
    _wire_worker(monkeypatch, _row_with_org(org), run=AsyncMock(side_effect=ScanTimeoutError(TIMEOUT)))

    with pytest.raises(ScanTimeoutError):
        await scan_service.execute_scan_and_persist(scan_id, "https://smartians.ai/")

    audit.mark_failed.assert_awaited_once()
    audit.record.assert_awaited_once()
    kwargs = audit.record.await_args.kwargs
    assert kwargs["action"] == "scan.failed"
    assert kwargs["entity_type"] == "consent_scan" and kwargs["entity_id"] == scan_id
    assert kwargs["org_id"] == org
    assert kwargs["after"]["stage"] == "website_scan"
    assert kwargs["after"]["error_type"] == "ScanTimeoutError"
    assert "exceeded its 1800s budget" in kwargs["after"]["error"]


async def test_a_failure_while_classifying_names_that_stage(monkeypatch, audit):
    _wire_worker(monkeypatch, _row_with_org(uuid.uuid4()))
    monkeypatch.setattr(scan_service, "classify_scan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad lookup")))

    with pytest.raises(RuntimeError):
        await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://example.com/")

    assert audit.record.await_args.kwargs["after"]["stage"] == "classification"


async def test_a_failure_while_saving_names_data_structuring(monkeypatch, audit):
    _wire_worker(monkeypatch, _row_with_org(uuid.uuid4()))
    monkeypatch.setattr(scan_service.scan_repository, "save_scan_result", AsyncMock(side_effect=RuntimeError("db down")))

    with pytest.raises(RuntimeError):
        await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://example.com/")

    assert audit.record.await_args.kwargs["after"]["stage"] == "data_structuring"


async def test_a_scan_that_succeeds_writes_no_failure_entry(monkeypatch, audit):
    _wire_worker(monkeypatch, _row_with_org(uuid.uuid4()))

    await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://example.com/")

    audit.record.assert_not_awaited()
    audit.mark_failed.assert_not_awaited()


async def test_an_audit_write_that_fails_does_not_replace_the_scans_own_error(monkeypatch, audit):
    audit.record.side_effect = RuntimeError("audit store unavailable")
    _wire_worker(monkeypatch, _row_with_org(uuid.uuid4()), run=AsyncMock(side_effect=ScanTimeoutError(TIMEOUT)))

    with pytest.raises(ScanTimeoutError):          # still the scan's error, not the audit's
        await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://smartians.ai/")

    audit.mark_failed.assert_awaited_once()


async def test_a_scan_row_with_no_organisation_is_still_failed_without_crashing(monkeypatch, audit):
    _wire_worker(monkeypatch, _Row({}, status="queued"), run=AsyncMock(side_effect=ScanTimeoutError(TIMEOUT)))

    with pytest.raises(ScanTimeoutError):
        await scan_service.execute_scan_and_persist(uuid.uuid4(), "https://smartians.ai/")

    audit.mark_failed.assert_awaited_once()
    audit.record.assert_not_awaited()
