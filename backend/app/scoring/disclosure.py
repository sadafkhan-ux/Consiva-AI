"""What the website was observed collecting, set against what its privacy policy says it collects, why, with whom, and for how long.

The ten-check engine (ten_checks.py) calls `cross_check` once, after the four policy-content checks (2 data collected, 3 purposes, 4 sharing, 5 retention) have been
drafted from the policy analysis. A policy that states a purpose for form submissions only, on a site the scan saw running Google Analytics, Tag Manager or an
advertising pixel, does not state why that data is used, so check 3 cannot pass in full. The same comparison is made for the data described (check 2), the recipients
(check 4) and the retention (check 5). Nothing here scores anything: it only moves a draft result from Passed to Partially Passed, or adds the observed item to the text of a
result that was already not a pass, and says in words which observed item the policy does not cover.

Deterministic and conservative. The policy side is the passages the policy analysis already verified word for word (stored with the scan); the observed side is the stored
scan evidence (forms, trackers, cookies). A passage that is silent is not proof the policy is silent elsewhere, so every changed result carries that limitation. Reading a policy in
which a purpose is stated elsewhere than in the verified passage can still be marked Partially Passed here; the limitation line says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

_F = re.IGNORECASE | re.ASCII

ANALYTICS_TERMS = re.compile(
    r"analytic|statistic|measur|performance|usage (data|information)|how (you|visitors|users) (use|interact)|traffic|tag manager|improv\w+ (our )?(site|website|services?|user experience)", _F)
MARKETING_TERMS = re.compile(
    r"market|advertis|remarket|retarget|promotion|campaign|pixel|interest-based|personali[sz]ed (ads?|content|offers)|social media (plugin|advertis)", _F)
COOKIE_TERMS = re.compile(r"cookie|tracking technolog|web beacon|pixel|device (identifier|information|data)|ip address|browser|usage (data|information)", _F)
FORM_TERMS = re.compile(r"contact|enquir|inquir|form|submi|regist|sign[- ]?up|subscri|newsletter|applic|request|message|support|query|feedback|you (provide|give|share)", _F)
_GENERIC_PERSONAL = re.compile(r"contact (details|information)|personal (details|information|data) (that )?you (provide|submit|give|share)|information (that )?you (provide|submit|give|share)|name and contact", _F)
_ANALYTICS_RECIPIENT = re.compile(ANALYTICS_TERMS.pattern + r"|google", _F)
_MARKETING_RECIPIENT = re.compile(MARKETING_TERMS.pattern + r"|\bmeta\b|facebook|doubleclick|google ads|linkedin|third[- ]party (advertis|marketing)", _F)
_COOKIE_DURATION = re.compile(
    r"cookie[^.]{0,160}(\d+\s*(day|week|month|year)|session|expir|persist)|(\d+\s*(day|week|month|year)|session|expir|persist)[^.]{0,160}cookie", _F)

_EMAIL = re.compile(r"e-?mail", _F)
_PHONE = re.compile(r"phone|mobile|\btel\b|telephone", _F)
_NAME = re.compile(r"(^|[_\-\s])(first|last|full|your)?name\b", _F)
_FORM_KIND_WORDS = {"email": re.compile(r"e-?mail", _F), "phone": re.compile(r"phone|mobile|telephone", _F), "name": re.compile(r"\bname", _F)}


@dataclass
class Observed:
    """What the scan saw the site collecting, named so a result can say which item the policy does not cover."""
    analytics: list[str] = field(default_factory=list)
    marketing: list[str] = field(default_factory=list)
    form_kinds: list[str] = field(default_factory=list)
    forms: int = 0
    persistent_cookies: list[tuple[str, int | None]] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.analytics or self.marketing or self.forms)


def _name_of(item: dict[str, Any]) -> str | None:
    if item.get("vendor"):
        return str(item["vendor"])
    src = item.get("script_src")
    if src:
        try:
            host = urlsplit(src).hostname
        except ValueError:
            host = None
        if host:
            return host.removeprefix("www.")
    return item.get("name") or None


def observe(evidence: dict | None) -> Observed:
    """The data collection the scan evidence records: analytics and advertising trackers and cookies, forms that ask for personal data, persistent cookies."""
    o = Observed()
    if not evidence:
        return o
    for kind, cats in (("analytics", ("analytics",)), ("marketing", ("marketing", "advertising"))):
        names: list[str] = []
        for item in [*(evidence.get("trackers") or []), *(evidence.get("cookies") or [])]:
            if (item.get("category") or "").lower() in cats:
                n = _name_of(item)
                if n and n not in names:
                    names.append(n)
        setattr(o, kind, names[:4])
    kinds: list[str] = []
    for f in evidence.get("forms") or []:
        if (f.get("purpose_guess") or "") == "search":
            continue
        found = []
        for fld in f.get("fields") or []:
            label = f"{fld.get('name') or ''} {fld.get('field_type') or ''}"
            if _EMAIL.search(label):
                found.append("email")
            if _PHONE.search(label):
                found.append("phone")
            if _NAME.search(label):
                found.append("name")
        if found:
            o.forms += 1
            kinds.extend(found)
    o.form_kinds = list(dict.fromkeys(kinds))
    for c in evidence.get("cookies") or []:
        if (c.get("category") or "").lower() in ("analytics", "marketing", "advertising") and not c.get("is_session"):
            days = c.get("lifetime_days")
            o.persistent_cookies.append((str(c.get("name") or "cookie"), int(days) if isinstance(days, (int, float)) else None))
    o.persistent_cookies = o.persistent_cookies[:4]
    return o


def _list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else (", ".join(names[:-1]) + " and " + names[-1] if names else "")


def _seen(o: Observed, analytics: bool = True, marketing: bool = True, forms: bool = True) -> str:
    parts = []
    if analytics and o.analytics:
        parts.append(f"{_list(o.analytics)} (analytics)")
    if marketing and o.marketing:
        parts.append(f"{_list(o.marketing)} (advertising or marketing)")
    if forms and o.forms:
        parts.append(f"{'a form' if o.forms == 1 else f'{o.forms} forms'} collecting {_list(o.form_kinds)}")
    return _list(parts)


def _append(d: Any, sentence: str) -> None:
    d.explanation = f"{d.explanation} {sentence}".strip()


def _downgrade(d: Any, quality: str, coverage: str, sentence: str, limitation: str) -> None:
    """Passed becomes Partially Passed; anything already short of a pass keeps its result and gains the sentence."""
    if d.result == "Passed":
        d.result = "Partially Passed"
        d.quality = quality
        d.coverage = coverage
        d.limitation = limitation
        d.basis = None
    _append(d, sentence)


def cross_check(a: dict | None, evidence: dict | None, c2: Any, c3: Any, c4: Any, c5: Any) -> None:
    """Compare observed collection with the verified policy passages and amend drafts c2 to c5 in place. A draft that could not be assessed (`completed` False) is left alone."""
    o = observe(evidence)

    def quote(req_id: str) -> str:
        for r in (a or {}).get("requirements") or []:
            if r.get("id") == req_id and r.get("status") in ("verified", "partial"):
                return r.get("quote") or ""
        return ""

    def open_for(d: Any) -> bool:
        return d.completed is not False and (a or {}).get("status") != "not_run"

    purposes = " ".join([quote("notice_purposes"), quote("cookies_tracking"), quote("lawful_basis")])
    itemised = " ".join([quote("notice_data_itemised"), quote("cookies_tracking")])
    sharing = " ".join([quote("data_sharing"), quote("cookies_tracking")])
    retention = " ".join([quote("retention"), quote("cookies_tracking")])
    lim = "Only the passages the analysis verified were compared with what the scan observed; the policy may state this elsewhere."

    # 3. purposes: every kind of collection observed needs a purpose the policy states
    if open_for(c3) and o.any:
        miss = Observed(
            analytics=o.analytics if o.analytics and not ANALYTICS_TERMS.search(purposes) else [],
            marketing=o.marketing if o.marketing and not MARKETING_TERMS.search(purposes) else [],
            forms=o.forms if o.forms and not FORM_TERMS.search(purposes) else 0,
            form_kinds=o.form_kinds,
        )
        if c3.result in ("Passed", "Partially Passed") and miss.any:
            what = _seen(miss)
            _downgrade(c3, "Limited", "Purpose not stated for everything observed",
                       f"The scan observed {what}, but the passage that states the purposes does not mention it, so no purpose is evidenced for it. The policy should say what each is used for.", lim)
        elif c3.result == "Failed":
            _append(c3, f"The scan observed {_seen(o)}, so a purpose should be stated for each.")

    # 2. data described: form fields and the cookie and device data of trackers
    if open_for(c2) and o.any:
        generic = bool(_GENERIC_PERSONAL.search(itemised))
        missing_fields = [k for k in o.form_kinds if not _FORM_KIND_WORDS[k].search(itemised)] if o.forms and not generic else []
        no_device = bool((o.analytics or o.marketing) and not COOKIE_TERMS.search(itemised) and not ANALYTICS_TERMS.search(itemised))
        gaps = []
        if missing_fields:
            gaps.append(f"the {_list(missing_fields)} that a form on the site asks for")
        if no_device:
            gaps.append(f"the cookie and device data collected by {_list([*o.analytics, *o.marketing][:4])}")
        if c2.result in ("Passed", "Partially Passed") and gaps:
            _downgrade(c2, "Limited", "Observed data not all described",
                       f"The scan observed data the policy's description does not mention: {_list(gaps)}.", lim)
        elif c2.result == "Failed" and (o.forms or o.analytics or o.marketing):
            _append(c2, f"The scan observed {_seen(o)}, which the policy should describe.")

    # 4. recipients: the analytics and advertising services the scan saw receiving data
    if open_for(c4) and (o.analytics or o.marketing):
        gaps = []
        if o.analytics and not _ANALYTICS_RECIPIENT.search(sharing):
            gaps.append(f"{_list(o.analytics)} (analytics)")
        if o.marketing and not _MARKETING_RECIPIENT.search(sharing):
            gaps.append(f"{_list(o.marketing)} (advertising or marketing)")
        if c4.result in ("Passed", "Partially Passed") and gaps:
            if c4.result == "Passed":
                _downgrade(c4, "General", "Observed recipients not named", f"The scan observed {_list(gaps)}, which the sharing passage does not name as a recipient.", lim)
            elif "The scan observed" not in c4.explanation:         # the recipients rule of 1.0.0 may already have named what was seen
                _append(c4, f"The scan observed {_list(gaps)}, which the passage does not name.")
        elif c4.result == "Failed":
            _append(c4, f"The scan observed {_seen(o, forms=False)} receiving data, which the policy should name as recipients.")

    # 5. retention: the persistent cookies the scan saw set, and the form data
    if open_for(c5) and (o.persistent_cookies or o.forms):
        cookie_gap = bool(o.persistent_cookies) and not _COOKIE_DURATION.search(retention)
        named = _list([f"{n} ({d} days)" if d else n for n, d in o.persistent_cookies])
        if c5.result in ("Passed", "Partially Passed") and cookie_gap:
            _downgrade(c5, "Limited", "Cookie retention not stated",
                       f"The scan observed persistent cookies ({named}), but no retention period or criterion is stated for them.", lim)
        elif c5.result == "Failed":
            kept = []
            if o.persistent_cookies:
                kept.append(f"persistent cookies ({named})")
            if o.forms:
                kept.append(f"form data ({_list(o.form_kinds)})")
            _append(c5, f"The scan observed {_list(kept)}, for which the policy should state how long it is kept.")


# ── contact details of the DPO or grievance officer ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
_C_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", _F)
_C_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_C_DESIGNATION = re.compile(
    r"\b(?:grievance officer|data protection officer|dpo|privacy officer|compliance officer|nodal officer|privacy office|head of [a-z ]+|chief [a-z ]+|director|manager|officer)\b", _F)
_C_NAME = re.compile(
    r"\b(?:Mr|Ms|Mrs|Dr|Shri|Smt)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*|\bName\s*[:\-\u2013\u2014]\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+"
    r"|[A-Z][a-z]+\s+[A-Z][a-z]+\s*[,\u2013\u2014-]\s*(?:Grievance|Data Protection|Privacy|Compliance|Nodal)\s+Officer")
_C_ADDRESS = re.compile(r"\b(?:registered office|postal address|address)\b|\b\d{6}\b", _F)
_C_PERIOD = re.compile(r"\b\d+\s*(?:working\s+)?(?:hours?|days?)\b|\bone month\b|\bwithin (?:a|one|two|three|seven|fifteen|thirty) \w+", _F)


def contact_elements(text: str) -> tuple[list[str], list[str]]:
    """Which parts of a published DPO or grievance contact the passages contain: (detected, not detected). Name detection is by pattern ("Mr", "Name:", "Jane Doe, Grievance Officer")
    and is reported as "not detected", never as absent."""
    found, missing = [], []
    for label, rx in (("email", _C_EMAIL), ("phone", _C_PHONE), ("name", _C_NAME), ("designation", _C_DESIGNATION), ("postal address", _C_ADDRESS), ("response period", _C_PERIOD)):
        m = next((x for x in rx.finditer(text) if rx is not _C_PHONE or sum(c.isdigit() for c in x.group(0)) >= 10), None)
        (found if m else missing).append(label)
    return found, missing


def contact_advisory(a: dict | None, c7: Any) -> None:
    """Adds to a check 7 result that rests on a policy passage the parts of the contact found and not detected. Changes no mark: the DPDP Act and Rules require business contact
    information (s.8(9), Rule 9) and a stated response time (Rule 14), not a name or a designation; other regimes can (SPDI Rules 2011, e-commerce rules; to be confirmed per site)."""
    if c7.result not in ("Passed", "Partially Passed") or c7.completed is False:
        return
    text = " ".join((r.get("quote") or "") for r in (a or {}).get("requirements") or []
                    if r.get("id") in ("grievance_contact", "dpo_contact", "grievance_timeline") and r.get("status") in ("verified", "partial"))
    if not text.strip():
        return
    found, missing = contact_elements(text)
    note = f"Contact details in the passages found: {_list(found) or 'none identified'}."
    if missing:
        note += f" Not detected: {_list(missing)}."
    if "name" in missing or "designation" in missing:
        note += " The DPDP Act and Rules ask for business contact information, not a name or designation; other rules (SPDI Rules 2011, e-commerce rules) may require them, depending on the site."
    _append(c7, note)


# ── consent behaviour: which tracker or cookie was seen in which consent state ─────────────────────────────────────────────────────────────────────────────────────────────
_TRACKING = re.compile(r"analytic|market|advert|track", _F)
_STATE_WORDS = (("pre_consent", "before consent"), ("post_accept", "after Accept"), ("post_reject", "after Reject"))


def _utc(iso: str | None) -> str | None:
    """'2026-11-03T06:10:17.710832+00:00' -> '2026-11-03 06:10:17 UTC'. Anything that is not an ISO time is not printed."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", iso or "")
    return f"{m.group(1)} {m.group(2)} UTC" if m else None


