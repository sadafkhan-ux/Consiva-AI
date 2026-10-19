"""Classification safety and triage: what a request IS decides what the agent may do.

Found by reviewing real cases in the running app:
  * "withdraw consent but not delete my data" was classified DELETION (0.95)
  * "do not delete my data" / "please don't erase my account" were classified DELETION --
    the exact opposite of what was asked, at the highest confidence band
  * a grievance was sent into a data search, where it could only fail
  * a case with no connected source was queued and FAILED instead of being refused up front
"""

import uuid
from types import SimpleNamespace

import pytest

from app.agents.dsr.errors import SourceNotAuthorizedError
from app.agents.dsr.rules import request_classifier as rc
from app.agents.dsr.schemas import case
from app.agents.dsr.services import case_service, lifecycle
from app.api.v1.routes import dsr as dsr_routes
from app.core.security import CurrentUser
from app.db.repositories import dsr_repository

from tests.test_dsr_end_to_end import (  # noqa: F401 -- `world` is a pytest fixture
    ORG, USER, FakeConnector, FakeDB, _open_case, _plan, _search, _use_connector, _verify,
    match, world,
)


# ── Classification ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("I want to withdraw consent but not delete my data", case.WITHDRAW_CONSENT),
    ("Please withdraw my consent for marketing emails", case.WITHDRAW_CONSENT),
    ("I revoke my consent to this processing", case.WITHDRAW_CONSENT),
    ("I want to opt out of your mailing list", case.WITHDRAW_CONSENT),
    ("I have a grievance about how my data was handled", case.GRIEVANCE),
    ("I want to file a complaint against your company", case.GRIEVANCE),
])
def test_consent_withdrawal_and_grievance_have_their_own_types(text, expected):
    result = rc.classify(text)
    assert result.request_type == expected, (text, result.request_type, result.evidence)
    assert not result.ambiguous


@pytest.mark.parametrize("text", [
    "do not delete my data, just stop the emails",
    "please don't erase my account",
    "I want you to keep my data and not delete anything",
    "Withdraw my consent, without deleting my data",
    "no deletion please, only stop marketing",
])
def test_a_declined_erasure_is_never_classified_as_deletion(text):
    result = rc.classify(text)
    assert result.request_type != case.DELETION, (text, result.evidence)
    assert case.DELETION not in result.candidates


def test_declining_an_erasure_is_recorded_in_the_evidence():
    result = rc.classify("please don't erase my account")
    assert any("explicitly declined" in e for e in result.evidence)


@pytest.mark.parametrize("text", [
    "Delete my personal information.",
    "Please erase my data",
    "I want to exercise my right to be forgotten",
    # "not" appears, but not as the negator of the erasure verb.
    "I do not want to keep my data, delete it",
    "I did not receive any email, please delete my data",
])
def test_genuine_deletion_requests_are_still_deletion(text):
    assert rc.classify(text).request_type == case.DELETION


def test_consent_withdrawal_together_with_a_real_deletion_is_ambiguous_not_erasure():
    result = rc.classify("Withdraw my consent and delete my data")
    assert result.ambiguous and result.request_type is None
    assert set(result.candidates) == {case.WITHDRAW_CONSENT, case.DELETION}


def test_new_types_are_in_the_closed_vocabulary():
    for t in (case.WITHDRAW_CONSENT, case.GRIEVANCE):
        assert t in case.REQUEST_TYPES and t in case.CLASSIFIABLE_TYPES
        assert rc.coerce_model_type(t) == t
    assert rc.coerce_model_type("partial_deletion") is None


# ── Neither new type can ever plan a write ───────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "I want to withdraw consent but not delete my data",
    "I have a grievance about how my data was handled",
])
async def test_consent_withdrawal_and_grievance_never_plan_a_mutating_action(world, monkeypatch, text):
    _use_connector(monkeypatch, FakeConnector(matches=[match(record_id=7), match("orders", 11)]))
    request = await _open_case(text)
    await _verify(request)
    await _search(request)

    _, actions = await _plan(request)

    assert actions, "the case should still be planned so a person sees it"
    for action in actions:
        assert action.operation not in case.MUTATING_OPERATIONS, action.operation
        assert action.requires_approval, "a person must decide what this request needs"


# ── /search: a person reads it first when a search should not start ──────────────

def _user():
    return CurrentUser(user_id=str(USER), org_id=str(ORG), role="admin")


async def _no_enqueue(monkeypatch):
    queued = []

    async def fake_enqueue(db, *, org_id, job_type, payload):
        queued.append(job_type)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(dsr_routes.queue, "enqueue", fake_enqueue)
    return queued


