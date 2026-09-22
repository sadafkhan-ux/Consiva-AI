"""Turning a detected change into something a person can decide on (spec §4 steps 5-6, §15).

This is the only module in Agent 5 that talks to a model, and it is arranged so that
nothing the model says can become a fact on its own.

THE ORDER IS THE GUARANTEE
--------------------------
    1. relevance     -- deterministic rules, three-valued
    2. priority      -- deterministic, derived from (1)
    3. impact        -- deterministic, by reference to the other agents' rows
    4. interpretation -- OPTIONAL, a model, grounded in the approved corpus

Steps 1-3 run without a model and are complete on their own. Step 4 adds prose and
citations. If step 4 fails, times out, or returns something that cannot be grounded,
the finding still reaches a reviewer with everything from steps 1-3 and an open
question saying the interpretation is missing. A failed model never fails the watch.

WHAT THE MODEL MAY AND MAY NOT DO
---------------------------------
MAY: summarise what a passage of the approved corpus says, in plain words, and point
at which retrieved chunk it came from.

MAY NOT: establish that an obligation applies, invent a citation, or reach CONFIRMED.
The first is a legal determination; the second is checked mechanically below (every
citation not in the retrieved set is dropped, and an interpretation left with no
citations at all is discarded entirely); the third is enforced by
MACHINE_ASSERTABLE_CONFIDENCE, which does not contain CONFIRMED.

The finding always ends at REVIEW_REQUIRED. There is no path through this module to
an approved finding.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.regwatch.rules import change_detection
from app.agents.regwatch.rules import relevance as relevance_rules
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import impact_service, lifecycle
from app.config import get_settings
from app.db.models import Organization, RegWatchChange, RegWatchFinding, RegWatchSource
from app.db.repositories import regwatch_repository as repo
from app.llm.client import NvidiaLLMClient, generate_structured_with_fallback
from app.rag import retriever
from app.services import audit_service

logger = logging.getLogger(__name__)

# How much of the changed text the model sees. A regulator's page can be long; the
# diff excerpt is the part that actually moved, and sending the whole document would
# both cost more and bury the change.
MAX_CHANGE_CHARS = 6000
RAG_TOP_K = 5
# The cutoff beyond which a chunk is not really about the query comes from
# `settings.rag_max_distance`, the SAME value Agents 1 and 4 retrieve with. It is not
# a constant here.
#
# It was a constant here, set to 0.55, and that was wrong in a way worth recording.
# This embedding model's cosine distances sit in a higher band than that: measured
# against the live corpus, the query "consent notice withdrawal data principal rights"
# returns the DPDP Act itself at 0.635. Every retrieval was therefore discarded, every
# finding reported "no passage was close enough to ground an interpretation", and the
# whole grounded-interpretation path was dead code -- while looking, from the outside,
# exactly like an honest corpus miss. Reading one number off the other two agents
# beats inventing one, and it means a future re-tuning applies to all three.


class _Interpretation(BaseModel):
    """What the model is allowed to return. Note what is absent: no `applies`, no
    `obligation`, no confidence field it could set to `confirmed`."""

    plain_summary: str = Field(
        description="2-4 sentences, plain English, describing what the source text says changed"
    )
    what_to_check: list[str] = Field(
        default_factory=list,
        description="questions a compliance reviewer should answer; questions, never conclusions",
    )
    cited_chunk_ids: list[str] = Field(
        default_factory=list,
        description="chunk_id values from the supplied context that support the summary",
    )


_SYSTEM_PROMPT = """You are a regulatory-monitoring assistant inside a privacy compliance system.

You are given (a) a description of a change detected on an official regulatory source
and (b) passages from an approved regulatory knowledge base, each with a chunk_id.

Your job is to describe, in plain English, what the change appears to say -- and
nothing more.

Hard rules:
- You do NOT decide whether any obligation applies to this organisation. That is a
  legal determination made by a person. Never write that something "must" or "is
  required" of the reader.
- Every chunk_id you cite must appear in the supplied context. Never invent one, and
  never cite a passage you did not use.
- If the supplied passages do not support a statement, leave the statement out.
- If you cannot describe the change from what you were given, say so in
  plain_summary and return no citations.
