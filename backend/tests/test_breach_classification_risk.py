"""Incident classification (§9) and the severity/risk engine (§10, §19).

The classification tests use phrasings from real incident reports, because that is
what arrives. The risk tests are mostly about one thing: that missing evidence
produces a recorded gap rather than a comfortable score.
"""

from datetime import timedelta

import pytest

from app.agents.breach.rules import classification as clf
from app.agents.breach.rules import risk
from app.agents.breach.schemas import incident


# ── Classification ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Unauthorized access detected in the customer database", incident.TYPE_UNAUTHORIZED_ACCESS),
        ("Suspicious login from an unfamiliar country", incident.TYPE_UNAUTHORIZED_ACCESS),
        ("Brute force attempts against the admin portal", incident.TYPE_UNAUTHORIZED_ACCESS),
        ("Customer records were exposed on a public endpoint", incident.TYPE_DATA_EXPOSURE),
        ("Employee downloaded the full customer list before leaving", incident.TYPE_INSIDER),
        ("Finance laptop was stolen from a car", incident.TYPE_LOST_DEVICE),
        ("Ransomware encrypted our file server", incident.TYPE_MALWARE),
        ("Our vendor notified us of a breach on their side", incident.TYPE_THIRD_PARTY),
        ("An S3 bucket was publicly accessible with no authentication", incident.TYPE_MISCONFIGURATION),
        ("Invoice emailed to the wrong recipient", incident.TYPE_ACCIDENTAL_DISCLOSURE),
        ("Customer data appeared for sale on the dark web", incident.TYPE_DATA_LEAKAGE),
        ("An admin password was phished", incident.TYPE_CREDENTIAL_COMPROMISE),
    ],
)
def test_real_incident_phrasings_classify(text, expected):
    result = clf.classify(text)
    assert result.incident_type == expected, f"{text!r} -> {result.incident_type} ({result.evidence})"
    assert result.method == clf.METHOD_DETERMINISTIC
    assert not result.ambiguous


def test_the_root_cause_wins_over_the_consequence():
    """Incidents are reported as chains. The useful classification is the root cause,
    because that is what the containment action has to address."""
    result = clf.classify(
        "A phished credential was used to make an unauthorized login to the database"
    )
    assert result.incident_type == incident.TYPE_CREDENTIAL_COMPROMISE


def test_misconfiguration_beats_the_exposure_it_caused():
    result = clf.classify("A misconfigured bucket left customer records exposed")
    assert result.incident_type == incident.TYPE_MISCONFIGURATION


def test_exfiltration_beats_mere_exposure():
    """Data reachable and data taken are different incidents; the worse one wins."""
    result = clf.classify("Records were exposed and subsequently posted online")
    assert result.incident_type == incident.TYPE_DATA_LEAKAGE


def test_a_bare_keyword_does_not_decide_between_three_readings():
    """"stolen" means something different in each of three incident types, so a
    keyword alone must not settle it."""
    for text, expected in [
        ("A laptop was stolen", incident.TYPE_LOST_DEVICE),
        ("Credentials were stolen", incident.TYPE_CREDENTIAL_COMPROMISE),
    ]:
        assert clf.classify(text).incident_type == expected


def test_genuinely_competing_readings_are_flagged_not_guessed():
    result = clf.classify(
        "A laptop was stolen, ransomware encrypted the file server, and our vendor "
        "reported a breach"
    )
    assert result.ambiguous
    assert result.incident_type is None
    assert len(result.candidates) >= 3
    assert result.needs_reasoning


def test_an_unrecognised_report_is_other_not_none():
    result = clf.classify("Something odd happened this morning, please look into it")
    assert result.incident_type == incident.TYPE_OTHER
    assert result.needs_reasoning


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_text_does_not_crash(text):
    assert clf.classify(text).incident_type == incident.TYPE_OTHER


@pytest.mark.parametrize(
    "answer", ["mega_breach", "unclassified", "", None, "SQL injection", "anything"]
)
def test_a_model_cannot_invent_an_incident_type(answer):
    assert clf.coerce_model_type(answer) is None


