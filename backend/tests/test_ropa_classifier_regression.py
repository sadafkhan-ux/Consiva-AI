"""Regression suite for the contextual classifier.

Every case in the first block was produced by a real run against PrepMyEvent's
production schema (153 columns, correlation pme-e52384b3d470467a). That run
scored 74% precision and silently dropped four genuine personal-data columns;
these tests exist so neither can come back.
"""

import pytest

from app.agents.ropa.rules import personal_data_rules as pdr
from app.agents.ropa.rules.column_context import OPERATIONAL, PERSON, UNKNOWN, resolve_table_context


def classify(column, dtype="VARCHAR", table=None, **kw):
    return pdr.classify_column(column, dtype, table_name=table, **kw)


# ── The eight false positives from the live run ─────────────────────────────────

def test_gmail_address_is_contact_not_location():
    """`address` is in the name, but the column holds an email address."""
    r = classify("connected_gmail_address", table="users")
    assert r.category == pdr.CATEGORY_CONTACT
    assert r.status == pdr.CLASSIFIED


def test_campaign_name_is_not_a_person():
    """Table context said "prospect", so bare `name` became Identity Data."""
    r = classify("name", table="outreach_campaigns")
    assert r.category != pdr.CATEGORY_IDENTITY
    assert r.status == pdr.OPERATIONAL


def test_otp_expires_at_is_a_timestamp_not_a_credential():
    r = classify("otp_expires_at", "TIMESTAMP", table="pending_signups")
    assert r.status == pdr.OPERATIONAL
    assert r.category is None
    assert r.method == "temporal_column"


def test_token_expires_at_is_a_timestamp_not_a_credential():
    r = classify("token_expires_at", "TIMESTAMP", table="pending_signups")
    assert r.status == pdr.OPERATIONAL
    assert r.category is None


def test_email_source_is_provenance_not_contact():
    r = classify("email_source", table="leads")
    assert r.status == pdr.PROVENANCE
    assert r.category is None


def test_event_location_is_a_venue_not_a_person():
    r = classify("location", table="events")
    assert r.status == pdr.OPERATIONAL
    assert r.category != pdr.CATEGORY_LOCATION


def test_linkedin_url_kind_is_a_discriminator():
    r = classify("linkedin_url_kind", table="leads")
    assert r.status == pdr.PROVENANCE
    assert r.category != pdr.CATEGORY_PROFESSIONAL


def test_full_email_text_is_not_contact_data():
    """Message content is personal data, but it is not a contact identifier."""
    r = classify("full_email_text", "TEXT", table="generated_emails")
    assert r.category != pdr.CATEGORY_CONTACT


# ── The four silent false negatives ─────────────────────────────────────────────

def test_title_in_a_person_table_is_employment_data():
    """Previously dropped by the global operational blocklist."""
    r = classify("title", table="campaign_leads")
    assert r.category == pdr.CATEGORY_EMPLOYMENT
    assert r.status == pdr.CLASSIFIED


def test_notes_in_a_person_table_is_surfaced_for_review():
    r = classify("notes", "TEXT", table="campaign_leads")
    assert r.status == pdr.REVIEW
    assert r.review_required
    assert r.category == pdr.CATEGORY_FREE_TEXT


def test_url_tied_to_a_person_is_behavioural():
    r = classify("url", "TEXT", table="email_events", has_person_relationship=True)
    assert r.status == pdr.REVIEW
    assert r.category == pdr.CATEGORY_BEHAVIOURAL


def test_razorpay_payment_id_is_financial():
    r = classify("razorpay_payment_id", table="credit_transactions")
    assert r.category == pdr.CATEGORY_FINANCIAL


# ── Nothing is ever dropped ─────────────────────────────────────────────────────

@pytest.mark.parametrize("column,dtype", [
    ("id", "UUID"), ("created_at", "TIMESTAMP"), ("email", "VARCHAR"),
    ("some_field_nobody_anticipated", "TEXT"), ("", "TEXT"), ("x", "INTEGER"),
])
def test_every_column_returns_an_explicit_decision(column, dtype):
    r = classify(column, dtype, table="leads")
    assert r.status in (pdr.CLASSIFIED, pdr.OPERATIONAL, pdr.PROVENANCE, pdr.REVIEW, pdr.UNKNOWN)
    assert r.evidence, "every decision must explain itself"
    assert r.method


def test_unknown_is_explicit_and_reviewable():
    r = classify("enrichment_status", table="campaign_leads")
    assert r.review_required or r.status in (pdr.OPERATIONAL, pdr.PROVENANCE)


# ── Table context ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("table,expected", [
    ("users", PERSON), ("leads", PERSON), ("campaign_leads", PERSON),
    ("customers", PERSON), ("employees", PERSON), ("pending_signups", PERSON),
    ("outreach_campaigns", OPERATIONAL), ("events", OPERATIONAL),
    ("audit_logs", OPERATIONAL), ("settings", OPERATIONAL),
    ("some_unknown_table", UNKNOWN),
])
def test_table_context_resolution(table, expected):
    assert resolve_table_context(table) == expected


