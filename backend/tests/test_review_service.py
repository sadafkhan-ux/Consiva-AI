"""Mocked unit tests for review_service.py's orchestration logic -- approve/reject/edit,
tenant isolation, and the atomic pending-only race guard (finding_repository's real SQL
against a live DB is already covered manually/live; this locks in the ORCHESTRATION
contract so a future regression here is caught automatically, not just by hand-testing).
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import FindingAlreadyDecidedError, NotFoundError
from app.db.models import ConsentFinding
from app.services import review_service


def _finding(*, status="pending", scan_id=None, agent_run_id=None, **overrides):
    defaults = {
        "id": uuid.uuid4(), "scan_id": scan_id or uuid.uuid4(), "agent_run_id": agent_run_id or uuid.uuid4(),
        "category": "functional", "risk_level": "medium", "priority": "medium", "finding_text": "text",
        "evidence": [], "dpdp_reference": [], "requires_human_review": True, "status": status,
    }
    defaults.update(overrides)
    return ConsentFinding(**defaults)


def _patch_common(monkeypatch, *, finding, correct_org_id, resume_ok=True):
    """Wires finding_repository.get_finding to mirror the real repository's own
    org-enforcing join: returns `finding` only when called with `correct_org_id`,
    None otherwise (a wrong-org caller gets exactly what the real SQL join would
    give it -- no row -- not a value this mock decides after the fact)."""
    async def fake_get_finding(_db, _finding_id, org_id):
        return finding if org_id == correct_org_id else None

    monkeypatch.setattr(review_service.finding_repository, "get_finding", fake_get_finding)
    monkeypatch.setattr(review_service.finding_repository, "create_approval", AsyncMock())
    monkeypatch.setattr(review_service.audit_service, "record", AsyncMock())

    db = AsyncMock()

    if resume_ok:
        fake_graph = AsyncMock()
        monkeypatch.setattr(review_service, "get_compiled_graph", AsyncMock(return_value=fake_graph))
    else:
        monkeypatch.setattr(
            review_service, "get_compiled_graph", AsyncMock(side_effect=RuntimeError("checkpointer down"))
        )
    return db


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_approve_finding_raises_not_found_for_wrong_org(monkeypatch):
    finding = _finding()
    correct_org = uuid.uuid4()
    db = _patch_common(monkeypatch, finding=finding, correct_org_id=correct_org)
    update_mock = AsyncMock()
    monkeypatch.setattr(review_service.finding_repository, "update_finding_status", update_mock)

    with pytest.raises(NotFoundError):
        await review_service.approve_finding(
            db, finding_id=finding.id, org_id=uuid.uuid4(), reviewer_user_id=uuid.uuid4(), reason=None
        )
    update_mock.assert_not_awaited()  # never reaches the status write for a cross-org request


async def test_approve_finding_raises_not_found_when_finding_missing(monkeypatch):
    monkeypatch.setattr(review_service.finding_repository, "get_finding", AsyncMock(return_value=None))
    db = AsyncMock()
    with pytest.raises(NotFoundError):
        await review_service.approve_finding(
            db, finding_id=uuid.uuid4(), org_id=uuid.uuid4(), reviewer_user_id=uuid.uuid4(), reason=None
        )


# ---------------------------------------------------------------------------
# Approve / reject happy paths
# ---------------------------------------------------------------------------


async def test_approve_finding_success(monkeypatch):
    org_id = uuid.uuid4()
    finding = _finding()
    db = _patch_common(monkeypatch, finding=finding, correct_org_id=org_id)
    monkeypatch.setattr(
        review_service.finding_repository, "update_finding_status", AsyncMock(return_value=finding)
    )

    result = await review_service.approve_finding(
        db, finding_id=finding.id, org_id=org_id, reviewer_user_id=uuid.uuid4(), reason="looks right"
    )

    assert result is finding
    review_service.finding_repository.create_approval.assert_awaited_once()
    review_service.audit_service.record.assert_awaited_once()
    db.commit.assert_awaited_once()


async def test_reject_finding_success(monkeypatch):
    org_id = uuid.uuid4()
    finding = _finding()
    db = _patch_common(monkeypatch, finding=finding, correct_org_id=org_id)
    monkeypatch.setattr(
        review_service.finding_repository, "update_finding_status", AsyncMock(return_value=finding)
    )

    await review_service.reject_finding(
        db, finding_id=finding.id, org_id=org_id, reviewer_user_id=uuid.uuid4(), reason="false positive"
    )

    call_kwargs = review_service.finding_repository.create_approval.await_args.kwargs
    assert call_kwargs["decision"] == "rejected"


# ---------------------------------------------------------------------------
# Race guard: update_finding_status/apply_edit returning None means "already decided"
# ---------------------------------------------------------------------------


async def test_approve_finding_raises_conflict_when_already_decided(monkeypatch):
    """The atomic UPDATE...WHERE status='pending' in finding_repository returns None
    when the finding was already decided by someone else -- approve_finding must turn
    that into a 409, never silently succeed or overwrite the prior decision."""
    org_id = uuid.uuid4()
    finding = _finding(status="approved")  # already decided, per the read
    db = _patch_common(monkeypatch, finding=finding, correct_org_id=org_id)
    monkeypatch.setattr(review_service.finding_repository, "update_finding_status", AsyncMock(return_value=None))

    with pytest.raises(FindingAlreadyDecidedError, match="no longer pending"):
        await review_service.approve_finding(
            db, finding_id=finding.id, org_id=org_id, reviewer_user_id=uuid.uuid4(), reason=None
        )

    # No approval/audit row for a decision that didn't actually apply.
    review_service.finding_repository.create_approval.assert_not_awaited()
    review_service.audit_service.record.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_reject_finding_raises_conflict_when_already_decided(monkeypatch):
    org_id = uuid.uuid4()
    finding = _finding(status="rejected")
    db = _patch_common(monkeypatch, finding=finding, correct_org_id=org_id)
    monkeypatch.setattr(review_service.finding_repository, "update_finding_status", AsyncMock(return_value=None))

    with pytest.raises(FindingAlreadyDecidedError):
        await review_service.reject_finding(
            db, finding_id=finding.id, org_id=org_id, reviewer_user_id=uuid.uuid4(), reason=None
        )


async def test_edit_finding_raises_conflict_when_already_decided(monkeypatch):
    org_id = uuid.uuid4()
    finding = _finding(status="edited")
    db = _patch_common(monkeypatch, finding=finding, correct_org_id=org_id)
    monkeypatch.setattr(review_service.finding_repository, "apply_edit", AsyncMock(return_value=None))

    with pytest.raises(FindingAlreadyDecidedError):
        await review_service.edit_finding(
            db, finding_id=finding.id, org_id=org_id, reviewer_user_id=uuid.uuid4(),
            edited_payload={"risk_level": "high"}, reason="escalating",
        )
    review_service.finding_repository.create_approval.assert_not_awaited()


async def test_edit_finding_success_applies_payload(monkeypatch):
    """edit_finding (like approve/reject) returns the `finding` object fetched at the
    top of the function, NOT apply_edit's own return value -- this is only correct in
    real usage because apply_edit's `UPDATE ... RETURNING ConsentFinding` runs through
    the ORM on the SAME session, so SQLAlchemy's identity map merges the updated row
    into that already-loaded `finding` instance in place (confirmed live: the API
    response's `status` field does reflect "edited", not stale "pending"). A fully
    mocked `db`/`apply_edit` has no such synchronization, so this test asserts the
    real contract (same object identity) rather than the misleading "same values"."""
    org_id = uuid.uuid4()
    finding = _finding()
    db = _patch_common(monkeypatch, finding=finding, correct_org_id=org_id)
    edited = _finding(status="edited", risk_level="high")
    apply_edit_mock = AsyncMock(return_value=edited)
    monkeypatch.setattr(review_service.finding_repository, "apply_edit", apply_edit_mock)

    result = await review_service.edit_finding(
        db, finding_id=finding.id, org_id=org_id, reviewer_user_id=uuid.uuid4(),
        edited_payload={"risk_level": "high"}, reason="escalating",
    )

    apply_edit_mock.assert_awaited_once_with(db, finding.id, {"risk_level": "high"}, org_id)
    assert result is finding


# ---------------------------------------------------------------------------
# _maybe_resume_agent_run: a resume failure must not crash the review decision itself
# ---------------------------------------------------------------------------


async def test_approve_finding_succeeds_even_if_resume_fails(monkeypatch):
    """The finding's own approval must persist even if resuming the paused LangGraph
    run fails afterward (e.g. a transient checkpointer error) -- the reviewer's
    decision is already committed by that point and must not be reported as failed."""
    org_id = uuid.uuid4()
    finding = _finding()
    db = _patch_common(monkeypatch, finding=finding, correct_org_id=org_id, resume_ok=False)
    monkeypatch.setattr(
        review_service.finding_repository, "update_finding_status", AsyncMock(return_value=finding)
    )

    result = await review_service.approve_finding(
        db, finding_id=finding.id, org_id=org_id, reviewer_user_id=uuid.uuid4(), reason=None
    )

    assert result is finding


async def test_resume_agent_run_retries_transient_failure_then_succeeds(monkeypatch):
    """A resume attempt that fails once (a transient checkpointer hiccup) then
    succeeds must actually resume the run -- not strand it at "paused" just because
    the first attempt failed. Distinguishes "retries and recovers" from the other
    test above, which only proves "gives up gracefully after exhausting retries"."""
    fake_graph = AsyncMock()
    attempts = {"n": 0}

    async def flaky_get_compiled_graph():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient checkpointer hiccup")
        return fake_graph

    monkeypatch.setattr(review_service, "get_compiled_graph", flaky_get_compiled_graph)

    await review_service._maybe_resume_agent_run(uuid.uuid4())

    assert attempts["n"] == 2
    fake_graph.ainvoke.assert_awaited_once()
