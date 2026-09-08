import re
import time
import uuid

from app.agents.consent_agent.state import AgentState
from app.llm.schemas import ConsentAnalysisResponse
from app.observability.stage_tracker import track_stage

MAX_VALIDATION_ATTEMPTS = 2

# Catches the LLM writing a specific-sounding legal citation number directly into its
# free-text finding/recommendation prose (e.g. "DPDP Rule 17(1)(b)") -- a real, observed
# failure mode distinct from an ungrounded dpdp_reference chunk_id: the structured
# citation mechanism above only checks that a cited chunk_id was actually retrieved, it
# never checks whether a specific number written into the narrative text is actually
# supported by that (or any retrieved) chunk's content. A live audit caught the model
# citing "Rule 17(1)(b)" when none of the 6 retrieved chunks mentioned "17" or "reject"
# at all. Deliberately conservative: matches only Rule/Section/Chapter + a number, since
# that's the exact shape of the observed hallucination.
_NARRATIVE_CITATION_RE = re.compile(r"\b(Rule|Section|Chapter)\s+(\d+[A-Za-z]?)\b", re.IGNORECASE)


def _unverified_narrative_citations(text: str, chunk_pool_text: str) -> list[str]:
    """Returns the "Keyword N" citations found in `text` whose keyword+number pair does
    not appear anywhere in `chunk_pool_text` (the concatenated content of every chunk
    actually retrieved for this scan, not just this one finding's cited subset --
    deliberately lenient to avoid flagging a citation that's real but attached to a
    different finding in the same run)."""
    unverified = []
    for match in _NARRATIVE_CITATION_RE.finditer(text):
        keyword, number = match.group(1), match.group(2)
        phrase = f"{keyword} {number}"
        if not re.search(re.escape(phrase), chunk_pool_text, re.IGNORECASE):
            unverified.append(match.group(0))
    return unverified


async def validate_output(state: AgentState) -> dict:
    """Step 9 — Pydantic schema validity is already guaranteed by llm/client.py's own
    retry loop; what's checked here is domain-specific: every dpdp_reference must be a
    chunk_id that was actually retrieved. An ungrounded citation is treated as a hard
    failure of this attempt, not a warning (docs/architecture §I)."""
    async with track_stage(
        uuid.UUID(state.scan_id), "output_validation", agent_run_id=uuid.UUID(state.agent_run_id)
    ) as meta:
        response = ConsentAnalysisResponse.model_validate(state.llm_output)
        valid_chunk_ids = {c["chunk_id"] for c in state.rag_chunks}
        ungrounded = sorted({
            ref for finding in response.findings for ref in finding.dpdp_reference if ref not in valid_chunk_ids
        })
        meta["findings_checked"] = len(response.findings)
        meta["ungrounded_citations"] = ungrounded

        if not ungrounded:
            # The grounding check above only verifies citations that are PRESENT --
            # a finding with an empty dpdp_reference trivially satisfies it, so an
            # uncited legal-compliance claim could otherwise reach a customer
            # unreviewed. Safety backstop: no citation means it can't be auto-trusted
            # as grounded, so force human review regardless of what the LLM set.
            uncited_forced_review = 0
            for finding in response.findings:
                if not finding.dpdp_reference and not finding.requires_human_review:
                    finding.requires_human_review = True
                    uncited_forced_review += 1
            meta["uncited_forced_review"] = uncited_forced_review

            # Second, distinct safety backstop: a specific rule/section NUMBER written
            # into the narrative finding/recommendation text is a separate claim from
            # the structured dpdp_reference citations above, and nothing else in this
            # pipeline checks it against the retrieved chunk content. Flag (not retry --
            # this check is necessarily best-effort/regex-based and could false-positive
            # on a real citation phrased differently than its source text) and force
            # human review so an unverified narrative citation never reaches a customer
            # silently.
            chunk_pool_text = " ".join(c.get("content", "") for c in state.rag_chunks)
            unverified_by_finding = {}
            for i, finding in enumerate(response.findings):
                unverified = _unverified_narrative_citations(
                    f"{finding.finding} {finding.recommendation}", chunk_pool_text
                )
                if unverified:
                    unverified_by_finding[i] = unverified
                    finding.requires_human_review = True
            meta["unverified_narrative_citations"] = unverified_by_finding

            meta["outcome"] = "valid"
            return {"validation_status": "valid", "error": None, "llm_output": response.model_dump()}

        if state.validation_attempts >= MAX_VALIDATION_ATTEMPTS:
            meta["outcome"] = "failed"
            return {
                "validation_status": "failed",
                "error": (
                    f"LLM cited dpdp_reference ids not present in retrieved context after "
                    f"{state.validation_attempts} retries: {ungrounded}"
                ),
            }

        # Graph-level share of the cross-layer wall-clock budget (see
        # AgentState.llm_deadline_epoch): don't route back to llm_reasoning for another
        # full generate/validate cycle when the analysis's LLM time is already spent --
        # the retry counter alone can't see how long the previous attempts took.
        if state.llm_deadline_epoch is not None and time.time() > state.llm_deadline_epoch:
            meta["outcome"] = "failed"
            return {
                "validation_status": "failed",
                "error": (
                    f"LLM wall-clock deadline exceeded before validation retry "
                    f"{state.validation_attempts + 1}; ungrounded citations: {ungrounded}"
                ),
            }

        meta["outcome"] = "retry"
        return {
            "validation_status": "retry",
            "validation_attempts": state.validation_attempts + 1,
            "error": (
                f"Your dpdp_reference values {ungrounded} are not among the provided chunk_ids "
                f"({sorted(valid_chunk_ids)}). Only cite chunk_ids that were given to you, or omit "
                f"dpdp_reference entries for a finding if nothing supplied is relevant."
            ),
        }
