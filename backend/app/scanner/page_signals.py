"""Small, read-only observations about the pages a scan loads, for two report sections: language support and observable security signals.

Both are OBSERVATIONS, not findings. They are what the scanner saw on the pages it loaded and nothing more:
  * language: whether the pages carry text in the scripts of India's scheduled languages, what the pages declare (<html lang>, hreflang
    alternates), and whether a link or button is written in such a script (a language switcher). DPDP Act s.5(3) gives a person the option to
    read the notice in English or any Eighth Schedule language; whether the notice itself is offered that way is not something the pages' text
    can prove, so the report calls an absence "not observed", never "not supported".
  * security: the transport (https) of the scanned pages and the response headers of the homepage that browsers use as safeguards. DPDP Act
    s.8(5) and Rule 6 require reasonable security safeguards; what a website shows from outside is a few signals, not the safeguards.
Nothing here changes the score. Everything is bounded and pure so it can be tested without a browser.
"""

from __future__ import annotations

# The scripts of the Eighth Schedule languages that have their own script. (Urdu, Kashmiri and Sindhi share the Arabic script with Arabic itself,
# so it is not counted: a count of Arabic-script text would say nothing about Indian-language support.)
SCRIPT_RANGES: list[tuple[str, int, int]] = [
    ("Devanagari (Hindi, Marathi, Sanskrit, Nepali, Konkani, Maithili, Dogri, Bodo)", 0x0900, 0x097F),
    ("Bengali / Assamese", 0x0980, 0x09FF),
    ("Gurmukhi (Punjabi)", 0x0A00, 0x0A7F),
    ("Gujarati", 0x0A80, 0x0AFF),
    ("Odia", 0x0B00, 0x0B7F),
    ("Tamil", 0x0B80, 0x0BFF),
    ("Telugu", 0x0C00, 0x0C7F),
    ("Kannada", 0x0C80, 0x0CFF),
    ("Malayalam", 0x0D00, 0x0D7F),
    ("Ol Chiki (Santali)", 0x1C50, 0x1C7F),
]

#: The most hreflang values or scripts kept, so a page with a long list cannot grow the scan record.
MAX_HREFLANG = 25


def indic_script_counts(text: str | None) -> dict[str, int]:
    """Characters of each Indian script in `text`: {} when there are none."""
    counts: dict[str, int] = {}
    if not text:
        return counts
    for ch in text:
        o = ord(ch)
        if o < 0x0900:
            continue
        for name, lo, hi in SCRIPT_RANGES:
            if lo <= o <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    return counts


_SECURITY_HEADERS = {
    "strict-transport-security": "hsts",
    "content-security-policy": "csp",
    "x-frame-options": "x_frame_options",
    "x-content-type-options": "x_content_type_options",
    "referrer-policy": "referrer_policy",
    "permissions-policy": "permissions_policy",
}


def security_headers(headers: dict | None) -> dict[str, str | None]:
    """The browser safeguard headers of a response: each one's value (cut to 300 characters), or None when it was not sent."""
    low = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    return {label: (low[name][:300] if name in low else None) for name, label in _SECURITY_HEADERS.items()}


