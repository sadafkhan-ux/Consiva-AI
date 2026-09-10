"""ROPA record generation, human-review item collection, and final output
assembly (ROPA prompt §15, §19, §22).

Every RopaRecord is built from what the earlier stages actually established.
Fields with no supporting evidence stay the explicit "Unknown" the schema
defines rather than being omitted or filled in -- an incomplete ROPA that says
so is the goal, not a complete-looking one (prompt §24).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.output import (
    AccessFinding,
    DataFlowMapping,
    DiscoverySummary,
    RetentionFinding,
    RopaAgentOutput,
)
from app.agents.ropa.schemas.ropa import (
    ChangeDetectionEntry,
    ConfidenceSummary,
    DataSubjectMapping,
    HumanReviewItem,
    PersonalDataElement,
    ProcessingActivity,
    PurposeMapping,
    RiskGapFinding,
    RopaRecord,
    TransferInfo,
)


def build_ropa_records(
    evidence: DiscoveryEvidence,
    activities: list[ProcessingActivity],
    elements: list[PersonalDataElement],
    data_flows: list[DataFlowMapping],
    retention: list[RetentionFinding],
    access: list[AccessFinding],
) -> list[RopaRecord]:
    generated_at = datetime.now(UTC).isoformat()
    retention_by_table = {r.target: r for r in retention}
    access_by_table = {a.target: a for a in access}
    flows_by_activity: dict[str, list[DataFlowMapping]] = {}
    for flow in data_flows:
        flows_by_activity.setdefault(flow.processing_activity, []).append(flow)

    records: list[RopaRecord] = []
    for activity in activities:
        activity_tables = _tables_for_activity(activity, elements)
        activity_elements = [e for e in elements if e.table in activity_tables and e.classification != "Unknown"]

        retentions = {retention_by_table[t].retention for t in activity_tables if t in retention_by_table}
        resolved_retention = _single_or_unknown(retentions)

        owners = {access_by_table[t].owner for t in activity_tables if t in access_by_table}
        resolved_owner = _single_or_unknown(owners)

        access_roles = sorted({
            role for t in activity_tables if t in access_by_table for role in access_by_table[t].access_roles
        })

        flows = flows_by_activity.get(activity.name, [])
        storage_locations = sorted({node for flow in flows for node in flow.path[1:2]})

        processors = [
            v for v in _vendors_as_processors(evidence)
        ]

        confidences = [e.confidence for e in activity_elements] or [activity.confidence]
        confidence = round(sum(confidences) / len(confidences), 4)

        review_required = (
            activity.review_required
            or activity.purpose == "Unknown"
            or resolved_retention == "Unknown"
            or resolved_owner == "Unknown"
            or not processors
        )

        records.append(
            RopaRecord(
                processing_activity=activity.name,
                description=activity.description,
                purpose=activity.purpose,
                data_subjects=activity.data_subjects,
                personal_data_categories=activity.personal_data_categories,
                data_elements=sorted({f"{e.table}.{e.column}" for e in activity_elements}),
                source_systems=activity.source_systems,
                storage_locations=storage_locations,
                processors=processors,
                # Recipients are only asserted when a processor is evidenced; an
                # empty list means "not established", not "none exist".
                recipients=sorted({p.name for p in processors}),
                data_flows=[],
                retention=resolved_retention,
                access_roles=access_roles,
                business_owner=resolved_owner,
                transfer_information=_transfer_info(processors),
                consent_or_processing_context=None,
                security_control_status=None,
                evidence=activity.evidence,
                confidence=confidence,
                review_required=review_required,
                generated_at=generated_at,
                source_run_id=evidence.discovery_run_id,
                version=1,
            )
        )

    return records


def _tables_for_activity(activity: ProcessingActivity, elements: list[PersonalDataElement]) -> set[str]:
    """Activity evidence carries table local_ids and table names; the element
    rows carry names, so match on the names present in both."""
    names = {e.table for e in elements if e.table}
    return {ref for ref in activity.evidence if ref in names}


def _single_or_unknown(values: set[str]) -> str:
    """Collapse a set of evidenced values to one. Conflicting values are NOT
    silently picked between -- that becomes Unknown and therefore a review item."""
    concrete = {v for v in values if v and v != "Unknown"}
    if len(concrete) == 1:
        return concrete.pop()
    return "Unknown"


def _vendors_as_processors(evidence: DiscoveryEvidence):
    from app.agents.ropa.schemas.ropa import VendorProcessor

    return [
        VendorProcessor(
            name=v.name,
            role=v.role,
            purpose=None,
            data_shared=[],
            location=v.location,
            dpa_status=v.dpa_status,
            evidence=[v.local_id],
        )
        for v in evidence.vendors
    ]


def _transfer_info(processors: list) -> TransferInfo:
    """A transfer is only asserted where a processor location is evidenced
    (prompt §14). No location evidence means unknown, never 'domestic'."""
    located = [p for p in processors if p.location]
    if not located:
        return TransferInfo(
            is_international_transfer=None,
            destination_country=None,
            evidence=[],
            review_required=True,
        )
    return TransferInfo(
        is_international_transfer=None,
        destination_country=", ".join(sorted({p.location for p in located})),
        evidence=[ref for p in located for ref in p.evidence],
        review_required=True,
    )


def collect_review_items(
    elements: list[PersonalDataElement],
    purposes: list[PurposeMapping],
    subjects: list[DataSubjectMapping],
    activities: list[ProcessingActivity],
    findings: list[RiskGapFinding],
    records: list[RopaRecord],
) -> list[HumanReviewItem]:
    """Everything the pipeline could not establish with confidence (prompt §19).
    Per-column unknowns are aggregated by table so the queue stays reviewable."""
    items: list[HumanReviewItem] = []

    unknown_by_table: dict[str, list[PersonalDataElement]] = {}
    for element in elements:
        if element.review_required:
            unknown_by_table.setdefault(element.table or "unknown_table", []).append(element)
    for table, group in sorted(unknown_by_table.items()):
        items.append(
            HumanReviewItem(
                target=f"table:{table}",
                reason=(
                    f"{len(group)} column(s) need classification review: "
                    + ", ".join(e.column for e in group[:10])
                ),
                evidence=sorted({ref for e in group for ref in e.evidence})[:50],
            )
        )

    for purpose in purposes:
        if purpose.review_required:
            items.append(
                HumanReviewItem(
                    target=f"purpose:{purpose.evidence[1] if len(purpose.evidence) > 1 else purpose.evidence[0]}",
                    reason="Processing purpose could not be established from evidence.",
                    evidence=purpose.evidence,
                )
            )

    for subject in subjects:
        if subject.review_required:
            items.append(
                HumanReviewItem(
                    target=f"data_subject:{subject.evidence[1] if len(subject.evidence) > 1 else subject.evidence[0]}",
                    reason="Data subject could not be established from evidence.",
                    evidence=subject.evidence,
                )
            )

    for activity in activities:
        if activity.review_required:
            items.append(
                HumanReviewItem(
                    target=f"activity:{activity.name}",
                    reason="Processing activity requires confirmation before publication.",
                    evidence=activity.evidence,
                )
            )

    for finding in findings:
        if finding.review_required:
            items.append(
                HumanReviewItem(
                    target=f"finding:{finding.status}",
                    reason=finding.finding,
                    evidence=finding.related_evidence,
                )
            )

    for record in records:
        if record.review_required:
            items.append(
                HumanReviewItem(
                    target=f"ropa:{record.processing_activity}",
                    reason="ROPA record has unresolved fields and requires human confirmation.",
                    evidence=record.evidence,
                )
            )

    return items


def build_confidence_summary(
    elements: list[PersonalDataElement],
    review_items: list[HumanReviewItem],
    *,
    low_confidence_threshold: float = 0.7,
) -> ConfidenceSummary:
    scored = [e for e in elements if e.classification != "Unknown"]
    overall = round(sum(e.confidence for e in scored) / len(scored), 4) if scored else 0.0
    return ConfidenceSummary(
        overall_confidence=overall,
        low_confidence_count=sum(1 for e in scored if e.confidence < low_confidence_threshold),
        review_required_count=len(review_items),
        notes=(
            None
            if scored
            else "No column classified as personal data; overall confidence is not meaningful."
        ),
    )


def build_output(
    evidence: DiscoveryEvidence,
    elements: list[PersonalDataElement],
    purposes: list[PurposeMapping],
    subjects: list[DataSubjectMapping],
    activities: list[ProcessingActivity],
    data_flows: list[DataFlowMapping],
    retention: list[RetentionFinding],
    access: list[AccessFinding],
    findings: list[RiskGapFinding],
    records: list[RopaRecord],
    review_items: list[HumanReviewItem],
    *,
    changes: list[ChangeDetectionEntry] | None = None,
) -> RopaAgentOutput:
    classified = [e for e in elements if e.classification != "Unknown"]
    return RopaAgentOutput(
        discovery_summary=DiscoverySummary(
            sources_scanned=len(evidence.sources),
            tables_scanned=len(evidence.tables),
            columns_scanned=len(evidence.columns),
            personal_data_elements_found=len(classified),
        ),
        personal_data_inventory=classified,
        classifications=elements,
        data_subject_mappings=subjects,
        purpose_mappings=purposes,
        processing_activities=activities,
        data_flows=data_flows,
        processors_and_vendors=_vendors_as_processors(evidence),
        retention_findings=retention,
        access_findings=access,
        risk_and_gap_findings=findings,
        ropa_records=records,
        human_review_items=review_items,
        evidence_references=sorted({ref for e in elements for ref in e.evidence}),
        change_detection=changes or [],
        confidence_summary=build_confidence_summary(elements, review_items),
    )
