import re
import time
import uuid

from app.agents.consent_agent.state import AgentState
from app.llm.prompts import compact_scan_evidence, pre_post_reject_vendor_lists
from app.llm.schemas import ConsentAnalysisResponse
from app.observability.stage_tracker import track_stage
from app.rules.tracker_catalog import CMP_CATALOG, TRACKER_CATALOG

MAX_VALIDATION_ATTEMPTS = 2

# The model's own catalog of nameable vendors, checked against finding/recommendation
# text verbatim -- see _unsupported_vendor_claims below.
_KNOWN_VENDOR_NAMES = sorted({sig.vendor for sig in TRACKER_CATALOG} | {sig.cmp_vendor for sig in CMP_CATALOG})

# Vendor names that are also ordinary words. "analytics fired for every visitor segment" does not name Segment, so these count only when written with the
# vendor's own capital letter. Every other name is matched whole-word, case-insensitively: "cookiebot-free" or "segments" is not a mention of a vendor.
_ORDINARY_WORD_VENDORS = frozenset({"Segment"})


def _names_vendor(text: str, vendor: str) -> bool:
    flags = 0 if vendor in _ORDINARY_WORD_VENDORS else re.IGNORECASE
    return re.search(rf"(?<![\w-]){re.escape(vendor)}(?![\w-])", text, flags) is not None

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


# A finding asserting tracking carried on after the visitor rejected consent: a
# reject-word preceded within a few words by after/despite/following. Matches "after
# the visitor clicked Reject", "despite the user declining", "following rejection";
# leaves alone prose that only describes the automation limit ("the Reject control
# could not be automated", "no 'reject all' option was found"), which carries no
# temporal claim about what happened once a rejection was made.
_POST_REJECT_CLAIM_RE = re.compile(
    r"\b(?:after|despite|following|post)\W+(?:\w+\W+){0,4}?"
    r"(?:reject\w*|declin\w*|refus\w*|den(?:y|ied|ying)|opt(?:ed|ing)?[- ]?out)",
    re.IGNORECASE,
)


def _claims_tracking_after_reject(text: str) -> bool:
    return bool(_POST_REJECT_CLAIM_RE.search(text or ""))


# Mirrors _POST_REJECT_CLAIM_RE for the other side of the same failure mode: a finding
# asserting tracking fired before the visitor interacted with consent at all.
_PRE_CONSENT_CLAIM_RE = re.compile(
    r"\b(?:before|prior to|ahead of)\W+(?:\w+\W+){0,4}?consent\b|\bpre-?consent\b",
    re.IGNORECASE,
)


def _claims_tracking_before_consent(text: str) -> bool:
    return bool(_PRE_CONSENT_CLAIM_RE.search(text or ""))


# A claim phrase that is only being DENIED is not a claim. "No tracker fired before consent" and "nothing continued after
# Reject" are statements about an absence, and they are exactly what a clean site's findings say; flagging them as "named as
# firing before consent" rejected correct findings and sent the whole analysis round again (measured: in 9 of 23 stored scans the
# vendor-timing check failed attempt after attempt on findings of this kind, 4 scans ran out of attempts and fell back to rule
# findings, and each retry re-sent the entire 4-11k token prompt).
#
# Deliberately narrow, so the check keeps catching an actual false assertion:
#   - a negation must come BEFORE the phrase, in the same sentence, with no contrast word ("but", "however") in between;
#   - "did not stop / failed to stop / did not wait / did not honour" are negations of a good behaviour, so they ASSERT the
#     tracking happened and stay claims;
#   - an exception after the phrase ("nothing before consent except Google Analytics") names a vendor and stays a claim.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_NEGATION_RE = re.compile(r"\b(?:no|not|never|none|nothing|neither|nor|without|cannot|nobody)\b|n't\b", re.IGNORECASE)
_CONTRAST_RE = re.compile(r"\b(?:but|however|although|though|whereas|yet|while)\b", re.IGNORECASE)
_CLAUSE_BREAK_RE = re.compile(r",\s*(?:and|so|then)\b", re.IGNORECASE)
_EXCEPTION_RE = re.compile(r"\b(?:except|other than|apart from|besides|aside from)\b", re.IGNORECASE)
_NEGATED_GOOD_BEHAVIOUR_RE = re.compile(
    r"(?:\b(?:did|does|do|was|were|has|have|had|is|are)\s+not|n't|\bfail(?:ed|s)?\s+to|\bnever)\s+(?:\w+\s+)?"
    r"(?:stop\w*|cease\w*|halt\w*|wait\w*|honou?r\w*|respect\w*|block\w*|prevent\w*|suppress\w*|pause\w*)",
    re.IGNORECASE,
)


