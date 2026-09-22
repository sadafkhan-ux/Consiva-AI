"""Relevance, priority, impact and the one model call Agent 5 makes.

The property this file exists to hold: NOTHING a rule or a model produces can become
a fact on its own. Concretely --

  * relevance is three-valued, and `undetermined` is what an unrecognised change gets,
    never `not_relevant`;
  * no rule in the agent may reach `confirmed`;
  * an impact link says "look here", not "this is affected";
  * a model's citation that was not in the retrieved context is dropped, and an
    interpretation left with no citations is discarded entirely rather than shown.
"""

import inspect
from dataclasses import dataclass

import pytest

from app.agents.regwatch.rules import relevance as rel
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import assessment_service, impact_service

# ── Relevance is three-valued, and reluctant ────────────────────────────────────

def test_an_unrecognised_change_is_undetermined_not_irrelevant():
    """The failure mode this guards: a two-valued filter has to call everything it
    does not recognise irrelevant, which is how a change that mattered gets dropped."""
    result = rel.assess(
        source_jurisdiction=None,
        org_jurisdictions=("India",),
        change_text="Notice regarding office timings during the festival period.",
        change_kind=watch.CHANGE_CONTENT,
    )
    assert result.relevance == watch.UNDETERMINED
    assert result.relevance != watch.NOT_RELEVANT
    assert result.needs_a_person


def test_a_source_that_could_not_be_collected_is_never_assessed_as_irrelevant():
    """There is no document. Guessing at relevance from an absent page would be
    inventing the very thing this agent refuses to invent."""
    result = rel.assess(
        source_jurisdiction="India",
        org_jurisdictions=("India",),
        change_text=None,
        change_kind=watch.CHANGE_UNREACHABLE,
    )
    assert result.relevance == watch.UNDETERMINED
    assert result.confidence == watch.UNKNOWN
    assert "could not be collected" in result.reason


def test_an_org_with_no_recorded_jurisdictions_rules_nothing_out():
    result = rel.assess(
        source_jurisdiction="EU",
        org_jurisdictions=(),
        change_text="consent and cookie rules",
        change_kind=watch.CHANGE_CONTENT,
    )
    assert result.relevance == watch.UNDETERMINED
    assert result.confidence == watch.UNKNOWN
    assert "no recorded jurisdictions" in result.reason


def test_a_jurisdiction_mismatch_is_the_one_positive_finding_of_irrelevance():
    result = rel.assess(
        source_jurisdiction="EU",
        org_jurisdictions=("India",),
        change_text="consent requirements amended",
        change_kind=watch.CHANGE_CONTENT,
    )
    assert result.relevance == watch.NOT_RELEVANT
    # Probable, never confirmed: an organisation can acquire an obligation in a
    # jurisdiction before somebody updates its profile.
    assert result.confidence == watch.PROBABLE


def test_a_global_org_is_not_filtered_out_by_jurisdiction():
    result = rel.assess(
        source_jurisdiction="EU",
        org_jurisdictions=("global",),
        change_text="consent requirements amended",
        change_kind=watch.CHANGE_CONTENT,
    )
    assert result.relevance != watch.NOT_RELEVANT


@pytest.mark.parametrize("spelling", ["IN", "in", "India", "INDIA", "in-IN", "bharat"])
def test_jurisdictions_are_compared_loosely(spelling):
    """Both sides are typed by hand. A spelling difference must not read as a positive
    finding that a change does not apply."""
    assert rel.normalise_jurisdiction(spelling) == "india"


def test_a_matching_jurisdiction_and_topic_is_relevant_but_only_probable():
    result = rel.assess(
        source_jurisdiction="India",
        org_jurisdictions=("India",),
        change_text="Rules on consent and cookie banners have been amended.",
        change_kind=watch.CHANGE_CONTENT,
    )
    assert result.relevance == watch.RELEVANT
    assert result.confidence == watch.PROBABLE
    assert "consent" in result.topics
    # Being told a change matters is the beginning of a decision, not the end of one.
    assert result.needs_a_person


# ── No rule may reach CONFIRMED ─────────────────────────────────────────────────

def test_confirmed_is_not_available_to_any_rule():
    assert watch.CONFIRMED not in watch.MACHINE_ASSERTABLE_CONFIDENCE


