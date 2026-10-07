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
from app.agents.ropa.services.classification_service import is_personal_data


def _display_name(table) -> str:
    """Schema-qualified label for descriptions/logs -- never used as a
    matching key (see build_activities' docstring note on table_local_id)."""
    return f"{table.schema_name}.{table.table_name}" if table.schema_name else table.table_name


def build_activities(
    evidence: DiscoveryEvidence,
    elements: list[PersonalDataElement],
) -> list[ProcessingActivity]:
    """Groups by purpose (see module docstring). Tables are identified by
    their `local_id` throughout -- NOT by `table_name` -- because a table
    name is not unique within one discovery run: two different schemas or two
    different sources in the same evidence bundle can legitimately both have
    a table named "users", and matching on the bare name would silently merge
    their columns into one activity. `local_id` is the one thing the evidence
    contract already guarantees is distinct per table (see _shape.assemble,
    which keys assignment on (schema, table)); `table_name`/`schema_name`
    stay purely for human-readable descriptions.
    """
    source_names = {s.local_id: s.name for s in evidence.sources}
    table_source = {t.local_id: source_names.get(t.source_local_id, "unknown_source") for t in evidence.tables}
    elements_by_table: dict[str, list[PersonalDataElement]] = {}
    for element in elements:
        elements_by_table.setdefault(element.table_local_id or "unknown_table", []).append(element)

    related = _related_table_local_ids(evidence)

    # purpose -> accumulated activity data
    grouped: dict[str, dict] = {}
    unknown_tables: list = []

    for table in evidence.tables:
        match = purpose_rules.match_table(table.table_name)
        if match is None:
            unknown_tables.append(table)
            continue

        bucket = grouped.setdefault(
            match.purpose,
            {
                "table_local_ids": [],
                "display_names": [],
                "subjects": set(),
                "categories": set(),
                "systems": set(),
                "evidence": [],
                "confidences": [],
            },
        )
        bucket["table_local_ids"].append(table.local_id)
        bucket["display_names"].append(_display_name(table))
        if match.data_subject:
            bucket["subjects"].add(match.data_subject)
        bucket["systems"].add(table_source.get(table.local_id, "unknown_source"))
        bucket["evidence"].extend([table.local_id, table.table_name])
        bucket["confidences"].append(match.confidence)
        for element in elements_by_table.get(table.local_id, []):
            if is_personal_data(element):
                bucket["categories"].add(element.classification)

    activities: list[ProcessingActivity] = []
    for purpose, bucket in sorted(grouped.items()):
        display_names = sorted(set(bucket["display_names"]))
        linked = sorted({lid for lid in bucket["table_local_ids"] if lid in related})
        description = f"Processing across table(s): {', '.join(display_names)}."
        if linked:
            linked_names = sorted({_lid_display(evidence, lid) for lid in linked})
            description += f" Foreign-key relationships corroborate grouping for: {', '.join(linked_names)}."
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
                # Contains each table's local_id (what ropa_service's
                # _tables_for_activity actually matches on) and its bare
                # table_name (kept for human-readable evidence citations).
                evidence=sorted(set(bucket["evidence"])),
                confidence=confidence,
                # An activity with no classified personal data at all is a weak
                # claim -- surface it rather than presenting it as established.
                review_required=no_categories,
            )
        )

    for table in sorted(unknown_tables, key=lambda t: (t.schema_name or "", t.table_name)):
        categories = sorted(
            {e.classification for e in elements_by_table.get(table.local_id, []) if is_personal_data(e)}
        )
        if not categories:
            # No personal data found and no recognizable purpose: nothing to
            # assert about this table, so don't manufacture an activity for it.
            continue
        display_name = _display_name(table)
        activities.append(
            ProcessingActivity(
                name=f"Unclassified processing: {display_name}",
                description=(
                    f"Table {display_name!r} holds personal data but no processing purpose "
                    "could be established from evidence."
                ),
                data_subjects=["Unknown"],
                personal_data_categories=categories,
                purpose="Unknown",
                source_systems=[table_source.get(table.local_id, "unknown_source")],
                evidence=[table.local_id, display_name],
                confidence=0.0,
                review_required=True,
            )
        )

    return activities


def _lid_display(evidence: DiscoveryEvidence, local_id: str) -> str:
    table = next((t for t in evidence.tables if t.local_id == local_id), None)
    return _display_name(table) if table else local_id


def _related_table_local_ids(evidence: DiscoveryEvidence) -> set[str]:
    """local_ids of tables that participate in at least one discovered
    foreign key -- identity-based, not name-based, for the same reason
    build_activities keys everything else on local_id."""
    known = {t.local_id for t in evidence.tables}
    related: set[str] = set()
    for rel in evidence.relationships:
        for local_id in (rel.from_table_local_id, rel.to_table_local_id):
            if local_id in known:
                related.add(local_id)
    return related
