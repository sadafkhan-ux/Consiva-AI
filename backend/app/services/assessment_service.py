"""The ten-check assessment of one stored scan: gathers what the engine reads and runs it.

The engine (scoring/ten_checks.py) is a pure function; this module only assembles its inputs from what is stored for the scan (the evidence, the policy analysis, the website-scan
stage record and the homepage preview's address), the way the dashboard and the report pages used to assemble them in the browser. It stores nothing: like the weighted score
it is worked out on request, so the number always matches the evidence and the policy analysis as they are now.
"""

from __future__ import annotations

from typing import Any

from app.scoring.ten_checks import assess_ten_checks
from app.scoring.ten_inputs import build_scan_pages, build_visual

#: The version of the ten-check methodology the numbers come from. Raise it when a rule that can change a result changes.
TEN_CHECK_VERSION = "1.1.1"


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


def assess_parts(evidence: dict | None, policy_result: dict | None, meta: dict | None, screenshot_page_url: str | None) -> dict[str, Any]:
    """Runs the engine on already-loaded parts. `policy_result` is the stored policy analysis, or None when it has not run (an analysis recorded as not run counts as not run)."""
    policy = policy_result if policy_result and policy_result.get("status") != "not_run" else None
    pages = (evidence or {}).get("pages") if evidence is not None else None
    return assess_ten_checks(evidence, policy, build_scan_pages(pages, meta), build_visual(evidence, meta), screenshot_page_url)
