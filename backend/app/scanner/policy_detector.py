"""Privacy/cookie/terms policy page detection via link text + URL keyword heuristics,
run against links aggregated across the whole crawl (a policy is commonly only linked
from the footer, not necessarily itself crawled)."""

from app.scanner._domain import registered_domain
from app.scanner.page_parser import ParsedLink
from app.scanner.schemas import PolicyRecord

_KEYWORDS = {
    "privacy_policy": ("privacy",),
    "cookie_policy": ("cookie",),
    "terms": ("terms of service", "terms & conditions", "terms and conditions", "/terms"),
}


def _classify_link(link: ParsedLink) -> str | None:
    haystack = f"{link.href.lower()} {link.text.lower()}"
    for policy_type, keywords in _KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return policy_type
    return None


def detect_policies(
    all_links: list[ParsedLink], crawled_page_text: dict[str, str], site_domain: str
) -> list[PolicyRecord]:
    """`site_domain` restricts acceptance to links whose registrable domain matches the
    scanned site's own -- a live scan of swaransoft.com accepted https://copilots.in/
    as its privacy policy and https://consiva.ai/ as its cookie policy, purely because
    those pages' URL/link text happened to contain "privacy"/"cookie"; neither domain
    has anything to do with the scanned site. Rejected outright rather than kept and
    flagged: a policy link pointing at an unrelated domain is not evidence of anything
    about THIS site's compliance, so keeping it under a different status still risks
    it being read as real. `registered_domain` is the same eTLD+1 helper crawler.py
    already uses for its own first-party/third-party tracker split, reused here rather
    than a second domain-comparison written from scratch."""
    site_registered = registered_domain(site_domain)
    seen_urls: set[str] = set()
    records: list[PolicyRecord] = []
    counter = 0
    for link in all_links:
        if link.href in seen_urls:
            continue
        policy_type = _classify_link(link)
        if policy_type is None:
            continue
        if registered_domain(link.href) != site_registered:
            continue
        seen_urls.add(link.href)
        text = crawled_page_text.get(link.href)
        records.append(PolicyRecord(
            local_id=f"policy-{counter}",
            url=link.href,
            policy_type=policy_type,  # type: ignore[arg-type]
            extracted_text_ref=text[:2000] if text else None,
        ))
        counter += 1
    return records
