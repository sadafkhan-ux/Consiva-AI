"""End-to-end ROPA pipeline test over synthetic evidence.

Uses an event/attendee shaped schema (the PrepMyEvent-style case) so the test
exercises the full chain: classification -> subject -> purpose -> activity ->
data flow -> risk -> ROPA record -> human review.
"""

import pytest

from app.agents.ropa.schemas.evidence import (
    BusinessMetadataRecord,
    ColumnRecord,
    DiscoveryEvidence,
    RelationshipRecord,
    RoleRecord,
    SourceRecord,
    TableRecord,
    VendorRecord,
)
from app.agents.ropa.services import discovery_service, risk_service


def _column(local_id, table_local_id, name, data_type="text"):
    return ColumnRecord(
        local_id=local_id, table_local_id=table_local_id, column_name=name, data_type=data_type
    )


@pytest.fixture
def evidence() -> DiscoveryEvidence:
    return DiscoveryEvidence(
        org_id="org-1",
        discovery_run_id="run-1",
        sources=[
            SourceRecord(
                local_id="source-1", name="prepmyevent.com", source_type="database",
                connector="postgres", location="db.internal",
            )
        ],
        tables=[
            TableRecord(local_id="table-1", source_local_id="source-1", table_name="attendees"),
            TableRecord(local_id="table-2", source_local_id="source-1", table_name="events"),
            TableRecord(local_id="table-3", source_local_id="source-1", table_name="payments"),
        ],
        columns=[
            _column("column-1", "table-1", "id"),
            _column("column-2", "table-1", "full_name"),
            _column("column-3", "table-1", "email"),
            _column("column-4", "table-1", "phone"),
            _column("column-5", "table-1", "created_at", "timestamp with time zone"),
            _column("column-6", "table-2", "event_name"),
            _column("column-7", "table-2", "venue_city"),
            _column("column-8", "table-3", "card_number"),
            _column("column-9", "table-3", "billing_address"),
        ],
        relationships=[
            RelationshipRecord(
                local_id="rel-1", from_table_local_id="table-1", from_column="event_id",
                to_table_local_id="table-2", to_column="id", constraint_name="fk_attendee_event",
            )
        ],
        vendors=[
            VendorRecord(
                local_id="vendor-1", name="Stripe", role="processor",
                integration_local_id="source-1", location="US", dpa_status="Confirmed",
            )
        ],
        business_metadata=[
            BusinessMetadataRecord(
                local_id="meta-1", subject_local_id="table-1",
                business_owner="Events Team", retention_policy="24 months",
            )
        ],
    )


def test_pipeline_detects_personal_data(evidence):
    out = discovery_service.run_pipeline(evidence)
    found = {(e.table, e.column, e.classification) for e in out.personal_data_inventory}

    assert ("attendees", "email", "Contact Data") in found
    assert ("attendees", "phone", "Contact Data") in found
    assert ("attendees", "full_name", "Identity Data") in found
    assert ("payments", "card_number", "Financial Data") in found


def test_operational_columns_are_not_personal_data(evidence):
    out = discovery_service.run_pipeline(evidence)
    columns = {e.column for e in out.personal_data_inventory}
    assert "id" not in columns
    assert "created_at" not in columns
    # `event_name` names an event, not a person.
    assert "event_name" not in columns


def test_data_subject_and_purpose_mapped(evidence):
    out = discovery_service.run_pipeline(evidence)
    attendee_elements = [e for e in out.personal_data_inventory if e.table == "attendees"]
    assert attendee_elements
    assert all(e.data_subject == "Attendee" for e in attendee_elements)

    purposes = {p.purpose for p in out.purpose_mappings}
    assert "Event Attendee Management" in purposes
    assert "Payment Processing" in purposes


def test_purpose_is_never_invented(evidence):
    """`events` matches no purpose rule, so it must be Unknown + review."""
    out = discovery_service.run_pipeline(evidence)
    unknown = [p for p in out.purpose_mappings if p.purpose == "Unknown"]
    assert unknown, "events table should yield an Unknown purpose"
    assert all(p.review_required for p in unknown)