@pytest.mark.parametrize("valid", sorted(incident.CLASSIFIABLE_TYPES))
def test_model_answers_inside_the_vocabulary_are_accepted(valid):
    assert clf.coerce_model_type(valid) == valid
    assert clf.coerce_model_type(valid.replace("_", " ").upper()) == valid


def test_classification_always_carries_evidence():
    for text in ["ransomware", "nothing recognisable", ""]:
        assert clf.classify(text).evidence


# ── Severity: uncertainty is not zero ────────────────────────────────────────────

def test_an_incident_with_no_evidence_is_not_quietly_low_confidence_high_score():
    """The whole trap this engine avoids: scoring absence of evidence as absence of
    harm, so a serious breach is triaged as routine."""
    a = risk.assess_severity(incident_type=incident.TYPE_UNAUTHORIZED_ACCESS)
    assert a.gaps, "an incident with nothing attached reported no open questions"
    assert any("no evidence" in g for g in a.gaps)
    assert a.confidence in (incident.UNKNOWN, incident.POSSIBLE)


def test_severity_never_claims_confirmed_confidence():
    """§12: an engine does not get to be certain. Only a human does."""
    for kind in incident.CLASSIFIABLE_TYPES:
        a = risk.assess_severity(
            incident_type=kind, evidence_count=10, has_external_evidence=True,
            affected_system_count=5, personal_data_involved=incident.CONFIRMED,
        )
        assert a.confidence != incident.CONFIRMED


def test_an_unclassified_incident_records_that_as_a_gap():
    a = risk.assess_severity(incident_type=incident.TYPE_UNCLASSIFIED)
    assert any("not been classified" in g for g in a.gaps)


def test_derived_only_evidence_does_not_corroborate_anything():
    """Evidence Consiva produced about itself is not evidence that something
    happened."""
    a = risk.assess_severity(
        incident_type=incident.TYPE_DATA_EXPOSURE, evidence_count=3,
        has_external_evidence=False,
    )
    assert any("no external evidence" in g for g in a.gaps)
    derived = next(f for f in a.factors if f.code == "DERIVED_ONLY")
    assert not derived.evidenced


def test_a_reporter_outranking_the_engine_is_surfaced_not_overridden():
    """If somebody said critical and the engine says medium, that disagreement is the
    most interesting thing on the incident."""
    a = risk.assess_severity(
        incident_type=incident.TYPE_ACCIDENTAL_DISCLOSURE,
        reported_severity=incident.SEV_CRITICAL,
    )
    assert a.level != incident.SEV_CRITICAL
    assert any("reporter assessed this as critical" in g for g in a.gaps)


def test_severity_rises_with_scope_and_personal_data():
    low = risk.assess_severity(incident_type=incident.TYPE_LOST_DEVICE)
    high = risk.assess_severity(
        incident_type=incident.TYPE_DATA_LEAKAGE,
        personal_data_involved=incident.CONFIRMED,
        affected_system_count=4, has_external_evidence=True, evidence_count=6,
    )
    assert high.score > low.score
    assert incident.SEVERITY_ORDER[high.level] > incident.SEVERITY_ORDER[low.level]


# ── Risk: gaps are recorded, never scored as zero ────────────────────────────────

def test_an_unknown_subject_count_is_a_gap_not_a_zero():
    a = risk.assess_risk(
        incident_type=incident.TYPE_DATA_EXPOSURE,
        data_categories=("Contact Data",), subject_total=None,
    )
    assert any("number of affected individuals is not known" in g for g in a.gaps)
    assert not any(f.code == "SCALE" for f in a.factors)


def test_an_estimated_count_is_a_softer_input_than_a_counted_one():
    counted = risk.assess_risk(
        incident_type=incident.TYPE_DATA_EXPOSURE, data_categories=("Contact Data",),
        subject_total=11_500, count_basis="counted",
    )
    estimated = risk.assess_risk(
        incident_type=incident.TYPE_DATA_EXPOSURE, data_categories=("Contact Data",),
        subject_total=11_500, count_basis="estimated",
    )
    assert counted.score == estimated.score, "an estimate should not change the score"
    counted_scale = next(f for f in counted.factors if f.code == "SCALE")
    estimated_scale = next(f for f in estimated.factors if f.code == "SCALE")
    assert counted_scale.evidenced and not estimated_scale.evidenced
    assert any("estimate, not a count" in g for g in estimated.gaps)


