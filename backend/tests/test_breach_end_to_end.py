"""The five breach scenarios the prompt requires (§44), end to end.

Each walks an incident through the real services -- intake, classification, evidence,
timeline, impact, risk, planning, approval, attested containment, communications,
report, closure -- against an in-memory fake of the repository. No live Postgres, same
as the rest of this suite.

What these hold to account is the JOIN. The unit tests prove each service behaves on
its own; these prove an incident moving through all of them cannot reach a state the
lifecycle forbids, never gains confidence nobody granted it, and always ends somewhere
a person could defend.

Scenario 4 is the one that matters most. An alert that turns out to be nothing must
come out the other end as nothing -- not as a small breach, not as a low-risk breach.
A system that cannot say "this was not a breach" is a system that will eventually say
the opposite of the truth to a regulator.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect as sa_inspect

from app.agents.breach.errors import (
    ActionBlockedError,
    ApprovalRequiredError,
    IncidentNotReadyError,
    InvalidIncidentTransitionError,
)
from app.agents.breach.schemas import incident as vocab
from app.agents.breach.services import (
    communication_service,
    incident_service,
    investigation_service,
    lifecycle,
    response_service,
)
from app.agents.ropa.rules import personal_data_rules
from app.db.models import (
    IncidentAction,
    IncidentAffectedData,
    IncidentAffectedSubjects,
    IncidentAffectedSystem,
    IncidentApproval,
    IncidentCase,
    IncidentCommunication,
    IncidentEvidence,
    IncidentExecution,
    IncidentReport,
    IncidentRiskAssessment,
    IncidentTimelineEntry,
)
from app.services import incident_run_service

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
ORG = uuid.uuid4()
USER = uuid.uuid4()


# ── In-memory world ──────────────────────────────────────────────────────────────

class World:
    """Everything the repository would persist, held in lists."""

    def __init__(self):
        self.incidents: dict[uuid.UUID, IncidentCase] = {}
        self.evidence: list[IncidentEvidence] = []
        self.timeline: list[IncidentTimelineEntry] = []
        self.systems: list[IncidentAffectedSystem] = []
        self.data: list[IncidentAffectedData] = []
        self.subjects: list[IncidentAffectedSubjects] = []
        self.risks: list[IncidentRiskAssessment] = []
        self.actions: list[IncidentAction] = []
        self.approvals: list[IncidentApproval] = []
        self.executions: list[IncidentExecution] = []
        self.communications: list[IncidentCommunication] = []
        self.reports: list[IncidentReport] = []
        self.audit: list[dict] = []
        # What Agent 2 already knows, keyed by system name.
        self.baselines: dict[str, dict] = {}
        self.data_sources: list = []

    def audit_actions(self) -> list[str]:
        return [entry["action"] for entry in self.audit]


class _DB:
    """Only what the services touch: flush is a no-op, and nothing else is called."""

    async def flush(self):
        return None

    async def commit(self):
        return None


def _assign_id(row):
    """Stand in for the flush these fakes skip.

    SQLAlchemy applies column defaults at INSERT, not in `__init__`, so a row built in
    memory has None where the database would have "unknown" or 0. Applying them here
    matters more than it looks: without it, `breach_confirmed is not CONFIRMED` would
    hold because the field is None, which proves nothing about the rule under test.
    """
    if getattr(row, "id", None) is None:
        row.id = uuid.uuid4()
    if getattr(row, "created_at", None) is None:
        row.created_at = NOW
    for column in sa_inspect(type(row)).columns:
        default = column.default
        if default is None or not default.is_scalar:
            continue
        attr = column.key
        if getattr(row, attr, None) is None:
            setattr(row, attr, default.arg)
    return row


def wire(monkeypatch, world: World) -> None:
    """Patch the repository module itself, so every service that imports it -- and
    there are five -- sees the same fake without each needing its own harness."""
    from app.db.repositories import incident_repository as repo
    from app.db.repositories import ropa_repository
    from app.services import audit_service

    async def record(db, **kw):
        world.audit.append(kw)

    monkeypatch.setattr(audit_service, "record", record)

    # -- incidents --
    async def create_incident(db, **kw):
        row = _assign_id(IncidentCase(status=vocab.REPORTED, **kw))
        world.incidents[row.id] = row
        return row

    async def get_incident(db, incident_id, org_id):
        row = world.incidents.get(incident_id)
        return row if row is not None and row.org_id == org_id else None

    async def reference_exists(db, org_id, reference):
        return any(
            c.reference == reference and c.org_id == org_id for c in world.incidents.values()
        )

    async def find_by_idempotency_key(db, org_id, key):
        return next(
            (c for c in world.incidents.values()
             if c.org_id == org_id and c.idempotency_key == key),
            None,
        )

    # -- evidence and timeline --
    async def add_evidence(db, org_id, row):
        assert row.org_id == org_id
        world.evidence.append(_assign_id(row))
        return row

    async def list_evidence(db, incident_id, org_id):
        return [e for e in world.evidence if e.incident_id == incident_id and e.org_id == org_id]

    async def add_timeline_entries(db, org_id, rows):
        world.timeline.extend(_assign_id(r) for r in rows)

    async def list_timeline(db, incident_id, org_id):
        return sorted(
            (t for t in world.timeline if t.incident_id == incident_id and t.org_id == org_id),
            key=lambda t: t.occurred_at,
        )

    # -- impact --
    async def add_affected_system(db, row):
        world.systems.append(_assign_id(row))
        return row

    async def list_affected_systems(db, incident_id, org_id):
        return [s for s in world.systems if s.incident_id == incident_id and s.org_id == org_id]

    async def replace_affected_data(db, incident_id, org_id, rows):
        world.data = [
            d for d in world.data
            if not (d.incident_id == incident_id and d.derived_from != "manual")
        ]
        world.data.extend(_assign_id(r) for r in rows)

    async def list_affected_data(db, incident_id, org_id):
        return [d for d in world.data if d.incident_id == incident_id and d.org_id == org_id]

    async def upsert_affected_subjects(db, **kw):
        existing = next(
            (s for s in world.subjects
             if s.incident_id == kw["incident_id"] and s.subject_group == kw["subject_group"]),
            None,
        )
        if existing is None:
            existing = _assign_id(IncidentAffectedSubjects(
                org_id=kw["org_id"], incident_id=kw["incident_id"],
                subject_group=kw["subject_group"],
            ))
            world.subjects.append(existing)
        for field in ("record_count", "count_basis", "basis_note", "confidence", "evidence_id"):
            setattr(existing, field, kw.get(field))
        return existing

    async def list_affected_subjects(db, incident_id, org_id):
        return [s for s in world.subjects if s.incident_id == incident_id and s.org_id == org_id]

    # -- risk --
    async def next_risk_version(db, incident_id, org_id):
        return len([r for r in world.risks if r.incident_id == incident_id]) + 1

    async def create_risk_assessment(db, row):
        world.risks.append(_assign_id(row))
        return row

    async def supersede_risk_assessments(db, incident_id, org_id):
        for row in world.risks:
            if row.incident_id == incident_id and row.review_status != "superseded":
                row.review_status = "superseded"

    async def get_current_risk(db, incident_id, org_id):
        live = [r for r in world.risks
                if r.incident_id == incident_id and r.review_status != "superseded"]
        return live[-1] if live else None

    # -- plan, approval, execution --
    async def add_actions(db, org_id, rows):
        world.actions.extend(_assign_id(r) for r in rows)

    async def list_actions(db, incident_id, org_id):
        return [a for a in world.actions if a.incident_id == incident_id and a.org_id == org_id]

    async def get_action(db, action_id, org_id):
        return next(
            (a for a in world.actions if a.id == action_id and a.org_id == org_id), None
        )

    async def record_approval(db, **kw):
        row = _assign_id(IncidentApproval(**kw))
        world.approvals.append(row)
        return row

    async def latest_approval_for_action(db, action_id, org_id):
        matching = [a for a in world.approvals if a.action_id == action_id]
        return matching[-1] if matching else None

    async def claim_execution(db, **kw):
        existing = next(
            (e for e in world.executions if e.idempotency_key == kw["idempotency_key"]), None
        )
        if existing is not None:
            return existing, False
        row = _assign_id(IncidentExecution(status="pending", **kw))
        world.executions.append(row)
        return row, True

    async def list_executions(db, incident_id, org_id):
        return [e for e in world.executions if e.incident_id == incident_id]

    # -- communications and reports --
    async def create_communication(db, row):
        world.communications.append(_assign_id(row))
        return row

    async def get_communication(db, communication_id, org_id):
        return next(
            (c for c in world.communications
             if c.id == communication_id and c.org_id == org_id),
            None,
        )

    async def next_report_version(db, incident_id, org_id):
        return len([r for r in world.reports if r.incident_id == incident_id]) + 1

    async def create_report(db, row):
        world.reports.append(_assign_id(row))
        return row

    async def get_latest_report(db, incident_id, org_id):
        matching = [r for r in world.reports if r.incident_id == incident_id]
        return matching[-1] if matching else None

    # Named explicitly rather than swept out of locals(): a fake that silently fails
    # to replace its real counterpart would make every assertion below meaningless.
    fakes = {
        "create_incident": create_incident,
        "get_incident": get_incident,
        "reference_exists": reference_exists,
        "find_by_idempotency_key": find_by_idempotency_key,
        "add_evidence": add_evidence,
        "list_evidence": list_evidence,
        "add_timeline_entries": add_timeline_entries,
        "list_timeline": list_timeline,
        "add_affected_system": add_affected_system,
        "list_affected_systems": list_affected_systems,
        "replace_affected_data": replace_affected_data,
        "list_affected_data": list_affected_data,
        "upsert_affected_subjects": upsert_affected_subjects,
        "list_affected_subjects": list_affected_subjects,
        "next_risk_version": next_risk_version,
        "create_risk_assessment": create_risk_assessment,
        "supersede_risk_assessments": supersede_risk_assessments,
        "get_current_risk": get_current_risk,
        "add_actions": add_actions,
        "list_actions": list_actions,
        "get_action": get_action,
        "record_approval": record_approval,
        "latest_approval_for_action": latest_approval_for_action,
        "claim_execution": claim_execution,
        "list_executions": list_executions,
        "create_communication": create_communication,
        "get_communication": get_communication,
        "next_report_version": next_report_version,
        "create_report": create_report,
        "get_latest_report": get_latest_report,
    }
    for name, fn in fakes.items():
        assert hasattr(repo, name), f"the repository has no {name}; this fake is stale"
        monkeypatch.setattr(repo, name, fn)

    # -- Agent 2's map --
    async def get_current_baseline_snapshot(db, org_id, source_name):
        return world.baselines.get(source_name)

    async def list_data_sources(db, org_id):
        return world.data_sources

    monkeypatch.setattr(
        ropa_repository, "get_current_baseline_snapshot", get_current_baseline_snapshot
    )
    monkeypatch.setattr(ropa_repository, "list_data_sources", list_data_sources)

    # Regulatory retrieval reaches an embedding API; the E2E is about the join, not
    # about pgvector. An empty result is one of the real outcomes anyway.
    async def no_regulatory_context(db, case, assessment):
        return []

    monkeypatch.setattr(incident_run_service, "_regulatory_context", no_regulatory_context)


# ── Helpers the scenarios share ──────────────────────────────────────────────────

async def open_incident(db, world, *, title, description, source=vocab.SOURCE_SIEM,
                        occurred_at=None, detected_at=NOW):
    # `now` is passed explicitly everywhere: an incident's clocks are checked against
    # it (a detection time in the future is rejected), so a test that let the wall
    # clock decide would pass or fail depending on the hour it ran.
    case, is_new = await incident_service.create_incident(
        db, org_id=ORG, title=title, description=description, source=source,
        detected_at=detected_at, occurred_at=occurred_at, created_by_user_id=USER,
        now=NOW,
    )
    assert is_new
    await incident_service.classify_incident(db, case, actor_user_id=USER)
    await incident_service.transition(db, case, vocab.VALIDATING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.INVESTIGATING, actor_user_id=USER, now=NOW)
    return case


async def approve_everything(db, world, case):
    """Approve every action awaiting a decision, as a reviewer would."""
    for action in list(world.actions):
        if action.incident_id == case.id and action.requires_approval:
            await response_service.decide_action(
                db, case, action.id, reviewer_user_id=USER,
                decision=vocab.DECISION_APPROVE, now=NOW,
                reason="Proportionate to the evidence; approved by the incident lead.",
            )


# ── Scenario 1: unauthorized access ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_1_unauthorized_access_reaches_a_defensible_close(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()

    case = await open_incident(
        db, world,
        title="Unauthorized access to the customer database",
        description=(
            "An account with no business reason to touch the customer database "
            "queried it 400 times overnight from an unrecognised address."
        ),
        occurred_at=NOW - timedelta(hours=14),
    )
    assert case.incident_type == vocab.TYPE_UNAUTHORIZED_ACCESS
    assert case.classification_method != "llm", "classification must be deterministic"

    evidence = await investigation_service.add_evidence(
        db, case, kind=vocab.EV_ACCESS_LOG, source_system="siem",
        summary="400 SELECTs against customers from 203.0.113.9",
        detail={"query_count": 400, "source_ip": "203.0.113.9"},
        observed_at=NOW - timedelta(hours=14), actor_user_id=USER,
    )
    await investigation_service.add_timeline_entry(
        db, case, occurred_at=NOW - timedelta(hours=14),
        event="First unexpected query against the customer table",
        confidence=vocab.PROBABLE, evidence_id=evidence.id, actor_user_id=USER,
    )

    world.baselines["crm-postgres"] = {
        "tables": ["customers"],
        "columns": {"customers.email": "text", "customers.phone": "text"},
        "classifications": {
            "customers.email": personal_data_rules.CATEGORY_CONTACT,
            "customers.phone": personal_data_rules.CATEGORY_CONTACT,
        },
    }
    system = await investigation_service.record_affected_system(
        db, case, system_name="crm-postgres", system_kind=vocab.SYS_DATABASE,
        confidence=vocab.PROBABLE, evidence_id=evidence.id, actor_user_id=USER,
    )
    # No registry link: the fake ropa lookup is keyed by name, and the service only
    # consults it for a system it recognises, so link it as the real one would.
    system.data_source_id = uuid.uuid4()

    await investigation_service.record_affected_subjects(
        db, case, subject_group="customers", record_count=400,
        count_basis="counted", basis_note="distinct subject ids in the query log",
        confidence=vocab.PROBABLE, actor_user_id=USER,
    )

    rows, gaps = await investigation_service.derive_affected_data(db, case)
    assert {r.data_category for r in rows} == {personal_data_rules.CATEGORY_CONTACT}
    assert all(r.confidence == vocab.POSSIBLE for r in rows), (
        "the ROPA map says what a system contains, not what was touched"
    )
    assert not gaps

    assessment = await incident_run_service.assess_and_store_risk(db, case, extra_gaps=gaps)
    assert assessment.level in vocab.SEVERITIES
    assert assessment.confidence != vocab.CONFIRMED, (
        "no machine path may assert CONFIRMED"
    )

    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)
    await response_service.build_response_plan(db, case, actor_user_id=USER)
    assert world.actions, "an unauthorized-access incident with evidence got no plan"

    await incident_service.transition(db, case, vocab.APPROVAL_REQUIRED, actor_user_id=USER, now=NOW)
    await approve_everything(db, world, case)
    summary = await response_service.plan_summary(db, case)
    assert summary["ready_to_respond"]

    await incident_service.transition(db, case, vocab.APPROVED, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONDING, actor_user_id=USER, now=NOW)

    cleared = [
        a for a in world.actions
        if a.status == "approved" or (a.status == "proposed" and not a.requires_approval)
    ]
    assert cleared, "nothing was cleared for containment, so there is nothing to perform"
    execution = await response_service.record_tracked_execution(
        db, case, cleared[0].id, performed_by="priya@corp",
        attestation="Revoked the account's database role at 09:40.",
        actor_user_id=USER, now=NOW,
    )
    assert execution.verification_status == "attested"
    assert "cannot confirm" in execution.verification_detail["note"]

    await incident_service.transition(db, case, vocab.VERIFYING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.COMMUNICATION_PENDING, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.CLOSURE_REVIEW, actor_user_id=USER, now=NOW)

    report = await communication_service.generate_report(db, case, actor_user_id=USER)
    assert report.drafted_by_model is None, "the report is deterministic"
    assert report.grounded_facts

    case.closure_summary = "Access revoked; 400 customer records read; customers told."
    await incident_service.transition(db, case, vocab.CLOSED, actor_user_id=USER, now=NOW)
    assert lifecycle.is_terminal(case.status)


# ── Scenario 2: data exposure ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_2_data_exposure_keeps_the_count_basis_with_the_number(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()

    case = await open_incident(
        db, world,
        title="Customer records exposed by a public storage bucket",
        description=(
            "A storage bucket holding exported customer records was found "
            "publicly readable; records were exposed to the internet."
        ),
        source=vocab.SOURCE_SECURITY_TEAM,
    )
    assert case.incident_type in (vocab.TYPE_DATA_EXPOSURE, vocab.TYPE_MISCONFIGURATION)

    await investigation_service.record_affected_subjects(
        db, case, subject_group="customers", record_count=12_000,
        count_basis="estimated", basis_note="row count of the exported file",
        confidence=vocab.POSSIBLE, actor_user_id=USER,
    )
    await investigation_service.record_affected_subjects(
        db, case, subject_group="former customers", record_count=None,
        count_basis="unknown", basis_note="the archive's contents were not enumerated",
        confidence=vocab.UNKNOWN, actor_user_id=USER,
    )

    from app.db.repositories import incident_repository as repo

    rows = await repo.list_affected_subjects(db, case.id, ORG)
    total, basis = investigation_service.impact_total(rows)
    assert total is None and basis == "unknown", (
        "one unknown group must make the whole total unknown, not 12,000"
    )

    assessment = await incident_run_service.assess_and_store_risk(db, case)
    assert assessment.gaps, "an unknown subject count must surface as an open question"
    assert assessment.confidence in vocab.MACHINE_ASSERTABLE_CONFIDENCE

    report = await communication_service.generate_report(db, case, actor_user_id=USER)

    # The report says the total is not established, rather than printing 12,000 and
    # letting the reader assume it is the whole figure.
    assert "no total is given" in report.body_text
    assert "12,000" not in report.body_text and "12000" not in report.body_text

    # And it ends with what is still open, which is the section that keeps a partial
    # picture from reading as a complete one.
    assert "What is not yet established" in report.body_text
    assert "how many individuals are affected" in report.body_text

    # Each group's basis travels with its number in the grounded facts, so a reader
    # of the structured output cannot pick the number up without it either.
    groups = [f for f in report.grounded_facts if f["type"] == "affected_subjects"]
    assert {g["subject_group"] if "subject_group" in g else g["group"] for g in groups} == {
        "customers", "former customers"
    }
    assert all("count_basis" in g for g in groups)
    assert {g["count_basis"] for g in groups} == {"estimated", "unknown"}


# ── Scenario 3: credential compromise ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_3_credential_compromise_cannot_be_contained_without_approval(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()

    case = await open_incident(
        db, world,
        title="Administrator credentials compromised in a phishing campaign",
        description=(
            "An administrator entered their password into a phishing page. The "
            "credentials are assumed compromised."
        ),
        source=vocab.SOURCE_EMPLOYEE_REPORT,
    )
    assert case.incident_type == vocab.TYPE_CREDENTIAL_COMPROMISE

    await incident_service.transition(db, case, vocab.RISK_ASSESSMENT, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONSE_PENDING, actor_user_id=USER, now=NOW)
    await response_service.build_response_plan(db, case, actor_user_id=USER)

    plan = [a for a in world.actions if a.incident_id == case.id]
    assert vocab.ACT_DISABLE_ACCOUNT in {a.action_kind for a in plan}
    assert all(a.status == "proposed" for a in plan), "planning must not act"
    assert all(a.execution_mode == vocab.EXECUTION_MODE_TRACKED for a in plan), (
        "Consiva cannot disable an account; nothing in the plan may claim it can"
    )

    gated = next(a for a in plan if a.action_kind == vocab.ACT_DISABLE_ACCOUNT)
    assert gated.requires_approval

    await incident_service.transition(db, case, vocab.APPROVAL_REQUIRED, actor_user_id=USER, now=NOW)

    # THE GATE. Attempting containment before approval must fail, not warn.
    with pytest.raises(ApprovalRequiredError):
        await response_service.record_tracked_execution(
            db, case, gated.id, performed_by="mallory@corp",
            attestation="Disabled it early", actor_user_id=USER, now=NOW,
        )
    assert not world.executions, "a refused action still created an execution row"

    await response_service.decide_action(
        db, case, gated.id, reviewer_user_id=USER,
        decision=vocab.DECISION_APPROVE, now=NOW,
        reason="The credentials are known compromised; disabling is proportionate.",
    )
    await incident_service.transition(db, case, vocab.APPROVED, actor_user_id=USER, now=NOW)
    await incident_service.transition(db, case, vocab.RESPONDING, actor_user_id=USER, now=NOW)

    first = await response_service.record_tracked_execution(
        db, case, gated.id, performed_by="priya@corp",
        attestation="Disabled the administrator account and forced a reset.",
        actor_user_id=USER, now=NOW,
    )
    assert first.verification_status == "attested"

    # A second attestation on the same action is refused rather than recorded twice.
    # Disabling an account twice is harmless; rotating a credential twice can lock out
    # the very people trying to respond, so the refusal is the same either way.
    with pytest.raises(ActionBlockedError):
        await response_service.record_tracked_execution(
            db, case, gated.id, performed_by="priya@corp",
            attestation="Disabled the administrator account and forced a reset.",
            actor_user_id=USER, now=NOW,
        )
    assert len(world.executions) == 1

    # And the idempotency ledger stands behind that check for the case it cannot
    # catch -- two submissions in flight at once, both past the preflight.
    from app.db.repositories import incident_repository as repo

    key = response_service.idempotency_key(gated)
    again, is_new = await repo.claim_execution(
        db, org_id=ORG, incident_id=case.id, action_id=gated.id,
        idempotency_key=key, execution_mode=vocab.EXECUTION_MODE_TRACKED,
    )
    assert not is_new and again.id == first.id


# ── Scenario 4: the false positive ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_4_a_false_positive_never_becomes_a_confirmed_breach(monkeypatch):
    """The scenario the whole confidence vocabulary exists for.

    An alert that turns out to be a backup job must end as 'not a breach'. Nothing in
    the pipeline may quietly promote it, and no severity, no risk level and no report
    sentence may imply otherwise.
    """
    world = World()
    wire(monkeypatch, world)
    db = _DB()

    case = await open_incident(
        db, world,
        title="Unusual bulk read of the customer table",
        description="A large number of rows were read from the customer table overnight.",
    )

    # Nothing is asserted at intake.
    assert case.breach_confirmed == vocab.UNKNOWN
    assert case.personal_data_involved == vocab.UNKNOWN

    await investigation_service.add_evidence(
        db, case, kind=vocab.EV_DATABASE_EVENT, source_system="postgres",
        summary="Bulk read at 02:00 from 10.0.0.4",
        detail={"rows": 50_000, "source_ip": "10.0.0.4"},
        observed_at=NOW - timedelta(hours=7), actor_user_id=USER,
    )

    # Analysis runs and reaches its own conclusion. Whatever it decides, it may not
    # decide CONFIRMED -- there is no code path from a rule to that word.
    assessment = await incident_run_service.assess_and_store_risk(db, case)
    assert assessment.confidence in vocab.MACHINE_ASSERTABLE_CONFIDENCE
    assert case.breach_confirmed != vocab.CONFIRMED
    stored = world.risks[-1]
    assert stored.confidence != vocab.CONFIRMED
    assert stored.assessed_by == "engine"

    # A person investigates and finds the scheduled backup that caused it.
    await investigation_service.add_evidence(
        db, case, kind=vocab.EV_OBSERVATION, source_system="ops",
        summary="10.0.0.4 is the nightly backup host; the read was the scheduled job",
        observed_at=NOW - timedelta(hours=1), actor_user_id=USER,
    )
    await incident_service.set_finding(
        db, case, field="breach_confirmed", confidence=vocab.UNKNOWN,
        actor_user_id=USER, reason="Traced to the nightly backup job; no external access.",
    )

    # It goes to REJECTED, and REJECTED is terminal.
    await incident_service.transition(
        db, case, vocab.REVIEW_REQUIRED, actor_user_id=USER,
    )
    await incident_service.transition(
        db, case, vocab.REJECTED, actor_user_id=USER,
        error_code=vocab.ERR_INVALID_INCIDENT,
        error_detail="Not an incident: the read was the scheduled nightly backup.",
    )
    assert lifecycle.is_terminal(case.status)
    assert case.breach_confirmed != vocab.CONFIRMED

    # Nothing can restart it into a response.
    for forbidden in (vocab.RESPONDING, vocab.APPROVED, vocab.INVESTIGATING):
        with pytest.raises(InvalidIncidentTransitionError):
            await incident_service.transition(db, case, forbidden, actor_user_id=USER, now=NOW)

    # And no containment was ever performed.
    assert not world.executions
    assert not [c for c in world.communications if c.audience in vocab.EXTERNAL_AUDIENCES]


@pytest.mark.asyncio
async def test_scenario_4b_confirming_a_breach_requires_a_person_and_a_reason(monkeypatch):
    """The other half of the same rule: `confirmed` is reachable, but only this way."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Records taken from the customer database",
        description="An attacker exfiltrated customer records to an external host.",
    )

    with pytest.raises(IncidentNotReadyError):
        await incident_service.set_finding(
            db, case, field="breach_confirmed", confidence=vocab.CONFIRMED,
            actor_user_id=USER, reason="",
        )
    with pytest.raises(IncidentNotReadyError):
        await incident_service.set_finding(
            db, case, field="breach_confirmed", confidence=vocab.CONFIRMED,
            actor_user_id=None, reason="the logs are clear",
        )

    await incident_service.set_finding(
        db, case, field="breach_confirmed", confidence=vocab.CONFIRMED,
        actor_user_id=USER,
        reason="Egress logs show 12,000 rows leaving to an external host at 02:14.",
    )
    assert case.breach_confirmed == vocab.CONFIRMED
    # With a name against it, in the audit trail.
    confirmations = [a for a in world.audit if a["action"] == vocab.AUDIT_INVESTIGATION_COMPLETED]
    assert confirmations and confirmations[-1]["actor_user_id"] == USER


