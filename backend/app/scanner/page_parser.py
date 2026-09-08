"""Static parsing of one page's fully-rendered HTML (post network-idle, so JS-injected
elements like GTM-loaded scripts or consent banners are present in the DOM). BeautifulSoup
only — no JS execution happens here, that's the crawler's job via Playwright.
"""

from dataclasses import dataclass, field
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

    return ParsedPage(title=title, scripts=scripts, links=links, forms=forms, visible_text=visible_text)
