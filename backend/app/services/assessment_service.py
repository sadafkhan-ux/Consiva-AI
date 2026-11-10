"""The assessment of one stored scan: gathers what the engine reads and runs it.

The engines (scoring/domain_score.py, the Scanner Score, and scoring/ten_checks.py, kept for rollback) are pure functions; this module only assembles their inputs from what is stored
for the scan (the evidence, the policy analysis, the website-scan stage record and the homepage preview's address). It stores nothing: the result is worked out on request, so the
number always matches the evidence and the policy analysis as they are now.

SCORING_ENGINE chooses the engine ("scanner" by default; "ten_check" is the rollback). Either way the result has the same fields the dashboard and the PDFs read, so a rollback is a
setting, not a deploy of different code.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.scoring import domain_score, scorecard
from app.scoring.ten_checks import assess_ten_checks
from app.scoring.ten_inputs import build_scan_pages, build_visual

#: The version of the ten-check methodology the numbers come from. Raise it when a rule that can change a result changes. Kept for rollback (SCORING_ENGINE=ten_check).
TEN_CHECK_VERSION = "1.1.1"

#: The Scanner Score rules version (scoring/domain_score.py). Raise it when a rule that can change a result changes (L-15, SC-07).
SCANNER_VERSION = domain_score.SCORE_VERSION


#: What the crawler records about the pages, in the website-scan stage record, for Scanner Score A5, D1, D2 and G1 (scanner/page_signals.py).
def _check_name(code: str) -> str:
    """How a blocking check is named to the reader: "Check 6" for a scorecard row (P6), never the internal code. A code that is not a scorecard row reads as "an earlier check"."""
    return f"Check {code[1:]}" if len(code) > 1 and code[0] == "P" and code[1:].isdigit() else "an earlier check"


SCAN_SIGNAL_KEYS = ("language_signals", "notice_link_signals", "child_surfaces", "form_signals", "security_signals", "banner_by_page", "storage_signals", "mixed_content", "tls_signals")


def active_engine() -> str:
    """Which engine serves /assessment: "scanner" or, for rollback, "ten_check" (setting SCORING_ENGINE)."""
    return get_settings().scoring_engine


def active_version() -> str:
    """The rules version of the engine now serving results. It keys the report and PDF cache, so changing engine never serves a PDF written by the other."""
    engine = active_engine()
    return f"scorecard-{scorecard.SCORECARD_VERSION}" if engine == "scorecard" else f"scanner-{SCANNER_VERSION}" if engine == "scanner" else f"ten-check-{TEN_CHECK_VERSION}"


def policy_stage_state(stages: list[Any]) -> str:
    """Where the policy reading stands: done (a completed result exists), running, failed, or not_run (no such stage)."""
    rows = [s for s in stages if s.stage == "policy_analysis"]
    if any(s.status == "completed" and (s.stage_metadata or {}).get("result") for s in rows):
        return "done"
    if any(s.status in ("running", "pending") for s in rows):
        return "running"
    if any(s.status == "failed" or s.status == "completed" for s in rows):
        return "failed"
    return "not_run"


def website_scan_meta(stages: list[Any]) -> dict | None:
    """The website-scan stage's record, the first one, as the dashboard reads it."""
    return next(((s.stage_metadata or {}) for s in stages if s.stage == "website_scan"), None)


def scan_mode(scan_options: dict | None) -> str:
    """"deep" for an authorised application scan (the Deep Scan), "quick" for the public web profile. Read from the options the server stored on the scan."""
    from app.scanner.scan_profiles import APPLICATION, profile_for_scan_options

    return "deep" if profile_for_scan_options(scan_options).name == APPLICATION else "quick"


