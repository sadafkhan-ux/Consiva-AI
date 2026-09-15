"""Orchestration for Agent 4, and its entry point from the worker.

Lives in app/services/ next to ropa_run_service and dsr_run_service for the same
reason: it is the seam between the platform (jobs, sessions, transactions) and the
agent's own logic, so app/jobs/worker.py imports one module per agent from one place.

WHAT RUNS IN THE BACKGROUND, AND WHY
------------------------------------
Only the analysis: deriving the affected-data map from ROPA, seeding the timeline from
evidence, and assessing risk. Those read schema baselines and iterate evidence, which
is work that should not sit inside a request.

Containment is NOT queued. A person performs a tracked action and attests to it; there
is nothing for a worker to do, and a queued "disable account" job would be precisely
the fake execution §50 forbids.
"""

from __future__ import annotations

import logging
import uuid

from app.agents.breach.errors import IncidentError
from app.agents.breach.rules import risk as risk_rules
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import (
    incident_service,
    investigation_service,
    response_service,
)
from app.config import get_settings
from app.db.models import IncidentRiskAssessment
from app.db.repositories import incident_repository
from app.db.session import async_session_factory
from app.llm.client import get_reasoning_llm_client
from app.rag import retriever

logger = logging.getLogger(__name__)


async def run_analysis(
    incident_id: uuid.UUID, org_id: uuid.UUID, job_id: uuid.UUID | None = None
) -> None:
    """Worker entry point for `incident_analysis`.

    Derives what can be derived, then stops. Every conclusion it reaches is written at
    the confidence its evidence supports, and anything it could not establish is left
    visible as a gap rather than filled in.
    """
    async with async_session_factory() as db:
        case = await incident_service.get_incident_or_raise(db, incident_id, org_id)
        try:
            await investigation_service.seed_timeline_from_evidence(db, case)
            _, gaps = await investigation_service.derive_affected_data(db, case)
            assessment = await assess_and_store_risk(db, case, extra_gaps=gaps)

            # Where the assessment is thin, the incident goes to a human rather than
            # onward: a risk level held with UNKNOWN confidence is not a basis for
            # planning containment.
            target = (
                vocab.REVIEW_REQUIRED
                if assessment.confidence == vocab.UNKNOWN
                else vocab.RESPONSE_PENDING
            )
            if case.status != target:
                await incident_service.transition(
                    db, case, target,
                    audit_action=vocab.AUDIT_RISK_ASSESSED,
                    detail={
                        "risk_level": assessment.level,
                        "confidence": assessment.confidence,
                        "open_questions": len(assessment.gaps),
                    },
                )

            if target == vocab.RESPONSE_PENDING:
                await response_service.build_response_plan(db, case)

            await db.commit()
        except IncidentError as exc:
            await incident_service.transition(
                db, case, vocab.FAILED,
                error_code=exc.code or vocab.ERR_INVESTIGATION_FAILED,
                error_detail=exc.message,
            )
            await db.commit()
            logger.warning("Incident analysis stopped for %s: %s", case.reference, exc.code)
        except Exception as exc:
            await incident_service.transition(
                db, case, vocab.FAILED,
                error_code=vocab.ERR_INVESTIGATION_FAILED,
                error_detail=f"{type(exc).__name__} during analysis",
            )
            await db.commit()
            logger.exception("Unexpected error analysing incident %s", case.reference)
            raise


