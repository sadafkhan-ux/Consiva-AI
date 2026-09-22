"""Impact links that say something, and the two that were deliberately not built.

Before this, every link was a topic rule at POSSIBLE: "the change is about transfers
and you have ROPA records". True, and nearly useless. The records themselves know
which of them names a processor abroad.

The line this file guards is where evidence stops. A link may be upgraded on
something quoted from the record; it may never be upgraded on the record merely
existing, and it may never reach CONFIRMED unless a person put it there.
"""

import inspect
import uuid

import pytest

from app.agents.regwatch.errors import (
    ApprovalRequiredError,
    InvalidWatchTransitionError,
    WatchNotReadyError,
)
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import impact_service
from app.api.v1.routes import regwatch as routes
from app.db.models import RegWatchFinding


def _finding():
    return RegWatchFinding(
        org_id=uuid.uuid4(), change_id=uuid.uuid4(), source_id=uuid.uuid4(),
        reference="REG-X", status=watch.REVIEW_REQUIRED,
    )


# ── Evidence has to be evidence ─────────────────────────────────────────────────

def test_an_offshore_processor_is_evidence_for_a_transfer_change():
    payload = {"processors": [{"name": "Stripe", "location": "US"}]}
    evidence = impact_service._evidence_for("transfer", payload)
    assert evidence is not None
    # The reviewer has to be able to CHECK it, so it names the thing.
    assert "Stripe" in evidence and "US" in evidence


def test_a_domestic_processor_is_not_evidence_of_a_cross_border_transfer():
    payload = {"processors": [{"name": "Razorpay", "location": "India"}]}
    assert impact_service._evidence_for("transfer", payload) is None


def test_an_unrecognised_location_produces_no_claim_at_all():
    """Better silence than a guess. The marker list is explicit precisely so that
    what it does not cover produces nothing rather than a default."""
    payload = {"processors": [{"name": "Somebody", "location": "Freedonia"}]}
    assert impact_service._evidence_for("transfer", payload) is None


@pytest.mark.parametrize("placeholder", [
    "Unknown", "unknown", "  Not determined ", "N/A", "TBD", "none", "", None, "-",
])
def test_a_placeholder_is_a_gap_not_evidence(placeholder):
    """Measured live: four of five records carried retention="Unknown". Treating that
    as evidence would stamp PROBABLE on the ORGANISATION NOT KNOWING its retention --
    the exact false precision this agent exists to avoid."""
    assert impact_service._evidence_for("retention", {"retention": placeholder}) is None
    assert impact_service._evidence_for(
        "security", {"security_control_status": placeholder}
    ) is None
    assert impact_service._evidence_for(
        "consent", {"consent_or_processing_context": placeholder}
    ) is None


def test_a_real_retention_period_is_evidence():
    evidence = impact_service._evidence_for("retention", {"retention": "24 months"})
    assert evidence is not None and "24 months" in evidence


def test_children_evidence_needs_data_subjects_who_are_children():
    assert impact_service._evidence_for(
        "children", {"data_subjects": ["Attendee", "Customer"]}
    ) is None
    evidence = impact_service._evidence_for("children", {"data_subjects": ["Student"]})
    assert evidence is not None and "Student" in evidence


def test_rights_is_not_an_evidence_topic():
    """It matched every record, because every ROPA record has data elements. A
    confidence that never discriminates teaches a reviewer to ignore the confidence."""
    assert "rights" not in impact_service._ROPA_EVIDENCE
    assert impact_service._evidence_for("rights", {"data_elements": ["a.b", "c.d"]}) is None


def test_an_evidenced_link_is_probable_and_never_confirmed():
    body = inspect.getsource(impact_service._ropa_evidence_links)
    assert "confidence=watch.PROBABLE" in body
    assert "CONFIRMED" not in body
    assert watch.PROBABLE in watch.MACHINE_ASSERTABLE_CONFIDENCE


def test_the_evidence_is_quoted_in_the_rationale():
    """So a reviewer can check the claim rather than take it on trust."""
    body = inspect.getsource(impact_service._ropa_evidence_links)
    assert "{evidence}" in body
    # Matched on one source line: the sentence itself wraps across several.
    assert "finding that any obligation applies to it" in body
    assert "the kind of processing the change is about" in body


def test_a_record_reached_by_both_passes_appears_once_at_the_better_confidence():
    """Two rows for one record -- one possible, one probable -- makes the list longer
    and the reader less sure."""
    from app.db.models import RegWatchImpact

    target = uuid.uuid4()
    broad = [RegWatchImpact(
        org_id=uuid.uuid4(), finding_id=uuid.uuid4(),
        target_kind=watch.TARGET_ROPA_RECORD, target_id=target, target_label="x",
        confidence=watch.POSSIBLE, derived_from=watch.DERIVED_RULE,
    )]
    evidenced = [RegWatchImpact(
        org_id=uuid.uuid4(), finding_id=uuid.uuid4(),
        target_kind=watch.TARGET_ROPA_RECORD, target_id=target, target_label="x",
        confidence=watch.PROBABLE, derived_from=watch.DERIVED_ROPA,
    )]
    merged = impact_service._merge_links(broad, evidenced)
    assert len(merged) == 1
    assert merged[0].confidence == watch.PROBABLE
    assert merged[0].derived_from == watch.DERIVED_ROPA