def consent_events(evidence: dict | None, accept_tested: bool, reject_tested: bool) -> str:
    """The analytics and marketing requests and cookies the scan saw in each consent state it tested, by name, with the time the scanner first saw each state's cookies.

    Built from the stored scan evidence only (trackers and cookies with their consent states, and each cookie's observation record). A state the scan did not test is said to be untested,
    never reported as clean. Request times are not stored for third-party requests, only the state they were seen in, so none is printed for them. Full request addresses are in the scan evidence."""
    ev = evidence or {}
    trackers = [t for t in ev.get("trackers") or [] if _TRACKING.search(t.get("category") or "")]
    cookies = [c for c in ev.get("cookies") or [] if _TRACKING.search(c.get("category") or "")]
    parts = []
    for state, words in _STATE_WORDS:
        if (state == "post_accept" and not accept_tested) or (state == "post_reject" and not reject_tested):
            parts.append(f"{words}: not tested")
            continue
        reqs = list(dict.fromkeys(n for t in trackers if state in (t.get("consent_states") or []) for n in [_name_of(t)] if n))
        cks = [c for c in cookies if state in (c.get("consent_states") or [])]
        if not reqs and not cks:
            parts.append(f"{words}: none")
            continue
        bits = []
        if reqs:
            bits.append(f"requests to {_list(reqs[:3])}" + (f" and {len(reqs) - 3} more" if len(reqs) > 3 else ""))
        if cks:
            when = sorted(filter(None, (_utc(o.get("observed_at")) for c in cks for o in c.get("observations") or [] if o.get("consent_state") == state)))
            names = [str(c.get("name")) for c in cks]
            bits.append(f"cookies {_list(names[:3])}" + (f" and {len(names) - 3} more" if len(names) > 3 else "") + (f", first seen {when[0]}" if when else ""))
        parts.append(f"{words}: {'; '.join(bits)}")
    return "Seen " + " | ".join(parts) + "."
