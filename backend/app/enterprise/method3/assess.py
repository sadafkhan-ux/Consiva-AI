"""Method 3.0.0: the 96 parameters worked out from a scan's stored evidence and facts. Deterministic: no model call, no browser.

Inputs (method3/context.py): the evidence of every inspected page (cookies, requests, forms, headers, HTML), the consent tests, the facts the text analysis quoted from the policies
(each with its verified quote), the model's few interpretations, and the 51-parameter results of the same scan (reused where a parameter is the same reading).

Rules of evidence:
  * URL-wise parameters are worked out on every applicable URL; the result is Pass if every URL meets it, Partial if at least half do, Fail if fewer than half do.
  * A policy-judged parameter rests on a quoted passage. Its absence is a Fail; if part of the policy could not be analysed, the Fail says so and is marked for review.
  * Duties internal to the organisation are judged from the published policy and say "internal records not inspected".
  * Nothing here invents a finding: every result lists the evidence it rests on, with its URL.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from urllib.parse import urlparse

from app.enterprise.analysis import consolidate as co
from app.enterprise.analysis import textrules as tr
from app.enterprise.method3 import catalogue as cat
from app.enterprise.method3.context import NOTICE_HREF, M3Context, same_host
from app.enterprise.method3.engine import FAIL, INFORMATION, NA, PARTIAL, PASS, Result, UrlCheck
from app.enterprise.method3.site_category import CHILDREN, EDTECH, LABELS, THIRD_SCHEDULE, THRESHOLD_LABEL, SiteCategory
from app.enterprise.method3.site_category import detect as detect_site
from app.enterprise.scoring.engine import Assessment

PRIVACY, COOKIE, GRIEVANCE, CONTACT, ABOUT, TERMS = co.PRIVACY, co.COOKIE, co.GRIEVANCE, co.CONTACT, co.ABOUT, co.TERMS
POL = (PRIVACY, GRIEVANCE, TERMS, CONTACT, COOKIE)
_CAT_ORDER = {PRIVACY: 0, GRIEVANCE: 1, COOKIE: 2, TERMS: 3, CONTACT: 4, ABOUT: 5}
_CSS_QUOTE = re.compile(r"[{}]|^\s*\.[a-z][\w-]*\s*$|^\s*\[dir=", re.I)
REC = "Published policy only: the organisation's internal records were not inspected, so this reflects what the policy states, not what is done."
MAX_EV = 6

#: The 51-parameter result each 3.0.0 parameter corresponds to one-to-one, for the "differs from v2" note.
V2_SAME = {"N01": "N1", "N04": "N4", "N05": "N2", "N06": "N3", "N10": "N6", "N12": "N7", "N13": "N8", "C01": "C10", "C04": "C11", "C06": "C9", "C10": "C2", "C11": "C12",
           "C12": "C4", "C13": "C5", "C15": "C6", "C16": "C7", "C17": "C8", "C18": "C13", "F02": "P2", "F08": "G9", "R02": "G6", "R05": "G4", "R07": "G10", "RT4": "R1",
           "G01": "G1", "X01": "X1", "X02": "X2", "X03": "X3", "K05": "K2", "M01": "M1", "M02": "M2", "TS03": "TS2", "TS04": "TS3", "TS13": "TS6"}
_V2_STATUS = {"met": PASS, "partly_met": PARTIAL, "not_met": FAIL, "not_applicable": NA}


# ---- helpers ----------------------------------------------------------------------------------------------------------------------------------------------------------------

def cite(f: dict) -> str:
    return f'{f["url"]}{" p." + str(f["page_number"]) if f.get("page_number") else ""}: "{" ".join(f["quote"].split())[:320]}"'


def refs_of(facts: list[dict]) -> list[str]:
    return list(dict.fromkeys(f"ev:{f['evidence_id']}" for f in facts))[:12]


def blob(f: dict) -> str:
    return f"{f.get('quote', '')} {f.get('value', '')}"


class A:
    """The assessment run: the context, the 51-parameter results, and the results so far."""

    def __init__(self, m: M3Context, v2: dict[str, Assessment], site: SiteCategory | None = None):
        self.m, self.v2, self.ctx = m, v2, m.ctx
        self.out: dict[str, Result] = {}
        self.pages = self._pages()
        self.site = site if site is not None else detect_site(m)

    # facts ------------------------------------------------------------------------------------------------------------------------------------------------------------
    def of(self, topic: str, *cats: str, pattern: str | None = None) -> list[dict]:
        """Facts of a topic, the privacy notice's own first, then the rights and grievance page, the cookie policy, the terms, and the contact pages: the evidence a result quotes
        comes from the notice whenever the notice says it."""
        return sorted([f for f in self.ctx.of(topic, *(cats or POL), pattern=pattern) if not _CSS_QUOTE.search(f.get("quote", ""))],
                      key=lambda f: (_CAT_ORDER.get(f.get("category"), 9), 0 if "privacy" in urlparse(f.get("url", "")).path.lower() else 1, f.get("id", 0)))

    def route_links(self) -> list[tuple[str, str]]:
        """Links in the notice pages that lead to a route for exercising rights or withdrawing consent, best first. The site's menu is on the notice page too, so a product or
        marketing page about rights ('Data Principal Rights Management') is not a route; a link named for a request, a grievance or a portal is."""
        links = [(h, t) for u in self.policy_urls() for h, t in (self.m.html.get(u).links if self.m.html.get(u) else [])]
        out = []
        for h, t in links:
            hay = h + " " + t
            if not re.search(r"right|request|grievance|withdraw|preference|portal|dsar|data-?principal|cookie[- ]settings|complain", hay, re.I) or NOTICE_HREF.fullmatch(urlparse(h).path or ""):
                continue
            if re.search(r"management|software|platform|pricing|solution|product|/blog/|/industry/|/industries/", urlparse(h).path, re.I):
                continue                                   # a page that sells or explains a product, not the route a person uses
            rank = 0 if re.search(r"submit|request|grievance|portal|dsar", hay, re.I) else 1
            out.append((rank, h, t))
        seen, best = set(), []
        for _, h, t in sorted(out, key=lambda x: x[0]):
            if h not in seen:
                seen.add(h)
                best.append((h, t))
        return best

    def has_policy(self) -> bool:
        return bool(self.ctx.src(PRIVACY))

    def policy_urls(self) -> list[str]:
        urls = {s.url for s in self.ctx.src(PRIVACY) if not s.virtual} or {s.url for s in self.ctx.src(PRIVACY)}
        return sorted(urls, key=lambda u: (0 if "privacy" in urlparse(u).path.lower() else 1, u))

    def text(self, *cats: str) -> str:
        return self.ctx.text(*(cats or POL))

    def sentences(self, pattern: str, *cats: str, limit: int = 3) -> list[dict]:
        """Sentences of the policy pages' own text that match, as fact-shaped records (url, quote, evidence id). Used where the extracted facts miss a passage the text plainly
        holds (a page the analysis did not read, a statement split over a table): the quote is verbatim, so it can be checked like any other."""
        rx = re.compile(pattern, re.I)
        out, seen = [], set()
        for s in self.ctx.src(*(cats or POL)):
            body = s.text.split("[shadow DOM]")[0]
            for m in rx.finditer(body):
                start = max(body.rfind(". ", 0, m.start()), body.rfind("\n", 0, m.start())) + 1
                ends = [i for i in (body.find(". ", m.end()), body.find("\n", m.end())) if i != -1]
                end = min(ends) + 1 if ends else len(body)
                q = " ".join(body[start:end].split())[:400]
                if q and len(q) >= 40 and q not in seen:              # a heading alone ("Verifying Who You Are") is not a statement
                    seen.add(q)
                    out.append({"url": s.url, "quote": q, "value": q, "evidence_id": s.evidence_id, "page_number": s.page_number if s.kind == "document_text" else 0, "specific": True})
                if len(out) >= limit:
                    return out
        return out

    def unread_note(self, *cats: str) -> str | None:
        n = sum(self.ctx.unread.get(c, 0) for c in {PRIVACY, *cats})
        return None if n == 0 else f"{n} passage(s) of the policy pages could not be analysed (model or API failure); a re-run of the analysis may find the statement."

    # results ----------------------------------------------------------------------------------------------------------------------------------------------------------
    def put(self, pid: str, status: str, explanation: str, evidence=(), *, refs=(), basis: str = "", urls: list[UrlCheck] | None = None, review: bool = False,
            limitation: str | None = None, differs: str | None = None, applicability: str | None = None) -> None:
        p = cat.BY_ID[pid]
        if applicability == cat.HUMAN_REVIEW:
            status, review = NA, True
        if p.kind == cat.INFO:
            status = INFORMATION
        if status == PARTIAL and not p.partial_allowed:
            status = FAIL
            explanation += " (this requirement has no partial credit, so a partly met reading is a Fail)"
        if p.kind == cat.RECORDS and not limitation:
            limitation = REC
        if differs is None and pid in V2_SAME:
            a = self.v2.get(V2_SAME[pid])
            if a is not None and a.state in _V2_STATUS and _V2_STATUS[a.state] != status and status in (PASS, PARTIAL, FAIL, NA):
                differs = f"Enterprise v2 {a.parameter_id} recorded {a.state.replace('_', ' ')} ({(a.note or '')[:180]})."
        self.out[pid] = Result(pid, status, explanation, [e for e in evidence if e][:MAX_EV], list(dict.fromkeys(refs))[:12], basis or p.kind, urls or [], review, limitation, differs,
                               applicability)

    def policy_result(self, pid: str, found_full: list[dict], found_part: list[dict], what: str, *, full_note: str, part_note: str, cats=POL, review: bool = False,
                      extra_ev: list[str] = ()) -> None:
        """The common shape of a policy-judged parameter: Pass on a quote that satisfies it, Partial on one that satisfies part, Fail when the policy is silent."""
        if found_full:
            self.put(pid, PASS, full_note, [cite(f) for f in found_full[:3]] + list(extra_ev), refs=refs_of(found_full), basis="Read from the published policy", review=review)
        elif found_part:
            self.put(pid, PARTIAL, part_note, [cite(f) for f in found_part[:3]] + list(extra_ev), refs=refs_of(found_part), basis="Read from the published policy", review=review)
        else:
            self.absent(pid, what, cats)

    def absent(self, pid: str, what: str, cats=POL) -> None:
        if not self.has_policy():
            self.put(pid, FAIL, f"No privacy notice was found on the crawled pages, so {what} is not published.", basis="Read from the published policy")
            return
        un = self.unread_note(*cats)
        self.put(pid, FAIL, f"The policy pages were read and say nothing about {what}.", [f"Policy read: {u}" for u in self.policy_urls()[:3]],
                 refs=list(self.ctx.refs(PRIVACY)), basis="Read from the published policy", review=bool(un), limitation=un)

    # pages ------------------------------------------------------------------------------------------------------------------------------------------------------------
    def _pages(self) -> dict[str, dict]:
        """Every inspected web page: its network record, its cookies before a choice, its headers, its forms, and the evidence ids."""
        pages: dict[str, dict] = {}
        for ev, n in self.ctx.loaded.networks:
            u = n.get("page_url")
            if u:
                pages.setdefault(u, {"url": u, "net": n, "cookies": [], "forms": [], "sec": None, "ev": []})["ev"].append(f"ev:{ev}")
        for ev, c in self.ctx.loaded.cookies:
            if c.get("page_url") in pages:
                pages[c["page_url"]]["cookies"] = c.get("cookies") or []
        for ev, h in self.ctx.loaded.headers:
            u = h.get("page_url")
            if u in pages and "security" in h and pages[u]["sec"] is None:
                pages[u]["sec"] = h
        for ev, d in self.ctx.loaded.form_data:
            u = d.get("page_url")
            if u in pages:
                pages[u]["forms"] = d.get("forms") or []
                pages[u]["ev"].append(f"ev:{ev}")
        return dict(sorted(pages.items()))

    def per_url(self, pid: str, fn: Callable[[dict], tuple[str | None, str]]) -> tuple[str, list[UrlCheck]]:
        res = []
        for u, pg in self.pages.items():
            st, note = fn(pg)
            if st is not None:
                res.append(UrlCheck(u, st, note))
        if not res:
            return NA, res
        ok = sum(1 for r in res if r.status == PASS)
        return (PASS if ok == len(res) else PARTIAL if ok * 2 >= len(res) else FAIL), res

    @staticmethod
    def summary(res: list[UrlCheck]) -> str:
        c = Counter(r.status for r in res)
        return f"{c.get(PASS, 0)} of {len(res)} applicable URLs pass"

    def consent(self, name: str) -> tuple[str | None, dict]:
        ev = self.ctx.loaded.consent.get(name)
        return (f"ev:{ev[0]}", ev[1]) if ev else (None, {})


# ---- forms and trackers ----------------------------------------------------------------------------------------------------------------------------------------------

def is_website_box(f: dict) -> bool:
    """A box that takes a website address (a free scan, a site audit) collects no personal data."""
    return set(f.get("collects") or []) <= {"address"} and bool(re.search(r"website|url|domain|site address", " ".join([f.get("label_text") or "", f.get("text") or ""]), re.I))


def pii_forms(pg: dict) -> list[dict]:
    return [f for f in pg["forms"] if co._collects_personal(f) and not is_website_box(f)]


_TRACKING_PROVIDER = re.compile(r"google (analytics|ads|\(doubleclick\))|meta|facebook|microsoft|bing|clarity|hotjar|hubspot|linkedin|pinterest|tiktok|segment|yandex|mixpanel|"
                                r"amplitude|outbrain|youtube|criteo|taboola|adobe|twitter|snap|quora|reddit", re.I)
_TRACKING_NAME = re.compile(r"^(_ga|_gid|_gat|_gcl|_fbp|_fbc|_hj|ajs_|_clck|_clsk|_uet|mp_|amplitude|_pin|_tt|_ttp|ide|nid|muid|anonchk|clid|sm|mr|srm_b|test_cookie|_rdt|_scid)",
                            re.I)
_TRACKING_DOMAIN = re.compile(r"clarity\.ms|bing\.com|doubleclick|google-analytics|facebook|linkedin|hotjar|tiktok|segment|mixpanel|amplitude|outbrain|taboola|criteo|loom\.com", re.I)


#: The kinds of data a form collects that a notice must itemise, and the words that itemise them. Stricter than a mention anywhere in the policy: "contact details" does not name a
#: phone number, and the message a person writes is the enquiry itself, not a category of personal data to itemise.
ITEMISED_KINDS = {
    "email": r"e-?mail", "phone": r"phone|mobile|telephone number|contact number", "name": r"\bnames?\b", "address": r"postal address|billing address|home address|residential address|street|city|pin ?code|postcode",
    "date of birth": r"date of birth|\bbirth|\bage\b|\bdob\b", "company": r"company|organi[sz]ation|employer|business name|job title|designation|role",
}


def itemised(kind: str, text_lower: str) -> bool:
    return bool(re.search(ITEMISED_KINDS.get(kind, re.escape(kind)), text_lower, re.I))


def tracking_cookie(c: dict) -> bool:
    from app.rules.cookie_purposes import describe_cookie            # the 5-page agent's cookie catalogue, used by import only (never changed)
    d = describe_cookie(c.get("name", ""))
    if d.get("provider"):
        return bool(_TRACKING_PROVIDER.search(d["provider"]))
    return bool(_TRACKING_NAME.match(c.get("name", ""))) or bool(_TRACKING_DOMAIN.search(c.get("domain", "")))


def describe(c: dict) -> dict:
    from app.rules.cookie_purposes import describe_cookie
    return describe_cookie(c.get("name", ""))


#: Names that, in a policy, name a service; the parent company alone (Google, Microsoft) does not name a product it runs several of.
_PRODUCT_TOKENS = {
    "google analytics": ("google analytics", "ga4", "universal analytics"), "google tag manager": ("google tag manager", "tag manager", "gtm"),
    "google ads": ("google ads", "adwords", "doubleclick", "googlesyndication", "google marketing platform", "google adsense"),
    "microsoft clarity": ("clarity",), "bing ads": ("bing", "microsoft advertising", "clarity"), "meta pixel": ("meta pixel", "facebook pixel", "meta", "facebook"),
    "linkedin insight": ("linkedin",), "hotjar": ("hotjar",), "hubspot": ("hubspot",), "segment": ("segment",), "openai": ("openai",),
}


def vendor_named(vendor: str, host: str, text_lower: str) -> bool:
    key = (vendor or "").lower()
    toks = set(_PRODUCT_TOKENS.get(key, ()))
    if not toks:
        toks = {t for t in tr.vendor_tokens(vendor, host) if t not in ("google", "microsoft", "amazon", "apple", "yahoo")}
    return tr.names_any(text_lower, toks)


# ---- the rules ----------------------------------------------------------------------------------------------------------------------------------------------------------------

def notice(a: A) -> None:
    pol = a.ctx.src(PRIVACY)
    v2 = a.v2.get("N1")
    if pol:
        own = [s for s in pol if not s.virtual and s.kind == "page_text"]
        docs = [s for s in pol if s.kind == "document_text"]
        urls = a.policy_urls()
        if own:
            a.put("N01", PASS, f"A privacy notice page loaded and its full text was read ({len(urls)} notice page(s)).", [f"{u}: notice read in full" for u in urls[:4]],
                  refs=list(a.ctx.refs(PRIVACY)), basis="Observed (crawl)")
        elif docs:
            a.put("N01", PARTIAL, "The notice was found only as a document (PDF), not as a page of the site.", [f"{s.url}: document" for s in docs[:3]], refs=list(a.ctx.refs(PRIVACY)),
                  basis="Observed (crawl)")
        else:
            a.put("N01", PASS, "The privacy notice is published as a section of another page, and its text was read.", [f"{s.url}: notice section" for s in pol[:3]],
                  refs=list(a.ctx.refs(PRIVACY)), basis="Observed (crawl)")
    else:
        lim = (v2.note if v2 is not None and v2.state == "not_assessed" else None)
        if lim:
            # The crawl was cut short (a limit, or URLs never reached): "not found" is not a finding, as in v2 N1. The marks are held back for human review, never a Fail.
            a.put("N01", NA, "No privacy notice was found in the pages reached, but the crawl did not reach every page, so this is not a finding. "
                  "Held back for human review: confirm where the notice is published, or scan again with a higher limit.", basis="Observed (crawl)", review=True, limitation=lim,
                  applicability=cat.HUMAN_REVIEW)
        else:
            a.put("N01", FAIL, "No page or document classified as a privacy notice was found on the crawled pages.", basis="Observed (crawl)")

    notice_paths = {urlparse(u).path.rstrip("/") for u in a.policy_urls()}

    def n02(pg):
        h = a.m.html.get(pg["url"])
        if h is None:
            return None, ""
        ok = h.notice_link or any(urlparse(href).path.rstrip("/") in notice_paths for href, _ in h.links)
        return (PASS, "links to the notice") if ok else (FAIL, "no link to the notice in the page")
    st, res = a.per_url("N02", n02)
    a.put("N02", st, f"{A.summary(res)}: a link to the privacy notice in the page HTML.", [f"{r.url}: {r.note}" for r in res if r.status != PASS][:MAX_EV], urls=res,
          basis="Observed URL by URL (page HTML)")

    # N03: standalone, or relying on another document for a key part
    if not pol:
        a.put("N03", FAIL, "No privacy notice was found, so there is no standalone notice.", basis="Read from the published policy")
    else:
        full = "\n".join(s.text for s in pol if not s.virtual) or "\n".join(s.text for s in pol)
        refer = re.search(r"[^.\n]{0,120}\b(set out separately|see (our|the) (cookie|privacy rights|terms)|refer to (our|the) (cookie|terms)|described in (our|the) (cookie|terms)|"
                          r"full list of cookies[^.\n]{0,60}(see|in) our cookie policy|how to exercise these[^.\n]{0,60}→)[^.\n]{0,120}", full, re.I)
        only_section = all(s.virtual for s in pol)
        if only_section:
            a.put("N03", PARTIAL, "The notice is a section of another document rather than a notice of its own.", [f"{pol[0].url}: notice embedded in the page"], refs=list(a.ctx.refs(PRIVACY)),
                  basis="Read from the published policy")
        elif refer:
            a.put("N03", PARTIAL, "The notice is a page of its own, but it sends the reader to another document for a key part (cookies or the request process).",
                  [f'{a.policy_urls()[0]}: "{" ".join(refer.group(0).split())[:260]}"'], refs=list(a.ctx.refs(PRIVACY)), basis="Read from the published policy", review=True,
                  limitation="A reviewer may accept linked companion pages as part of the notice; if so this becomes Pass.")
        else:
            a.put("N03", PASS, "The notice is a standalone page with its own sections and does not depend on another document to be understood.",
                  [f"{u}: standalone notice ({sum(len(s.text) for s in pol if s.url == u):,} characters)" for u in a.policy_urls()[:2]], refs=list(a.ctx.refs(PRIVACY)),
                  basis="Read from the published policy")

    # N04: the model's reading, always for a person to confirm
    j = a.m.judgments.get("N4") or {}
    v2 = a.v2.get("N4")
    if not pol:
        a.put("N04", FAIL, "No privacy notice was found.", basis="Read from the published policy")
    elif j.get("verdict") in ("yes", "partly", "no"):
        st = {"yes": PASS, "partly": PARTIAL, "no": FAIL}[j["verdict"]]
        a.put("N04", st, f"Readability figures and the model's reading of sampled passages: {j.get('reason', '')[:260]}", [f"Model reading ({j.get('confidence')} confidence) of {a.policy_urls()[0]}"],
              refs=list(a.ctx.refs(PRIVACY)), basis="Model reading of the notice (flagged for human review)", review=True)
    else:
        why = (v2.note if v2 else "") or "the model could not settle the reading"
        a.put("N04", PARTIAL, f"The plain-language reading could not be settled: {why[:220]}", basis="Model reading of the notice (flagged for human review)", review=True,
              limitation="A person should read the notice and decide; Partial is held until then.")

    # N05: itemised data, checked against what the forms ask for
    items = a.of("data_category", PRIVACY)
    specific = [f for f in items if f.get("specific")]
    generic = [f for f in items if not f.get("specific")]
    kinds = sorted({k for pg in a.pages.values() for f in pii_forms(pg) for k in f.get("collects", []) if k in ITEMISED_KINDS})
    itemised_text = " ".join(blob(f) for f in items).lower()
    missing = [k for k in kinds if not itemised(k, itemised_text)]
    ev = [cite(f) for f in specific[:2]] + ([f"Generic item: {cite(generic[0])}"] if generic else []) + ([f"Forms ask for {', '.join(missing)}, not named in the notice's list of data"] if missing else [])
    if not items:
        a.absent("N05", "the kinds of personal data collected", (PRIVACY,))
    elif len(specific) >= 3 and not missing and not generic:
        a.put("N05", PASS, f"The notice names the personal data specifically ({len(specific)} items), including every kind the forms ask for.", ev, refs=refs_of(specific),
              basis="Read from the policy against the forms observed")
    elif specific:
        why = "; ".join(x for x in (f"forms ask for {', '.join(missing)}, which the notice does not name" if missing else "",
                                     f"some data is described only generically ({generic[0]['value'][:60]})" if generic else "",
                                     "fewer than three items are named" if len(specific) < 3 else "") if x)
        a.put("N05", PARTIAL, f"Personal data is itemised in part: {why}.", ev, refs=refs_of(items), basis="Read from the policy against the forms observed")
    else:
        a.put("N05", FAIL, "The notice describes personal data only in general terms.", ev, refs=refs_of(items), basis="Read from the published policy")

    purposes = a.of("purpose", PRIVACY)
    sp = [f for f in purposes if f.get("specific")]
    if not purposes:
        a.absent("N06", "the purposes of processing", (PRIVACY,))
    elif len(sp) >= 2 and len(sp) * 3 >= len(purposes) * 2:
        a.put("N06", PASS, f"Specific purposes are stated ({len(sp)} of {len(purposes)} purpose statements are specific).", [cite(f) for f in sp[:3]], refs=refs_of(sp),
              basis="Read from the published policy")
    elif sp:
        a.put("N06", PARTIAL, f"Some purposes are specific ({len(sp)} of {len(purposes)}), others are vague.", [cite(f) for f in sp[:2]] + [cite(f) for f in purposes if not f.get("specific")][:1],
              refs=refs_of(purposes), basis="Read from the published policy")
    else:
        a.put("N06", FAIL, "Only generic purposes are stated.", [cite(f) for f in purposes[:2]], refs=refs_of(purposes), basis="Read from the published policy")

    svc = [f for f in purposes if re.search(r"service|order|deliver|account|enquir|inquir|walkthrough|onboard|subscri|newsletter|support|consult|resource|guide|demo|platform|product|"
                                             r"billing|invoice|payment|booking|recruit|job|application|event|webinar|download", blob(f), re.I)]
    a.policy_result("N07", svc if len(svc) >= 2 else [], svc, "the goods, services or uses the processing enables",
                    full_note=f"Purposes are tied to the services or uses they enable ({len(svc)} statements).", part_note="Only one purpose is tied to a service or use.", cats=(PRIVACY,))

    wd = a.of("withdraw_consent", pattern=r"withdraw|revoke|opt[- ]?out|unsubscribe|change your (consent|choice)")
    means = [f for f in wd if re.search(r"@|https?://|link|setting|preference|banner|portal|form|unsubscribe|button|email|write to|contact", blob(f), re.I)]
    a.policy_result("N08", means, wd, "how consent can be withdrawn", full_note="Withdrawal is stated with a means of withdrawing.", part_note="Withdrawal is stated without a means.")

    rights = a.of("right_access") + a.of("right_correction") + a.of("right_erasure")
    chan = [f for f in a.of("exercise_channel") if re.search(r"@|https?://|portal|form|link|address|write|email|post|request", blob(f), re.I)]
    if chan and rights:
        a.put("N09", PASS, "The notice says how to exercise the rights, with a means.", [cite(f) for f in chan[:2]] + [cite(rights[0])], refs=refs_of(chan + rights[:1]),
              basis="Read from the published policy")
    elif rights or chan:
        a.put("N09", PARTIAL, "The rights are stated without a means of exercising them." if rights else "A channel is given, but the rights are not stated with it.",
              [cite(f) for f in (rights or chan)[:2]], refs=refs_of(rights or chan), basis="Read from the published policy")
    else:
        a.absent("N09", "how to exercise the rights")

    board = a.of("board_complaint", pattern=r"board")
    route = [f for f in board if re.search(r"https?://|www\.|portal|online|meity|website|file (a|your) complaint|complaint (form|channel|mechanism)|address|e-?mail|app", blob(f), re.I)] or \
        a.sentences(r"board[^.\n]{0,200}(meity\.gov\.in|https?://|www\.|portal|complaint channel|filing details|file a complaint online)|"
                    r"(meity\.gov\.in|complaint channel|filing details)[^.\n]{0,200}board", PRIVACY, GRIEVANCE)
    a.policy_result("N10", route, board, "how to complain to the Data Protection Board", full_note="The Board is named and the route to it is given.",
                    part_note="The Data Protection Board is named, but no route to it (link, portal or how to file) is given.")

    route_links = a.route_links()
    emails = [f for f in a.of("exercise_channel") + a.of("withdraw_consent") if re.search(r"@|address|post", blob(f), re.I)]
    if route_links:
        h, t = route_links[0]
        a.put("N11", PASS, "The notice links to a route for withdrawing consent or exercising rights.", [f'Link "{t or h}" → {h}'] + [f'Link "{x[1]}" → {x[0]}' for x in route_links[1:3]
                                                                                                                   if re.search(r"submit|request|grievance|portal|dsar|withdraw|cookie[- ]settings", x[0] + " " + x[1], re.I)],
              refs=list(a.ctx.refs(PRIVACY)), basis="Observed (notice HTML)")
    elif emails:
        a.put("N11", PARTIAL, "Only an e-mail or postal address is given, not a link on the site.", [cite(f) for f in emails[:2]], refs=refs_of(emails), basis="Read from the published policy")
    else:
        a.absent("N11", "a link for withdrawing consent and exercising rights")

    v2n7 = a.v2.get("N7")
    opts = a.of("language_option", PRIVACY)
    if v2n7 is not None and v2n7.state == "met":
        a.put("N12", PASS, f"{v2n7.note[:1].upper() + v2n7.note[1:]}." + (" The notice also offers other languages." if opts else ""), [cite(f) for f in opts[:2]] or [f"{u}: notice text" for u in a.policy_urls()[:1]],
              refs=list(v2n7.evidence_refs), basis="Read from the published policy")
    else:
        a.put("N12", FAIL, "The notice is neither in English nor in an Eighth Schedule language, and no such option is offered." if pol else "No privacy notice was found.",
              basis="Read from the published policy")

    def n13(pg):
        fs = pii_forms(pg)
        if not fs:
            return None, ""
        bad = [f for f in fs if not co._has_notice_link(f)]
        return (FAIL, f"{len(bad)} of {len(fs)} form(s) without a notice link: '{(bad[0].get('text') or '')[:40].strip()}'") if bad else (PASS, f"{len(fs)} form(s), each with a notice link")
    st, res = a.per_url("N13", n13)
    nf = sum(len(pii_forms(pg)) for pg in a.pages.values())
    nbad = sum(1 for pg in a.pages.values() for f in pii_forms(pg) if not co._has_notice_link(f))
    if st == NA:
        a.put("N13", NA, "No form on the crawled pages collects personal data, so no consent request needs a notice beside it.", basis="Observed URL by URL")
    else:
        a.put("N13", st, f"{A.summary(res)}; {nf - nbad} of {nf} data-collecting forms carry a link to the notice or name it beside the request.",
              [f"{r.url}: {r.note}" for r in res if r.status == FAIL][:MAX_EV], urls=res, basis="Observed URL by URL")

    full_pol = a.text(PRIVACY, TERMS)
    existing = re.search(r"[^.\n]{0,160}\b(existing|current|earlier|previous(ly)?|prior)\b[^.\n]{0,80}\b(users?|customers?|data principals?|consents?|collected)\b[^.\n]{0,160}", full_pol, re.I)
    told = existing and re.search(r"notif|inform|send (you )?(a )?notice|tell", existing.group(0), re.I)
    if told:
        a.put("N14", PASS, "The policy says people whose data was collected earlier are notified.", [f'"{existing.group(0).strip()[:300]}"'], refs=list(a.ctx.refs(PRIVACY)),
              basis="Read from the published policy")
    elif existing and re.search(r"before|commence|in force|existing", existing.group(0), re.I):
        a.put("N14", PARTIAL, "The policy refers to existing users or earlier consent without saying they are notified.", [f'"{existing.group(0).strip()[:300]}"'], refs=list(a.ctx.refs(PRIVACY)),
              basis="Read from the published policy")
    else:
        a.absent("N14", "notifying people whose data was collected before the Act's notice duty applies", (PRIVACY, TERMS))


def consent(a: A) -> None:
    from app.rules.tracker_catalog import match_cmp                   # the 5-page agent's consent-tool catalogue, used by import only

    acc_ref, acc = a.consent("accept")
    rej_ref, rej = a.consent("reject")
    rp_ref, rp = a.consent("reject_persistence")
    wd_ref, wd = a.consent("withdrawal")
    pur_ref, pur = a.consent("purposes")
    rc_ref, recheck = a.consent("pre_consent_recheck")
    tracking_anywhere = any(n.get("non_essential") for _, n in a.ctx.loaded.networks) or bool(acc.get("non_essential")) or bool(wd.get("accepted_non_essential")) \
        or any(tracking_cookie(c) for pg in a.pages.values() for c in pg["cookies"]) or any(tracking_cookie(c) for c in acc.get("cookies") or [])
    banner = _banner_core(a.m.banner_text or "")
    no_tracking = "The site was not seen running any analytics or advertising service, before or after a choice, so there is nothing to consent to or refuse."

    def cmp_on(pg):
        urls = pg["net"].get("script_urls") or []
        hit = next((u for u in urls if match_cmp(script_src=u) is not None), None)
        return hit
    if not tracking_anywhere:
        a.put("C01", NA, no_tracking, basis="Observed URL by URL")
    else:
        st, res = a.per_url("C01", lambda pg: (PASS, f"consent tool loaded ({urlparse(cmp_on(pg)).hostname})") if cmp_on(pg) else (FAIL, "no consent tool loaded"))
        tools = sorted({urlparse(cmp_on(pg)).hostname or "" for pg in a.pages.values() if cmp_on(pg)})
        a.put("C01", st, f"{A.summary(res)}: a consent tool loaded on the page" + (f" ({', '.join(tools)})." if tools else "."),
              [f"{r.url}: {r.note}" for r in res if r.status != PASS][:MAX_EV] or [f"Consent tool script on every page: {', '.join(tools)}"], urls=res, basis="Observed URL by URL")

    def c02(pg):
        fs = pii_forms(pg)
        if not fs:
            return None, ""
        bad = [f for f in fs if not f.get("consent_controls")]
        return (FAIL, f"{len(bad)} form(s) with no consent control" + (" (relies on 'by submitting')" if any(co._BY_SUBMIT.search(" ".join([f.get('text') or '', f.get('label_text') or ''])) for f in bad) else "")) \
            if bad else (PASS, f"{len(fs)} form(s), each with a consent checkbox")
    st, res = a.per_url("C02", c02)
    nf = sum(len(pii_forms(pg)) for pg in a.pages.values())
    nctl = sum(1 for pg in a.pages.values() for f in pii_forms(pg) if f.get("consent_controls"))
    wording = next((f"Consent wording beside a form: \"{(c.get('label') or '')[:200]}\"" for pg in a.pages.values() for f in pii_forms(pg) for c in f.get("consent_controls") or [] if c.get("label")), None)
    if st == NA:
        a.put("C02", NA, "No form on the crawled pages collects personal data.", basis="Observed URL by URL")
    else:
        a.put("C02", st, f"{A.summary(res)}; {nctl} of {nf} data-collecting forms carry a consent checkbox the person ticks.", [f"{r.url}: {r.note}" for r in res if r.status == FAIL][:5] + [wording],
              urls=res, basis="Observed URL by URL")

    def c03(pg):
        fs = pii_forms(pg)
        if not fs:
            return None, ""
        pre = [x for f in fs for x in f.get("preticked") or []]
        return (FAIL, f"{len(pre)} control(s) ticked on load") if pre else (PASS, "nothing ticked on load")
    st, res = a.per_url("C03", c03)
    if st == NA:
        a.put("C03", NA, "No form on the crawled pages collects personal data.", basis="Observed URL by URL")
    else:
        a.put("C03", st, f"{A.summary(res)}: no consent checkbox is ticked when the page loads." if st == PASS else f"{A.summary(res)}: some consent controls are ticked when the page loads.",
              [f"{r.url}: {r.note}" for r in res if r.status == FAIL][:MAX_EV], urls=res, basis="Observed URL by URL")

    purposes = [p for p in pur.get("purposes") or [] if p.get("label")]
    pref_words = re.search(r"\b(preferences|customi[sz]e|manage (cookies|preferences|choices|options)|cookie settings|settings)\b", banner, re.I)
    cats_in_policy = a.of("cookie_declared", COOKIE, PRIVACY, pattern=r"analytic|statistic|marketing|advertis|functional|preference")
    if not tracking_anywhere:
        a.put("C04", NA, no_tracking, basis="Observed (consent test)")
    elif pur.get("tested") and len(purposes) >= 2:
        a.put("C04", PASS, f"The preferences layer was opened and offers a separate choice per purpose: {', '.join(p['label'].split(' ')[0] for p in purposes)}.",
              [f"Purpose: {p['label'][:100]}" for p in purposes[:4]], refs=[pur_ref] if pur_ref else [], basis="Observed (consent test)")
    elif pref_words or cats_in_policy:
        a.put("C04", PARTIAL, "A preferences control exists" + (" and the policy lists cookie categories" if cats_in_policy else "") + ", but the scan could not open per-purpose toggles to confirm them.",
              [f"Banner control: \"{pref_words.group(0)}\"" if pref_words else "", cite(cats_in_policy[0]) if cats_in_policy else "", f"Consent test 'purposes': {pur.get('reason') or 'not run'}"],
              refs=[r for r in (pur_ref,) if r], basis="Observed (banner and consent test)", review=True,
              limitation="A reviewer should open the preferences layer and confirm a toggle per purpose; if present, this becomes Pass.")
    else:
        a.put("C04", FAIL, "The consent request is all-or-nothing: no per-purpose choice was found.", [f"Consent test 'purposes': {pur.get('reason') or 'not run'}"], basis="Observed (banner and consent test)")

    # 3.0.2: a further page that did not load at all (an HTTP error such as 404) is not a cookie wall: a cookie wall serves the page and covers it. Only pages that loaded count,
    # for C05 and C14 alike; the ones that failed are named so the reader sees why they were left out.
    further_all = rp.get("further_pages") or []
    further = [x for x in further_all if x.get("loaded")]
    unloaded = [x.get("url") for x in further_all if not x.get("loaded")]
    unloaded_note = (f"{len(unloaded)} further page(s) could not be loaded at all after Reject ({', '.join(u for u in unloaded[:2] if u)}); a page that does not load is an HTTP error, "
                     "not a cookie wall, and is left out.") if unloaded else None
    if not tracking_anywhere:
        a.put("C05", NA, no_tracking, basis="Observed (consent test)")
    elif further:
        a.put("C05", PASS, "After Reject, further pages loaded and worked: no cookie wall.", [f"After Reject: {x.get('url')} loaded=True" for x in further],
              refs=[rp_ref] if rp_ref else [], basis="Observed (consent test)", limitation=unloaded_note)
    else:
        a.put("C05", PASS, f"Every one of the {len(a.pages)} crawled pages was read without any consent being given: access does not depend on consenting.",
              [f"{len(a.pages)} pages inspected in a browser that gave no consent"], basis="Observed (crawl)", limitation=unloaded_note)

    v2c9 = a.v2.get("C9")
    form_urls = [UrlCheck(pg["url"], PASS, ", ".join(sorted({k for f in pii_forms(pg) for k in f.get("collects", [])}))) for pg in a.pages.values() if pii_forms(pg)]
    if not form_urls:
        a.put("C06", NA, "No form on the crawled pages collects personal data.", basis="Observed URL by URL")
    elif v2c9 is not None and v2c9.state in ("met", "partly_met", "not_met"):
        st = _V2_STATUS[v2c9.state]
        a.put("C06", st, f"Each distinct form was compared with its evident purpose: {v2c9.note[:220]}", [f"Enterprise v2 C9: {v2c9.note[:260]}"], refs=list(v2c9.evidence_refs),
              basis="Model reading of every distinct form (flagged for human review)", urls=form_urls if st == PASS else [], review=True)
    else:
        a.put("C06", PARTIAL, "The forms could not be compared with their purposes by the model; a person should check them.", [f"{u.url}: {u.note}" for u in form_urls[:4]], basis="Observed (forms)",
              review=True, limitation="A reviewer should confirm each form asks only for what its purpose needs; if so this becomes Pass.")

    has_banner = bool(re.search(r"cookie|consent|accept|reject|decline", banner, re.I))
    # 3.0.2: the consent tests open the live banner in their own browsers. When they found and used it (Accept or Reject clicked, the purposes listed, the choice reopened) but the
    # homepage text did not capture its wording (a banner drawn late or in a closed shadow root), the banner exists: the checks below never report "no banner" against the tests' own result.
    banner_seen = has_banner or bool(acc.get("tested") or rej.get("tested") or pur.get("tested") or wd.get("reopen_control"))
    banner_unread = banner_seen and not has_banner
    unread_why = "A consent banner was found and used by the consent tests, but its wording could not be captured from the page"
    if not tracking_anywhere and not banner_seen:
        a.put("C07", NA, no_tracking, basis="Observed (banner)")
    elif has_banner and re.search(r"privacy|cookie (policy|notice)", banner, re.I):
        a.put("C07", PASS, "The consent banner refers the person to the privacy or cookie notice.", [f'Banner: "{_banner_snippet(banner)}"'], basis="Observed (banner text)")
    elif has_banner:
        a.put("C07", FAIL, "The consent banner does not refer to the notice.", [f'Banner: "{_banner_snippet(banner)}"'], basis="Observed (banner text)")
    elif banner_unread and not a.has_policy():
        a.put("C07", FAIL, "The consent banner was found and used by the consent tests, but the site publishes no privacy or cookie notice for it to link to.", basis="Observed (consent test)")
    elif banner_unread:
        a.put("C07", NA, f"{unread_why}, so whether it links to the notice could not be checked.", basis="Observed (consent test)", review=True, applicability=cat.HUMAN_REVIEW,
              limitation="A reviewer should open the banner and confirm it links to the privacy or cookie notice.")
    else:
        a.put("C07", FAIL, "Analytics or advertising runs, but no consent banner was found to link to the notice.", basis="Observed (banner text)")

    j = a.m.judgments.get("C1") or {}
    if j.get("verdict") in ("yes", "partly", "no"):
        st = {"yes": PASS, "partly": PARTIAL, "no": FAIL}[j["verdict"]]
        a.put("C08", st, f"The model's reading of the consent wording: {j.get('reason', '')[:240]}", [f"Model reading ({j.get('confidence')} confidence) of the consent requests"],
              basis="Model reading of the consent wording (flagged for human review)", review=True)
    elif has_banner:
        a.put("C08", PARTIAL, "The banner's wording was not read by the model; a person should confirm it is plain and specific.", [f'Banner: "{_banner_snippet(banner)}"'],
              basis="Observed (banner text)", review=True, limitation="Held at Partial until a person confirms the wording.")
    elif banner_unread:
        a.put("C08", PARTIAL, f"{unread_why}; a person should confirm it is plain and specific.", basis="Observed (consent test)", review=True,
              limitation="Held at Partial until a person confirms the wording.")
    elif not tracking_anywhere:
        a.put("C08", NA, "No consent request (banner or form) was found, and none is needed: no analytics or advertising runs.", basis="Observed")
    else:
        a.put("C08", FAIL, "No consent request was found where one is needed.", basis="Observed")

    langs = re.search(r"(?:\b(?:english|hindi|हिंदी|বাংলা|தமிழ்|తెలుగు|ಕನ್ನಡ|മലയാളം|ગુજરાતી|ਪੰਜਾਬੀ|ଓଡ଼ିଆ|मराठी|اردو)\b.*){3,}", (a.m.banner_text or "").split("[shadow DOM]")[-1][-6000:], re.I | re.S)
    reqs = co.consent_requests(a.ctx)
    if banner_unread and not reqs:
        a.put("C09", NA, f"{unread_why}, so its language could not be checked.", basis="Observed (consent test)", review=True, applicability=cat.HUMAN_REVIEW,
              limitation="A reviewer should confirm the banner is in English or an Eighth Schedule language.")
    elif has_banner or reqs:
        script = tr.detect_language(banner + " " + " ".join(" ".join([f.get("text") or "", f.get("label_text") or ""]) for _, _, f in reqs[:20]))["language"]
        if script in ("en", "eighth_schedule"):
            a.put("C09", PASS, "The consent request is in English" + (", with a selector for Eighth Schedule languages." if langs else "."),
                  [f"Banner language selector: {' '.join(langs.group(0).split())[:200]}"] if langs else [f'Banner: "{_banner_snippet(banner)}"' if has_banner else "Form consent wording in English"],
                  basis="Observed (banner)")
        else:
            a.put("C09", FAIL, "The consent request is neither in English nor in an Eighth Schedule language.", basis="Observed (banner)")
    elif not tracking_anywhere:
        a.put("C09", NA, "No consent request was found, and none is needed: no analytics or advertising runs.", basis="Observed")
    else:
        a.put("C09", FAIL, "No consent request was found where one is needed.", basis="Observed")

    officer = co._OFFICER
    contact = lambda s: bool(tr.EMAIL.search(s) or tr.PHONE.search(s) or tr.URL.search(s))          # noqa: E731
    have, lack = [], []
    for e, u, f in reqs:
        b = " ".join([f.get("text") or "", f.get("label_text") or ""])
        (have if officer.search(b) and contact(b) else lack).append(u)
    banner_has = bool(has_banner and officer.search(banner) and contact(banner))
    total = len(reqs) + (1 if has_banner else 0)
    good = len(have) + (1 if banner_has else 0)
    footer = sum(1 for pg in a.pages.values() if (a.m.html.get(pg["url"]) and a.m.html[pg["url"]].emails & _contact_emails(a)))
    if banner_unread and not reqs:
        a.put("C10", NA, f"{unread_why}, so whether it gives the contact of a DPO or a person who answers questions could not be checked.", basis="Observed (consent test)",
              review=True, applicability=cat.HUMAN_REVIEW, limitation="A reviewer should open the banner and its preferences and confirm the contact given there belongs to this organisation.")
    elif total == 0:
        a.put("C10", NA if not tracking_anywhere else FAIL, "No consent request was found." if not tracking_anywhere else "No consent request was found where one is needed.", basis="Observed")
    else:
        st = PASS if good == total else PARTIAL if good else FAIL
        a.put("C10", st, f"{good} of {total} consent request(s) (banner and forms) give the contact of a DPO or a person who answers questions." +
              (f" The contact is printed elsewhere on {footer} page(s) (the footer), but not in or beside the consent request." if st != PASS and footer else ""),
              [f"Requests without a contact, e.g. {sorted(set(lack))[0]}" if lack else "", "Banner: no contact" if has_banner and not banner_has else ""],
              basis="Observed (banner and forms)", review=st != PASS and bool(footer),
              limitation="A reviewer may accept a contact on the same page as accompanying the request." if st != PASS and footer else None)

    v2c12 = a.v2.get("C12")
    if v2c12 is not None and v2c12.state == "not_met":
        a.put("C11", FAIL, f"A consent term waives a right: {v2c12.note[:240]}", [v2c12.note[:300]], refs=list(v2c12.evidence_refs), basis="Read from the published policy")
    elif a.has_policy() or a.ctx.src(TERMS):
        un = a.unread_note(TERMS)
        a.put("C11", PASS, "No consent term waives a right or limits the complaint to the Board in the policy and terms read.", [f"Read: {u}" for u in a.policy_urls()[:2]],
              refs=list(a.ctx.refs(PRIVACY, TERMS)), basis="Read from the published policy", review=bool(un), limitation=un)
    else:
        a.put("C11", NA, "No privacy policy or terms were found, so there is no consent term to check.", basis="Read from the pages")

    # C12: before any choice, page by page; a page that did not fire again in clean reloads is recorded as such
    rechecked = {p["url"].rstrip("/"): p for p in recheck.get("pages") or [] if p.get("loaded")}

    def c12(pg):
        services: dict[str, list[str]] = {}
        for c in pg["cookies"]:
            if tracking_cookie(c):
                services.setdefault(_cookie_service(c), []).append(c["name"])
        for x in pg["net"].get("non_essential") or []:
            services.setdefault(x.get("vendor") or x.get("host"), [])
        hits = [f"{k} ({', '.join(sorted(set(v))[:4])} cookie{'s' if len(set(v)) > 1 else ''})" if v else f"{k} (request)" for k, v in sorted(services.items())]
        if not hits:
            return PASS, "nothing analytics or advertising before a choice"
        r = rechecked.get(pg["url"].rstrip("/"))
        if r is not None and not r.get("fired"):
            return PASS, f"seen once during the crawl ({', '.join(hits[:3])}), not repeated in {r.get('loads', 1)} clean reload(s)"
        return FAIL, "before a choice: " + ", ".join(hits[:8]) + (f" (repeated in {r.get('fired_loads')} of {r.get('loads')} clean reloads)" if r else "")
    if not tracking_anywhere:
        a.put("C12", PASS, f"No analytics or advertising cookie or request was seen before a choice on any of the {len(a.pages)} pages.", basis="Observed URL by URL",
              urls=[UrlCheck(u, PASS, "nothing before a choice") for u in a.pages])
    else:
        st, res = a.per_url("C12", c12)
        bad = [r for r in res if r.status == FAIL]
        vendors = Counter(lbl for r in bad for lbl in {h.split(" (")[0] for h in re.split(r", (?![^()]*\))", r.note.split(" (repeated")[0].replace("before a choice: ", ""))})
        not_repeated = [r for r in res if "not repeated" in r.note]
        a.put("C12", st, f"{A.summary(res)}." + (f" Before any choice: {', '.join(f'{v} ({n} pages)' for v, n in vendors.most_common(5))}." if bad else "") +
              (f" {len(not_repeated)} page(s) fired once during the crawl but not in clean reloads." if not_repeated else ""),
              [f"{r.url}: {r.note}" for r in bad[:4]] + [f"{r.url}: {r.note}" for r in not_repeated[:2]], refs=[rc_ref] if rc_ref else [], urls=res, basis="Observed URL by URL",
              review=bool(not_repeated) or bool(bad and not recheck),
              limitation=None if recheck else ("Observed in the scanner's fresh browser; a clean reload was not run for this scan." if bad else None))

    if not tracking_anywhere:
        for pid in ("C13", "C14", "C16", "C17"):
            a.put(pid, NA, no_tracking, basis="Observed (consent test)")
    else:
        after = rej.get("non_essential") or []
        ck_after = [c for c in rej.get("cookies") or [] if tracking_cookie(c)]
        if not rej.get("tested"):
            a.put("C13", FAIL, f"Reject could not be exercised: {rej.get('reason') or 'no reject control was found'}.", refs=[rej_ref] if rej_ref else [], basis="Observed (consent test)")
        elif not after and not ck_after:
            a.put("C13", PASS, "After Reject in a fresh browser, no analytics or advertising cookie or request was seen.", [f"After Reject on {rej.get('page_url')}: nothing non-essential"],
                  refs=[rej_ref] if rej_ref else [], basis="Observed (consent test)")
        elif not after:
            a.put("C13", PARTIAL, "After Reject no analytics or advertising request was made, but tracking cookies set before the choice were not removed.",
                  [f"Cookies still present: {', '.join(sorted({c['name'] for c in ck_after}))}"], refs=[rej_ref] if rej_ref else [], basis="Observed (consent test)")
        else:
            a.put("C13", FAIL, f"After Reject, {', '.join(sorted({x.get('vendor') or x.get('host') for x in after}))} was still contacted.",
                  [f"After Reject: {', '.join(sorted({x.get('host') for x in after}))}", f"Cookies present: {', '.join(sorted({c['name'] + ' (' + c['domain'] + ')' for c in ck_after}))}" if ck_after else ""],
                  refs=[rej_ref] if rej_ref else [], basis="Observed (consent test)")
        if not rp.get("tested"):
            a.put("C14", FAIL, f"Reject could not be carried to further pages: {rp.get('reason') or 'not tested'}.", basis="Observed (consent test)")
        elif not further:
            a.put("C14", NA, "None of the further pages chosen for the test loaded after Reject (HTTP errors), so whether the refusal holds on other pages could not be observed.",
                  [f"Not loaded: {u}" for u in unloaded[:3] if u], refs=[rp_ref] if rp_ref else [], basis="Observed (consent test)", review=True, applicability=cat.HUMAN_REVIEW,
                  limitation="A reviewer should open further pages after Reject and confirm no analytics or advertising runs.")
        else:
            leaky = [x for x in further if x.get("non_essential")]
            st = PASS if not leaky else PARTIAL if len(leaky) < len(further) else FAIL
            a.put("C14", st, "The refusal held on every further page." if st == PASS else f"On {len(leaky)} of {len(further)} further page(s) loaded after Reject, tracking ran again.",
                  [f"{x.get('url')}: {', '.join(sorted({y.get('vendor') or y.get('host') for y in x.get('non_essential') or []})) or 'nothing non-essential'}" for x in further],
                  refs=[rp_ref] if rp_ref else [], basis="Observed (consent test)")
        if wd.get("reopen_control") and wd.get("withdraw_control"):
            a.put("C16", PASS, f"After Accept, the choice was reopened ('{wd['reopen_control']}') and withdrawn ('{wd['withdraw_control']}').", [f"reopen: {wd['reopen_control']}; withdraw: {wd['withdraw_control']}"],
                  refs=[wd_ref] if wd_ref else [], basis="Observed (consent test)")
        elif wd.get("reopen_control"):
            a.put("C16", PARTIAL, f"The choice can be reopened ('{wd['reopen_control']}') but withdrawal was not available in one step.", refs=[wd_ref] if wd_ref else [], basis="Observed (consent test)")
        else:
            a.put("C16", FAIL, f"No control to reopen the choice after Accept was found ({wd.get('reason') or 'withdrawal not tested'}).", refs=[wd_ref] if wd_ref else [], basis="Observed (consent test)")
        started = {x.get("vendor") or x.get("host") for x in wd.get("accepted_non_essential") or []}
        later = {y.get("vendor") or y.get("host") for x in wd.get("further_pages") or [] for y in x.get("non_essential") or []}
        on_page = {x.get("vendor") or x.get("host") for x in wd.get("after_withdrawal") or []}
        cont = (on_page | later) & started if started else on_page | later
        if not wd.get("tested"):
            a.put("C17", FAIL, f"Withdrawal could not be exercised, so processing cannot be shown to stop ({wd.get('reason') or 'not tested'}).", refs=[wd_ref] if wd_ref else [], basis="Observed (consent test)")
        elif not started and not (on_page | later):
            a.put("C17", NA, "Accept started no analytics or advertising service, so there was nothing for withdrawal to stop.", ["Started by Accept: none"],
                  refs=[wd_ref] if wd_ref else [], basis="Observed (consent test)")
        elif not (on_page | later):
            a.put("C17", PASS, f"After withdrawal, none of the {len(started)} services started by Accept was contacted, on the page or on further pages.", [f"Started by Accept: {', '.join(sorted(started))}"],
                  refs=[wd_ref] if wd_ref else [], basis="Observed (consent test)")
        else:
            st = FAIL if started and cont >= started else PARTIAL
            a.put("C17", st, f"After withdrawal {len(cont)} of the {len(started)} services started by Accept were still contacted ({', '.join(sorted(cont))})" +
                  (" on the page itself" if on_page and not later else " on the page or the further pages loaded after it") + ".",
                  [f"Started by Accept: {', '.join(sorted(started))}", f"Same page after withdrawal: {', '.join(sorted(on_page)) or 'none'}"] +
                  [f"{x.get('url')} after withdrawal: {', '.join(sorted({y.get('vendor') or y.get('host') for y in x.get('non_essential') or []})) or 'none'}" for x in wd.get("further_pages") or []],
                  refs=[wd_ref] if wd_ref else [], basis="Observed (consent test)")

    # C15: Reject as easy as Accept
    ctl = banner
    acc_w = re.search(r"\baccept( all)?\b|\ballow( all)?\b|\bagree\b|\bi accept\b|\bok\b", ctl, re.I)
    rej_w = re.search(r"\breject( all)?\b|\bdecline\b|\bdeny\b|\brefuse\b|\bdisagree\b|\bonly (necessary|essential)\b|\bnecessary only\b", ctl, re.I)
    if not tracking_anywhere and not banner_seen:
        a.put("C15", NA, no_tracking, basis="Observed (banner)")
    elif acc_w and rej_w:
        a.put("C15", PASS, f"'{acc_w.group(0)}' and '{rej_w.group(0)}' are both on the banner's first layer.", [f'Banner: "{_banner_snippet(banner)}"'], basis="Observed (banner text)")
    elif rej.get("tested") and rej.get("clicked") != "preferences":
        a.put("C15", PASS, "A reject control was found and clicked on the banner, in a fresh browser, as Accept was.", ["Reject test: control found and clicked"],
              refs=[rej_ref] if rej_ref else [], basis="Observed (consent test)", review=True,
              limitation="A reviewer may compare how prominent Reject is against Accept (size, colour).")
    elif pref_words:
        a.put("C15", PARTIAL, "Refusing is possible only one layer deeper, in the preferences.", [f'Banner: "{_banner_snippet(banner)}"'], basis="Observed (banner text)")
    else:
        a.put("C15", FAIL, "No way to refuse was found on the banner.", [f'Banner: "{_banner_snippet(banner)}"'] if has_banner else [], basis="Observed (banner text)")

    rec = a.of("consent_record", PRIVACY, COOKIE, TERMS)
    if rec:
        a.put("C18", PASS, "The policy says consent records are kept.", [cite(f) for f in rec[:2]], refs=refs_of(rec), basis="Read from the published policy")
    else:
        a.absent("C18", "keeping records of consent", (PRIVACY, COOKIE, TERMS))


def _home(a: A) -> str:
    want = a.m.root_url.rstrip("/")
    return next((u for u in a.m.five_page if u.rstrip("/") == want), a.m.root_url)


_EMBEDDED = ((r"(^|\.)loom\.com$", "Loom"), (r"(^|\.)(youtube\.com|youtube-nocookie\.com|ytimg\.com)$", "YouTube"), (r"(^|\.)(vimeo\.com|vimeocdn\.com)$", "Vimeo"),
             (r"(^|\.)calendly\.com$", "Calendly"), (r"(^|\.)typeform\.com$", "Typeform"), (r"(^|\.)(hubspot\.com|hsforms\.net|hs-scripts\.com)$", "HubSpot"),
             (r"(^|\.)(intercom\.io|intercomcdn\.com)$", "Intercom"), (r"(^|\.)tawk\.to$", "Tawk.to"), (r"(^|\.)(zendesk\.com|zdassets\.com)$", "Zendesk"),
             (r"(^|\.)(drift\.com|driftt\.com)$", "Drift"), (r"(^|\.)(crisp\.chat)$", "Crisp"), (r"(^|\.)(wistia\.com|wistia\.net)$", "Wistia"),
             (r"(^|\.)(soundcloud\.com)$", "SoundCloud"), (r"(^|\.)(spotify\.com)$", "Spotify"), (r"(^|\.)(maps\.googleapis\.com|maps\.gstatic\.com)$", "Google Maps"),
             (r"(^|\.)(platform\.twitter\.com|twimg\.com)$", "X (Twitter)"), (r"(^|\.)(connect\.facebook\.net)$", "Meta"), (r"(^|\.)(cdninstagram\.com)$", "Instagram"),
             (r"(^|\.)(disqus\.com|disquscdn\.com)$", "Disqus"), (r"(^|\.)(jotform\.com)$", "Jotform"), (r"(^|\.)(forms\.office\.com)$", "Microsoft Forms"))


def _embedded_service(host: str) -> str | None:
    """The embedded third-party service (video player, booking, form, chat or social widget) a host belongs to, or None. Such a service receives the visitor's data, so the notice must name it."""
    for rx, name in _EMBEDDED:
        if re.search(rx, host or "", re.I):
            return name
    return None


