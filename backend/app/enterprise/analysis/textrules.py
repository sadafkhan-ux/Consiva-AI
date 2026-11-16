"""Small deterministic text readers used by the parameter rules: language, durations, readability, and whether a vendor, cookie or data item is named in a text.

None of this calls a model. Each is a heuristic with a stated limit; where a limit matters the rule that uses it says so and lowers its confidence.
"""

from __future__ import annotations

import re
import unicodedata

from app.scanner._domain import registered_domain

# ---- language ---------------------------------------------------------------------------------------------------------------------------------------------------------

#: Scripts used by the Eighth Schedule languages (Arabic script is ambiguous: Urdu, Kashmiri and Sindhi are listed, Arabic itself is not, so it is not counted without a declared option).
EIGHTH_SCHEDULE_SCRIPTS = {
    "DEVANAGARI": "Hindi, Marathi, Sanskrit, Nepali, Konkani, Maithili, Dogri or Bodo", "BENGALI": "Bengali, Assamese or Manipuri", "GURMUKHI": "Punjabi", "GUJARATI": "Gujarati",
    "ORIYA": "Odia", "TAMIL": "Tamil", "TELUGU": "Telugu", "KANNADA": "Kannada", "MALAYALAM": "Malayalam", "OL": "Santali", "MEETEI": "Manipuri"}
EIGHTH_SCHEDULE_CODES = frozenset({"as", "bn", "brx", "doi", "gu", "hi", "kn", "ks", "kok", "mai", "ml", "mni", "mr", "ne", "or", "pa", "sa", "sat", "sd", "ta", "te", "ur"})
EIGHTH_SCHEDULE_NAMES = (
    "assamese", "bengali", "bangla", "bodo", "dogri", "gujarati", "hindi", "kannada", "kashmiri", "konkani", "maithili", "malayalam", "manipuri", "meitei", "marathi", "nepali", "odia", "oriya",
    "punjabi", "sanskrit", "santali", "sindhi", "tamil", "telugu", "urdu")
_ENGLISH_WORDS = frozenset("the of and to in is are we you your our for that this with as or by on be it from at an may will can not any all if which when such other their about use used".split())
_WORD = re.compile(r"[A-Za-z]+")


def detect_language(text: str) -> dict:
    """{"language": "en" | "eighth_schedule" | "other", "script": str | None, "english_ratio": float}. English means Latin script with common English words making up a fifth of the words."""
    sample = (text or "")[:30000]
    scripts: dict[str, int] = {}
    latin = 0
    for ch in sample:
        if not ch.isalpha():
            continue
        if ch.isascii():
            latin += 1
            continue
        try:
            name = unicodedata.name(ch).split()[0]
        except ValueError:
            continue
        scripts[name] = scripts.get(name, 0) + 1
    # A rendered page carries menus, footers and button labels: lines of a word or two that hold almost none of the small words English prose is full of, and that would drag the ratio down.
    # The ratio is therefore taken over PROSE lines (eight or more words) when there are any, and over everything only when there are none.
    prose = "\n".join(line for line in sample.splitlines() if len(_WORD.findall(line)) >= 8)
    words = _WORD.findall(prose or sample)
    ratio = (sum(1 for w in words if w.lower() in _ENGLISH_WORDS) / len(words)) if words else 0.0
    total = latin + sum(scripts.values())
    if total == 0:
        return {"language": "other", "script": None, "english_ratio": 0.0}
    top = max(scripts.items(), key=lambda kv: kv[1], default=(None, 0))
    # Text that is almost all Latin script is English when the small words English prose is full of make up a twelfth of it. A policy page is mostly lists and tables (cookie names, vendors, durations),
    # which hold few of those words, so a fifth would call a plainly English policy foreign (swaransoft.com/privacy measures 12%). French or Spanish text measures about 3% on the same list.
    # Text that is less purely Latin keeps the stricter fifth.
    if (latin >= total * 0.9 and ratio >= 0.08) or (latin >= total * 0.5 and ratio >= 0.2):
        return {"language": "en", "script": "LATIN", "english_ratio": round(ratio, 3)}
    if top[0] in EIGHTH_SCHEDULE_SCRIPTS and top[1] >= total * 0.5:
        return {"language": "eighth_schedule", "script": top[0], "english_ratio": round(ratio, 3)}
    return {"language": "other", "script": top[0] if top[1] > latin else "LATIN", "english_ratio": round(ratio, 3)}


