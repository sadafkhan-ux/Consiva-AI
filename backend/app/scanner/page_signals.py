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