def _cookie_service(c: dict) -> str:
    """A cookie's service in words: its provider, and for a third-party cookie also the site that set it when the provider's name does not say so
    (ajs_anonymous_id on loom.com is Loom's own cookie, written by the Segment library: 'Segment (set by loom.com)')."""
    prov = describe(c).get("provider") or ""
    dom = (c.get("domain") or "").lstrip(".").lower()
    if not prov:
        return dom or "unidentified"
    label = dom.split(".")[-2] if dom.count(".") >= 1 else dom
    if not c.get("first_party") and dom and label and label not in prov.lower().replace(" ", ""):
        return f"{prov} (set by {dom})"
    return prov


_BANNER_START = re.compile(r"we use cookies|this (web)?site uses cookies|cookie (preferences|settings|consent|notice)|your privacy choices|manage consent", re.I)


def _banner_core(text: str) -> str:
    """The consent banner's own words: the window around its heading in the homepage's shadow-DOM or control text, without the style sheet a consent tool ships beside it.
    Empty when no banner wording is found (the page menu and footer are not the banner)."""
    hits = list(_BANNER_START.finditer(text))
    if not hits:
        return ""
    m = hits[-1]                                                # the drawn banner comes after the tool's style sheet
    window = text[text.rfind("\n", 0, m.start()) + 1:m.end() + 900]            # from the heading's own line: the page menu above it is not the banner
    lines = [ln.strip() for ln in window.splitlines() if ln.strip() and not re.search(r"[{};]|^\.|^\[dir=|^@media", ln.strip())]
    return "\n".join(lines)


