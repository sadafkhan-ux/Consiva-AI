"""Pure detection functions for the consent-gap analyser.

Every function here takes already-captured page data (HTML, script srcs, raw cookies,
form descriptors) and returns a finding. No I/O, no browser, no LLM -- which is what
makes the compliance output reproducible and unit-testable without a network.
"""

from __future__ import annotations

import time
from urllib.parse import urljoin, urlparse

from app.gap_analyser import tables as T
from app.scanner._domain import registered_domain


# --------------------------------------------------------------------------------
# CMP / banner / privacy policy
# --------------------------------------------------------------------------------
def detect_cmp(html: str, script_srcs: list[str]) -> tuple[bool, str | None]:
    """First matching vendor wins. Searches the page HTML and every <script src>."""
    haystack = (html + " " + " ".join(script_srcs)).lower()
    for vendor, needles in T.CMP_SIGNATURES:
        if any(n in haystack for n in needles):
            return True, vendor
    return False, None


def detect_cookie_banner(html: str, visible_text: str) -> bool:
    """A banner with no recognised vendor still counts -- it typically informs the
    visitor while setting trackers regardless."""
    lowered = html.lower()
    if any(hint in lowered for hint in T.BANNER_SELECTOR_HINTS):
        return True
    return any(p.search(visible_text) for p in T.BANNER_TEXT_PATTERNS)


def detect_privacy_policy(links: list[dict]) -> bool:
    """`links`: [{"href": str, "text": str}, ...].

    Three ways to qualify, because "Privacy Policy" is only the most formal spelling:
    the combined href+text phrase, a whole `/privacy/`-style URL path segment, or link
    text that is exactly the word. The latter two exist because a bare "Privacy" link
    (wordpress.org's only one) matched nothing and cost the site 15 score points."""
    for link in links:
        href = link.get("href") or ""
        text = link.get("text") or ""
        if T.PRIVACY_LINK_PATTERN.search(f"{href} {text}"):
            return True
        if T.PRIVACY_HREF_PATH_PATTERN.search(href):
            return True
        if T.PRIVACY_EXACT_TEXT_PATTERN.match(text):
            return True
    return False


# --------------------------------------------------------------------------------
# Cookies
# --------------------------------------------------------------------------------
def _prefix_match(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name.startswith(p) for p in prefixes)


def classify_cookie(name: str, party: str) -> str:
    """Order matters: the most serious category a cookie qualifies for wins.

    The final `third_party` branch is the important one -- any third-party cookie is a
    tracker even when its name is unrecognised. Name lists always lag reality (CLID,
    SRM_B and ANONCHK were a third of one real site's trackers and matched nothing)."""
    if _prefix_match(name, T.ADVERTISING_PREFIXES):
        return "advertising"
    if _prefix_match(name, T.SESSION_REPLAY_PREFIXES):
        return "session_replay"
    if _prefix_match(name, T.ALL_TRACKER_PREFIXES):
        return "analytics"
    if party == "third":
        return "third_party"
    return "functional"