def test_the_audit_records_the_confidences_that_were_actually_written():
    """It used to record a single hardcoded POSSIBLE, which stopped being true the
    moment an evidenced link could reach PROBABLE."""
    body = inspect.getsource(impact_service.map_impact)
    assert '"confidences": sorted({r.confidence for r in rows})' in body
    assert '"confidence": watch.POSSIBLE' not in body


# ── The two new targets ─────────────────────────────────────────────────────────

def test_ropa_data_sources_and_policies_are_now_mapped():
    body = inspect.getsource(impact_service._targets_for)
    assert "RopaDataSource" in body
    assert "Policy" in body


def test_policies_are_scoped_through_their_scan():
    """`policies` has no org_id of its own. Reading it unscoped would hand one tenant
    another tenant's policy URLs."""
    from app.db.models import Policy

    assert "org_id" not in {c.name for c in Policy.__table__.columns}
    body = inspect.getsource(impact_service._targets_for)
    policy_branch = body.split("if kind == watch.TARGET_POLICY:")[1].split("if kind ==")[0]
    assert "ConsentScan.org_id == org_id" in policy_branch
    assert "join(ConsentScan" in policy_branch


def test_only_enabled_ropa_sources_are_linked():
    body = inspect.getsource(impact_service._targets_for)
    ropa_branch = body.split("if kind == watch.TARGET_ROPA_SOURCE:")[1].split("if kind ==")[0]
    assert "enabled.is_(True)" in ropa_branch


# ── What was deliberately NOT built ─────────────────────────────────────────────

def test_control_is_not_mapped_automatically_and_the_code_says_why():
    """There is no control register in this platform. A rule pointing at "your
    controls" would be inventing the target."""
    module = inspect.getsource(impact_service)
    assert "WHY `control` IS NOT IN THAT TABLE" in module
    for targets in impact_service._TOPIC_TARGETS.values():
        assert watch.TARGET_CONTROL not in targets
    # But a person may still file one by hand.
    assert watch.TARGET_CONTROL in watch.IMPACT_TARGET_KINDS


def test_no_link_is_ever_derived_from_a_model():
    """A model-invented impact link is an applicability claim on no evidence, which
    is the thing this agent is built to refuse. The value stays in the vocabulary;
    nothing writes it."""
    module = inspect.getsource(impact_service)
    assert "watch.DERIVED_MODEL" not in module
    assert "derived_from=watch.DERIVED_MODEL" not in module


# ── The one place a person may assert ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_manual_link_needs_a_named_person():
    with pytest.raises(ApprovalRequiredError):
        await impact_service.add_manual_impact(
            None, _finding(), target_kind=watch.TARGET_OTHER,
            target_label="x", rationale="y" * 30, reviewer_user_id=None,
        )


@pytest.mark.asyncio
async def test_a_manual_link_needs_a_real_rationale():
    """It can assert CONFIRMED, which no rule may. That weight is why the floor is
    higher than on a review decision."""
    with pytest.raises(WatchNotReadyError):
        await impact_service.add_manual_impact(
            None, _finding(), target_kind=watch.TARGET_OTHER,
            target_label="x", rationale="too short", reviewer_user_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_a_manual_link_refuses_an_unknown_target_kind():
    with pytest.raises(InvalidWatchTransitionError):
        await impact_service.add_manual_impact(
            None, _finding(), target_kind="whatever",
            target_label="x", rationale="y" * 30, reviewer_user_id=uuid.uuid4(),
        )


def test_a_manual_link_is_the_only_route_to_confirmed():
    manual = inspect.getsource(impact_service.add_manual_impact)
    assert "watch.CONFIRMED" in manual
    # And nowhere else in the module writes it.
    module = inspect.getsource(impact_service)
    without_manual = module.replace(manual, "")
    assert "watch.CONFIRMED" not in without_manual


def test_a_manual_link_is_recorded_as_a_persons_assertion():
    body = inspect.getsource(impact_service.add_manual_impact)
    assert "derived_from=watch.DERIVED_MANUAL" in body
    assert '"asserted_by_a_person": True' in body


def test_re_running_the_mapper_does_not_erase_a_manual_link():
    """A reviewer's assertion destroyed by an automatic re-run would be the worst
    possible behaviour here."""
    from app.db.repositories import regwatch_repository

    body = inspect.getsource(regwatch_repository.replace_impacts)
    assert 'RegWatchImpact.derived_from != "manual"' in body


def test_only_a_manual_link_can_be_deleted():
    """A derived link deleted would reappear on the next assessment, so removing it
    would look like it worked and then silently undo itself."""
    body = inspect.getsource(routes.remove_manual_impact)
    assert "watch.DERIVED_MANUAL" in body
    assert "re-derives it" in body
