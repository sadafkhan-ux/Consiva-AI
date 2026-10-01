"""Regressions for defects found in a line-by-line review of the four agents.

Each test here failed against the code as it stood, and names the specific thing that
was wrong rather than restating what the module is supposed to do -- so a future edit
that reintroduces the defect fails on the defect, not on a paraphrase of the design.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agents.ropa.schemas.evidence import (
    ColumnRecord,
    DiscoveryEvidence,
    SourceRecord,
    TableRecord,
)
from app.agents.ropa.schemas.ropa import PersonalDataElement
from app.agents.ropa.services import (
    change_detection_service,
    discovery_service,
    enrichment_service,
)

# ── ROPA: non-personal columns must not be reported as personal data ───────────
#
# classification_service labels a column it has ruled out as "Not Personal Data
# (operational)" / "(provenance)". Five downstream consumers tested
# `classification != "Unknown"` to mean "is personal data", which was true before
# those labels existed and silently became wrong afterwards.


def _evidence_with_one_personal_column() -> DiscoveryEvidence:
    """One genuine personal-data column among four that are explicitly not."""
    return DiscoveryEvidence(
        org_id="org-1",
        discovery_run_id="run-1",
        sources=[
            SourceRecord(local_id="s1", name="crm", source_type="database", connector="postgres")
        ],
        tables=[TableRecord(local_id="t1", source_local_id="s1", table_name="customers")],
        columns=[
            ColumnRecord(local_id="c1", table_local_id="t1", column_name="email", data_type="text"),
            ColumnRecord(local_id="c2", table_local_id="t1", column_name="created_at", data_type="timestamp"),
            ColumnRecord(local_id="c3", table_local_id="t1", column_name="updated_at", data_type="timestamp"),
            ColumnRecord(local_id="c4", table_local_id="t1", column_name="is_active", data_type="boolean"),
            ColumnRecord(local_id="c5", table_local_id="t1", column_name="email_source", data_type="text"),
        ],
    )


def test_operational_columns_are_not_listed_as_personal_data_categories():
    """A ROPA record used to carry the literal string "Not Personal Data
    (operational)" in `personal_data_categories` -- a compliance document naming
    "not personal data" as a category of personal data."""
    output = discovery_service.run_pipeline(_evidence_with_one_personal_column())

    for activity in output.processing_activities:
        assert not [c for c in activity.personal_data_categories if c.startswith("Not Personal Data")], (
            f"activity {activity.name!r} lists a non-personal label as a personal data "
            f"category: {activity.personal_data_categories}"
        )
    for record in output.ropa_records:
        assert not [c for c in record.personal_data_categories if c.startswith("Not Personal Data")], (
            f"ROPA record {record.processing_activity!r} lists a non-personal label as a "
            f"personal data category: {record.personal_data_categories}"
        )


def test_ropa_record_data_elements_hold_only_personal_data():
    """`data_elements` is the ROPA's list of the personal data it processes. It used
    to list every column in the table -- five where one was personal -- while the
    same output's `personal_data_elements_found` correctly said one."""
    output = discovery_service.run_pipeline(_evidence_with_one_personal_column())

    assert output.discovery_summary.personal_data_elements_found == 1
    assert [e.column for e in output.personal_data_inventory] == ["email"]

    record = next(r for r in output.ropa_records)
    assert record.data_elements == ["customers.email"], (
        "the ROPA record reports data elements the classifier ruled out as "
        f"non-personal: {record.data_elements}"
    )


def test_confidence_summary_scores_only_personal_data_elements():
    """overall_confidence is a statement about the personal-data findings. Averaging
    the operational rows in dragged it toward the non-personal majority."""
    output = discovery_service.run_pipeline(_evidence_with_one_personal_column())

    email = next(e for e in output.personal_data_inventory if e.column == "email")
    assert output.confidence_summary.overall_confidence == pytest.approx(email.confidence), (
        "confidence summary averaged non-personal columns alongside the personal one"
    )


