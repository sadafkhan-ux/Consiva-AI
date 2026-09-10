"""Groups classified evidence into business-level processing activities
(ROPA prompt §9).

Grouping key is the PURPOSE, not the table: four tables that all serve
"Event Attendee Management" are one processing activity, which is what a ROPA
reader expects. The prompt warns against both failure modes -- don't invent
dozens of artificial activities, and don't merge unrelated activities just
because they share a system -- so tables whose purpose is Unknown are NOT
swept into a neighbouring activity. They each become their own review item.

Foreign-key relationships (evidence.relationships) are used as corroborating
evidence that two tables genuinely belong together, rather than as the grouping
key itself.
"""

from __future__ import annotations

from app.agents.ropa.rules import purpose_rules
from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.ropa import PersonalDataElement, ProcessingActivity


def build_activities(
    evidence: DiscoveryEvidence,
    elements: list[PersonalDataElement],
) -> list[ProcessingActivity]:
    source_names = {s.local_id: s.name for s in evidence.sources}
    table_source = {t.local_id: source_names.get(t.source_local_id, "unknown_source") for t in evidence.tables}
    elements_by_table: dict[str, list[PersonalDataElement]] = {}
    for element in elements:
        elements_by_table.setdefault(element.table or "unknown_table", []).append(element)

    related = _related_table_names(evidence)

    # purpose -> accumulated activity data
    grouped: dict[str, dict] = {}
    unknown_tables: list[tuple[str, str]] = []

    for table in evidence.tables:
        match = purpose_rules.match_table(table.table_name)
        if match is None:
            unknown_tables.append((table.table_name, table_source.get(table.local_id, "unknown_source")))
            continue

        bucket = grouped.setdefault(
            match.purpose,
            {
                "tables": [],
                "subjects": set(),
                "categories": set(),
                "systems": set(),
                "evidence": [],
                "confidences": [],
            },
        )
        bucket["tables"].append(table.table_name)
        if match.data_subject:
            bucket["subjects"].add(match.data_subject)
        bucket["systems"].add(table_source.get(table.local_id, "unknown_source"))
        bucket["evidence"].extend([table.local_id, table.table_name])
        bucket["confidences"].append(match.confidence)
        for element in elements_by_table.get(table.table_name, []):
            if element.classification != "Unknown":
                bucket["categories"].add(element.classification)

    activities: list[ProcessingActivity] = []
    for purpose, bucket in sorted(grouped.items()):
        tables = sorted(set(bucket["tables"]))
        linked = sorted({t for t in tables if t in related})
        description = f"Processing across table(s): {', '.join(tables)}."
        if linked:
            description += f" Foreign-key relationships corroborate grouping for: {', '.join(linked)}."
        confidence = round(sum(bucket["confidences"]) / len(bucket["confidences"]), 4)
        no_categories = not bucket["categories"]
        activities.append(
            ProcessingActivity(
                name=purpose,
                description=description,
                data_subjects=sorted(bucket["subjects"]) or ["Unknown"],
                personal_data_categories=sorted(bucket["categories"]),
                purpose=purpose,
                source_systems=sorted(bucket["systems"]),
                evidence=sorted(set(bucket["evidence"])),
                confidence=confidence,
                # An activity with no classified personal data at all is a weak
                # claim -- surface it rather than presenting it as established.
                review_required=no_categories,
            )
        )

    for table_name, source_name in sorted(unknown_tables):
        categories = sorted(
            {e.classification for e in elements_by_table.get(table_name, []) if e.classification != "Unknown"}
        )
        if not categories:
            # No personal data found and no recognizable purpose: nothing to
            # assert about this table, so don't manufacture an activity for it.
            continue
        activities.append(
            ProcessingActivity(
                name=f"Unclassified processing: {table_name}",
                description=(
                    f"Table {table_name!r} holds personal data but no processing purpose "
                    "could be established from evidence."
                ),
                data_subjects=["Unknown"],
                personal_data_categories=categories,
                purpose="Unknown",
                source_systems=[source_name],
                evidence=[table_name],
                confidence=0.0,
                review_required=True,
            )
        )

    return activities


def _related_table_names(evidence: DiscoveryEvidence) -> set[str]:
    """Table names that participate in at least one discovered foreign key."""
    names = {t.local_id: t.table_name for t in evidence.tables}
    related: set[str] = set()
    for rel in evidence.relationships:
        for local_id in (rel.from_table_local_id, rel.to_table_local_id):
            if local_id in names:
                related.add(names[local_id])
    return related
