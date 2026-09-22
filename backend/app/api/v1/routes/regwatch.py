"""Agent 5 (Regulatory Watch) API.

Authenticated and org-scoped through the SAME dependency Agents 1-4 use. No route
accepts an org_id from the caller; it comes from the verified token and nowhere else.

TWO THINGS THIS API IS CAREFUL ABOUT
------------------------------------
1. NO CREDENTIAL EVER LEAVES. A source stores `credential_ref` -- the NAME of an
   environment variable. The name is returned (a reviewer needs to know a source is
   authenticated and which secret it expects); the value is never read here, and the
   registration endpoint refuses a config carrying credential-shaped keys.

2. A SOURCE IS NEVER RENDERED AS CURRENT UNLESS IT IS. Every source response carries
   the full health block -- state, is_current, last_success_at, consecutive_failures
   -- computed in one place, so no surface can accidentally present a failing watch as
   a working one. `GET /sources/unwatched` exists so that question has a direct answer
   rather than requiring the caller to derive it.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.regwatch.errors import (
    FindingNotFoundError,
    InvalidSourceError,
    SourceNotFoundError,
    WatchNotReadyError,
)
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import (
    action_sla_service,
    collection_service,
    review_service,
    source_service,
)
from app.core.security import CurrentUser, get_current_user
from app.db.repositories import regwatch_repository as repo
from app.db.session import get_db
from app.jobs import queue
from app.services import regwatch_run_service

router = APIRouter(prefix="/api/v1/regwatch", tags=["regwatch"])


# ── Request models ───────────────────────────────────────────────────────────────

class SourceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    url: str = Field(max_length=2000)
    jurisdiction: str = Field(min_length=1, max_length=100)
    connector: str = watch.CONNECTOR_HTTP
    topic: str | None = Field(default=None, max_length=200)
    authority: str | None = Field(default=None, max_length=200)
    check_interval_minutes: int = Field(default=1440, ge=5, le=525_600)
    # The NAME of an environment variable. Never a secret. The service checks this
    # again, and also refuses a config dict carrying credential-shaped keys.
    credential_ref: str | None = Field(default=None, max_length=200)
    config: dict | None = None

    @field_validator("connector")
    @classmethod
    def _known_connector(cls, value: str) -> str:
        if value not in watch.CONNECTORS:
            raise ValueError(f"connector must be one of {sorted(watch.CONNECTORS)}")
        return value

    @field_validator("credential_ref")
    @classmethod
    def _looks_like_a_name_not_a_secret(cls, value: str | None) -> str | None:
        """A ref is an env-var NAME. Anything with whitespace, punctuation or lowercase
        sprawl is very likely somebody pasting the secret itself into the field."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not cleaned.replace("_", "").isalnum() or cleaned != cleaned.upper():
            raise ValueError(
                "credential_ref is the NAME of an environment variable (e.g. "
                "REGWATCH_MEITY_TOKEN), not the secret itself"
            )
        return cleaned


class SourceToggle(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=2000)


class BaselineAccept(BaseModel):
    collection_id: uuid.UUID
    note: str | None = Field(default=None, max_length=2000)


class Decision(BaseModel):
    decision: str
    reason: str | None = Field(default=None, max_length=5000)
    edited_payload: dict | None = None

    @field_validator("decision")
    @classmethod
    def _known_decision(cls, value: str) -> str:
        if value not in watch.DECISIONS:
            raise ValueError(f"decision must be one of {sorted(watch.DECISIONS)}")
        return value


class ActionIn(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    rationale: str = Field(min_length=10, max_length=5000)
    expected_result: str = Field(min_length=5, max_length=2000)
    owner_label: str | None = Field(default=None, max_length=200)
    due_at: datetime | None = None


class ActionsIn(BaseModel):
    actions: list[ActionIn] = Field(min_length=1, max_length=25)


class ActionComplete(BaseModel):
    completed_by: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=10, max_length=5000)


class ActionStatus(BaseModel):
    status: str
    reason: str | None = Field(default=None, max_length=5000)

    @field_validator("status")
    @classmethod
    def _settable(cls, value: str) -> str:
        if value not in watch.ACTION_STATUSES:
            raise ValueError(f"status must be one of {sorted(watch.ACTION_STATUSES)}")
        if value == watch.ACTION_COMPLETED:
            raise ValueError(
                "an action is completed through the completion endpoint, which requires "
                "who did the work and a note describing it"
            )
        return value