def assess_parts(
    evidence: dict | None, policy_result: dict | None, meta: dict | None, screenshot_page_url: str | None, mode: str = "quick", scanned_at: str | None = None, engine: str | None = None,
) -> dict[str, Any]:
    """Runs the active engine on already-loaded parts. `policy_result` is the stored policy analysis, or None when it has not run (an analysis recorded as not run counts as not run).

    The result always has the fields the dashboard and the PDFs read (checks, counts, score, score_text, summary, recommendations, pages, limitations). With the Scanner Score it also
    carries the whole result under `scanner` (band, coverage, confidence, parameters, findings); with the ten-check engine `scanner` is None.
    """
    policy = policy_result if policy_result and policy_result.get("status") != "not_run" else None
    pages = (evidence or {}).get("pages") if evidence is not None else None
    scan_pages, visual = build_scan_pages(pages, meta), build_visual(evidence, meta)
    if (engine or active_engine()) == "ten_check":
        return {**assess_ten_checks(evidence, policy, scan_pages, visual, screenshot_page_url), "engine": "ten_check", "scanner": None}
    signals = {k: meta[k] for k in SCAN_SIGNAL_KEYS if meta and k in meta}
    if (engine or active_engine()) == "scorecard":
        return as_scorecard(scorecard.assess_scorecard(evidence, policy, scan_pages, visual, screenshot_page_url, mode=mode, scanned_at=scanned_at, scan_signals=signals))
    return as_assessment(domain_score.assess_scanner(evidence, policy, scan_pages, visual, screenshot_page_url, mode=mode, scanned_at=scanned_at, scan_signals=signals))


_RESULT = {"earned": "Passed", "partial": "Partially Passed", "not_met": "Failed", "prerequisite": "Failed", "not_assessable": "Not Assessed", "not_applicable": "Not Applicable"}
_KIND = {"A": "dpdp", "B": "dpdp", "C": "dpdp", "D": "dpdp", "E": "practice", "F": "technical", "G": "dpdp"}
_BASIS = {"earned": "Verified", "partial": "Partially verified", "not_met": "Not found in scanned pages", "prerequisite": "Not evidenced", "not_applicable": "Not applicable"}
_NOT_ASSESSED_BASIS = {"Restricted": "Crawling restricted", "Not tested": "Not tested", "Inconclusive": "Evidence unavailable"}


