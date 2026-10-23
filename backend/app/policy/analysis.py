"""Privacy-policy content analysis: what the policy actually says, with the words to prove it.

The scan shows what a site DOES (cookies, trackers, forms, consent behaviour); this reads what it SAYS it does. For each requirement in
app/policy/requirements.py the model is asked to find the passage, and to copy it exactly. A passage counts only if it is found,
character for character after whitespace and punctuation are normalised, in the policy text that was read: the model's paraphrase,
or a quote it half-remembers, is rejected and the requirement is reported as "not found in the policy text". The legal references
come from the catalogue (checked against the Act and Rules), never from the model.

Three checks are done in code, not by the model, because they have a definite answer: a grievance contact has to contain an email,
phone number or form link; the grievance response period has to be a number of days (Rule 14(3): at most ninety); a rights request
needs a means. All of this is informational and never changes the DPDP Compliance Score.

The step is fail-soft: if no policy text is available or the model cannot be reached, the result says "unavailable" and why, and the
scan, the score and the findings are untouched.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel
from sqlalchemy import select

from app.config import get_settings
from app.db.models import ConsentScan, Policy
from app.db.repositories import scan_repository
from app.db.session import async_session_factory
from app.policy.requirements import BY_ID, CHILD_SECTORS, REQUIREMENTS, PolicyRequirement
from app.scanner.url_safety import assert_safe_url

logger = logging.getLogger(__name__)

ANALYSED, UNAVAILABLE = "analysed", "unavailable"
VERIFIED, PARTIAL, NOT_FOUND, NOT_APPLICABLE = "verified", "partial", "not_found", "not_applicable"

#: Per document, so one very long policy cannot crowd out the others or the prompt budget.
MAX_DOC_CHARS = 30_000
MIN_QUOTE_CHARS = 25
MAX_QUOTE_CHARS = 450
FETCH_TIMEOUT = 15.0
FETCH_MAX_BYTES = 2_000_000

LIMITATIONS = [
    "Only the text of the privacy and cookie policy pages that the scan reached or could fetch was read. Policies in PDF files, behind a login, in images, or loaded after a click are not read.",
    "A requirement that is not found in the policy text is reported as not found, not as a violation: the site may state it elsewhere, and a policy is only one place where duties are met.",
    "Passages are quoted exactly or not at all. A requirement the model described but could not quote is reported as not found.",
    "Legal references are checked against the DPDP Act, 2023 and the DPDP Rules, 2025. Requirements marked as good practice have a duty behind them in the Act but are not stated to be required in the policy itself.",
    "This analysis is informational. It never changes the DPDP Compliance Score and is not legal advice.",
]


# ---------------------------------------------------------------------------------------------------------------------------
# What the model returns
# ---------------------------------------------------------------------------------------------------------------------------

class PolicyReqResult(BaseModel):
    id: str
    found: bool
    quote: str
    document: int


class PolicyExtraction(BaseModel):
    results: list[PolicyReqResult]


SYSTEM_PROMPT = (
    "You read a website's privacy policy documents and report what they state, for a data-protection review. "
    "For EACH requirement you are given, decide whether the documents state it. If they do, copy the single most relevant passage "
    "EXACTLY as written, character for character, no longer than 400 characters, and give the number of the document it came from. "
    "Never paraphrase, summarise, translate or join separate passages; do not add words. If the documents do not clearly state the requirement, "
    "return found=false, an empty quote and document 0. A heading alone is not a statement. Do not infer what a policy probably means. "
    "Return one result for every requirement id given, in the same order."
)


# ---------------------------------------------------------------------------------------------------------------------------
# Quote verification
# ---------------------------------------------------------------------------------------------------------------------------

_PUNCT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-", " ": " ", "•": " "})


def normalise(text: str) -> str:
    """Whitespace, case, quote and dash styles are not differences of substance; nothing else is normalised away."""
    t = unicodedata.normalize("NFKC", text or "").translate(_PUNCT).casefold()
    return re.sub(r"\s+", " ", t).strip()


def verify_quote(quote: str, doc_text: str) -> bool:
    q = normalise(quote)
    return MIN_QUOTE_CHARS <= len(q) <= MAX_QUOTE_CHARS and q in normalise(doc_text)


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+?\d[\d\s().-]{8,}\d)")
_FORM = re.compile(r"https?://\S+|\b(?:contact|grievance|support|request)\s+(?:form|portal|page)\b|\bform\b|\bportal\b", re.I)
_WINDOW = 400


def _window(quote: str, doc_text: str) -> str:
    """The quote and the text around it, where a contact detail would sit."""
    nq, nd = normalise(quote), normalise(doc_text)
    i = nd.find(nq)
    return nd[max(0, i - _WINDOW): i + len(nq) + _WINDOW] if i >= 0 else nq


_URL = re.compile(r"https?://[^\s)\]>\"']+", re.I)
_CONTACT_REQS = ("grievance_contact", "dpo_contact", "rights_means")


def _raw_window(quote: str, doc_text: str) -> str:
    """The quote and the text around it as the policy wrote it (case and punctuation kept), where contact details are read from."""
    words = [re.escape(w) for w in quote.split()]
    m = re.search(r"\s+".join(words), doc_text, re.I) if words else None
    if not m:
        return _window(quote, doc_text)
    return doc_text[max(0, m.start() - _WINDOW): m.end() + _WINDOW]


def extract_contacts(text: str) -> dict[str, list[str]]:
    """The email addresses, phone numbers and links in `text`, each kept once and at most five of each, exactly as written."""
    def uniq(items: list[str]) -> list[str]:
        out: list[str] = []
        for i in items:
            i = i.strip().rstrip(".,;:")
            if i and i not in out:
                out.append(i)
        return out[:5]

    phones = [p.strip() for p in _PHONE.findall(text) if len(re.sub(r"\D", "", p)) >= 10]
    return {"emails": uniq(_EMAIL.findall(text)), "phones": uniq(phones), "urls": uniq(_URL.findall(text))}


def has_contact(text: str, *, allow_form: bool = True) -> bool:
    return bool(_EMAIL.search(text) or _PHONE.search(text) or (allow_form and _FORM.search(text)))


_PERIOD = re.compile(r"(\d+)\s*(?:\(\w+\)\s*)?(?:business\s+|working\s+|calendar\s+)?(hour|hours|day|days|week|weeks|month|months)\b")
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "seven": 7, "ten": 10, "fifteen": 15, "thirty": 30, "forty-five": 45, "sixty": 60, "ninety": 90, "forty eight": 48, "forty-eight": 48}
_UNIT_DAYS = {"hour": 1 / 24, "day": 1, "week": 7, "month": 30}


def response_days(text: str) -> float | None:
    """The longest response period stated in `text`, in days, or None if none is stated."""
    t = normalise(text)
    for word, n in _WORD_NUMBERS.items():
        t = re.sub(rf"\b{re.escape(word)}\b(?=\s*(?:\(\d+\)\s*)?(?:business\s+|working\s+|calendar\s+)?(?:hour|day|week|month))", str(n), t)
    found = [int(n) * _UNIT_DAYS[u.rstrip("s")] for n, u in _PERIOD.findall(t)]
    return max(found) if found else None


# ---------------------------------------------------------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------------------------------------------------------

@dataclass
class PolicyDocument:
    url: str
    policy_type: str
    text: str
    source: str            # "crawl" (read from the rendered page during the scan) or "fetch" (fetched afterwards)
    truncated: bool = False


def _html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


async def fetch_policy_text(url: str) -> str | None:
    """The plain-HTTP text of a policy page the crawl did not read. Every hop is checked by the same SSRF guard as the crawler.
    Returns None for anything that is not a readable HTML page; a page that needs JavaScript will yield little or nothing."""
    try:
        current = url
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False, headers={"User-Agent": "ConsivaPolicyReader/1.0"}) as client:
            for _ in range(4):
                await assert_safe_url(current)
                resp = await client.get(current)
                if resp.is_redirect and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                ctype = resp.headers.get("content-type", "")
                if resp.status_code != 200 or "html" not in ctype.lower() or len(resp.content) > FETCH_MAX_BYTES:
                    return None
                text = _html_to_text(resp.text)
                return text if len(text) >= 200 else None
    except Exception as exc:  # fail-soft: an unreadable policy is a limitation, not an error
        logger.info("Could not fetch policy %s: %s", url, exc)
    return None


def _same_page(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    host = lambda p: (p.hostname or "").removeprefix("www.")
    return host(pa) == host(pb) and pa.path.rstrip("/") == pb.path.rstrip("/")


async def collect_documents(policies: list[Policy], fetcher=fetch_policy_text) -> tuple[list[PolicyDocument], list[str]]:
    """Privacy and cookie policies, one per page, with the text the crawl captured or else a fetched copy. Terms pages are not read:
    they are about the contract, not the processing of personal data. Returns the documents and notes on what could not be read."""
    docs: list[PolicyDocument] = []
    notes: list[str] = []
    for p in policies:
        if p.policy_type not in ("privacy_policy", "cookie_policy"):
            continue
        if any(_same_page(p.url, d.url) for d in docs):
            continue
        text, source = (p.extracted_text_ref or "").strip(), "crawl"
        if len(text) < 200:
            fetched = await fetcher(p.url)
            text, source = (fetched or "").strip(), "fetch"
        if len(text) < 200:
            notes.append(f"{p.url} could not be read (no text was captured and it could not be fetched as a plain page).")
            continue
        # Some sites render several legal documents into every legal page, so two policy URLs can yield the very same text. It is read once.
        twin = next((d for d in docs if normalise(d.text) == normalise(text[:MAX_DOC_CHARS])), None)
        if twin is not None:
            notes.append(f"{p.url} has the same text as {twin.url}, so it was read once.")
            continue
        docs.append(PolicyDocument(p.url, p.policy_type, text[:MAX_DOC_CHARS], source, truncated=len(text) > MAX_DOC_CHARS))
    return docs, notes


# ---------------------------------------------------------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------------------------------------------------------

def applicable(req: PolicyRequirement, sector: str | None, child_signal: bool) -> tuple[bool, str]:
    if req.applies == "children" and not (sector in CHILD_SECTORS or child_signal):
        return False, "No sign that this site is aimed at, or collects data from, children (its sector and form fields do not suggest it), so this is not assessed."
    return True, ""


def build_prompt(docs: list[PolicyDocument], reqs: list[PolicyRequirement]) -> str:
    parts = ["## Policy documents (quote only from these)"]
    for i, d in enumerate(docs, 1):
        parts.append(f"### Document {i}: {d.policy_type.replace('_', ' ')} - {d.url}\n{d.text}")
    parts.append("## Requirements (return one result for each, in this order)")
    for r in reqs:
        parts.append(f"- id: {r.id}\n  what to find: {r.look_for}")
    return "\n\n".join(parts)


def judge(req: PolicyRequirement, quote: str, doc: PolicyDocument) -> tuple[str, str]:
    """Status and a plain explanation for a quote that was found verbatim in `doc`."""
    ctx = _window(quote, doc.text)
    if req.id in ("grievance_contact", "dpo_contact"):
        if has_contact(ctx):
            return VERIFIED, "Quoted, with a contact detail beside it."
        return PARTIAL, "The role is mentioned but no email, phone number or form is given next to it."
    if req.id == "rights_means":
        if has_contact(ctx):
            return VERIFIED, "Quoted, with a means of making the request."
        return PARTIAL, "Rights are mentioned but no concrete means of making a request (email, form or portal) is given."
    if req.id == "grievance_timeline":
        days = response_days(quote)
        if days is None:
            return PARTIAL, "Grievances are mentioned but no response period is stated in the passage."
        if days > 90:
            return PARTIAL, f"The stated period is about {days:g} days; the Rules allow a reasonable period not exceeding ninety days."
        return VERIFIED, f"Quoted: a response period of about {days:g} days, within the ninety-day limit."
    return VERIFIED, "Quoted from the policy."


def analyse(
    extraction: PolicyExtraction | None, docs: list[PolicyDocument], sector: str | None, child_signal: bool,
) -> dict:
    """Turns the model's extraction into the report. `extraction` is None when the model could not be used."""
    by_id = {r.id: r for r in (extraction.results if extraction else [])}
    rows: list[dict] = []
    rejected = 0
    for req in REQUIREMENTS:
        ok, why = applicable(req, sector, child_signal)
        row = {"id": req.id, "title": req.title, "group": req.group, "basis": req.basis, "provisions": list(req.provisions),
               "status": NOT_APPLICABLE, "quote": "", "source_url": None, "detail": why, "fix": req.fix,
               "contacts": None, "response_days": None}
        if ok:
            res = by_id.get(req.id)
            row["status"], row["detail"] = NOT_FOUND, "Not found in the policy text."
            if res and res.found and res.quote.strip():
                idx = res.document - 1
                candidates = [docs[idx]] if 0 <= idx < len(docs) else []
                candidates += [d for d in docs if d not in candidates]   # the model may name the wrong document; the quote decides
                doc = next((d for d in candidates if verify_quote(res.quote, d.text)), None)
                if doc is None:
                    rejected += 1
                    row["detail"] = "Not found in the policy text (the passage offered was not an exact quotation and was rejected)."
                else:
                    row["status"], row["detail"] = judge(req, res.quote, doc)
                    row["quote"], row["source_url"] = res.quote.strip(), doc.url
                    if req.id in _CONTACT_REQS:
                        row["contacts"] = extract_contacts(_raw_window(res.quote, doc.text))
                    if req.id == "grievance_timeline":
                        row["response_days"] = response_days(res.quote)
        rows.append(row)
    live = [r for r in rows if r["status"] != NOT_APPLICABLE]
    verified = sum(1 for r in live if r["status"] == VERIFIED)
    partial = sum(1 for r in live if r["status"] == PARTIAL)
    return {
        "requirements": rows,
        "summary": {
            "applicable": len(live), "verified": verified, "partial": partial, "not_found": len(live) - verified - partial,
            "not_applicable": len(rows) - len(live), "quotes_rejected": rejected,
            "completeness_percent": round((verified + 0.5 * partial) / len(live) * 100, 1) if live else None,
        },
    }


