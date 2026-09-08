"""Structured LLM output contracts (docs/architecture §9). The LLM's response is
validated against these on every call — malformed or ungrounded output is a hard
failure handled by agents/consent_agent/nodes/validate_output.py, not silently
accepted."""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["analytics", "marketing", "functional", "other"]
RiskLevel = Literal["low", "medium", "high"]
Priority = Literal["low", "medium", "high"]


class ConsentFindingLLM(BaseModel):
    """What the LLM itself must return."""
    # `dpdp_reference` stays a list of chunk_ids -- not the richer {section, source_doc,
    # version} object the API/DB ultimately store -- because section/source_doc/version
    # are looked up from the retrieved chunk's own metadata by create_findings.py, never
    # transcribed by the model. That keeps the one place those fields could be wrong (an
    # LLM mis-copying a version string) out of the loop entirely.
    #
    # This is a plain comment, not a docstring: model_json_schema() embeds every class's
    # docstring into its "description" field, which gets sent to the LLM on every single
    # call (and every retry) — this implementation rationale has no instructional value
    # to the model, it was just adding ~450 unused chars to every prompt.

    finding: str
    category: Category
    risk_level: RiskLevel
    priority: Priority
    evidence: list[str] = Field(description="local_id references into the supplied scan evidence")
    dpdp_reference: list[str] = Field(description="knowledge_chunk ids from the supplied RAG context only")
    recommendation: str
    requires_human_review: bool


class ConsentAnalysisResponse(BaseModel):
    findings: list[ConsentFindingLLM]


class DpdpReference(BaseModel):
    """The structured citation shape actually stored on consent_findings and returned
    by the API (master prompt §8) — built by create_findings.py from a chunk_id plus
    the retrieved chunk's own metadata, not supplied directly by the LLM."""

    chunk_id: str
    section: str | None = None
    source_doc: str
    version: str | None = None