def build_cookie_record(raw: dict, final_domain: str, now: float | None = None) -> dict:
    """Normalise one Playwright cookie into the output record.

    The cookie VALUE is dropped here, at capture, and never enters the record: values
    are live tracking identifiers for real visitors and are arguably personal data
    under DPDP. A compliance product must not accumulate other people's tracking IDs.
    """
    now = time.time() if now is None else now
    cookie_domain = (raw.get("domain") or "").lstrip(".")
    party = "first" if registered_domain(cookie_domain) == final_domain else "third"

    expires = raw.get("expires", -1)
    # Playwright uses -1 (and sometimes 0) for a session cookie.
    session = expires is None or expires <= 0
    lifetime_days = None if session else max(0, int((expires - now) // 86400))

    name = raw.get("name") or ""
    return {
        "name": name,
        "domain": cookie_domain,
        "party": party,
        "session": session,
        "lifetime_days": lifetime_days,
        "secure": bool(raw.get("secure")),
        "same_site": raw.get("sameSite"),
        "http_only": bool(raw.get("httpOnly")),
        "category": classify_cookie(name, party),
    }


def summarise_cookies(records: list[dict]) -> dict:
    trackers = [c for c in records if c["category"] != "functional"]
    third_party_domains = sorted({c["domain"] for c in records if c["party"] == "third"})
    lifetimes = [c["lifetime_days"] for c in records if c["lifetime_days"] is not None]
    return {
        "cookies_total": len(records),
        "tracking_cookie_count": len(trackers),
        "third_party_count": len(third_party_domains),
        "third_party_domains": third_party_domains,
        "max_cookie_lifetime_days": max(lifetimes) if lifetimes else None,
        "long_lived_count": sum(1 for d in lifetimes if d > T.LONG_LIVED_DAYS),
    }


def tracker_lifetime_stats(records: list[dict]) -> tuple[int | None, int]:
    """Longest lifetime and >180-day count across TRACKERS only.

    Separate from summarise_cookies (which reports over all cookies) because the score
    weights "trackers living beyond 180 days" specifically, and because describing a
    long-lived functional cookie as a long-lived tracker is simply false -- a real site
    with two cookies, neither a tracker, was otherwise summarised as having a tracker
    persisting 364 days."""
    lifetimes = [
        c["lifetime_days"] for c in records
        if c["category"] != "functional" and c["lifetime_days"] is not None
    ]
    if not lifetimes:
        return None, 0
    return max(lifetimes), sum(1 for d in lifetimes if d > T.LONG_LIVED_DAYS)


# --------------------------------------------------------------------------------
# Forms
# --------------------------------------------------------------------------------
def classify_field(descriptor: str) -> set[str]:
    """Classify one field from its combined descriptor string.

    Handles the email/address overlap explicitly: "Email Address" matches both
    patterns, and without discarding `address` on an email hit every contact form
    falsely reports collecting postal addresses."""
    matched = {
        category for category, patterns in T.FIELD_PATTERNS.items()
        if any(p.search(descriptor) for p in patterns)
    }
    if "email" in matched:
        matched.discard("address")
    return matched


def analyse_forms(forms: list[dict], page_url: str) -> dict:
    """`forms`: browser-extracted form data (see analyser._EXTRACT_JS).

    Only forms collecting an identifying field count as findings -- a bare message box
    is not personal data."""
    page_host = urlparse(page_url).netloc
    collecting: list[dict] = []
    all_field_types: set[str] = set()
    third_party_hosts: set[str] = set()

    for form in forms:
        field_types: set[str] = set()
        for descriptor in form.get("field_descriptors", []):
            field_types |= classify_field(descriptor)
        if not (field_types & T.IDENTIFYING_FIELDS):
            continue

        collecting.append(form)
        all_field_types |= field_types

        # Personal data posting to another host is a processor the company almost
        # certainly has not documented -- one of the strongest findings available.
        action = form.get("action") or ""
        if action:
            action_host = urlparse(urljoin(page_url, action)).netloc
            if action_host and action_host != page_host:
                third_party_hosts.add(action_host)

    return {
        "has_data_form": bool(collecting),
        "form_field_types": sorted(all_field_types),
        # True only if EVERY collecting form has one -- one unprotected form is a gap.
        "form_consent_checkbox": bool(collecting) and all(f.get("has_consent_checkbox") for f in collecting),
        "form_privacy_link": bool(collecting) and any(f.get("has_privacy_link") for f in collecting),
        "form_third_party_hosts": sorted(third_party_hosts),
    }


def detect_form_provider(html: str, script_srcs: list[str]) -> str | None:
    haystack = (html + " " + " ".join(script_srcs)).lower()
    for provider, needles in T.FORM_PROVIDERS:
        if any(n in haystack for n in needles):
            return provider
    return None


# --------------------------------------------------------------------------------
# Tech stack
# --------------------------------------------------------------------------------
def _first_match(html_lower: str, signatures) -> str | None:
    for label, needles in signatures:
        if any(n in html_lower for n in needles):
            return label
    return None


def _all_matches(html_lower: str, signatures) -> list[str]:
    return [label for label, needles in signatures if any(n in html_lower for n in needles)]


def detect_tech(html: str, script_srcs: list[str]) -> dict:
    raw = html + " " + " ".join(script_srcs)
    lowered = raw.lower()

    analytics = _all_matches(lowered, T.ANALYTICS_SIGNATURES)
    # GA4 is identified by a case-sensitive measurement id, not a lowercase substring.
    if T.GA4_ID_PATTERN.search(raw) and "Google Analytics 4" not in analytics:
        analytics.insert(0, "Google Analytics 4")
    if T.GTM_ID_PATTERN.search(raw) and "Google Tag Manager" not in analytics:
        analytics.append("Google Tag Manager")

    return {
        "tech_cms": _first_match(lowered, T.CMS_SIGNATURES),
        "tech_ecommerce_platform": _first_match(lowered, T.ECOMMERCE_SIGNATURES),
        "tech_payment_gateway": _first_match(lowered, T.PAYMENT_SIGNATURES),
        "tech_analytics_tools": analytics,
        "tech_marketing_tools": _all_matches(lowered, T.MARKETING_SIGNATURES),
    }


# --------------------------------------------------------------------------------
# Page status
# --------------------------------------------------------------------------------
def detect_page_status(
    *, title: str, body_text: str, http_status: int | None, final_domain: str
) -> str:
    """Returns "ok" | "parked" | "blocked". "unreachable" is decided by the caller,
    which is the only layer that knows a navigation failed outright.

    Parked is checked FIRST and deliberately: dead stores commonly answer 402/503, and
    treating those as "blocked" means retrying them forever."""
    haystack = f"{title}\n{body_text}".lower()

    if any(m in haystack for m in T.PARKED_MARKERS):
        return "parked"
    # A <title> that is just the hostname, or effectively no page at all.
    stripped_title = title.strip().lower()
    if stripped_title and stripped_title.rstrip("/") == final_domain.lower():
        return "parked"
    if not stripped_title and len(body_text.strip()) < 80:
        return "parked"

    if any(m in haystack for m in T.BLOCKED_MARKERS):
        return "blocked"
    if http_status in T.BLOCKED_STATUSES and len(body_text.strip()) < T.BLOCKED_BODY_MAX_CHARS:
        return "blocked"

    return "ok"