def option_languages(alternates: list[dict], page_lang: str | None) -> set[str]:
    """Language codes (primary subtag) a page declares: its <html lang> and its hreflang alternates."""
    codes = {str(a.get("lang", "")).lower().split("-")[0] for a in alternates or [] if a.get("lang")}
    if page_lang:
        codes.add(page_lang.lower().split("-")[0])
    codes.discard("x")
    return {c for c in codes if c}


def names_language_option(value: str) -> bool:
    v = (value or "").lower()
    return "english" in v or any(n in v for n in EIGHTH_SCHEDULE_NAMES)


# ---- durations --------------------------------------------------------------------------------------------------------------------------------------------------------

_NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
             "forty-five": 45, "fortyfive": 45, "sixty": 60, "ninety": 90, "a": 1, "an": 1}
_UNIT_DAYS = {"hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}
_DURATION = re.compile(r"(?<![A-Za-z0-9.])(?P<n>\d+(?:\.\d+)?|" + "|".join(sorted(_NUMWORDS, key=len, reverse=True)) + r")\s*(?:\(\s*\d+\s*\)\s*)?(?:[-\s](?:working|business|calendar|consecutive)\s+)?\s*(?P<u>hours?|days?|weeks?|months?|years?)\b", re.I)


def parse_days(text: str) -> list[float]:
    """Every period the text states, in days (hours /24, weeks 7, months 30, years 365). Working or business days count as days. "A fortnight" is 14. Nothing is guessed from words like
    "promptly" or "reasonable": those give no number."""
    out: list[float] = []
    for m in _DURATION.finditer(text or ""):
        raw = m.group("n").lower()
        n = float(raw) if raw[0].isdigit() else float(_NUMWORDS[raw])
        out.append(n * _UNIT_DAYS[m.group("u").lower().rstrip("s")])
    if re.search(r"\bfortnight\b", text or "", re.I):
        out.append(14.0)
    return out


# ---- readability ------------------------------------------------------------------------------------------------------------------------------------------------------

def _syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    n = len(re.findall(r"[aeiouy]+", w))
    if w.endswith("e") and not w.endswith(("le", "ee")) and n > 1:
        n -= 1
    return max(1, n)


def readability(text: str) -> dict:
    """Average sentence length, the share of long sentences, and Flesch reading ease, over the whole text. Meaningful for English only."""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n{2,}", text or "") if len(_WORD.findall(s)) >= 3]
    words = _WORD.findall(text or "")
    if not sentences or not words:
        return {"sentences": 0, "words": len(words), "avg_sentence_words": 0.0, "long_sentence_share": 0.0, "flesch": None}
    lens = [len(_WORD.findall(s)) for s in sentences]
    syl = sum(_syllables(w) for w in words)
    flesch = 206.835 - 1.015 * (len(words) / len(sentences)) - 84.6 * (syl / len(words))
    return {"sentences": len(sentences), "words": len(words), "avg_sentence_words": round(len(words) / len(sentences), 1),
            "long_sentence_share": round(sum(1 for n in lens if n > 35) / len(sentences), 3), "flesch": round(flesch, 1)}


# ---- who is named -----------------------------------------------------------------------------------------------------------------------------------------------------

_GENERIC = frozenset({"analytics", "cdn", "tag", "tags", "manager", "static", "media", "ads", "adservice", "track", "tracking", "pixel", "cloud", "content", "assets", "api", "www", "net", "web", "data", "service", "services", "global"})
_ALIASES = {
    "googletagmanager": ("google",), "google-analytics": ("google",), "googleadservices": ("google",), "googlesyndication": ("google",), "doubleclick": ("google", "doubleclick"),
    "gstatic": ("google",), "googleapis": ("google",), "youtube": ("youtube", "google"), "ytimg": ("youtube", "google"), "facebook": ("facebook", "meta"), "fbcdn": ("facebook", "meta"),
    "instagram": ("instagram", "meta"), "licdn": ("linkedin",), "clarity": ("clarity", "microsoft"), "bing": ("bing", "microsoft"), "twimg": ("twitter",), "ads-twitter": ("twitter",),
    "hs-scripts": ("hubspot",), "hsforms": ("hubspot",), "hs-analytics": ("hubspot",), "hsappstatic": ("hubspot",), "tiktokcdn": ("tiktok",), "snapchat": ("snap",)}


def vendor_tokens(vendor: str | None, host: str | None) -> set[str]:
    """Words that, appearing in a policy, mean the vendor is named: the vendor's own name, the registered-domain label, its distinctive parts, and well-known aliases (Google for
    googletagmanager.com, Meta for facebook.net)."""
    toks: set[str] = set()
    if vendor and len(vendor.strip()) >= 3:
        toks.add(vendor.strip().lower())
        toks.update(w for w in re.split(r"[^a-z0-9]+", vendor.lower()) if len(w) >= 4 and w not in _GENERIC)
    if host:
        reg = registered_domain(host) or host
        label = reg.split(".")[0].lower()
        if len(label) >= 3:
            toks.add(label)
            toks.update(p for p in label.split("-") if len(p) >= 4 and p not in _GENERIC)
        toks.update(_ALIASES.get(label, ()))
        sub = host.split(".")[0].lower()
        toks.update(_ALIASES.get(sub, ()))
    return {t for t in toks if t}


def names_any(text_lower: str, tokens: set[str]) -> bool:
    return any(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text_lower) for t in tokens)


