"""Which kind of website this is, and which parts of the DPDP Act and Rules depend on that.

What the law actually ties to a sector (checked against the Act and Rules in knowledge_sources/):
  * Third Schedule (Rules 2025, rule 8): e-commerce entities with at least two crore registered users in India must erase
    most personal data three years after the person last used the service, after 48 hours' notice.
  * Fourth Schedule Part A (rule 12): clinical establishments, mental health establishments, healthcare professionals, allied
    healthcare professionals and educational institutions are exempt from s.9(1) and (3) (parental consent; no tracking or
    behavioural monitoring of children) ONLY for processing restricted to the purposes the Schedule names.
  * Section 10(1)(a): the volume and sensitivity of the data are factors in notifying a Significant Data Fiduciary.
  * Section 8(7) with illustration (II): retention that another law requires (the illustration is a bank) overrides erasure.
  * Section 17(1)(f): a financial institution may process data to ascertain the assets and liabilities of a loan defaulter.
  * Section 7(a): processing for the purpose a person voluntarily gave her data for (the Act's illustrations are a pharmacy and
    a real estate broker), and no further once she says she no longer needs it.
  * Sections 7(b), 17(2)(a) and 17(4): the State and notified instrumentalities have their own uses and exemptions. A private
    vendor to the State is not the State.
Everything else in the Act applies to every data fiduciary alike. There is no separate rule set for manufacturers, hotels,
software firms or law firms; for those the module says so rather than inventing one.

The sector is INFERRED from what the scan stored: page titles and paths, form field names, cookie names and the names of
third-party services, with the host name as a hint. A match on the host name alone is never used: something else on the site
has to agree with it. It is indicative, never a finding, and it NEVER changes a score. Whether an entity-specific duty applies
is a fact about the organisation, not the website, so those requirements stay excluded unless declared (see Applicability).
The organisation may also declare its own sector; that is business context the scan cannot see, and the scan's own reading is
then shown beside it as a cross-check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

SECTORS: dict[str, str] = {
    "healthcare": "Healthcare",
    "education": "Education",
    "bfsi": "BFSI (banking and financial services)",
    "insurance": "Insurance",
    "real_estate": "Real estate",
    "retail": "Retail and e-commerce",
    "manufacturing": "Manufacturing",
    "hospitality": "Hospitality",
    "it": "IT services",
    "saas": "SaaS",
    "technology": "Technology and AI",
    "government_vendor": "Government vendor",
    "legal": "Legal services",
    "general": "General website",
}

# (word or phrase, weight). Strong terms are specific to the sector; weak terms also occur elsewhere.
_STRONG, _WEAK = 3, 1


def _lex(strong: tuple[str, ...], weak: tuple[str, ...]) -> list[tuple[str, int]]:
    return [(w, _STRONG) for w in strong] + [(w, _WEAK) for w in weak]


_LEXICON: dict[str, list[tuple[str, int]]] = {
    "healthcare": _lex(
        ("hospital", "hospitals", "clinic", "doctor", "doctors", "patient", "patients", "medical", "healthcare", "health care", "diagnostic", "diagnostics",
         "pathology", "pharmacy", "physician", "surgeon", "telemedicine", "dental", "dentist", "ayush", "nursing", "radiology"),
        ("health", "wellness", "treatment", "symptom", "symptoms", "medicine", "therapy")),
    "education": _lex(
        ("school", "college", "university", "admission", "admissions", "student", "students", "curriculum", "academy", "tutor", "scholarship", "campus",
         "learner", "learners", "enrolment", "enrollment", "alumni"),
        ("course", "courses", "institute", "exam", "exams", "training", "learning")),
    "bfsi": _lex(
        ("bank", "banking", "loan", "loans", "nbfc", "credit card", "demat", "fintech", "emi", "ifsc", "wealth management", "stock broking", "brokerage",
         "lending", "mutual fund", "mutual funds", "net banking", "internet banking", "fixed deposit", "home loan", "rbi", "sebi"),
        ("finance", "financial", "investment", "investments", "payments", "tax", "savings")),
    "insurance": _lex(
        ("insurance", "insurer", "insurers", "policyholder", "policyholders", "policy holder", "claim settlement", "irdai", "sum assured", "term plan",
         "health insurance", "motor insurance", "life insurance", "premium calculator", "claims"),
        ("premium", "policy number", "claim", "nominee", "renewal")),
    "real_estate": _lex(
        ("real estate", "realty", "property", "properties", "apartment", "apartments", "flats", "villa", "villas", "rera", "site visit", "floor plan",
         "floor plans", "possession", "township", "plots", "housing", "square feet", "sq ft", "bhk", "builder"),
        ("residential", "commercial space", "rent", "lease", "location advantages", "amenities")),
    "retail": _lex(
        ("add to cart", "cart", "checkout", "wishlist", "buy now", "shop now", "add to bag", "free shipping", "coupon", "shopify", "woocommerce",
         "magento", "bigcommerce", "store locator", "our stores", "showroom", "outlets", "loyalty programme", "loyalty program", "gift card"),
        ("shop", "store", "order", "orders", "product", "products", "shipping", "delivery", "sale", "offers")),
    "manufacturing": _lex(
        ("manufacturing", "manufacturer", "manufacturers", "factory", "industrial", "fabrication", "machinery", "oem", "foundry", "spare parts",
         "iso 9001", "production capacity", "assembly line", "plant"),
        ("industries", "equipment", "supply chain", "export", "engineering", "products")),
    "hospitality": _lex(
        ("hotel", "hotels", "resort", "resorts", "check in", "check-in", "check out", "check-out", "suites", "banquet", "room tariff", "hospitality", "homestay",
         "guest", "guests", "restaurant", "restaurants", "stay"),
        ("rooms", "booking", "book now", "dining", "travel", "tour", "spa")),
    "it": _lex(
        ("it services", "it consulting", "managed services", "systems integrator", "outsourcing", "digital transformation", "application development",
         "software development", "offshore", "staffing", "it solutions"),
        ("consulting", "solutions", "enterprise", "services", "digital")),
    "saas": _lex(
        ("saas", "free trial", "start free", "book a demo", "request a demo", "per user", "integrations", "api documentation", "pricing plans", "sign up free",
         "per month"),
        ("platform", "dashboard", "pricing", "subscription", "api", "workflow", "demo")),
    "technology": _lex(
        ("artificial intelligence", "machine learning", "generative ai", "agentic", "cloud", "devops", "data analytics", "cybersecurity", "iot", "blockchain",
         "robotics", "deep learning", "software", "technology partner"),
        ("technology", "technologies", "ai", "automation", "engineering", "innovation")),
    "government_vendor": _lex(
        ("tender", "tenders", "gem portal", "gem seller", "e-governance", "egovernance", "empanelled", "empanelment", "empanelment", "public sector", "psu",
         "smart city", "citizen services", "government projects", "government clients", "govt", "rfp", "rfq"),
        ("government", "ministry", "municipal", "department", "public services")),
    "legal": _lex(
        ("law firm", "advocate", "advocates", "attorney", "attorneys", "litigation", "lawyer", "lawyers", "solicitor", "notary", "arbitration",
         "legal services", "barrister"),
        ("legal", "law", "counsel", "compliance")),
}
_COMPILED = {s: [(re.compile(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])"), w, wt) for w, wt in words] for s, words in _LEXICON.items()}

# How much one distinct match is worth, by where it was seen. A field name or a shop platform says more than a page title.
_SOURCE_WEIGHT = {"host": 2, "title": 1, "path": 1, "form field": 2, "service": 3, "cookie": 3}

# Third-party services and cookies that are themselves evidence of a sector. A payment gateway is NOT: any business takes payments.
_SERVICE_SECTORS = {
    "retail": ("shopify", "woocommerce", "magento", "bigcommerce"),
    "hospitality": ("siteminder", "cloudbeds", "staah", "rategain", "bookingjini", "beds24"),
}
_COOKIE_SECTORS = {
    "retail": ("woocommerce", "_shopify", "shopify", "wp_woocommerce_session"),
}
_PAYMENT_SERVICES = ("razorpay", "payu", "cashfree", "paytm", "billdesk", "ccavenue", "phonepe", "stripe", "paypal")

_CHILD_FIELD = re.compile(r"parent|guardian|date[_ -]?of[_ -]?birth|\bdob\b|birth|\bage\b|\bgrade\b|\bclass\b|student|ward|child|children|infant|\bkids?\b|minor", re.I)
_HEALTH_FIELD = re.compile(r"symptom|diagnos|medical|patient|prescription|blood|allerg|disease|condition|health|insurance[_ -]?id|doctor|appointment|pre[_ -]?existing", re.I)
_FINANCE_FIELD = re.compile(r"\bpan\b|account[_ -]?(no|number)|ifsc|card|cvv|iban|salary|income|loan|credit|aadhaar|aadhar", re.I)
_LEGAL_FIELD = re.compile(r"\bcase\b|matter|dispute|legal|claim", re.I)
_IDENTITY_FIELD = re.compile(r"\bpan\b|aadhaar|aadhar|passport|nationality|id[_ -]?(proof|number)|driving|voter", re.I)
_BOOKING_FIELD = re.compile(r"check[_ -]?in|check[_ -]?out|arrival|departure|guests?|rooms?|adults|nights", re.I)
_POLICY_FIELD = re.compile(r"policy|nominee|claim|sum[_ -]?assured|vehicle|registration[_ -]?(no|number)|pre[_ -]?existing|medical[_ -]?history", re.I)
_ENQUIRY_FIELD = re.compile(r"budget|bhk|property|unit|site[_ -]?visit|plot|location|requirement|enquiry|inquiry|message", re.I)
_BUSINESS_FIELD = re.compile(r"company|organi[sz]ation|gst|gstin|designation|quantity|requirement|rfq|tender|resume|\bcv\b|department", re.I)

# The threshold a sector needs to be named, and the lead it needs over the runner-up.
MIN_SCORE = 5
MIN_LEAD = 2
HIGH_SCORE, HIGH_LEAD = 12, 5
# Evidence from outside the host name has to be corroborated: two kinds of source, or three different terms from one.
MIN_SOURCE_KINDS = 2
MIN_DISTINCT_TERMS = 3


@dataclass(frozen=True)
class SectorSignal:
    sector: str
    source: str   # host, title, path, form field, service, cookie
    match: str    # the word that matched
    seen_in: str  # where: the host, the page title, the path, the field name, the service or the cookie
    points: int


@dataclass(frozen=True)
class SectorNote:
    id: str
    title: str
    provisions: list[str]
    text: str
    observed: list[str]
    action: str


@dataclass(frozen=True)
class DeclarationPrompt:
    flag: str
    requirement_id: str
    label: str
    why: str
    declared: bool | None


@dataclass(frozen=True)
class SectorAssessment:
    sector: str
    label: str
    confidence: str                                    # high | medium | low | declared
    summary: str
    signals: list[SectorSignal]
    also_possible: list[str]
    notes: list[SectorNote]
    declarations: list[DeclarationPrompt]
    limitations: list[str]
    sector_scores: dict[str, int] = field(default_factory=dict)
    # Business context: the sector the organisation declared, if any, and what the scan alone made of the site.
    declared: bool = False
    detected_sector: str | None = None
    agrees_with_scan: bool | None = None


# ---------------------------------------------------------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------------------------------------------------------

def _host(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url if "//" in url else f"//{url}").hostname or "").lower()


def _match(sector: str, text: str, source: str = "") -> list[tuple[str, int]]:
    low = (text or "").lower()
    found = [(w, wt) for rx, w, wt in _COMPILED[sector] if rx.search(low)]
    if source == "host":
        # A host name is often one run-together word ("apollohospitals"), so a long sector term may sit inside it. Only terms of
        # six or more letters are matched this way, so short ones ("ai", "api", "emi") never match by accident.
        squashed = low.replace(" ", "")
        have = {w for w, _ in found}
        found += [(w, wt) for _, w, wt in _COMPILED[sector] if wt == _STRONG and " " not in w and len(w) >= 6 and w in squashed and w not in have]
    return found


def _collect(evidence: dict, scan_url: str | None) -> list[tuple[str, str]]:
    """(source, text) pairs the scan stored. Page body text is not stored, so it cannot be used."""
    items: list[tuple[str, str]] = []
    host = _host(scan_url)
    if host:
        items.append(("host", host.replace("-", " ").replace(".", " ")))
    for p in evidence.get("pages") or []:
        if p.get("title"):
            items.append(("title", p["title"]))
        path = urlparse(p.get("url") or "").path
        if path.strip("/"):
            items.append(("path", re.sub(r"[-_/]+", " ", path)))
    for f in evidence.get("forms") or []:
        for fld in f.get("fields") or []:
            items.append(("form field", re.sub(r"[-_]+", " ", fld.get("name") or "")))
    return items


def detect_sector_signals(evidence: dict, scan_url: str | None) -> tuple[dict[str, int], list[SectorSignal]]:
    scores = {s: 0 for s in SECTORS if s != "general"}
    signals: list[SectorSignal] = []
    seen: set[tuple[str, str, str]] = set()   # one match counts once per source kind, however many pages repeat it
    for source, text in _collect(evidence, scan_url):
        for sector in scores:
            for word, weight in _match(sector, text, source):
                key = (sector, source, word)
                if key in seen:
                    continue
                seen.add(key)
                points = weight * _SOURCE_WEIGHT[source]
                scores[sector] += points
                signals.append(SectorSignal(sector, source, word, text.strip()[:80], points))
    for svc in evidence.get("third_party_services") or []:
        name = (svc.get("service_name") or "").lower()
        for sector, names in _SERVICE_SECTORS.items():
            for n in names:
                if n in name and (sector, "service", n) not in seen:
                    seen.add((sector, "service", n))
                    points = _STRONG * _SOURCE_WEIGHT["service"] // 2
                    scores[sector] += points
                    signals.append(SectorSignal(sector, "service", n, svc.get("service_name") or n, points))
    for ck in evidence.get("cookies") or []:
        name = (ck.get("name") or "").lower()
        for sector, names in _COOKIE_SECTORS.items():
            for n in names:
                if n in name and (sector, "cookie", n) not in seen:
                    seen.add((sector, "cookie", n))
                    points = _STRONG * _SOURCE_WEIGHT["cookie"] // 2
                    scores[sector] += points
                    signals.append(SectorSignal(sector, "cookie", n, ck.get("name") or n, points))
    return scores, signals


def _corroborated(sector: str, signals: list[SectorSignal]) -> bool:
    """Whether the evidence for `sector` rests on more than the host name: two kinds of source, or three different terms from one."""
    own = [s for s in signals if s.sector == sector and s.source != "host"]
    kinds = {s.source for s in own}
    terms = {s.match for s in own}
    return len(kinds) >= MIN_SOURCE_KINDS or len(terms) >= MIN_DISTINCT_TERMS


# ---------------------------------------------------------------------------------------------------------------------------
# What the scan saw that bears on the sector's provisions
# ---------------------------------------------------------------------------------------------------------------------------

def _field_names(evidence: dict) -> list[str]:
    return [fld.get("name") or "" for f in evidence.get("forms") or [] for fld in f.get("fields") or []]


def _matching_fields(evidence: dict, rx: re.Pattern) -> list[str]:
    seen: list[str] = []
    for name in _field_names(evidence):
        if name and rx.search(name.replace("-", " ").replace("_", " ")) and name not in seen:
            seen.append(name)
    return seen


def _fields_line(evidence: dict, rx: re.Pattern, what: str, none: str) -> str:
    found = _matching_fields(evidence, rx)
    return f"Fields that suggest {what}: {', '.join(found)}." if found else none


def _tracking_line(evidence: dict) -> str | None:
    t = [x for x in evidence.get("trackers") or [] if x.get("category") in ("analytics", "marketing")]
    if not t:
        return None
    vendors = sorted({x.get("vendor") or "unclassified" for x in t})
    before = sum(1 for x in t if "pre_consent" in (x.get("consent_states") or []))
    return (f"{len(t)} analytics or marketing request(s) seen ({', '.join(vendors[:6])}{', …' if len(vendors) > 6 else ''}); "
            f"{before} of them before any consent choice.")


def _payment_services(evidence: dict) -> list[str]:
    names = {(s.get("service_name") or "") for s in evidence.get("third_party_services") or []}
    names |= {(t.get("vendor") or "") for t in evidence.get("trackers") or []}
    return sorted(n for n in names if any(p in n.lower() for p in _PAYMENT_SERVICES))


def _policy_line(evidence: dict) -> str:
    kinds = sorted({(p.get("policy_type") or "").replace("_", " ") for p in evidence.get("policies") or [] if p.get("policy_type")})
    return f"Policy pages found: {', '.join(kinds)}." if kinds else "No privacy, cookie or terms page was found on the scanned pages."


def _banner_line(evidence: dict) -> str:
    signals = evidence.get("consent_signals") or []
    if not signals or signals[0].get("mechanism_type") in (None, "none"):
        return "No consent banner was detected."
    s = signals[0]
    return f"Consent banner: {s.get('cmp_vendor') or s.get('mechanism_type')}; reject-all {'present' if s.get('has_reject_all') else 'not seen'}."


def _notes_for(sector: str, evidence: dict) -> list[SectorNote]:
    children = _matching_fields(evidence, _CHILD_FIELD)
    tracking = _tracking_line(evidence)
    forms = evidence.get("forms") or []
    form_line = f"{len(forms)} form(s) with {len(_field_names(evidence))} field(s) were found." if forms else "No forms were found on the scanned pages."
    notice = _policy_line(evidence)
    notes: list[SectorNote] = []

    child_obs = ([f"Fields that suggest children's data: {', '.join(children)}."] if children else ["No form field suggesting children's data was seen."])
    if tracking:
        child_obs.append(tracking)
    tail = ([tracking] if tracking else [])

    if sector == "healthcare":
        notes.append(SectorNote(
            "healthcare-children-exemption", "Children's data and the healthcare exemption",
            ["DPDP Act s.9(1), s.9(3), s.9(4)", "DPDP Rules r.12, Fourth Schedule Part A items 1 and 2"],
            "A clinical establishment, mental health establishment, healthcare professional or allied healthcare professional is exempt from "
            "parental consent and from the ban on tracking children only for processing restricted to providing health services to the child, "
            "to the extent necessary to protect her health (allied professionals: supporting a treatment and referral plan). The exemption "
            "belongs to that kind of entity. A health-information site, a pharmacy or a wellness brand that is none of those has no exemption.",
            child_obs,
            "Declare whether the organisation processes children's data (DPDP-X10) and whether it is one of the named healthcare entities.",
        ))
        notes.append(SectorNote(
            "healthcare-sensitivity", "Health data and Significant Data Fiduciary status",
            ["DPDP Act s.10(1)(a)"],
            "The Act has no separate 'sensitive data' category and no healthcare-specific notice rule. Sensitivity of the data is one of the factors "
            "the Central Government weighs in notifying a Significant Data Fiduciary, which adds duties (an India-based Data Protection Officer, an "
            "independent audit and periodic impact assessments). Only a notified entity has those duties.",
            [_fields_line(evidence, _HEALTH_FIELD, "health information", "No form field suggesting health information was seen.")] + tail,
            "Declare Significant Data Fiduciary status (DPDP-X13) only if the organisation has been notified.",
        ))
    elif sector == "education":
        notes.append(SectorNote(
            "education-children", "Children's data and the educational-institution exemption",
            ["DPDP Act s.9(1), s.9(3), s.9(4)", "DPDP Rules r.10, r.12, Fourth Schedule Part A item 3"],
            "Verifiable parental consent is needed before processing a child's data, and tracking, behavioural monitoring and targeted advertising "
            "directed at children are barred. An educational institution is exempt from both only for tracking and behavioural monitoring for its "
            "educational activities or for the safety of enrolled children. Marketing trackers, advertising and any other processing are outside "
            "that exemption. A coaching brand or course marketplace is a data fiduciary the same way; whether it is an 'educational institution' "
            "(an institution of learning, including vocational) is a fact about the organisation.",
            child_obs,
            "Declare whether the organisation processes children's data (DPDP-X10).",
        ))
    elif sector == "bfsi":
        pay = _payment_services(evidence)
        notes.append(SectorNote(
            "bfsi-retention", "Retention required by other law",
            ["DPDP Act s.8(7), illustration (II)"],
            "A data fiduciary must erase personal data when consent is withdrawn or the purpose is served, unless retention is necessary for "
            "compliance with a law. The Act's own illustration is a bank that must keep client identity records for ten years after an account closes. "
            "The notice should therefore say what is kept and for how long. Sector laws that require retention are not assessed here.",
            [_fields_line(evidence, _FINANCE_FIELD, "financial or identity data", "No form field suggesting financial data was seen.")]
            + ([f"Payment services seen: {', '.join(pay)}."] if pay else []) + [notice],
            "Check that the privacy notice states retention periods and the law that requires them.",
        ))
        notes.append(SectorNote(
            "bfsi-defaulters-sdf", "Loan recovery and Significant Data Fiduciary status",
            ["DPDP Act s.17(1)(f)", "DPDP Act s.10(1)(a)"],
            "A financial institution may process data to ascertain the financial information, assets and liabilities of a person who has defaulted on "
            "a loan, subject to the law on disclosure of such information. That is the only finance-specific carve-out in the Act. The volume and "
            "sensitivity of the data are also factors in notifying a Significant Data Fiduciary; the Act has no other finance rule set.",
            tail, "Declare Significant Data Fiduciary status (DPDP-X13) only if the organisation has been notified.",
        ))
    elif sector == "insurance":
        notes.append(SectorNote(
            "insurance-health-retention", "Health details in proposals, and retention",
            ["DPDP Act s.8(7)", "DPDP Act s.10(1)(a)"],
            "Proposal and claim forms often ask for medical history. The Act has no separate sensitive-data category, but sensitivity and volume "
            "are factors for Significant Data Fiduciary notification. Erasure on withdrawal or when the purpose ends yields to retention that "
            "another law requires, so the notice should say what an insurer keeps and why. Insurance regulations themselves are not assessed.",
            [_fields_line(evidence, _HEALTH_FIELD, "health information", "No form field suggesting health information was seen."),
             _fields_line(evidence, _POLICY_FIELD, "policy or claim details", "No form field suggesting policy or claim details was seen."), notice],
            "Check that proposal forms state their purpose and that the notice gives retention periods. Declare Significant Data Fiduciary status "
            "(DPDP-X13) only if notified.",
        ))
        notes.append(SectorNote(
            "insurance-nominees-children", "Nominees and insured children",
            ["DPDP Act s.9(1), s.9(3)", "DPDP Rules r.10"],
            "A nominee or an insured child may be under eighteen. Processing a child's data needs verifiable parental consent, and tracking or "
            "targeted advertising directed at children is barred. The Fourth Schedule exemptions do not name insurers.",
            child_obs, "Declare whether the organisation processes children's data (DPDP-X10).",
        ))
    elif sector == "real_estate":
        notes.append(SectorNote(
            "real-estate-enquiries", "Enquiries, site visits and follow-up marketing",
            ["DPDP Act s.7(a), illustration (II)", "DPDP Act s.5, s.6"],
            "The Act's own illustration is a real estate broker: a person who shares her details to find a rental may be served for that purpose, and "
            "once she says she no longer needs help the broker must stop processing her data. Enquiry, site-visit and brochure forms are the usual "
            "collection points. Using those details for unrelated marketing, or passing them to other brokers, needs its own notice and consent. "
            "The Act has no real-estate-specific rules.",
            [form_line, _fields_line(evidence, _ENQUIRY_FIELD, "enquiry details", "No form field suggesting a property enquiry was seen."),
             _fields_line(evidence, _IDENTITY_FIELD, "identity documents", "No form field asking for identity documents was seen.")] + tail,
            "Check that each enquiry form states its purpose, links the notice, and offers a way to stop being contacted.",
        ))
    elif sector == "retail":
        pay = _payment_services(evidence)
        shop = sorted({s.match for s in detect_sector_signals(evidence, None)[1] if s.sector == "retail"})
        notes.append(SectorNote(
            "retail-erasure", "Erasure after inactivity for large e-commerce entities",
            ["DPDP Rules r.8(1), r.8(2), Third Schedule item 1", "DPDP Act s.8(7)"],
            "An e-commerce entity with at least two crore registered users in India must erase personal data for most purposes three years after "
            "the person last used the service or exercised her rights, and must warn her at least 48 hours before. Logging in to her account and "
            "using a stored virtual token are excluded. A seller on someone else's marketplace is not covered. Smaller shops, and shops with no "
            "online store, are not in this class but still owe erasure when consent is withdrawn or the purpose ends (s.8(7)), unless a law requires retention.",
            [f"Shop signals: {', '.join(shop[:6]) or 'none'}.", f"Payment services seen: {', '.join(pay)}." if pay else "No payment service was seen on the scanned pages.", form_line],
            "Declare whether the entity meets the Third Schedule threshold (DPDP-X8). A website scan cannot count registered users.",
        ))
        notes.append(SectorNote(
            "retail-purpose-children", "Purchase purpose, loyalty data and advertising to children",
            ["DPDP Act s.7(a), illustration (I)", "DPDP Act s.9(3)"],
            "The Act's illustration is a pharmacy: a customer's phone number given for a payment receipt may be used to send the receipt. Loyalty and "
            "marketing uses need their own notice and consent. Targeted advertising directed at children is barred for every data fiduciary, so retail "
            "sites that run marketing trackers should know whether their audience includes children.",
            child_obs, "Declare whether the organisation processes children's data (DPDP-X10).",
        ))
    elif sector == "manufacturing":
        notes.append(SectorNote(
            "manufacturing-contacts", "Business contacts, careers and supplier forms",
            ["DPDP Act s.2(t)", "DPDP Act s.5, s.6", "DPDP Act s.8(2)"],
            "The Act has no manufacturing-specific rules. A manufacturer's website still collects personal data: the names and work emails of "
            "buyers who send an enquiry, and the CVs of job applicants. Personal data means data about an identifiable individual, whether or not "
            "the transaction is business to business. A processor engaged to handle such data (for example a recruitment or CRM vendor) must be "
            "engaged under a valid contract.",
            [form_line, _fields_line(evidence, _BUSINESS_FIELD, "business or recruitment details", "No form field suggesting business or recruitment details was seen.")] + tail,
            "Check that enquiry and careers forms link to the notice and state their purpose.",
        ))
    elif sector == "hospitality":
        notes.append(SectorNote(
            "hospitality-guests", "Guest bookings, identity details and family stays",
            ["DPDP Act s.7(a)", "DPDP Act s.5, s.6", "DPDP Act s.9(1), s.9(3)"],
            "The Act has no hospitality-specific rules. A booking is data given for a purpose (s.7(a)), and marketing to past guests needs its own notice "
            "and consent. Booking forms may ask for identity documents or nationality, and for the number or ages of children travelling; a child's "
            "data needs verifiable parental consent, and tracking or targeted advertising directed at children is barred. If another law requires "
            "guest records to be kept, erasure yields to it (s.8(7)).",
            [form_line, _fields_line(evidence, _BOOKING_FIELD, "booking details", "No form field suggesting a booking was seen."),
             _fields_line(evidence, _IDENTITY_FIELD, "identity documents", "No form field asking for identity documents was seen."), *child_obs],
            "Declare whether the organisation processes children's data (DPDP-X10) if bookings can include minors.",
        ))
    elif sector in ("it", "saas", "technology"):
        notes.append(SectorNote(
            f"{sector}-roles", "Fiduciary or processor, and business contacts",
            ["DPDP Act s.8(2)", "DPDP Act s.2(t)"],
            "Personal data means any data about an identifiable individual, so the names and work emails of business contacts are covered. A company "
            "that processes data on a customer's behalf is a Data Processor, which a Data Fiduciary may engage only under a valid contract; the "
            "same company is a Data Fiduciary for its own website visitors and leads. The Act has no IT-, SaaS- or technology-specific rule set.",
            [form_line] + tail,
            "Check which role applies to the data on this site: the visitor and lead data is the company's own.",
        ))
    elif sector == "government_vendor":
        notes.append(SectorNote(
            "government-vendor-not-the-state", "A vendor to the State is not the State",
            ["DPDP Act s.7(b)", "DPDP Act s.17(2)(a), s.17(4)", "DPDP Act s.8(2)"],
            "The Act lets the State and its instrumentalities process data for subsidies, benefits, services, certificates, licences and permits, and "
            "lets the Central Government notify instrumentalities for exemption (s.17(2)(a), s.17(4)). A private company that supplies or operates a "
            "service for a government body is not the State: where it handles data for the body it is a Data Processor, engaged under a valid contract "
            "(s.8(2)); for its own website visitors, leads and staff it is a Data Fiduciary with every general duty.",
            [form_line, _fields_line(evidence, _BUSINESS_FIELD, "tender, business or recruitment details", "No form field suggesting tender or business details was seen.")] + tail + [notice],
            "Check the role for each data set (processor for the body's data, fiduciary for the company's own) and that the contract says so.",
        ))
    elif sector == "legal":
        notes.append(SectorNote(
            "legal-general", "No legal-sector provisions",
            ["DPDP Act s.5, s.6", "DPDP Act s.17(1)(a)"],
            "The Act and Rules have no provisions specific to law firms or legal services. Enquiry and intake forms collect personal data and need "
            "the same notice and consent as any other. Processing necessary for enforcing a legal right or claim is carved out of Chapter II "
            "(s.17(1)(a)), but that is a ground for the processing, not a general exemption for a firm. Professional confidentiality duties under "
            "other law are not assessed.",
            [form_line, _fields_line(evidence, _LEGAL_FIELD, "case details", "No form field suggesting case details was seen.")],
            "Check that each enquiry form links to the privacy notice and states its purpose.",
        ))
    else:
        notes.append(SectorNote(
            "general", "General duties",
            ["DPDP Act s.5, s.6, s.8"],
            "No sector was identified with enough evidence. The general duties apply to every data fiduciary, and the entity-specific "
            "requirements stay excluded unless declared.",
            [form_line, notice, _banner_line(evidence)] + tail,
            "Declare any entity-specific applicability that is known, or declare the sector so the relevant provisions can be described.",
        ))
    return notes


_CHILD_SECTORS = ("education", "healthcare", "insurance", "hospitality")
_SDF_SECTORS = ("bfsi", "insurance", "healthcare", "retail", "government_vendor")


def _declarations(sector: str, evidence: dict, applicability) -> list[DeclarationPrompt]:
    def flag(key: str):
        return applicability.flag(key) if applicability is not None else None

    out: list[DeclarationPrompt] = []
    children = bool(_matching_fields(evidence, _CHILD_FIELD))
    if sector in _CHILD_SECTORS or children:
        out.append(DeclarationPrompt(
            "children", "DPDP-X10", "Processes children's personal data",
            "The form fields suggest children may be involved." if sector not in _CHILD_SECTORS
            else "This kind of service can involve children, and the Fourth Schedule exemptions apply only to named entities and purposes.",
            flag("children")))
    if sector == "retail":
        out.append(DeclarationPrompt("erasure_class", "DPDP-X8", "E-commerce entity with at least two crore registered users in India",
                                     "Third Schedule item 1. A website scan cannot count registered users.", flag("erasure_class")))
    if sector in _SDF_SECTORS:
        out.append(DeclarationPrompt("significant", "DPDP-X13", "Notified Significant Data Fiduciary",
                                     "Notification under s.10 considers the volume and sensitivity of the data. Only the Central Government can make it.", flag("significant")))
    return out


_LIMITATIONS = [
    "The sector is inferred from page titles and paths, form field names, cookie names and third-party services, with the host name only as a hint that something else must agree with. The scan stores no page body text, so it is an indication, not a finding.",
    "The sector never changes the score. Duties that depend on what the organisation is, rather than what its website shows, stay excluded unless declared.",
    "Laws outside the DPDP Act and Rules, such as rules set by sector regulators, are not assessed.",
]


def assess_sector(evidence: dict, scan_url: str | None = None, applicability=None, declared_sector: str | None = None) -> SectorAssessment:
    scores, signals = detect_sector_signals(evidence, scan_url)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, top_score = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0
    lead = top_score - runner
    host_only = False
    if top_score >= MIN_SCORE and lead >= MIN_LEAD:
        if _corroborated(top, signals):
            detected, confidence = top, ("high" if top_score >= HIGH_SCORE and lead >= HIGH_LEAD else "medium")
        else:
            detected, confidence, host_only = "general", "low", True
    else:
        detected, confidence = "general", "low"

    declared = declared_sector if declared_sector in SECTORS and declared_sector != "general" else None
    sector = declared or detected
    shown = sorted([s for s in signals if s.sector == sector], key=lambda s: s.points, reverse=True)[:8]
    also = [SECTORS[s] for s, sc in ranked if s != sector and sc >= MIN_SCORE - 1 and s != detected] if declared is None else \
        ([SECTORS[detected]] if detected not in (sector, "general") else [])
    if declared:
        reading = ("the scan's own reading is the same" if detected == declared else
                   f"the scan's own reading was {SECTORS[detected].lower()} ({confidence} confidence)" if detected != "general" else
                   "the scan found too little evidence to name a sector")
        summary = f"Declared by the organisation as {SECTORS[declared].lower()}; {reading}."
        confidence = "declared"
    elif detected == "general":
        summary = ("A name in the host matched a sector, but nothing else on the scanned pages agreed with it, so it was not used."
                   if host_only else "No sector was identified with enough evidence, so only the general duties are described.")
    else:
        summary = (f"The scan points to the {SECTORS[detected]} sector ({confidence} confidence), from "
                   f"{len([s for s in signals if s.sector == detected])} signal(s) in the scan.")
    return SectorAssessment(
        sector=sector, label=SECTORS[sector], confidence=confidence, summary=summary, signals=shown, also_possible=also,
        notes=_notes_for(sector, evidence), declarations=_declarations(sector, evidence, applicability),
        limitations=list(_LIMITATIONS), sector_scores=scores,
        declared=declared is not None, detected_sector=detected,
        agrees_with_scan=(detected == declared) if declared else None,
    )