# ── Scenario 5: several systems, unevenly known ──────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_5_multiple_systems_and_the_gaps_between_them(monkeypatch):
    """Three systems: one Agent 2 has profiled, one it knows but never profiled, one
    it has never seen. The two it cannot speak for must show as gaps, not as clean."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()

    case = await open_incident(
        db, world,
        title="Credentials reused across three systems after a vendor breach",
        description=(
            "A vendor disclosed a breach of their systems. The same service account "
            "credentials were reused in our CRM, our billing service and a vendor portal."
        ),
        source=vocab.SOURCE_VENDOR_NOTIFICATION,
    )

    world.baselines["crm-postgres"] = {
        "tables": ["customers"],
        "columns": {"customers.email": "text", "customers.aadhaar": "text"},
        "classifications": {
            "customers.email": personal_data_rules.CATEGORY_CONTACT,
            "customers.aadhaar": personal_data_rules.CATEGORY_GOVERNMENT_ID,
        },
    }

    profiled = await investigation_service.record_affected_system(
        db, case, system_name="crm-postgres", system_kind=vocab.SYS_DATABASE,
        confidence=vocab.PROBABLE, actor_user_id=USER,
    )
    profiled.data_source_id = uuid.uuid4()

    known_unprofiled = await investigation_service.record_affected_system(
        db, case, system_name="billing-service", system_kind=vocab.SYS_APPLICATION,
        confidence=vocab.POSSIBLE, actor_user_id=USER,
    )
    known_unprofiled.data_source_id = uuid.uuid4()   # connected, never baselined

    await investigation_service.record_affected_system(
        db, case, system_name="vendor-portal", system_kind=vocab.SYS_VENDOR,
        confidence=vocab.POSSIBLE, actor_user_id=USER,
    )   # no data_source_id: Consiva has never seen it

    rows, gaps = await investigation_service.derive_affected_data(
        db, case, actor_user_id=USER
    )

    categories = {r.data_category for r in rows}
    assert categories == {
        personal_data_rules.CATEGORY_CONTACT, personal_data_rules.CATEGORY_GOVERNMENT_ID,
    }, (
        "the profiled system's categories should come through"
    )
    assert len(gaps) == 2, f"expected a gap per unknowable system, got {gaps}"
    assert any("billing-service" in g and "never profiled" in g for g in gaps)
    assert any("vendor-portal" in g and "not a source Consiva has discovered" in g for g in gaps)

    # The gaps reach the assessment rather than being dropped on the floor.
    assessment = await incident_run_service.assess_and_store_risk(db, case, extra_gaps=gaps)
    assert "billing-service" in world.risks[-1].reason
    assert "vendor-portal" in world.risks[-1].reason

    # A vendor system in scope is a risk factor, and it is recorded as one.
    codes = [f.code.lower() for f in assessment.factors]
    assert any("third_party" in c or "vendor" in c for c in codes), codes

    # And the report says what is not known, rather than reporting only what is.
    await incident_service.transition(db, case, vocab.REVIEW_REQUIRED, actor_user_id=USER, now=NOW)
    report = await communication_service.generate_report(db, case, actor_user_id=USER)
    assert "What is not yet established" in report.body_text, (
        "a report built on two unprofiled systems must say what it could not establish"
    )


# ── Cross-cutting: the external gate ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_external_communication_cannot_be_sent_without_approval(monkeypatch):
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Customer records exposed publicly",
        description="A storage bucket holding customer records was publicly readable.",
    )

    draft = await communication_service.draft_communication(
        db, case, audience=vocab.COMM_AFFECTED_INDIVIDUAL,
        subject="About a recent security incident", actor_user_id=USER,
    )
    assert draft.status == "review_required", (
        "an external draft should open needing review, not as an ordinary draft"
    )

    with pytest.raises((ApprovalRequiredError, IncidentNotReadyError)):
        await communication_service.mark_communication_sent(
            db, case, draft.id, actor_user_id=USER, now=NOW
        )

    await communication_service.approve_communication(
        db, case, draft.id, reviewer_user_id=USER,
        decision=vocab.DECISION_APPROVE, now=NOW,
    )
    sent = await communication_service.mark_communication_sent(
        db, case, draft.id, actor_user_id=USER, now=NOW
    )
    assert sent.status == "sent"
    assert sent.sent_at is not None


@pytest.mark.asyncio
async def test_an_internal_note_does_not_need_an_approval(monkeypatch):
    """The gate is about reaching people outside the organisation, not about
    paperwork. Briefing your own privacy team should not require a ceremony."""
    world = World()
    wire(monkeypatch, world)
    db = _DB()
    case = await open_incident(
        db, world, title="Unauthorized access to the customer database",
        description="An account read the customer table without a business reason.",
    )
    draft = await communication_service.draft_communication(
        db, case, audience=vocab.COMM_PRIVACY_TEAM,
        subject="Incident briefing", actor_user_id=USER,
    )
    sent = await communication_service.mark_communication_sent(
        db, case, draft.id, actor_user_id=USER, now=NOW
    )
    assert sent.status == "sent"