def cookie_declared(name: str, domain: str, text_lower: str) -> bool:
    """A cookie is declared when the text gives its name (exactly, or its stable prefix for names such as _ga_ABC123), or names the service the cookie's domain belongs to."""
    n = (name or "").lower()
    if len(n) >= 3 and re.search(r"(?<![a-z0-9_])" + re.escape(n) + r"(?![a-z0-9])", text_lower):
        return True
    m = re.match(r"^(_[a-z]+)_[a-z0-9]{6,}$", n) or re.match(r"^([a-z]+_)[a-z0-9]{6,}$", n)
    if m and len(m.group(1)) >= 3 and m.group(1).lower() in text_lower:
        return True
    return bool(domain) and names_any(text_lower, vendor_tokens(None, domain)) and registered_domain(domain) != ""


# ---- what a form collects, in the words a policy uses ------------------------------------------------------------------------------------------------------------------

DATA_SYNONYMS: dict[str, tuple[str, ...]] = {
    "email": ("email", "e-mail"), "phone": ("phone", "mobile", "telephone", "contact number", "contact details"), "name": ("name",),
    "address": ("address", "postal", "location", "city", "pin code", "postcode"), "password": ("password", "credentials", "login", "log-in", "account details"),
    "date of birth": ("date of birth", "birth", "age", "dob"), "message": ("message", "enquir", "inquir", "comments", "feedback", "content you provide", "information you provide", "query"),
    "company": ("company", "organisation", "organization", "employer", "business information", "job title", "designation")}


def policy_mentions(kind: str, text_lower: str) -> bool:
    return any(re.search(r"(?<![a-z0-9])" + re.escape(s), text_lower) for s in DATA_SYNONYMS.get(kind, (kind,)))        # a word start, so 'age' is not found in 'manage'


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
URL = re.compile(r"https?://|www\.|[A-Za-z0-9-]+\.(?:com|in|org|net|io|co|gov|ai)/", re.I)