def test_confidence_summary_says_so_when_nothing_is_personal_data():
    """The "not meaningful" note is suppressed exactly when it matters most: a table
    of purely operational columns produced a confident-looking score and no note,
    because those columns counted as `!= "Unknown"`."""
    evidence = DiscoveryEvidence(
        org_id="org-1",
        discovery_run_id="run-2",
        sources=[
            SourceRecord(local_id="s1", name="crm", source_type="database", connector="postgres")
        ],
        tables=[TableRecord(local_id="t1", source_local_id="s1", table_name="customers")],
        columns=[
            ColumnRecord(local_id="c1", table_local_id="t1", column_name="created_at", data_type="timestamp"),
            ColumnRecord(local_id="c2", table_local_id="t1", column_name="updated_at", data_type="timestamp"),
        ],
    )

    summary = discovery_service.run_pipeline(evidence).confidence_summary

    assert summary.overall_confidence == 0.0
    assert summary.notes is not None, (
        "no column classified as personal data, but the summary reported a confidence "
        "figure as though some had"
    )


# ── ROPA: a dropped table is a material change ────────────────────────────────


def test_dropped_table_is_flagged_for_review_and_agrees_with_is_material():
    """The removed-table entry was emitted as `deleted_field` with an override that
    `review_required` did not recognise, so a vanished TABLE needed no review while a
    vanished column did -- and `is_material()`, reading change_type, said the
    opposite about the very same entry."""
    baseline = {
        "tables": ["customers", "orders"],
        "columns": {"customers.email": "text", "orders.total": "numeric"},
        "classifications": {},
    }
    current = {
        "tables": ["customers"],
        "columns": {"customers.email": "text"},
        "classifications": {},
    }

    changes = change_detection_service.detect_changes(baseline, current)
    dropped = [c for c in changes if c.target == "orders" and c.previous_value == "orders"]
    assert dropped, "a dropped table produced no change entry at all"
    entry = dropped[0]

    assert entry.change_type == "removed_table", (
        f"a dropped table is reported as {entry.change_type!r}, which describes a field"
    )
    assert entry.review_required is True, "a dropped table was recorded as needing no review"
    assert change_detection_service.is_material(entry) is entry.review_required, (
        "is_material() and review_required disagree about the same change entry"
    )


# ── ROPA: enrichment may only touch columns that were actually sent ───────────


def test_enrichment_ignores_a_suggestion_for_a_column_never_sent():
    """`ambiguous_elements` caps the request at MAX_AMBIGUOUS_COLUMNS, but
    apply_suggestions matched against EVERY Unknown column, so a guessed name that
    collided with an un-sent column was written into the inventory -- the one thing
    the function's own docstring says cannot happen."""
    elements = [
        PersonalDataElement(
            source="s", table="t1", column=f"col{i}", classification="Unknown",
            confidence=0.0, evidence=[f"c{i}"],
        )
        for i in range(enrichment_service.MAX_AMBIGUOUS_COLUMNS + 1)
    ]
    requested = enrichment_service.ambiguous_elements(elements)
    never_sent = elements[enrichment_service.MAX_AMBIGUOUS_COLUMNS].column
    assert never_sent not in {e.column for e in requested}

    response = enrichment_service.EnrichmentResponse(
        suggestions=[
            enrichment_service.ColumnSuggestion(
                column=never_sent, category="Health Data", reasoning="guessed"
            )
        ]
    )

    result = enrichment_service.apply_suggestions(elements, response, requested=requested)

    unchanged = next(e for e in result if e.column == never_sent)
    assert unchanged.classification == "Unknown", (
        f"a suggestion for {never_sent!r}, which was never sent to the model, was "
        f"applied anyway as {unchanged.classification!r}"
    )


# ── DSR: an edited action is an approved action ──────────────────────────────


class _Approval:
    """The two fields `is_approval_current` reads off a DsrApproval row."""

    def __init__(self, decision: str, expires_at: datetime | None):
        self.decision = decision
        self.expires_at = expires_at


def test_an_edited_decision_authorizes_execution():
    """decide_action maps `edited` to action.status="approved" and
    plan_decision_summary then reports the plan ready to execute -- but
    is_approval_current tested `decision != "approved"` and refused every execution
    forever with "no current approval (latest: edited)"."""
    from app.agents.dsr.services import approval_service

    now = datetime.now(UTC)
    edited = _Approval("edited", now + timedelta(days=7))

    assert approval_service.is_approval_current(edited, now=now) is True, (
        "an edited-and-accepted action can never be executed"
    )


