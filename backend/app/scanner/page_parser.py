"""Static parsing of one page's fully-rendered HTML (post network-idle, so JS-injected
elements like GTM-loaded scripts or consent banners are present in the DOM). BeautifulSoup
only — no JS execution happens here, that's the crawler's job via Playwright.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup


@dataclass
class ParsedScript:
    src: str | None
    inline_snippet: str | None  # first 200 chars, for CMP global-var heuristics only
    # False for a tag the browser will not execute: a CMP that blocks a tag until consent rewrites it
    # to type="text/plain" (and restores it after consent). The tag is still in the DOM, so counting it
    # as a tracker that fired reported a blocked script as pre-consent tracking.
    executes: bool = True


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
    #: The link's class and id, lower-cased. Not shown to a reader, but "privacy-notice-link" names what the link is when its text is an icon (policy_detector reads it, narrowly).
    attrs: str = ""


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
    # What the page declares about its language: <html lang> and the hreflang of its rel=alternate links (read-only observations; see page_signals).
    html_lang: str | None = None
    hreflang: list[str] = field(default_factory=list)


# MIME types a browser runs as script. Anything else (text/plain, text/template, ...) is inert.
_EXECUTABLE_SCRIPT_TYPES = frozenset({
    "", "module", "text/javascript", "application/javascript", "application/x-javascript",
    "text/ecmascript", "application/ecmascript", "text/jscript", "text/livescript", "text/x-javascript",
})


def script_executes(type_attr: str | None) -> bool:
    """Whether a <script type=...> is run by the browser."""
    return (type_attr or "").split(";")[0].strip().lower() in _EXECUTABLE_SCRIPT_TYPES


def parse_page(html: str, page_url: str) -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")

    title = soup.title.get_text(strip=True) if soup.title else None

    scripts = [
        ParsedScript(
            src=urljoin(page_url, tag["src"]) if tag.get("src") else None,
            inline_snippet=(tag.get_text() or "")[:200] or None,
            executes=script_executes(tag.get("type")),
        )
        for tag in soup.find_all("script")
    ]

    index_by_href: dict[str, int] = {}
    links: list[ParsedLink] = []
    for a in soup.find_all("a", href=True):
        if a["href"].startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        href = urljoin(page_url, a["href"])
        # What the link says: its visible text, else its accessible name. A policy link that is an icon or an image (konicaminolta.com's OneTrust "privacy-notice-link": no text, an
        # aria-label "More information about your privacy rights") has no text at all, and was missed.
        text = a.get_text(strip=True) or str(a.get("aria-label") or a.get("title") or "").strip()
        attrs = " ".join([*(a.get("class") or []), str(a.get("id") or "")]).lower().strip()
        if href in index_by_href:
            # The same address linked twice: keep what either occurrence says (the first used to win, even when it was an empty icon link and the second said "Privacy Policy").
            first = links[index_by_href[href]]
            if not first.text and text:
                first.text = text
            if attrs and attrs not in first.attrs:
                first.attrs = f"{first.attrs} {attrs}".strip()
            continue
        index_by_href[href] = len(links)
        links.append(ParsedLink(href=href, text=text, attrs=attrs))

    forms = []
    for i, form_tag in enumerate(soup.find_all("form")):
        fields = [
            ParsedFormField(
                # `placeholder`/`aria-label` are a real, rendered fallback -- plenty of
                # JS-built forms (React/Vue state-bound inputs) carry no `name`/`id` at
                # all, which previously fell straight through to an opaque "field-0"/
                # "field-1" placeholder with zero signal for form_detector's purpose
                # guess. The visible/accessible label text is evidence of the field's
                # purpose exactly like a `name` attribute is, not a guess.
                name=(
                    inp.get("name") or inp.get("id") or inp.get("placeholder")
                    or inp.get("aria-label") or f"field-{j}"
                ),
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

    html_tag = soup.find("html")
    html_lang = (html_tag.get("lang") or "").strip() or None if html_tag else None
    hreflang = [
        str(t.get("hreflang")).strip() for t in soup.find_all("link", attrs={"rel": lambda r: r and "alternate" in ([r] if isinstance(r, str) else r), "hreflang": True})
        if str(t.get("hreflang")).strip()
    ]

    return ParsedPage(
        title=title, scripts=scripts, links=links, forms=forms,
        visible_text=visible_text, control_text=extract_control_text(soup), html_lang=html_lang, hreflang=hreflang,
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
    re.IGNORECASE,
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