def _legal(statutory: list[dict]) -> str:
    """The provisions behind a check, grouped by instrument: "DPDP Act s.5(1)(iii), s.6(4); DPDP Rules r.3(c)(i), r.3(c)(iii)"."""
    groups: dict[str, list[str]] = {}
    for s in statutory:
        instrument, _, ref = s["provision"].rpartition(" ")
        groups.setdefault(instrument, []).append(ref)

    return "; ".join(f"{k} {', '.join(v)}" for k, v in groups.items())


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def as_assessment(r: dict[str, Any]) -> dict[str, Any]:
    """The Scanner Score in the fields the dashboard, the report pages and the PDFs already read, plus the whole result under `scanner`.

    Parameters a Quick Scan cannot reach are not listed as checks: they are itemised in `scanner.beyond_quick_scan` (SC-13). Nothing is scored here; every number is the engine's.
    """
    quick = r["mode"] == "quick"
    # Every parameter is listed, in the report's table too: those a Quick Scan cannot reach are rows marked "Deep Scan only", never left out of sight (Scoring Mechanism SC-13). They carry no marks and
    # are not in the score; the explanation says what a Deep Scan assesses.
    def beyond(p: dict) -> bool:
        return quick and p["reach"] == "deep"

    # The parameters this scan type assesses come first, numbered 1 to N in the matrix's order; those a Quick Scan cannot reach follow, so the numbers in the table run without gaps.
    listed = [p for p in r["parameters"] if not beyond(p)] + [p for p in r["parameters"] if beyond(p)]
    checks = []
    for i, p in enumerate(listed, start=1):
        st = p["state"]
        text = p["found"] or p["reason"] or ""
        if st == "prerequisite" and p["blocked_by"]:
            text = f"{text} (depends on {_check_name(p['blocked_by'])})".strip()
        ev = p["evidence"]
        in_play = st in domain_score.IN_PLAY
        checks.append({
            "id": i, "ref": p["ref"], "name": f"{p['ref']} {p['name']}", "kind": _KIND[p["domain"]], "result": _RESULT[st],
            "awarded": p["awarded"] if in_play else None, "max": p["points"] if in_play else None,
            "marks_text": f"{p['awarded']} of {p['points']} pts" if in_play else ("Deep Scan only" if beyond(p) else "Not assessed" if st == "not_assessable" else "N/A"),
            "evidence": ev[0]["observed"] if ev else "", "source_url": next((e["page"] for e in ev if e.get("page")), None), "explanation": text,
            "legal": _legal(p["statutory"]) or domain_score.DOMAINS[p["domain"]]["anchor"], "completed": st != "not_assessable", "limitation": p["limitation"] or (p["reason"] if st == "not_assessable" else None),
            "basis": _NOT_ASSESSED_BASIS.get(p["outcome"], "Evidence unavailable") if st == "not_assessable" else _BASIS[st],
            "quality": {"earned": "Specific", "partial": "Limited", "not_assessable": "Not assessed", "not_applicable": "n/a"}.get(st, "None"), "coverage": None,
        })

    def count(k: str) -> int:
        return sum(1 for c in checks if c["result"] == k)

    n_beyond = sum(1 for p in listed if beyond(p))
    counts = {"passed": count("Passed"), "partial": count("Partially Passed"), "failed": count("Failed"), "not_assessed": count("Not Assessed") - n_beyond, "not_applicable": count("Not Applicable"),
              "beyond_scan_type": n_beyond}
    pts, conf, pg = r["points"], r["confidence"], r["pages"]
    head = (("No score is given: " + ("fewer than 60% of the points this scan can reach could be assessed" if r["coverage"] < 60 and r["points"]["in_play"] else "nothing could be assessed on the evidence available")
             + f". Band: {r['band']}. Coverage {r['coverage']}%. Evidence confidence: {conf['label']}. This is not a finding about the website.") if r["score"] is None
            else f"Band: {r['band']}. Coverage {r['coverage']}%. Evidence confidence: {conf['label']}.")
    limitations = []
    if pg["scanned"]:
        found = f" of {pg['discovered']} found" if pg["discovered"] is not None else ""
        limitations.append(f"{pg['scanned']} page{_plural(pg['scanned'], '', 's')} of the entered domain {_plural(pg['scanned'], 'was', 'were')} read{found}; the text of pages other than the privacy notice is not kept.")
    if pg["failed"]:
        limitations.append(f"{pg['failed']} page{_plural(pg['failed'], '', 's')} did not load (see the page list).")
    if pg["skipped"]:
        limitations.append(f"{pg['skipped']} page{' was' if pg['skipped'] == 1 else 's were'} found but not read.")
    by: dict[str, list[str]] = {}
    for p in listed:
        if p["state"] == "not_assessable" and not beyond(p):
            by.setdefault(p["reason"], []).append(p["ref"])
    for reason, refs in by.items():
        limitations.append(f"Parameter{_plural(len(refs), '', 's')} {', '.join(refs)} could not be assessed (left out of the score): {reason}")
    # The note about the parameters a Quick Scan cannot reach is the table's own row; the other band notes (why a band was lowered or a score held at 99) stay.
    limitations.extend(n for n in r["band_notes"] if not n.startswith("A Quick Scan assesses"))
    # A recommendation names the check it follows from, by the number in the checks table (RP-15): never its place in the priority list.
    number = {c["ref"]: c["id"] for c in checks}
    recs = [{"id": number.get(f["ref"], 0), "text": f["action"]} for f in r["priority_actions"] if f["ref"] in number]
    if any(p["state"] == "not_assessable" and not beyond(p) for p in listed):
        recs.append({"id": 0, "text": "Run the scan again when the pages and states it could not assess are reachable, so the parameters left out can be assessed."})
    if n_beyond:
        recs.append({"id": 0, "text": f"Run a Deep Scan to assess the {n_beyond} parameters a Quick Scan cannot reach."})
    return {
        "checks": checks, "awarded": pts["awarded"], "applicable_max": pts["in_play"], "score": r["score"], "counts": counts, "formula": r["explanation"],
        "total_text": "No applicable check" if pts["in_play"] == 0 else r["score_text"], "score_text": r["score_text"], "incomplete": counts["not_assessed"],
        "limited_basis": r["band"] == domain_score.INSUFFICIENT, "summary": f"{head} {r['explanation']}", "recommendations": recs,
        "pages": pg, "limitations": limitations, "engine": "scanner", "scanner": r,
    }


_SC_RESULT = {"pass": "Passed", "partial": "Partially Passed", "fail": "Failed", "not_assessed": "Not Assessed", "not_applicable": "Not Applicable"}
_SC_BASIS = {"pass": "Verified", "partial": "Partially verified", "fail": "Not found in scanned pages", "not_applicable": "Not applicable", "not_assessed": "Not tested"}


