"""The 10-check engine gives the result the browser engine gave, case for case.

tests/fixtures/ten_checks_parity.json was recorded from frontend/src/report/tenChecks.ts (and the page and consent summaries
it read, scanPages.ts / reportVisual.ts) while that engine's own test suite ran, just before it was replaced by
app/scoring/ten_checks.py. Every case must come out identical: every result, mark, explanation, limitation and the score.

If a deliberate change to a check makes a case differ, this test is doing its job: update the expected output for that
case on purpose, never by regenerating the whole file blind.
"""

import json
import re
from pathlib import Path

import pytest

from app.scoring import ten_checks as T

DATA = json.loads((Path(__file__).parent / "fixtures" / "ten_checks_parity.json").read_text())


def _snake(o):
    if isinstance(o, dict):
        return {re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): _snake(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_snake(x) for x in o]
    return o


def _inputs(rec: dict) -> T.TenInputs:
    i, sp, v = rec["input"], rec["input"]["scanPages"], rec["input"]["visual"]
    return T.TenInputs(
        evidence=i["evidence"], policy_analysis=i["policyAnalysis"],
        scan_pages=None if sp is None else T.ScanPages(
            rows=sp["rows"], scanned_count=sp["scannedCount"], failed=sp["failed"], skipped=sp["skipped"],
            discovered=sp["discovered"], stop_reason=sp["stopReason"]),
        visual=None if v is None else T.Visual(
            pages_scanned=v["pagesScanned"], pages_found=v["pagesFound"],
            consent=T.Consent(known=v["consent"]["known"], accept_tested=v["consent"]["acceptTested"],
                              reject_tested=v["consent"]["rejectTested"])),
        screenshot_page_url=i["screenshotPageUrl"],
    )


def _comparable(out: dict) -> dict:
    """The fields the browser engine produced. `methodology` and each check's `kind_label` were added by the backend."""
    out = {k: v for k, v in out.items() if k != "methodology"}
    out["checks"] = [{k: v for k, v in c.items() if k != "kind_label"} for c in out["checks"]]
    return json.loads(json.dumps(out), parse_float=float, parse_int=float)


@pytest.mark.parametrize("n", range(len(DATA["engine"])))
def test_each_recorded_case_gives_the_identical_result(n):
    rec = DATA["engine"][n]
    got = _comparable(T.assess(_inputs(rec)))
    want = json.loads(json.dumps(_snake(rec["output"])), parse_float=float, parse_int=float)
    assert got == want


@pytest.mark.parametrize("n", range(len(DATA["inputs"])))
def test_the_page_and_consent_summaries_match(n):
    rec = DATA["inputs"][n]
    ev, meta = rec["input"]["evidence"], rec["input"]["meta"]
    sp = T.build_scan_pages(ev.get("pages") if ev is not None else None, meta)
    vis = T.build_visual(ev, meta)
    want_sp, want_vis = rec["output"]["scanPages"], rec["output"]["visual"]

    if want_sp is None:
        assert sp is None
    else:
        key = lambda r: json.dumps(r, sort_keys=True)  # noqa: E731 -- the order of the rows is not read by any check
        assert sorted(sp.rows, key=key) == sorted(want_sp["rows"], key=key)
        assert (sp.scanned_count, sp.failed, sp.skipped, sp.discovered, sp.stop_reason) == (
            want_sp["scannedCount"], want_sp["failed"], want_sp["skipped"], want_sp["discovered"], want_sp["stopReason"])
    if want_vis is None:
        assert vis is None
    else:
        assert (vis.pages_scanned, vis.pages_found, vis.consent.known, vis.consent.accept_tested, vis.consent.reject_tested) == (
            want_vis["pagesScanned"], want_vis["pagesFound"], want_vis["consent"]["known"],
            want_vis["consent"]["acceptTested"], want_vis["consent"]["rejectTested"])


def test_the_recordings_cover_every_kind_of_result():
    """Guards against the fixture being cut down to cases that cannot catch much."""
    seen = {r for rec in DATA["engine"] for r in (c["result"] for c in rec["output"]["checks"])}
    assert seen == {"Passed", "Partially Passed", "Failed", "Not Assessed", "Not Applicable"}
    assert len(DATA["engine"]) >= 150
