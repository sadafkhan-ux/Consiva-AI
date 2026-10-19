"""Identity verification for a DSR case (prompt §13).

This is a security boundary, not a workflow step. Until it passes, nothing reads the
requester's records -- `assert_identity_satisfied` is what every search path calls,
and it is the only thing standing between "someone typed an email address into a
form" and "we handed them that person's data".

Design decisions worth keeping:

  * The challenge is stored as a SHA-256 hash, never in the clear. A database dump
    therefore cannot be replayed to pass verification. It is compared with
    `hmac.compare_digest`, so a wrong guess takes the same time as a right one.

  * Attempts are capped and expiry is enforced on READ, not by a sweep. A row that
    has sat past its expiry is treated as expired the moment anyone looks at it,
    so a stalled background job can never leave a verification usable forever.

  * Manual verification requires both an actor and an evidence note. "Verified
    because the reviewer said so" with no record of what they checked is exactly
    the claim-without-evidence §13 forbids.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.errors import (
    CaseNotReadyError,
    IdentityFailedError,
    IdentityLimitError,
    IdentityRequiredError,
)
from app.agents.dsr.schemas import case
from app.db.models import DsrIdentityVerification, DsrRequest
from app.db.repositories import dsr_repository
from app.services import audit_service

# A verification is good for this long once issued. Long enough for a person to find
# the email, short enough that a leaked challenge has a small window.
CHALLENGE_TTL = timedelta(hours=24)
MAX_ATTEMPTS = 5

# Limits that stop a lockout being undone by simply asking for another code. Each is enforced
# from rows that already exist (dsr_identity_verifications), so nothing new is stored and they
# cannot be bypassed by calling a different route.
#
#   * a case gets MAX_CHALLENGES_PER_CASE codes in total, expired or not; after that the
#     requester has to be verified by a person (manual verification, which records who and why)
#   * CHALLENGE_COOLDOWN between two codes on one case
#   * LOCKOUT_COOLDOWN after a code has been burned by wrong answers
#   * MAX_CHALLENGES_PER_REQUESTER per REQUESTER (normalised email) across every case in the
#     organisation within REQUESTER_WINDOW, so opening a fresh case is no way round the above
MAX_CHALLENGES_PER_CASE = 3
CHALLENGE_COOLDOWN = timedelta(seconds=60)
LOCKOUT_COOLDOWN = timedelta(minutes=15)
MAX_CHALLENGES_PER_REQUESTER = 6
REQUESTER_WINDOW = timedelta(hours=24)

# The code is something a person reads out or types, so it uses an alphabet without the
# characters people confuse (0/O, 1/I/L) and is shown in groups: "K7QM-4TXR-9BWD". Twelve
# characters from 31 is about 59 bits from a CSPRNG -- far beyond guessing within five attempts,
# and it is stored only as a hash, as before.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 12
_CODE_GROUP = 4

# Manual verification must say HOW the person was verified, against WHAT, and why that is enough.
MANUAL_METHODS = {
    "id_document": "ID document checked",
    "account_login": "Verified through their signed-in account",
    "known_fact_match": "Matched against facts only they should know",
    "in_person": "Verified in person",
    "other": "Other (explained in the note)",
}
MIN_MANUAL_NOTE_LENGTH = 10


def generate_challenge() -> str:
    chars = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
    return "-".join(chars[i:i + _CODE_GROUP] for i in range(0, _CODE_LENGTH, _CODE_GROUP))


def _normalize_code(challenge: str) -> str:
    """Case, spaces and dashes do not matter to a person typing a code."""
    return re.sub(r"[\s-]", "", challenge or "").upper()


def hash_challenge(challenge: str) -> str:
    return hashlib.sha256(_normalize_code(challenge).encode()).hexdigest()


def _legacy_hash(challenge: str) -> str:
    """How challenges were hashed before codes became groups. A 32-character code issued
    before this change is still pending for up to its 24 hour life and must still verify."""
    return hashlib.sha256((challenge or "").strip().encode()).hexdigest()


def is_expired(verification: DsrIdentityVerification, *, now: datetime | None = None) -> bool:
    if verification.expires_at is None:
        return False
    return (now or datetime.now(UTC)) > verification.expires_at


def effective_status(verification: DsrIdentityVerification | None, *, now: datetime | None = None) -> str:
    """The status as it actually stands right now, with expiry applied.

    Read this rather than `verification.status` anywhere a decision depends on it:
    a row can say "pending" while its expiry has long passed, and treating that as
    still-pending would leave a stale challenge usable.
    """
    if verification is None:
        return case.IDV_PENDING
    if verification.status in case.IDV_SATISFIED:
        # A completed verification does not expire retroactively -- expiry governs
        # how long the CHALLENGE may be answered, not how long the answer counts.
        return verification.status
    if verification.status in (case.IDV_PENDING, case.IDV_IN_PROGRESS) and is_expired(verification, now=now):
        return case.IDV_EXPIRED
    return verification.status


def is_satisfied(verification: DsrIdentityVerification | None, *, now: datetime | None = None) -> bool:
    return effective_status(verification, now=now) in case.IDV_SATISFIED


async def assert_identity_satisfied(
    db: AsyncSession, request: DsrRequest
) -> DsrIdentityVerification:
    """The gate. Raises unless this case's identity verification has actually passed.

    Every path that reads the requester's records calls this first. It re-reads from
    the database rather than trusting the case's status column, so a case whose
    status was advanced by some other route still cannot search without a real,
    unexpired verification row behind it.
    """
    verification = await dsr_repository.get_latest_identity_verification(db, request.id, request.org_id)
    status = effective_status(verification)
    if status in case.IDV_SATISFIED:
        return verification  # type: ignore[return-value]  -- IDV_SATISFIED implies non-None
    if status == case.IDV_FAILED:
        raise IdentityFailedError(
            f"Identity verification for case {request.reference} failed; "
            "it must be re-issued or manually verified before any search."
        )
    if status == case.IDV_EXPIRED:
        raise IdentityRequiredError(
            f"Identity verification for case {request.reference} expired; "
            "issue a new challenge before searching."
        )
    raise IdentityRequiredError(
        f"Case {request.reference} has not completed identity verification "
        f"(status: {status}); no record may be searched until it does."
    )


async def _enforce_challenge_limits(
    db: AsyncSession, request: DsrRequest, moment: datetime
) -> None:
    """Refuse a new code when the case or the requester has had its share (see the constants)."""
    prior = [
        v for v in await dsr_repository.list_identity_verifications(db, request.id, request.org_id)
        if v.method == "email_challenge"
    ]
    if len(prior) >= MAX_CHALLENGES_PER_CASE:
        raise IdentityLimitError(
            f"Case {request.reference} has already had {MAX_CHALLENGES_PER_CASE} verification "
            "codes. No more can be generated; verify the requester manually instead."
        )
    latest = prior[-1] if prior else None
    if latest is not None:
        issued = latest.created_at
        last_activity = latest.updated_at or issued
        if (
            effective_status(latest, now=moment) == case.IDV_FAILED
            and last_activity is not None
            and moment < last_activity + LOCKOUT_COOLDOWN
        ):
            wait = (last_activity + LOCKOUT_COOLDOWN - moment).total_seconds()
            raise IdentityLimitError(
                "Too many wrong codes were entered for this case. A new code can be generated "
                f"in {int(wait // 60) + 1} minute(s), or verify the requester manually."
            )
        if issued is not None and moment < issued + CHALLENGE_COOLDOWN:
            wait = int((issued + CHALLENGE_COOLDOWN - moment).total_seconds()) + 1
            raise IdentityLimitError(
                "A code was generated for this case moments ago. "
                f"Wait {wait} second(s) before generating another."
            )
    if request.requester_email:
        issued_to_requester = await dsr_repository.count_requester_challenges(
            db, request.org_id, request.requester_email, moment - REQUESTER_WINDOW
        )
        if issued_to_requester >= MAX_CHALLENGES_PER_REQUESTER:
            raise IdentityLimitError(
                "This requester has had the maximum number of verification codes in 24 hours "
                "across all their cases. Verify them manually, or try again tomorrow."
            )


async def start_challenge(
    db: AsyncSession, request: DsrRequest, *, now: datetime | None = None
) -> tuple[DsrIdentityVerification, str]:
    """Issue a verification code. Returns (row, plaintext_challenge).

    The plaintext is returned exactly once, for the caller to deliver to the
    requester. It is not stored and cannot be recovered afterwards -- a lost
    challenge means issuing a new one, which is the correct outcome. How many new ones a
    case or requester may have is limited, so asking again is not a way round a lockout.
    """
    if not request.requester_email:
        raise CaseNotReadyError(
            f"Case {request.reference} has no requester email; an email challenge "
            "cannot be issued. Use manual verification instead."
        )
    moment = now or datetime.now(UTC)
    await _enforce_challenge_limits(db, request, moment)
    challenge = generate_challenge()
    verification = await dsr_repository.create_identity_verification(
        db,
        org_id=request.org_id,
        request_id=request.id,
        method="email_challenge",
        challenge_hash=hash_challenge(challenge),
        expires_at=moment + CHALLENGE_TTL,
        max_attempts=MAX_ATTEMPTS,
    )
    verification.status = case.IDV_IN_PROGRESS
    await db.flush()
    return verification, challenge


async def submit_challenge(
    db: AsyncSession, request: DsrRequest, submitted: str, *, now: datetime | None = None
) -> DsrIdentityVerification:
    """Check a submitted challenge. Never raises on a wrong answer -- it records the
    attempt and returns the row, so the caller can report failure without the
    difference between "wrong code" and "no such case" being observable."""
    moment = now or datetime.now(UTC)
    verification = await dsr_repository.get_latest_identity_verification(db, request.id, request.org_id)
    if verification is None:
        raise CaseNotReadyError(
            f"Case {request.reference} has no identity verification in progress."
        )

    status = effective_status(verification, now=moment)
    if status in case.IDV_SATISFIED:
        return verification  # already done; re-submitting is a no-op, not an error
    if status == case.IDV_EXPIRED:
        verification.status = case.IDV_EXPIRED
        await db.flush()
        return verification
    if status == case.IDV_FAILED:
        return verification

    verification.attempts += 1
    # Both comparisons always run, so a wrong guess takes the same time either way.
    stored = verification.challenge_hash or ""
    matched_now = hmac.compare_digest(stored, hash_challenge(submitted))
    matched_legacy = hmac.compare_digest(stored, _legacy_hash(submitted))
    matched = bool(stored) and (matched_now or matched_legacy)

    if matched:
        verification.status = case.IDV_VERIFIED
        verification.verified_at = moment
    elif verification.attempts >= verification.max_attempts:
        # Burned. A fresh challenge must be issued; this row cannot be retried. The time is
        # kept so the lockout cooldown counts from the last wrong answer, not from issue.
        verification.status = case.IDV_FAILED
        verification.updated_at = moment
        await db.flush()
        # The only place this case's identity verification permanently failed; with
        # no audit entry, a reviewer investigating why a case stalled (or an incident
        # review afterward) would see no trace of the exhausted attempts at all. No
        # actor_user_id -- this is the (unauthenticated) requester's own submission.
        await audit_service.record(
            db, org_id=request.org_id, actor_user_id=None,
            action=case.AUDIT_IDENTITY_FAILED, entity_type=case.AUDIT_ENTITY,
            entity_id=request.id,
            after={"verification_id": str(verification.id), "attempts": verification.attempts},
        )
        return verification
    await db.flush()
    return verification


async def verify_manually(
    db: AsyncSession,
    request: DsrRequest,
    *,
    reviewer_user_id: uuid.UUID,
    evidence_note: str,
    method: str | None = None,
    reference: str | None = None,
    now: datetime | None = None,
) -> DsrIdentityVerification:
    """Record that a human verified this requester out of band.

    The note is mandatory and must be substantive. §13 says "do not claim verification
    without evidence" -- an empty note is a claim without evidence, so it is refused rather
    than stored. When `method` is given (the screens always give it) the verification must
    also name WHAT was checked -- an ID reference, a login, the facts compared -- and the method
    and reference are written to the audit trail as separate fields, not buried in prose.

    This writes its own audit_logs entry, unconditionally -- it does not rely on the
    caller also moving the case's status (case_service.transition only audits a
    STATUS change, and only fires for a handful of pre-search statuses). A case that
    is already past IDENTITY_VERIFIED (e.g. a verification is being redone mid-flow)
    still gets this claim recorded with who made it and what they checked; without
    this, that event left no trace in the shared audit trail at all.
    """
    if not evidence_note or not evidence_note.strip():
        raise CaseNotReadyError(
            "Manual identity verification requires an evidence note describing what "
            "was checked (§13: verification is never claimed without evidence)."
        )
    note = evidence_note.strip()
    reference = (reference or "").strip() or None
    if method is not None:
        if method not in MANUAL_METHODS:
            raise CaseNotReadyError(
                f"{method!r} is not a verification method; expected one of {sorted(MANUAL_METHODS)}"
            )
        if not reference:
            raise CaseNotReadyError(
                "Manual verification needs a reference for what was checked "
                "(for example a document number or the account it was matched to)."
            )
        if len(note) < MIN_MANUAL_NOTE_LENGTH:
            raise CaseNotReadyError(
                f"Describe what was checked in at least {MIN_MANUAL_NOTE_LENGTH} characters."
            )
    moment = now or datetime.now(UTC)
    stored_note = (
        f"{MANUAL_METHODS[method]}. Reference: {reference}. {note}" if method else note
    )
    verification = await dsr_repository.create_identity_verification(
        db,
        org_id=request.org_id,
        request_id=request.id,
        method="manual",
        challenge_hash=None,
        expires_at=None,
        max_attempts=0,
    )
    verification.status = case.IDV_MANUALLY_VERIFIED
    verification.verified_at = moment
    verification.verified_by_user_id = reviewer_user_id
    verification.evidence_note = stored_note
    await db.flush()
    await audit_service.record(
        db,
        org_id=request.org_id,
        actor_user_id=reviewer_user_id,
        action=case.AUDIT_IDENTITY_VERIFIED,
        entity_type=case.AUDIT_ENTITY,
        entity_id=request.id,
        after={
            "method": "manual",
            "verification_method": method,
            "reference": reference,
            "verification_id": str(verification.id),
            "verified_by_user_id": str(reviewer_user_id),
            "evidence_note": note,
        },
    )
    return verification
