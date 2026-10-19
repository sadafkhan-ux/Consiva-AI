"""Fixes from the 16 October 2026 DSR audit that live in the backend.

  * a response is never drafted for a case where nothing was searched (P0-3)
  * a letter never says "we searched" or "we found nothing" without a finished search
  * the verification code is human-usable and still stored only as a hash (P1-1)
  * manual verification records method, reference and note separately (P1-2)
  * asking for another code is no way round a lockout, per case or per requester (P1-3)
  * common English and Hinglish access requests classify; ambiguous ones stay for a person (P1-5)
  * a second open case for the same requester needs a reason; the list shows the identity status (P2)

Synthetic data and in-memory fakes only -- no database, no connector, no network.
"""

import re
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.dsr.errors import (
    CaseNotReadyError,
    ConnectorUnavailableError,
    DuplicateActiveCaseError,
    IdentityLimitError,
    IdentityRequiredError,
)
from app.agents.dsr.rules import request_classifier as rc
from app.agents.dsr.schemas import case
from app.agents.dsr.services import case_service, identity_service, response_service
from app.api.v1.routes import dsr as dsr_routes
from app.core.security import CurrentUser
from tests.test_dsr_end_to_end import (  # noqa: F401 -- `world` is a pytest fixture
    NOW,
    ORG,
    USER,
    FakeConnector,
    FakeDB,
    _open_case,
    _search,
    _use_connector,
    _verify,
    match,
    world,
)

ADMIN = CurrentUser(user_id=str(USER), org_id=str(ORG), role="admin")


def _new_case_with_email(world, email, text="Show me my personal data."):
    async def go():
        request, _ = await case_service.create_case(
            FakeDB(), org_id=ORG, raw_request=text, requester_email=email,
            created_by_user_id=USER, now=NOW,
        )
        await case_service.classify_case(FakeDB(), request, actor_user_id=USER)
        return request
    return go()


# ── P0-3: no response without a verified requester and a finished search ────────────────

@pytest.mark.asyncio
async def test_a_response_is_refused_before_identity_is_verified(world):
    request = await _open_case("Show me my personal data.")

    with pytest.raises(IdentityRequiredError, match="no search has been run"):
        await dsr_routes.generate_response(request_id=request.id, user=ADMIN, db=FakeDB())

    assert world.responses == [], "nothing may be drafted"


@pytest.mark.asyncio
async def test_a_response_is_refused_when_no_search_has_run(world):
    request = await _open_case("Show me my personal data.")
    await _verify(request)

    with pytest.raises(CaseNotReadyError, match="No search has been run"):
        await dsr_routes.generate_response(request_id=request.id, user=ADMIN, db=FakeDB())

    assert world.responses == []


@pytest.mark.asyncio
async def test_a_response_is_refused_when_the_only_search_failed(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(search_raises=ConnectorUnavailableError("down")))
    request = await _open_case("Show me my personal data.")
    await _verify(request)
    await _search(request)
    assert [r.status for r in world.search_runs] == ["failed"]

    with pytest.raises(CaseNotReadyError, match="did not complete"):
        await dsr_routes.generate_response(request_id=request.id, user=ADMIN, db=FakeDB())

    assert world.responses == []


@pytest.mark.asyncio
async def test_a_completed_search_with_no_match_may_say_nothing_was_found(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[]))
    request = await _open_case("Show me my personal data.")
    await _verify(request)
    await _search(request)

    drafted = await dsr_routes.generate_response(request_id=request.id, user=ADMIN, db=FakeDB())

    assert "did not find any record" in drafted["body_text"]
    assert any(f["type"] == "search" for f in drafted["grounded_facts"]), (
        "the claim must trace to a search that actually ran"
    )