def test_processing_activities_grouped_by_purpose(evidence):
    out = discovery_service.run_pipeline(evidence)
    names = {a.name for a in out.processing_activities}
    assert "Event Attendee Management" in names
    assert "Payment Processing" in names

    attendee_activity = next(a for a in out.processing_activities if a.name == "Event Attendee Management")
    assert attendee_activity.data_subjects == ["Attendee"]
    assert "Contact Data" in attendee_activity.personal_data_categories


def test_data_flow_uses_only_evidenced_nodes(evidence):
    out = discovery_service.run_pipeline(evidence)
    assert out.data_flows
    for flow in out.data_flows:
        assert flow.path[0] == "prepmyevent.com"
        # Stripe is the only evidenced processor; nothing else may appear.
        assert set(flow.path[2:]) <= {"Stripe"}


def test_ropa_records_generated_with_evidence(evidence):
    out = discovery_service.run_pipeline(evidence)
    assert out.ropa_records

    attendee = next(r for r in out.ropa_records if r.processing_activity == "Event Attendee Management")
    assert attendee.purpose == "Event Attendee Management"
    assert attendee.data_subjects == ["Attendee"]
    assert attendee.retention == "24 months"      # from business metadata
    assert attendee.business_owner == "Events Team"
    assert attendee.source_run_id == "run-1"
    assert attendee.evidence


def test_unknown_retention_stays_unknown(evidence):
    """payments has no business metadata, so retention must NOT be borrowed
    from the attendees table."""
    out = discovery_service.run_pipeline(evidence)
    payments = next(r for r in out.ropa_records if r.processing_activity == "Payment Processing")
    assert payments.retention == "Unknown"
    assert payments.business_owner == "Unknown"
    assert payments.review_required


def test_sensitive_data_raises_high_severity_finding(evidence):
    out = discovery_service.run_pipeline(evidence)
    high = [f for f in out.risk_and_gap_findings if f.severity == "high"]
    assert high
    assert all(f.severity_factors for f in high), "every severity must be explainable"


def test_no_finding_claims_a_legal_violation(evidence):
    out = discovery_service.run_pipeline(evidence)
    allowed = {"Potential Gap", "Requires Review", "Evidence Incomplete", "Potential Privacy Risk"}
    for finding in out.risk_and_gap_findings:
        assert finding.status in allowed
        assert "violat" not in finding.finding.lower()


def test_human_review_items_generated(evidence):
    out = discovery_service.run_pipeline(evidence)
    assert out.human_review_items
    assert all(item.decision is None for item in out.human_review_items), "undecided until a human acts"


def test_output_matches_pydantic_contract(evidence):
    """The full 16-section output must serialize cleanly."""
    out = discovery_service.run_pipeline(evidence)
    payload = out.model_dump()
    for section in (
        "discovery_summary", "personal_data_inventory", "classifications",
        "data_subject_mappings", "purpose_mappings", "processing_activities",
        "data_flows", "processors_and_vendors", "retention_findings",
        "access_findings", "risk_and_gap_findings", "ropa_records",
        "human_review_items", "evidence_references", "change_detection",
        "confidence_summary",
    ):
        assert section in payload


def test_multiple_processors_are_separate_branches_not_a_chain(evidence):
    """Two independent processors must never be rendered as one path.

    "source -> Stripe -> SendGrid" asserts that Stripe forwards data to
    SendGrid -- a relationship no evidence supports, and a false statement in a
    compliance record. Prompt §10: do not invent intermediate systems.
    """
    from app.agents.ropa.schemas.evidence import VendorRecord

    evidence.vendors.append(
        VendorRecord(local_id="vendor-2", name="SendGrid", role="processor",
                     integration_local_id="source-1", location="US", dpa_status="Missing")
    )
    out = discovery_service.run_pipeline(evidence)

    for flow in out.data_flows:
        vendors_in_path = [n for n in flow.path if n in {"Stripe", "SendGrid"}]
        assert len(vendors_in_path) <= 1, (
            f"path {flow.path} chains multiple processors; each needs its own branch"
        )

    # Both processors must still be represented, just separately.
    named = {n for f in out.data_flows for n in f.path}
    assert {"Stripe", "SendGrid"} <= named