def _claim_is_denied(sentence: str, match: re.Match) -> bool:
    """True when the claim phrase `match` in `sentence` is being denied rather than asserted."""
    if _EXCEPTION_RE.search(sentence[match.end():]):
        return False
    before = sentence[:match.start()]
    # A negation in an earlier, separate clause ("No banner was shown, and Google Analytics fired before consent") does not
    # deny a claim made in a new clause.
    clauses = list(_CLAUSE_BREAK_RE.finditer(before))
    if clauses:
        before = before[clauses[-1].end():]
    contrasts = list(_CONTRAST_RE.finditer(before))
    if contrasts:
        before = before[contrasts[-1].end():]
    if _NEGATED_GOOD_BEHAVIOUR_RE.search(before):
        return False
    return bool(_NEGATION_RE.search(before))


def _asserts(text: str, pattern: re.Pattern) -> bool:
    """Whether `text` ASSERTS what `pattern` describes in at least one sentence (a denied mention does not count)."""
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        for match in pattern.finditer(sentence):
            if not _claim_is_denied(sentence, match):
                return True
    return False


def _unsupported_vendor_claims(
    finding, pre_consent_vendors: list[str], post_reject_vendors: list[str]
) -> list[str]:
    """A finding naming a vendor as having fired BEFORE consent or AFTER a confirmed
    Reject is a specific, checkable claim -- the failure this catches was observed
    live: a finding named "Google Ads" as firing before consent, and another named it
    as continuing after Reject, when this scan's own tracker evidence showed Google
    Ads firing only during the post_accept pass. `pre_consent_vendors`/
    `post_reject_vendors` are the same ground-truth lists handed to the model in the
    prompt (see llm/prompts.py:pre_post_reject_vendor_lists) -- checked here in code
    because the model restating them correctly is not guaranteed.

    Only an ASSERTED timing counts (see _claim_is_denied): "no tracker fired before consent" is
    true of a clean site and is not a claim that any vendor did."""
    text = f"{finding.finding} {finding.recommendation}"
    claims_pre = _asserts(text, _PRE_CONSENT_CLAIM_RE)
    claims_post_reject = _asserts(text, _POST_REJECT_CLAIM_RE)
    if not (claims_pre or claims_post_reject):
        return []
    mentioned = [v for v in _KNOWN_VENDOR_NAMES if _names_vendor(text, v)]
    unsupported = []
    for vendor in mentioned:
        if claims_pre and vendor not in pre_consent_vendors:
            unsupported.append(f"{vendor} (named as firing before consent)")
        if claims_post_reject and vendor not in post_reject_vendors:
            unsupported.append(f"{vendor} (named as firing after reject)")
    return unsupported


def _reject_was_confirmed(scan_evidence: dict | None) -> bool:
    """True only when the scanner both clicked Reject AND observed it take effect --
    the same two halves consent_rules requires before R-003 may fire. Anything weaker,
    including a missing flag, is unconfirmed."""
    signals = (scan_evidence or {}).get("consent_signals") or []
    if not signals:
        return False
    evidence = (signals[0] or {}).get("evidence") or {}
    return (
        evidence.get("reject_interaction") == "clicked"
        and evidence.get("reject_click_confirmed") is True
    )


