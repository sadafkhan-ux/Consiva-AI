"""Turns DiscoveryEvidence into a personal-data inventory (ROPA prompt §5, §6).

Classification order follows the prompt's §6 exactly:
  1. an existing approved classification carried in on the evidence (never
     overwritten automatically)
  2. a deterministic rule from rules/personal_data_rules.py
  3. otherwise Unknown + review_required -- never a guess

Structurally operational columns (ids, timestamps, status flags) are dropped
rather than returned as Unknown, because flooding the review queue with
`created_at` rows makes the real unknowns impossible to see.
"""

from __future__ import annotations

from app.agents.ropa.rules import personal_data_rules, purpose_rules
from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.ropa import PersonalDataElement

DEFAULT_REVIEW_THRESHOLD = 0.7


def _lookup_maps(evidence: DiscoveryEvidence) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """table_local_id -> table name, table_local_id -> source name, and
    table_local_id -> schema-qualified label."""
    sources = {s.local_id: s.name for s in evidence.sources}
    table_names = {t.local_id: t.table_name for t in evidence.tables}
    table_sources = {t.local_id: sources.get(t.source_local_id, "unknown_source") for t in evidence.tables}
    return table_names, table_sources, sources


def classify_evidence(
    evidence: DiscoveryEvidence,
    *,
    review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
) -> list[PersonalDataElement]:
    table_names, table_sources, _ = _lookup_maps(evidence)
    elements: list[PersonalDataElement] = []

    for column in evidence.columns:
        table_name = table_names.get(column.table_local_id, "unknown_table")
        source_name = table_sources.get(column.table_local_id, "unknown_source")
        dotted = f"{source_name}.{table_name}.{column.column_name}"
        # Both the machine-resolvable ref and the human-readable path, because
        # findings are read by people but joined by local_id.
        evidence_refs = [column.local_id, dotted]

        subject_match = purpose_rules.match_table(table_name)
        data_subject = subject_match.data_subject if subject_match and subject_match.data_subject else "Unknown"

        if column.existing_classification:
            elements.append(
                PersonalDataElement(
                    source=source_name,
                    table=table_name,
                    column=column.column_name,
                    classification=column.existing_classification,
                    data_subject=column.existing_data_subject or data_subject,
                    confidence=1.0,
                    evidence=evidence_refs,
                    review_required=False,
                    review_reason=None,
                )
            )
            continue

        if personal_data_rules.is_operational(column.column_name):
            continue

        # A generic column like `name` only means a person when the table is
        # about people -- `cookies.name` is a cookie's name, not someone's.
        person_context = bool(subject_match and subject_match.data_subject)
        match = personal_data_rules.classify_column(
            column.column_name, column.data_type, person_context=person_context
        )
        if match is None:
            elements.append(
                PersonalDataElement(
                    source=source_name,
                    table=table_name,
                    column=column.column_name,
                    classification="Unknown",
                    data_subject=data_subject,
                    confidence=0.0,
                    evidence=evidence_refs,
                    review_required=True,
                    review_reason="No deterministic rule matched this column name; classification is unknown.",
                )
            )
            continue

        needs_review = match.confidence < review_threshold
        elements.append(
            PersonalDataElement(
                source=source_name,
                table=table_name,
                column=column.column_name,
                classification=match.category,
                data_subject=data_subject,
                confidence=match.confidence,
                evidence=[*evidence_refs, f"rule:{match.rule_id}"],
                review_required=needs_review,
                review_reason=(
                    f"Confidence {match.confidence} is below the {review_threshold} threshold "
                    f"(matched on {match.match_kind} '{match.matched_on}')."
                    if needs_review
                    else None
                ),
            )
        )

    return elements


def personal_data_only(elements: list[PersonalDataElement]) -> list[PersonalDataElement]:
    """The subset that actually classified as personal data -- i.e. excluding the
    Unknown rows, which are review candidates rather than confirmed findings."""
    return [e for e in elements if e.classification != "Unknown"]
