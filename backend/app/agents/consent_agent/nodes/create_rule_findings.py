"""Findings from the deterministic rules, when the model never produced any.

WHY THIS EXISTS
---------------
A real validation run against hubspot.com made the gap concrete. The crawl worked:
25 pages, a confirmed Accept and a confirmed Reject click, 840 trackers and 124
cookies persisted. The rules engine worked: three rules matched in 1ms, including
the post-reject tracking that is this product's entire reason to exist -- 105
trackers and 37 cookies still firing after the visitor pressed Reject. RAG worked:
real DPDP Act and IT Act citations retrieved in 370ms.

Then `llm_analysis` ran for nine minutes, timed out, and the customer got ZERO
findings. Everything needed to tell them their site tracks people after they say no
was sitting in the database, and the report said nothing at all -- because
`validate_output` routes a failed analysis straight to the audit log, past the node
that creates findings.

That is a single point of failure in front of the only output anybody cares about.
The model was never the thing that detected the violation; the rules did. The model
writes the narrative. Losing the narrative should cost you the narrative, not the
finding.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not invent the prose the model would have written, and it does not pretend
the analysis succeeded. A rule-derived finding carries the rule's own summary, says
in its own plain-language text that the narrative step failed, and ALWAYS requires
human review regardless of what the rule thought -- a finding nobody has explained is
exactly the kind that needs a person to read it before it reaches a customer.

It also never puts internal diagnostics in front of a customer. A real scan's
finding_text once read "LLM output failed grounding after 2 retries: ungrounded
dpdp_reference ids ['24977b5b-...', ...]; unsupported vendor-timing claims {...}" --
the raw exception, UUIDs included, presented as the explanation of a DPDP finding.
That detail is real and worth keeping, so it still goes into `internal_notes` (see
migrations/0036) and the stage record below -- it just never reaches finding_text.
"""

import uuid

from app.agents.consent_agent.state import AgentState
from app.db.repositories import finding_repository
from app.db.session import async_session_factory
from app.llm.schemas import ConsentFindingLLM, DpdpReference
from app.observability.stage_tracker import track_stage

# Rule confidence maps onto the finding's priority, not onto a claim about the
# finding being true. A high-confidence rule match is a high-priority thing to read.
_PRIORITY_BY_RISK = {"high": "high", "medium": "medium", "low": "low"}

_NARRATIVE_MISSING = (
    "A plain-language write-up of this finding could not be produced this run. What "
    "you see above is a measured fact read directly from this scan's own evidence, "
    "not an inference -- but it has not been written up in plain language, cited "
    "against the regulatory excerpts below, or checked by a person yet. Read the "
    "evidence and the citations before acting on it."
)


def _as_finding(rule: dict) -> ConsentFindingLLM:
    """One rule match, as a finding a person can read.

    `requires_human_review` is forced True. The rule may have been confident enough
    to skip review when a narrative was going to accompany it; without one, nothing
    should reach a customer unread.
    """
    # Coerced, not trusted. `risk_level` on the finding schema is a Literal, so a
    # rule carrying anything else raises -- and this node runs precisely when
    # something has already gone wrong. A fallback that crashes on unexpected input
    # is not a fallback. Unknown severity becomes medium: it still reaches a person,
    # without claiming to be the most urgent thing on the list.
    risk = rule.get("risk_level")
    if risk not in _PRIORITY_BY_RISK:
        risk = "medium"
    summary = (rule.get("summary") or "").strip() or "A compliance rule matched this scan."
    return ConsentFindingLLM(
        category=rule.get("category", "other"),
        risk_level=risk,
        priority=_PRIORITY_BY_RISK.get(risk, "medium"),
        finding=f"{summary}\n\n{_NARRATIVE_MISSING}",
        evidence=list(rule.get("evidence_ids", [])),
        # Empty on purpose -- this is the field the LLM fills in, and it cited
        # nothing. The real, backend-resolved citations this finding is persisted
        # with (see _rule_citations / create_rule_findings below) travel through the
        # SEPARATE `dpdp_reference` argument to finding_repository.create_finding,
        # exactly like create_findings.py does for a model-cited finding.
        dpdp_reference=[],
        requires_human_review=True,
        recommendation=(
            "Re-run the analysis to get the written explanation for this finding. "
            "The underlying evidence is already collected and does not need "
            "re-scanning."
        ),
    )


def _rule_citations(rag_chunks: list[dict]) -> list[dict]:
    """Every chunk retrieved for this scan's rule findings, in the same resolved
    {chunk_id, section, source_doc, version} shape create_findings.py builds for a
    model-cited finding (see its own _resolve_citations).

    Attaching these is not the model citing something it didn't verify -- it is the
    opposite of that failure mode. retrieve_rag.py queries the knowledge base using
    each rule finding's own summary (plus, where relevant, an explicit DPDP-provision
    hint -- see rules/consent_rules.py:RULE_DPDP_TOPIC_HINTS) as the query, which is a
    deterministic step that already ran and already matched the right violation type,
    before the narrative step that would have cited them ever got the chance to fail.
    Leaving a legal-compliance finding with zero citations just because the write-up
    step failed is not more careful; it withholds evidence that was already found."""
    return [
        DpdpReference(
            chunk_id=c["chunk_id"], section=c.get("section"),
            source_doc=c["document_title"], version=c.get("document_version"),
        ).model_dump()
        for c in rag_chunks
    ]


async def create_rule_findings(state: AgentState) -> dict:
    """Persist the rule matches as findings, because the model produced none.

    Reached only from `validate_output`'s `failed` route. On the success path
    `create_findings` runs instead and this never executes -- the two never both
    write findings for one run.
    """
    reason = (state.error or "the analysis step did not complete").strip()
    created_ids: list[str] = []
    citations = _rule_citations(state.rag_chunks or [])

    async with track_stage(
        uuid.UUID(state.scan_id),
        "rule_findings_generated",
        agent_run_id=uuid.UUID(state.agent_run_id),
    ) as meta:
        rules = list(state.rule_findings or [])
        async with async_session_factory() as db:
            for rule in rules:
                row = await finding_repository.create_finding(
                    db,
                    scan_id=uuid.UUID(state.scan_id),
                    agent_run_id=uuid.UUID(state.agent_run_id),
                    finding=_as_finding(rule),
                    dpdp_reference=citations,
                    internal_notes=reason,
                )
                created_ids.append(str(row.id))
            await db.commit()

        meta["findings_created"] = len(created_ids)
        meta["source"] = "rules"
        meta["reason"] = reason
        meta["rule_ids"] = [r.get("rule_id") for r in rules]
        # Stated in the record, not only in this docstring: these findings are not
        # the product's normal output and a reader of the audit trail should know.
        meta["narrative_missing"] = True
        meta["requires_human_review"] = True
        meta["citations_attached"] = len(citations)

    return {"created_finding_ids": created_ids}