@pytest.mark.asyncio
async def test_a_completed_search_with_records_is_reported(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Show me my personal data.")
    await _verify(request)
    await _search(request)

    drafted = await dsr_routes.generate_response(request_id=request.id, user=ADMIN, db=FakeDB())

    assert "We found 1 record(s)" in drafted["body_text"]


def test_the_letter_text_never_claims_a_search_when_there_was_none():
    request = SimpleNamespace(
        reference="DSR-X", request_type=case.ACCESS, received_at=NOW,
    )
    body = response_service._compose(request, [], [], [], [])

    assert "did not find any record" not in body
    assert "We searched" not in body
    assert "does not say whether we hold any data" in body


def test_the_letter_text_does_not_turn_a_failed_search_into_no_data():
    request = SimpleNamespace(reference="DSR-X", request_type=case.ACCESS, received_at=NOW)
    failed = SimpleNamespace(source_name="crm", status="failed", error_code="SEARCH_FAILED")

    body = response_service._compose(request, [], [failed], [], [])

    assert "unable to complete the search" in body
    assert "did not find any record" not in body


@pytest.mark.asyncio
async def test_a_complaint_may_be_answered_and_the_letter_says_no_search_was_run(world):
    request = await _open_case("I have a grievance about how my data was handled")

    drafted = await dsr_routes.generate_response(request_id=request.id, user=ADMIN, db=FakeDB())

    assert "No search of your data was performed" in drafted["body_text"]
    assert "did not find" not in drafted["body_text"]


# ── P1-1: the code is usable by a person and still never stored in the clear ─────────────

def test_a_code_is_short_grouped_and_free_of_look_alike_characters():
    for _ in range(200):
        code = identity_service.generate_challenge()
        assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code), code
        assert not set(code) & set("01OIL"), code


def test_codes_are_unpredictable_and_only_their_hash_is_kept():
    codes = {identity_service.generate_challenge() for _ in range(500)}
    assert len(codes) == 500
    code = next(iter(codes))
    digest = identity_service.hash_challenge(code)
    assert code.replace("-", "") not in digest and len(digest) == 64


@pytest.mark.parametrize("typed", ["{code}", "{lower}", "  {code}  ", "{nodash}", "{spaced}"])
def test_how_a_person_types_the_code_does_not_matter(typed):
    code = identity_service.generate_challenge()
    entered = typed.format(
        code=code, lower=code.lower(), nodash=code.replace("-", ""), spaced=code.replace("-", " "),
    )
    assert identity_service.hash_challenge(entered) == identity_service.hash_challenge(code)


@pytest.mark.asyncio
async def test_a_code_issued_before_the_format_change_still_verifies(world):
    """A 32-character hex code from the old format is pending for up to 24 hours."""
    request = await _open_case("Show me my personal data.")
    legacy = "0123456789abcdef0123456789abcdef"
    row = await world_verification(world, request, identity_service._legacy_hash(legacy))

    result = await identity_service.submit_challenge(FakeDB(), request, legacy, now=NOW)

    assert result is row and row.status == case.IDV_VERIFIED


async def world_verification(world, request, challenge_hash, **kw):
    from app.db.repositories import dsr_repository as repo
    row = await repo.create_identity_verification(
        None, org_id=request.org_id, request_id=request.id, method="email_challenge",
        challenge_hash=challenge_hash, expires_at=NOW + timedelta(hours=24),
    )
    row.status = case.IDV_IN_PROGRESS
    for key, value in kw.items():
        setattr(row, key, value)
    return row


# ── Wrong code, attempts remaining, expiry, success -- through the real route ────────────

@pytest.mark.asyncio
async def test_a_wrong_code_reports_how_many_attempts_remain(world):
    request = await _open_case("Show me my personal data.")
    issued = await dsr_routes.start_identity_challenge(request_id=request.id, user=ADMIN, db=FakeDB())
    assert issued["emailed"] is False and "No email was sent" in issued["delivery_note"]

    wrong = await dsr_routes.verify_identity(
        request_id=request.id, payload=dsr_routes.VerifyIdentity(challenge="WRONG-CODE-0000"),
        user=ADMIN, db=FakeDB(),
    )

    assert wrong["satisfied"] is False
    assert wrong["attempts"] == 1 and wrong["max_attempts"] == 5
    assert wrong["attempts_remaining"] == 4


@pytest.mark.asyncio
async def test_the_right_code_verifies_and_the_case_moves_on(world):
    request = await _open_case("Show me my personal data.")
    issued = await dsr_routes.start_identity_challenge(request_id=request.id, user=ADMIN, db=FakeDB())

    ok = await dsr_routes.verify_identity(
        request_id=request.id, payload=dsr_routes.VerifyIdentity(challenge=issued["challenge"].lower()),
        user=ADMIN, db=FakeDB(),
    )

    assert ok["satisfied"] is True and ok["case"]["identity_status"] == "verified"
    assert request.status == case.IDENTITY_VERIFIED


