"""The 10-check engine's arithmetic and its guarantees, stated directly (the case-by-case behaviour is in
test_ten_checks_parity.py)."""

import pytest

from app.scoring import ten_checks as T


def _check(result: str) -> dict:
    m = T.MARKS[result]
    return {"result": result, "awarded": m, "max": None if m is None else 1}


@pytest.mark.parametrize("v,expected", [(31.25, 31.3), (66.66666, 66.7), (50.0, 50.0), (0.05, 0.1)])
def test_scores_round_half_up_to_one_decimal(v, expected):
    assert T.round_score(v) == expected


def test_not_assessed_and_not_applicable_are_left_out_of_the_maximum():
    sc = T.score_checks([_check("Passed"), _check("Partially Passed"), _check("Failed"),
                         _check("Not Assessed"), _check("Not Applicable")])
    assert (sc["awarded"], sc["applicable_max"], sc["score"]) == (1.5, 3, 50.0)
    assert sc["total_text"] == "1.5 / 3" and sc["score_text"] == "50%"
    assert "1 check not applicable" in sc["formula"] and "1 check could not be assessed" in sc["formula"]


def test_no_applicable_check_means_no_score_never_zero():
    sc = T.score_checks([_check("Not Assessed"), _check("Not Applicable")])
    assert sc["score"] is None and sc["score_text"] == "No meaningful score"


def test_numbers_print_the_way_the_reports_always_printed_them():
    assert T._en_in(123456) == "1,23,456" and T._en_in(999) == "999" and T._en_in(1234567) == "12,34,567"
    assert T._fmt(7.5) == "7.5" and T._fmt(10) == "10" and T._fmt(1 / 3) == "0.33"


def test_an_empty_scan_still_gives_ten_checks_and_no_score():
    out = T.assess(T.build_inputs(evidence=None, meta=None, policy_analysis=None, screenshot_page_url=None))
    assert [c["id"] for c in out["checks"]] == list(range(1, 11))
    assert out["score"] is None
    assert all(c["result"] == "Not Assessed" for c in out["checks"])


def test_every_check_that_did_not_pass_gets_its_own_advice():
    out = T.assess(T.build_inputs(evidence={"pages": [{"url": "https://x.test/", "discovered_via": "seed"}], "policies": [],
                                            "cookies": [], "trackers": [], "consent_signals": []},
                                  meta={"pages_scanned": 1, "accept_interaction": "clicked", "reject_interaction": "clicked"},
                                  policy_analysis=None, screenshot_page_url="https://x.test/"))
    advised = {r["id"] for r in out["recommendations"]}
    for c in out["checks"]:
        if c["result"] in ("Failed", "Partially Passed"):
            assert c["id"] in advised
    assert out["checks"][0]["result"] == "Failed"           # no policy page among the scanned pages
    assert out["checks"][8]["result"] == "Not Applicable"   # no banner and nothing that tracks


def test_a_not_run_policy_analysis_is_the_same_as_none():
    ev = {"pages": [], "policies": [], "cookies": [], "trackers": [], "consent_signals": []}
    a = T.assess(T.build_inputs(evidence=ev, meta=None, policy_analysis={"status": "not_run"}, screenshot_page_url=None))
    b = T.assess(T.build_inputs(evidence=ev, meta=None, policy_analysis=None, screenshot_page_url=None))
    assert a == b


# ── Invariants every result keeps, over every recorded case (formerly the browser engine's "100 varied scenarios") ──

def _recorded_results():
    from tests.test_ten_checks_parity import DATA, _inputs
    return [T.assess(_inputs(rec)) for rec in DATA["engine"]]


_NOT_ASSESSED_BASES = {"Evidence unavailable", "Scan failed", "Not evidenced", "Assessment incomplete", "Content extraction failed",
                       "Access failed", "Crawling restricted", "Not tested", "Not found in scanned pages"}
_LIMIT_BASES = {"Content extraction failed", "Access failed", "Crawling restricted", "Assessment incomplete", "Scan failed"}


def test_every_result_keeps_the_methodology_invariants():
    import re

    for n, t in enumerate(_recorded_results()):
        checks = t["checks"]
        assert [c["id"] for c in checks] == list(range(1, 11)), n
        for c in checks:
            assert c["result"] in T.MARKS, n
            assert c["marks_text"] == ("N/A" if c["result"] == "Not Applicable" else "Not assessed" if c["result"] == "Not Assessed"
                                       else f"{T._num(c['awarded'])} / 1"), n
            assert len(c["explanation"]) > 5, n
            if c["result"] == "Passed":
                assert c["basis"] in ("Verified", "Partially extracted"), n
                assert c["evidence"] != "" or c["source_url"] is not None, n
            if c["result"] == "Not Assessed":
                assert c["basis"] in _NOT_ASSESSED_BASES, n
            if c["basis"] == "Verified":
                assert c["result"] == "Passed", n
            if c["basis"] in _LIMIT_BASES:
                # a limit of the scan is never worded as a disclosure that was looked for and not found
                assert "was not found in the policy text that was read" not in c["explanation"], n
        total = sum(c["awarded"] or 0 for c in checks if c["max"] is not None)
        maximum = sum(c["max"] for c in checks if c["max"] is not None)
        assert (t["awarded"], t["applicable_max"]) == (total, maximum), n
        assert t["applicable_max"] == 10 - t["counts"]["not_assessed"] - t["counts"]["not_applicable"], n
        assert t["score"] == (None if maximum == 0 else T.round_score(total / maximum * 100)), n
        assert sum(t["counts"].values()) == 10 and t["incomplete"] == t["counts"]["not_assessed"], n
        assert len(t["summary"]) > 20, n
        text = " ".join([t["summary"], *(c["explanation"] for c in checks), *(c["limitation"] or "" for c in checks)])
        assert not re.search(r"unable to verify", text, re.I), n
        assert not re.search(r"is a violation|violates|in violation|breach of the law|non-compliant|proof that the (policy|disclosure)", text, re.I), n


def test_the_same_input_always_gives_the_same_answer():
    from tests.test_ten_checks_parity import DATA, _inputs

    rec = DATA["engine"][0]
    first = T.assess(_inputs(rec))
    assert all(T.assess(_inputs(rec)) == first for _ in range(50))
