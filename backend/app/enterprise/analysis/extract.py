"""Level 1: page-level extraction. The model reads one chunk of one page, document page or form and lists facts, each with the exact words it rests on.

The guard against invented evidence: every fact must carry a quote, and the quote must be a verbatim substring of the chunk (whitespace, typographic quotes and dashes normalised, nothing
else). A fact whose quote is not found is DISCARDED and counted; it never reaches an assessment. The model is asked for facts only, never for a verdict, and never for a section number
(legal references come from the parameter catalogue alone).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from app.enterprise.analysis.constants import MAX_QUOTE_CHARS, MIN_QUOTE_CHARS
from app.enterprise.analysis.llm import Answer, LLMCallable, call_structured

Topic = Literal[
    "data_category", "purpose", "sharing_recipient", "retention", "right_access", "right_correction", "right_erasure", "right_nominate", "withdraw_consent", "exercise_channel",
    "grievance_mechanism", "grievance_period", "board_complaint", "dpo_contact", "children_directed", "children_not_directed", "children_consent", "children_no_tracking",
    "security", "breach", "language_option", "consent_language", "notice_link", "cross_border", "consent_manager", "policy_date", "cookie_declared",
    "legal_ground", "processor_contract", "data_accuracy", "consent_record", "request_identifier", "identity_verification", "log_retention", "sdf_duty",
    "disability_guardian", "inactivity_erasure", "rights_waiver", "exemption_claim"]
TOPICS: tuple[str, ...] = Topic.__args__           # type: ignore[attr-defined]

TOPIC_HELP = {
    "data_category": "a kind of personal data the page says it collects or processes (value: the item, e.g. 'email address', 'IP address', 'Aadhaar number')",
    "purpose": "a purpose the page says personal data is used for (value: the purpose, e.g. 'to process your order')",
    "sharing_recipient": "someone personal data is shared with or sent to (value: the name or the kind, e.g. 'Google Analytics', 'payment processors')",
    "retention": "how long data is kept, or when it is deleted (value: the period or rule)",
    "right_access": "the person may access or obtain a copy of their data",
    "right_correction": "the person may correct or update their data",
    "right_erasure": "the person may have their data erased or deleted",
    "right_nominate": "the person may nominate another person to exercise their rights on their behalf (death or incapacity)",
    "withdraw_consent": "how or that consent can be withdrawn (value: the means)",
    "exercise_channel": "how to exercise rights: an email, form, portal, link or postal address (value: the channel, with the link or address if given)",
    "grievance_mechanism": "how to raise a grievance or complaint with the company (value: the means or the officer)",
    "grievance_period": "the time within which the company answers or resolves a grievance or rights request (value: the period as written, e.g. '30 days')",
    "board_complaint": "the person may complain to the Data Protection Board of India (value: how)",
    "dpo_contact": "contact details of a Data Protection Officer, grievance officer, privacy officer or other responsible person (value: name or role and the channel)",
    "children_directed": "the page says the site or service is for children or minors, or has child users",
    "children_not_directed": "the page says the site is not for children or minors, or forbids them",
    "children_consent": "parental or guardian consent for a child's data is described",
    "children_no_tracking": "the page says it does not track, profile or target advertising at children",
    "security": "a statement of security safeguards (value: the measure, e.g. 'encryption in transit')",
    "breach": "what the company does or says if a personal data breach happens (value: the process)",
    "language_option": "the notice or site is offered in another language, or a language is named (value: the language)",
    "consent_language": "wording that asks the person to consent or agree (value: what they agree to)",
    "notice_link": "a mention of or link to the privacy notice next to a request for consent or a form",
    "cross_border": "a statement about sending data outside India (value: where or on what terms)",
    "consent_manager": "a mention of a Consent Manager registered under the DPDP Rules (value: the name)",
    "policy_date": "the date the policy was issued, updated or took effect (value: the date as written)",
    "cookie_declared": "a cookie or tracker the page names or lists (value: its name, vendor or purpose)",
    "legal_ground": "the basis the company relies on to process personal data: consent, or a legitimate use such as data the person provided voluntarily, a legal obligation, employment (value: the ground and the purpose it covers)",
    "processor_contract": "processors, vendors or service providers act under a contract or agreement with the company, or are bound to protect or delete the data (value: what they are bound to)",
    "data_accuracy": "the company keeps personal data accurate, complete, up to date or consistent (value: how)",
    "consent_record": "the company records, logs or keeps proof of consent given or withdrawn (value: what is recorded)",
    "request_identifier": "the identifier or particulars a person must give to make a rights request, such as a registered email, mobile number, account or customer ID (value: the identifier)",
    "identity_verification": "how the company verifies the identity of a person making a request (value: the method)",
    "log_retention": "how long logs (access, activity, server or audit logs) are kept (value: the period as written)",
    "sdf_duty": "the company says it is a Significant Data Fiduciary, or names an independent data auditor, a data protection impact assessment, a periodic audit, or a DPO based in India (value: what is said)",
    "disability_guardian": "consent of a lawful guardian for a person with disability is described (value: how)",
    "inactivity_erasure": "data is erased after a period of inactivity, or the person is told before their data is erased (value: the period or notice as written)",
    "rights_waiver": "a term asking the person to give up or waive a right, for example not to complain to a regulator or the Data Protection Board, or to waive claims (value: the right waived)",
    "exemption_claim": "the company says an exemption from data protection law applies to it or to some processing (value: the exemption claimed)",
}


class Fact(BaseModel):
    topic: Topic
    value: str
    quote: str
    specific: bool


class PageExtraction(BaseModel):
    facts: list[Fact]


SYSTEM = (
    "You extract facts from one passage of a company's website or privacy documents, to be checked later against the Digital Personal Data Protection Act, 2023 of India.\n"
    "Rules:\n"
    "1. Report only what the passage itself says. Do not infer, do not use outside knowledge, do not judge compliance.\n"
    "2. Every fact needs `quote`: the exact words from the passage that support it, copied character for character (one sentence or phrase; never join separate places with '...'; "
    "never paraphrase). If you cannot quote it, leave the fact out.\n"
    "3. `value` is a short plain restatement. `specific` is true when the value names a concrete item or entity (a named company, an exact data item, a stated number) and false when it is "
    "only a general category ('our partners', 'certain information', 'a reasonable time').\n"
    "4. If the passage contains nothing relevant, return an empty list. Do not invent a legal section number.\n"
    "Topics you may use:\n" + "\n".join(f"- {t}: {h}" for t, h in TOPIC_HELP.items())
)


def build_user(url: str, kind: str, chunk_text: str, *, index: int, count: int) -> str:
    return f"Source: {url} ({kind}); passage {index + 1} of {count}.\n\n<<<PASSAGE\n{chunk_text}\nPASSAGE>>>"


_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"', "–": "-", "—": "-", "−": "-", " ": " "})


def normalise(s: str) -> str:
    """The comparison form of a text: Unicode-normalised, typographic quotes and dashes made plain, all whitespace one space."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "").translate(_QUOTES)).strip()