async def assess_and_store_risk(
    db, case, *, extra_gaps: list[str] | None = None, actor_user_id: uuid.UUID | None = None
) -> risk_rules.Assessment:
    """Assess risk from what the incident has established, and store it as a version.

    Reads only facts already on the incident. Anything absent becomes a recorded gap
    rather than a zero, which is the rule the whole engine is built around.
    """
    data = await incident_repository.list_affected_data(db, case.id, case.org_id)
    subjects = await incident_repository.list_affected_subjects(db, case.id, case.org_id)
    systems = await incident_repository.list_affected_systems(db, case.id, case.org_id)
    total, basis = investigation_service.impact_total(subjects)

    exposure = None
    if case.occurred_at and case.detected_at:
        exposure = case.detected_at - case.occurred_at

    assessment = risk_rules.assess_risk(
        incident_type=case.incident_type,
        data_categories=tuple(sorted({d.data_category for d in data})),
        subject_total=total,
        count_basis=basis,
        # Not inferred from the incident type: "this kind of incident usually involves
        # exfiltration" is a prior, not evidence, and the engine only takes facts.
        exfiltration=case.breach_confirmed,
        unauthorized_access=(
            vocab.PROBABLE
            if case.incident_type in (vocab.TYPE_UNAUTHORIZED_ACCESS, vocab.TYPE_INSIDER)
            else vocab.UNKNOWN
        ),
        exposure_window=exposure,
        third_party_involved=any(s.system_kind == vocab.SYS_VENDOR for s in systems),
        personal_data_involved=case.personal_data_involved,
    )

    regulatory = await _regulatory_context(db, case, assessment)

    await incident_repository.supersede_risk_assessments(db, case.id, case.org_id)
    version = await incident_repository.next_risk_version(db, case.id, case.org_id)
    row = IncidentRiskAssessment(
        org_id=case.org_id, incident_id=case.id, version=version,
        risk_level=assessment.level, risk_score=assessment.score,
        confidence=assessment.confidence,
        factors=[f.as_dict() for f in assessment.factors],
        reason=assessment.reason + (
            f" Additional open questions: {'; '.join(extra_gaps)}." if extra_gaps else ""
        ),
        regulatory_context=regulatory,
        assessed_by="engine", review_status="pending",
    )
    await incident_repository.create_risk_assessment(db, row)

    # Severity travels with the risk: an incident whose risk has just been assessed
    # high should not still read as medium on the dashboard.
    case.severity = assessment.level
    case.severity_score = assessment.score
    case.severity_confidence = assessment.confidence
    await db.flush()
    return assessment


async def _regulatory_context(db, case, assessment) -> list[dict]:
    """Retrieve relevant passages from the approved knowledge base (§20).

    This is NOT a determination that an obligation applies. It is the material a person
    should read before deciding, kept in its own column so a system fact and a legal
    question can never be mistaken for one another. The LLM's memory is not consulted;
    only the indexed, versioned corpus is.

    Best-effort: a retrieval failure must not stop an incident assessment, and an empty
    result is honest rather than filled in from somewhere else.
    """
    query = (
        f"personal data breach notification requirement {case.incident_type} "
        f"{assessment.level} risk"
    )
    try:
        # The same retrieval path Agent 1 uses, over the same approved corpus -- the
        # distance threshold matters here especially, because padding a regulatory
        # answer out with barely-related passages is how a reviewer ends up reading
        # the wrong section with confidence.
        chunks = await retriever.retrieve(
            query, db=db, llm_client=get_reasoning_llm_client(), top_k=3,
            max_distance=get_settings().rag_max_distance,
        )
    except Exception:  # noqa: BLE001 -- see below
        # Deliberately broad: retrieval reaches an embedding API and pgvector, and any
        # of it failing is a reason to assess the incident without regulatory context
        # rather than to stop assessing the incident.
        logger.warning("Regulatory retrieval failed for incident %s", case.reference)
        return []

    return [
        {
            "source": chunk.document_title,
            "version": chunk.document_version,
            "excerpt": chunk.content[:600],
            "distance": round(chunk.distance, 4),
            "note": (
                "Retrieved for a reviewer to read. Not a determination that this "
                "requirement applies to this incident."
            ),
        }
        for chunk in (chunks or [])
    ]


def next_status_after_analysis(assessment) -> str:
    """Where an incident goes once analysis finishes.

    A risk level nobody can trust is not a basis for planning containment, so an
    UNKNOWN-confidence assessment routes to a human instead.
    """
    return (
        vocab.REVIEW_REQUIRED
        if assessment.confidence == vocab.UNKNOWN
        else vocab.RESPONSE_PENDING
    )