- `what_to_check` holds QUESTIONS for a reviewer, not conclusions.
- Return nothing but the structured response."""


async def assess(
    db: AsyncSession,
    finding: RegWatchFinding,
    *,
    actor_user_id: uuid.UUID | None = None,
    use_llm: bool = True,
) -> RegWatchFinding:
    """Assess a detected finding and leave it waiting for a person.

    Returns the finding. It always ends at REVIEW_REQUIRED unless the deterministic
    steps themselves could not run, in which case it ends at FAILED with a code --
    never quietly at DETECTED, where nothing would ever pick it up again.
    """
    lifecycle.assert_transition(finding.status, watch.ASSESSING)
    before_status = finding.status
    finding.status = watch.ASSESSING
    finding.updated_at = datetime.now(UTC)
    await db.flush()

    source = await repo.get_source(db, finding.source_id, finding.org_id)
    change = await repo.get_change(db, finding.change_id, finding.org_id)
    if source is None or change is None:
        return await _fail(
            db, finding, before_status,
            code=watch.ERR_ASSESSMENT_FAILED,
            detail=(
                "the source or change this finding was raised from is missing; it "
                "cannot be assessed"
            ),
            actor_user_id=actor_user_id,
        )

    jurisdictions = await org_jurisdictions(db, finding.org_id)

    # ── 1. Relevance, deterministic ────────────────────────────────────────────
    result = relevance_rules.assess(
        source_jurisdiction=source.jurisdiction,
        org_jurisdictions=jurisdictions,
        change_text=_change_text(change),
        change_kind=change.change_kind,
        source_topic=source.topic,
    )
    finding.relevance = result.relevance
    finding.relevance_confidence = result.confidence
    finding.relevance_reason = result.reason
    _assert_machine_assertable(result.confidence, "relevance")

    # ── 2. Priority, deterministic ─────────────────────────────────────────────
    priority, priority_confidence = relevance_rules.priority_for(
        result, change_is_minor=_is_minor(change)
    )
    finding.priority = priority
    finding.priority_confidence = priority_confidence
    _assert_machine_assertable(priority_confidence, "priority")

    await audit_service.record(
        db, org_id=finding.org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_RELEVANCE_ASSESSED,
        entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
        after={
            "relevance": result.relevance, "confidence": result.confidence,
            "reason": result.reason, "evidence": list(result.evidence),
            "topics": list(result.topics),
            "priority": priority, "priority_confidence": priority_confidence,
            "org_jurisdictions": list(jurisdictions),
        },
    )

    # ── 3. Impact, deterministic and by reference ──────────────────────────────
    impacts, gaps = await impact_service.map_impact(
        db, finding,
        topics=impact_service.suggest_topics(result),
        actor_user_id=actor_user_id,
    )
    finding.impact_summary = impact_service.summarise_impact(impacts, gaps)

    # Rebuilt from the change row, NOT carried over from `finding.open_questions`.
    # Assessment can run more than once -- a retry, a reviewer sending it back -- and
    # appending this run's notes to the last run's produced a finding that carried
    # five citations AND a note saying nothing could be cited. The change-level notes
    # are permanently true of the change; this run's notes are not.
    questions = list(change_detection.notes_for_change(
        change_kind=change.change_kind,
        added_lines=change.added_lines,
        removed_lines=change.removed_lines,
        failure_reason=_failure_reason(finding),
    ))
    questions.extend(gaps)

    # ── 4. Interpretation, optional, grounded ──────────────────────────────────
    #
    # Everything the interpretation owns is RESET first and written only on success.
    # Leaving the previous run's citations in place while this run reported that it
    # had nothing to cite produced a finding carrying five citations under a note
    # saying none could be found -- and re-merging the drafted prose onto an already
    # merged summary compounded it a paragraph at a time. Each of these fields is now
    # a function of this run alone.
    deterministic_summary = change_detection.summarise_stored(
        change_kind=change.change_kind,
        added_lines=change.added_lines,
        removed_lines=change.removed_lines,
        source_name=source.name,
    )
    finding.summary = deterministic_summary
    finding.citations = []
    finding.grounded_facts = []
    finding.drafted_by_model = None

    if change.change_kind == watch.CHANGE_UNREACHABLE:
        # Checked first: there is no document, so there is nothing to interpret and
        # nothing a model could be asked about.
        questions.append(
            "The source could not be collected, so there is no text to interpret. "
            "What the page says now is unknown."
        )
    elif not use_llm:
        # Said out loud. An empty citation list with no note beside it reads as
        # "there was nothing to say", when the truth is that nobody asked.
        questions.append(
            "No interpretation was attempted for this change; it is described from "
            "the source text only. The absence of citations here is not a finding "
            "that the approved knowledge base has nothing on it."
        )
    else:
        interpreted, note = await _interpret(db, finding, source, change)
        if interpreted is None:
            # The deterministic assessment stands; the finding says what is missing.
            questions.append(note)
            logger.warning(
                "Interpretation unavailable for finding %s: %s", finding.reference, note
            )
        else:
            finding.summary = _merge_summary(
                deterministic_summary, interpreted["plain_summary"]
            )
            finding.citations = interpreted["citations"]
            finding.grounded_facts = interpreted["grounded_facts"]
            finding.drafted_by_model = interpreted["model"]
            questions.extend(interpreted["what_to_check"])
            await audit_service.record(
                db, org_id=finding.org_id, actor_user_id=actor_user_id,
                action=watch.AUDIT_INTERPRETED,
                entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
                after={
                    "model": interpreted["model"],
                    "citations": len(interpreted["citations"]),
                    "dropped_citations": interpreted["dropped"],
                    "note": "prose only; no obligation was established by the model",
                },
            )

    # Deduplicated but order-preserving: the same gap can arrive from two topics.
    finding.open_questions = list(dict.fromkeys(q for q in questions if q))
    finding.requires_human_review = True

    lifecycle.assert_transition(finding.status, watch.REVIEW_REQUIRED)
    finding.status = watch.REVIEW_REQUIRED
    finding.updated_at = datetime.now(UTC)
    await db.flush()

    await audit_service.record(
        db, org_id=finding.org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_REVIEW_REQUESTED,
        entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
        before={"status": watch.ASSESSING},
        after={
            "status": watch.REVIEW_REQUIRED,
            "relevance": finding.relevance,
            "priority": finding.priority,
            "impacts": len(impacts),
            "open_questions": len(finding.open_questions),
        },
    )
    return finding


async def org_jurisdictions(db: AsyncSession, org_id: uuid.UUID) -> tuple[str, ...]:
    """Where this organisation says it operates.

    Deliberately NOT derived from the registered sources: an organisation that
    monitors the EDPB is not thereby established as operating in the EU, and deriving
    one from the other would make the jurisdiction test vacuous. An empty tuple is a
    real answer -- the rules report `undetermined` and ask for them to be recorded.
    """
    result = await db.execute(
        select(Organization.jurisdictions).where(Organization.id == org_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return ()
    return tuple(str(j).strip() for j in row if str(j).strip())


def _failure_reason(finding: RegWatchFinding) -> str | None:
    """The collector's reason, as recorded on the finding when it was raised.

    Read from the finding rather than re-fetched: the collection that failed is the
    one this finding was raised from, and its reason was written into the first open
    question at creation. Re-deriving it would mean another round trip to say the
    same thing.
    """
    existing = list(finding.open_questions or [])
    return existing[0] if existing else None


def _change_text(change: RegWatchChange) -> str | None:
    """What the rules read. The diff excerpt, because that is what moved."""
    return (change.diff_excerpt or "")[:MAX_CHANGE_CHARS] or None


def _is_minor(change: RegWatchChange) -> bool:
    """A handful of lines. Used only to soften priority, never to suppress a finding --
    a one-line amendment can be the entire change."""
    return (change.added_lines + change.removed_lines) <= 3


def _assert_machine_assertable(confidence: str, what: str) -> None:
    """CONFIRMED is not available to any rule in this agent. A rule that reached it
    would be asserting a legal position on the organisation's behalf."""
    if confidence not in watch.MACHINE_ASSERTABLE_CONFIDENCE:
        raise AssertionError(
            f"a rule produced {confidence!r} {what} confidence; only "
            f"{sorted(watch.MACHINE_ASSERTABLE_CONFIDENCE)} may be asserted without a person"
        )


