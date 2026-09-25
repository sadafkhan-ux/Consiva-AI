"""Static parsing of one page's fully-rendered HTML (post network-idle, so JS-injected
elements like GTM-loaded scripts or consent banners are present in the DOM). BeautifulSoup
only — no JS execution happens here, that's the crawler's job via Playwright.
"""

from dataclasses import dataclass, field
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup


@dataclass
class ParsedScript:
    src: str | None
    inline_snippet: str | None  # first 200 chars, for CMP global-var heuristics only


@dataclass
class ParsedFormField:
    name: str
    field_type: str
    required: bool


@dataclass
class ParsedForm:
    selector: str
    action_url: str | None
    method: str | None
    fields: list[ParsedFormField]


@dataclass
class ParsedLink:
    href: str
    text: str


@dataclass
class ParsedPage:
    title: str | None
    scripts: list[ParsedScript] = field(default_factory=list)
    links: list[ParsedLink] = field(default_factory=list)
    forms: list[ParsedForm] = field(default_factory=list)
    visible_text: str = ""  # used by policy_detector's keyword search only
    # Text of CLICKABLE controls and dialog containers only -- what a consent banner
    # is actually made of. Kept separate from visible_text because consent detection
    # must never match ordinary page prose; see control_text() below.
    control_text: str = ""


def parse_page(html: str, page_url: str) -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")

    title = soup.title.get_text(strip=True) if soup.title else None

    scripts = [
        ParsedScript(
            src=urljoin(page_url, tag["src"]) if tag.get("src") else None,
            inline_snippet=(tag.get_text() or "")[:200] or None,
        )
        for tag in soup.find_all("script")
    ]

    seen_hrefs: set[str] = set()
    links: list[ParsedLink] = []
    for a in soup.find_all("a", href=True):
        if a["href"].startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        href = urljoin(page_url, a["href"])
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        links.append(ParsedLink(href=href, text=a.get_text(strip=True)))

    forms = []
    for i, form_tag in enumerate(soup.find_all("form")):
        fields = [
            ParsedFormField(
                name=inp.get("name") or inp.get("id") or f"field-{j}",
                field_type=inp.get("type", "text") if inp.name == "input" else inp.name,
                required=inp.has_attr("required"),
            )
            for j, inp in enumerate(form_tag.find_all(["input", "textarea", "select"]))
            if inp.get("type") not in ("hidden", "submit", "button")
        ]
        forms.append(ParsedForm(
            selector=form_tag.get("id") and f"#{form_tag['id']}" or f"form:nth-of-type({i + 1})",
            action_url=urljoin(page_url, form_tag["action"]) if form_tag.get("action") else None,
            method=form_tag.get("method"),
            fields=fields,
        ))

    visible_text = soup.get_text(separator=" ", strip=True)[:5000]

    return ParsedPage(
        title=title, scripts=scripts, links=links, forms=forms,
        visible_text=visible_text, control_text=extract_control_text(soup),
    )


# Elements a person can actually click, plus the containers a consent dialog uses.
# A consent banner is, definitionally, interactive: it has an Accept control and
# usually a Reject one. Prose is not a consent mechanism.
_CONTROL_SELECTORS = (
    "button", "a", "input", "summary", "label",
    "[role=button]", "[role=dialog]", "[role=alertdialog]", "[aria-modal=true]",
)

# A container whose own id/class names it as consent UI. Its inner text counts even
# when the markup does not use a button element (plain <div onclick> banners are
# common), which keeps the check from being defeated by sloppy markup.
_CONSENT_CONTAINER_HINT = re.compile(
    r"(cookie|consent|gdpr|ccpa|cmp|privacy)[-_]?(banner|bar|notice|dialog|modal|popup|consent)?",
    re.I,
)


def extract_control_text(soup: BeautifulSoup) -> str:
    """The text of clickable controls and consent-dialog containers, lowercased.

    WHY THIS EXISTS. Consent detection used to keyword-match the whole page's rendered
    text. On news.ycombinator.com -- a forum with no cookie banner, no consent UI of
    any kind, and zero matches for cookie/consent/gdpr in its HTML -- that reported a
    consent banner anyway, because an ordinary user comment contained the words "both
    parties agree on this". The pipeline then raised a high-priority DPDP finding
    about a Reject control that does not exist on the site, which is the worst failure
    mode available to compliance software: a confident, specific, fabricated finding
    that sends a privacy team looking for a UI element that was never there.

    Scoping to controls removes that entire class of false positive, because forum
    prose is not a button.
    """
    parts: list[str] = []
    for element in soup.select(", ".join(_CONTROL_SELECTORS)):
        parts.append(element.get_text(separator=" ", strip=True))
        # <input type="button" value="Accept all"> carries its label in an attribute.
        for attr in ("value", "aria-label", "title"):
            if element.get(attr):
                parts.append(str(element[attr]))
    for element in soup.find_all(attrs={"class": _CONSENT_CONTAINER_HINT}):
        parts.append(element.get_text(separator=" ", strip=True))
    for element in soup.find_all(attrs={"id": _CONSENT_CONTAINER_HINT}):
        parts.append(element.get_text(separator=" ", strip=True))
    return " ".join(p for p in parts if p).lower()[:5000]