def test_unconfirmed_exfiltration_is_not_treated_as_no_exfiltration():
    none_known = risk.assess_risk(
        incident_type=incident.TYPE_DATA_EXPOSURE, data_categories=("Contact Data",),
        exfiltration=incident.UNKNOWN,
    )
    possible = risk.assess_risk(
        incident_type=incident.TYPE_DATA_EXPOSURE, data_categories=("Contact Data",),
        exfiltration=incident.POSSIBLE,
    )
    confirmed = risk.assess_risk(
        incident_type=incident.TYPE_DATA_EXPOSURE, data_categories=("Contact Data",),
        exfiltration=incident.CONFIRMED,
    )
    assert none_known.score < possible.score < confirmed.score
    assert any("whether data left" in g for g in none_known.gaps)


def test_the_most_sensitive_category_dominates_rather_than_the_count():
    """Ten contact fields are not more harmful than one set of credentials."""
    many_mild = risk.assess_risk(
        incident_type=incident.TYPE_DATA_EXPOSURE,
        data_categories=("Contact Data", "Online Identifier", "Behavioural Data",
                         "Professional / Social Profile"),
    )
    one_severe = risk.assess_risk(
        incident_type=incident.TYPE_DATA_EXPOSURE,
        data_categories=("Credential / Secret",),
    )
    assert one_severe.score >= many_mild.score


def test_an_unrecognised_category_still_counts_for_something():
    """An unknown category is not a harmless one."""
    a = risk.assess_risk(
        incident_type=incident.TYPE_DATA_EXPOSURE,
        data_categories=("Some Bespoke Customer Category",),
    )
    assert any(f.code == "DATA_SENSITIVITY" for f in a.factors)


def test_every_factor_explains_its_own_contribution():
    """§19: a reviewer must be able to disagree with one input rather than with an
    opaque number."""
    a = risk.assess_risk(
        incident_type=incident.TYPE_DATA_LEAKAGE,
        data_categories=("Financial Data", "Identity Data"),
        subject_total=5_000, count_basis="counted",
        exfiltration=incident.CONFIRMED, exposure_window=timedelta(days=9),
        third_party_involved=True,
    )
    assert a.factors
    for f in a.factors:
        assert f.code and f.label and f.detail
        assert isinstance(f.contribution, int)
    assert sum(f.contribution for f in a.factors) == a.score


def test_a_thin_assessment_says_so_in_its_reason():
    a = risk.assess_risk(incident_type=incident.TYPE_UNAUTHORIZED_ACCESS)
    assert "could not be established" in a.reason
    assert a.confidence in (incident.UNKNOWN, incident.POSSIBLE)


def test_a_well_evidenced_assessment_reaches_probable_but_never_confirmed():
    a = risk.assess_risk(
        incident_type=incident.TYPE_DATA_LEAKAGE,
        data_categories=("Financial Data",), subject_total=20_000, count_basis="counted",
        exfiltration=incident.CONFIRMED, unauthorized_access=incident.CONFIRMED,
        exposure_window=timedelta(days=3), personal_data_involved=incident.CONFIRMED,
    )
    assert a.confidence == incident.PROBABLE
    assert a.level in (incident.SEV_HIGH, incident.SEV_CRITICAL)


def test_assessability_tells_a_caller_when_there_is_nothing_to_assess():
    assert not risk.is_assessable((), None, incident.UNKNOWN)
    assert risk.is_assessable(("Contact Data",), None, incident.UNKNOWN)
    assert risk.is_assessable((), 100, incident.UNKNOWN)
    assert risk.is_assessable((), None, incident.POSSIBLE)


def test_scores_stay_inside_their_bands():
    for level, score in [
        (incident.SEV_LOW, 10), (incident.SEV_MEDIUM, 30),
        (incident.SEV_HIGH, 60), (incident.SEV_CRITICAL, 90),
    ]:
        assert risk._band(score) == level