def _banner_snippet(banner: str) -> str:
    m = re.search(r"[^\n]{0,40}(we use cookies|cookies?|consent)[^\n]{0,260}", banner, re.I)
    return " ".join((m.group(0) if m else banner[:260]).split())[:300]


def _contact_emails(a: A) -> set[str]:
    found: set[str] = set()
    for f in a.of("dpo_contact") + a.of("grievance_mechanism") + a.of("exercise_channel"):
        found |= {e.lower() for e in tr.EMAIL.findall(blob(f))}
    on_pages = set().union(*(h.emails for h in a.m.html.values())) if a.m.html else set()
    # an address glued to the word before it in the extracted text ("Officersaurabhk@...") is matched back to the address the pages really show
    fixed = {next((p for p in on_pages if e.endswith(p)), e) for e in found}
    return {e for e in fixed if e in on_pages} or fixed


def lawful(a: A) -> None:
    grounds = a.of("legal_ground", PRIVACY, TERMS)
    specific = [f for f in grounds if f.get("specific")]
    a.policy_result("L01", specific if len(specific) >= 2 else [], grounds, "the ground relied on for processing (consent or a legitimate use)",
                    full_note=f"The ground is stated for each processing ({len(specific)} grounds tied to their purposes).",
                    part_note="A ground is stated, but not tied to each purpose.", cats=(PRIVACY, TERMS))
    good = [f for f in grounds if re.search(r"voluntar|legal obligation|compl(y|iance) with (the |applicable )?law|required by law|court|judgment|medical|emergenc|disaster|employ|"
                                             r"state|subsid|section 7|legitimate use", blob(f), re.I)]
    bad = [f for f in grounds if re.search(r"legitimate (interest|business|operational)|our legitimate|performance of (a |the )?contract", blob(f), re.I)]
    nonconsent = a.of("purpose", PRIVACY, pattern=r"legal obligation|comply with (the )?law|fraud|security|tax|statutory|court|regulat")
    if good and not bad:
        a.put("L02", PASS, "Processing without consent is tied to the legitimate uses of s.7.", [cite(f) for f in good[:3]], refs=refs_of(good), basis="Read from the published policy")
    elif bad:
        a.put("L02", PARTIAL, "A ground relied on is not one of the s.7 legitimate uses (" + bad[0]["value"][:80] + ").", [cite(f) for f in bad[:2]] + [cite(f) for f in good[:1]],
              refs=refs_of(bad + good), basis="Read from the published policy", review=True)
    elif nonconsent:
        a.put("L02", PARTIAL, "Purposes that do not rest on consent appear, without naming the s.7 legitimate use relied on.", [cite(f) for f in nonconsent[:2]], refs=refs_of(nonconsent),
              basis="Read from the published policy", review=True)
    elif a.has_policy():
        a.put("L02", NA, "The policy describes no processing without consent, so no legitimate use needs to be identified.", [f"Read: {u}" for u in a.policy_urls()[:1]],
              basis="Read from the published policy")
    else:
        a.absent("L02", "the legitimate uses relied on")