def as_scorecard(r: dict[str, Any]) -> dict[str, Any]:
    """The scorecard in the fields the dashboard, the report pages and the PDFs already read: ten rows (P1 to P10) as the checks, with the whole result, its three categories and the 24 detailed checks under `scanner`."""
    checks = []
    for i, x in enumerate(r["rows"], start=1):
        o = x["outcome"]
        in_play = o in scorecard.IN_PLAY
        text = x["found"] or x["reason"] or ""
        if x["blocked_by"]:
            text = f"{text} (depends on {_check_name(x['blocked_by'])})".strip()
        ev = x["evidence"]
        checks.append({
            "id": i, "ref": x["id"], "name": x["name"], "category": x["category"], "kind": "dpdp" if x["category"] != "Forms & Security" else "technical", "result": _SC_RESULT[o],
            "awarded": x["awarded"] if in_play else None, "max": x["marks"] if in_play else None,
            "marks_text": f"{x['awarded']} of {x['marks']} pts" if in_play else ("Not assessed" if o == "not_assessed" else "N/A"),
            "evidence": ev[0]["observed"] if ev else "", "source_url": next((e["page"] for e in ev if e.get("page")), None), "explanation": text,
            "legal": _legal(x["statutory"]), "completed": o != "not_assessed", "limitation": x["limitation"] or (x["reason"] if o == "not_assessed" else None),
            "basis": _SC_BASIS[o], "quality": {"pass": "Specific", "partial": "Limited", "not_assessed": "Not assessed", "not_applicable": "n/a"}.get(o, "None"), "coverage": None,
        })

    def count(k: str) -> int:
        return sum(1 for c in checks if c["result"] == k)

    counts = {"passed": count("Passed"), "partial": count("Partially Passed"), "failed": count("Failed"), "not_assessed": count("Not Assessed"), "not_applicable": count("Not Applicable"), "beyond_scan_type": 0}
    pts, conf, pg = r["points"], r["confidence"], r["pages"]
    cats = "; ".join(f"{c['category']} {c['awarded']} of {c['assessable']}" for c in r["categories"])
    head = (("No score is given: fewer than 60% of the marks could be assessed" + f". Band: {r['band']}. Coverage {r['coverage']}%. Evidence confidence: {conf['label']}. This is not a finding about the website.")
            if r["score"] is None else f"Band: {r['band']}. Coverage {r['coverage']}%. Evidence confidence: {conf['label']}. {cats}.")
    limitations = []
    if pg["scanned"]:
        found = f" of {pg['discovered']} found" if pg["discovered"] is not None else ""
        limitations.append(f"{pg['scanned']} page{_plural(pg['scanned'], '', 's')} of the entered domain {_plural(pg['scanned'], 'was', 'were')} read{found}; the text of pages other than the privacy notice is not kept.")
    if pg["failed"]:
        limitations.append(f"{pg['failed']} page{_plural(pg['failed'], '', 's')} did not load (see the page list).")
    if pg["skipped"]:
        limitations.append(f"{pg['skipped']} page{' was' if pg['skipped'] == 1 else 's were'} found but not read.")
    by: dict[str, list[str]] = {}
    for x in r["rows"]:
        if x["outcome"] == "not_assessed":
            by.setdefault(x["reason"], []).append(x["id"][1:])      # the number in the checks table, never the internal P-code
    for reason, ids in by.items():
        limitations.append(f"Check{_plural(len(ids), '', 's')} {', '.join(ids)} could not be assessed (left out of the score): {reason}")
    limitations.extend(r["band_notes"])
    number = {c["ref"]: c["id"] for c in checks}
    recs = [{"id": number.get(f["ref"], 0), "text": f["action"]} for f in r["priority_actions"]]
    if by:
        recs.append({"id": 0, "text": "Run the scan again when the pages and states it could not assess are reachable, so the rows left out can be assessed."})
    return {
        "checks": checks, "awarded": pts["awarded"], "applicable_max": pts["in_play"], "score": r["score"], "counts": counts, "formula": r["explanation"],
        "total_text": "No applicable check" if pts["in_play"] == 0 else r["score_text"], "score_text": r["score_text"], "incomplete": counts["not_assessed"],
        "limited_basis": r["band"] == domain_score.INSUFFICIENT, "summary": f"{head} {r['explanation']}", "recommendations": recs,
        "pages": pg, "limitations": limitations, "engine": "scorecard", "scanner": r,
    }