@pytest.mark.asyncio
async def test_five_wrong_codes_lock_the_challenge_and_the_correct_one_no_longer_works(world):
    request = await _open_case("Show me my personal data.")
    issued = await dsr_routes.start_identity_challenge(request_id=request.id, user=ADMIN, db=FakeDB())
    for _ in range(5):
        last = await dsr_routes.verify_identity(
            request_id=request.id, payload=dsr_routes.VerifyIdentity(challenge="NOPE-NOPE-NOPE"),
            user=ADMIN, db=FakeDB(),
        )
    assert last["status"] == case.IDV_FAILED and last["attempts_remaining"] == 0

    after = await dsr_routes.verify_identity(
        request_id=request.id, payload=dsr_routes.VerifyIdentity(challenge=issued["challenge"]),
        user=ADMIN, db=FakeDB(),
    )

    assert after["satisfied"] is False, "a burned challenge cannot be answered, even correctly"


@pytest.mark.asyncio
async def test_an_expired_code_is_reported_as_expired_not_as_wrong(world):
    request = await _open_case("Show me my personal data.")
    row = await world_verification(world, request, identity_service.hash_challenge("ABCD-EFGH-JKMN"))

    result = await identity_service.submit_challenge(
        FakeDB(), request, "ABCD-EFGH-JKMN", now=NOW + timedelta(hours=25),
    )

    assert result is row and identity_service.effective_status(row, now=NOW + timedelta(hours=25)) == case.IDV_EXPIRED


# ── P1-3: another code is no way round a lockout ────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_second_code_straight_after_the_first_is_refused(world):
    request = await _open_case("Show me my personal data.")
    await identity_service.start_challenge(FakeDB(), request, now=NOW)

    with pytest.raises(IdentityLimitError, match="moments ago"):
        await identity_service.start_challenge(FakeDB(), request, now=NOW + timedelta(seconds=10))

    # After the cooldown it is allowed again.
    await identity_service.start_challenge(FakeDB(), request, now=NOW + timedelta(minutes=2))


@pytest.mark.asyncio
async def test_a_locked_out_case_cannot_get_a_new_code_until_the_lockout_has_passed(world):
    request = await _open_case("Show me my personal data.")
    first, _code = await identity_service.start_challenge(FakeDB(), request, now=NOW)
    for _ in range(identity_service.MAX_ATTEMPTS):
        await identity_service.submit_challenge(FakeDB(), request, "WRONG-WRONG-XXXX", now=NOW + timedelta(minutes=2))
    assert first.status == case.IDV_FAILED

    with pytest.raises(IdentityLimitError, match="wrong codes"):
        await identity_service.start_challenge(FakeDB(), request, now=NOW + timedelta(minutes=5))

    later = NOW + timedelta(minutes=2) + identity_service.LOCKOUT_COOLDOWN + timedelta(seconds=1)
    second, _ = await identity_service.start_challenge(FakeDB(), request, now=later)
    assert second.attempts == 0 and second is not first


@pytest.mark.asyncio
async def test_a_case_gets_only_a_fixed_number_of_codes_in_total(world):
    request = await _open_case("Show me my personal data.")
    moment = NOW
    for _ in range(identity_service.MAX_CHALLENGES_PER_CASE):
        await identity_service.start_challenge(FakeDB(), request, now=moment)
        moment += timedelta(hours=1)

    with pytest.raises(IdentityLimitError, match="verify the requester manually"):
        await identity_service.start_challenge(FakeDB(), request, now=moment + timedelta(days=3))


@pytest.mark.asyncio
async def test_opening_new_cases_is_no_way_round_the_requester_limit(world):
    moment = NOW
    for n in range(identity_service.MAX_CHALLENGES_PER_REQUESTER):
        request = await _new_case_with_email(world, "same.person@example.com")
        await identity_service.start_challenge(FakeDB(), request, now=moment)
        moment += timedelta(minutes=2)

    fresh = await _new_case_with_email(world, "same.person@example.com")
    with pytest.raises(IdentityLimitError, match="maximum number of verification codes"):
        await identity_service.start_challenge(FakeDB(), fresh, now=moment)

    # A different requester is unaffected.
    other = await _new_case_with_email(world, "someone.else@example.com")
    await identity_service.start_challenge(FakeDB(), other, now=moment)


