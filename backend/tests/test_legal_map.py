"""Every provision the Scanner Score relies on is checked against the text of the Act and the Rules (scoring/legal_map.py).

A phrase copied from each provision must be found in the official PDF in knowledge_sources/. The test is skipped, not passed, where the PDFs or pdftotext are not available.
"""

import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from app.scoring import domain_score as ds
from app.scoring import legal_map as lm

SOURCES = Path(__file__).resolve().parents[2] / "knowledge_sources"


def _text(name: str) -> str | None:
    pdf = SOURCES / name
    if not pdf.exists() or shutil.which("pdftotext") is None:
        return None
    out = ""
    for flags in (["-layout"], []):
        out += " " + subprocess.run(["pdftotext", *flags, str(pdf), "-"], capture_output=True, text=True, check=False).stdout
    return re.sub(r"\s+", " ", out).lower()


ACT, RULES = _text("DPDP Act 2023.pdf"), _text("DPDP Rules.pdf")


@pytest.mark.skipif(ACT is None or RULES is None, reason="the Act and Rules PDFs or pdftotext are not available")
def test_every_anchor_phrase_is_in_the_provision_it_names():
    missing = [f"{a.label}: {a.phrase!r}" for a in lm.all_anchors() if re.sub(r"\s+", " ", a.phrase).lower() not in (ACT if a.instrument == "Act" else RULES)]
    assert not missing, "\n".join(missing)


def test_every_parameter_and_indicator_has_provisions_with_a_relation_and_a_commencement_date():
    for p in ds.PARAMETERS:
        assert lm.ANCHORS.get(p[0]), p[0]
    assert set(lm.INDICATOR_ANCHORS) == {i[0] for i in ds.INDICATORS}
    for a in lm.all_anchors():
        assert a.relation in (lm.SOURCE, lm.CONTEXT, lm.PRACTICE) and a.commences in (lm.NOV_2025, lm.NOV_2026, lm.MAY_2027) and a.summary and a.phrase


def test_the_commencement_dates_follow_the_notification_and_rule_1():
    # every operative provision used here starts 13 May 2027 (Act ss.3 to 17; Rules 3 and 5 to 16)
    assert {a.commences for a in lm.all_anchors()} == {lm.MAY_2027}
    assert lm.S5_1.in_force_on(date(2026, 11, 5)) is False and lm.S5_1.in_force_on(date(2027, 5, 13)) is True and lm.S5_1.in_force_on(None) is False


def test_the_statutory_references_of_a_finding_say_whether_each_provision_is_in_force_on_the_scan_date():
    rows = lm.statutory_of("B2", date(2026, 11, 5))
    assert rows and all(r["in_force"] is False and r["commences"] == "2027-05-13" and r["relation"] for r in rows)
    assert all(r["in_force"] for r in lm.statutory_of("B2", date(2027, 6, 1)))
    assert lm.statutory_of("NOPE", date(2026, 1, 1)) == []