def test_an_edited_decision_expires_like_any_other_approval():
    """An approval that authorizes work must lapse. `expires_at` was only stamped for
    a plain `approved`, so an edited one would have authorized execution forever had
    the check above simply been widened."""
    from app.agents.dsr.services import approval_service

    now = datetime.now(UTC)
    stale = _Approval("edited", now - timedelta(seconds=1))

    assert approval_service.is_approval_current(stale, now=now) is False


def test_non_authorizing_decisions_still_refuse_execution():
    """The widening must not let a rejection, an escalation or a request for more
    information through."""
    from app.agents.dsr.services import approval_service

    now = datetime.now(UTC)
    for decision in ("rejected", "escalated", "request_more_information"):
        approval = _Approval(decision, now + timedelta(days=7))
        assert approval_service.is_approval_current(approval, now=now) is False, (
            f"a {decision!r} decision authorized execution"
        )


def test_an_approving_decision_is_not_audited_as_a_rejection():
    """Every DSR decision lands in the shared append-only audit log. The action name
    was "approved if APPROVED else rejected", which filed an `edited` decision -- one
    that authorizes the write -- as a REJECTION."""
    from app.agents.dsr.schemas import case
    from app.agents.dsr.services import approval_service

    mapping = approval_service._AUDIT_ACTION_FOR_DECISION

    assert set(mapping) == approval_service.DECISIONS, (
        "a decision exists with no audit action mapped for it"
    )
    for decision in approval_service.APPROVING_DECISIONS:
        assert mapping[decision] == case.AUDIT_APPROVED, (
            f"{decision!r} authorizes execution but is audited as {mapping[decision]!r}"
        )
    assert mapping["rejected"] == case.AUDIT_REJECTED
    assert mapping["escalated"] == case.AUDIT_ESCALATED
    assert mapping["request_more_information"] == case.AUDIT_REVIEW_REQUESTED


# ── Breach: an edited containment action is an approved one ─────────────────
#
# The same defect as the DSR one above, in Agent 4's own approval path. Worse here:
# what cannot be carried out is containment during a live incident.


def test_an_edited_containment_decision_authorizes_the_response():
    """response_service.decide_action maps `edited` to action.status="approved" and
    plan_summary then reports `ready_to_respond`, but is_approval_current tested
    `decision != "approved"` -- so _preflight refused every attempt to record the work
    with "no current approval (latest: edited)"."""
    from app.agents.breach.services import response_service

    now = datetime.now(UTC)
    edited = _Approval("edited", now + timedelta(hours=24))

    assert response_service.is_approval_current(edited, now=now) is True, (
        "an edited-and-accepted containment action can never be carried out"
    )


def test_an_edited_containment_decision_still_expires():
    from app.agents.breach.services import response_service

    now = datetime.now(UTC)
    stale = _Approval("edited", now - timedelta(seconds=1))

    assert response_service.is_approval_current(stale, now=now) is False


def test_breach_non_authorizing_decisions_still_refuse_the_response():
    from app.agents.breach.services import response_service

    now = datetime.now(UTC)
    for decision in ("rejected", "escalated", "request_more_information"):
        approval = _Approval(decision, now + timedelta(hours=24))
        assert response_service.is_approval_current(approval, now=now) is False, (
            f"a {decision!r} decision authorized containment"
        )


def test_every_breach_decision_is_audited_as_what_it_was():
    """An `edited` decision authorises containment; filing it as a REJECTION
    misreports the incident's own history in an append-only table."""
    from app.agents.breach.schemas import incident as vocab
    from app.agents.breach.services import response_service

    mapping = response_service._AUDIT_ACTION_FOR_DECISION

    assert set(mapping) == vocab.DECISIONS, "a decision exists with no audit action mapped"
    for decision in response_service.APPROVING_DECISIONS:
        assert mapping[decision] == vocab.AUDIT_APPROVED, (
            f"{decision!r} authorises containment but is audited as {mapping[decision]!r}"
        )
    assert mapping[vocab.DECISION_REJECT] == vocab.AUDIT_REJECTED


def test_the_two_agents_agree_on_what_authorizes_work():
    """Agents 3 and 4 run the same approve/edit/reject vocabulary through two separate
    implementations. They drifted once; this is what notices if they drift again."""
    from app.agents.breach.services import response_service as breach_response
    from app.agents.dsr.services import approval_service as dsr_approval

    assert breach_response.APPROVING_DECISIONS == dsr_approval.APPROVING_DECISIONS


# ── Consent agent: the graph is built once per process ───────────────────────


