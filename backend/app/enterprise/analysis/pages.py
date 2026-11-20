"""Deterministic classification of a stored page or document: what kind of page is it?

Categories: privacy_policy, cookie_policy, terms, contact, grievance, about, blog_article, product, other.

The rule that matters most: a BLOG ARTICLE is never a policy, however often it says "privacy" or "DPDP". (A past bug picked /blog/dpdp-act-2023-compliance-guide as the policy.) So:
  1. A path segment that marks editorial content (blog, news, article, insights, press, stories ...) or a dated path (/2024/05/...) makes the page a blog article, final.
  2. A last path segment that is exactly a policy slug (privacy-policy, cookie-policy, terms-of-use, grievance-redressal ...) decides the category.
  3. Otherwise the content decides. A policy needs a title (or first heading) that IS a policy name, with policy-style section headings (information collected, use, sharing, retention,
     rights, security, children, contact, changes). Article cues (a byline, "posted on", "min read", "related posts") without policy headings make an article.
  4. A page that is neither clearly a policy nor clearly something else is `ambiguous`: the pipeline asks the model one tiny question about it (`ambiguous_prompt`). If that fails the page
     stays `other`, and the reason is recorded; nothing is silently promoted to a policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

PRIVACY, COOKIE, TERMS, CONTACT, GRIEVANCE, ABOUT, ARTICLE, PRODUCT, OTHER = (
    "privacy_policy", "cookie_policy", "terms", "contact", "grievance", "about", "blog_article", "product", "other")
CATEGORIES = (PRIVACY, COOKIE, TERMS, CONTACT, GRIEVANCE, ABOUT, ARTICLE, PRODUCT, OTHER)
#: Categories whose text is read by the model for facts.
ANALYSED = (PRIVACY, COOKIE, TERMS, CONTACT, GRIEVANCE, ABOUT)
#: Categories that together make up "the notice and its companions" when text is searched for what the site declares.
POLICY_LIKE = (PRIVACY, COOKIE, GRIEVANCE)

ARTICLE_SEGMENTS = frozenset({
    "blog", "blogs", "news", "newsroom", "article", "articles", "insights", "insight", "press", "press-release", "press-releases", "stories", "story", "journal", "magazine",
    "posts", "post", "knowledge", "knowledge-base", "learn", "resources", "whitepapers", "webinars", "podcast", "podcasts", "case-studies", "thought-leadership"})
PRODUCT_SEGMENTS = frozenset({"product", "products", "pricing", "solutions", "solution", "services", "service", "features", "platform", "shop", "store", "plans", "demo", "industries"})
_DATED_PATH = re.compile(r"/(19|20)\d{2}/(0?[1-9]|1[0-2])(/|$)")

_SLUGS: tuple[tuple[str, re.Pattern], ...] = (
    (PRIVACY, re.compile(r"(our[-_]|website[-_]|online[-_])?(privacy|data[-_]?privacy|data[-_]?protection|personal[-_]?data)([-_](and|&)[-_]cookies?)?([-_](policy|policies|notice|statement|centre|center|terms))?([-_](india|in|en|v?\d+))?|"
                          r"policy[-_]privacy|privacypolicy|dpdp[-_](policy|notice)|privacy[-_]and[-_]cookie[-_]policy")),
    (COOKIE, re.compile(r"cookies?([-_](policy|policies|notice|statement|settings|preferences))?|cookiepolicy")),
    (TERMS, re.compile(r"terms([-_](of[-_](use|service|services|sale)|and[-_]conditions|conditions|&[-_]conditions))?|tos|terms[-_]conditions|user[-_]agreement|legal[-_]terms|termsofuse")),
    (GRIEVANCE, re.compile(r"grievances?([-_](redressal|redress|officer|policy|mechanism|procedure))*|complaints?([-_](policy|procedure))?|redressal|"
                            r"(privacy|data[-_]principal|your|user|data[-_]subject)[-_]rights([-_](and|&)[-_](grievances?|requests?|complaints?|redressal))?|"
                            r"(privacy|data|rights)[-_]requests?|exercise[-_]your[-_]rights|dsar")),
    (CONTACT, re.compile(r"contact([-_]us)?|contacts|get[-_]in[-_]touch|reach[-_]us|contact[-_]information|enquiry|enquiries")),
    (ABOUT, re.compile(r"about([-_]us)?|company|who[-_]we[-_]are|our[-_]story|our[-_]company")),
)

_TITLE_NAMES: tuple[tuple[str, re.Pattern], ...] = (
    (PRIVACY, re.compile(r"^(our |the |website |online )?(privacy|data protection|data privacy)( (&|and) cookies?)?( (policy|notice|statement|policies|centre|center))?( for .{1,60})?$", re.I)),
    (COOKIE, re.compile(r"^(our |the )?cookies?( (policy|notice|statement|policies))$", re.I)),
    (TERMS, re.compile(r"^(our |the |website )?(terms( (of|and|&) (use|service|services|conditions|sale))?|conditions of use|user agreement)$", re.I)),
    (GRIEVANCE, re.compile(r"^(grievance( redressal| redress)?( officer| policy| mechanism)?|complaints?( policy| procedure)?|"
                           r"(privacy|your|data principal) rights( (&|and) (grievances?|requests?|complaints?))?|rights requests?|exercise your rights)$", re.I)),
    (CONTACT, re.compile(r"^(contact( us)?|get in touch|reach us)$", re.I)),
    (ABOUT, re.compile(r"^(about( us)?|who we are|our story|our company)$", re.I)),
)

_HEADING_GROUPS: dict[str, re.Pattern] = {
    "collect": re.compile(r"information (that )?(we|you)|personal (data|information)|data we (collect|process)|what (data|information)|types of (personal )?(data|information)|categories of (personal )?(data|information)|collection", re.I),
    "use": re.compile(r"how we use|use of (your )?(information|data)|purposes?\b|why we (collect|process)|legal basis|basis for processing", re.I),
    "share": re.compile(r"shar(e|ing)|disclos|third[- ]part|recipients|service providers|processors", re.I),
    "retain": re.compile(r"retention|how long|retain|storage period|erasure|deletion", re.I),
    "rights": re.compile(r"your rights|(data )?(principal|subject)s?'? rights|rights of|access (and|or) correct|your choices", re.I),
    "security": re.compile(r"security|safeguard|protect(ion)? of (your )?(data|information)", re.I),
    "cookies": re.compile(r"cookies|tracking technolog", re.I),
    "children": re.compile(r"children|minors|under (the age of )?1[0-9]", re.I),
    "contact": re.compile(r"contact us|grievance|data protection officer|\bdpo\b|how to reach|complain", re.I),
    "changes": re.compile(r"changes to (this|our)|updates? to (this|our)|amendments?|policy updates|revisions", re.I),
    "consent": re.compile(r"\bconsent\b|withdraw", re.I),
}
_ARTICLE_CUES = (
    re.compile(r"\b(posted|published|updated|written)\s+(on|by)\b", re.I), re.compile(r"\b\d+\s*[- ]?min(ute)?s?\s+read\b", re.I), re.compile(r"\bshare (this|on|via)\b", re.I),
    re.compile(r"\brelated (posts|articles|reading)\b", re.I), re.compile(r"\bleave a (comment|reply)\b", re.I), re.compile(r"\b(tags|categories|filed under)\s*:", re.I),
    re.compile(r"\bby\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b"), re.compile(r"\bread more\b", re.I), re.compile(r"\b(in this (article|post|guide)|this (article|blog post|guide) (explains|covers|will))\b", re.I),
    re.compile(r"\bkey takeaways?\b", re.I),
)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_H = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>|<(?:div|p|span|strong|b)[^>]*role=['\"]heading['\"][^>]*>(.*?)</(?:div|p|span|strong|b)>", re.I | re.S)
_SEP = re.compile(r"\s+[|–—·:~]\s+|\s+-\s+")


@dataclass(frozen=True)
class PageClass:
    category: str
    strength: str                       # "high" | "medium" | "ambiguous"
    reasons: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()    # for an ambiguous page, what it might be

    @property
    def ambiguous(self) -> bool:
        return self.strength == "ambiguous"


@dataclass
class PageFacts:
    """What the classifier looks at, so a test (or the pipeline) can build it from stored evidence."""
    url: str
    title: str = ""
    headings: list[str] = field(default_factory=list)
    text: str = ""
    slug_kinds: tuple[str, ...] = ()      # what the model suggested from the site's addresses: a hint that asks for a content check, never a classification by itself


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"&nbsp;|&#160;", " ", _TAG.sub(" ", s))).strip()


def title_and_headings(html: str) -> tuple[str, list[str]]:
    """The <title> and the h1 to h3 headings of stored page HTML."""
    m = _TITLE.search(html or "")
    return (_clean(m.group(1)) if m else ""), [h for h in (_clean(x[1] or x[2]) for x in _H.findall(html or "")) if h][:120]


def heading_lines(text: str) -> list[str]:
    """Heading-looking lines of plain text (short, no sentence end, followed by prose), for a page stored without its HTML."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    out = []
    for i, ln in enumerate(lines):
        if 3 <= len(ln) <= 90 and not ln.endswith((".", ",", ";")) and re.search(r"[A-Za-z]", ln) and any(len(x) > 120 for x in lines[i + 1:i + 3]):
            out.append(ln)
    return out[:120]