@pytest.mark.parametrize("change_kind", sorted(watch.CHANGE_KINDS))
@pytest.mark.parametrize("org", [(), ("India",), ("EU", "UK")])
def test_no_combination_of_inputs_produces_confirmed_relevance(change_kind, org):
    """Exhaustive rather than illustrative: `confirmed` appearing anywhere in the
    rules would be the agent taking a legal position on the organisation's behalf."""
    for text in (None, "consent", "erasure and portability", "cross-border transfer"):
        result = rel.assess(
            source_jurisdiction="India", org_jurisdictions=org,
            change_text=text, change_kind=change_kind,
        )
        assert result.confidence in watch.MACHINE_ASSERTABLE_CONFIDENCE
        priority, confidence = rel.priority_for(result, change_is_minor=False)
        assert confidence in watch.MACHINE_ASSERTABLE_CONFIDENCE
        assert priority in watch.PRIORITIES


def test_the_assessment_service_asserts_this_rather_than_assuming_it():
    """A future rule change that reached `confirmed` should break loudly here, not
    quietly write a legal conclusion into a finding."""
    with pytest.raises(AssertionError):
        assessment_service._assert_machine_assertable(watch.CONFIRMED, "relevance")


def test_a_one_line_amendment_is_not_downgraded_out_of_sight():
    """Softened to medium, never to low. A single line can be the entire change."""
    relevant = rel.RelevanceResult(
        relevance=watch.RELEVANT, confidence=watch.PROBABLE, reason="x",
    )
    priority, _ = rel.priority_for(relevant, change_is_minor=True)
    assert priority == watch.PRIORITY_MEDIUM


# ── Impact links point, they do not conclude ────────────────────────────────────

def test_every_impact_link_is_held_at_possible():
    body = inspect.getsource(impact_service.map_impact)
    assert "confidence=watch.POSSIBLE" in body
    assert "watch.CONFIRMED" not in body
    assert "watch.PROBABLE" not in body


def test_an_empty_impact_list_is_reported_as_a_gap_not_as_nothing_affected():
    """"No impacts" and "nothing is affected" look identical on a screen. The second
    is a conclusion this cannot reach."""
    sentence = impact_service.summarise_impact([], ["this org has no websites recorded"])
    assert "could be linked" in sentence
    assert "no websites recorded" in sentence
    assert "not affected" not in sentence


def test_a_change_matching_no_topic_records_why_nothing_was_linked():
    body = inspect.getsource(impact_service.map_impact)
    assert "if not topics:" in body
    assert "gaps.append" in body


def test_impacts_are_stored_by_reference_never_by_copy():
    """A copied label would drift the moment the other agent's row changed."""
    from app.db.models import RegWatchImpact

    fields = {c.name for c in RegWatchImpact.__table__.columns}
    # The reference itself, and a label for a human to read.
    assert {"target_kind", "target_id", "target_label"} <= fields
    # Nothing that would duplicate the owning agent's data and then drift from it.
    assert "payload" not in fields
    assert "target_snapshot" not in fields
    assert "target_state" not in fields


def test_the_sample_limit_reports_the_count_it_did_not_show():
    """Ten rows out of six hundred, presented as ten, is a misleading picture of
    scope."""
    body = inspect.getsource(impact_service._targets_for)
    assert "further ROPA record(s)" in body
    assert "count - _SAMPLE_LIMIT" in body


def test_impact_reads_the_columns_the_ropa_model_actually_has():
    """Caught live: this referenced `purpose` and `source_name`, neither of which
    exists on the row. It would have raised at the first real assessment."""
    from app.db.models import RopaRecordRow

    columns = {c.name for c in RopaRecordRow.__table__.columns}
    body = inspect.getsource(impact_service._targets_for)
    for attr in ("processing_activity", "status", "created_at", "org_id"):
        assert attr in columns
    assert "RopaRecordRow.purpose" not in body
    assert "RopaRecordRow.source_name" not in body


# ── The model cannot invent a citation ──────────────────────────────────────────

@dataclass
class _Chunk:
    chunk_id: str
    document_id: str = "doc-1"
    document_title: str = "DPDP Act 2023"
    document_version: str | None = "1.0"
    content: str = "Section 6 concerns consent."
    section: str | None = "6"


def test_a_citation_not_in_the_retrieved_context_is_dropped():
    body = inspect.getsource(assessment_service._interpret)
    assert "cid in allowed" in body
    assert "dropped" in body


