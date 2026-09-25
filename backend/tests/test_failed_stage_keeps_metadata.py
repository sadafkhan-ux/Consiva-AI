"""A stage that fails must keep what it observed before failing.

`track_stage` gives each stage a `meta` dict, collects whatever it writes there, and
passes it to the repository on the way out. Only `complete_stage` ever stored it, so
on the failure path the whole dict was silently dropped -- and the failure path is the
one worth instrumenting.

Measured on a real prepmyevent.com run: `llm_analysis` failed after 548 seconds and the
stored row had a duration and the string "Request timed out." Everything the node had
already recorded -- `provider_failure`, `degraded_to_rule_findings`, `prompt_chars`,
`rag_chunks_used`, `evidence_stats` -- existed in memory at the moment of failure and
reached the database on no run at all. Diagnosing that scan meant re-deriving the
prompt by hand instead of reading its own audit trail.
"""

import uuid

import pytest

from app.db.repositories import stage_repository
from app.observability import stage_tracker


class _Row:
    def __init__(self):
        self.status = "running"
        self.completed_at = None
        self.duration_ms = None
        self.error = None
        self.stage_metadata = None


class _Session:
    def __init__(self, row):
        self._row = row

    async def get(self, _model, _id):
        return self._row

    async def flush(self):
        return None


async def test_fail_stage_persists_the_metadata_it_is_given():
    """The bug, as one assertion."""
    row = _Row()
    await stage_repository.fail_stage(
        _Session(row), uuid.uuid4(), duration_ms=548_359, error="Request timed out.",
        metadata={"provider_failure": "APITimeoutError", "degraded_to_rule_findings": True},
    )
    assert row.status == "failed"
    assert row.error == "Request timed out."
    assert row.stage_metadata == {
        "provider_failure": "APITimeoutError", "degraded_to_rule_findings": True,
    }


async def test_metadata_stays_optional_for_existing_callers():
    """Defaulted rather than required, so nothing that already called this breaks."""
    row = _Row()
    await stage_repository.fail_stage(
        _Session(row), uuid.uuid4(), duration_ms=10, error="boom",
    )
    assert row.status == "failed"
    assert row.stage_metadata is None


async def test_an_empty_metadata_dict_does_not_overwrite(monkeypatch):
    """A stage that recorded nothing should leave the column alone rather than
    replacing a value with {}."""
    row = _Row()
    row.stage_metadata = {"set_at_start": True}
    await stage_repository.fail_stage(
        _Session(row), uuid.uuid4(), duration_ms=10, error="boom", metadata={},
    )
    assert row.stage_metadata == {"set_at_start": True}


async def test_track_stage_forwards_metadata_on_the_failure_path(monkeypatch):
    """End of the wiring: what the stage body wrote has to reach fail_stage.

    Exercises the real context manager, with only the two repository writes replaced,
    because the gap was in the hand-off between them and not in either one alone.
    """
    captured: dict = {}

    async def fake_start(db, *, scan_id, agent_run_id, stage):
        return type("R", (), {"id": uuid.uuid4()})()

    async def fake_fail(db, stage_id, *, duration_ms, error, metadata=None):
        captured["error"] = error
        captured["metadata"] = metadata

    async def fake_complete(db, stage_id, *, duration_ms, metadata):
        captured["completed"] = True

    monkeypatch.setattr(stage_repository, "start_stage", fake_start)
    monkeypatch.setattr(stage_repository, "fail_stage", fake_fail)
    monkeypatch.setattr(stage_repository, "complete_stage", fake_complete)

    with pytest.raises(RuntimeError):
        async with stage_tracker.track_stage(uuid.uuid4(), "llm_analysis") as meta:
            meta["prompt_chars"] = 10_426
            meta["degraded_to_rule_findings"] = True
            raise RuntimeError("Request timed out.")

    assert captured["error"] == "Request timed out."
    assert captured["metadata"] == {"prompt_chars": 10_426, "degraded_to_rule_findings": True}
    assert "completed" not in captured, "a failed stage must not also be recorded as completed"


async def test_the_exception_still_propagates():
    """track_stage records the failure; it must never swallow it. The graph routes on
    that exception reaching llm_reasoning's wrapper."""
    with pytest.raises(ValueError):
        async with stage_tracker.track_stage(uuid.uuid4(), "llm_analysis"):
            raise ValueError("boom")