def test_vendor_processor_is_scoped_to_its_own_activity():
    """A vendor evidenced for one processing activity's data flow must not be
    attributed to an unrelated activity that has no vendor evidence of its
    own -- `ropa_service._vendors_as_processors` used to dump every
    org-wide-evidenced vendor onto every RopaRecord regardless of which
    activity's flows actually referenced it, asserting a transfer
    relationship (and a processor/recipient/transfer_information entry) no
    evidence supported for the unrelated activity.
    """
    from app.agents.ropa.schemas.evidence import DiscoveryEvidence, SourceRecord, VendorRecord
    from app.agents.ropa.schemas.output import DataFlowMapping
    from app.agents.ropa.schemas.ropa import ProcessingActivity
    from app.agents.ropa.services import ropa_service

    minimal_evidence = DiscoveryEvidence(
        org_id="org-1", discovery_run_id="run-1",
        sources=[SourceRecord(local_id="source-1", name="app-db", source_type="database", connector="postgres")],
        vendors=[VendorRecord(local_id="vendor-1", name="Stripe", role="processor",
                               integration_local_id="source-1", location="US")],
    )
    billing = ProcessingActivity(name="Billing", purpose="Payment processing", evidence=["table-billing"], confidence=0.9)
    marketing = ProcessingActivity(name="Marketing", purpose="Email marketing", evidence=["table-marketing"], confidence=0.9)
    flows = [
        DataFlowMapping(processing_activity="Billing", path=["app-db", "Stripe"],
                         evidence=["table-billing", "vendor-1"]),
    ]

    records = ropa_service.build_ropa_records(
        evidence=minimal_evidence, activities=[billing, marketing], elements=[],
        data_flows=flows, retention=[], access=[],
    )
    by_name = {r.processing_activity: r for r in records}
    assert [p.name for p in by_name["Billing"].processors] == ["Stripe"]
    assert by_name["Marketing"].processors == []
    assert by_name["Marketing"].recipients == []


def test_flow_with_no_processor_is_flagged_for_review(evidence):
    """No evidenced processor means the flow is incomplete, not that the data
    stays put."""
    evidence.vendors.clear()
    out = discovery_service.run_pipeline(evidence)
    assert out.data_flows
    assert all(f.review_required for f in out.data_flows)


# ── Schema/source identity: a table name is not unique within one run ──────────


@pytest.fixture
def duplicate_table_name_evidence() -> DiscoveryEvidence:
    """Two DIFFERENT tables, both named "users", in different schemas of the
    same source -- a public SaaS app schema and a separately-migrated CRM
    schema. Both resolve to the same purpose ("User Account Management"), so
    they correctly end up in ONE processing activity -- but each table's own
    columns, retention and ownership metadata must stay attributable to ITS
    OWN table, never silently merged into the other's."""
    return DiscoveryEvidence(
        org_id="org-1",
        discovery_run_id="run-2",
        sources=[
            SourceRecord(
                local_id="source-1", name="acme.example.com", source_type="database",
                connector="postgres", location="db.internal",
            )
        ],
        tables=[
            TableRecord(local_id="table-1", source_local_id="source-1",
                        schema_name="public", table_name="users"),
            TableRecord(local_id="table-2", source_local_id="source-1",
                        schema_name="crm", table_name="users"),
        ],
        columns=[
            _column("column-1", "table-1", "email"),
            _column("column-2", "table-2", "ssn"),
        ],
        business_metadata=[
            BusinessMetadataRecord(
                local_id="meta-1", subject_local_id="table-1",
                business_owner="Product Team", retention_policy="24 months",
            ),
            BusinessMetadataRecord(
                local_id="meta-2", subject_local_id="table-2",
                business_owner="Sales Ops", retention_policy="7 years",
            ),
        ],
    )