@pytest.mark.asyncio
async def test_a_refused_code_is_recorded_on_the_audit_trail(world):
    request = await _open_case("Show me my personal data.")
    await dsr_routes.start_identity_challenge(request_id=request.id, user=ADMIN, db=FakeDB())
    world.verifications[-1].created_at = identity_service.datetime.now(identity_service.UTC)

    with pytest.raises(IdentityLimitError):
        await dsr_routes.start_identity_challenge(request_id=request.id, user=ADMIN, db=FakeDB())

    refused = [a for a in world.audit if a["action"] == case.AUDIT_IDENTITY_CHALLENGE_REFUSED]
    assert len(refused) == 1 and refused[0]["entity_id"] == request.id


# ── P1-2: manual verification is structured ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manual_verification_records_method_reference_and_note_separately(world):
    request = await _open_case("Show me my personal data.")

    row = await identity_service.verify_manually(
        FakeDB(), request, reviewer_user_id=USER, evidence_note="Passport photo matched the account holder",
        method="id_document", reference="PP-4471", now=NOW,
    )

    assert row.status == case.IDV_MANUALLY_VERIFIED and "PP-4471" in row.evidence_note
    entry = [a for a in world.audit if a["action"] == case.AUDIT_IDENTITY_VERIFIED][-1]
    assert entry["after"]["verification_method"] == "id_document"
    assert entry["after"]["reference"] == "PP-4471"
    assert entry["after"]["evidence_note"] == "Passport photo matched the account holder"
    assert entry["actor_user_id"] == USER


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"method": "telepathy", "reference": "x", "evidence_note": "long enough note here"},
    {"method": "id_document", "reference": "  ", "evidence_note": "long enough note here"},
    {"method": "id_document", "reference": "PP-1", "evidence_note": "ok"},
])
async def test_a_structured_manual_verification_with_a_gap_is_refused(world, kwargs):
    request = await _open_case("Show me my personal data.")

    with pytest.raises(CaseNotReadyError):
        await identity_service.verify_manually(FakeDB(), request, reviewer_user_id=USER, now=NOW, **kwargs)

    assert world.verifications == [], "nothing is stored for a refused verification"


def test_the_manual_route_body_must_carry_method_and_reference():
    with pytest.raises(ValidationError):
        dsr_routes.VerifyIdentity(manual=True, evidence_note="I checked their passport")
    with pytest.raises(ValidationError):
        dsr_routes.VerifyIdentity(manual=True, method="id_document", evidence_note="I checked their passport")
    dsr_routes.VerifyIdentity(manual=True, method="id_document", reference="PP-1", evidence_note="I checked it")
    dsr_routes.VerifyIdentity(challenge="ABCD-EFGH-JKMN")


# ── P1-5: classification of common English and Hinglish access requests ──────────────────

@pytest.mark.parametrize("text", [
    "I want to know what data you have about me.",
    "Show me my personal data.",
    "Mujhe mera data dikhao.",
    "Mera data kahan use ho raha hai?",
    "What information does the company hold about me?",
    "Please let me see my records",
    "aapke paas mera kya data hai",
])
def test_common_access_requests_are_recognised(text):
    result = rc.classify(text)
    assert result.request_type == case.ACCESS and result.confidence >= case.MIN_CONFIDENCE_TO_SEARCH, (
        text, result.request_type, result.evidence,
    )


@pytest.mark.parametrize("text,expected", [
    ("mera data delete kar do", case.DELETION),
    ("mera data sahi karo", case.CORRECTION),
    ("mera consent wapas lo", case.WITHDRAW_CONSENT),
    ("meri shikayat hai", case.GRIEVANCE),
])
def test_common_hinglish_requests_of_other_types_are_recognised(text, expected):
    assert rc.classify(text).request_type == expected


@pytest.mark.parametrize("text", [
    "Send me my data and then delete it",
    "Mujhe mera data dikhao aur phir delete karo",
])
def test_two_requests_in_one_message_stay_with_a_person(text):
    result = rc.classify(text)
    assert result.ambiguous and result.request_type is None
    assert set(result.candidates) == {case.ACCESS, case.DELETION}