def test_an_interpretation_with_no_surviving_citation_is_discarded_entirely():
    """Not shown with a caveat. Prose with nothing behind it reads exactly like prose
    with something behind it."""
    body = inspect.getsource(assessment_service._interpret)
    assert "if not cited:" in body
    assert "discarded" in body


def test_the_model_is_never_asked_whether_an_obligation_applies():
    prompt = assessment_service._SYSTEM_PROMPT
    assert "do NOT decide whether any obligation applies" in prompt
    fields = set(assessment_service._Interpretation.model_fields)
    # No field the model could use to assert applicability or confidence.
    assert fields == {"plain_summary", "what_to_check", "cited_chunk_ids"}
    assert "confidence" not in fields


def test_an_empty_corpus_produces_no_interpretation_rather_than_an_unsourced_one():
    body = inspect.getsource(assessment_service._interpret)
    assert "if not chunks:" in body
    assert "return None" in body


def test_the_deterministic_summary_survives_the_drafted_one():
    """The first sentence is what the system OBSERVED; the second is what a model made
    of it. Replacing the first would leave only the unverifiable half."""
    merged = assessment_service._merge_summary(
        "Source X changed: 4 lines added, 1 removed.", "The amendment concerns consent."
    )
    assert merged.startswith("Source X changed")
    assert "concerns consent" in merged


def test_a_model_failure_does_not_fail_the_assessment():
    body = inspect.getsource(assessment_service.assess)
    assert "if interpreted is None:" in body
    assert "questions.append(note)" in body
    # And the finding still reaches a person.
    assert "watch.REVIEW_REQUIRED" in body


def test_assessment_always_ends_needing_a_person():
    body = inspect.getsource(assessment_service.assess)
    assert "finding.requires_human_review = True" in body
    assert watch.APPROVED not in body.split("def assess")[1].split("async def")[0] \
        or "watch.APPROVED" not in body


def test_an_unreachable_source_is_never_sent_to_the_model():
    """It is the FIRST branch, so there is no ordering in which a collection that
    failed reaches _interpret: there is no document to interpret."""
    body = inspect.getsource(assessment_service.assess)
    interpretation = body.split("# ── 4. Interpretation")[1]
    first_branch = interpretation.split("elif")[0]
    assert "if change.change_kind == watch.CHANGE_UNREACHABLE:" in first_branch
    assert "_interpret(" not in first_branch


def test_org_jurisdictions_are_not_derived_from_the_registered_sources():
    """Deriving one from the other makes the jurisdiction test vacuous: every source
    would match by construction and `not_relevant` could never be reached."""
    body = inspect.getsource(assessment_service.org_jurisdictions)
    assert "Organization" in body
    assert "RegWatchSource" not in body


def test_a_missing_source_or_change_fails_the_finding_rather_than_leaving_it_detected():
    """Back to DETECTED would mean the next sweep picks it up and fails again,
    forever, with nobody told."""
    body = inspect.getsource(assessment_service._fail)
    assert "watch.FAILED" in body
    assert "error_code" in body
    assert "requires_human_review = True" in body


# ── Assessment must be idempotent ───────────────────────────────────────────────
#
# Three bugs of one shape, all found by re-assessing a real finding rather than by
# reading the code. Assessment can run more than once -- a retry, a reviewer sending
# it back -- and every field it writes has to be a function of THAT run. It wasn't:
#
#   * open_questions accumulated, so a finding carried five citations AND a note
#     saying nothing could be cited;
#   * citations / grounded_facts / drafted_by_model survived a run that failed to
#     ground anything, contradicting that run's own note;
#   * the drafted prose was merged onto an already-merged summary, compounding a
#     paragraph at a time.

