"""Audit pass over identity verification (§13) and request classification (§14).

Scope (see the lane brief this session was given):

  1. Identity challenge expiration -- an expired challenge cannot be used to pass
     verification, produces a clear and distinct rejection, and a new challenge can
     always be issued afterwards.
  2. Manual verification always leaves WHO verified and WHY in the shared audit
     trail (audit_service.record), not just on the DsrIdentityVerification row --
     and does so regardless of whether the case's status also happens to change.
  3. The gate cannot be bypassed by a stale "verified" flag carried through a job
     payload: both search_service.run_search and execution_service.execute_action
     re-read identity verification from the database at the moment they run, not
     from whatever was true when the work was queued.
  4. request_classifier.classify() never crashes and never returns a type outside
     CLASSIFIABLE_TYPES, across a wide range of real-world phrasing, typos, empty
     input, unrelated chatter, and non-English text.

Style matches tests/test_dsr_identity.py: pure logic is tested directly; DB-touching
paths go through a minimal in-memory fake, no live Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.dsr.errors import (
    ApprovalRequiredError,
    CaseNotReadyError,
    IdentityFailedError,
    IdentityRequiredError,
    SourceNotAuthorizedError,
)
from app.agents.dsr.rules import request_classifier as rc
from app.agents.dsr.schemas import case
from app.agents.dsr.services import execution_service, identity_service, search_service
from app.db.models import DsrAction, DsrIdentityVerification, DsrRequest

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
ORG = uuid.uuid4()
USER = uuid.uuid4()


class FakeSession:
    """Enough AsyncSession surface for these services: add/flush only."""

    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


def make_request(**kw) -> DsrRequest:
    return DsrRequest(
        id=kw.get("id", uuid.uuid4()),
        org_id=kw.get("org_id", ORG),
        reference=kw.get("reference", "DSR-AUDIT1"),
        raw_request=kw.get("raw_request", "delete my data"),
        request_type=kw.get("request_type", case.DELETION),
        due_at=NOW + timedelta(days=30),
        requester_email=kw.get("requester_email", "person@example.com"),
        status=kw.get("status", case.RECEIVED),
    )


def make_verification(**kw) -> DsrIdentityVerification:
    v = DsrIdentityVerification(
        id=kw.get("id", uuid.uuid4()),
        org_id=kw.get("org_id", ORG),
        request_id=kw.get("request_id", uuid.uuid4()),
        method=kw.get("method", "email_challenge"),
        status=kw.get("status", case.IDV_PENDING),
        challenge_hash=kw.get("challenge_hash"),
        expires_at=kw.get("expires_at"),
        max_attempts=kw.get("max_attempts", 5),
        created_at=kw.get("created_at", NOW),
    )
    v.attempts = kw.get("attempts", 0)
    v.verified_by_user_id = kw.get("verified_by_user_id")
    v.evidence_note = kw.get("evidence_note")
    return v


def _stub_latest(value):
    async def _get(db, request_id, org_id):
        return value
    return _get


def _stub_create(**defaults):
    async def _create(db, *, org_id, request_id, method, challenge_hash, expires_at, max_attempts=5):
        v = DsrIdentityVerification(
            id=uuid.uuid4(), org_id=org_id, request_id=request_id, method=method,
            status=case.IDV_PENDING, challenge_hash=challenge_hash,
            expires_at=expires_at, max_attempts=max_attempts, created_at=defaults.get("created_at", NOW),
        )
        v.attempts = 0
        return v
    return _create


# ── 1. Challenge expiration: issue -> answer in time -> expire -> re-issue ───────

@pytest.mark.asyncio
async def test_verifying_before_expiry_succeeds(monkeypatch):
    request = make_request()
    challenge = "race-condition-safe"
    issued_at = NOW
    v = make_verification(
        status=case.IDV_IN_PROGRESS, challenge_hash=identity_service.hash_challenge(challenge),
        expires_at=issued_at + identity_service.CHALLENGE_TTL,
    )
    monkeypatch.setattr(identity_service.dsr_repository, "get_latest_identity_verification", _stub_latest(v))

    just_before_expiry = issued_at + identity_service.CHALLENGE_TTL - timedelta(seconds=1)
    result = await identity_service.submit_challenge(FakeSession(), request, challenge, now=just_before_expiry)

    assert result.status == case.IDV_VERIFIED
    assert identity_service.is_satisfied(result, now=just_before_expiry)


@pytest.mark.asyncio
async def test_verifying_after_expiry_is_rejected_clearly(monkeypatch):
    """A late answer must not quietly succeed, and the gate's rejection for an
    expired challenge must be distinguishable from "never verified at all"."""
    request = make_request()
    challenge = "race-condition-safe"
    issued_at = NOW
    v = make_verification(
        status=case.IDV_IN_PROGRESS, challenge_hash=identity_service.hash_challenge(challenge),
        expires_at=issued_at + identity_service.CHALLENGE_TTL,
    )
    monkeypatch.setattr(identity_service.dsr_repository, "get_latest_identity_verification", _stub_latest(v))

    just_after_expiry = issued_at + identity_service.CHALLENGE_TTL + timedelta(seconds=1)
    # The correct answer, submitted one second too late, must not verify.
    result = await identity_service.submit_challenge(FakeSession(), request, challenge, now=just_after_expiry)
    assert result.status == case.IDV_EXPIRED
    assert not identity_service.is_satisfied(result, now=just_after_expiry)

    # The gate itself raises a distinct, actionable error -- not the generic
    # "never verified" message -- so an operator knows to re-issue rather than
    # wonder whether the challenge was simply wrong.
    with pytest.raises(IdentityRequiredError) as excinfo:
        await identity_service.assert_identity_satisfied(FakeSession(), request)
    assert "expired" in str(excinfo.value).lower()

    # And it must be distinct from the "failed" (attempts exhausted) rejection.
    failed = make_verification(status=case.IDV_FAILED)
    monkeypatch.setattr(identity_service.dsr_repository, "get_latest_identity_verification", _stub_latest(failed))
    with pytest.raises(IdentityFailedError):
        await identity_service.assert_identity_satisfied(FakeSession(), request)


@pytest.mark.asyncio
async def test_a_new_challenge_can_be_issued_after_expiry(monkeypatch):
    """Expiry must not be a dead end: re-issuing creates a fresh, independently
    usable challenge rather than getting stuck on the expired row."""
    request = make_request()
    monkeypatch.setattr(
        identity_service.dsr_repository, "create_identity_verification", _stub_create(),
    )
    _no_prior_challenges(monkeypatch)
    fresh_start = NOW + timedelta(hours=48)  # well past the first challenge's TTL
    verification, plaintext = await identity_service.start_challenge(FakeSession(), request, now=fresh_start)

    assert verification.status == case.IDV_IN_PROGRESS
    assert verification.expires_at == fresh_start + identity_service.CHALLENGE_TTL
    assert plaintext  # a usable plaintext was actually returned

    # The new challenge verifies normally -- expiry of a PREVIOUS row does not taint it.
    monkeypatch.setattr(
        identity_service.dsr_repository, "get_latest_identity_verification", _stub_latest(verification),
    )
    result = await identity_service.submit_challenge(FakeSession(), request, plaintext, now=fresh_start)
    assert result.status == case.IDV_VERIFIED


# ── 2. Manual verification: WHO and WHY land in the shared audit trail ──────────

@pytest.mark.asyncio
async def test_manual_verification_is_rejected_without_a_reason(monkeypatch):
    request = make_request()
    monkeypatch.setattr(identity_service.dsr_repository, "create_identity_verification", _stub_create())
    calls = []
    monkeypatch.setattr(identity_service.audit_service, "record", _record_spy(calls))

    with pytest.raises(CaseNotReadyError):
        await identity_service.verify_manually(
            FakeSession(), request, reviewer_user_id=USER, evidence_note="   ",
        )
    # An empty-reason claim is refused before anything is written -- including audit.
    assert calls == []


@pytest.mark.asyncio
async def test_manual_verification_always_writes_an_audit_entry_with_who_and_why(monkeypatch):
    """§13's "never claim verification without evidence" has to hold in the audit
    trail itself, not just on the DsrIdentityVerification row -- a reviewer or
    regulator reconstructing a case's history reads audit_logs, and a manual
    verification that left no trace there is indistinguishable from one that was
    never reviewed by anyone.

    This must hold regardless of the case's current status: case_service.transition
    only audits a STATUS CHANGE, and only for a case still in RECEIVED /
    IDENTITY_PENDING / CLASSIFIED (see routes/dsr.py's verify_identity). A case
    already past IDENTITY_VERIFIED that is re-verified manually triggers no such
    transition, so if verify_manually did not audit itself, this event would leave
    no record at all.
    """
    for status in (case.RECEIVED, case.SEARCHING, case.SEARCH_COMPLETED, case.APPROVED):
        request = make_request(status=status)
        monkeypatch.setattr(identity_service.dsr_repository, "create_identity_verification", _stub_create())
        calls = []
        monkeypatch.setattr(identity_service.audit_service, "record", _record_spy(calls))

        result = await identity_service.verify_manually(
            FakeSession(), request, reviewer_user_id=USER,
            evidence_note="Checked passport in person, ref PP-4471", now=NOW,
        )

        assert len(calls) == 1, f"expected exactly one audit entry when case.status={status!r}"
        entry = calls[0]
        assert entry["actor_user_id"] == USER
        assert entry["action"] == case.AUDIT_IDENTITY_VERIFIED
        assert entry["entity_type"] == case.AUDIT_ENTITY
        assert entry["entity_id"] == request.id
        assert entry["after"]["method"] == "manual"
        assert entry["after"]["verified_by_user_id"] == str(USER)
        assert "PP-4471" in entry["after"]["evidence_note"]
        assert result.status == case.IDV_MANUALLY_VERIFIED


def _record_spy(calls):
    async def _record(db, **kw):
        calls.append(kw)
    return _record


@pytest.mark.asyncio
async def test_exhausting_identity_challenge_attempts_writes_an_audit_entry(monkeypatch):
    """AUDIT_IDENTITY_FAILED existed as a constant but was never actually recorded
    anywhere -- a requester burning through every attempt with the wrong answer left
    zero trace in the shared audit trail, indistinguishable from a case nobody ever
    tried to verify."""
    request = make_request()
    challenge = "race-condition-safe"
    v = make_verification(
        status=case.IDV_IN_PROGRESS, challenge_hash=identity_service.hash_challenge(challenge),
        expires_at=NOW + identity_service.CHALLENGE_TTL, max_attempts=3, attempts=2,
    )
    monkeypatch.setattr(identity_service.dsr_repository, "get_latest_identity_verification", _stub_latest(v))
    calls = []
    monkeypatch.setattr(identity_service.audit_service, "record", _record_spy(calls))

    result = await identity_service.submit_challenge(FakeSession(), request, "wrong-guess", now=NOW)

    assert result.status == case.IDV_FAILED
    assert len(calls) == 1
    entry = calls[0]
    assert entry["actor_user_id"] is None
    assert entry["action"] == case.AUDIT_IDENTITY_FAILED
    assert entry["entity_type"] == case.AUDIT_ENTITY
    assert entry["entity_id"] == request.id
    assert entry["after"]["attempts"] == 3


@pytest.mark.asyncio
async def test_a_wrong_attempt_that_does_not_exhaust_the_limit_writes_no_audit_entry(monkeypatch):
    """Only the terminal failure is audited -- an ordinary wrong guess that still has
    attempts remaining is not itself an event worth a permanent trail entry."""
    request = make_request()
    challenge = "race-condition-safe"
    v = make_verification(
        status=case.IDV_IN_PROGRESS, challenge_hash=identity_service.hash_challenge(challenge),
        expires_at=NOW + identity_service.CHALLENGE_TTL, max_attempts=5, attempts=1,
    )
    monkeypatch.setattr(identity_service.dsr_repository, "get_latest_identity_verification", _stub_latest(v))
    calls = []
    monkeypatch.setattr(identity_service.audit_service, "record", _record_spy(calls))

    result = await identity_service.submit_challenge(FakeSession(), request, "wrong-guess", now=NOW)

    assert result.status == case.IDV_IN_PROGRESS
    assert calls == []


# ── 3. Verification cannot be bypassed via a worker picking up a stale job ──────
#
# A case's identity could be satisfied at the moment `POST /search` queues the job
# and no longer satisfied by the time a worker actually runs it (a new challenge was
# issued in between, superseding the old satisfied row -- the realistic version of
# "the state changed in the queueing window", since a VERIFIED row never expires
# retroactively by design). These prove the gate is re-read from the CURRENT latest
# row at the moment of the search/execution, not from any flag carried along from
# queue time.

class _FakeRepoForSearch:
    """Just enough of dsr_repository for search_service.run_search to reach (and
    stop at) the identity gate, or to get past it and fail for an unrelated,
    identifiable reason -- which is itself the proof it got past the gate."""

    def __init__(self, *, latest_verification):
        self._latest = latest_verification

    async def get_latest_identity_verification(self, db, request_id, org_id):
        return self._latest

    async def list_source_authorizations(self, db, org_id):
        # Deliberately empty: if we get this far, identity already passed, and this
        # raises SourceNotAuthorizedError -- a different, recognizable failure.
        return []


@pytest.mark.asyncio
async def test_search_rechecks_identity_at_run_time_not_queue_time(monkeypatch):
    request = make_request(status=case.SEARCHING)

    # At "queue time" the case had a VERIFIED row.
    verified_row = make_verification(
        request_id=request.id, status=case.IDV_VERIFIED, created_at=NOW,
    )
    # Before the worker actually runs, a NEW (unanswered) challenge was issued --
    # e.g. an operator decided the requester needed to re-prove themselves. The
    # newest row is what must govern, not the older satisfied one.
    newer_unsatisfied_row = make_verification(
        request_id=request.id, status=case.IDV_IN_PROGRESS,
        expires_at=NOW + timedelta(hours=1), created_at=NOW + timedelta(minutes=5),
    )

    fake_repo = _FakeRepoForSearch(latest_verification=newer_unsatisfied_row)
    monkeypatch.setattr(search_service, "dsr_repository", fake_repo)
    monkeypatch.setattr(identity_service, "dsr_repository", fake_repo)

    with pytest.raises(IdentityRequiredError):
        await search_service.run_search(FakeSession(), request)


@pytest.mark.asyncio
async def test_search_proceeds_past_the_gate_when_the_latest_row_is_satisfied(monkeypatch):
    """The mirror image of the above: confirms the gate is a real gate (it can be
    passed), not a check that always fails -- it fails afterwards for an unrelated,
    identifiable reason (no authorized source), proving identity itself was fine."""
    request = make_request(status=case.SEARCHING)
    verified_row = make_verification(request_id=request.id, status=case.IDV_VERIFIED, created_at=NOW)

    fake_repo = _FakeRepoForSearch(latest_verification=verified_row)
    monkeypatch.setattr(search_service, "dsr_repository", fake_repo)
    monkeypatch.setattr(identity_service, "dsr_repository", fake_repo)

    with pytest.raises(SourceNotAuthorizedError):
        await search_service.run_search(FakeSession(), request)


class _FakeRepoForExecution:
    def __init__(self, *, latest_verification, action):
        self._latest = latest_verification
        self._action = action

    async def get_latest_identity_verification(self, db, request_id, org_id):
        return self._latest

    async def get_action(self, db, action_id, org_id):
        return self._action

    async def latest_approval_for_action(self, db, action_id, org_id):
        return None


def make_action(**kw) -> DsrAction:
    return DsrAction(
        id=kw.get("id", uuid.uuid4()),
        org_id=kw.get("org_id", ORG),
        request_id=kw.get("request_id"),
        plan_id=uuid.uuid4(),
        source_name="crm",
        table_name="customers",
        record_reference={"id": 1},
        operation=case.OP_DELETE_RECORD,
        operation_payload={},
        reason="requester asked for erasure",
        expected_result="record deleted",
        risk="high",
        requires_approval=kw.get("requires_approval", True),
        status=kw.get("status", "proposed"),
        data_source_id=kw.get("data_source_id", uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_execution_rechecks_identity_at_run_time_not_approval_time(monkeypatch):
    """Mirrors the search-side check for execute_action: a verification satisfied
    when the action was approved must not let execution through if the LATEST
    verification row has since moved to an unsatisfied state."""
    request = make_request(status=case.APPROVED)
    action = make_action(request_id=request.id)

    newer_unsatisfied_row = make_verification(
        request_id=request.id, status=case.IDV_IN_PROGRESS,
        expires_at=NOW + timedelta(hours=1), created_at=NOW + timedelta(minutes=5),
    )
    fake_repo = _FakeRepoForExecution(latest_verification=newer_unsatisfied_row, action=action)
    monkeypatch.setattr(execution_service, "dsr_repository", fake_repo)
    monkeypatch.setattr(identity_service, "dsr_repository", fake_repo)

    with pytest.raises(IdentityRequiredError):
        await execution_service.execute_action(FakeSession(), request, action.id)


@pytest.mark.asyncio
async def test_execution_proceeds_past_the_gate_when_the_latest_row_is_satisfied(monkeypatch):
    """Confirms execute_action's identity check is a real, passable gate: with a
    satisfied latest row it gets past identity and fails afterwards for an
    unrelated, identifiable reason (no current approval) -- proving the gate itself
    was not the blocker."""
    request = make_request(status=case.APPROVED)
    action = make_action(request_id=request.id, requires_approval=True)

    verified_row = make_verification(request_id=request.id, status=case.IDV_VERIFIED, created_at=NOW)
    fake_repo = _FakeRepoForExecution(latest_verification=verified_row, action=action)
    monkeypatch.setattr(execution_service, "dsr_repository", fake_repo)
    monkeypatch.setattr(identity_service, "dsr_repository", fake_repo)

    monkeypatch.setattr(
        execution_service.approval_service, "is_approval_current", lambda approval, now=None: False
    )

    with pytest.raises(ApprovalRequiredError):
        await execution_service.execute_action(FakeSession(), request, action.id)


# ── 4. Classification: never crashes, never invents a type ──────────────────────

CLASSIFICATION_SAMPLES = [
    # (label, text)
    ("empty string", ""),
    ("whitespace only", "   \n\t  "),
    ("none-ish", None),
    ("unrelated question", "what's your refund policy?"),
    ("unrelated small talk", "hi, just checking in, how are you today"),
    ("gibberish", "asdkjhaskjdh qweoiuqwe xkcd zzz"),
    ("digits only", "12345 67890"),
    ("non-english (spanish deletion)", "Por favor, borre todos mis datos personales"),
    ("non-english (hindi access, transliterated)", "mujhe mera data chahiye"),
    ("emoji / symbols only", "!!! ??? ***"),
    ("real intent + chatter", "Hi! Hope you're well. Anyway, please delete my account. Thanks so much!"),
    ("typo'd deletion", "pls delete my acount ASAP"),
    ("typo'd correction", "my adress is rong, plz corect it"),
    ("shouted keyword", "DELETE"),
    ("mixed case phrase", "PLEASE Delete ALL MY personal Data"),
    ("very long repeated token", "delete" * 500),
    ("conflicting intents, explicit join", "send me a copy of my data and then delete my account"),
    ("conflicting intents, semicolon", "export my data; delete my account"),
    ("single clause subsuming export+access", "please download a copy of my data"),
    ("single clause subsuming correction+access", "my phone number is wrong, please correct it"),
    ("information request", "why do you process my location data?"),
    ("retention question", "how long do you keep my records?"),
    ("html-ish injection attempt", "<script>alert(1)</script> delete my data"),
    ("sql-ish injection attempt", "'; DROP TABLE users; -- delete my data"),
    ("extremely short", "hi"),
    ("single punctuation", "?"),
]


@pytest.mark.parametrize("label,text", CLASSIFICATION_SAMPLES, ids=[s[0] for s in CLASSIFICATION_SAMPLES])
def test_classify_never_crashes_and_never_invents_a_type(label, text):
    result = rc.classify(text)
    assert result.request_type in case.CLASSIFIABLE_TYPES or result.request_type is None
    if result.request_type is not None:
        assert result.request_type in case.CLASSIFIABLE_TYPES
    assert 0.0 <= result.confidence <= 1.0
    assert isinstance(result.evidence, tuple)
    # ambiguous=True is the only case request_type may be None; never a silent gap.
    assert result.ambiguous or result.request_type is not None


def test_classify_empty_and_none_are_unresolved_not_a_guess():
    for text in ("", "   ", None):
        result = rc.classify(text)
        assert result.request_type == case.OTHER
        assert result.confidence == 0.0
        assert not result.ambiguous


def test_classify_explicit_multi_intent_is_ambiguous_not_subsumed():
    result = rc.classify("send me a copy of my data and then delete my account")
    assert result.ambiguous
    assert result.request_type is None
    assert set(result.candidates) == {case.ACCESS, case.DELETION}


def test_classify_single_clause_subsumption_still_resolves():
    """'download a copy of my data' reads as EXPORT and ACCESS but is one request --
    must resolve to the narrower type, not go ambiguous."""
    result = rc.classify("please download a copy of my data")
    assert result.request_type == case.EXPORT
    assert not result.ambiguous


@pytest.mark.parametrize("text", [s[1] for s in CLASSIFICATION_SAMPLES if s[1]])
def test_classify_result_type_is_always_in_the_closed_vocabulary_or_none(text):
    result = rc.classify(text)
    assert result.request_type is None or result.request_type in case.CLASSIFIABLE_TYPES
    # The closed vocabulary never includes the pre-classification default.
    assert result.request_type != case.UNCLASSIFIED


def test_coerce_model_type_never_admits_an_invented_type():
    assert rc.coerce_model_type("partial_deletion") is None
    assert rc.coerce_model_type("gdpr_request") is None
    assert rc.coerce_model_type("") is None
    assert rc.coerce_model_type(None) is None
    assert rc.coerce_model_type("  ACCESS  ") == case.ACCESS
    for t in case.CLASSIFIABLE_TYPES:
        assert rc.coerce_model_type(t) == t


def _no_prior_challenges(monkeypatch):
    """The challenge limits read earlier rows; a fresh case has none."""
    async def _none(db, request_id, org_id):
        return []

    async def _zero(db, org_id, requester_email, since):
        return 0

    monkeypatch.setattr(identity_service.dsr_repository, "list_identity_verifications", _none)
    monkeypatch.setattr(identity_service.dsr_repository, "count_requester_challenges", _zero)