@pytest.mark.parametrize("text", [
    "mera data delete mat karo",
    "please don't delete my data",
    "hello, can you help me with something",
    "I have a question",
])
def test_an_unclear_or_declined_request_is_never_forced_into_a_type(text):
    result = rc.classify(text)
    assert result.request_type in (None, case.OTHER) and result.confidence < case.MIN_CONFIDENCE_TO_SEARCH
    assert result.request_type != case.DELETION


# ── P2: duplicate open cases, and one identity status in list and detail ─────────────────

@pytest.mark.asyncio
async def test_a_second_open_case_for_the_same_requester_is_refused_without_a_reason(world):
    first = await dsr_routes.create_request(
        payload=dsr_routes.DsrCreate(raw_request="Show me my data", requester_email="Dup@Example.com"),
        user=ADMIN, db=FakeDB(),
    )

    with pytest.raises(DuplicateActiveCaseError, match=first["reference"]):
        await dsr_routes.create_request(
            payload=dsr_routes.DsrCreate(raw_request="Show me my data again", requester_email="dup@example.com"),
            user=ADMIN, db=FakeDB(),
        )

    assert len(world.requests) == 1


@pytest.mark.asyncio
async def test_a_second_case_is_allowed_with_a_reason_and_the_reason_is_audited(world):
    first = await dsr_routes.create_request(
        payload=dsr_routes.DsrCreate(raw_request="Show me my data", requester_email="dup@example.com"),
        user=ADMIN, db=FakeDB(),
    )

    second = await dsr_routes.create_request(
        payload=dsr_routes.DsrCreate(
            raw_request="Delete my data", requester_email="dup@example.com",
            duplicate_override_reason="Separate request about a different account",
        ),
        user=ADMIN, db=FakeDB(),
    )

    assert second["id"] != first["id"]
    created = [a for a in world.audit if a["action"] == case.AUDIT_CREATED and a["after"].get("duplicate_of")]
    assert created[0]["after"]["duplicate_override_reason"] == "Separate request about a different account"
    assert created[0]["after"]["duplicate_of"] == [first["reference"]]


def test_a_one_word_override_reason_is_not_a_reason():
    with pytest.raises(ValidationError):
        dsr_routes.DsrCreate(raw_request="x", requester_email="a@b.com", duplicate_override_reason="ok")


@pytest.mark.asyncio
async def test_a_closed_case_does_not_count_as_a_duplicate(world):
    first = await dsr_routes.create_request(
        payload=dsr_routes.DsrCreate(raw_request="Show me my data", requester_email="done@example.com"),
        user=ADMIN, db=FakeDB(),
    )
    world.requests[uuid.UUID(first["id"])].status = case.COMPLETED

    again = await dsr_routes.create_request(
        payload=dsr_routes.DsrCreate(raw_request="Show me my data", requester_email="done@example.com"),
        user=ADMIN, db=FakeDB(),
    )
    assert again["id"] != first["id"]


@pytest.mark.asyncio
async def test_an_imported_case_with_its_own_idempotency_key_is_not_blocked(world):
    await dsr_routes.create_request(
        payload=dsr_routes.DsrCreate(raw_request="Show me my data", requester_email="p@example.com"),
        user=ADMIN, db=FakeDB(),
    )

    imported = await dsr_routes.create_request(
        payload=dsr_routes.DsrCreate(
            raw_request="Portal request 77", requester_email="p@example.com", idempotency_key="portal-77",
        ),
        user=ADMIN, db=FakeDB(),
    )

    assert imported["reference"]


@pytest.mark.asyncio
async def test_the_active_case_lookup_is_scoped_to_the_callers_organisation(world):
    await dsr_routes.create_request(
        payload=dsr_routes.DsrCreate(raw_request="Show me my data", requester_email="scoped@example.com"),
        user=ADMIN, db=FakeDB(),
    )

    mine = await dsr_routes.active_requests_for_requester(email="scoped@example.com", user=ADMIN, db=FakeDB())
    theirs = await dsr_routes.active_requests_for_requester(
        email="scoped@example.com",
        user=CurrentUser(user_id=str(uuid.uuid4()), org_id=str(uuid.uuid4()), role="admin"), db=FakeDB(),
    )

    assert len(mine) == 1 and theirs == []


