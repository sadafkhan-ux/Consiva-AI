"""Phase 12: ROPA schema change detection."""

import pytest

from app.agents.ropa.schemas.evidence import ColumnRecord, DiscoveryEvidence, SourceRecord, TableRecord
from app.agents.ropa.schemas.ropa import PersonalDataElement
from app.agents.ropa.services import change_detection_service as cds
from app.agents.ropa.services import discovery_service


def _evidence(columns: dict[str, str], table: str = "attendees") -> DiscoveryEvidence:
    return DiscoveryEvidence(
        org_id="org-1",
        discovery_run_id="run-1",
        sources=[SourceRecord(local_id="source-1", name="prepmyevent.com", source_type="database")],
        tables=[TableRecord(local_id="table-1", source_local_id="source-1", table_name=table)],
        columns=[
            ColumnRecord(local_id=f"column-{i}", table_local_id="table-1", column_name=name, data_type=dtype)
            for i, (name, dtype) in enumerate(columns.items(), start=1)
        ],
    )


def test_first_run_reports_no_changes():
    """An empty baseline must not report a first run as 'everything is new'."""
    snapshot = cds.build_snapshot(_evidence({"email": "text"}))
    assert cds.detect_changes({}, snapshot) == []


def test_identical_snapshots_report_no_changes():
    snapshot = cds.build_snapshot(_evidence({"email": "text", "phone": "text"}))
    assert cds.detect_changes(snapshot, snapshot) == []


def test_new_column_detected():
    before = cds.build_snapshot(_evidence({"email": "text"}))
    after = cds.build_snapshot(_evidence({"email": "text", "aadhaar": "text"}))
    changes = cds.detect_changes(before, after)
    assert [c.change_type for c in changes] == ["new_field"]
    assert changes[0].target == "attendees.aadhaar"
    assert changes[0].review_required


def test_removed_column_detected():
    before = cds.build_snapshot(_evidence({"email": "text", "phone": "text"}))
    after = cds.build_snapshot(_evidence({"email": "text"}))
    changes = cds.detect_changes(before, after)
    assert [c.change_type for c in changes] == ["deleted_field"]
    assert changes[0].target == "attendees.phone"


def test_changed_data_type_detected():
    before = cds.build_snapshot(_evidence({"attendee_count": "integer"}))
    after = cds.build_snapshot(_evidence({"attendee_count": "text"}))
    changes = cds.detect_changes(before, after)
    assert [c.change_type for c in changes] == ["changed_data_type"]
    assert changes[0].previous_value == "integer"
    assert changes[0].current_value == "text"


def test_new_table_detected():
    before = cds.build_snapshot(_evidence({"email": "text"}, table="attendees"))
    after = cds.build_snapshot(_evidence({"amount": "numeric"}, table="payments"))
    changes = cds.detect_changes(before, after)
    types = {c.change_type for c in changes}
    assert "new_table" in types


def test_new_table_does_not_double_count_its_columns():
    """A brand-new table's columns must not ALSO be reported as new_field."""
    before = cds.build_snapshot(_evidence({"email": "text"}, table="attendees"))
    after = cds.build_snapshot(_evidence({"a": "text", "b": "text"}, table="payments"))
    changes = cds.detect_changes(before, after)
    new_fields = [c for c in changes if c.change_type == "new_field" and c.target.startswith("payments.")]
    assert not new_fields, "columns of a new table are already covered by new_table"


def test_column_becoming_personal_data_is_flagged():
    """The single most important signal: a column that now holds personal data."""
    evidence = _evidence({"notes": "text"})
    before = cds.build_snapshot(evidence, [
        PersonalDataElement(source="s", table="attendees", column="notes",
                            classification="Unknown", confidence=0.0, evidence=["column-1"]),
    ])
    after = cds.build_snapshot(evidence, [
        PersonalDataElement(source="s", table="attendees", column="notes",
                            classification="Contact Data", confidence=0.9, evidence=["column-1"]),
    ])
    changes = cds.detect_changes(before, after)
    assert [c.change_type for c in changes] == ["new_personal_data_category"]
    assert changes[0].current_value == "Contact Data"
    assert changes[0].review_required


def test_snapshot_contains_no_row_values():
    evidence = _evidence({"email": "text"})
    snapshot = cds.build_snapshot(evidence)
    assert set(snapshot) == {"tables", "columns", "classifications"}
    assert snapshot["columns"] == {"attendees.email": "text"}


def test_material_changes_require_review():
    before = cds.build_snapshot(_evidence({"email": "text"}))
    after = cds.build_snapshot(_evidence({"email": "text", "aadhaar": "text"}))
    for change in cds.detect_changes(before, after):
        if cds.is_material(change):
            assert change.review_required


def test_pipeline_populates_change_detection():
    """Regression guard: change_detection used to be hardcoded to []."""
    before_evidence = _evidence({"email": "text"})
    baseline = cds.build_snapshot(
        before_evidence, discovery_service.classification_service.classify_evidence(before_evidence)
    )

    after_evidence = _evidence({"email": "text", "aadhaar": "text"})
    output = discovery_service.run_pipeline(after_evidence, baseline_snapshot=baseline)

    assert output.change_detection, "pipeline must surface detected changes"
    assert any(c.target == "attendees.aadhaar" for c in output.change_detection)


def test_pipeline_without_baseline_reports_no_changes():
    output = discovery_service.run_pipeline(_evidence({"email": "text"}))
    assert output.change_detection == []


# ── Tier-4 LLM enrichment wiring (prompt §6) ────────────────────────────────────


async def test_enriched_pipeline_falls_back_to_rules_when_llm_unavailable(monkeypatch):
    """Enrichment is an escalation, never a dependency: an LLM outage must
    still produce a complete rules-only ROPA."""
    async def _boom(*_a, **_k):
        raise RuntimeError("LLM down")

    monkeypatch.setattr("app.llm.client.generate_structured_with_fallback", _boom)
    out = await discovery_service.run_pipeline_enriched(_evidence({"email": "text"}))
    assert out.personal_data_inventory, "rules result must survive an LLM failure"
    assert out.confidence_summary is not None


async def test_enriched_pipeline_applies_suggestions(monkeypatch):
    """A suggestion for an ambiguous column is applied, but stays unverified."""
    from app.agents.ropa.services.enrichment_service import ColumnSuggestion, EnrichmentResponse

    async def _fake(*_a, **_k):
        return EnrichmentResponse(suggestions=[
            ColumnSuggestion(column="mystery_field", category="Contact Data", reasoning="looks contact-ish")
        ]), {}

    monkeypatch.setattr("app.llm.client.generate_structured_with_fallback", _fake)
    out = await discovery_service.run_pipeline_enriched(_evidence({"mystery_field": "text"}))
    enriched = [e for e in out.classifications if e.column == "mystery_field"]
    assert enriched and enriched[0].classification == "Contact Data"
    assert enriched[0].review_required, "an LLM suggestion must never be authoritative"
    assert "source:llm_suggestion" in enriched[0].evidence


def test_sync_pipeline_makes_no_llm_call(monkeypatch):
    """The default path must stay fully deterministic and offline."""
    def _explode(*_a, **_k):
        raise AssertionError("run_pipeline must not call the LLM")

    monkeypatch.setattr("app.llm.client.generate_structured_with_fallback", _explode)
    out = discovery_service.run_pipeline(_evidence({"email": "text"}))
    assert out.personal_data_inventory