_SEC = {"F04": ("encryption, masking or tokenisation", r"encrypt|mask|tokeni|pseudonym|hash|cryptograph|ssl|tls"),
        "F05": ("access control", r"access control|restrict\w* access|authori[sz]ed (personnel|staff|employees|persons)|need[- ]to[- ]know|role[- ]based|least privilege|multi[- ]factor|\bmfa\b|2fa"),
        "F06": ("logging, monitoring and review", r"\blogs?\b|logging|monitor|audit trail|audit log|detect|intrusion|alert|investigat"),
        "F07": ("backups and continuity", r"back[- ]?ups?|recover|continu|redundan|disaster|resilien")}
_CERT = re.compile(r"iso[/ -]*(iec)?[ -]*27001|soc ?2|iso 27701|certified", re.I)


def fiduciary(a: A) -> None:
    cats = (PRIVACY, GRIEVANCE, TERMS)
    contracts = sorted(a.of("processor_contract", PRIVACY, TERMS), key=lambda f: 0 if re.search(r"contract|agreement|bound", blob(f), re.I) else 1)
    recips = a.of("sharing_recipient", PRIVACY, COOKIE)
    if contracts:
        a.put("F01", PASS, "Processors are engaged under a contract or agreement.", [cite(f) for f in contracts[:2]], refs=refs_of(contracts), basis="Read from the published policy")
    elif recips:
        a.put("F01", PARTIAL, "Processors or recipients are named, but not said to act under a contract.", [cite(f) for f in recips[:2]], refs=refs_of(recips), basis="Read from the published policy")
    else:
        a.absent("F01", "processors acting under a contract", (PRIVACY, TERMS))

    # the organisation's own duty (s.8(3)), not the person's right to correction, a cookie table kept current, or a warranty disclaimer
    acc = [f for f in a.of("data_accuracy", PRIVACY, TERMS)
           if re.search(r"\b(we|our|the company)\b[^.]{0,80}\b(keep|maintain|ensure|take (all )?(reasonable )?steps|strive|endeavou?r)\w*[^.]{0,80}(accura|complete|up[- ]to[- ]date|consisten)", blob(f), re.I)
           and not re.search(r"\byou (may|can|have the right|are entitled)|right to (correct|rectif|update)|upon request|on request|cookie|warrant", blob(f), re.I)]
    if acc:
        a.put("F02", PASS, "The policy states the organisation keeps personal data accurate and complete.", [cite(f) for f in acc[:2]], refs=refs_of(acc), basis="Read from the published policy")
    else:
        rights = a.of("right_correction")
        a.absent("F02", "the organisation's own duty to keep personal data accurate, complete and consistent", (PRIVACY, TERMS))
        if rights:
            r = a.out["F02"]
            r.explanation = "The policy gives people the right to correct their data, but does not state the organisation's own duty to keep it accurate, complete and consistent."
            r.evidence = [cite(rights[0])]
            r.review = True

    scats = (PRIVACY, GRIEVANCE, COOKIE)
    sec = a.of("security", *scats) or a.of("security", *cats)
    pool = sec + a.of("retention", *scats) + a.of("log_retention", *scats)
    found = {pid: [f for f in pool if re.search(rx, blob(f), re.I)] for pid, (_, rx) in _SEC.items()}
    found["F07"] = found["F07"] or a.sentences(r"\bback[- ]?ups?\b[^.\n]{0,120}(encrypt|cycle|days|retain|kept|daily|weekly|rolling)", *cats)
    measures = [pid for pid, fs in found.items() if fs]
    certs = [f for f in sec if _CERT.search(blob(f))] or ([] if not _CERT.search(a.text(PRIVACY)) else [])
    if len(measures) >= 2:
        a.put("F03", PASS, f"Technical and organisational measures are described ({', '.join(_SEC[p][0] for p in measures)}).", [cite(found[p][0]) for p in measures[:3]],
              refs=refs_of([f for p in measures for f in found[p]]), basis="Read from the published policy")
    elif sec:
        a.put("F03", PARTIAL, "Only a general statement of safeguards is given.", [cite(f) for f in sec[:2]], refs=refs_of(sec), basis="Read from the published policy")
    else:
        a.absent("F03", "the security safeguards used", cats)
    for pid, (what, _) in _SEC.items():
        if found[pid]:
            a.put(pid, PASS, f"{what[:1].upper() + what[1:]} is named.", [cite(f) for f in found[pid][:2]], refs=refs_of(found[pid]), basis="Read from the published policy")
        elif sec or certs:
            a.put(pid, PARTIAL, f"Only a general safeguard statement or a certification is given; {what} is not named.", [cite(f) for f in (sec or certs)[:2]], refs=refs_of(sec or certs),
                  basis="Read from the published policy")
        else:
            a.absent(pid, what, cats)

    logs = a.of("log_retention", *cats) + [f for f in a.of("security", *cats) + a.of("retention", *cats) if re.search(r"\blogs?\b", f["quote"], re.I)]
    year = [f for f in logs if any(d >= 365 for d in (tr.parse_days(f["value"]) or tr.parse_days(f["quote"]) or []))
            or re.search(r"\b(one|1|two|2|three|3|five|5|seven|7|ten|10)\s*\(?\d*\)?\s*years?\b|12 months|365 days|rule 8\(3\)", blob(f), re.I)]
    a.policy_result("F08", year, logs, "how long logs are kept", full_note="Logs are kept for one year or more.", part_note="Logs are mentioned without a period of at least one year.", cats=cats)

    safe = [f for f in contracts if re.search(r"secur|safeguard", blob(f), re.I) and not re.search(r"transfer|outside india|cross[- ]border|abroad", blob(f), re.I)]
    conf = contracts + [f for f in recips if re.search(r"confidential|contract|agreement", blob(f), re.I)]
    a.policy_result("F09", safe, conf, "processor contracts requiring security safeguards", full_note="Processor contracts are stated to require security safeguards.",
                    part_note="Contracts or confidentiality with processors are mentioned, but not that they require security safeguards.", cats=(PRIVACY, TERMS))

    breach = a.of("breach", *cats, pattern=r"breach|incident|compromis|unauthori[sz]ed")
    people = [f for f in breach if re.search(r"\b(you|affected|users?|individuals?|data principals?|customers?|persons?|people)\b", blob(f), re.I)
              and re.search(r"notif|inform|intimat|tell|communicat|alert", blob(f), re.I)]
    board = [f for f in breach if re.search(r"\bboard\b|authorit|regulator|cert-in|government", blob(f), re.I)]
    timed = [f for f in board if re.search(r"72|seventy[- ]two|without (undue )?delay|promptly|immediately|within \d+", blob(f), re.I)]
    a.policy_result("F10", people, breach, "notifying affected people of a personal data breach", full_note="The policy says each affected person is informed of a breach.",
                    part_note="A breach is mentioned, but not that affected people are told.", cats=cats)
    a.policy_result("F11", timed, board, "notifying the Data Protection Board of a breach", full_note="The policy describes intimation to the Board, with its timing.",
                    part_note="The Board is said to be informed, but not the process (without delay, a detailed report within 72 hours).", cats=cats)
    if not board and breach:
        a.out["F11"].explanation = "A breach is mentioned, but not that the Data Protection Board is told."
        a.out["F11"].status = FAIL


