"""The ONLY LLM touchpoint in the consent-gap analyser.

Three soft descriptive fields -- company_tagline, founder_name, notable_clients. They
are nice-to-have context, never compliance findings, and they can never fail the run:
any error at all returns nulls and the analysis carries on.

Reuses the project's existing provider-agnostic client (self-hosted primary, NVIDIA/
Groq fallback) rather than opening a second path to a model.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_MAX_PAGE_CHARS = 6000

SYSTEM_PROMPT = """You extract three descriptive facts about a company from the text of \
its own website. You are NOT performing compliance analysis.

Absolute rules:
- Use ONLY what the supplied page text states. Never use outside knowledge about the \
company, and never infer, guess or embellish.
- If the page does not clearly state something, return null (or an empty list). A null \
is always better than a plausible guess.
- notable_clients means ORGANISATIONS the company states it has worked for or supplied. \
A PERSON'S NAME IS NEVER A CLIENT. Testimonial authors, reviewers, quoted individuals, \
staff, founders and team members must NEVER be listed as clients, even when their quote \
praises the company. If the page only shows customer reviews by individuals, return an \
empty list.
- founder_name is a person only if the page explicitly identifies them as founder, \
co-founder, owner, proprietor or similar.
- company_tagline is the company's own short positioning line, not a sentence you wrote.

Respond with JSON only."""


class _SoftFields(BaseModel):
    company_tagline: str | None = Field(default=None)
    founder_name: str | None = Field(default=None)
    notable_clients: list[str] = Field(default_factory=list)


_NULL_RESULT: dict = {"company_tagline": None, "founder_name": None, "notable_clients": []}


async def extract_soft_fields(page_text: str, title: str) -> dict:
    """Best-effort. Returns nulls on any failure -- never raises, never blocks the
    compliance result from being returned."""
    if not page_text.strip():
        return dict(_NULL_RESULT)

    try:
        from app.llm.client import generate_structured_with_fallback

        user = (
            f"Page title: {title}\n\n"
            f"Page text:\n{page_text[:_MAX_PAGE_CHARS]}\n\n"
            "Extract company_tagline, founder_name and notable_clients per the rules."
        )
        parsed, _meta = await generate_structured_with_fallback(
            system=SYSTEM_PROMPT, user=user, schema=_SoftFields, max_attempts=2,
        )
        clients = [c.strip() for c in (parsed.notable_clients or []) if c and c.strip()]
        return {
            "company_tagline": (parsed.company_tagline or None),
            "founder_name": (parsed.founder_name or None),
            "notable_clients": clients,
        }
    except Exception as exc:  # noqa: BLE001 -- deliberately total: a model problem must
        # degrade three optional fields, never fail an otherwise complete analysis.
        logger.info("Soft-field extraction unavailable, returning nulls: %s", exc)
        return dict(_NULL_RESULT)