def _merge_summary(deterministic: str | None, drafted: str) -> str:
    """The deterministic sentence stays and the drafted prose follows it.

    Not replaced. The first sentence is what the system OBSERVED (this source
    changed, this many lines moved); the second is what a model made of it. Losing
    the first would leave only the part that cannot be verified.
    """
    head = (deterministic or "").strip()
    if not head:
        return drafted.strip()
    return f"{head}\n\n{drafted.strip()}"


async def _interpret(
    db: AsyncSession,
    finding: RegWatchFinding,
    source: RegWatchSource,
    change: RegWatchChange,
) -> tuple[dict | None, str]:
    """Draft plain-English prose grounded in the approved corpus.

    Returns (payload, note). `payload` is None whenever the interpretation could not
    be produced OR could not be grounded -- and `note` then says which, in words that
    are meant to be read by a compliance reviewer, not a developer.
    """
    text = _change_text(change)
    if not text:
        return None, (
            "There was no changed text to interpret -- the change was detected by "
            "content hash, but no excerpt was captured."
        )

    settings = get_settings()
    try:
        chunks = await retriever.retrieve(
            f"{source.jurisdiction} {source.topic or ''} {text[:1000]}",
            db=db,
            llm_client=NvidiaLLMClient(settings),
            top_k=RAG_TOP_K,
            max_distance=settings.rag_max_distance,
        )
    except Exception as exc:  # noqa: BLE001 -- any retrieval failure is the same outcome here
        logger.warning("Regulatory corpus retrieval failed: %s", exc)
        return None, (
            "The approved regulatory knowledge base could not be searched, so no "
            "interpretation was drafted. The change itself is recorded and unaffected."
        )

    if not chunks:
        # Not an error. The corpus genuinely has nothing on this, and a model invited
        # to write about it with no context is exactly the invention this forbids.
        return None, (
            "No passage in the approved regulatory knowledge base was close enough to "
            "this change to ground an interpretation. It is described from the source "
            "text only, and needs a reviewer who knows the instrument."
        )

    allowed = {chunk.chunk_id: chunk for chunk in chunks}
    context = "\n\n".join(
        f"[chunk_id: {chunk.chunk_id}] ({chunk.document_title})\n{chunk.content}"
        for chunk in chunks
    )
    user_prompt = (
        f"SOURCE: {source.name} ({source.authority or 'authority not recorded'}), "
        f"jurisdiction {source.jurisdiction}\n"
        f"CHANGE KIND: {change.change_kind} "
        f"(+{change.added_lines}/-{change.removed_lines} lines)\n\n"
        f"CHANGED TEXT:\n{text}\n\n"
        f"APPROVED CONTEXT:\n{context}"
    )

    try:
        drafted, meta = await generate_structured_with_fallback(
            system=_SYSTEM_PROMPT, user=user_prompt,
            schema=_Interpretation, settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 -- both providers exhausted; recorded, never raised
        logger.warning("Interpretation model failed for %s: %s", finding.reference, exc)
        return None, (
            "The interpretation model was unavailable, so this change is presented "
            "from the source text only. The detection itself is unaffected."
        )

    # The mechanical grounding check. A chunk_id the model produced that was not in
    # the context is a fabricated citation, and it is dropped rather than corrected.
    cited = [cid for cid in drafted.cited_chunk_ids if cid in allowed]
    dropped = [cid for cid in drafted.cited_chunk_ids if cid not in allowed]
    if dropped:
        logger.warning(
            "Dropped %d citation(s) the model invented on %s: %s",
            len(dropped), finding.reference, dropped,
        )
    if not cited:
        # Prose with nothing behind it reads exactly like prose with something behind
        # it, which is why it is discarded rather than shown with a caveat.
        return None, (
            "An interpretation was drafted but cited nothing in the approved knowledge "
            "base, so it was discarded rather than shown. This change needs to be read "
            "by a person."
        )

    model_name = meta.get("primary_model") if meta.get("primary_status") == "success" \
        else meta.get("fallback_model") or meta.get("primary_model")

    return {
        "plain_summary": drafted.plain_summary,
        "what_to_check": [q for q in drafted.what_to_check if q and q.strip()],
        "citations": [
            {
                "chunk_id": cid,
                "document_id": allowed[cid].document_id,
                "document_title": allowed[cid].document_title,
                "document_version": allowed[cid].document_version,
                "section": allowed[cid].section,
            }
            for cid in cited
        ],
        # What the prose was built from, kept verbatim so a reviewer can check the
        # summary against the passage rather than taking it on trust.
        "grounded_facts": [
            {"chunk_id": cid, "excerpt": allowed[cid].content[:1000]} for cid in cited
        ],
        "model": model_name,
        "dropped": dropped,
    }, ""


async def _fail(
    db: AsyncSession,
    finding: RegWatchFinding,
    before_status: str,
    *,
    code: str,
    detail: str,
    actor_user_id: uuid.UUID | None,
) -> RegWatchFinding:
    """Park a finding at FAILED with a reason.

    FAILED, not back to DETECTED: a finding returned to DETECTED would be picked up
    by the next sweep and fail again, forever, with nobody told.
    """
    lifecycle.assert_transition(finding.status, watch.FAILED)
    finding.status = watch.FAILED
    finding.error_code = code
    finding.error_detail = detail[:2000]
    finding.requires_human_review = True
    finding.updated_at = datetime.now(UTC)
    await db.flush()
    await audit_service.record(
        db, org_id=finding.org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_STATUS_CHANGED,
        entity_type=watch.AUDIT_ENTITY, entity_id=finding.id,
        before={"status": before_status},
        after={"status": watch.FAILED, "error_code": code, "error_detail": detail},
    )
    logger.error("Assessment failed for finding %s: %s", finding.reference, detail)
    return finding
