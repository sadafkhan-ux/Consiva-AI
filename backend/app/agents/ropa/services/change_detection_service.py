"""Schema change detection for ROPA continuous monitoring (ROPA prompt §20).

Deliberately separate from services/diff_engine.py. That module diffs consent
evidence and keys on cookie/tracker identity; this one diffs SCHEMA SHAPE
(tables, columns, data types) and on personal-data classification. Sharing one
differ would mean refactoring code Agent 1's live monitoring depends on, for no
functional gain.

Pure functions over snapshots -- no I/O, no database -- so the whole comparison
is unit-testable without a live source.
"""

from __future__ import annotations

from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.ropa import ChangeDetectionEntry, PersonalDataElement

# Changes that alter what personal data exists, or its meaning. These always
# create a review item; the rest are recorded but don't demand attention.
_MATERIAL_CHANGE_TYPES = frozenset({
    "new_table", "deleted_field", "new_field", "changed_data_type",
    "new_personal_data_category", "changed_purpose",
})


def build_snapshot(
    evidence: DiscoveryEvidence,
    elements: list[PersonalDataElement] | None = None,
) -> dict:
    """Reduce a run to a comparable fingerprint.

    Metadata only: table names, `table.column -> data_type`, and the
    classification each column resolved to. No row values ever enter a snapshot.
    """
    table_names = {t.local_id: t.table_name for t in evidence.tables}
    columns: dict[str, str] = {}
    for column in evidence.columns:
        table = table_names.get(column.table_local_id, "unknown_table")
        columns[f"{table}.{column.column_name}"] = column.data_type

    classifications: dict[str, str] = {}
    for element in elements or []:
        if element.table:
            classifications[f"{element.table}.{element.column}"] = element.classification

    return {
        "tables": sorted(table_names.values()),
        "columns": columns,
        "classifications": classifications,
    }


def detect_changes(baseline: dict, current: dict) -> list[ChangeDetectionEntry]:
    """Compare two snapshots. An empty baseline means this is the first run --
    that is NOT reported as "everything is new", because a first run has nothing
    to have changed from."""
    if not baseline or not baseline.get("columns"):
        return []

    changes: list[ChangeDetectionEntry] = []

    baseline_tables = set(baseline.get("tables", []))
    current_tables = set(current.get("tables", []))
    for table in sorted(current_tables - baseline_tables):
        changes.append(_entry("new_table", table, None, table))
    for table in sorted(baseline_tables - current_tables):
        changes.append(_entry("deleted_field", table, table, None,
                              override_type="removed_table"))

    baseline_columns: dict[str, str] = baseline.get("columns", {})
    current_columns: dict[str, str] = current.get("columns", {})

    for key in sorted(set(current_columns) - set(baseline_columns)):
        # A new column inside a brand-new table is already covered by new_table;
        # reporting both would double-count the same event.
        if key.split(".", 1)[0] in (current_tables - baseline_tables):
            continue
        changes.append(_entry("new_field", key, None, current_columns[key]))

    for key in sorted(set(baseline_columns) - set(current_columns)):
        if key.split(".", 1)[0] in (baseline_tables - current_tables):
            continue
        changes.append(_entry("deleted_field", key, baseline_columns[key], None))

    for key in sorted(set(baseline_columns) & set(current_columns)):
        if baseline_columns[key] != current_columns[key]:
            changes.append(_entry("changed_data_type", key,
                                  baseline_columns[key], current_columns[key]))

    baseline_class: dict[str, str] = baseline.get("classifications", {})
    current_class: dict[str, str] = current.get("classifications", {})
    for key in sorted(set(current_class)):
        was, now = baseline_class.get(key), current_class[key]
        if was == now:
            continue
        # A column becoming personal data is the single most important signal
        # this whole module exists to catch.
        if now != "Unknown" and (was is None or was == "Unknown"):
            changes.append(_entry("new_personal_data_category", key, was, now))
        elif was is not None and now != "Unknown":
            changes.append(_entry("changed_purpose", key, was, now))

    return changes


def _entry(
    change_type: str,
    target: str,
    previous: str | None,
    current: str | None,
    *,
    override_type: str | None = None,
) -> ChangeDetectionEntry:
    return ChangeDetectionEntry(
        change_type=change_type,
        target=target,
        previous_value=previous,
        current_value=current,
        evidence=[target],
        # A material change must be seen by a human before it becomes the new
        # normal; anything else is recorded without demanding attention.
        review_required=(override_type or change_type) in _MATERIAL_CHANGE_TYPES,
    )


def is_material(change: ChangeDetectionEntry) -> bool:
    return change.change_type in _MATERIAL_CHANGE_TYPES