async def validate_output(state: AgentState) -> dict:
    """Step 9 — Pydantic schema validity is already guaranteed by llm/client.py's own
    retry loop; what's checked here is domain-specific: every dpdp_reference must be a
    chunk_id that was actually retrieved.

    An ungrounded citation is never a warning: it is removed, and the finding that
    carried it is forced to human review, so it cannot reach a customer either as an
    invented citation or as an auto-approved claim. It escalates to a hard failure of
    the whole attempt (a retry) only when EVERY finding is ungrounded -- see the
    comment on the stripping step for why one bad reference no longer discards the
    findings that were correctly grounded."""
    async with track_stage(
        uuid.UUID(state.scan_id), "output_validation", agent_run_id=uuid.UUID(state.agent_run_id)
    ) as meta:
        # Nothing to validate: the analysis node could not reach any provider and has
        # already marked this run failed. Passing that status through unchanged is
        # what routes the graph to the rule-derived fallback. Calling
        # model_validate(None) here would raise instead, killing the run at the node
        # immediately after the one that was supposed to have degraded gracefully.
        if state.llm_output is None:
            meta["skipped"] = True
            meta["reason"] = state.error or "the analysis step produced no output"
            return {"validation_status": "failed", "error": state.error}

        response = ConsentAnalysisResponse.model_validate(state.llm_output)
        valid_chunk_ids = {c["chunk_id"] for c in state.rag_chunks}
        ungrounded = sorted({
            ref for finding in response.findings for ref in finding.dpdp_reference if ref not in valid_chunk_ids
        })

        compacted_evidence = compact_scan_evidence(state.scan_evidence or {})
        pre_consent_vendors, post_reject_vendors = pre_post_reject_vendor_lists(compacted_evidence)
        unsupported_vendor_claims = {
            i: claims
            for i, finding in enumerate(response.findings)
            if (claims := _unsupported_vendor_claims(finding, pre_consent_vendors, post_reject_vendors))
        }

        meta["findings_checked"] = len(response.findings)
        meta["ungrounded_citations"] = ungrounded
        meta["unsupported_vendor_claims"] = unsupported_vendor_claims

        # A citation the model invented does not require throwing away the findings it
        # got right. Previously ANY ungrounded id failed the whole attempt and re-ran
        # the entire analysis: one bad reference out of six findings cost a second full
        # generate/validate cycle (measured live -- 6 findings drafted, 1 ungrounded id,
        # a 33.6s re-run on top of a 365.7s first attempt). Worse, the id in that run
        # was an EVIDENCE id, not a chunk id, so the model had confused two namespaces
        # and a blind re-roll was not especially likely to fix it.
        #
        # So strip the ungrounded ids from the findings carrying them and force those
        # findings to human review. The grounding guarantee is unchanged -- no customer
        # ever sees an invented citation, because it is removed, and the finding cannot
        # be auto-approved either, since the uncited-findings backstop below routes
        # anything with no dpdp_reference to a human. A retry is still the right answer
        # when EVERY finding is ungrounded, which reads as the model misunderstanding
        # the task rather than slipping on one reference.
        #
        # This narrowing applies ONLY to citation grounding. An unsupported vendor-timing
        # claim is a different failure -- the finding asserts something about the scan
        # that the evidence does not say, which stripping a field cannot repair -- so it
        # keeps its own retry below, untouched.
        findings_with_bad_refs = [
            f for f in response.findings if any(ref not in valid_chunk_ids for ref in f.dpdp_reference)
        ]
        if ungrounded and len(findings_with_bad_refs) < len(response.findings):
            for finding in findings_with_bad_refs:
                finding.dpdp_reference = [r for r in finding.dpdp_reference if r in valid_chunk_ids]
                finding.requires_human_review = True
            meta["ungrounded_stripped_from_findings"] = len(findings_with_bad_refs)
            ungrounded = []

        if not ungrounded and not unsupported_vendor_claims:
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

            # Third backstop, and the only one about SEVERITY rather than citations.
            #
            # `requires_human_review` is chosen by the LLM, per finding. On a live scan
            # of a real site it set False on both high-risk findings -- "analytics fired
            # before any consent interaction" and "analytics continued firing after the
            # visitor clicked Reject" -- while setting True on a low-risk "could not
            # classify one script". The serious violations would have reached the
            # customer auto-accepted; the trivial one got the human.
            #
            # A high-risk DPDP finding is an accusation against the customer's website.
            # Whether it is auto-approved is not a judgement to delegate to the model,
            # so it is taken away from it here. The LLM may still RAISE review on
            # anything it likes; it can no longer waive it on the findings that matter.
            high_risk_forced_review = 0
            for finding in response.findings:
                if finding.risk_level == "high" and not finding.requires_human_review:
                    finding.requires_human_review = True
                    high_risk_forced_review += 1
            meta["high_risk_forced_review"] = high_risk_forced_review

            # Fourth backstop: the model is handed the post_reject evidence whether or
            # not the rejection was ever established, and the prompt telling it so is
            # not a control. A claim that tracking continued after a rejection, on a
            # scan that cannot show a rejection took effect, goes to a human --
            # whatever risk level the model filed it under.
            unconfirmed_reject_forced_review = 0
            if not _reject_was_confirmed(state.scan_evidence):
                for finding in response.findings:
                    if (
                        _claims_tracking_after_reject(f"{finding.finding} {finding.recommendation}")
                        and not finding.requires_human_review
                    ):
                        finding.requires_human_review = True
                        unconfirmed_reject_forced_review += 1
            meta["unconfirmed_reject_forced_review"] = unconfirmed_reject_forced_review

            meta["outcome"] = "valid"
            return {"validation_status": "valid", "error": None, "llm_output": response.model_dump()}

        if state.validation_attempts >= MAX_VALIDATION_ATTEMPTS:
            meta["outcome"] = "failed"
            return {
                "validation_status": "failed",
                "error": (
                    f"LLM output failed grounding after {state.validation_attempts} retries: "
                    f"ungrounded dpdp_reference ids {ungrounded}; unsupported vendor-timing "
                    f"claims {unsupported_vendor_claims}"
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
                    f"{state.validation_attempts + 1}; ungrounded citations: {ungrounded}; "
                    f"unsupported vendor-timing claims: {unsupported_vendor_claims}"
                ),
            }

        meta["outcome"] = "retry"
        error_parts = []
        if ungrounded:
            error_parts.append(
                f"Your dpdp_reference values {ungrounded} are not among the provided chunk_ids "
                f"({sorted(valid_chunk_ids)}). Only cite chunk_ids that were given to you, or omit "
                f"dpdp_reference entries for a finding if nothing supplied is relevant."
            )
        if unsupported_vendor_claims:
            error_parts.append(
                "Some findings name a vendor as firing before consent or after reject that is "
                f"not in the supplied vendor lists for that state: {unsupported_vendor_claims}. "
                f"Vendors confirmed pre_consent: {pre_consent_vendors or 'none'}; confirmed "
                f"post_reject: {post_reject_vendors or 'none'}. Only make that specific claim "
                "about a vendor that is actually in the matching list."
            )
        return {
            "validation_status": "retry",
            "validation_attempts": state.validation_attempts + 1,
            "error": " ".join(error_parts),
        }
