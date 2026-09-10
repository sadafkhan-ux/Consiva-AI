"""Data-subject and purpose mapping (ROPA prompt §7, §8).

Both mappings are produced per TABLE, not per column, because a purpose is a
property of what a table is for. Where rules/purpose_rules.py has no match the
mapping is emitted as "Unknown" with review_required=True -- the prompt forbids
inventing a purpose, so an unrecognized table becomes a review item, never a
plausible-sounding guess.
"""

from __future__ import annotations

from app.agents.ropa.rules import purpose_rules
from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.ropa import DataSubjectMapping, PurposeMapping


def _elements_by_table(evidence: DiscoveryEvidence) -> dict[str, list[str]]:
    """table_local_id -> the column local_ids it contains."""
    grouped: dict[str, list[str]] = {}
    for column in evidence.columns:
        grouped.setdefault(column.table_local_id, []).append(column.local_id)
    return grouped


def map_purposes_and_subjects(
    evidence: DiscoveryEvidence,
) -> tuple[list[PurposeMapping], list[DataSubjectMapping]]:
    by_table = _elements_by_table(evidence)
    purposes: list[PurposeMapping] = []
    subjects: list[DataSubjectMapping] = []

    for table in evidence.tables:
        column_ids = by_table.get(table.local_id, [])
        if not column_ids:
            continue

        match = purpose_rules.match_table(table.table_name)
        table_evidence = [table.local_id, table.table_name]

        if match is None:
            purposes.append(
                PurposeMapping(
                    element_evidence=column_ids,
                    purpose="Unknown",
                    confidence=0.0,
                    evidence=table_evidence,
                    review_required=True,
                )
            )
            subjects.append(
                DataSubjectMapping(
                    element_evidence=column_ids,
                    data_subject="Unknown",
                    confidence=0.0,
                    evidence=table_evidence,
                    review_required=True,
                )
            )
            continue

        purposes.append(
            PurposeMapping(
                element_evidence=column_ids,
                purpose=match.purpose,
                confidence=match.confidence,
                evidence=[*table_evidence, f"rule:{match.rule_id}"],
                review_required=False,
            )
        )
        subjects.append(
            DataSubjectMapping(
                element_evidence=column_ids,
                data_subject=match.data_subject or "Unknown",
                confidence=match.confidence if match.data_subject else 0.0,
                evidence=[*table_evidence, f"rule:{match.rule_id}"],
                review_required=match.data_subject is None,
            )
        )

    return purposes, subjects