@pytest.mark.parametrize("kind,added,removed", [
    (watch.CHANGE_FIRST_CAPTURE, 99, 0),
    (watch.CHANGE_CONTENT, 4, 1),
    (watch.CHANGE_CONTENT, 1, 0),
    (watch.CHANGE_CONTENT, 0, 2),
    (watch.CHANGE_UNREACHABLE, 0, 0),
])
def test_notes_rebuilt_from_a_stored_row_match_what_detect_produced(kind, added, removed):
    """The row is what survives, so the row has to be enough to rebuild them."""
    from app.agents.regwatch.rules import change_detection as cd

    rebuilt = cd.notes_for_change(
        change_kind=kind, added_lines=added, removed_lines=removed,
        failure_reason="probe failed",
    )
    if kind == watch.CHANGE_FIRST_CAPTURE:
        original = cd.detect(
            baseline_text=None, baseline_hash=None,
            new_text="\n".join("x" for _ in range(added)), new_hash="h",
        )
    elif kind == watch.CHANGE_UNREACHABLE:
        original = cd.detect(
            baseline_text=None, baseline_hash=None, new_text=None, new_hash=None,
            collection_failed=True, failure_reason="probe failed",
        )
    else:
        before = "\n".join(f"line {i}" for i in range(20))
        after_lines = [f"line {i}" for i in range(20)]
        for i in range(removed):
            after_lines.pop(0)
        after = "\n".join([f"new {i}" for i in range(added)] + after_lines)
        original = cd.detect(
            baseline_text=before, baseline_hash="a", new_text=after, new_hash="b",
        )
        # The diff decides the real counts; rebuild against those, not the request.
        rebuilt = cd.notes_for_change(
            change_kind=original.kind,
            added_lines=original.added_lines,
            removed_lines=original.removed_lines,
        )
    assert rebuilt == original.notes


@pytest.mark.parametrize("kind", sorted(watch.CHANGE_KINDS))
def test_the_deterministic_summary_can_be_rebuilt_from_the_row(kind):
    """It has to be reproducible so a re-assessment can REPLACE it rather than append
    a second copy of the drafted prose to it."""
    from app.agents.regwatch.rules import change_detection as cd

    first = cd.summarise_stored(
        change_kind=kind, added_lines=3, removed_lines=1, source_name="MeitY"
    )
    again = cd.summarise_stored(
        change_kind=kind, added_lines=3, removed_lines=1, source_name="MeitY"
    )
    assert first == again
    assert first.startswith("MeitY")
    if kind == watch.CHANGE_UNREACHABLE:
        assert "not a report that it is unchanged" in first


def test_assessment_rebuilds_its_questions_instead_of_appending_to_them():
    body = inspect.getsource(assessment_service.assess)
    assert "change_detection.notes_for_change" in body
    assert "list(finding.open_questions or [])" not in body, (
        "assessment is carrying the previous run's notes forward again"
    )


def test_a_failed_interpretation_clears_the_previous_ones_artifacts():
    """Otherwise the finding shows citations under a note saying none were found."""
    body = inspect.getsource(assessment_service.assess)
    assert "finding.citations = []" in body
    assert "finding.grounded_facts = []" in body
    assert "finding.drafted_by_model = None" in body
    # And the merge takes the freshly rebuilt head, never the stored summary.
    assert "_merge_summary(\n                deterministic_summary" in body
    assert "_merge_summary(finding.summary" not in body


def test_the_retrieval_cutoff_is_the_platforms_not_this_modules():
    """It was a local constant at 0.55. Measured against the live corpus, the query
    'consent notice withdrawal data principal rights' returns the DPDP Act itself at
    0.635, so every retrieval was discarded and the grounded-interpretation path was
    dead code -- while looking exactly like an honest corpus miss."""
    from app.config import get_settings

    body = inspect.getsource(assessment_service._interpret)
    assert "settings.rag_max_distance" in body
    assert not hasattr(assessment_service, "RAG_MAX_DISTANCE"), (
        "the local threshold constant is back"
    )
    # And it is loose enough to actually retain this corpus's distances.
    assert get_settings().rag_max_distance >= 0.7


def test_every_route_out_of_the_interpretation_step_leaves_a_note():
    """Zero citations with no note beside them reads as "there was nothing to say".
    Four ways to reach zero -- unreachable source, interpretation not requested, the
    model failed, the model cited nothing -- and each has to say which it was."""
    body = inspect.getsource(assessment_service.assess)
    assert "elif not use_llm:" in body
    assert "No interpretation was attempted" in body
    assert "no text to interpret" in body
    # Nothing falls through the branch without appending something.
    branch = body.split("if change.change_kind == watch.CHANGE_UNREACHABLE:")[1]
    branch = branch.split("# Deduplicated")[0]
    assert branch.count("questions.append") >= 3


@pytest.mark.parametrize("use_llm", [True, False])
def test_the_absence_of_citations_is_never_left_to_speak_for_itself(use_llm):
    """The claim this file exists to protect, stated as a property: a reader can
    always tell WHY there are no citations."""
    body = inspect.getsource(assessment_service.assess)
    if not use_llm:
        assert "No interpretation was attempted" in body
        assert "The absence of citations here is not a finding" in body
    else:
        assert "questions.append(note)" in body
