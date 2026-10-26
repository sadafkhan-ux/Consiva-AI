"""What a scan may say about a consent banner, decided in one place.

A consent tool's script on a page is not a banner on screen. Many tools show nothing to a visitor outside the regions their site targets, or behind a gate. When the scanner never saw the
banner (no Accept or Reject wording on a control) and neither control was clicked, then what the banner "offers" (a reject-all button, per-purpose choices) was not observed: it is unknown, not
false and not true. Every consumer reads this one rule: the consent rules, the DPDP score, the public API, the evidence API, and so the report.

`banner_not_displayed` is the single test. `observed_signal` applies it to a stored consent-signal row so that a scan stored before the detector learned this (it recorded "no reject-all" for a
footer cookie link) is read the same way as one stored after.
"""

from __future__ import annotations

from typing import Any


def banner_not_displayed(mechanism_type: str | None, interaction_evidence: dict | None) -> bool:
    """True for a consent tool (not a keyword-matched banner) whose banner was never observed and whose Accept and Reject were never clicked. Missing observation data (a scan from before it was
    recorded) is unknown, not "not displayed"."""
    ev = interaction_evidence or {}
    return (
        mechanism_type == "cmp" and ev.get("banner_ui_observed") is False
        and ev.get("accept_interaction") != "clicked" and ev.get("reject_interaction") != "clicked"
    )


def banner_displayed(mechanism_type: str | None, interaction_evidence: dict | None) -> bool | None:
    """False when the banner is known not to have been displayed, True when it is known to have been (seen, or a click on it), else None (unknown, or no consent tool)."""
    ev = interaction_evidence or {}
    if banner_not_displayed(mechanism_type, ev):
        return False
    if mechanism_type == "cmp" and (ev.get("banner_ui_observed") is True or ev.get("accept_interaction") == "clicked" or ev.get("reject_interaction") == "clicked"):
        return True
    return None


def cmp_script_failure(interaction_evidence: dict | None) -> dict | None:
    """The first consent-tool script that failed to load during the scan ({"url", "error"}), or None. Seen live: api.consiva.ai/sdk.js failed with ERR_CERT_DATE_INVALID on four sites,
    so the tool never ran and no banner ever appeared, which the report described only as "no banner was shown to the scanner"."""
    failure = (interaction_evidence or {}).get("cmp_script_failure")
    if isinstance(failure, dict) and failure.get("url"):
        return {"url": str(failure["url"]), "error": str(failure.get("error") or "the request failed")}
    return None


def script_failure_note(interaction_evidence: dict | None) -> str:
    """One sentence saying why no banner appeared when the tool's own script did not load, or ''. It says what was observed, and what follows if visitors' browsers behave the same."""
    failure = cmp_script_failure(interaction_evidence)
    if not failure:
        return ""
    return (f" The consent tool's script ({failure['url']}) could not be loaded ({failure['error']}), which is why no banner appeared. A visitor's browser that fails the same way "
            "would show no banner either; check the tool's host (an expired certificate, a blocked or missing file) before re-scanning.")


def observed_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """A copy of a consent-signal row with what a banner offers cleared when no banner was displayed, and `banner_displayed` added. Everything else is untouched."""
    out = dict(signal)
    ev = signal.get("evidence") or {}
    mechanism = signal.get("mechanism_type")
    if banner_not_displayed(mechanism, ev):
        out["has_reject_all"] = None
        out["has_granular_choices"] = None
    out["banner_displayed"] = banner_displayed(mechanism, ev)
    return out
