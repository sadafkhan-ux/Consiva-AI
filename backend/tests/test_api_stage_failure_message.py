"""The integration API never shows a visitor the provider's raw exception.

consiva.ai's public scan widget showed: "Stage 'rag_retrieval' failed: Error code: 400 - {'error': 'This model does not
support image input as query'}". That is an internal provider error. The caller is told what happened and whether
anything was produced; the detail stays in the platform's own stage record.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import consent_agent_api_service as svc

RAW = "Error code: 400 - {'error': 'This model does not support image input as query'}"


def _stage(stage, status, error=None):
    return SimpleNamespace(stage=stage, status=status, error=error, stage_metadata={}, duration_ms=1)


def _scan():
    return SimpleNamespace(id=uuid.uuid4(), url="https://digitalglobal.ai", status="completed", error=None,
                           started_at=datetime.now(UTC), completed_at=None, scanner_version="1", scan_options={})


async def _status(monkeypatch, stages, run_status):
    async def fake_stages(db, scan_id):
        return stages

    async def fake_run(db, scan_id):
        return SimpleNamespace(status=run_status)

    monkeypatch.setattr(svc, "_stages", fake_stages)
    monkeypatch.setattr(svc.scan_repository, "latest_agent_run", fake_run)
    return await svc.get_status(None, _scan())


async def test_a_failed_retrieval_that_still_produced_findings_is_completed_and_explained_plainly(monkeypatch):
    stages = [_stage("website_scan", "completed"), _stage("rag_retrieval", "failed", RAW),
              _stage("rule_findings_generated", "completed")]

    out = await _status(monkeypatch, stages, "failed")

    assert out["status"] == "completed"
    assert out["error"]["code"] == "STAGE_FAILED" and out["error"]["recoverable"] is True
    assert "regulatory references" in out["error"]["message"] and "cite no legal provision" in out["error"]["message"]


@pytest.mark.parametrize("leak", ["Error code: 400", "image input", "{'error'", "failed:"])
async def test_the_providers_raw_error_never_reaches_the_caller(monkeypatch, leak):
    stages = [_stage("rag_retrieval", "failed", RAW), _stage("rule_findings_generated", "completed")]

    out = await _status(monkeypatch, stages, "failed")

    assert leak not in repr(out)


async def test_a_failure_that_produced_nothing_says_so_and_is_not_recoverable(monkeypatch):
    out = await _status(monkeypatch, [_stage("rag_retrieval", "failed", RAW)], "failed")

    assert out["status"] == "failed"
    assert out["error"]["recoverable"] is False
    assert "No result was produced" in out["error"]["message"] and "image input" not in out["error"]["message"]


async def test_the_stage_name_stays_available_for_integrators(monkeypatch):
    out = await _status(monkeypatch, [_stage("llm_analysis", "failed", "boom"), _stage("rule_findings_generated", "completed")], "failed")

    assert out["error"]["stage"] == "llm_analysis" and "written analysis" in out["error"]["message"]
