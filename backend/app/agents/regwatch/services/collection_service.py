"""Collecting a source, comparing it, and raising a finding (spec §4 steps 2-4, §10).

The order of operations here is the agent's central guarantee, so it is worth stating
plainly: the collection row is written BEFORE anything is compared, and it is written
whether the fetch succeeded or failed. A failure is a row with `status='failed'` and a
code -- never an early return, never a swallowed exception, never a quiet "nothing
changed".

WHY A FAILED COLLECTION STILL PRODUCES A FINDING
------------------------------------------------
It would be easy to treat a fetch failure as an operational blip and log it. That is
exactly the behaviour the spec's last guardrail forbids, and the reason is not
pedantry: a compliance team that sees a green dashboard concludes it is being watched.
If the regulator's site has been unreachable for a week, the truthful statement is "we
do not know what that page says now", and the only place a compliance team will
reliably read it is on the same list as everything else demanding attention.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.regwatch.connectors import feed_source, http_source
from app.agents.regwatch.errors import (
    ContentUnusableError,
    RegWatchError,
    SourceNotAuthorizedError,
    SourceUnreachableError,
)
from app.agents.regwatch.rules import change_detection
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import lifecycle, source_service
from app.db.models import (
    RegWatchBaseline,
    RegWatchChange,
    RegWatchCollection,
    RegWatchFinding,
    RegWatchSource,
)
from app.db.repositories import regwatch_repository as repo
from app.services import audit_service

logger = logging.getLogger(__name__)

_REFERENCE_BYTES = 6  # 12 hex chars, same sizing as Agent 4's incident reference


def new_reference() -> str:
    return f"REG-{secrets.token_hex(_REFERENCE_BYTES).upper()}"


async def _unused_reference(db: AsyncSession, org_id: uuid.UUID) -> str:
    for _ in range(5):
        candidate = new_reference()
        if not await repo.reference_exists(db, org_id, candidate):
            return candidate
    raise RegWatchError("could not allocate an unused finding reference after 5 attempts")


def _resolve_credential(source: RegWatchSource) -> str | None:
    """Read a secret by the NAME the source configuration gives.

    Every error names the ref, never a value -- these messages reach logs and API
    responses. Same contract as Agents 2 and 3.
    """
    if not source.credential_ref:
        return None
    secret = os.getenv(source.credential_ref)
    if secret is None:
        raise SourceNotAuthorizedError(
            f"credential_ref {source.credential_ref!r} is not set in this environment; "
            "configure the secret before collecting this source"
        )
    return secret


async def collect(
    db: AsyncSession,
    source: RegWatchSource,
    *,
    job_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> RegWatchCollection:
    """Fetch one approved source and record the attempt, whatever the outcome.

    Returns the collection row. It ALWAYS returns one -- a failure is a recorded row,
    not an exception propagated to the caller, because the record of having tried and
    failed is the thing this function exists to produce.
    """
    if not source.enabled:
        raise SourceNotAuthorizedError(
            f"source {source.name!r} is disabled; only approved, enabled sources are collected"
        )

    moment = now or datetime.now(UTC)

    if source.connector == watch.CONNECTOR_MANUAL:
        # A manual-upload source has no URL to poll, and before this it was fetched
        # anyway -- at an empty string, failing every sweep and raising a fresh
        # "unreachable" finding each time. A source that a person feeds by hand is not
        # unreachable; it is simply not due to be fetched.
        #
        # `skipped` rather than `collected`: it belongs to COLLECTION_NOT_CURRENT, so
        # nothing downstream can read this as confirmation the content is current.
        row = RegWatchCollection(
            org_id=source.org_id, source_id=source.id,
            status=watch.COLLECTION_SKIPPED, job_id=job_id,
            error_code=watch.ERR_SOURCE_NOT_AUTHORIZED,
            error_detail=(
                f"{source.name!r} is a manual-upload source and is never fetched "
                "automatically. Its content is whatever was last uploaded by a person; "
                "this is not a report that it is unchanged."
            ),
        )
        await repo.add_collection(db, source.org_id, row)
        await audit_service.record(
            db, org_id=source.org_id, actor_user_id=actor_user_id,
            action=watch.AUDIT_COLLECTION_STARTED,
            entity_type=watch.AUDIT_SOURCE_ENTITY, entity_id=source.id,
            after={
                "source": source.name, "status": watch.COLLECTION_SKIPPED,
                "reason": "manual-upload sources are not polled",
            },
        )
        return row

    await audit_service.record(
        db, org_id=source.org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_COLLECTION_STARTED,
        entity_type=watch.AUDIT_SOURCE_ENTITY, entity_id=source.id,
        after={"source": source.name, "url": source.url, "job_id": str(job_id) if job_id else None},
    )

    config = source.config or {}
    minimum = int(config.get("min_usable_chars", http_source.MIN_USABLE_CHARS))
    headers = dict(config.get("headers") or {})
    token = _resolve_credential(source)
    if token:
        headers.setdefault("Authorization", f"Bearer {token}")

    row = RegWatchCollection(
        org_id=source.org_id, source_id=source.id,
        status=watch.COLLECTION_COLLECTING, job_id=job_id,
    )

    try:
        fetched = await http_source.fetch(
            source.url, headers=headers or None, min_usable_chars=minimum
        )
    except (SourceUnreachableError, ContentUnusableError) as exc:
        # The failure path, and the reason this function does not re-raise. The row is
        # the product; the exception was only how the connector told us.
        row.status = watch.COLLECTION_FAILED
        row.error_code = exc.code or watch.ERR_COLLECTION_FAILED
        row.error_detail = str(exc)[:2000]
        await repo.add_collection(db, source.org_id, row)
        await source_service.record_check_outcome(db, source, succeeded=False, now=moment)
        await audit_service.record(
            db, org_id=source.org_id, actor_user_id=actor_user_id,
            action=watch.AUDIT_COLLECTION_FAILED,
            entity_type=watch.AUDIT_SOURCE_ENTITY, entity_id=source.id,
            after={
                "collection_id": str(row.id), "error_code": row.error_code,
                "error_detail": row.error_detail,
                "note": "the source was NOT confirmed unchanged; its content is unknown",
            },
        )
        logger.warning(
            "Regulatory source %r could not be collected: %s", source.name, row.error_code
        )
        return row

    text, content_hash = _normalise_for(source, fetched)

    row.status = watch.COLLECTION_COLLECTED
    row.content_hash = content_hash
    row.content_text = text
    row.content_bytes = fetched.byte_count
    row.http_status = fetched.http_status
    row.retrieved_at = moment
    await repo.add_collection(db, source.org_id, row)
    await source_service.record_check_outcome(db, source, succeeded=True, now=moment)
    await audit_service.record(
        db, org_id=source.org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_COLLECTION_SUCCEEDED,
        entity_type=watch.AUDIT_SOURCE_ENTITY, entity_id=source.id,
        after={
            "collection_id": str(row.id), "http_status": fetched.http_status,
            "content_hash": fetched.content_hash, "bytes": fetched.byte_count,
        },
    )
    return row


async def accept_manual_content(
    db: AsyncSession,
    source: RegWatchSource,
    content: str,
    *,
    uploaded_by_user_id: uuid.UUID,
    note: str | None = None,
) -> tuple[RegWatchCollection, RegWatchChange, RegWatchFinding | None]:
    """Record content a person supplied for a manual-upload source, and compare it.

    The other half of the manual connector. Registering such a source was possible
    from the start; giving it content was not, so it sat in the registry permanently
    uncollected while every sweep logged a failure against it.

    The upload is attributed. A collection that arrived through an automated fetch and
    one somebody pasted in are different kinds of evidence, and a reviewer reading the
    record later has to be able to tell which this was -- so the uploader's id goes in
    the audit entry and the collection's own detail says how it got here.
    """
    if source.connector != watch.CONNECTOR_MANUAL:
        raise SourceNotAuthorizedError(
            f"{source.name!r} is a {source.connector!r} source and is collected "
            "automatically; uploading content by hand would put unverified text "
            "alongside fetched evidence with no way to tell them apart"
        )
    text = http_source.normalize(content)
    if not text.strip():
        raise ContentUnusableError("the uploaded content is empty once normalised")

    row = RegWatchCollection(
        org_id=source.org_id, source_id=source.id,
        status=watch.COLLECTION_COLLECTED,
        content_text=text,
        content_hash=http_source.hash_content(text),
        content_bytes=len(content.encode("utf-8")),
        http_status=None,  # nothing was fetched; there is no status to report
        retrieved_at=datetime.now(UTC),
        error_detail=f"Uploaded by hand. {note}" if note else "Uploaded by hand.",
    )
    await repo.add_collection(db, source.org_id, row)
    await source_service.record_check_outcome(db, source, succeeded=True)
    await audit_service.record(
        db, org_id=source.org_id, actor_user_id=uploaded_by_user_id,
        action=watch.AUDIT_COLLECTION_SUCCEEDED,
        entity_type=watch.AUDIT_SOURCE_ENTITY, entity_id=source.id,
        after={
            "collection_id": str(row.id),
            "content_hash": row.content_hash,
            "bytes": row.content_bytes,
            "note": note,
            # The distinction that matters on a later read of the log.
            "fetched": False,
            "uploaded_by_hand": True,
        },
    )
    change, finding = await compare_and_record(
        db, source, row, actor_user_id=uploaded_by_user_id
    )
    return row, change, finding


def _normalise_for(source: RegWatchSource, fetched) -> tuple[str, str]:
    """Reduce a fetched document the way its connector says it should be read.

    A feed parsed as a feed is one line per entry, so a new advisory is a one-line
    diff. The same document flattened by the HTML stripper is a single reflowed
    paragraph in which that one fact is invisible.

    Falls back to whatever the connector already produced whenever the feed parse
    returns nothing: a feed we cannot read is still a document we can diff, and
    degrading to a coarser comparison beats failing a collection that succeeded.
    """
    if source.connector != watch.CONNECTOR_RSS:
        return fetched.text, fetched.content_hash

    parsed = feed_source.normalize(fetched.raw or fetched.text)
    if not parsed:
        logger.info(
            "Source %r is registered as a feed but could not be parsed as one; "
            "comparing it as text instead", source.name,
        )
        return fetched.text, fetched.content_hash
    return parsed, http_source.hash_content(parsed)


async def compare_and_record(
    db: AsyncSession,
    source: RegWatchSource,
    collection: RegWatchCollection,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> tuple[RegWatchChange, RegWatchFinding | None]:
    """Compare a collection against the accepted baseline and record what it means.

    Returns (change, finding). The finding is None only for `no_change` -- the one
    outcome that should stay silent.
    """
    baseline = await repo.current_baseline(db, source.id, source.org_id)
    baseline_text: str | None = None
    if baseline is not None:
        baseline_collection = await repo.get_collection(db, baseline.collection_id, source.org_id)
        baseline_text = baseline_collection.content_text if baseline_collection else None

    failed = collection.status == watch.COLLECTION_FAILED
    result = change_detection.detect(
        baseline_text=baseline_text,
        baseline_hash=baseline.content_hash if baseline else None,
        new_text=collection.content_text,
        new_hash=collection.content_hash,
        collection_failed=failed,
        failure_reason=collection.error_detail if failed else None,
    )

    change = RegWatchChange(
        org_id=source.org_id, source_id=source.id,
        from_baseline_id=baseline.id if baseline else None,
        to_collection_id=collection.id,
        change_kind=result.kind,
        added_lines=result.added_lines,
        removed_lines=result.removed_lines,
        diff_excerpt=result.excerpt,
    )
    await repo.create_change(db, change)
    await audit_service.record(
        db, org_id=source.org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_CHANGE_DETECTED,
        entity_type=watch.AUDIT_SOURCE_ENTITY, entity_id=source.id,
        after={
            "change_id": str(change.id), "kind": result.kind,
            "added": result.added_lines, "removed": result.removed_lines,
            "summary": change_detection.summarise(result, source_name=source.name),
        },
    )

    if not result.raises_finding:
        return change, None

    # A fresh change retires any finding for this source that nobody has decided on
    # yet -- but never one a person has approved or dismissed. Two live findings
    # competing for the same decision is confusing; overwriting somebody's decision is
    # worse.
    await _supersede_undecided(db, source, actor_user_id=actor_user_id)

    finding = RegWatchFinding(
        org_id=source.org_id, change_id=change.id, source_id=source.id,
        reference=await _unused_reference(db, source.org_id),
        status=watch.DETECTED,
        summary=change_detection.summarise(result, source_name=source.name),
        jurisdiction=source.jurisdiction,
        # Relevance is assessed later, with evidence. Until then it is undetermined --
        # not "not relevant", which would be a conclusion nobody reached.
        relevance=watch.UNDETERMINED,
        relevance_confidence=watch.UNKNOWN,
        open_questions=list(result.notes),
        requires_human_review=True,
    )
    await repo.create_finding(db, finding)
    await audit_service.record(
        db, org_id=source.org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_FINDING_CREATED,
        entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
        after={
            "reference": finding.reference, "source": source.name,
            "change_kind": result.kind, "summary": finding.summary,
        },
    )
    return change, finding


async def _supersede_undecided(
    db: AsyncSession, source: RegWatchSource, *, actor_user_id: uuid.UUID | None
) -> int:
    open_findings = await repo.list_open_findings_for_source(
        db, source.id, source.org_id, statuses=tuple(sorted(lifecycle.SUPERSEDABLE))
    )
    for stale in open_findings:
        if not lifecycle.can_be_superseded(stale.status):
            continue
        lifecycle.assert_transition(stale.status, watch.SUPERSEDED)
        before = stale.status
        stale.status = watch.SUPERSEDED
        stale.updated_at = datetime.now(UTC)
        await db.flush()
        await audit_service.record(
            db, org_id=source.org_id, actor_user_id=actor_user_id,
            action=watch.AUDIT_STATUS_CHANGED,
            entity_type=watch.AUDIT_ENTITY, entity_id=stale.id,
            before={"status": before},
            after={
                "status": watch.SUPERSEDED,
                "reason": "a newer change was detected for this source before this "
                          "finding was decided",
            },
        )
    return len(open_findings)


async def accept_as_baseline(
    db: AsyncSession,
    source: RegWatchSource,
    collection: RegWatchCollection,
    *,
    approved_by_user_id: uuid.UUID,
    note: str | None = None,
) -> RegWatchBaseline:
    """Make a collection the new reference point. The ONLY way a baseline moves.

    Requires a named person, and requires the collection to have actually succeeded --
    adopting a failed collection as the baseline would set the reference point to
    "nothing", and the next real content would then read as an enormous change.
    """
    if approved_by_user_id is None:
        raise SourceNotAuthorizedError(
            "a baseline is advanced by a person, not by the agent; no user was supplied"
        )
    if collection.status != watch.COLLECTION_COLLECTED:
        raise ContentUnusableError(
            f"collection {collection.id} has status {collection.status!r} and cannot "
            "become a baseline; only successfully collected content can"
        )

    await repo.supersede_baselines(db, source.id, source.org_id)
    version = await repo.next_baseline_version(db, source.id, source.org_id)
    baseline = RegWatchBaseline(
        org_id=source.org_id, source_id=source.id, collection_id=collection.id,
        version=version, content_hash=collection.content_hash,
        approved_by_user_id=approved_by_user_id, note=note,
    )
    await repo.create_baseline(db, baseline)
    await audit_service.record(
        db, org_id=source.org_id, actor_user_id=approved_by_user_id,
        action=watch.AUDIT_BASELINE_ADVANCED,
        entity_type=watch.AUDIT_SOURCE_ENTITY, entity_id=source.id,
        after={
            "baseline_id": str(baseline.id), "version": version,
            "collection_id": str(collection.id), "content_hash": collection.content_hash,
            "note": note,
        },
    )
    return baseline