def test_identical_table_names_across_schemas_are_not_merged(duplicate_table_name_evidence):
    """Regression test for the processing-activity/table-identity collision
    bug: before the fix, retention_by_table/access_by_table were dicts keyed
    by the bare table name ("users"), so the SECOND table's business metadata
    silently overwrote the first's in the dict comprehension -- a reviewer
    would see only one schema's retention/owner for an activity that actually
    spans two unrelated tables with conflicting policies."""
    out = discovery_service.run_pipeline(duplicate_table_name_evidence)

    # Both tables' columns must be present and correctly classified -- neither
    # table's evidence was dropped because the other table shared its name.
    found = {(e.table_local_id, e.column, e.classification) for e in out.personal_data_inventory}
    assert ("table-1", "email", "Contact Data") in found
    assert ("table-2", "ssn", "Government Identifier / High-Risk") in found

    # Both resolve to the same purpose, so this must be ONE activity, not two.
    activity = next(a for a in out.processing_activities if a.purpose == "User Account Management")
    assert {"table-1", "table-2"} <= set(activity.evidence)

    record = next(r for r in out.ropa_records if r.processing_activity == activity.name)
    # Both tables' distinct columns made it into the one merged record...
    assert "users.email" in record.data_elements or any(
        d.endswith(".email") for d in record.data_elements
    )
    assert any(d.endswith(".ssn") for d in record.data_elements)
    # ...but their CONFLICTING retention/ownership must not silently collapse
    # to whichever table's metadata happened to be read last. Before the fix,
    # retention_by_table/access_by_table were keyed by the bare name "users",
    # so one of these two rows was silently discarded.
    assert record.retention == "Unknown", (
        "two schemas disagree on retention (24 months vs 7 years); presenting "
        "either one as THE answer would be a false statement in the ROPA"
    )
    assert record.business_owner == "Unknown", "same reasoning for conflicting owners"
    assert record.review_required


# ── Access findings: a role must not be attributed to every table ──────────────


def _two_table_evidence(roles: list[RoleRecord]) -> DiscoveryEvidence:
    return DiscoveryEvidence(
        org_id="org-1",
        discovery_run_id="run-3",
        sources=[SourceRecord(local_id="source-1", name="acme.example.com",
                               source_type="database", connector="postgres")],
        tables=[
            TableRecord(local_id="table-1", source_local_id="source-1", table_name="attendees"),
            TableRecord(local_id="table-2", source_local_id="source-1", table_name="payments"),
        ],
        roles=roles,
    )


def test_role_with_no_declared_tables_maps_nothing():
    """Regression test for the access-findings bug: a role that exists
    SOMEWHERE in the evidence, with no table_local_ids (which is what every
    sender has actually ever sent, since this field is new), must leave
    EVERY table Unknown -- not "Mapped" just because a role was present
    anywhere. Global role existence is not table-specific access evidence."""
    evidence = _two_table_evidence(
        roles=[RoleRecord(local_id="role-1", name="app_readonly")]
    )
    findings = risk_service.build_access_findings(evidence)
    assert len(findings) == 2
    for finding in findings:
        assert finding.access_status == "Unknown"
        assert finding.access_roles == []
        assert finding.review_required


def test_role_declared_against_one_table_maps_only_that_table():
    """The fix: a role declaring table_local_ids=["table-1"] is evidence FOR
    table-1 specifically, and must not bleed into table-2's finding."""
    evidence = _two_table_evidence(
        roles=[RoleRecord(local_id="role-1", name="attendees_reader",
                           table_local_ids=["table-1"])]
    )
    findings = risk_service.build_access_findings(evidence)
    attendees = next(f for f in findings if f.table_local_id == "table-1")
    payments = next(f for f in findings if f.table_local_id == "table-2")

    assert attendees.access_status == "Mapped"
    assert attendees.access_roles == ["attendees_reader"]
    assert payments.access_status == "Unknown", (
        "the role named only table-1; table-2 must not inherit it"
    )
    assert payments.access_roles == []
    assert payments.review_required
