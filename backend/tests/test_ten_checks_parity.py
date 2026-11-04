"""The Python ten-check engine gives exactly the results of the browser engine it replaced.

tests/fixtures/ten_check_golden.json is the recorded input and output of the TypeScript engine (frontend report/tenChecks.ts) across its whole test suite: every scenario of the
ten checks, the scoring boundaries, the discovery and extraction states and the 100 randomised combinations (159 distinct cases). The Python engine must reproduce every one,
check by check and word for word, so moving the score to the server changed no result.
"""

import json
import re
from pathlib import Path

import pytest

from app.scoring.ten_checks import assess_ten_checks

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "ten_check_golden.json").read_text(encoding="utf8"))


def _snake(k: str) -> str:
    return re.sub(r"[A-Z]", lambda m: "_" + m.group(0).lower(), k)


def _snake_keys(v):
    if isinstance(v, list):
        return [_snake_keys(x) for x in v]
    if isinstance(v, dict):
        return {_snake(k): _snake_keys(x) for k, x in v.items()}
    return v


def _inputs(case):
    return (
        GOLDEN["evidence_pool"][case["evidence_ref"]],
        case["policy_analysis"],
        _snake_keys(case["scan_pages"]) if case["scan_pages"] is not None else None,
        _snake_keys(case["visual"]) if case["visual"] is not None else None,
        case["screenshot_page_url"],
    )


def test_the_oracle_is_the_size_it_should_be():
    assert len(GOLDEN["cases"]) >= 150 and len(GOLDEN["evidence_pool"]) >= 50


@pytest.mark.parametrize("i", range(len(GOLDEN["cases"])))
def test_the_python_engine_matches_the_browser_engine(i):
    case = GOLDEN["cases"][i]
    got = assess_ten_checks(*_inputs(case))
    want = case["expected"]
    # check by check first, so a difference names the check and the field
    assert [c["id"] for c in got["checks"]] == [c["id"] for c in want["checks"]]
    for g, w in zip(got["checks"], want["checks"], strict=True):
        assert g == w, f"case {i}, check {w['id']} ({w['name']})"
    assert got == want, f"case {i}: the assessment as a whole"
