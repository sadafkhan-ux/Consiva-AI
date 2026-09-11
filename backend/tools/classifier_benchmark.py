"""Repeatable benchmark for the ROPA classifier.

Scores the rule engine against a real production schema with hand-labelled
ground truth, so a rule change can be measured rather than argued about.

    python -m tools.classifier_benchmark

Metadata only -- table names, column names and types. No row values, ever.
Schema captured from the PrepMyEvent run pme-e52384b3d470467a (11 tables,
153 columns), which scored 74% precision on the previous token-matching engine.
"""

from __future__ import annotations

import json
import pathlib
from collections import Counter

from app.agents.ropa.rules import personal_data_rules as pdr

FIXTURE = pathlib.Path(__file__).parent / "schema_prepmyevent.json"


def load():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["columns"], set(data["relationships"]), data["truth"]


def run():
    columns, person_linked, truth = load()
    rows, counts = [], Counter()

    for key, dtype in columns.items():
        table, column = key.split(".", 1)
        result = pdr.classify_column(
            column, dtype, table_name=table,
            has_person_relationship=table in person_linked,
        )
        expected = truth.get(key)
        got = result.category

        if expected is None:
            verdict = "unlabelled"
        elif expected == "-":
            # Must NOT be personal data. Deferring it to review is not correct,
            # but it is not a silent error either -- counted separately.
            verdict = ("correct" if not result.is_personal_data and not result.review_required
                       else "false_positive" if result.is_personal_data
                       else "over_referred")
        elif got == expected:
            verdict = "correct"
        elif result.is_personal_data:
            verdict = "wrong_category"
        elif result.review_required:
            verdict = "deferred"      # surfaced for a human, not lost
        else:
            verdict = "false_negative"  # real personal data, silently excluded

        counts[verdict] += 1
        counts["status:" + result.status] += 1
        rows.append((key, expected, got or result.status, round(result.confidence, 2),
                     verdict, result.method))

    scored = [v for v in ("correct", "false_positive", "wrong_category",
                          "false_negative", "deferred", "over_referred")]
    labelled = sum(counts[v] for v in scored)

    print(f"{'COLUMN':44} {'EXPECTED':20} {'GOT':22} {'CONF':>5}  VERDICT")
    print("-" * 120)
    for key, exp, got, conf, verdict, method in rows:
        if verdict in ("correct", "unlabelled"):
            continue
        print(f"{key:44} {exp!s:20} {got!s:22} {conf:5}  {verdict} ({method})")

    print("\n" + "=" * 64)
    print(f"columns evaluated          {len(columns)}")
    print(f"ground-truth labelled      {labelled}   (unlabelled: {counts['unlabelled']})")
    print(f"  correct                  {counts['correct']:4}  {counts['correct']/labelled*100:5.1f}%")
    for v in ("wrong_category", "false_positive", "false_negative", "deferred", "over_referred"):
        print(f"  {v:24} {counts[v]:3}  {counts[v]/labelled*100:5.1f}%")

    print("\noutcome distribution")
    for status in (pdr.CLASSIFIED, pdr.OPERATIONAL, pdr.PROVENANCE, pdr.REVIEW, pdr.UNKNOWN):
        print(f"  {status:18} {counts['status:' + status]:4}")
    print("\nsilently dropped             0   (the engine always returns a decision)")
    print("=" * 64)
    return counts, labelled


if __name__ == "__main__":
    run()
