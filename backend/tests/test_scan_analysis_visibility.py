"""A scan that has not been analysed must not look like a clean result.

Found by a live debug sweep while the LLM was unavailable. The self-hosted model
rejected every analysis prompt (6,462 tokens against a 4,096-token context) and the
NVIDIA fallback timed out, so the analysis never ran. The API reported:

    status: completed
    findings: []

Which is exactly what it reports for a site that was analysed and found compliant.
Four different situations collapsed into one clean-looking answer:

    1. analysed, nothing found   -> genuinely clean
    2. analysis still running    -> unknown
    3. analysis failed           -> unknown
    4. analysis never requested  -> unknown

For a compliance tool that is the most dangerous output there is, and it is the same
class of thing R-010 already refuses inside the rules engine ("incomplete, not a
confirmed absence of tracking"). The scan response now carries the analysis state
alongside the scan state.
"""

import inspect
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_the_scan_response_reports_analysis_state_separately():
    from app.api.v1.routes import consent_scans

    fields = consent_scans.ScanStatusResponse.model_fields
    assert "analysis_status" in fields, (
        "the scan response no longer says whether analysis ran; a scan that was never "
        "analysed is indistinguishable from one analysed and found clean"
    )
    assert "findings_complete" in fields


def test_findings_are_only_complete_once_analysis_reached_a_settled_state():
    """`paused` counts as complete: that is the human-review gate, and the findings are
    all present, waiting on a decision. Everything else -- never run, still running,
    failed -- means the list is not yet the answer."""
    body = inspect.getsource(
        __import__("app.api.v1.routes.consent_scans", fromlist=["get_scan"]).get_scan
    )
    assert "latest_agent_run" in body
    assert '"completed", "paused"' in body, (
        "the settled-state set changed; check that a still-running or failed analysis "
        "cannot report findings_complete=True"
    )


def test_a_scan_with_no_analysis_run_reports_none_not_a_default():
    """None means "never started". A default of, say, "pending" would be a claim about
    something that was never asked for."""
    from app.api.v1.routes import consent_scans

    field = consent_scans.ScanStatusResponse.model_fields["analysis_status"]
    assert field.default is None
    assert consent_scans.ScanStatusResponse.model_fields["findings_complete"].default is False, (
        "findings_complete defaults to True; a response that omits it would claim "
        "completeness it has not established"
    )


def test_the_repository_lookup_takes_the_most_recent_run():
    """Re-analysis creates a new run. Reading an older one would report a stale state
    for the findings currently on screen."""
    from app.db.repositories import scan_repository

    body = inspect.getsource(scan_repository.latest_agent_run)
    assert "started_at.desc()" in body
    assert "limit(1)" in body
