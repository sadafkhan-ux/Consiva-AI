"""The Python derivations the ten-check engine reads (page accounting, the consent comparison) match the browser builders they replace, on the recorded cases in
tests/fixtures/ten_inputs_golden.json."""

import json
from pathlib import Path

import pytest

from app.scoring.ten_inputs import build_scan_pages, build_visual, untested_consent_states

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "ten_inputs_golden.json").read_text(encoding="utf8"))


def test_the_oracle_has_cases():
    assert len(GOLDEN["pages"]) >= 40 and len(GOLDEN["visuals"]) >= 40


@pytest.mark.parametrize("i", range(len(GOLDEN["pages"])))
def test_page_accounting_matches(i):
    case = GOLDEN["pages"][i]
    got = build_scan_pages(case.get("pages"), case.get("meta"))
    want = case["expected"]
    if want is None:
        assert got is None
        return
    assert got is not None
    assert got["scanned_count"] == want["scanned_count"]
    assert got["discovered"] == want["discovered"]
    assert got["failed"] == want["failed"]
    assert [s["count"] for s in got["skipped"]] == want["skipped"]
    assert got["stop_reason"] == want["stop_reason"]
    # the engine reads the scanned addresses as a set (the browser builder happens to list the homepage first)
    assert sorted(r["url"] for r in got["rows"]) == sorted(want["scanned_urls"])


@pytest.mark.parametrize("i", range(len(GOLDEN["visuals"])))
def test_the_consent_comparison_matches(i):
    case = GOLDEN["visuals"][i]
    got = build_visual(case.get("evidence"), case.get("meta"))
    want = case["expected"]
    if want is None:
        assert got is None
        return
    assert got == want
    # the states the browser builder was told were untested are the ones this derives from the scan record
    assert set(case["untested"]) == set(untested_consent_states(case.get("meta")))


def test_with_no_scan_record_nothing_is_reported_as_untested():
    assert untested_consent_states(None) == []
    assert untested_consent_states({}) == ["post_accept", "post_reject"]
    assert untested_consent_states({"accept_interaction": "clicked", "reject_interaction": "click_failed"}) == ["post_reject"]