def response_headers(response) -> dict:
    """The headers of a Playwright response as a plain dict, or {} when they cannot be read (never raises: a signal must not fail a scan)."""
    try:
        h = getattr(response, "headers", None)
        return dict(h) if isinstance(h, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def merge_language(acc: dict, *, indic: dict[str, int], control_indic: dict[str, int], html_lang: str | None, hreflang: list[str], is_seed: bool) -> dict:
    """Folds one page's language observations into the scan-wide record `acc` (created when empty) and returns it."""
    acc.setdefault("pages_checked", 0)
    acc.setdefault("pages_with_indic_script", 0)
    acc.setdefault("indic_scripts", {})
    acc.setdefault("switcher_scripts", [])
    acc.setdefault("hreflang", [])
    acc.setdefault("html_lang", None)
    acc["pages_checked"] += 1
    if indic:
        acc["pages_with_indic_script"] += 1
        for k, v in indic.items():
            acc["indic_scripts"][k] = acc["indic_scripts"].get(k, 0) + v
    for k in control_indic:
        if k not in acc["switcher_scripts"]:
            acc["switcher_scripts"].append(k)
    for h in hreflang:
        if h and h not in acc["hreflang"] and len(acc["hreflang"]) < MAX_HREFLANG:
            acc["hreflang"].append(h)
    if is_seed and html_lang:
        acc["html_lang"] = html_lang[:20]
    return acc


def merge_security(acc: dict, *, final_url: str | None, headers: dict | None, is_seed: bool) -> dict:
    """Folds one page into the scan-wide security record: how many scanned pages were served over https, and (for the homepage) the safeguard headers."""
    acc.setdefault("pages_total", 0)
    acc.setdefault("pages_https", 0)
    acc["pages_total"] += 1
    if (final_url or "").lower().startswith("https://"):
        acc["pages_https"] += 1
    if is_seed:
        acc["homepage_https"] = (final_url or "").lower().startswith("https://")
        acc["homepage_headers"] = security_headers(headers)
    return acc


# ── what the pages say about the notice link, children and forms (read-only observations; they feed Scanner Score parameters A5, D1, D2 and G1) ─────────────────────────────

import re  # noqa: E402

from urllib.parse import urlsplit  # noqa: E402

_PRIVACY_LINK = re.compile(r"privacy|data[-\s]?protection|dpdp", re.IGNORECASE)
MAX_LISTED = 20


def links_to_privacy_notice(links) -> bool:
    """Whether a page links to a privacy notice: a link whose text, address or class names one."""
    return any(_PRIVACY_LINK.search(f"{l.text} {l.href} {l.attrs}") for l in links)


def merge_notice_links(acc: dict, *, page_url: str, has_link: bool) -> dict:
    """Folds one page into the scan-wide record of how many pages read link to a privacy notice, and which do not."""
    acc.setdefault("pages", 0)
    acc.setdefault("with_link", 0)
    acc.setdefault("missing", [])
    acc["pages"] += 1
    if has_link:
        acc["with_link"] += 1
    elif len(acc["missing"]) < MAX_LISTED:
        acc["missing"].append(page_url[:300])
    return acc


# The objective test for a child-directed surface. It looks only at what a page says about itself, never at who visits it, and is deliberately narrow: a privacy notice that says
# "we do not knowingly collect data from children" is not a page for children.
_CHILD_WORDS = r"kids?|children|child|juniors?|teens?|toddlers?|preschool|nursery|k-?12|youngsters"
_CHILD_URL = re.compile(rf"(^|[/_\-.])({_CHILD_WORDS})([/_\-.]|$)", re.IGNORECASE)
_CHILD_TITLE = re.compile(rf"\b({_CHILD_WORDS})\b", re.IGNORECASE)
_CHILD_TEXT = re.compile(r"\b(for kids|for children|kids'? (zone|corner|club|area|section|program(me)?)|children'?s? (zone|corner|club|program(me)?|section)|children aged|kids aged|ages? \d{1,2}\s?(-|to|–)\s?\d{1,2}|(age|aged) (group )?under (13|14|16|18))\b", re.IGNORECASE)
_NOT_A_CHILD_PAGE = re.compile(r"privacy|policy|terms|cookie|legal|grievance|disclaimer", re.IGNORECASE)


def child_directed_signal(url: str, title: str | None, text: str | None) -> str | None:
    """Why a page is taken to be aimed at children, in the page's own words, or None. Policy and legal pages are never taken as child-directed."""
    path = urlsplit(url).path or ""
    if _NOT_A_CHILD_PAGE.search(path) or _NOT_A_CHILD_PAGE.search(title or ""):
        return None
    if _CHILD_URL.search(path):
        return f"address contains “{_CHILD_URL.search(path).group(2).lower()}”"
    if title and _CHILD_TITLE.search(title):
        return f"title contains “{_CHILD_TITLE.search(title).group(1).lower()}”"
    if text and (m := _CHILD_TEXT.search(text)):
        return f"text says “{m.group(0).lower()}”"
    return None


def merge_child_surface(acc: list, *, page_url: str, signal: str | None) -> list:
    if signal and len(acc) < MAX_LISTED:
        acc.append({"url": page_url[:300], "signal": signal[:200]})
    return acc


# ── what the pages show about the banner, browser storage and insecure content (read-only observations; they feed Scorecard rows P1, P3 and P10) ─────────────────────────────

#: Keys that analytics and advertising tools write to browser storage. Only the names are ever kept (never the values, which routinely hold personal data: Functional Scope QS-18, GR-16).
_TRACKING_KEY = re.compile(r"^(_ga|_gid|_gcl|_fbp|_fbc|ajs_|amplitude|mixpanel|mp_|hjsession|hjuser|_hj|_clck|_clsk|_uet|intercom|segment|optimizely|fs_uid|__hs|_pk_|_vwo|heap|_tt_|_scid|li_|_pin_)", re.IGNORECASE)
MAX_KEYS = 40


def tracking_storage_keys(keys: list[str]) -> list[str]:
    """The storage key names that look like an analytics or advertising identifier."""
    return [k for k in keys if _TRACKING_KEY.search(k)]


def merge_storage_keys(acc: dict, storage: object) -> dict:
    """Folds one page's browser-storage key names into the scan-wide record. A page whose storage could not be read adds nothing (it is not counted as clean)."""
    if not isinstance(storage, dict):
        return acc
    names = [str(k)[:80] for k in [*(storage.get("local") or []), *(storage.get("session") or [])]]
    acc.setdefault("pages_checked", 0)
    acc.setdefault("keys", [])
    acc.setdefault("pre_consent_tracking_keys", [])
    acc["pages_checked"] += 1
    for k in names:
        if k not in acc["keys"] and len(acc["keys"]) < MAX_KEYS:
            acc["keys"].append(k)
    for k in tracking_storage_keys(names):
        if k not in acc["pre_consent_tracking_keys"] and len(acc["pre_consent_tracking_keys"]) < MAX_KEYS:
            acc["pre_consent_tracking_keys"].append(k)
    return acc


def merge_mixed_content(acc: dict, *, page_url: str, has_form: bool, insecure: list[str]) -> dict:
    """Pages that collect details (a form) and load something over plain http while being served over https."""
    acc.setdefault("pages_checked", 0)
    acc.setdefault("pages_with_insecure_content", [])
    acc["pages_checked"] += 1
    if has_form and insecure and len(acc["pages_with_insecure_content"]) < MAX_LISTED:
        acc["pages_with_insecure_content"].append(page_url[:300])
    return acc


def banner_on_page(*, scripts, visible_text: str, global_vars: list[str], control_text: str | None) -> bool:
    """Whether a consent banner or consent platform is present on this page, by the same detector the scan uses overall."""
    from app.scanner.consent_signal_detector import detect_consent_signal

    return detect_consent_signal(scripts=scripts, visible_text=visible_text, detected_global_vars=global_vars, control_text=control_text).mechanism_type in ("banner", "cmp")