async def test_concurrent_callers_share_one_compiled_graph(monkeypatch):
    """The worker runs `worker_concurrency` jobs concurrently and never builds the
    graph at startup, so two analyze jobs starting together both walked through the
    `is not None` check and each built their own checkpointer pool. The second
    overwrote `_exit_stack`, leaking the first for the life of the process."""
    import asyncio

    from app.agents.consent_agent import graph as graph_module

    builds = 0
    closed: list[object] = []

    class _FakeCheckpointer:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            closed.append(self)
            return False

    def _fake_from_conn_string(_dsn):
        return _FakeCheckpointer()

    async def _fake_ensure(_checkpointer):
        # The real one awaits DDL; the yield here is what makes the race reachable.
        await asyncio.sleep(0)

    def _fake_build_graph():
        nonlocal builds
        builds += 1

        class _Compilable:
            def compile(self, *, checkpointer):
                return object()

        return _Compilable()

    monkeypatch.setattr(
        graph_module.AsyncPostgresSaver, "from_conn_string", staticmethod(_fake_from_conn_string)
    )
    monkeypatch.setattr(graph_module, "_ensure_checkpoint_tables", _fake_ensure)
    monkeypatch.setattr(graph_module, "build_graph", _fake_build_graph)
    monkeypatch.setattr(graph_module, "_compiled_graph", None)
    monkeypatch.setattr(graph_module, "_exit_stack", None)

    try:
        graphs = await asyncio.gather(*(graph_module.get_compiled_graph() for _ in range(4)))

        assert builds == 1, f"{builds} graphs were built for one process"
        assert len(set(map(id, graphs))) == 1, "concurrent callers got different graphs"
    finally:
        await graph_module.close_graph_resources()

    assert len(closed) == 1, (
        f"{len(closed)} checkpointer(s) closed -- a pool was built and then leaked"
    )


# ── DSR: a source that cannot count subjects must not report a clean result ───


class _StubRun:
    """The fields `_search_one_source` writes onto a dsr_search_runs row."""

    def __init__(self):
        self.id = None
        self.status = None
        self.error_code = None
        self.error_detail = None
        self.match_count = 0
        self.distinct_subject_count = 0
        self.tables_searched = None
        self.completed_at = None


class _StubMatch:
    table_name = "customers"
    schema_name = None
    matched_column = "email"
    identifier_kind = "email"
    match_type = "normalized_exact"
    confidence = 1.0

    def __init__(self):
        self.record_reference = {"id": 1}
        self.record_snapshot = {"email": "a@example.com"}


class _StubOutcome:
    """What PostgresDsrConnector returns with no identity table configured: exactly
    one 'distinct subject' however many rows matched, because it cannot tell."""

    def __init__(self, matches):
        self.matches = matches
        self.tables_searched = ("customers",)
        self.distinct_subjects = 1 if matches else 0
        self.truncated = False
        self.notes = ()


class _StubConnector:
    def __init__(self, *, has_identity_table: bool):
        self.has_identity_table = has_identity_table

    async def search_subject(self, *, identifiers, limit=500):
        return _StubOutcome([_StubMatch()])


async def _search_with(monkeypatch, *, has_identity_table: bool):
    """Drive one source through `_search_one_source`, stubbing only its two
    persistence collaborators and the connector factory."""
    import types

    from app.agents.dsr.services import search_service

    run = _StubRun()

    async def _create_search_run(*_a, **_k):
        return run

    async def _add_evidence(*_a, **_k):
        return None

    monkeypatch.setattr(
        search_service, "dsr_repository",
        types.SimpleNamespace(create_search_run=_create_search_run, add_evidence=_add_evidence),
    )
    monkeypatch.setattr(
        search_service, "factory",
        types.SimpleNamespace(
            build_connector=lambda **_k: _StubConnector(has_identity_table=has_identity_table)
        ),
    )

    async def _flush():
        return None

    summary = search_service.SearchSummary()
    await search_service._search_one_source(
        types.SimpleNamespace(flush=_flush),
        types.SimpleNamespace(org_id=None, id=None, reference="DSR-1"),
        types.SimpleNamespace(id=None),
        types.SimpleNamespace(id=None, name="crm", enabled=True),
        {"email": "a@example.com"},
        summary,
        job_id=None,
        correlation_id=None,
    )
    return summary, run


