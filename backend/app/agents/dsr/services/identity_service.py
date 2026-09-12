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
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dsr.errors import CaseNotReadyError, IdentityFailedError, IdentityRequiredError
from app.agents.dsr.schemas import case
from app.db.models import DsrIdentityVerification, DsrRequest
from app.db.repositories import dsr_repository

# A verification is good for this long once issued. Long enough for a person to find
# the email, short enough that a leaked challenge has a small window.
CHALLENGE_TTL = timedelta(hours=24)
MAX_ATTEMPTS = 5
# 32 hex characters from a CSPRNG. Not a 6-digit code: this is emailed, not typed
# from memory, so there is no usability reason to make it guessable.
_CHALLENGE_BYTES = 16


def generate_challenge() -> str:
    return secrets.token_hex(_CHALLENGE_BYTES)


def hash_challenge(challenge: str) -> str:
    return hashlib.sha256(challenge.strip().encode()).hexdigest()


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


async def start_challenge(
    db: AsyncSession, request: DsrRequest, *, now: datetime | None = None
) -> tuple[DsrIdentityVerification, str]:
    """Issue an email challenge. Returns (row, plaintext_challenge).

    The plaintext is returned exactly once, for the caller to deliver to the
    requester. It is not stored and cannot be recovered afterwards -- a lost
    challenge means issuing a new one, which is the correct outcome.
    """
    if not request.requester_email:
        raise CaseNotReadyError(
            f"Case {request.reference} has no requester email; an email challenge "
            "cannot be issued. Use manual verification instead."
        )
    moment = now or datetime.now(UTC)
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
    matched = bool(verification.challenge_hash) and hmac.compare_digest(
        verification.challenge_hash, hash_challenge(submitted or "")
    )

    if matched:
        verification.status = case.IDV_VERIFIED
        verification.verified_at = moment
    elif verification.attempts >= verification.max_attempts:
        # Burned. A fresh challenge must be issued; this row cannot be retried.
        verification.status = case.IDV_FAILED
    await db.flush()
    return verification


async def verify_manually(
    db: AsyncSession,
    request: DsrRequest,
    *,
    reviewer_user_id: uuid.UUID,
    evidence_note: str,
    now: datetime | None = None,
) -> DsrIdentityVerification:
    """Record that a human verified this requester out of band.

    Both arguments are mandatory and the note must be substantive. §13 says "do not
    claim verification without evidence" -- an empty note is a claim without
    evidence, so it is refused rather than stored.
    """
    if not evidence_note or not evidence_note.strip():
        raise CaseNotReadyError(
            "Manual identity verification requires an evidence note describing what "
            "was checked (§13: verification is never claimed without evidence)."
        )
    moment = now or datetime.now(UTC)
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
    verification.evidence_note = evidence_note.strip()
    await db.flush()
    return verification
