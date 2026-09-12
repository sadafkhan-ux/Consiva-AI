"""Identity verification -- the security boundary before any sensitive search (§13).

These tests exercise the pure logic (hashing, expiry, status resolution) directly and
the DB-touching paths through an in-memory fake session, so they run without Postgres
like the rest of this suite.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.agents.dsr.errors import CaseNotReadyError, IdentityFailedError, IdentityRequiredError
from app.agents.dsr.schemas import case
from app.agents.dsr.services import identity_service as ids
from app.db.models import DsrIdentityVerification, DsrRequest

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


class FakeSession:
    """Enough AsyncSession surface for these services: add/flush, and a stash the
    repository stubs read back."""

    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


def make_request(**kw) -> DsrRequest:
    return DsrRequest(
        id=kw.get("id", uuid.uuid4()),
        org_id=kw.get("org_id", uuid.uuid4()),
        reference=kw.get("reference", "DSR-TEST01"),
        raw_request="delete my data",
        due_at=NOW + timedelta(days=30),
        requester_email=kw.get("requester_email", "person@example.com"),
        status=case.RECEIVED,
    )


def make_verification(**kw) -> DsrIdentityVerification:
    v = DsrIdentityVerification(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        method=kw.get("method", "email_challenge"),
        status=kw.get("status", case.IDV_PENDING),
        challenge_hash=kw.get("challenge_hash"),
        expires_at=kw.get("expires_at"),
        max_attempts=kw.get("max_attempts", 5),
    )
    v.attempts = kw.get("attempts", 0)
    return v


# ── The challenge is never recoverable from what is stored ───────────────────────

def test_challenge_is_stored_only_as_a_hash():
    challenge = ids.generate_challenge()
    digest = ids.hash_challenge(challenge)
    assert challenge not in digest
    assert len(digest) == 64
    assert ids.hash_challenge(challenge) == digest, "hashing must be stable"


def test_different_challenges_hash_differently():
    assert ids.hash_challenge("aaa") != ids.hash_challenge("bbb")


def test_generated_challenges_are_unpredictable():
    assert len({ids.generate_challenge() for _ in range(200)}) == 200


# ── Expiry is applied on read, not by a sweep ────────────────────────────────────

def test_a_stale_pending_row_reads_as_expired():
    """A background sweep that never ran must not leave a challenge usable."""
    v = make_verification(status=case.IDV_PENDING, expires_at=NOW - timedelta(minutes=1))
    assert ids.effective_status(v, now=NOW) == case.IDV_EXPIRED
    assert not ids.is_satisfied(v, now=NOW)


def test_a_live_pending_row_stays_pending():
    v = make_verification(status=case.IDV_IN_PROGRESS, expires_at=NOW + timedelta(hours=1))
    assert ids.effective_status(v, now=NOW) == case.IDV_IN_PROGRESS


def test_a_completed_verification_does_not_expire_retroactively():
    """Expiry governs how long the challenge may be ANSWERED, not how long a
    successful answer counts for."""
    v = make_verification(status=case.IDV_VERIFIED, expires_at=NOW - timedelta(days=5))
    assert ids.effective_status(v, now=NOW) == case.IDV_VERIFIED
    assert ids.is_satisfied(v, now=NOW)


def test_missing_verification_is_pending_not_satisfied():
    assert ids.effective_status(None) == case.IDV_PENDING
    assert not ids.is_satisfied(None)


@pytest.mark.parametrize("status", sorted(case.IDV_STATUSES - case.IDV_SATISFIED))
def test_only_verified_and_manually_verified_satisfy_the_gate(status):
    v = make_verification(status=status, expires_at=NOW + timedelta(hours=1))
    assert not ids.is_satisfied(v, now=NOW), f"{status} must not open the gate"


# ── The gate itself ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_refuses_an_unverified_case(monkeypatch):
    request = make_request()
    monkeypatch.setattr(
        ids.dsr_repository, "get_latest_identity_verification",
        _stub_latest(None),
    )
    with pytest.raises(IdentityRequiredError):
        await ids.assert_identity_satisfied(FakeSession(), request)


@pytest.mark.asyncio
async def test_gate_refuses_an_expired_verification(monkeypatch):
    request = make_request()
    v = make_verification(status=case.IDV_IN_PROGRESS, expires_at=NOW - timedelta(days=2))
    monkeypatch.setattr(ids.dsr_repository, "get_latest_identity_verification", _stub_latest(v))
    with pytest.raises(IdentityRequiredError):
        await ids.assert_identity_satisfied(FakeSession(), request)


@pytest.mark.asyncio
async def test_gate_refuses_a_failed_verification(monkeypatch):
    request = make_request()
    v = make_verification(status=case.IDV_FAILED)
    monkeypatch.setattr(ids.dsr_repository, "get_latest_identity_verification", _stub_latest(v))
    with pytest.raises(IdentityFailedError):
        await ids.assert_identity_satisfied(FakeSession(), request)


@pytest.mark.asyncio
async def test_gate_allows_a_verified_case(monkeypatch):
    request = make_request()
    v = make_verification(status=case.IDV_VERIFIED)
    monkeypatch.setattr(ids.dsr_repository, "get_latest_identity_verification", _stub_latest(v))
    assert await ids.assert_identity_satisfied(FakeSession(), request) is v


# ── Submitting a challenge ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_correct_challenge_verifies(monkeypatch):
    request = make_request()
    challenge = "abc123"
    v = make_verification(
        status=case.IDV_IN_PROGRESS, challenge_hash=ids.hash_challenge(challenge),
        expires_at=NOW + timedelta(hours=1),
    )
    monkeypatch.setattr(ids.dsr_repository, "get_latest_identity_verification", _stub_latest(v))
    result = await ids.submit_challenge(FakeSession(), request, challenge, now=NOW)
    assert result.status == case.IDV_VERIFIED
    assert result.verified_at == NOW


@pytest.mark.asyncio
async def test_wrong_challenge_counts_an_attempt_without_verifying(monkeypatch):
    request = make_request()
    v = make_verification(
        status=case.IDV_IN_PROGRESS, challenge_hash=ids.hash_challenge("right"),
        expires_at=NOW + timedelta(hours=1),
    )
    monkeypatch.setattr(ids.dsr_repository, "get_latest_identity_verification", _stub_latest(v))
    result = await ids.submit_challenge(FakeSession(), request, "wrong", now=NOW)
    assert result.status == case.IDV_IN_PROGRESS
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_attempts_are_capped(monkeypatch):
    """A challenge that could be guessed indefinitely is not a control."""
    request = make_request()
    v = make_verification(
        status=case.IDV_IN_PROGRESS, challenge_hash=ids.hash_challenge("right"),
        expires_at=NOW + timedelta(hours=1), max_attempts=3, attempts=2,
    )
    monkeypatch.setattr(ids.dsr_repository, "get_latest_identity_verification", _stub_latest(v))
    result = await ids.submit_challenge(FakeSession(), request, "wrong", now=NOW)
    assert result.attempts == 3
    assert result.status == case.IDV_FAILED


@pytest.mark.asyncio
async def test_expired_challenge_cannot_be_answered_even_correctly(monkeypatch):
    request = make_request()
    challenge = "abc123"
    v = make_verification(
        status=case.IDV_IN_PROGRESS, challenge_hash=ids.hash_challenge(challenge),
        expires_at=NOW - timedelta(minutes=1),
    )
    monkeypatch.setattr(ids.dsr_repository, "get_latest_identity_verification", _stub_latest(v))
    result = await ids.submit_challenge(FakeSession(), request, challenge, now=NOW)
    assert result.status == case.IDV_EXPIRED


@pytest.mark.asyncio
async def test_empty_submission_never_matches_a_null_hash(monkeypatch):
    """A row with no challenge_hash (a manual verification in progress) must not be
    satisfiable by submitting an empty string."""
    request = make_request()
    v = make_verification(status=case.IDV_IN_PROGRESS, challenge_hash=None,
                          expires_at=NOW + timedelta(hours=1))
    monkeypatch.setattr(ids.dsr_repository, "get_latest_identity_verification", _stub_latest(v))
    result = await ids.submit_challenge(FakeSession(), request, "", now=NOW)
    assert result.status != case.IDV_VERIFIED


# ── Manual verification demands evidence ─────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["", "   ", "\n"])
async def test_manual_verification_requires_an_evidence_note(monkeypatch, note):
    """§13: do not claim verification without evidence."""
    request = make_request()
    monkeypatch.setattr(ids.dsr_repository, "create_identity_verification", _stub_create())
    with pytest.raises(CaseNotReadyError):
        await ids.verify_manually(
            FakeSession(), request, reviewer_user_id=uuid.uuid4(), evidence_note=note,
        )


@pytest.mark.asyncio
async def test_manual_verification_records_actor_and_note(monkeypatch):
    request = make_request()
    monkeypatch.setattr(ids.dsr_repository, "create_identity_verification", _stub_create())
    reviewer = uuid.uuid4()
    result = await ids.verify_manually(
        FakeSession(), request, reviewer_user_id=reviewer,
        evidence_note="Checked passport in person, ref PP-4471", now=NOW,
    )
    assert result.status == case.IDV_MANUALLY_VERIFIED
    assert result.verified_by_user_id == reviewer
    assert "PP-4471" in result.evidence_note
    assert ids.is_satisfied(result, now=NOW)


@pytest.mark.asyncio
async def test_email_challenge_needs_an_email(monkeypatch):
    request = make_request(requester_email=None)
    with pytest.raises(CaseNotReadyError):
        await ids.start_challenge(FakeSession(), request, now=NOW)


# ── helpers ──────────────────────────────────────────────────────────────────────

def _stub_latest(value):
    async def _get(db, request_id, org_id):
        return value
    return _get


def _stub_create():
    async def _create(db, *, org_id, request_id, method, challenge_hash, expires_at, max_attempts=5):
        v = DsrIdentityVerification(
            id=uuid.uuid4(), org_id=org_id, request_id=request_id, method=method,
            status=case.IDV_PENDING, challenge_hash=challenge_hash,
            expires_at=expires_at, max_attempts=max_attempts,
        )
        v.attempts = 0
        return v
    return _create