async def test_a_source_without_an_identity_table_forces_review(monkeypatch):
    """PostgresDsrConnector documents that it reports `distinct_subjects=1` for any
    number of matches when no identity table is configured, and leaves "the search
    service to treat that as a review signal rather than a clean result". Nothing did.
    `identity_tables` defaults to empty, so on a default-configured source the §42
    scenario-5 guard -- the one that stops a plan being built over records that may
    belong to two different people -- was silently inert."""
    from app.agents.dsr.schemas import case

    summary, run = await _search_with(monkeypatch, has_identity_table=False)

    assert summary.ambiguous is True, (
        "a source that cannot count distinct subjects reported a clean single-subject "
        "result, so the case would have proceeded straight to planning"
    )
    assert run.error_code == case.ERR_MULTIPLE_MATCHES
    assert any("identity_tables" in note for note in summary.notes)


async def test_a_properly_configured_source_is_unaffected(monkeypatch):
    """A source that really did establish one subject must still pass cleanly --
    otherwise the fix routes every ordinary access request to a human."""
    summary, run = await _search_with(monkeypatch, has_identity_table=True)

    assert summary.ambiguous is False
    assert run.error_code is None
    assert run.status == "completed"


# ── DSR: a response must not claim a change that did not happen ──────────────


def _response_text(*, request_type: str, executions, actions=()):
    import types
    from datetime import datetime as _dt

    from app.agents.dsr.services import response_service

    request = types.SimpleNamespace(
        reference="DSR-ABC123", request_type=request_type,
        received_at=_dt(2026, 9, 1, tzinfo=UTC),
    )
    runs = [types.SimpleNamespace(
        source_name="crm", status="completed", error_code=None,
        match_count=1, tables_searched=["customers"], id="r1",
    )]
    evidence = [types.SimpleNamespace(
        source_name="crm", table_name="customers", matched_column="email",
        match_type="exact", record_snapshot={"email": "a@example.com"}, id="e1",
    )]
    return response_service._compose(request, evidence, runs, executions, actions)


def _execution(*, rows_affected: int, action_id: str):
    import types

    return types.SimpleNamespace(
        status="verified", verification_status="passed",
        rows_affected=rows_affected, error_code=None, id="x1", action_id=action_id,
    )


def _action(*, operation: str, action_id: str, status: str = "executed"):
    import types

    return types.SimpleNamespace(
        id=action_id, operation=operation, status=status,
        source_name="crm", table_name="customers",
        blocked_reason=None, requester_explanation=None,
    )


def test_an_access_response_does_not_claim_records_were_updated():
    """A disclose is executed like any other action and, being non-mutating, records
    rows_affected=0 with verification_status='passed'. Selecting on that alone told a
    requester who asked to SEE their data that we "updated 0 record(s) as requested,
    and confirmed the change by re-reading each record afterwards" -- one line below
    telling them the data was attached."""
    from app.agents.dsr.schemas import case

    body = _response_text(
        request_type=case.ACCESS,
        executions=[_execution(rows_affected=0, action_id="a1")],
        actions=[_action(operation=case.OP_DISCLOSE, action_id="a1")],
    )

    assert "updated 0 record(s)" not in body, f"access response claims an update:\n{body}"
    assert "confirmed the change" not in body, (
        f"access response asserts a verified change that never happened:\n{body}"
    )
    assert "attached to this response" in body, "the access response lost its actual answer"


def test_a_deletion_response_still_reports_what_was_deleted():
    """The fix must not silence a real change."""
    from app.agents.dsr.schemas import case

    body = _response_text(
        request_type=case.DELETION,
        executions=[_execution(rows_affected=2, action_id="a1")],
        actions=[_action(operation=case.OP_DELETE_RECORD, action_id="a1")],
    )

    assert "We deleted 2 record(s) as requested" in body, body
    assert "confirmed the change" in body


def test_a_verified_deletion_of_zero_rows_is_still_reported():
    """A delete whose record was already gone affects 0 rows but is a real, planned
    mutating action -- it must still be reported rather than dropped by the fix."""
    from app.agents.dsr.schemas import case

    body = _response_text(
        request_type=case.DELETION,
        executions=[_execution(rows_affected=0, action_id="a1")],
        actions=[_action(operation=case.OP_DELETE_RECORD, action_id="a1")],
    )

    assert "We deleted 0 record(s) as requested" in body, body