def retention(a: A) -> None:
    cats = (PRIVACY, GRIEVANCE, TERMS)
    ret = a.of("retention", *cats)
    purpose = [f for f in ret if re.search(r"as long as|no longer (needed|necessary|required)|purpose|necessary for", blob(f), re.I)] or \
        a.sentences(r"(retain|keep|store)\w*[^.\n]{0,80}(only )?(as long as|for as long as) (is )?(necessary|needed|required)|no longer (necessary|needed|required) for the purpose", *cats)
    withdraw = [f for f in ret if re.search(r"withdraw|unsubscri|opt[- ]?out|until you|revoke", blob(f), re.I)] + \
        [f for f in a.of("withdraw_consent", *cats) if re.search(r"delet|eras|remov", blob(f), re.I)]
    if purpose and withdraw:
        a.put("RT1", PASS, "Erasure is stated both when the purpose is served and when consent is withdrawn.", [cite(purpose[0]), cite(withdraw[0])], refs=refs_of(purpose + withdraw),
              basis="Read from the published policy")
    elif purpose or withdraw:
        a.put("RT1", PARTIAL, "Erasure is stated for " + ("the end of the purpose" if purpose else "withdrawal of consent") + " only.", [cite((purpose or withdraw)[0])],
              refs=refs_of(purpose or withdraw), basis="Read from the published policy")
    else:
        a.absent("RT1", "erasing data when the purpose is served or consent is withdrawn", cats)

    erase = [f for f in a.of("processor_contract", PRIVACY, TERMS) + ret if re.search(r"(processor|vendor|service provider|sub-?processor|third part)[^.]{0,120}(delet|eras|destroy|return)|"
                                                                                       r"(delet|eras|destroy|return)[^.]{0,120}(processor|vendor|service provider|sub-?processor)", blob(f), re.I)]
    if erase:
        a.put("RT2", PASS, "Processors are made to erase the data.", [cite(f) for f in erase[:2]], refs=refs_of(erase), basis="Read from the published policy")
    else:
        a.absent("RT2", "processors being made to erase the data", (PRIVACY, TERMS))

    has_forms = any(pii_forms(pg) for pg in a.pages.values())
    has_news = any(re.search(r"newsletter|subscribe|updates|marketing", " ".join([f.get("text") or "", f.get("label_text") or ""]), re.I) for pg in a.pages.values() for f in pii_forms(pg))
    has_analytics = any(n.get("non_essential") for _, n in a.ctx.loaded.networks) or bool(a.consent("accept")[1].get("non_essential"))
    need = [(k, rx) for k, rx, on in (("enquiry and form data", r"enquir|inquir|contact|form|lead|walkthrough|submission|request", has_forms),
                                      ("marketing and newsletter data", r"marketing|newsletter|subscri|communication", has_news),
                                      ("analytics and usage data", r"analytic|usage|log|cookie|traffic|tracking", has_analytics)) if on]
    periods = [f for f in ret if tr.parse_days(f["value"]) or tr.parse_days(f["quote"]) or re.search(r"until you|withdraw|\d+\s*(day|month|year)", blob(f), re.I)]
    covered = [(k, next((f for f in periods if re.search(rx, blob(f), re.I)), None)) for k, rx in need]
    have = [(k, f) for k, f in covered if f]
    if not ret:
        a.absent("RT3", "how long each kind of data is kept", cats)
    elif not need:
        st = PASS if periods else FAIL
        a.put("RT3", st, "Retention periods are given." if periods else "Retention is described without any period.", [cite(f) for f in (periods or ret)[:2]], refs=refs_of(periods or ret),
              basis="Read from the published policy")
    else:
        st = PASS if len(have) == len(need) else PARTIAL if have else FAIL
        a.put("RT3", st, f"A retention period is given for {len(have)} of the {len(need)} kinds of data the site collects" +
              (f"; none for {', '.join(k for k, f in covered if not f)}." if st != PASS else f" ({', '.join(k for k, _ in need)})."),
              [cite(f) for _, f in have[:3]], refs=refs_of([f for _, f in have]), basis="Read from the policy against the data observed")

    rt4(a)

    one = [f for f in ret + a.of("log_retention", *cats) if re.search(r"rule 8\(3\)|one year|1 year|12 months|365 days", blob(f), re.I) and
           re.search(r"log|traffic|personal data|processing", blob(f), re.I)]
    if one:
        a.put("RT5", PASS, "Keeping personal data, traffic data and logs for one year is stated.", [cite(f) for f in one[:2]], refs=refs_of(one), basis="Read from the published policy")
    else:
        a.absent("RT5", "keeping personal data, traffic data and logs for one year", cats)