def _segments(url: str) -> list[str]:
    path = unquote(urlparse(url).path or "/").lower()
    return [s for s in path.split("/") if s]


def _slug(segs: list[str]) -> str:
    return re.sub(r"\.(html?|php|aspx?|pdf|docx?)$", "", segs[-1]) if segs else ""


def _title_name(title: str, headings: list[str]) -> str | None:
    """The category a title (any separator-delimited part) or the first heading names exactly: "Privacy Policy | Acme" names privacy_policy; "Is your privacy policy DPDP ready?" names nothing."""
    cands = [p for p in _SEP.split(title or "") if p] + headings[:1]
    for c in cands:
        c = c.strip().strip("\"'")
        for cat, rx in _TITLE_NAMES:
            if rx.match(c):
                return cat
    return None


def heading_groups(headings: list[str]) -> set[str]:
    return {g for g, rx in _HEADING_GROUPS.items() if any(rx.search(h) for h in headings)}


def article_cues(text: str) -> int:
    return sum(1 for rx in _ARTICLE_CUES if rx.search(text or ""))


def classify_page(f: PageFacts) -> PageClass:
    segs = _segments(f.url)
    parents, slug = set(segs[:-1]), _slug(segs)
    path = "/" + "/".join(segs)
    # Headings come from the stored HTML (h1 to h6 and role=heading). A policy that styles its headings with bold text has none there, so heading-looking lines of the text are added.
    headings = list(f.headings)
    if len(headings) < 6:
        headings += [h for h in heading_lines(f.text) if h not in headings]

    editorial = parents & ARTICLE_SEGMENTS
    if editorial or _DATED_PATH.search(path):
        where = f"an editorial segment ({', '.join(sorted(editorial))})" if editorial else "a dated path"
        # A path under /resources, /learn or /news is usually an article, but a real policy can be filed there (/resources/privacy-policy). The address alone is not allowed to decide:
        # when the slug or the title names a policy, the content decides, and a page that cannot be settled goes to the model as ambiguous, never straight to "article".
        slug_cat = next((cat for cat, rx in _SLUGS if slug and rx.fullmatch(slug) and cat in (PRIVACY, COOKIE, TERMS, GRIEVANCE)), None)
        named_here = _title_name(f.title, headings)
        policy_cat = slug_cat or (named_here if named_here in (PRIVACY, COOKIE, TERMS, GRIEVANCE) else None)
        if policy_cat is None:
            return PageClass(ARTICLE, "high", (f"the path has {where} and neither the address nor the title names a policy",))
        g, c, n = heading_groups(headings), article_cues(f.text), len(f.text or "")
        if (len(g) >= 3 or n > 2500) and c <= 1:
            return PageClass(policy_cat, "medium", (f"the path has {where}, but the page names a {policy_cat.replace('_', ' ')} and its content has policy sections ({', '.join(sorted(g)) or 'long text'})",))
        return PageClass(OTHER, "ambiguous", (f"the path has {where} and the page names a {policy_cat.replace('_', ' ')}, but its content does not settle it ({n} characters, {c} article cues)",),
                         (policy_cat, ARTICLE, OTHER))
    if slug in ARTICLE_SEGMENTS and len(segs) == 1:           # the index of the blog itself
        return PageClass(ARTICLE, "high", (f"/{slug} is an editorial index",))

    for cat, rx in _SLUGS:
        if slug and rx.fullmatch(slug):
            return PageClass(cat, "high", (f"the page address ends in a {cat.replace('_', ' ')} slug ({slug})",))

    cues = article_cues(f.text)
    groups = heading_groups(headings)
    named = _title_name(f.title, headings)
    length = len(f.text or "")

    if named in (PRIVACY, COOKIE, TERMS, GRIEVANCE):
        if named == PRIVACY and (len(groups) >= 3 or length > 2500) and cues <= 1:
            return PageClass(PRIVACY, "high", (f"the title names a privacy policy and the page has policy sections ({', '.join(sorted(groups))})",))
        if named != PRIVACY and (length > 800 or len(groups) >= 2) and cues <= 1:
            return PageClass(named, "high", (f"the title names a {named.replace('_', ' ')} and the page is long enough to be one",))
        return PageClass(OTHER, "ambiguous", (f"the title names a {named.replace('_', ' ')} but the page is short or reads like an article ({length} characters, {cues} article cues)",),
                         (named, ARTICLE, OTHER))
    if named in (CONTACT, ABOUT) and length < 6000:
        return PageClass(named, "medium", (f"the title names a {named} page",))
    if len(groups) >= 5 and length > 3000 and cues <= 1:
        return PageClass(PRIVACY, "medium", (f"no policy title, but {len(groups)} policy sections ({', '.join(sorted(groups))})",))
    if len(groups) >= 4 and (cues >= 2 or length > 1500):
        return PageClass(OTHER, "ambiguous", (f"{len(groups)} policy-like headings but no policy title ({cues} article cues)",), (PRIVACY, ARTICLE, OTHER))
    if cues >= 3 or (cues >= 2 and length > 2500 and len(groups) < 4):
        return PageClass(ARTICLE, "medium", (f"{cues} article cues (byline, date, read time, related posts) and no policy structure",))
    if parents & PRODUCT_SEGMENTS or slug in PRODUCT_SEGMENTS:
        return PageClass(PRODUCT, "medium", ("a product or pricing path",))
    hinted = [k for k in f.slug_kinds if k in (PRIVACY, COOKIE, TERMS, GRIEVANCE)]
    if hinted:
        # the address read like a policy to the model but nothing in the page settled it: the model reads the page itself and decides (a hint never classifies on its own)
        return PageClass(OTHER, "ambiguous", (f"the address read like a {hinted[0].replace('_', ' ')} but no rule settled the page ({length} characters, {len(groups)} policy sections)",), (hinted[0], ARTICLE, OTHER))
    return PageClass(OTHER, "medium", ("no rule matched",))


AMBIGUOUS_SYSTEM = (
    "You classify one web page of a company website. Decide only from the page title and opening text given. A privacy policy is the company's own legal notice about how it handles personal data; "
    "an article, blog post or guide ABOUT privacy law is not one. Answer with the category and your confidence."
)


def ambiguous_prompt(f: PageFacts, candidates: tuple[str, ...]) -> str:
    return f"URL: {f.url}\nTitle: {f.title}\nHeadings: {'; '.join(f.headings[:25])}\nCandidate categories: {', '.join(candidates)}\n\nOpening text:\n{(f.text or '')[:1500]}"