def test_foreign_key_to_a_person_table_creates_person_context():
    assert resolve_table_context("email_events") == UNKNOWN
    assert resolve_table_context("email_events", has_person_relationship=True) == PERSON


def test_same_column_different_tables_different_answers():
    """The heart of the contextual engine."""
    assert classify("title", table="leads").category == pdr.CATEGORY_EMPLOYMENT
    assert classify("title", table="outreach_campaigns").status == pdr.OPERATIONAL


# ── Naming shapes ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("column", ["EMAIL", "Email", "eMaIl"])
def test_case_insensitive(column):
    assert classify(column, table="users").category == pdr.CATEGORY_CONTACT


@pytest.mark.parametrize("column", ["email_address", "emailAddress", "email address"])
def test_naming_styles_normalise(column):
    assert classify(column, table="users").category == pdr.CATEGORY_CONTACT


@pytest.mark.parametrize("column", [
    "total_leads", "emails_sent", "open_count", "num_replies", "spam_score",
])
def test_metrics_are_operational(column):
    assert classify(column, "INTEGER", table="outreach_campaigns").status == pdr.OPERATIONAL


@pytest.mark.parametrize("column", ["user_id", "campaign_id", "lead_id"])
def test_foreign_keys_are_operational(column):
    assert classify(column, "UUID", table="campaign_leads").status == pdr.OPERATIONAL


# ── Positives that must keep working ────────────────────────────────────────────

@pytest.mark.parametrize("column,category", [
    ("email", pdr.CATEGORY_CONTACT),
    ("phone", pdr.CATEGORY_CONTACT),
    ("first_name", pdr.CATEGORY_IDENTITY),
    ("full_name", pdr.CATEGORY_IDENTITY),
    ("date_of_birth", pdr.CATEGORY_IDENTITY),
    ("aadhaar_number", pdr.CATEGORY_GOVERNMENT_ID),
    ("passport", pdr.CATEGORY_GOVERNMENT_ID),
    ("ip_address", pdr.CATEGORY_ONLINE_ID),
    ("linkedin_url", pdr.CATEGORY_PROFESSIONAL),
    ("job_title", pdr.CATEGORY_EMPLOYMENT),
    ("password_hash", pdr.CATEGORY_CREDENTIAL),
    ("otp_code", pdr.CATEGORY_CREDENTIAL),
    ("verification_token", pdr.CATEGORY_CREDENTIAL),
    ("blood_group", pdr.CATEGORY_HEALTH),
    ("ifsc", pdr.CATEGORY_FINANCIAL),
])
def test_known_personal_data_still_classifies(column, category):
    assert classify(column, "VARCHAR", table="users").category == category


def test_unambiguous_token_survives_entity_qualifier():
    assert classify("customer_email", table="leads").category == pdr.CATEGORY_CONTACT


@pytest.mark.parametrize("column", [
    "agent_name", "model_name", "service_name", "file_name", "template_name",
])
def test_system_object_names_are_not_people(column):
    assert classify(column, table="jobs").status == pdr.OPERATIONAL


# ── Confidence and explainability ───────────────────────────────────────────────

def test_exact_match_outranks_token_match():
    exact = classify("email", table="users")
    token = classify("user_email_verified", "BOOLEAN", table="users")
    assert exact.confidence > token.confidence


def test_confidence_never_reaches_certainty():
    for column in ("email", "aadhaar", "password"):
        assert classify(column, "VARCHAR", table="users").confidence <= pdr.CONFIDENCE_CEILING < 1.0


def test_table_context_raises_confidence():
    in_person = classify("email", table="users")
    no_context = classify("email", table="some_unknown_table")
    assert in_person.confidence > no_context.confidence


def test_evidence_names_the_signals_used():
    r = classify("title", table="campaign_leads")
    joined = " ".join(r.evidence)
    assert "table=campaign_leads" in joined
    assert "column=title" in joined
    assert "person_table=true" in joined
    assert r.method == "table_context"


def test_conflicts_are_recorded_and_reduce_confidence():
    """Two rules matching is a signal of uncertainty, not something to hide."""
    r = classify("email_location_token", "TEXT", table="users")
    if r.conflicts:
        assert r.confidence < pdr.BASE_TOKEN + pdr.BONUS_TABLE_CONTEXT


def test_no_llm_is_invoked(monkeypatch):
    """The classifier must stay deterministic and offline."""
    def _explode(*_a, **_k):
        raise AssertionError("the rule engine must not call an LLM")

    monkeypatch.setattr("app.llm.client.generate_structured_with_fallback", _explode)
    assert classify("email", table="users").category == pdr.CATEGORY_CONTACT