def _list_from_world(world, monkeypatch):
    from app.db.repositories import dsr_repository

    async def list_requests(db, org_id, **kw):
        return [r for r in world.requests.values() if r.org_id == org_id]
    monkeypatch.setattr(dsr_repository, "list_requests", list_requests)


@pytest.mark.asyncio
async def test_the_case_list_carries_the_same_identity_status_as_the_detail(world, monkeypatch):
    _list_from_world(world, monkeypatch)
    request = await _open_case("Show me my personal data.")
    await _verify(request)

    listed = await dsr_routes.list_requests(status_filter=None, limit=10, user=ADMIN, db=FakeDB())
    detail = await dsr_routes.get_request(request_id=request.id, user=ADMIN, db=FakeDB())

    assert [c["identity_status"] for c in listed] == [detail["identity_status"]] == ["manually_verified"]


@pytest.mark.asyncio
async def test_a_case_with_no_verification_reads_pending_in_the_list_not_null(world, monkeypatch):
    _list_from_world(world, monkeypatch)
    await _open_case("Show me my personal data.")

    listed = await dsr_routes.list_requests(status_filter=None, limit=10, user=ADMIN, db=FakeDB())

    assert listed[0]["identity_status"] == case.IDV_PENDING


# ── The audit trail and the identity gate are untouched by all of the above ──────────────

@pytest.mark.asyncio
async def test_search_still_cannot_start_without_identity_verification(world, monkeypatch):
    _use_connector(monkeypatch, FakeConnector(matches=[match()]))
    request = await _open_case("Show me my personal data.")

    with pytest.raises(IdentityRequiredError):
        await dsr_routes.start_search(request_id=request.id, user=ADMIN, db=FakeDB())


# ── Creating a request returns the SAVED record's own id, reference, email and status ───────────

@pytest.mark.asyncio
async def test_create_returns_the_values_of_the_record_that_was_saved(world):
    out = await dsr_routes.create_request(
        payload=dsr_routes.DsrCreate(raw_request="Show me my personal data.", requester_email="  Sadaf.Khan@Example.COM "),
        user=ADMIN, db=FakeDB(),
    )

    saved = world.requests[uuid.UUID(out["id"])]
    assert out["id"] == str(saved.id), "the id returned is the saved row's id, not a second one"
    assert out["reference"] == saved.reference
    assert out["requester_email"] == saved.requester_email == "sadaf.khan@example.com"
    assert out["status"] == saved.status == case.CLASSIFIED
    assert out["public_status"] == "PENDING", "the portal-facing name is a projection; the stored status is `status`"
    assert re.fullmatch(r"DSR-[0-9A-F]{12}", out["reference"])
    assert len(world.requests) == 1


@pytest.mark.asyncio
async def test_the_detail_call_works_with_the_returned_id_and_not_with_the_reference(world):
    out = await dsr_routes.create_request(
        payload=dsr_routes.DsrCreate(raw_request="Show me my personal data.", requester_email="a@example.com"),
        user=ADMIN, db=FakeDB(),
    )

    detail = await dsr_routes.get_request(request_id=uuid.UUID(out["id"]), user=ADMIN, db=FakeDB())
    assert detail["id"] == out["id"] and detail["reference"] == out["reference"]

    # The API addresses a case by its UUID. The reference is a label, not a key.
    with pytest.raises(ValueError):
        uuid.UUID(out["reference"])


def test_references_do_not_repeat_and_do_not_count_cases():
    refs = {case_service.new_reference() for _ in range(2000)}
    assert len(refs) == 2000
    assert all(re.fullmatch(r"DSR-[0-9A-F]{12}", r) for r in refs)


@pytest.mark.parametrize("bad", ["@", "a@", "a@b", "a b@c.com", "a@b c.com", "not-an-email", "@b.com"])
def test_a_malformed_requester_email_is_refused_before_anything_is_saved(bad):
    with pytest.raises(ValidationError):
        dsr_routes.DsrCreate(raw_request="x", requester_email=bad)


@pytest.mark.parametrize("good", ["a@b.com", "QA.Tester+dsr1@Example.COM", "  padded@example.com  ", None, ""])
def test_ordinary_requester_emails_are_still_accepted(good):
    dsr_routes.DsrCreate(raw_request="x", requester_email=good)
