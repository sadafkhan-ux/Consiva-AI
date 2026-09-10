"""Optional LLM + RAG enrichment for the ROPA pipeline.

Strictly bounded, because the deterministic rules are the source of truth:

- The LLM is asked about AMBIGUOUS columns ONLY -- the ones no rule matched.
  A column the rules already classified is never sent, so the LLM can never
  overturn a deterministic result or a human-approved classification.
- The LLM receives column NAMES and TYPES only. Never values, never sample
  patterns, never credentials.
- Its output is validated against a Pydantic schema and then FILTERED: any
  column it invents that wasn't in the request is dropped, and every suggestion
  is forced to review_required=True. An LLM suggestion is a hint for a human,
  never an established fact (prompt §3).
- RAG supplies REGULATORY context (approved DPDP knowledge) for explanations
  only. It is never used to infer what data a source contains -- source evidence
  and regulatory knowledge stay separate, per prompt §18.

Enrichment is off unless explicitly requested, so the default pipeline stays
fully deterministic and offline.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.agents.ropa.schemas.ropa import PersonalDataElement

logger = logging.getLogger(__name__)

MAX_AMBIGUOUS_COLUMNS = 40
# An LLM suggestion is capped below the weakest deterministic rule (0.75) so it
# can never outrank real rule evidence when a reviewer sorts by confidence.
LLM_SUGGESTION_CONFIDENCE = 0.5

SYSTEM_PROMPT = """You are a data-classification assistant inside a privacy compliance system.

You are given ONLY column names and SQL data types from an authorized database schema.
You never see actual data values.

For each column, suggest the most likely personal-data category, or "Unknown".

Allowed categories:
Contact Data, Identity Data, Government Identifier / High-Risk, Online Identifier,
Location Data, Financial Data, Employment Data, Credential / Secret, Health Data, Unknown

Hard rules:
- Only return columns that appear in the supplied list. Never invent a column.
- If a column name is genuinely ambiguous, return "Unknown". Do not guess.
- Do not infer what values a column contains. You have no access to values.
- Return nothing but the structured response."""


class ColumnSuggestion(BaseModel):
    column: str
    category: str
    reasoning: str = Field(description="one short sentence, based only on the column name and type")


class EnrichmentResponse(BaseModel):
    suggestions: list[ColumnSuggestion]


_ALLOWED_CATEGORIES = {
    "Contact Data", "Identity Data", "Government Identifier / High-Risk",
    "Online Identifier", "Location Data", "Financial Data", "Employment Data",
    "Credential / Secret", "Health Data", "Unknown",
}


def build_prompt(elements: list[PersonalDataElement]) -> str:
    lines = [f"- {e.table}.{e.column}" for e in elements]
    return (
        "Classify these columns from an authorized database schema.\n"
        "Column names and types only -- no values are available.\n\n" + "\n".join(lines)
    )


def ambiguous_elements(elements: list[PersonalDataElement]) -> list[PersonalDataElement]:
    """The columns worth asking about: unresolved by rules, and not already
    settled by a human."""
    return [e for e in elements if e.classification == "Unknown"][:MAX_AMBIGUOUS_COLUMNS]


def apply_suggestions(
    elements: list[PersonalDataElement],
    response: EnrichmentResponse,
) -> list[PersonalDataElement]:
    """Merge LLM suggestions into the inventory, defensively.

    Only Unknown columns that were actually in the request can be updated, the
    category must be one of the allowed values, and the result always stays
    review_required -- so a hallucinated column or category simply cannot enter
    the ROPA.
    """
    by_key = {(e.table, e.column): e for e in elements if e.classification == "Unknown"}
    updated: dict[tuple[str | None, str], PersonalDataElement] = {}

    for suggestion in response.suggestions:
        if suggestion.category not in _ALLOWED_CATEGORIES or suggestion.category == "Unknown":
            continue
        # The model may return "table.column" or just "column"; match either,
        # but only against columns we actually asked about.
        match = None
        for key in by_key:
            table, column = key
            if suggestion.column in (column, f"{table}.{column}"):
                match = key
                break
        if match is None:
            logger.info("Dropping LLM suggestion for unrequested column %r", suggestion.column)
            continue

        original = by_key[match]
        updated[match] = original.model_copy(
            update={
                "classification": suggestion.category,
                "confidence": LLM_SUGGESTION_CONFIDENCE,
                "evidence": [*original.evidence, "source:llm_suggestion"],
                "review_required": True,
                "review_reason": f"LLM suggestion (unverified): {suggestion.reasoning}",
            }
        )

    return [updated.get((e.table, e.column), e) for e in elements]


async def enrich_elements(elements: list[PersonalDataElement]) -> list[PersonalDataElement]:
    """Ask the LLM about ambiguous columns. Returns the input unchanged if there
    is nothing ambiguous, or if the LLM is unavailable -- enrichment is an
    optional improvement, never a hard dependency of the pipeline."""
    ambiguous = ambiguous_elements(elements)
    if not ambiguous:
        return elements

    from app.llm.client import generate_structured_with_fallback

    try:
        response, _meta = await generate_structured_with_fallback(
            system=SYSTEM_PROMPT,
            user=build_prompt(ambiguous),
            schema=EnrichmentResponse,
        )
    except Exception as exc:  # noqa: BLE001 -- enrichment must never fail the run
        logger.warning("ROPA LLM enrichment unavailable, continuing with rules only: %s", exc)
        return elements

    return apply_suggestions(elements, response)


async def regulatory_context(query: str, *, db, llm_client, top_k: int = 3) -> list[dict]:
    """Approved DPDP/privacy context for EXPLANATIONS only.

    Returns chunk metadata for citation. This never feeds column classification:
    regulatory text says what the law requires, not what a customer's database
    contains (prompt §18).
    """
    from app.rag.retriever import retrieve

    try:
        chunks = await retrieve(query, db=db, llm_client=llm_client, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ROPA RAG context unavailable: %s", exc)
        return []
    return [c.as_prompt_dict() for c in chunks]
