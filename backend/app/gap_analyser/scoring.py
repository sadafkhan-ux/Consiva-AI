"""Deterministic consent-gap score and summary.

Both are pure functions of the observations -- no LLM, no randomness, no clock. These
are the numbers quoted to a prospect, so the same page must always produce the same
score, and every point must be traceable to a specific observation.
"""

from __future__ import annotations

# Per-tracker weights. Advertising and session-replay count roughly double plain
# analytics: cross-site identification and behaviour recording before consent are
# materially more serious than page-view counting.
_CATEGORY_WEIGHTS: dict[str, float] = {
    "advertising": 2.0,
    "session_replay": 2.0,
    "analytics": 1.0,
    "third_party": 1.0,
    "functional": 0.0,
}

_PER_THIRD_PARTY_DOMAIN = 2.0
_PER_LONG_LIVED_TRACKER = 1.5

_NO_BANNER = 25.0
_NO_PRIVACY_POLICY = 15.0
_SESSION_REPLAY_PRESENT = 10.0
_ADVERTISING_PRESENT = 5.0
_FORM_WITHOUT_CONSENT = 25.0

MAX_SCORE = 100


def score_consent_gap(
    *,
    has_cmp: bool,
    has_cookie_banner: bool,
    has_privacy_policy: bool,
    cookies: list[dict],
    third_party_count: int,
    long_lived_count: int,
    has_data_form: bool,
    form_consent_checkbox: bool,
) -> int:
    """0-100. A site with a real CMP scores 0 -- it has a consent mechanism, which is
    the thing being measured; individual configuration quality is a separate question
    this analyser deliberately does not judge."""
    if has_cmp:
        return 0

    categories = [c["category"] for c in cookies]
    score = sum(_CATEGORY_WEIGHTS.get(c, 0.0) for c in categories)
    score += third_party_count * _PER_THIRD_PARTY_DOMAIN
    score += long_lived_count * _PER_LONG_LIVED_TRACKER

    if not has_cookie_banner:
        score += _NO_BANNER
    if not has_privacy_policy:
        score += _NO_PRIVACY_POLICY
    if "session_replay" in categories:
        score += _SESSION_REPLAY_PRESENT
    if "advertising" in categories:
        score += _ADVERTISING_PRESENT
    if has_data_form and not form_consent_checkbox:
        score += _FORM_WITHOUT_CONSENT

    return min(MAX_SCORE, round(score))


def build_summary(result: dict) -> str:
    """Assembled with string formatting, never a model.

    Leads with third-party recipients and cookie lifetimes -- both are far more
    concrete to a reader than a raw cookie count."""
    if result.get("has_cmp"):
        return (
            f"Consent management platform detected ({result['cmp_name']}); "
            "pre-consent tracking is being managed by a recognised CMP."
        )

    parts: list[str] = []

    tp_domains = result.get("third_party_domains") or []
    if tp_domains:
        shown = ", ".join(tp_domains[:4])
        more = f" and {len(tp_domains) - 4} more" if len(tp_domains) > 4 else ""
        parts.append(
            f"Personal data is shared with {len(tp_domains)} third-party "
            f"{'domain' if len(tp_domains) == 1 else 'domains'} before any consent "
            f"({shown}{more})"
        )

    # Tracker-specific, not all-cookie: calling a long-lived functional cookie a
    # "tracker" is false, and produced a self-contradicting summary on a real site
    # ("longest-lived tracker persists 364 days" next to "0 of 2 cookies are trackers").
    max_life = result.get("max_tracker_lifetime_days")
    long_lived = result.get("long_lived_tracker_count") or 0
    if max_life:
        clause = f"the longest-lived tracker persists for {max_life} days"
        if long_lived > 1:
            clause += f", with {long_lived} lasting beyond 180 days"
        parts.append(clause)

    tracking = result.get("tracking_cookie_count") or 0
    total = result.get("cookies_total") or 0
    if total:
        parts.append(f"{tracking} of {total} cookies set pre-consent are trackers")

    if not result.get("has_cookie_banner"):
        parts.append("no cookie banner or consent management platform was detected")
    if not result.get("has_privacy_policy"):
        parts.append("no privacy policy link was found")

    if result.get("has_data_form") and not result.get("form_consent_checkbox"):
        fields = ", ".join(result.get("form_field_types") or [])
        parts.append(f"a form collects personal data ({fields}) with no consent checkbox")

    hosts = result.get("form_third_party_hosts") or []
    if hosts:
        parts.append(f"form submissions are sent to an external host ({', '.join(hosts)})")

    if not parts:
        return "No pre-consent tracking or data-collection gaps were detected on this page."

    summary = parts[0][0].upper() + parts[0][1:]
    if len(parts) > 1:
        summary += "; " + "; ".join(parts[1:])
    return summary + "."