def quote_in(quote: str, haystack_norm: str) -> bool:
    q = normalise(quote)
    return MIN_QUOTE_CHARS <= len(q) <= MAX_QUOTE_CHARS and "..." not in q and "…" not in quote and q in haystack_norm


@dataclass
class Extraction:
    facts: list[dict]
    raw: int
    discarded: int
    answer: Answer


def verify(parsed: PageExtraction, chunk_text: str) -> tuple[list[dict], int]:
    """(kept facts, how many were discarded because their quote is not in the text)."""
    hay = normalise(chunk_text)
    kept, seen, dropped = [], set(), 0
    for f in parsed.facts:
        if not quote_in(f.quote, hay):
            dropped += 1
            continue
        key = (f.topic, normalise(f.quote))
        if key in seen:
            continue
        seen.add(key)
        kept.append({"topic": f.topic, "value": f.value.strip()[:300], "quote": normalise(f.quote), "specific": bool(f.specific and normalise(f.value) != "")})
    return kept, dropped


def extractor_key(provider_model: str) -> str:
    """Identifies how a passage is read: the extraction prompt and the model. A passage's facts are reused only when this is identical, so changing the prompt or the model re-reads everything."""
    import hashlib
    return hashlib.sha256("|".join((SYSTEM, provider_model, "extract-v2")).encode("utf-8")).hexdigest()[:24]


def text_hash(url: str, kind: str, chunk_text: str) -> str:
    import hashlib
    return hashlib.sha256("|".join((url, kind, chunk_text)).encode("utf-8")).hexdigest()


async def extract_chunk(llm: LLMCallable, *, url: str, kind: str, chunk_text: str, index: int, count: int) -> Extraction:
    """Read one chunk. Raises llm.LLMFailure when the model cannot answer; the caller records the chunk as failed (never as a finding)."""
    answer = await call_structured(llm, SYSTEM, build_user(url, kind, chunk_text, index=index, count=count), PageExtraction)
    kept, dropped = verify(answer.parsed, chunk_text)       # type: ignore[arg-type]
    return Extraction(kept, len(answer.parsed.facts), dropped, answer)   # type: ignore[attr-defined]
