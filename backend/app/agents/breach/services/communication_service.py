"""Communications and the incident report (§26, phase 17).

THE ONE RULE
------------
Consiva drafts. A person approves. A person sends. The agent must never automatically
send an important external communication (§26), and no outbound provider is configured
in this project anyway -- so `sent_at` is only ever stamped by somebody recording that
they sent it. The system never claims it did.

GROUNDING
---------
Every factual sentence is assembled from the incident's own rows: evidence, timeline,
affected systems, affected data, subject counts, executions. `grounded_facts` stores
the rows the prose was built from, so a reviewer can check each claim against a record
rather than against a narrative.

The hardest part is saying what is NOT known. A breach notification that states a
figure it cannot support is worse than one that says the figure is still being
established, because the first is repeated and the second is corrected.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.breach.errors import ApprovalRequiredError, IncidentNotReadyError
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import investigation_service
from app.db.models import IncidentCase, IncidentCommunication, IncidentReport
from app.db.repositories import incident_repository
from app.services import audit_service


async def _gather(db: AsyncSession, case: IncidentCase) -> dict:
    """Everything the incident has actually established, in one place."""
    return {
        "evidence": await incident_repository.list_evidence(db, case.id, case.org_id),
        "timeline": await incident_repository.list_timeline(db, case.id, case.org_id),
        "systems": await incident_repository.list_affected_systems(db, case.id, case.org_id),
        "data": await incident_repository.list_affected_data(db, case.id, case.org_id),
        "subjects": await incident_repository.list_affected_subjects(db, case.id, case.org_id),
        "risk": await incident_repository.get_current_risk(db, case.id, case.org_id),
        "actions": await incident_repository.list_actions(db, case.id, case.org_id),
        "executions": await incident_repository.list_executions(db, case.id, case.org_id),
    }


def _facts(case: IncidentCase, gathered: dict) -> list[dict]:
    """The machine-checkable claims behind whatever prose is generated."""
    facts: list[dict] = [{
        "type": "incident",
        "reference": case.reference,
        "incident_type": case.incident_type,
        "severity": case.severity,
        "detected_at": case.detected_at.isoformat() if case.detected_at else None,
        # Both carried explicitly: "we have not confirmed a breach" and "there was no
        # breach" are different statements and only one of them is usually true.
        "personal_data_involved": case.personal_data_involved,
        "breach_confirmed": case.breach_confirmed,
    }]
    for system in gathered["systems"]:
        facts.append({
            "type": "affected_system", "system": system.system_name,
            "kind": system.system_kind, "confidence": system.confidence,
            "system_id": str(system.id),
        })
    for category in sorted({d.data_category for d in gathered["data"]}):
        rows = [d for d in gathered["data"] if d.data_category == category]
        facts.append({
            "type": "affected_data", "category": category,
            "confidence": min((r.confidence for r in rows), key=vocab.CONFIDENCE_ORDER.get),
            "derived_from": sorted({r.derived_from for r in rows}),
            "column_count": len(rows),
        })
    for subject in gathered["subjects"]:
        facts.append({
            "type": "affected_subjects", "group": subject.subject_group,
            "record_count": subject.record_count, "count_basis": subject.count_basis,
            "confidence": subject.confidence,
        })
    if gathered["risk"]:
        facts.append({
            "type": "risk", "level": gathered["risk"].risk_level,
            "score": gathered["risk"].risk_score,
            "confidence": gathered["risk"].confidence,
            "risk_id": str(gathered["risk"].id),
        })
    for execution in gathered["executions"]:
        facts.append({
            "type": "response_action", "status": execution.status,
            "execution_mode": execution.execution_mode,
            # The distinction that must survive into any report.
            "verification": execution.verification_status,
            "execution_id": str(execution.id),
        })
    return facts


def _impact_sentence(gathered: dict) -> str:
    """How many people, said only as strongly as the evidence allows."""
    total, basis = investigation_service.impact_total(gathered["subjects"])
    if total is None:
        if gathered["subjects"]:
            return (
                "The number of individuals affected is not yet established for every "
                "group involved, so no total is given."
            )
        return "The individuals affected have not yet been identified."
    groups = ", ".join(
        f"{s.subject_group}: {s.record_count:,} ({s.count_basis})"
        for s in gathered["subjects"]
    )
    qualifier = "counted" if basis == "counted" else "an estimate"
    return f"Individuals potentially affected — {groups}. Total {total:,}, {qualifier}."


def _data_sentence(gathered: dict) -> str:
    if not gathered["data"]:
        return "The categories of personal data involved have not yet been established."
    categories = sorted({d.data_category for d in gathered["data"]})
    derived_only = all(d.derived_from == "ropa_metadata" for d in gathered["data"])
    sentence = "Categories of personal data potentially involved: " + ", ".join(categories) + "."
    if derived_only:
        # The distinction §15 insists on, carried all the way into the prose.
        sentence += (
            " These are drawn from what the affected systems are known to hold, and "
            "describe what may have been involved rather than what has been confirmed "
            "as accessed."
        )
    return sentence


def compose_report(case: IncidentCase, gathered: dict) -> str:
    """Deterministic prose, assembled from rows. No model in this path."""
    risk = gathered["risk"]
    lines = [
        f"Incident report — {case.reference}",
        "",
        f"Title: {case.title}",
        f"Type: {case.incident_type}",
        f"Severity: {case.severity or 'not yet assessed'}",
        f"Detected: {case.detected_at.isoformat() if case.detected_at else 'unknown'}",
        f"Status: {case.status}",
        "",
        "What we know",
        "-----------",
        case.description,
        "",
        f"Personal data involvement: {case.personal_data_involved}.",
        f"Breach determination: {case.breach_confirmed}.",
        "",
    ]

    if gathered["systems"]:
        lines.append("Affected systems")
        lines.append("----------------")
        for system in gathered["systems"]:
            component = f"/{system.component}" if system.component else ""
            lines.append(
                f"  - {system.system_name}{component} ({system.system_kind}) — "
                f"{system.confidence}"
            )
        lines.append("")
    else:
        lines.append("No affected system has been identified yet.")
        lines.append("")

    lines.append(_data_sentence(gathered))
    lines.append("")
    lines.append(_impact_sentence(gathered))
    lines.append("")

    if risk:
        lines.append(
            f"Risk assessment: {risk.risk_level} (score {risk.risk_score}), held with "
            f"{risk.confidence} confidence."
        )
        lines.append(risk.reason)
        lines.append("")

    if gathered["timeline"]:
        lines.append("Timeline")
        lines.append("--------")
        for entry in gathered["timeline"]:
            stamp = entry.occurred_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"  {stamp}  {entry.event}  [{entry.confidence}]")
        lines.append("")

    completed = [e for e in gathered["executions"] if e.status in ("succeeded", "verified")]
    failed = [e for e in gathered["executions"] if e.status == "failed"]
    outstanding = [a for a in gathered["actions"] if a.status in ("proposed", "approved")]

    lines.append("Response")
    lines.append("--------")
    if completed:
        attested = sum(1 for e in completed if e.verification_status == "attested")
        read_back = sum(1 for e in completed if e.verification_status == "read_back")
        lines.append(f"  {len(completed)} containment action(s) recorded as carried out.")
        if attested:
            # Never presented as verified. Somebody's word is somebody's word.
            lines.append(
                f"  {attested} of these were attested by the person who performed them; "
                "Consiva did not independently confirm their effect."
            )
        if read_back:
            lines.append(
                f"  {read_back} were performed by Consiva and confirmed by re-reading "
                "the affected records."
            )
    else:
        lines.append("  No containment action has been recorded as carried out.")
    if failed:
        lines.append(f"  {len(failed)} action(s) could not be completed.")
    if outstanding:
        lines.append(f"  {len(outstanding)} action(s) remain outstanding.")
    lines.append("")

    unknowns = _open_questions(case, gathered)
    if unknowns:
        # The most important section. A report that lists only what is known reads as
        # complete, and an incomplete investigation presented as complete is how a
        # figure that was never established ends up being repeated.
        lines.append("What is not yet established")
        lines.append("---------------------------")
        for item in unknowns:
            lines.append(f"  - {item}")
        lines.append("")

    return "\n".join(lines)


def _open_questions(case: IncidentCase, gathered: dict) -> list[str]:
    questions: list[str] = []
    if case.personal_data_involved != vocab.CONFIRMED:
        questions.append(
            f"whether personal data was involved ({case.personal_data_involved})"
        )
    if case.breach_confirmed != vocab.CONFIRMED:
        questions.append(f"whether this constitutes a breach ({case.breach_confirmed})")
    if not gathered["systems"]:
        questions.append("which systems were affected")
    if not gathered["data"]:
        questions.append("which categories of personal data were involved")
    total, _ = investigation_service.impact_total(gathered["subjects"])
    if total is None:
        questions.append("how many individuals are affected")
    if gathered["risk"] and gathered["risk"].confidence in (vocab.UNKNOWN, vocab.POSSIBLE):
        questions.append(
            f"the risk assessment is held with only {gathered['risk'].confidence} confidence"
        )
    if any(e.verification_status == "attested" for e in gathered["executions"]):
        questions.append(
            "the effect of attested containment actions has not been independently confirmed"
        )
    return questions


async def generate_report(
    db: AsyncSession, case: IncidentCase, *, actor_user_id: uuid.UUID | None = None
) -> IncidentReport:
    gathered = await _gather(db, case)
    version = await incident_repository.next_report_version(db, case.id, case.org_id)
    row = IncidentReport(
        org_id=case.org_id, incident_id=case.id, version=version,
        body_text=compose_report(case, gathered),
        grounded_facts=_facts(case, gathered),
        drafted_by_model=None,   # deterministic; no model in this path
        status="draft",
    )
    await incident_repository.create_report(db, row)
    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=actor_user_id,
        action=vocab.AUDIT_REPORT_GENERATED, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={"report_id": str(row.id), "version": version,
               "fact_count": len(row.grounded_facts), "drafted_by_model": None},
    )
    return row


async def draft_communication(
    db: AsyncSession,
    case: IncidentCase,
    *,
    audience: str,
    subject: str,
    body: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> IncidentCommunication:
    """Draft a communication. Always a draft; never sent by this function."""
    if audience not in vocab.COMMUNICATION_AUDIENCES:
        raise IncidentNotReadyError(f"{audience!r} is not a communication audience")
    if not (subject and subject.strip()):
        raise IncidentNotReadyError("a communication needs a subject line")

    gathered = await _gather(db, case)
    row = IncidentCommunication(
        org_id=case.org_id, incident_id=case.id, audience=audience,
        subject=subject.strip(),
        body=(body or _default_body(case, gathered, audience)).strip(),
        grounded_facts=_facts(case, gathered),
        drafted_by_model=None,
        # An external draft needs review before anything else can happen to it.
        status="review_required" if audience in vocab.EXTERNAL_AUDIENCES else "draft",
    )
    await incident_repository.create_communication(db, row)
    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=actor_user_id,
        action=vocab.AUDIT_COMMUNICATION_DRAFTED, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={"communication_id": str(row.id), "audience": audience,
               "external": audience in vocab.EXTERNAL_AUDIENCES, "status": row.status},
    )
    return row


def _default_body(case: IncidentCase, gathered: dict, audience: str) -> str:
    """A starting point a person edits, written at the register the audience needs.

    Deliberately reticent about anything unconfirmed. An external draft that asserts a
    figure the investigation has not established is the sentence most likely to be
    repeated verbatim and hardest to retract.
    """
    if audience in (vocab.COMM_INTERNAL, vocab.COMM_MANAGEMENT, vocab.COMM_PRIVACY_TEAM):
        return compose_report(case, gathered)

    total, basis = investigation_service.impact_total(gathered["subjects"])
    categories = sorted({d.data_category for d in gathered["data"]})
    lines = [
        (
            "We are writing about a security incident identified on "
            f"{case.detected_at.date().isoformat() if case.detected_at else 'a recent date'}."
        ),
        "",
        "What happened",
        case.description,
        "",
    ]
    if categories:
        lines += [
            "What information may be involved",
            ", ".join(categories) + ".",
            (
                "We are still confirming exactly which records were affected."
                if all(d.derived_from == "ropa_metadata" for d in gathered["data"])
                else ""
            ),
            "",
        ]
    else:
        lines += [
            "What information may be involved",
            (
                "We are still establishing which information was involved and will "
                "update this as soon as we know."
            ),
            "",
        ]
    if total is not None and basis == "counted":
        lines += [f"Approximately {total:,} individuals are affected.", ""]
    lines += [
        "What we are doing",
        (
            "We are investigating, have taken steps to contain the incident, and are "
            "reviewing what further action is required."
        ),
        "",
        (
            "[DRAFT — this text must be reviewed and approved before it is sent. "
            "Consiva does not send external communications.]"
        ),
    ]
    return "\n".join(line for line in lines if line != "")


async def approve_communication(
    db: AsyncSession,
    case: IncidentCase,
    communication_id: uuid.UUID,
    *,
    reviewer_user_id: uuid.UUID,
    decision: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> IncidentCommunication:
    """Record a decision on a draft. Approval is not sending."""
    if decision not in (vocab.DECISION_APPROVE, vocab.DECISION_REJECT):
        raise IncidentNotReadyError(
            "a communication is approved or rejected; other decisions belong on the draft itself"
        )
    if decision == vocab.DECISION_REJECT and not (reason and reason.strip()):
        raise IncidentNotReadyError("rejecting a draft requires a reason")

    comm = await incident_repository.get_communication(db, communication_id, case.org_id)
    if comm is None or comm.incident_id != case.id:
        raise IncidentNotReadyError(f"communication {communication_id} not found on {case.reference}")
    if comm.status == "sent":
        raise IncidentNotReadyError("this communication has already been sent")

    moment = now or datetime.now(UTC)
    comm.status = "approved" if decision == vocab.DECISION_APPROVE else "rejected"
    if decision == vocab.DECISION_APPROVE:
        comm.approved_by_user_id = reviewer_user_id
        comm.approved_at = moment
    await db.flush()

    await incident_repository.record_approval(
        db, org_id=case.org_id, incident_id=case.id, reviewer_user_id=reviewer_user_id,
        subject="communication", decision=decision, communication_id=comm.id, reason=reason,
    )
    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=reviewer_user_id,
        action=vocab.AUDIT_COMMUNICATION_APPROVED, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={"communication_id": str(comm.id), "audience": comm.audience,
               "decision": decision, "reason": reason},
    )
    return comm


async def mark_communication_sent(
    db: AsyncSession,
    case: IncidentCase,
    communication_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID,
    now: datetime | None = None,
) -> IncidentCommunication:
    """Record that a person sent an approved communication.

    Consiva does not send it. This records that somebody else did, which is why the
    external gate below is absolute: an unapproved external draft cannot be marked
    sent no matter who asks.
    """
    comm = await incident_repository.get_communication(db, communication_id, case.org_id)
    if comm is None or comm.incident_id != case.id:
        raise IncidentNotReadyError(f"communication {communication_id} not found")
    if comm.status == "sent":
        return comm  # already recorded; not an error

    if comm.audience in vocab.EXTERNAL_AUDIENCES and comm.status != "approved":
        raise ApprovalRequiredError(
            f"a communication to {comm.audience} must be approved before it is sent; "
            f"this one is {comm.status}"
        )

    comm.status = "sent"
    comm.sent_at = now or datetime.now(UTC)
    comm.sent_by_user_id = actor_user_id
    await db.flush()
    await audit_service.record(
        db, org_id=case.org_id, actor_user_id=actor_user_id,
        action=vocab.AUDIT_COMMUNICATION_SENT, entity_type=vocab.AUDIT_ENTITY,
        entity_id=case.id,
        after={
            "communication_id": str(comm.id), "audience": comm.audience,
            "sent_at": comm.sent_at.isoformat(),
            "note": "recorded as sent by a person; Consiva has no outbound provider",
        },
    )
    return comm