_INACTIVE = (r"(inactiv|not (used|accessed|logged in|approached)|dormant|last (use|login|log-in|access)|three years|3 years)[^.]{0,200}(eras|delet)|"
             r"(eras|delet)[^.]{0,200}(inactiv|not (used|accessed|logged in|approached)|dormant|last (use|login|log-in|access)|three years|3 years)")
_NOTICE_48 = re.compile(r"48 hours|forty[- ]eight hours", re.I)


def rt4(a: A) -> None:
    """RT4, 3.0.1. Rule 8(1) and the Third Schedule cover only e-commerce entities, online gaming intermediaries and social media intermediaries with at least the notified
    number of registered users in India. What the site IS decides applicability, never whether its policy mentions erasing data; and the user threshold is a fact the website
    rarely shows, so an unconfirmed threshold is human review, never a Pass, a Fail or a confirmed Not applicable."""
    v2r1 = a.v2.get("R1")
    site = a.site
    plat = [c for c in THIRD_SCHEDULE if site.has(c)]
    if not plat:
        weak = [LABELS[c] for c in THIRD_SCHEDULE if c in site.possible]
        a.put("RT4", NA, f"Not a Third Schedule platform: the site reads as {LABELS[site.primary].lower()}, not as an e-commerce, online gaming or social media platform"
              + (f" (only one weak signal of {', '.join(weak)}, not enough to apply the duty)" if weak else "") + ".",
              [e for c in THIRD_SCHEDULE for e in site.evidence_for(c, 2)], basis="Applicability", applicability=cat.NOT_APPLICABLE,
              differs=(f"Enterprise v2 R1 applied the duty because the policy describes erasure ({(v2r1.note or '')[:140]}); the site is not a Third Schedule platform."
                       if v2r1 is not None and v2r1.state not in ("not_applicable", "not_assessed") else None))
        return
    ev = [e for c in plat for e in site.evidence_for(c, 3)]
    labels = " and ".join(LABELS[c] for c in plat)
    over = [(c, u) for c in plat for u in site.users_stated if u["count"] >= THIRD_SCHEDULE[c]]
    if not over:
        stated = site.users_stated[0] if site.users_stated else None
        a.put("RT4", NA, f"The site reads as {labels}, a kind of platform the Third Schedule covers, but the website does not show that it has at least "
              f"{' / '.join(THRESHOLD_LABEL[c] for c in plat)} registered users in India"
              + (f' (it states "{stated["text"]}", which does not settle the number of registered users in India)' if stated else "")
              + ". The organisation must confirm its registered users in India; until then this duty's marks are held back.",
              ev + ([f'Stated on {stated["url"]}: "{stated["text"]}"'] if stated else []), basis="Applicability", applicability=cat.HUMAN_REVIEW,
              limitation="The Third Schedule threshold (registered users in India) is not visible from a website; the organisation's own figure decides it.")
        return
    c, u = over[0]
    sents = a.sentences(_INACTIVE, PRIVACY, TERMS, limit=4)
    full = [f for f in sents if _NOTICE_48.search(f["quote"])]
    basis_ev = [f'{LABELS[c]}; the site states "{u["text"]}" ({u["url"]}), at or above the {THRESHOLD_LABEL[c]} threshold'] + ev[:2]
    lim = "The user figure is as the site states it; that it counts registered users in India is for the organisation to confirm."
    if full:
        a.put("RT4", PASS, "Erasure after the Third Schedule period of inactivity, with 48 hours' notice before erasure, is stated.", [cite(f) for f in full[:2]] + basis_ev,
              refs=refs_of(full), basis="Read from the published policy", review=True, limitation=lim, applicability=cat.APPLICABLE)
    elif sents:
        a.put("RT4", PARTIAL, "Erasure after a period of inactivity is stated, but not the 48 hours' notice before erasure (Rule 8(2)).", [cite(f) for f in sents[:2]] + basis_ev,
              refs=refs_of(sents), basis="Read from the published policy", review=True, limitation=lim, applicability=cat.APPLICABLE)
    else:
        a.put("RT4", FAIL, "The policy says nothing about erasing the data of people who have not used the service for the Third Schedule period, or about the 48 hours' notice.",
              basis_ev + [f"Policy read: {x}" for x in a.policy_urls()[:2]], basis="Read from the published policy", review=True, limitation=lim, applicability=cat.APPLICABLE)


def contact(a: A) -> None:
    cats = (PRIVACY, GRIEVANCE, TERMS, CONTACT, ABOUT)
    dpo = a.of("dpo_contact", *cats)
    named = [f for f in dpo if re.search(r"officer|dpo|grievance|privacy (team|contact|lead)|responsible|authori[sz]ed|data protection", blob(f), re.I)
             and (tr.EMAIL.search(blob(f)) or tr.PHONE.search(blob(f)) or tr.URL.search(blob(f)))]
    generic = [f for f in dpo + a.of("exercise_channel", *cats) if tr.EMAIL.search(blob(f)) or tr.PHONE.search(blob(f))]
    a.policy_result("G01", named, generic, "a contact for questions about the processing", full_note="A named person or role answers questions about the processing, with an e-mail or phone.",
                    part_note="Only a generic contact is given, without a named person or role.", cats=cats)

    emails = _contact_emails(a)

    def g02(pg):
        h = a.m.html.get(pg["url"])
        if h is None:
            return None, ""
        mine = h.emails & emails if emails else {e for e in h.emails if e.split("@")[-1].endswith((urlparse(a.m.root_url).hostname or "").removeprefix("www."))}
        if not mine:
            return FAIL, "no privacy contact on the page"
        shown = sorted(mine)[0]
        # 3.0.1: the contact must be usable. Where the page links the address (mailto:), the link must open an address shown, not a different one.
        if h.mailto and not (h.mailto & mine) and not (h.mailto & emails):
            return PARTIAL, f"contact shown ({shown}), but its link opens a different address ({sorted(h.mailto)[0]})"
        return PASS, f"contact shown ({shown})" + (" and linked" if h.mailto & mine else "")
    st, res = a.per_url("G02", g02)
    shown_as = f"the privacy contact ({', '.join(sorted(emails)[:2])})" if emails else "a privacy contact"
    a.put("G02", st, f"{A.summary(res)}: " + (f"{shown_as} is shown on the page." if st == PASS else
                                               f"{shown_as} is not shown on every page." if emails else
                                               "no privacy contact address of the site's own was identified on the pages read."),
          [f"{r.url}: {r.note}" for r in res if r.status != PASS][:MAX_EV], urls=res, basis="Observed URL by URL (page HTML)",
          applicability=cat.APPLICABLE if st != NA else None,
          limitation="Rule 9 also requires the contact in every reply to a person's communication; replies cannot be seen from the website.")

    mech = a.of("grievance_mechanism", *cats, pattern=r"grievance|complain|redress|officer|concern|escalat")
    with_contact = sorted([f for f in mech if tr.EMAIL.search(blob(f)) or tr.PHONE.search(blob(f)) or re.search(r"officer|https?://", blob(f), re.I)],
                          key=lambda f: (0 if tr.EMAIL.search(blob(f)) and re.search(r"officer|grievance", blob(f), re.I) else 1, _CAT_ORDER.get(f.get("category"), 9)))
    officer = [f for f in a.of("dpo_contact", *cats) if re.search(r"grievance|officer", blob(f), re.I) and tr.EMAIL.search(blob(f))]
    if mech and not with_contact and officer:
        with_contact = mech[:1] + officer[:1]
    a.policy_result("G03", with_contact, mech, "a grievance redressal mechanism", full_note="A grievance route is published with a contact.",
                    part_note="A grievance route is described without a contact.", cats=cats)

    per = a.of("grievance_period", *cats)
    # the organisation's own period, not a sentence that describes the law's limit ("Rule 14(3) sets a 90-day limit")
    per = [f for f in per if not re.search(r"rule 14|dpdp rules|the rules|sets? a|the law|statutory|limit|maximum|up to ninety", blob(f), re.I)] or per
    days = [(d, f) for f in per for d in (tr.parse_days(f["value"]) or tr.parse_days(f["quote"]))]
    if days:
        worst, wf = max(days, key=lambda x: x[0])
        st = PASS if worst <= 90 else FAIL
        a.put("G04", st, f"The longest stated response period is {worst:g} days (limit 90).", [cite(wf)] + [cite(f) for _, f in days if f is not wf][:1], refs=refs_of([f for _, f in days]),
              basis="Read from the published policy")
    elif per:
        a.put("G04", PARTIAL, "A response period is mentioned without a number of days.", [cite(per[0])], refs=refs_of(per), basis="Read from the published policy")
    else:
        a.absent("G04", "the period within which grievances are answered", cats)


def rights(a: A) -> None:
    cats = (PRIVACY, GRIEVANCE, TERMS, CONTACT)
    acc = a.of("right_access", *cats, pattern=r"access|copy|obtain|know|confirm|summary")
    a.policy_result("R01", acc, [], "the right to a summary of the personal data", full_note="The right to a summary of the personal data and its processing is stated.", part_note="", cats=cats)
    recips = [x for x in a.of("sharing_recipient", PRIVACY, COOKIE) if x.get("specific")]
    names = sorted({x["value"] for x in recips})
    ident = [f for f in a.of("right_access", *cats) + a.of("sharing_recipient", *cats) if re.search(r"identit(y|ies) of|whom (we|it) (has |have )?shared|names? of (the )?(recipients|processors|third)", blob(f), re.I)]
    if ident or len(names) >= 3:
        a.put("R02", PASS, ("The right to the identities of recipients is stated" if ident else f"The recipients are named ({len(names)}: {', '.join(names[:5])})") + ".",
              [cite(f) for f in (ident or recips)[:3]], refs=refs_of(ident or recips), basis="Read from the published policy")
    elif names or a.of("sharing_recipient", PRIVACY):
        a.put("R02", PARTIAL, f"Recipients are described generically, with {len(names)} named.", [cite(f) for f in (recips or a.of('sharing_recipient', PRIVACY))[:2]],
              refs=refs_of(recips or a.of("sharing_recipient", PRIVACY)), basis="Read from the published policy")
    else:
        a.absent("R02", "who the data is shared with")
    cor = a.of("right_correction", *cats, pattern=r"correct|rectif|update|amend|complet")
    full = [f for f in cor if re.search(r"correct|rectif", blob(f), re.I) and re.search(r"complet|updat", blob(f), re.I)]
    a.policy_result("R03", full, cor, "the right to correction, completion and updating", full_note="Correction, completion and updating are stated.", part_note="Only part of the right (correction, completion, updating) is stated.", cats=cats)
    a.policy_result("R04", a.of("right_erasure", *cats, pattern=r"erase|erasure|delet|remov|forgotten"), [], "the right to erasure", full_note="The right to erasure is stated.", part_note="", cats=cats)
    a.policy_result("R05", a.of("right_nominate", *cats, pattern=r"nominat"), [], "the right to nominate another person", full_note="The right to nominate is stated.", part_note="", cats=cats)
    ch = a.of("exercise_channel", *cats)
    means = [f for f in ch if re.search(r"@|https?://|portal|form|link|address|post|write to", blob(f), re.I)]
    a.policy_result("R06", means, ch, "how to make a rights request", full_note="A means of making a request (form, portal, e-mail or address) is given.", part_note="Only 'contact us' is given.", cats=cats)
    idf = a.of("request_identifier", *cats) + a.of("identity_verification", *cats) or \
        a.sentences(r"verif\w* (your|the requester'?s?) identity|verifying who you are|confirm\w* (that )?you control the e-?mail|"
                    r"(tell us|provide|give|include)[^.\n]{0,80}(the e-?mail address|registered (e-?mail|mobile)|account (id|you used)|customer id)", *cats)
    a.policy_result("R07", idf, [], "the identifiers needed for a request, or how the requester is verified", full_note="The identifiers to give, or how the requester is verified, are stated.", part_note="", cats=cats)

    route = [(h, t) for h, t in a.route_links() if re.search(r"right|request|grievance|dsar|data-?principal|portal", h + " " + t, re.I)]
    if route:
        h, t = route[0]
        if same_host(h, a.m.root_url):
            done = any((r.get("final_url") or r.get("original_url") or "").rstrip("/") == h.rstrip("/") and r.get("status") == "done" for r in a.m.frontier_rows)
            a.put("R08", PASS, f"The request route is linked from the notice" + (" and loaded during the scan." if done else "."), [f'Link "{t or h}" → {h}'], basis="Observed (notice HTML)",
                  limitation=None if done else "The linked page was not reached by the crawl.")
        else:
            a.put("R08", PASS, "The request route is linked from the notice.", [f'Link "{t or h}" → {h}'], basis="Observed (notice HTML)",
                  limitation=f"The target is on {urlparse(h).hostname}, outside the scanned host, so the scan did not open it (exact-host rule).")
    else:
        a.put("R08", FAIL, "The notice has no link to a rights-request route.", basis="Observed (notice HTML)")


