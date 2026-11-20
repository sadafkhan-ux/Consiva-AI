"""The website's apparent business category, worked out from the scan's own evidence, so the conditional DPDP duties apply to the right sites (method 3.0.1).

Universal and deterministic: no model call, no site-specific rule. The same stored scan always gives the same answer.

Evidence used (all already stored by the crawl):
  * the address, title and headings of every inspected page of the site's own host;
  * the fields of every form (labels, names, placeholders);
  * the third-party services the pages contacted (payment gateways, store platforms);
  * the text of the privacy notice, the terms and the other pages (self-descriptions such as "we are an online marketplace").

A category needs two different kinds of signal, or the site's own statement of what it is. One signal alone is shown as "possible" and never applies a duty by itself.
An EdTech site is not a children's service unless something shows children use it. Nothing here treats missing information as proof that a condition does not exist:
where the evidence does not settle a legal condition (for example the Third Schedule user threshold), the duty goes to human review.

The Third Schedule thresholds (DPDP Rules, 2025, Rule 8(1)): e-commerce entity and social media intermediary, at least two crore registered users in India; online gaming
intermediary, at least fifty lakh registered users in India; period, three years from the person's last approach or use.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

GENERAL, ECOMMERCE, GAMING, SOCIAL, EDTECH, CHILDREN = "general", "ecommerce", "gaming", "social_media", "edtech", "children"
LABELS = {GENERAL: "General business / corporate", ECOMMERCE: "E-commerce", GAMING: "Online gaming", SOCIAL: "Social media", EDTECH: "EdTech / education",
          CHILDREN: "Children's service"}
#: Third Schedule platforms and their registered-user thresholds in India (Rule 8(1)).
THIRD_SCHEDULE = {ECOMMERCE: 20_000_000, SOCIAL: 20_000_000, GAMING: 5_000_000}
THRESHOLD_LABEL = {ECOMMERCE: "two crore", SOCIAL: "two crore", GAMING: "fifty lakh"}

# kinds of signal
PATH, FIELD, SERVICE, TEXT, SELF = "page", "form field", "third-party service", "page or policy text", "the site's own statement"

# (category, kind) -> pattern. Paths and titles are of the site's own pages only (the crawl never leaves the host), so a "follow us" link to a social network is not one.
_PATHS = {
    ECOMMERCE: r"/(cart|basket|checkout|bag)(/|$)|/(shop|store|products?|collections?)(/|$)|add[- ]to[- ](cart|bag)|shopping cart|buy now|wishlist",
    GAMING: r"/(games?|play|casino|rummy|poker|ludo|fantasy|contests?|tournaments?|leaderboard)(/|$)|play (now|online)|fantasy (sports|league|cricket)|real[- ]money",
    SOCIAL: r"/(feed|newsfeed|timeline|followers|following|friends|messages|inbox)(/|$)|news ?feed|create (a )?post|followers",
    EDTECH: r"/(courses?|classes|lessons?|learn|enrol|enroll|admissions?|students?|tutors?|curriculum|syllabus)(/|$)|online (courses?|classes|learning)|enrol(l)? now",
    CHILDREN: r"\bkids?\b|for children|\b(nursery|pre-?school|kindergarten|toddlers?)\b|\bk-?12\b|\bclass(es)? ([1-9]|1[0-2])(st|nd|rd|th)?\b|\bgrades? ([1-9]|1[0-2])\b|"
              r"school students|\bcbse\b|\bicse\b",
}
_FIELDS = {
    ECOMMERCE: r"card ?number|\bcvv\b|\bcvc\b|expiry|coupon|promo ?code|shipping|billing address|quantity",
    GAMING: r"team ?name|game ?(id|name)|deposit|wallet|referral code|in-?game",
    SOCIAL: r"username|handle|\bbio\b|display name|profile (photo|picture)",
    EDTECH: r"student|\bcourse\b|\bgrade\b|school|\bbatch\b",
    CHILDREN: r"guardian|parent|child'?s? (name|age)|date of birth of (the )?child|\bage\b",
}
_SERVICES = {
    ECOMMERCE: r"razorpay|stripe|payu|cashfree|paytm|phonepe|ccavenue|instamojo|juspay|billdesk|shopify|woocommerce|bigcommerce|magento|paypal",
}
_TEXT = {
    ECOMMERCE: r"\b(orders? (you place|placed)|place an order|cash on delivery|returns? and refunds?|refund policy|shipping policy|delivery charges)\b",
    GAMING: r"\b(online gam(e|es|ing)|real money gam|skill gam|game of skill|winnings|gameplay)\b",
    SOCIAL: r"\b(user[- ]generated content|content you (post|upload|share)|your posts|other users can see|followers)\b",
    EDTECH: r"\b(learners?|students?|courses?|curriculum|tutors?|lessons?) (enrol|enroll|attend|access|take|complete)|\b(online|e-?)learning\b|\beducational (content|services)\b",
    CHILDREN: r"\b(designed|intended|meant|made) for (children|kids|students under)|\b(children|kids) (can|may) (use|play|learn)|\bour young (users|learners)\b",
}
_SELF = {
    ECOMMERCE: r"\b(we are|we operate|we run) an? (online )?(e-?commerce|marketplace|online (store|shop|retailer))",
    GAMING: r"\b(we are|we operate|we run) an? (online )?(real[- ]money )?(gaming|game) (platform|intermediary|app|company)",
    SOCIAL: r"\b(we are|we operate|we run) an? social (media|network(ing)?) (platform|intermediary|app|service)",
    EDTECH: r"\b(we are|we operate|we run) an? (ed-?tech|e-?learning|online (learning|education)|education(al)?) (platform|company|provider|app)",
    CHILDREN: r"\b(we are|we operate|we run) an? (platform|app|service|website) (for|designed for) (children|kids)",
}
#: "The site is not for children": the policy's own words excluding them.
NOT_FOR_CHILDREN = re.compile(r"not (intended|directed|designed|meant|aimed) (for|at|to) (children|minors|kids|anyone under|individuals under|persons under|users under)|"
                              r"(do not|don't) (knowingly )?(collect|process)[^.]{0,60}(children|minors|under (the age of )?18)|"
                              r"(must|should) be (at least )?18|only (for|to) (adults|persons aged 18|users aged 18)", re.I)
#: Persons with disability who have a lawful guardian (s.9(1), Rule 11).
DISABILITY = re.compile(r"person(s)? with (a )?disabilit|lawful guardian|differently[- ]abled|special needs", re.I)
GUARDIAN_FIELD = re.compile(r"guardian|parent|child'?s? name|date of birth of (the )?child", re.I)
#: A stated number of users ("2 crore+ users", "50 million customers").
USERS = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(crore|cr|lakhs?|lacs?|million|mn|billion|bn|k|thousand)?\s*\+?\s*(?:registered |active |monthly active |happy |satisfied )?"
                   r"(users|customers|members|players|gamers|learners|students|subscribers)\b", re.I)
_UNIT = {"crore": 10_000_000, "cr": 10_000_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000, "million": 1_000_000, "mn": 1_000_000,
         "billion": 1_000_000_000, "bn": 1_000_000_000, "k": 1_000, "thousand": 1_000}


@dataclass
class Signal:
    category: str
    kind: str
    detail: str
    url: str


@dataclass
class SiteCategory:
    primary: str                                         # one of LABELS
    categories: list[str]                                # every category with enough evidence (two kinds of signal, or the site's own statement)
    possible: list[str]                                  # one signal only: shown, never applies a duty by itself
    confidence: dict[str, str]                           # category -> high | medium | low
    signals: list[Signal] = field(default_factory=list)
    users_stated: list[dict] = field(default_factory=list)       # [{"count": int, "text": str, "url": str}], largest first
    not_for_children: list[dict] = field(default_factory=list)   # [{"text", "url"}]
    disability: list[dict] = field(default_factory=list)         # [{"text", "url"}]
    guardian_fields: list[str] = field(default_factory=list)     # urls of forms asking for a parent or guardian

    def has(self, c: str) -> bool:
        return c in self.categories

    def evidence_for(self, c: str, limit: int = 4) -> list[str]:
        return [f"{s.kind}: {s.detail} ({s.url})" for s in self.signals if s.category == c][:limit]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = LABELS[self.primary]
        d["labels"] = [LABELS[c] for c in self.categories]
        return d


def _sentence(text: str, m: re.Match) -> str:
    start = max(text.rfind(". ", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
    ends = [i for i in (text.find(". ", m.end()), text.find("\n", m.end())) if i != -1]
    return " ".join(text[start:(min(ends) + 1 if ends else len(text))].split())[:240]


def users_stated(text: str) -> list[tuple[int, str]]:
    out = []
    for m in USERS.finditer(text or ""):
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        n *= _UNIT.get((m.group(2) or "").lower(), 1)
        if n >= 1000:                                    # "3 users" in a sentence is not a size statement
            out.append((int(n), " ".join(m.group(0).split())))
    return out


def detect(m) -> SiteCategory:
    """The category of the scanned site (m: method3.context.M3Context)."""
    loaded = m.ctx.loaded
    sig: list[Signal] = []

    def add(c, kind, detail, url):
        if not any(s.category == c and s.kind == kind and s.detail == detail for s in sig):
            sig.append(Signal(c, kind, detail[:160], url))

    for p in loaded.pages.values():
        hay = " ".join([urlparse(p.url).path.rstrip("/") + "/", p.title or "", " ".join((p.headings or [])[:8])])
        for c, rx in _PATHS.items():
            mm = re.search(rx, hay, re.I)
            if mm:
                add(c, PATH, f'"{mm.group(0).strip()}" in {urlparse(p.url).path or "/"}', p.url)
    guardian = []
    for _, d in loaded.form_data:
        for f in d.get("forms") or []:
            for x in f.get("fields") or []:
                label = " ".join(str(x.get(k) or "") for k in ("label", "name", "id", "placeholder"))
                for c, rx in _FIELDS.items():
                    mm = re.search(rx, label, re.I)
                    if mm and not (c == CHILDREN and mm.group(0).lower() == "age" and str(x.get("type") or "") == "hidden"):
                        add(c, FIELD, f'field "{label.strip()[:60]}"', d.get("page_url") or "")
                if GUARDIAN_FIELD.search(label):
                    guardian.append(d.get("page_url") or "")
    for _, n in loaded.networks:
        for h in n.get("hosts") or []:
            for c, rx in _SERVICES.items():
                if h.get("third_party") and re.search(rx, f"{h.get('host', '')} {h.get('vendor', '')}", re.I):
                    add(c, SERVICE, h.get("vendor") or h.get("host", ""), n.get("page_url") or "")

    stated, nfc, dis = [], [], []
    from app.enterprise.analysis import consolidate as co
    own = {co.PRIVACY, co.TERMS, co.COOKIE, co.GRIEVANCE, co.CONTACT, co.ABOUT}
    home = (m.root_url or "").rstrip("/")
    for s in loaded.texts:
        # What the site says about ITSELF: its notice, terms, about, contact and home pages. A blog post or a news article about other companies ("an e-commerce entity
        # with two crore users must ...") is not a statement of what this site is.
        if s.category not in own and s.url.rstrip("/") != home:
            continue
        body = (s.text or "").split("[shadow DOM]")[0]
        for c, rx in _SELF.items():
            for mm in re.finditer(rx, body, re.I):
                add(c, SELF, f'"{_sentence(body, mm)}"', s.url)
                break
        for c, rx in _TEXT.items():
            mm = re.search(rx, body, re.I)
            if mm:
                add(c, TEXT, f'"{_sentence(body, mm)}"', s.url)
        for mm in NOT_FOR_CHILDREN.finditer(body):
            nfc.append({"text": _sentence(body, mm), "url": s.url})
            break
        for mm in DISABILITY.finditer(body):
            dis.append({"text": _sentence(body, mm), "url": s.url})
            break
        for n_, t in users_stated(body):
            stated.append({"count": n_, "text": t, "url": s.url})

    cats, possible, conf = [], [], {}
    for c in (ECOMMERCE, GAMING, SOCIAL, EDTECH, CHILDREN):
        kinds = {s.kind for s in sig if s.category == c}
        if not kinds:
            continue
        if SELF in kinds or len(kinds) >= 3:
            conf[c] = "high"
        elif len(kinds) == 2:
            conf[c] = "medium"
        else:
            conf[c] = "low"
        # E-commerce needs a shop page or the site's own statement: a payment gateway and a checkout on a services site do not make it a store.
        shop_page = any(s.category == ECOMMERCE and s.kind == PATH and re.search(r"cart|basket|bag|shop|store|product|collection|buy now|wishlist", s.detail, re.I) for s in sig)
        if c == ECOMMERCE and SELF not in kinds and not shop_page:
            conf[c] = "low"
        (cats if conf[c] in ("high", "medium") else possible).append(c)
    order = [GAMING, SOCIAL, ECOMMERCE, CHILDREN, EDTECH]
    primary = next((c for c in order if c in cats and conf[c] == "high"), next((c for c in order if c in cats), GENERAL))
    stated.sort(key=lambda x: -x["count"])
    seen, uniq = set(), []
    for x in stated:
        if (x["count"], x["url"]) not in seen:
            seen.add((x["count"], x["url"]))
            uniq.append(x)
    return SiteCategory(primary, cats, possible, conf, sig, uniq[:5], nfc[:3], dis[:3], sorted(set(u for u in guardian if u)))
