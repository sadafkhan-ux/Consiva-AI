"""DSR outcome contract, reconciliation and source revocation.

Three properties, held against the in-memory world the rest of the DSR suite uses (synthetic data,
no database, no real connector):

  * a portal may show a request as "resolved" only for a VERIFIED outcome -- found records, a
    drafted response or an attempted action never earn it
  * reconciliation REPORTS a disagreement between a portal and the agent and never changes either
  * revoking a source withdraws everything that can write, and a revoked source is never searched
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agents.dsr.errors import DsrNotFoundError, SourceNotAuthorizedError
from app.agents.dsr.schemas import case
from app.agents.dsr.services import approval_service, case_service, outcome_service
from app.api.v1.routes import dsr as dsr_routes
from app.core.security import CurrentUser
from app.db.repositories import dsr_repository, ropa_repository
from app.services import audit_service, dsr_run_service
from tests.test_dsr_end_to_end import (  # noqa: F401 -- `world` is a pytest fixture
    ORG,
    USER,
    FakeConnector,
    FakeDB,
    _open_case,
    _plan,
    _search,
    _use_connector,
    _verify,
    match,
    world,
)
from tests.test_dsr_execution_closure_audit import _fake_purge, _run_worker_execution_against


def _src(**kw):
    return SimpleNamespace(
        id=kw.get("id", uuid.uuid4()), name=kw.get("name", "crm"), connector=kw.get("connector", "postgres"),
        enabled=kw.get("enabled", True), credential_ref=kw.get("credential_ref", "CRM_READ"),
        credential_ciphertext=kw.get("credential_ciphertext"),
    )


ADMIN = CurrentUser(user_id=str(USER), org_id=str(ORG), role="admin")


MEMBER = CurrentUser(user_id=str(USER), org_id=str(ORG), role="member")


NOW = datetime(2026, 10, 15, tzinfo=UTC)


class _Db:
    async def flush(self):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_revoking_clears_execution_and_both_write_credentials(monkeypatch):
    source = _src()
    row = SimpleNamespace(
        id=uuid.uuid4(), data_source_id=source.id, enabled=True, allow_execution=True,
        write_credential_ref="CRM_WRITE", write_credential_ciphertext="ciphertext",
        searchable_tables=["customers"],
    )
    audits = []

    async def get_source(db, sid, org_id):
        return source

    async def get_auth(db, sid, org_id):
        return row

    async def audit(db, **kw):
        audits.append(kw)

    monkeypatch.setattr(ropa_repository, "get_data_source", get_source)
    monkeypatch.setattr(dsr_repository, "get_source_authorization", get_auth)
    monkeypatch.setattr(audit_service, "record", audit)

    result = await dsr_routes.revoke_source(data_source_id=source.id, user=ADMIN, db=_Db())

    assert result == {"source_name": "crm", "enabled": False, "allow_execution": False}
    assert row.enabled is False and row.allow_execution is False
    assert row.write_credential_ref is None and row.write_credential_ciphertext is None
    assert row.searchable_tables == ["customers"], "the read allowlist stays for review"
    assert audits[0]["action"] == "dsr.source_authorization_revoked"
    assert "ciphertext" not in repr(audits[0])


@pytest.mark.asyncio
async def test_revoking_a_source_with_no_authorization_is_a_clear_error(monkeypatch):
    source = _src()

    async def get_source(db, sid, org_id):
        return source

    async def get_auth(db, sid, org_id):
        return None

    monkeypatch.setattr(ropa_repository, "get_data_source", get_source)
    monkeypatch.setattr(dsr_repository, "get_source_authorization", get_auth)
    with pytest.raises(DsrNotFoundError, match="no DSR authorization"):
        await dsr_routes.revoke_source(data_source_id=source.id, user=ADMIN, db=_Db())


@pytest.mark.asyncio
async def test_a_revoked_source_is_not_searched(world, monkeypatch):
    """The disabled authorization must stop a search, not merely be hidden in the UI."""
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Delete my personal information.")
    await _verify(request)

    async def revoked(db, org_id):
        return [SimpleNamespace(enabled=False)]
    monkeypatch.setattr(dsr_repository, "list_source_authorizations", revoked)

    async def no_enqueue(db, **kw):
        raise AssertionError("no search job may be queued against a revoked source")
    monkeypatch.setattr(dsr_routes.queue, "enqueue", no_enqueue)

    with pytest.raises(SourceNotAuthorizedError):
        await dsr_routes.start_search(request_id=request.id, user=ADMIN, db=FakeDB())


async def _approved(monkeypatch, n=1, connector=None):
    _use_connector(monkeypatch, connector or FakeConnector(matches=[match(record_id=7), match("orders", 11)][:n]))
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    _, actions = await _plan(request)
    await case_service.transition(FakeDB(), request, case.APPROVAL_REQUIRED)
    for a in actions:
        await approval_service.decide_action(
            FakeDB(), request, a.id, reviewer_user_id=USER, decision="approved", reason="ok",
        )
    await case_service.transition(FakeDB(), request, case.APPROVED)
    return request, actions


@pytest.mark.asyncio
async def test_found_records_alone_are_not_a_resolution(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    await _search(request)
    await _plan(request)

    out = await dsr_routes.get_outcome(request_id=request.id, user=ADMIN, db=FakeDB())

    assert out["search"]["state"] == "succeeded" and out["search"]["records_found"] == 1
    assert out["execution"]["executed_verified"] == 0
    assert out["closure"] == outcome_service.CLOSURE_OPEN
    assert out["portal_may_show_resolved"] is False


@pytest.mark.asyncio
async def test_a_verified_execution_and_completion_is_the_only_resolution(world, monkeypatch):
    from app.agents.dsr.services import response_service

    monkeypatch.setattr(dsr_repository, "purge_evidence_pushes", _fake_purge)
    request, actions = await _approved(monkeypatch, 1)
    _run_worker_execution_against(monkeypatch, FakeDB())
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)

    mid = await dsr_routes.get_outcome(request_id=request.id, user=ADMIN, db=FakeDB())
    assert mid["execution"]["executed_verified"] == 1
    assert mid["portal_may_show_resolved"] is False, "executed but not yet answered/closed"

    await response_service.build_response(FakeDB(), request)
    await dsr_routes.complete_case(request_id=request.id, user=ADMIN, db=FakeDB())
    done = await dsr_routes.get_outcome(request_id=request.id, user=ADMIN, db=FakeDB())

    assert done["closure"] == outcome_service.CLOSURE_VERIFIED_COMPLETE
    assert done["portal_may_show_resolved"] is True and done["public_status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_a_failed_execution_is_reported_as_failed_even_though_data_was_found(world, monkeypatch):
    from app.agents.dsr.errors import ActionFailedError

    fc = FakeConnector(matches=[match()], exec_raises=ActionFailedError("down"))
    request, _ = await _approved(monkeypatch, 1, connector=fc)
    _run_worker_execution_against(monkeypatch, FakeDB())
    await dsr_run_service.execute_queued_actions(request.id, request.org_id)

    out = await dsr_routes.get_outcome(request_id=request.id, user=ADMIN, db=FakeDB())

    assert out["search"]["records_found"] == 1
    assert out["execution"]["failed"] == 1 and out["execution"]["executed_verified"] == 0
    assert out["closure"] == outcome_service.CLOSURE_FAILED
    assert out["portal_may_show_resolved"] is False


def test_closure_for_an_unverified_or_unresolved_completion_never_reads_clean():
    base = dict.fromkeys(("executed_verified", "executed_unverified", "failed", "blocked", "rejected", "awaiting"), 0)
    assert outcome_service.closure(case.COMPLETED, None, {**base, "executed_verified": 2}) == "verified_complete"
    assert outcome_service.closure(case.COMPLETED, None, base) == "completed_nothing_to_do"
    for dirty in ("failed", "blocked", "rejected", "awaiting", "executed_unverified"):
        got = outcome_service.closure(case.COMPLETED, None, {**base, "executed_verified": 1, dirty: 1})
        assert got == "partially_complete", dirty
    assert outcome_service.closure(case.FAILED, case.ERR_SOURCE_NOT_AUTHORIZED, base) == "waiting_for_setup"
    assert outcome_service.closure(case.FAILED, case.ERR_ACTION_FAILED, base) == "failed"
    assert outcome_service.closure(case.PARTIALLY_COMPLETED, None, base) == "partially_complete"


@pytest.mark.asyncio
async def test_reconcile_reports_the_exact_mismatches_seen_in_the_live_app(world, monkeypatch):
    # Portal: "Resolved" / identity "Not verified".  Agent: FAILED / identity verified.
    resolved_case, _ = await case_service.create_case(
        FakeDB(), org_id=ORG, raw_request="Give me my data", requester_email="a@b.com",
        idempotency_key="PORTAL-1", created_by_user_id=USER, now=NOW,
    )
    await case_service.classify_case(FakeDB(), resolved_case, actor_user_id=USER)
    await _verify(resolved_case)
    await case_service.transition(FakeDB(), resolved_case, case.SEARCHING)
    await case_service.transition(
        FakeDB(), resolved_case, case.FAILED, error_code=case.ERR_SEARCH_FAILED, error_detail="x",
    )
    before = (resolved_case.status, resolved_case.error_code, resolved_case.updated_at)

    payload = dsr_routes.ReconcileRequest(items=[
        dsr_routes.ReconcileItem(client_reference="PORTAL-1", portal_status="Resolved", portal_identity_verified=False),
        dsr_routes.ReconcileItem(client_reference="PORTAL-404", portal_status="Open"),
        dsr_routes.ReconcileItem(client_reference="PORTAL-1", portal_status="Mystery Status"),
    ])
    out = await dsr_routes.reconcile_requests(payload=payload, user=ADMIN, db=FakeDB())

    first, missing, unmapped = out["results"]
    assert first["agrees"] is False
    assert "portal says 'Resolved' but the agent case is FAILED" in first["reason"]
    assert "identity differs" in first["reason"]
    assert missing["found"] is False and "no agent case" in missing["reason"]
    assert unmapped["agrees"] is None and "no mapping" in unmapped["reason"]
    assert (out["checked"], out["mismatches"], out["not_found"], out["unmapped"]) == (3, 1, 1, 1)
    assert (resolved_case.status, resolved_case.error_code, resolved_case.updated_at) == before, (
        "reconciliation must never change the case"
    )


def test_portal_status_matching_is_forgiving_about_spelling_but_strict_about_meaning():
    ok = outcome_service.reconcile_item(
        client_reference="x", portal_status="Partially Resolved", portal_identity_verified=None,
        request=SimpleNamespace(id=uuid.uuid4(), reference="DSR-1", status=case.PARTIALLY_COMPLETED, error_code=None),
        identity_satisfied=None,
    )
    assert ok["agrees"] is True
    wrong = outcome_service.reconcile_item(
        client_reference="x", portal_status="resolved", portal_identity_verified=None,
        request=SimpleNamespace(id=uuid.uuid4(), reference="DSR-1", status=case.PARTIALLY_COMPLETED, error_code=None),
        identity_satisfied=None,
    )
    assert wrong["agrees"] is False, "'resolved' must not agree with a partial outcome"