def unavailable(reason: str, docs: list[PolicyDocument] | None = None, notes: list[str] | None = None) -> dict:
    return {
        "status": UNAVAILABLE, "reason": reason, "sector": None, "documents": _doc_rows(docs or []), "notes": notes or [],
        "summary": None, "requirements": [], "limitations": list(LIMITATIONS), "model": None,
    }


def _doc_rows(docs: list[PolicyDocument]) -> list[dict]:
    return [{"url": d.url, "policy_type": d.policy_type, "characters": len(d.text), "source": d.source, "truncated": d.truncated} for d in docs]


def _child_signal(evidence: dict) -> bool:
    from app.scoring.sector import _CHILD_FIELD, _matching_fields

    return bool(_matching_fields(evidence, _CHILD_FIELD))


async def run(scan_id: uuid.UUID, *, extractor=None, fetcher=fetch_policy_text) -> dict:
    """Reads the scan's policies and returns the result (never raises). `extractor(system, user) -> (PolicyExtraction, meta)` can be
    injected for tests; by default the existing Claude-first client with NVIDIA/Groq fallback is used."""
    try:
        async with async_session_factory() as db:
            policies = list((await db.execute(select(Policy).where(Policy.scan_id == scan_id))).scalars().all())
            scan = await db.get(ConsentScan, scan_id)
            scan_url = scan.url if scan is not None else None
        if not any(p.policy_type in ("privacy_policy", "cookie_policy") for p in policies):
            return unavailable("No privacy or cookie policy page was found on the scanned pages, so there was nothing to read.")
        docs, notes = await collect_documents(policies, fetcher)
        if not docs:
            return unavailable("A policy page was found but its text could not be read.", notes=notes)

        evidence = await scan_repository.get_scan_evidence_summary_concurrent(scan_id, include_cookie_detail=False)
        from app.scoring.sector import assess_sector

        sector = assess_sector(evidence, scan_url).sector   # the same reading the score card shows
        child = _child_signal(evidence)
        reqs = [r for r in REQUIREMENTS if applicable(r, sector, child)[0]]

        if extractor is None:
            extractor = _default_extractor(str(scan_id), [d.url for d in docs])
        try:
            extraction, meta = await extractor(SYSTEM_PROMPT, build_prompt(docs, reqs))
        except Exception as exc:
            logger.warning("Policy analysis model call failed for %s: %s", scan_id, exc)
            return unavailable(f"The policy was read but the analysis model could not be reached ({type(exc).__name__}).", docs, notes)

        out = analyse(extraction, docs, sector, child)
        return {"status": ANALYSED, "reason": None, "sector": sector, "documents": _doc_rows(docs), "notes": notes,
                **out, "limitations": list(LIMITATIONS), "model": meta}
    except Exception as exc:
        logger.exception("Policy analysis failed for %s", scan_id)
        return unavailable(f"The policy analysis could not run ({type(exc).__name__}).")


def _default_extractor(scan_id: str, page_urls: list[str] | None = None):
    from app.llm.client import generate_structured_with_fallback

    async def extractor(system: str, user: str):
        import time

        settings = get_settings()
        usage: dict = {}
        response, fb = await generate_structured_with_fallback(
            system=system, user=user, schema=PolicyExtraction, settings=settings, usage_sink=usage,
            deadline_epoch=time.time() + 120, agent="consent-policy", scan_id=scan_id, stage="policy_analysis", page_urls=page_urls, max_attempts=2,
        )
        used = usage.get("usage_by_attempt") or []
        meta = {
            "provider": fb.get("fallback_provider") if fb.get("fallback_triggered") else fb.get("primary_provider"),
            "model": fb.get("fallback_model") if fb.get("fallback_triggered") else fb.get("primary_model"),
            "tokens_in": sum((u.get("input_tokens") or u.get("prompt_tokens") or 0) for u in used),
            "tokens_out": sum((u.get("output_tokens") or u.get("completion_tokens") or 0) for u in used),
        }
        return response, meta

    return extractor


__all__ = ["run", "analyse", "verify_quote", "response_days", "collect_documents", "build_prompt", "BY_ID"]
