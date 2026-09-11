"""Turns DiscoveryEvidence into a personal-data inventory (ROPA prompt §5, §6).

Classification order follows the prompt's §6 exactly:
  1. an existing approved classification carried in on the evidence (never
     overwritten automatically)
  2. the deterministic precedence engine in rules/personal_data_rules.py
  3. otherwise an explicit Unknown + review_required -- never a guess

NOTHING IS SILENTLY DROPPED. Every discovered column produces exactly one
element. The previous version returned early for anything the rules called
"operational", which meant `campaign_leads.title` -- a job title -- vanished
with no review item and no trace. Operational columns are now recorded as
operational, with the reason, so a reviewer can disagree.
"""

from __future__ import annotations

from app.agents.ropa.rules import column_context, personal_data_rules, purpose_rules
from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.ropa import PersonalDataElement

DEFAULT_REVIEW_THRESHOLD = 0.7


def _lookup_maps(evidence: DiscoveryEvidence):
    sources = {s.local_id: s.name for s in evidence.sources}
    table_names = {t.local_id: t.table_name for t in evidence.tables}
    table_sources = {t.local_id: sources.get(t.source_local_id, "unknown_source") for t in evidence.tables}
    return table_names, table_sources


def _person_linked_tables(evidence: DiscoveryEvidence) -> set[str]:
    """Tables holding a foreign key into a people table.

    `email_events` names an artefact, but every row references a `lead_id`, so
    its rows describe people. Without this, `email_events.url` reads as a
    generic URL instead of behavioural data about a person.
    """
    names = {t.local_id: t.table_name.lower() for t in evidence.tables}
    person_tables = column_context.person_tables()
    linked: set[str] = set()
    for rel in evidence.relationships:
        target = names.get(rel.to_table_local_id, "")
        source = names.get(rel.from_table_local_id, "")
        if target in person_tables and source:
            linked.add(source)
    return linked


def classify_evidence(
    evidence: DiscoveryEvidence,
    *,
    review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
) -> list[PersonalDataElement]:
    table_names, table_sources = _lookup_maps(evidence)
    person_linked = _person_linked_tables(evidence)
    elements: list[PersonalDataElement] = []

    for column in evidence.columns:
        table_name = table_names.get(column.table_local_id, "unknown_table")
        source_name = table_sources.get(column.table_local_id, "unknown_source")
        dotted = f"{source_name}.{table_name}.{column.column_name}"
        base_evidence = [column.local_id, dotted]

        subject_match = purpose_rules.match_table(table_name)
        data_subject = subject_match.data_subject if subject_match and subject_match.data_subject else "Unknown"

        # A human-approved classification is rule order #1 and is never
        # recomputed -- the whole point of recording a decision.
        if column.existing_classification:
            elements.append(PersonalDataElement(
                source=source_name, table=table_name, column=column.column_name,
                classification=column.existing_classification,
                data_subject=column.existing_data_subject or data_subject,
                confidence=1.0, evidence=[*base_evidence, "source:declared_by_owner"],
                review_required=False, review_reason=None,
            ))
            continue

        result = personal_data_rules.classify_column(
            column.column_name,
            column.data_type,
            table_name=table_name,
            has_person_relationship=table_name.lower() in person_linked,
        )

        needs_review = result.review_required or (
            result.status == personal_data_rules.CLASSIFIED and result.confidence < review_threshold
        )
        elements.append(PersonalDataElement(
            source=source_name, table=table_name, column=column.column_name,
            classification=result.category or _label_for(result.status),
            data_subject=data_subject if result.is_personal_data else "Unknown",
            confidence=result.confidence,
            evidence=[*base_evidence, f"method:{result.method}", *result.evidence],
            review_required=needs_review,
            review_reason=_reason(result, review_threshold) if needs_review else None,
        ))

    return elements


_STATUS_LABELS = {
    personal_data_rules.OPERATIONAL: "Not Personal Data (operational)",
    personal_data_rules.PROVENANCE: "Not Personal Data (provenance)",
    personal_data_rules.UNKNOWN: "Unknown",
    personal_data_rules.REVIEW: "Unknown",
}


def _label_for(status: str) -> str:
    return _STATUS_LABELS.get(status, "Unknown")


def _reason(result, threshold: float) -> str:
    if result.conflicts:
        return (f"Rules disagreed ({', '.join(result.conflicts)}); "
                f"{result.method} won on precedence but confidence was reduced.")
    if result.status == personal_data_rules.UNKNOWN:
        return "No rule matched this column; a reviewer should classify it or mark it non-personal."
    if result.status == personal_data_rules.REVIEW:
        return result.evidence[-1] if result.evidence else "Requires human review."
    return (f"Confidence {result.confidence} is below the {threshold} threshold "
            f"(matched by {result.method}).")


def personal_data_only(elements: list[PersonalDataElement]) -> list[PersonalDataElement]:
    """The subset that actually classified as personal data -- excluding the
    Unknown and explicitly non-personal rows, which are review candidates and
    documented exclusions rather than confirmed findings."""
    excluded = set(_STATUS_LABELS.values())
    return [e for e in elements if e.classification not in excluded]
