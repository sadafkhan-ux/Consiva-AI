"""A scan whose results the scan-data cleanup deleted must say so (410), never answer with empty tables that read as "no findings" or
"every parameter failed". Only the full cleanup's marker counts: the lighter retention pass keeps every result."""

from datetime import datetime, timezone

import pytest

from app.cleanup.core import Settings
from app.enterprise import errors, scans
from app.enterprise.api.results import cleared_message
from app.enterprise.models import EntScan


def _scan(purge_summary=None, cleared_at=None) -> EntScan:
    return EntScan(purge_summary=purge_summary, text_cleared_at=cleared_at)


def test_full_cleanup_marker_means_cleared():
    assert scans.results_cleared(_scan({"cleared_by": scans.FULL_CLEAR_MARK, "kept": "scan id, website, status, times"}))


@pytest.mark.parametrize("summary", [None, {}, {"evidence_bytes_cleared": 1234, "pages_cleared": 40}, {"cleared_by": "something else"}])
def test_retention_pass_or_nothing_is_not_cleared(summary):
    # retention.py also stamps text_cleared_at, but keeps findings, assessments, scores and reports
    assert not scans.results_cleared(_scan(summary, datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)))


def test_marker_matches_what_the_cleanup_wrote_before():
    # scans cleared by the earlier code carry this literal; they must keep being recognised
    assert scans.FULL_CLEAR_MARK == "local scan-data cleanup"


def test_cleared_is_410_with_the_date():
    assert errors.ScanResultsCleared.status_code == 410
    msg = cleared_message(_scan({"cleared_by": scans.FULL_CLEAR_MARK}, datetime(2026, 11, 20, 12, 36, tzinfo=timezone.utc)))
    assert "20 Nov 2026 12:36 UTC" in msg and "history" in msg


def test_cleanup_grace_defaults_to_24_hours(monkeypatch):
    monkeypatch.delenv("CLEANUP_GRACE_MINUTES", raising=False)
    assert Settings.from_env().grace_minutes == 1440
    monkeypatch.setenv("CLEANUP_GRACE_MINUTES", "30")
    assert Settings.from_env().grace_minutes == 30
