"""DSR request classification (prompt §14).

Real phrasings, because that is what arrives. The cases that matter most are the
last two groups: the model must never be able to invent a type, and a request that
asks for two different things must never be quietly resolved into one.
"""

import pytest

from app.agents.dsr.rules import request_classifier as rc
from app.agents.dsr.schemas import case


@pytest.mark.parametrize(
    "text,expected",
    [
        # The four the prompt names in §6.
        ("Show me what personal information you hold about me.", case.ACCESS),
        ("Correct my phone number.", case.CORRECTION),
        ("Delete my personal information.", case.DELETION),
        ("Tell me what information you process about me.", case.ACCESS),
        # Access, phrased the ways people actually phrase it.
        ("What personal data do you have on me?", case.ACCESS),
        ("I would like a copy of my personal data", case.ACCESS),
        ("Please give me all the records you keep about me", case.ACCESS),
        ("I am exercising my right of access under the DPDP Act", case.ACCESS),
        # Deletion.
        ("Please erase my data", case.DELETION),
        ("I want to exercise my right to be forgotten", case.DELETION),
        ("Remove all my personal details from your systems", case.DELETION),
        ("Stop processing my personal data", case.DELETION),
        # Correction.
        ("My address is wrong, please fix it", case.CORRECTION),
        ("Update my email address please", case.CORRECTION),
        ("The phone number you have is incorrect", case.CORRECTION),
        # Export.
        ("I want to export my data in a machine-readable format", case.EXPORT),
        ("Please provide my data for portability", case.EXPORT),
        # Information about processing, not the data itself.
        ("Why do you process my information?", case.INFORMATION),
        ("How long do you keep my data?", case.INFORMATION),
        ("Who do you share my data with?", case.INFORMATION),
    ],
)
def test_real_phrasings_classify_deterministically(text, expected):
    result = rc.classify(text)
    assert result.request_type == expected, f"{text!r} -> {result.request_type} ({result.evidence})"
    assert result.method == rc.METHOD_DETERMINISTIC
    assert not result.ambiguous


def test_export_is_not_collapsed_into_access():
    """An export request is a narrower controlled type, not a flavour of access --
    collapsing them would lose the portability obligation."""
    assert rc.classify("Can I download a copy of my data?").request_type == case.EXPORT


def test_deletion_mentioning_data_is_not_a_conflict():
    """Almost every deletion request names the data it wants deleted. Treating that
    as two competing intents would send every erasure to human review."""
    result = rc.classify("Please delete all my personal data that you hold")
    assert result.request_type == case.DELETION
    assert not result.ambiguous


# ── Genuine ambiguity is surfaced, never resolved by score ───────────────────────

def test_two_real_requests_are_flagged_ambiguous():
    """§47: honouring the deletion half and dropping the access half is exactly the
    silent loss the prompt forbids."""
    result = rc.classify("Send me a copy of my data and then correct my phone number")
    assert result.ambiguous
    assert result.request_type is None
    assert set(result.candidates) == {case.ACCESS, case.CORRECTION}
    assert result.needs_reasoning


def test_ambiguous_result_names_its_candidates():
    result = rc.classify("Please update my records and then erase my account")
    assert result.ambiguous
    assert case.DELETION in result.candidates
    assert case.CORRECTION in result.candidates


# ── No gaps, no invented types ───────────────────────────────────────────────────

def test_unmatched_request_is_other_not_none():
    """OTHER is a reviewable outcome. A None here would be a case that looks
    unprocessed rather than one a human has been asked to read."""
    result = rc.classify("Hello, I have a question about my recent order.")
    assert result.request_type == case.OTHER
    assert not result.ambiguous
    assert result.needs_reasoning


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_request_is_other_not_a_crash(text):
    assert rc.classify(text).request_type == case.OTHER


def test_every_deterministic_outcome_is_in_the_closed_vocabulary():
    samples = [
        "delete my data", "correct my email", "show me my data", "export my data",
        "why do you process my data", "hello there", "",
    ]
    for text in samples:
        result = rc.classify(text)
        assert result.request_type is None or result.request_type in case.CLASSIFIABLE_TYPES


@pytest.mark.parametrize(
    "model_answer",
    ["partial_deletion", "gdpr_request", "ACCESS_AND_DELETION", "unclassified", "", None, "anything"],
)
def test_model_cannot_invent_a_request_type(model_answer):
    """§26: the model may reason about ambiguity, but it cannot widen the vocabulary.
    `unclassified` is rejected too -- it is the pre-classification default, never an
    outcome the model may choose."""
    assert rc.coerce_model_type(model_answer) is None


@pytest.mark.parametrize("valid", sorted(case.CLASSIFIABLE_TYPES))
def test_model_answers_inside_the_vocabulary_are_accepted(valid):
    assert rc.coerce_model_type(valid) == valid
    assert rc.coerce_model_type(f"  {valid.upper()}  ") == valid


def test_classification_always_carries_evidence():
    """§19: a conclusion a reviewer cannot trace is not usable."""
    for text in ["delete my data", "hello", "send me my data and delete it"]:
        assert rc.classify(text).evidence, f"no evidence for {text!r}"