class BaselineFromFinding(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class ManualUpload(BaseModel):
    """Content a person supplies for a source that is never fetched."""

    content: str = Field(min_length=1, max_length=2_000_000)
    note: str | None = Field(default=None, max_length=2000)


class OrgJurisdictions(BaseModel):
    jurisdictions: list[str] = Field(max_length=50)


# ── Serialisers ──────────────────────────────────────────────────────────────────

def _source_response(source) -> dict:
    return {
        "id": str(source.id),
        "name": source.name,
        "url": source.url,
        "connector": source.connector,
        "jurisdiction": source.jurisdiction,
        "topic": source.topic,
        "authority": source.authority,
        "check_interval_minutes": source.check_interval_minutes,
        # The NAME only. There is no endpoint anywhere that returns a secret value.
        "credential_ref": source.credential_ref,
        "requires_credential": bool(source.credential_ref),
        "created_at": source.created_at.isoformat() if source.created_at else None,
        # Health travels with every source representation, so no client can render a
        # source without also having the information that it is not being watched.
        "health": source_service.health(source),
    }


def _finding_response(finding, *, impacts=None, actions=None, approvals=None) -> dict:
    body = {
        "id": str(finding.id),
        "reference": finding.reference,
        "status": finding.status,
        "summary": finding.summary,
        "jurisdiction": finding.jurisdiction,
        "relevance": finding.relevance,
        "relevance_confidence": finding.relevance_confidence,
        "relevance_reason": finding.relevance_reason,
        "impact_summary": finding.impact_summary,
        "priority": finding.priority,
        "priority_confidence": finding.priority_confidence,
        "citations": finding.citations or [],
        "open_questions": finding.open_questions or [],
        "requires_human_review": finding.requires_human_review,
        "drafted_by_model": finding.drafted_by_model,
        "error_code": finding.error_code,
        "error_detail": finding.error_detail,
        "source_id": str(finding.source_id),
        "change_id": str(finding.change_id),
        "reviewed_at": finding.reviewed_at.isoformat() if finding.reviewed_at else None,
        "closed_at": finding.closed_at.isoformat() if finding.closed_at else None,
        "created_at": finding.created_at.isoformat() if finding.created_at else None,
    }
    if impacts is not None:
        body["impacts"] = [
            {
                "id": str(i.id), "target_kind": i.target_kind,
                "target_id": str(i.target_id) if i.target_id else None,
                "target_label": i.target_label, "confidence": i.confidence,
                "derived_from": i.derived_from, "rationale": i.rationale,
            }
            for i in impacts
        ]
    if actions is not None:
        body["actions"] = [
            {
                "id": str(a.id), "title": a.title, "rationale": a.rationale,
                "expected_result": a.expected_result, "owner_label": a.owner_label,
                "status": a.status,
                "due_at": a.due_at.isoformat() if a.due_at else None,
                "completed_by": a.completed_by, "completion_note": a.completion_note,
                "completed_at": a.completed_at.isoformat() if a.completed_at else None,
                # The platform did not do this work and does not claim to have
                # verified it. Stated on every row rather than in a footnote.
                "attested_not_verified": a.status == watch.ACTION_COMPLETED,
                # Four-valued, not a boolean: "no date was set" and "on track" are
                # different facts, and `overdue: false` would erase the first.
                "due": action_sla_service.view(a),
            }
            for a in actions
        ]
    if approvals is not None:
        body["approvals"] = [
            {
                "id": str(p.id), "decision": p.decision, "subject": p.subject,
                "reason": p.reason,
                "reviewer_user_id": str(p.reviewer_user_id),
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in approvals
        ]
    return body


def _collection_response(row) -> dict:
    """Deliberately without `content_text`. A regulator's page can be hundreds of
    kilobytes, and a list endpoint that returned it would be unusable; the diff
    excerpt on the change is what a reviewer actually reads."""
    return {
        "id": str(row.id),
        "status": row.status,
        "http_status": row.http_status,
        "content_hash": row.content_hash,
        "content_bytes": row.content_bytes,
        "retrieved_at": row.retrieved_at.isoformat() if row.retrieved_at else None,
        "error_code": row.error_code,
        "error_detail": row.error_detail,
        # The guardrail, on the row itself: a caller cannot read this as current.
        "is_current": row.status == watch.COLLECTION_COLLECTED,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ── Sources ──────────────────────────────────────────────────────────────────────

@router.post("/sources", status_code=status.HTTP_201_CREATED)
async def register_source(
    payload: SourceCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source = await source_service.register_source(
        db,
        org_id=uuid.UUID(user.org_id),
        name=payload.name,
        url=payload.url,
        jurisdiction=payload.jurisdiction,
        connector=payload.connector,
        topic=payload.topic,
        authority=payload.authority,
        check_interval_minutes=payload.check_interval_minutes,
        credential_ref=payload.credential_ref,
        config=payload.config,
        created_by_user_id=uuid.UUID(user.user_id),
    )
    await db.commit()
    return _source_response(source)


@router.get("/sources")
async def list_sources(
    enabled_only: bool = False,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    sources = await repo.list_sources(db, uuid.UUID(user.org_id), enabled_only=enabled_only)
    unwatched = regwatch_run_service.unwatched_sources(sources)
    return {
        "sources": [_source_response(s) for s in sources],
        # Stated at the top level as well as per row. A dashboard that only read the
        # list could show ten green rows and one red one; this makes the count of
        # sources NOT being watched impossible to miss.
        "not_currently_watched": len(unwatched),
        "unwatched": unwatched,
    }


@router.get("/sources/unwatched")
async def list_unwatched(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Which sources are NOT being watched successfully right now.

    The spec's closing guardrail as an endpoint: monitoring failures are visible, and
    nothing here can report a source as current when its collection failed.
    """
    sources = await repo.list_sources(db, uuid.UUID(user.org_id))
    unwatched = regwatch_run_service.unwatched_sources(sources)
    return {"count": len(unwatched), "sources": unwatched, "total_sources": len(sources)}


@router.get("/sources/{source_id}")
async def get_source(
    source_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source = await source_service.get_source_or_raise(db, source_id, uuid.UUID(user.org_id))
    collections = await repo.list_collections(db, source_id, source.org_id, limit=20)
    baseline = await repo.current_baseline(db, source_id, source.org_id)
    return {
        **_source_response(source),
        "collections": [_collection_response(c) for c in collections],
        "baseline": None if baseline is None else {
            "id": str(baseline.id), "version": baseline.version,
            "content_hash": baseline.content_hash,
            "approved_at": baseline.approved_at.isoformat() if baseline.approved_at else None,
            "note": baseline.note,
        },
    }


@router.patch("/sources/{source_id}")
async def toggle_source(
    source_id: uuid.UUID,
    payload: SourceToggle,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source = await source_service.get_source_or_raise(db, source_id, uuid.UUID(user.org_id))
    await source_service.set_enabled(
        db, source, enabled=payload.enabled,
        actor_user_id=uuid.UUID(user.user_id), reason=payload.reason,
    )
    await db.commit()
    return _source_response(source)


@router.post("/sources/{source_id}/collect", status_code=status.HTTP_202_ACCEPTED)
async def collect_now(
    source_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Queue a collection immediately, outside the schedule.

    Queued, not performed inline: the fetch reaches a third-party site over the
    network and a request should not wait on a regulator's web server.
    """
    source = await source_service.get_source_or_raise(db, source_id, uuid.UUID(user.org_id))
    if not source.enabled:
        raise InvalidSourceError(
            f"source {source.name!r} is disabled; enable it before collecting"
        )
    if source.connector == watch.CONNECTOR_MANUAL:
        # Queuing here would return 202 and then deliberately do nothing, which is a
        # control that reports success for work it never intended to perform. The
        # console hides the button for these sources; the API refuses regardless,
        # because the console is not the only caller.
        raise InvalidSourceError(
            f"{source.name!r} is a manual-upload source and is never fetched. Queuing "
            "a collection would report success for work that will not happen; upload "
            "its content instead."
        )
    job = await queue.enqueue(
        db, org_id=source.org_id, job_type="regwatch_collect",
        payload={"source_id": str(source.id), "org_id": str(source.org_id)},
    )
    await db.commit()
    return {"job_id": str(job.id), "status": "queued", "source_id": str(source.id)}


@router.post("/sources/{source_id}/baseline", status_code=status.HTTP_201_CREATED)
async def accept_baseline(
    source_id: uuid.UUID,
    payload: BaselineAccept,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Make a successful collection the new reference point.

    The only way a baseline moves, and it requires a named person -- everything
    afterwards is measured against what this user accepted.
    """
    source = await source_service.get_source_or_raise(db, source_id, uuid.UUID(user.org_id))
    collection = await repo.get_collection(db, payload.collection_id, source.org_id)
    if collection is None or collection.source_id != source.id:
        raise SourceNotFoundError(f"collection {payload.collection_id} not found on this source")
    baseline = await collection_service.accept_as_baseline(
        db, source, collection,
        approved_by_user_id=uuid.UUID(user.user_id), note=payload.note,
    )
    await db.commit()
    return {
        "id": str(baseline.id), "version": baseline.version,
        "content_hash": baseline.content_hash, "note": baseline.note,
        "source_id": str(source.id),
    }


# ── Organisation profile ─────────────────────────────────────────────────────────

@router.get("/jurisdictions")
async def get_jurisdictions(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.agents.regwatch.services import assessment_service

    values = await assessment_service.org_jurisdictions(db, uuid.UUID(user.org_id))
    return {
        "jurisdictions": list(values),
        # Said plainly, because an empty list changes what every relevance answer
        # means and a UI showing an empty field would not convey that.
        "note": (
            "Relevance is undetermined for every source until these are recorded."
            if not values else
            "Sources outside these jurisdictions are assessed as probably not relevant."
        ),
    }


@router.put("/jurisdictions")
async def set_jurisdictions(
    payload: OrgJurisdictions,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from sqlalchemy import select

    from app.db.models import Organization
    from app.services import audit_service

    cleaned = [j.strip() for j in payload.jurisdictions if j and j.strip()]
    org = (await db.execute(
        select(Organization).where(Organization.id == uuid.UUID(user.org_id))
    )).scalar_one_or_none()
    if org is None:
        raise SourceNotFoundError("organisation not found")
    before = list(org.jurisdictions or [])
    org.jurisdictions = cleaned
    await db.flush()
    await audit_service.record(
        db, org_id=uuid.UUID(user.org_id), actor_user_id=uuid.UUID(user.user_id),
        action=watch.AUDIT_SOURCE_UPDATED,
        entity_type="organization", entity_id=org.id,
        before={"jurisdictions": before}, after={"jurisdictions": cleaned},
    )
    await db.commit()
    return {"jurisdictions": cleaned}


# ── Findings ─────────────────────────────────────────────────────────────────────

@router.get("/findings")
async def list_findings(
    finding_status: str | None = None,
    limit: int = 50,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if finding_status and finding_status not in watch.ALL_STATUSES:
        raise WatchNotReadyError(f"{finding_status!r} is not a finding status")
    rows = await repo.list_findings(
        db, uuid.UUID(user.org_id), status=finding_status, limit=min(limit, 200)
    )
    return {
        "findings": [_finding_response(f) for f in rows],
        "awaiting_review": sum(1 for f in rows if f.status == watch.REVIEW_REQUIRED),
    }


async def _finding_or_404(db, finding_id: uuid.UUID, org_id: uuid.UUID):
    finding = await repo.get_finding(db, finding_id, org_id)
    if finding is None:
        raise FindingNotFoundError(f"finding {finding_id} not found")
    return finding


@router.get("/findings/{finding_id}")
async def get_finding(
    finding_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    finding = await _finding_or_404(db, finding_id, org_id)
    change = await repo.get_change(db, finding.change_id, org_id)
    return {
        **_finding_response(
            finding,
            impacts=await repo.list_impacts(db, finding_id, org_id),
            actions=await repo.list_actions(db, finding_id, org_id),
            approvals=await repo.list_approvals(db, finding_id, org_id),
        ),
        "change": None if change is None else {
            "id": str(change.id), "change_kind": change.change_kind,
            "added_lines": change.added_lines, "removed_lines": change.removed_lines,
            "diff_excerpt": change.diff_excerpt,
            "detected_at": change.detected_at.isoformat() if change.detected_at else None,
        },
        # Kept separate from `citations` so a reviewer can see the passage a sentence
        # was built from, rather than a reference they would have to go and look up.
        "grounded_facts": finding.grounded_facts or [],
    }


@router.post("/findings/{finding_id}/decision")
async def decide(
    finding_id: uuid.UUID,
    payload: Decision,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    finding = await _finding_or_404(db, finding_id, org_id)
    await review_service.decide(
        db, finding,
        reviewer_user_id=uuid.UUID(user.user_id),
        decision=payload.decision,
        reason=payload.reason,
        edited_payload=payload.edited_payload,
    )
    await db.commit()
    return _finding_response(
        finding, approvals=await repo.list_approvals(db, finding_id, org_id)
    )


@router.post("/findings/{finding_id}/actions", status_code=status.HTTP_201_CREATED)
async def open_actions(
    finding_id: uuid.UUID,
    payload: ActionsIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    finding = await _finding_or_404(db, finding_id, org_id)
    await review_service.open_actions(
        db, finding,
        reviewer_user_id=uuid.UUID(user.user_id),
        actions=[a.model_dump() for a in payload.actions],
    )
    await db.commit()
    return _finding_response(
        finding, actions=await repo.list_actions(db, finding_id, org_id)
    )


@router.post("/actions/{action_id}/complete")
async def complete_action(
    action_id: uuid.UUID,
    payload: ActionComplete,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    action = await repo.get_action(db, action_id, org_id)
    if action is None:
        raise FindingNotFoundError(f"action {action_id} not found")
    await review_service.complete_action(
        db, action,
        actor_user_id=uuid.UUID(user.user_id),
        completed_by=payload.completed_by,
        note=payload.note,
    )
    await db.commit()
    return {
        "id": str(action.id), "status": action.status,
        "completed_by": action.completed_by, "completion_note": action.completion_note,
        "completed_at": action.completed_at.isoformat() if action.completed_at else None,
        "attested_not_verified": True,
    }


@router.post("/findings/{finding_id}/close")
async def close_finding(
    finding_id: uuid.UUID,
    payload: SourceToggle | None = None,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    finding = await _finding_or_404(db, finding_id, org_id)
    await review_service.close_finding(
        db, finding,
        actor_user_id=uuid.UUID(user.user_id),
        note=payload.reason if payload else None,
    )
    await db.commit()
    return _finding_response(finding)


@router.post("/findings/{finding_id}/accept-baseline", status_code=status.HTTP_201_CREATED)
async def accept_baseline_from_finding(
    finding_id: uuid.UUID,
    payload: BaselineFromFinding,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Adopt the snapshot this finding was raised from as the source's baseline.

    The decision nearly every `first_capture` finding is waiting for. Before this
    existed the console could raise those findings and never resolve them, so each
    re-check reported the same first capture again -- the loop the finding's own note
    warns about.
    """
    org_id = uuid.UUID(user.org_id)
    finding = await _finding_or_404(db, finding_id, org_id)
    change = await repo.get_change(db, finding.change_id, org_id)
    if change is None:
        raise WatchNotReadyError("this finding has no change to accept")
    collection = await repo.get_collection(db, change.to_collection_id, org_id)
    if collection is None:
        raise WatchNotReadyError("the collection this finding was raised from is missing")
    source = await source_service.get_source_or_raise(db, finding.source_id, org_id)

    baseline = await review_service.accept_baseline_from_finding(
        db, finding, source, collection,
        reviewer_user_id=uuid.UUID(user.user_id), note=payload.note,
    )
    await db.commit()
    return {
        "baseline": {
            "id": str(baseline.id), "version": baseline.version,
            "content_hash": baseline.content_hash,
        },
        "finding": _finding_response(finding),
    }


@router.patch("/actions/{action_id}")
async def set_action_status(
    action_id: uuid.UUID,
    payload: ActionStatus,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Move an action between open, in progress, blocked and cancelled.

    Cancelling matters more than it looks: a finding cannot be closed while any action
    is still open, so an action that turned out to be unnecessary used to keep its
    finding open with no way out.
    """
    org_id = uuid.UUID(user.org_id)
    action = await repo.get_action(db, action_id, org_id)
    if action is None:
        raise FindingNotFoundError(f"action {action_id} not found")
    await review_service.set_action_status(
        db, action, status=payload.status,
        actor_user_id=uuid.UUID(user.user_id), reason=payload.reason,
    )
    await db.commit()
    return {
        "id": str(action.id), "status": action.status,
        "completion_note": action.completion_note,
        # Nothing was carried out, whatever the status now says.
        "work_performed": False,
        **action_sla_service.view(action),
    }


@router.get("/actions/overdue")
async def overdue_actions(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Actions past the date this organisation set for them.

    Its own target, never a statutory deadline -- every row says so, and so does this
    response, because "overdue" read without that qualifier is alarming in a way the
    data does not support.
    """
    org_id = uuid.UUID(user.org_id)
    rows = await action_sla_service.overdue_for_org(db, org_id)
    return {
        "count": len(rows),
        "actions": rows,
        "note": action_sla_service.NOT_A_LEGAL_DEADLINE,
    }


@router.post("/sources/{source_id}/upload", status_code=status.HTTP_201_CREATED)
async def upload_manual_content(
    source_id: uuid.UUID,
    payload: ManualUpload,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Supply content by hand for a source that is never fetched.

    The other half of the manual-upload connector. Registering such a source was
    already possible; without this there was no way to give it any content, so it sat
    in the registry permanently uncollected.
    """
    org_id = uuid.UUID(user.org_id)
    source = await source_service.get_source_or_raise(db, source_id, org_id)
    collection, change, finding = await collection_service.accept_manual_content(
        db, source, payload.content,
        uploaded_by_user_id=uuid.UUID(user.user_id), note=payload.note,
    )
    await db.commit()
    return {
        "collection": _collection_response(collection),
        "change_kind": change.change_kind,
        "finding": _finding_response(finding) if finding else None,
    }


@router.get("/findings/{finding_id}/audit")
async def finding_audit(
    finding_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = uuid.UUID(user.org_id)
    await _finding_or_404(db, finding_id, org_id)
    entries = await repo.list_finding_audit(db, finding_id, org_id)
    return {
        "entries": [
            {
                "id": str(e.id), "action": e.action,
                "actor_user_id": str(e.actor_user_id) if e.actor_user_id else None,
                "before": e.before, "after": e.after,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]
    }


# ── Dashboard ────────────────────────────────────────────────────────────────────

@router.get("/summary")
async def summary(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What a compliance lead needs on one screen.

    The unwatched count sits alongside the finding counts deliberately. A screen
    showing "3 findings awaiting review" and nothing else invites the reading that
    everything else is fine, when two regulators may have been unreachable all week.
    """
    org_id = uuid.UUID(user.org_id)
    sources = await repo.list_sources(db, org_id)
    findings = await repo.list_findings(db, org_id, limit=200)
    unwatched = regwatch_run_service.unwatched_sources(sources)

    by_status: dict[str, int] = {}
    for f in findings:
        by_status[f.status] = by_status.get(f.status, 0) + 1

    return {
        "sources": len(sources),
        "sources_enabled": sum(1 for s in sources if s.enabled),
        "sources_not_currently_watched": len(unwatched),
        "unwatched": unwatched,
        "findings_by_status": by_status,
        "awaiting_review": by_status.get(watch.REVIEW_REQUIRED, 0),
        "open_actions": by_status.get(watch.ACTION_OPEN, 0),
        # Work this organisation set itself a date for and has passed. Not a statutory
        # deadline; the note says so, and it travels with the number so no screen has
        # to remember the caveat on its own.
        "overdue_actions": len(await action_sla_service.overdue_for_org(db, org_id)),
        "overdue_note": action_sla_service.NOT_A_LEGAL_DEADLINE,
        "coverage_note": (
            "Every figure here describes what this platform has collected. A source "
            "listed as not currently watched has unknown content -- it is not a "
            "report that nothing changed."
        ),
    }