@pytest.mark.asyncio
async def test_a_grievance_goes_to_a_person_and_is_never_searched(world, monkeypatch):
    fc = FakeConnector(matches=[match()])
    _use_connector(monkeypatch, fc)
    queued = await _no_enqueue(monkeypatch)
    request = await _open_case("I have a grievance about how my data was handled")
    await _verify(request)

    result = await dsr_routes.start_search(request_id=request.id, user=_user(), db=FakeDB())

    assert result["triaged"] is True and result["job_id"] is None
    assert request.status == case.REVIEW_REQUIRED
    assert queued == [], "no search job may be queued for a grievance"
    assert "complaint" in result["case"]["status_message"].lower()
    assert result["case"]["public_status"] == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_an_unclassifiable_request_waits_for_a_person_then_can_be_searched(world, monkeypatch):
    fc = FakeConnector(matches=[match()])
    _use_connector(monkeypatch, fc)
    queued = await _no_enqueue(monkeypatch)
    request = await _open_case("hello, can you help me with something about my account")
    assert request.request_type == case.OTHER and request.classification_confidence == 0.0
    await _verify(request)

    first = await dsr_routes.start_search(request_id=request.id, user=_user(), db=FakeDB())
    assert first["triaged"] is True and request.status == case.REVIEW_REQUIRED and queued == []

    # A person decides what it is. That decision is the thing the triage was waiting for.
    await case_service.classify_case(FakeDB(), request, actor_user_id=USER, override_type=case.ACCESS)
    second = await dsr_routes.start_search(request_id=request.id, user=_user(), db=FakeDB())

    assert second["job_id"] is not None and queued == ["dsr_search"]
    assert request.status == case.SEARCHING


@pytest.mark.asyncio
async def test_a_confident_request_is_searched_without_triage(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    queued = await _no_enqueue(monkeypatch)
    request = await _open_case("Delete my personal information.")
    await _verify(request)

    result = await dsr_routes.start_search(request_id=request.id, user=_user(), db=FakeDB())

    assert "triaged" not in result and queued == ["dsr_search"]


# ── /search: no connected source is a refusal, not a failed case ─────────────────

@pytest.mark.asyncio
async def test_search_with_no_connected_source_is_refused_and_the_case_is_left_alone(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    queued = await _no_enqueue(monkeypatch)

    async def none_authorized(db, org_id):
        return []
    monkeypatch.setattr(dsr_repository, "list_source_authorizations", none_authorized)
    request = await _open_case("Delete my personal information.")
    await _verify(request)
    before = request.status

    with pytest.raises(SourceNotAuthorizedError, match="No data source is connected"):
        await dsr_routes.start_search(request_id=request.id, user=_user(), db=FakeDB())

    assert request.status == before, "the case must stay open exactly where it was"
    assert request.status != case.FAILED and queued == []


@pytest.mark.asyncio
async def test_a_disabled_authorization_does_not_count_as_a_connected_source(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    await _no_enqueue(monkeypatch)

    async def only_disabled(db, org_id):
        return [SimpleNamespace(enabled=False)]
    monkeypatch.setattr(dsr_repository, "list_source_authorizations", only_disabled)
    request = await _open_case("Delete my personal information.")
    await _verify(request)

    with pytest.raises(SourceNotAuthorizedError):
        await dsr_routes.start_search(request_id=request.id, user=_user(), db=FakeDB())


# ── Plain language and the setup status ──────────────────────────────────────────

def test_a_case_failed_for_want_of_a_source_reads_as_waiting_for_setup_not_failed():
    assert lifecycle.public_status(case.FAILED, case.ERR_SOURCE_NOT_AUTHORIZED) == "WAITING_FOR_SETUP"
    assert lifecycle.public_status(case.FAILED, case.ERR_ACTION_FAILED) == "FAILED"
    message, step = lifecycle.plain_status(case.FAILED, case.ERR_SOURCE_NOT_AUTHORIZED, case.ACCESS)
    assert "no data source is connected" in message
    assert "Configuration" in step and "start the search again" in step


def test_every_status_has_a_plain_message():
    for status in case.ALL_STATUSES:
        message, _ = lifecycle.plain_status(status)
        assert message and "_" not in message, (status, message)


def test_plain_messages_do_not_expose_codes_or_internal_field_names():
    banned = ("SOURCE_NOT_AUTHORIZED", "allowed_transitions", "is_terminal", "confidence")
    for code in case.ERROR_CODES:
        message, step = lifecycle.plain_status(case.FAILED, code)
        for word in banned:
            assert word not in message and word not in (step or ""), (code, message, step)


def test_review_before_search_is_reachable_but_execution_still_is_not():
    for source in (case.CLASSIFIED, case.IDENTITY_VERIFIED):
        assert lifecycle.can_transition(source, case.REVIEW_REQUIRED)
        assert not lifecycle.can_transition(source, case.EXECUTING)
    assert not lifecycle.can_transition(case.RECEIVED, case.REVIEW_REQUIRED)


def test_needs_human_triage_respects_a_persons_decision():
    assert lifecycle.needs_human_triage(case.GRIEVANCE, 1.0, "manual"), (
        "a grievance is never searched, even when a person labelled it"
    )
    assert lifecycle.needs_human_triage(case.OTHER, 0.0, "deterministic")
    assert lifecycle.needs_human_triage(case.OTHER, 1.0, "manual") is None
    assert lifecycle.needs_human_triage(case.DELETION, 0.75, "deterministic") is None