def transfers(a: A) -> None:
    cats = (PRIVACY, TERMS, COOKIE)
    xb = a.of("cross_border", *cats)
    dest = [f for f in xb if f.get("specific") or re.search(r"united states|usa|\bus\b|europe|\beu\b|singapore|uk|ireland|country|countries", blob(f), re.I)]
    third = {h["host"] for _, n in a.ctx.loaded.networks for h in n.get("hosts", []) if h.get("third_party") and "." in (h.get("host") or "")}   # a browser-extension id is not a host
    if not xb and not third:
        a.put("T01", NA, "No third party was contacted and the policy names no recipient, so no transfer outside India is indicated.", basis="Applicability")
        a.put("T02", NA, "No transfer outside India is indicated.", basis="Applicability")
        return
    a.policy_result("T01", dest, xb, "transfers of personal data outside India", full_note="Transfers outside India are disclosed, with destinations.",
                    part_note="Transfers outside India are mentioned without destinations.", cats=cats)
    safe = sorted([f for f in xb if re.search(r"contract|safeguard|clause|adequa|protect|terms|agreement|restrict", blob(f), re.I)],
                  key=lambda f: 0 if re.search(r"safeguard|contractual|clause", f["quote"], re.I) else 1)
    a.policy_result("T02", safe, [], "the safeguards for transfers outside India", full_note="Safeguards for transfers are stated.", part_note="", cats=cats)


def cross_checks(a: A) -> None:
    text = a.text(PRIVACY, COOKIE)
    acc = a.consent("accept")[1]
    seen: dict[tuple, dict] = {}
    for pg in a.pages.values():
        for c in pg["cookies"]:
            seen.setdefault((c.get("name"), c.get("domain")), {**c})
    for c in acc.get("cookies") or []:
        seen.setdefault((c.get("name"), c.get("domain")), {**c})
    if not seen:
        a.put("X01", NA, "No cookie was observed on any page or after Accept.", basis="Observed against the cookie policy")
    elif not (a.has_policy() or a.ctx.src(COOKIE)):
        a.put("X01", FAIL, f"{len(seen)} cookies were observed and no privacy or cookie policy was found to declare them.", basis="Observed against the cookie policy")
    else:
        rows = []
        for (n, d), c in sorted(seen.items()):
            prov = describe(c).get("provider")
            by_name = tr.cookie_declared(n, "", text)
            by_service = bool(prov and prov != "The website" and vendor_named(prov, "", text)) or (not c.get("first_party") and vendor_named("", d, text))
            rows.append((n, d, c, by_name or by_service))
        nd = sum(1 for r in rows if r[3])
        st = PASS if nd == len(rows) else PARTIAL if nd * 2 >= len(rows) else FAIL
        miss = [f"{n} ({d})" for n, d, _, ok in rows if not ok]
        a.put("X01", st, f"{nd} of {len(rows)} distinct cookies observed are declared in the policy" + (f"; not declared: {', '.join(miss[:10])}." if miss else "."),
              [f"{n} ({d}, {(c.get('consent_state') or '').replace('_', ' ')}): {'declared' if ok else 'not declared'}" for n, d, c, ok in rows][:12],
              basis="Observed against the cookie policy (name and domain)", review=st != PASS)

    vendors: dict[str, str] = {}
    for _, n in a.ctx.loaded.networks:
        for t in n.get("non_essential") or []:
            vendors.setdefault(t.get("vendor") or t.get("host"), t.get("host") or "")
    for t in (acc.get("non_essential") or []) + (a.consent("withdrawal")[1].get("accepted_non_essential") or []):
        vendors.setdefault(t.get("vendor") or t.get("host"), t.get("host") or "")
    # 3.0.2: embedded third-party players, widgets and booking or chat tools the pages load, and third-party tracking cookies, are services the notice must name too.
    for _, n in a.ctx.loaded.networks:
        for h in n.get("hosts") or []:
            host = (h.get("host") or "").lower()
            name = _embedded_service(host) if h.get("third_party") else None
            if name:
                vendors.setdefault(name, host)
    for pg in a.pages.values():
        for c in pg["cookies"]:
            if tracking_cookie(c) and not c.get("first_party"):
                vendors.setdefault(_cookie_service(c), (c.get("domain") or "").lstrip("."))
    if not vendors:
        a.put("X02", NA, "No analytics, advertising or embedded service was contacted, before or after a choice.", basis="Observed against the policy")
    elif not a.has_policy():
        a.put("X02", FAIL, f"{len(vendors)} services were contacted and no privacy policy was found to name them.", basis="Observed against the policy")
    else:
        named = {v: vendor_named(v, h, text) for v, h in vendors.items()}
        nn = sum(named.values())
        st = PASS if nn == len(named) else PARTIAL if nn * 2 >= len(named) else FAIL
        a.put("X02", st, f"{nn} of {len(named)} analytics, advertising or embedded services contacted are named in the policy" +
              (f"; not named: {', '.join(v for v, ok in named.items() if not ok)}." if nn < len(named) else "."),
              [f"{v} ({vendors[v]}): {'named' if ok else 'not named'}" for v, ok in sorted(named.items())][:10], basis="Observed against the policy (product names, not the parent company)",
              review=st != PASS)

    items = " ".join(blob(f) for f in a.of("data_category", PRIVACY)).lower()

    def x03(pg):
        fs = pii_forms(pg)
        if not fs:
            return None, ""
        kinds = sorted({k for f in fs for k in f.get("collects", []) if k in ITEMISED_KINDS})
        if not kinds:
            return None, ""
        miss = [k for k in kinds if not itemised(k, items)]
        return (FAIL, f"asks for {', '.join(miss)}, not in the notice's list of data") if miss else (PASS, f"{', '.join(kinds)}: itemised")
    if not a.has_policy():
        st, res = a.per_url("X03", lambda pg: (FAIL, "no notice to itemise the data") if pii_forms(pg) else (None, ""))
        a.put("X03", st if res else NA, "No privacy notice was found to itemise the data the forms collect." if res else "No form collects personal data.", urls=res, basis="Observed URL by URL")
    else:
        st, res = a.per_url("X03", x03)
        if st == NA:
            a.put("X03", NA, "No form on the crawled pages collects personal data.", basis="Observed URL by URL")
        else:
            a.put("X03", st, f"{A.summary(res)}: every kind of data the forms ask for is named in the notice's list of personal data.",
                  [f"{r.url}: {r.note}" for r in res if r.status == FAIL][:MAX_EV], urls=res, basis="Observed URL by URL against the policy")


def _children_applicability(a: A) -> tuple[str, str, list[str], list[str]]:
    """K01-K04 (3.0.1): applicable, not applicable or human review, with the reason, the evidence and the evidence ids.

    Applicable: something shows the site itself serves children (a parent or guardian field, its own statement, or a children's service read from two kinds of signal).
    Not applicable: nothing shows it, or the policy excludes children and nothing contradicts it. Human review: the signals disagree (the policy excludes children but a form or
    statement says otherwise) or are too weak to decide (an EdTech site that does not say whether its learners are under 18, one signal only). An EdTech site is never assumed
    to process children's data, and missing information is never taken as proof either way."""
    site, k1 = a.site, a.v2.get("K1")
    directed = a.of("children_directed", PRIVACY, TERMS)
    # a statement about the site's OWN audience; terms that tell business customers how to handle their users' children's data, or an industry offering, are not one
    directed = [f for f in directed if not re.search(r"customer|you have obtained|submitted|submit|industry|offering|edtech|on (its|their) behalf|processor|platform to process", blob(f), re.I)]
    not_directed = a.of("children_not_directed", PRIVACY, TERMS)
    excl = [cite(not_directed[0])] if not_directed else [f'{x["url"]}: "{x["text"]}"' for x in site.not_for_children[:1]]
    refs = refs_of(not_directed + directed)
    strong, ev = [], []
    if site.guardian_fields:
        strong.append(f"a form asks for a parent or guardian ({site.guardian_fields[0]})")
    if directed:
        strong.append("the site says it serves children")
        ev.append(cite(directed[0]))
    if site.has(CHILDREN):
        strong.append(f"the site reads as a children's service ({site.confidence[CHILDREN]} confidence)")
        ev += site.evidence_for(CHILDREN, 3)
    weak = []
    if not strong:
        if CHILDREN in site.possible:
            weak.append("one signal of a children's service only")
            ev += site.evidence_for(CHILDREN, 2)
        if site.has(EDTECH):
            weak.append("the site reads as EdTech / education and does not show whether its learners are under 18")
            ev += site.evidence_for(EDTECH, 2)
        if k1 is not None and k1.state not in ("not_applicable", "not_assessed"):
            weak.append(f"the 51-parameter reading applied the duty ({(k1.note or '')[:140]})")
    if strong and not excl:
        why = "; ".join(strong)
        return cat.APPLICABLE, why[0].upper() + why[1:] + ".", ev, refs
    if strong:
        return (cat.HUMAN_REVIEW, f"The evidence disagrees: {'; '.join(strong)}, but the policy says the site is not for children. The organisation must confirm whether it "
                "processes children's data; until then these marks are held back.", ev + excl, refs)
    if excl:
        return (cat.NOT_APPLICABLE, "The policy says the site is not for children, and no form, page or statement shows otherwise"
                + (" (weaker signals were seen, listed in the evidence, but they are not enough to apply the duty)" if weak else "") + ".",
                excl + [f"Weaker signal: {x}" for x in weak] + ev, refs)
    if weak:
        return (cat.HUMAN_REVIEW, f"Not settled by the website: {'; '.join(weak)}. The organisation must confirm whether it processes the personal data of anyone under 18; "
                "until then these marks are held back.", ev, refs)
    return (cat.NOT_APPLICABLE, f"No evidence of children's data: the site reads as {LABELS[site.primary].lower()}, and no form, page or statement shows it serves anyone "
            "under 18.", ev, refs)


def k05(a: A) -> None:
    """K05 (3.0.1): consent of a verified lawful guardian for a person with disability (s.9(1), Rule 11). A policy clause that repeats the law is not a sign that such persons
    use the site; a guardian field together with the site's own words about persons with disability is."""
    site, k2 = a.site, a.v2.get("K2")
    dis = [f'{x["url"]}: "{x["text"]}"' for x in site.disability[:1]]
    if site.guardian_fields and site.disability:
        sents = a.sentences(r"lawful guardian|guardian of (a |the )?person with", PRIVACY, TERMS, limit=3)
        ver = [f for f in sents if re.search(r"verif", f["quote"], re.I)]
        why = f"Applies because a form asks for a guardian ({site.guardian_fields[0]}) and the site refers to persons with disability"
        if ver:
            a.put("K05", PASS, f"{why}; verified consent of the lawful guardian is described.", [cite(f) for f in ver[:2]] + dis, refs=refs_of(ver),
                  basis="Read from the published policy", review=True, applicability=cat.APPLICABLE)
        elif sents:
            a.put("K05", PARTIAL, f"{why}; the guardian's consent is described, but not how the guardian is verified (Rule 11).", [cite(f) for f in sents[:2]] + dis,
                  refs=refs_of(sents), basis="Read from the published policy", review=True, applicability=cat.APPLICABLE)
        else:
            a.put("K05", FAIL, f"{why}; the policy says nothing about the lawful guardian's verified consent.", dis, basis="Read from the published policy", review=True,
                  applicability=cat.APPLICABLE)
    elif k2 is not None and k2.state not in ("not_applicable", "not_assessed"):
        a.put("K05", NA, f"Not settled by the website: the 51-parameter reading applied this duty ({(k2.note or '')[:160]}), but no form or page of the site shows persons with "
              "disability who have a lawful guardian use it. The organisation must confirm; until then these marks are held back.", [k2.note[:300]] + dis,
              refs=list(k2.evidence_refs), basis="Applicability", applicability=cat.HUMAN_REVIEW)
    else:
        a.put("K05", NA, "No evidence that persons with disability who have a lawful guardian use the site: no guardian form" +
              (" (the policy mentions such persons, but a clause that restates the law is not a sign they use the site)" if dis else "") + ".",
              dis, basis="Applicability", applicability=cat.NOT_APPLICABLE)


def conditional(a: A) -> None:
    applicability, why, ev, refs = _children_applicability(a)
    k1 = a.v2.get("K1")
    v2_applied = k1 is not None and k1.state not in ("not_applicable", "not_assessed")
    if applicability != cat.APPLICABLE:
        for pid in ("K01", "K02", "K03", "K04"):
            a.put(pid, NA, why, ev, refs=refs, basis="Applicability", applicability=applicability,
                  limitation=("Whether the organisation processes children's data is a fact about its users, not always visible on the website."
                              if applicability == cat.HUMAN_REVIEW else None),
                  differs=(f"Enterprise v2 K1 read the site as serving children ({(k1.note or '')[:160]}); no page, form or statement of the site itself shows it does."
                           if pid == "K01" and v2_applied and applicability == cat.NOT_APPLICABLE else None))
    else:
        cats = (PRIVACY, TERMS)
        consent_f = a.of("children_consent", *cats, pattern=r"parent|guardian|verifiable|consent")
        notrack = a.of("children_no_tracking", *cats, pattern=r"track|profil|monitor")
        noads = a.of("children_no_tracking", *cats, pattern=r"advertis|target")
        a.policy_result("K01", [f for f in consent_f if re.search(r"verif", blob(f), re.I)], consent_f, "verifiable parental consent", full_note="Verifiable parental consent is described.",
                        part_note="Parental consent is described, but not how it is verified.", cats=cats, review=True)
        a.policy_result("K02", a.of("children_no_tracking", *cats) + consent_f[:0], [], "protecting children from detrimental processing", full_note="The policy limits processing of children's data.",
                        part_note="", cats=cats, review=True)
        a.policy_result("K03", notrack, [], "not tracking or monitoring children", full_note="The policy says children are not tracked or monitored.", part_note="", cats=cats, review=True)
        a.policy_result("K04", noads, [], "no targeted advertising at children", full_note="The policy says no advertising is targeted at children.", part_note="", cats=cats, review=True)
        for pid in ("K01", "K02", "K03", "K04"):
            r = a.out[pid]
            r.applicability = cat.APPLICABLE
            r.evidence = ([f"Applies because {why[0].lower() + why[1:]}"] + r.evidence)[:MAX_EV]
    k05(a)
    s1 = a.v2.get("S1")
    if s1 is None or s1.state in ("not_applicable", "not_assessed"):
        note = "The organisation is not shown to be notified as a Significant Data Fiduciary (a notification cannot be seen on a website; to be confirmed by the organisation)."
        for pid in ("S01", "S02", "S03", "S04", "S05", "S06"):
            a.put(pid, NA, note, basis="Applicability", review=pid == "S01")
    else:
        cats = (PRIVACY, ABOUT, TERMS)
        duty = a.of("sdf_duty", *cats)
        for pid, rx, what in (("S01", r"officer|dpo", "a DPO based in India"), ("S02", r"auditor", "an independent data auditor"), ("S03", r"impact assessment|dpia", "a periodic impact assessment"),
                              ("S04", r"audit", "a periodic audit"), ("S05", r"algorithm", "due diligence on algorithmic software"), ("S06", r"in india|within india|locali", "specified data processed in India")):
            hit = [f for f in duty if re.search(rx, blob(f), re.I)]
            a.policy_result(pid, hit, [], what, full_note=f"The policy states {what}.", part_note="", cats=cats, review=True)

    m1 = a.v2.get("M1")
    cm = [f for f in a.of("consent_manager") if re.search(r"consent manager", blob(f), re.I)
          and not re.search(r"cookie consent manager|consent management (platform|tool)|\bcmp\b", blob(f), re.I)]
    if m1 is not None and m1.state not in ("not_applicable", "not_assessed") and not cm:
        a.put("M01", NA, "No Board-registered Consent Manager is integrated: the only mention is of a cookie consent tool, which is not a Consent Manager under s.6(9).",
              [cite(f) for f in a.of("consent_manager")[:1]], basis="Applicability", review=True,
              differs=f"Enterprise v2 M1 read the mention as a Consent Manager ({(m1.note or '')[:150]}); it is a cookie consent tool.")
    elif m1 is None or m1.state in ("not_applicable", "not_assessed"):
        a.put("M01", NA, "No Board-registered Consent Manager is integrated; a cookie consent tool is not a Consent Manager under s.6(9).", basis="Applicability", review=False)
    else:
        a.put("M01", _V2_STATUS.get(m1.state, FAIL), m1.note[:300], [m1.note[:300]], refs=list(m1.evidence_refs), basis="Read from the pages", review=True)
    m2 = a.v2.get("M2")
    if m2 is None or m2.state in ("not_applicable", "not_assessed"):
        a.put("M02", NA, "The organisation does not say it is a Board-registered Consent Manager.", basis="Applicability")
    else:
        a.put("M02", _V2_STATUS.get(m2.state, FAIL), m2.note[:300], [m2.note[:300]], refs=list(m2.evidence_refs), basis="Read from the pages", review=True)

    claims = [f for f in a.of("exemption_claim", PRIVACY, TERMS) if re.search(r"exempt|section 17|s\.\s?17", f["quote"], re.I)
              and not re.search(r"not yet in force|as a matter of (our own )?practice|pending commencement|voluntar|not making a (general )?claim", blob(f), re.I)]
    if claims:
        a.put("E01", INFORMATION, "An exemption is claimed; whether it applies is for a person to decide.", [cite(f) for f in claims[:2]], refs=refs_of(claims), basis="Read from the policy and terms",
              review=True)
    else:
        a.put("E01", INFORMATION, "No exemption from the Act is claimed.", basis="Read from the policy and terms")


def technical(a: A) -> None:
    def sec(pg):
        return (pg["sec"] or {}).get("security") or {}
    st, res = a.per_url("TS01", lambda pg: (PASS, "https") if sec(pg).get("https") or pg["url"].startswith("https://") else (FAIL, "not https"))
    a.put("TS01", st, f"{A.summary(res)}.", [f"{r.url}: {r.note}" for r in res if r.status != PASS][:MAX_EV], urls=res, basis="Observed URL by URL")
    st, res = a.per_url("TS02", lambda pg: (PASS, "0 plain-HTTP requests") if not pg["net"].get("insecure_requests") else (FAIL, f"{pg['net']['insecure_requests']} plain-HTTP requests"))
    a.put("TS02", st, f"{A.summary(res)}.", [f"{r.url}: {r.note}" for r in res if r.status != PASS][:MAX_EV], urls=res, basis="Observed URL by URL")
    tls = a.m.tls or {}
    if tls.get("reachable") and tls.get("verified"):
        from datetime import datetime
        try:
            left = (datetime.fromisoformat(tls["not_after"]).date() - a.m.as_of).days
        except (KeyError, ValueError, TypeError):
            left = None
        st = PASS if left is None or left > 30 else PARTIAL if left > 0 else FAIL
        a.put("TS03", st, f"Certificate {tls.get('subject')} from {tls.get('issuer')}, verified, valid until {str(tls.get('not_after'))[:10]}" + (f" ({left} days after the scan)." if left is not None else "."),
              basis="Observed (TLS)")
        ver = tls.get("negotiated") or ""
        a.put("TS04", PASS if ver in ("TLSv1.2", "TLSv1.3") else FAIL, f"{ver or 'No TLS version'} negotiated" + ("; TLS 1.3 is not offered (recommended)." if ver == "TLSv1.2" and not tls.get("tls13_supported") else "."),
              basis="Observed (TLS)")
    else:
        a.put("TS03", FAIL, f"The certificate could not be verified: {tls.get('verify_error') or 'no TLS probe result'}.", basis="Observed (TLS)")
        a.put("TS04", FAIL, "No TLS 1.2 or 1.3 connection was established.", basis="Observed (TLS)")
    for pid, hdr, label in (("TS05", "strict-transport-security", "HSTS"), ("TS06", "content-security-policy", "Content-Security-Policy"), ("TS07", "x-content-type-options", "X-Content-Type-Options"),
                            ("TS08", "referrer-policy", "Referrer-Policy"), ("TS09", "permissions-policy", "Permissions-Policy")):
        st, res = a.per_url(pid, lambda pg, h=hdr: (None, "") if pg["sec"] is None else ((PASS, "sent") if h in (sec(pg).get("present") or {}) else (FAIL, "missing")))
        a.put(pid, st, f"{label}: {A.summary(res)}.", [], urls=res, basis="Observed URL by URL")

    def ts10(pg):
        if pg["sec"] is None:
            return None, ""
        pr = sec(pg).get("present") or {}
        ok = "x-frame-options" in pr or "frame-ancestors" in (pr.get("content-security-policy") or "")
        return (PASS, "X-Frame-Options or frame-ancestors") if ok else (FAIL, "missing")
    st, res = a.per_url("TS10", ts10)
    a.put("TS10", st, f"{A.summary(res)}.", [], urls=res, basis="Observed URL by URL")

    acc = a.consent("accept")[1]
    seen: dict[tuple, dict] = {}
    for pg in a.pages.values():
        for c in pg["cookies"]:
            seen.setdefault((c.get("name"), c.get("domain")), c)
    for c in acc.get("cookies") or []:
        seen.setdefault((c.get("name"), c.get("domain")), c)
    if not seen:
        a.put("TS11", NA, "No cookie was observed.", basis="Observed")
        a.put("TS12", NA, "No cookie was observed.", basis="Observed")
    else:
        sec_n = [c for c in seen.values() if c.get("secure")]
        st = PASS if len(sec_n) == len(seen) else PARTIAL if len(sec_n) * 2 >= len(seen) else FAIL
        a.put("TS11", st, f"{len(sec_n)} of {len(seen)} cookies carry Secure" + (f"; not: {', '.join(sorted({c['name'] for c in seen.values() if not c.get('secure')})[:8])}." if st != PASS else "."),
              basis="Observed (every cookie, before and after Accept)")
        ss = [c for c in seen.values() if c.get("same_site") not in (None, "", "unspecified")]
        st = PASS if len(ss) == len(seen) else PARTIAL if len(ss) * 2 >= len(seen) else FAIL
        a.put("TS12", st, f"{len(ss)} of {len(seen)} cookies set SameSite.", basis="Observed (every cookie, before and after Accept)")

    chains = [h.get("redirect_chain") or [] for _, h in a.ctx.loaded.headers if "security" in h]
    hops = [x for c in chains for x in c]
    redirects = [r for r in a.m.frontier_rows if (r.get("status_reason") or "").startswith("duplicate_destination") or (r.get("final_url") and r.get("original_url") and r["final_url"] != r["original_url"])]
    if not hops and not redirects:
        a.put("TS13", NA, "No redirect was observed.", basis="Observed")
    else:
        bad = [x for x in hops if str(x.get("url", "")).startswith("http:")] + [r for r in redirects if str(r.get("final_url") or "").startswith("http:")]
        a.put("TS13", PASS if not bad else FAIL, f"{len([c for c in chains if c]) + len(redirects)} redirect(s) observed; " + ("none left HTTPS." if not bad else f"{len(bad)} passed through plain HTTP."),
              [f"{r['original_url']} → {r.get('final_url') or (r.get('status_reason') or '')[:120]}" for r in redirects[:3]], basis="Observed")

    st, res = a.per_url("TS14", lambda pg: (None, "") if not pii_forms(pg) else ((PASS, "form actions https") if all(str(f.get("action", "")).startswith("https:") for f in pii_forms(pg)) else (FAIL, "an http form action")))
    a.put("TS14", st, f"{A.summary(res)}." if res else "No form collects personal data.", urls=res, basis="Observed URL by URL")

    def ts15(pg):
        fs = pii_forms(pg)
        if not fs:
            return None, ""
        if all((f.get("method") or "get").lower() == "post" for f in fs):
            return PASS, "POST"
        self_get = all(str(f.get("action", "")).split("#")[0].split("?")[0].rstrip("/") == pg["url"].rstrip("/") for f in fs if (f.get("method") or "get").lower() != "post")
        return (PARTIAL if self_get else FAIL), ("method GET, action = the page itself" if self_get else "method GET to another address")
    st, res = a.per_url("TS15", ts15)
    if st == NA:
        a.put("TS15", NA, "No form collects personal data.", basis="Observed URL by URL")
    else:
        forms = [f for pg in a.pages.values() for f in pii_forms(pg)]
        nget = sum(1 for f in forms if (f.get("method") or "get").lower() != "post")
        overall = PASS if nget == 0 else (FAIL if any(r.status == FAIL for r in res) and sum(1 for r in res if r.status == FAIL) * 2 > len(res) else PARTIAL)
        a.put("TS15", overall, "Every data-collecting form uses POST." if nget == 0 else
              f"{nget} of {len(forms)} data-collecting forms declare method GET. A script very likely sends the data (forms are never submitted by the scan), but if the browser default ran, the details would go into the address.",
              [f"Form method attribute: get ({nget} of {len(forms)})"], urls=res, basis="Observed URL by URL", review=nget > 0,
              limitation="Confirm in the site code that submission is handled by a script (POST/fetch), or set method=\"post\"." if nget else None)


def assess(m: M3Context, v2: list[Assessment], site: SiteCategory | None = None) -> list[Result]:
    """All 96 results, in catalogue order. A rule that fails on unexpected data is reported as a Fail with the error and marked for review; it never stops the others."""
    a = A(m, {x.parameter_id: x for x in v2}, site)
    for step in (notice, consent, lawful, fiduciary, retention, contact, rights, transfers, cross_checks, conditional, technical):
        try:
            step(a)
        except Exception as exc:                          # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("method 3.0.0: the %s rules failed", step.__name__)
            for p in cat.PARAMS:
                if p.id not in a.out and _owner(p.id) == step.__name__:
                    a.put(p.id, NA if p.kind == cat.CONDITIONAL else FAIL, f"The rule could not be applied ({type(exc).__name__}: {str(exc)[:120]}).", review=True,
                          limitation="An error in the assessment, not a finding about the site: re-run the assessment after the fix.")
    for p in cat.PARAMS:                                    # a parameter no rule reached (never expected) is still reported, never silently missing
        if p.id not in a.out:
            a.put(p.id, NA if p.kind == cat.CONDITIONAL else FAIL, "No rule produced a result for this parameter.", review=True, limitation="An error in the assessment, not a finding about the site.")
    return [a.out[p.id] for p in cat.PARAMS]


def _owner(pid: str) -> str:
    return {"N": "notice", "C": "consent", "L": "lawful", "F": "fiduciary", "RT": "retention", "G": "contact", "R": "rights", "T": "transfers", "X": "cross_checks",
            "K": "conditional", "S": "conditional", "M": "conditional", "E": "conditional", "TS": "technical"}[cat.BY_ID[pid].cat]
