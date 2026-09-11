"""Personal-data and purpose rule tests.

The false-positive cases here are not hypothetical: every one was produced by an
actual run against a real database before the guards were added.

classify_column now always returns a Classification rather than None -- `None`
used to mean "operational", "blocked" and "no rule matched" at once, which is
how genuine personal data went missing. These tests assert on `.category` and
`.status` accordingly. See test_ropa_classifier_regression.py for the
contextual behaviour this contract enabled.
"""

import pytest

from app.agents.ropa.rules import personal_data_rules as pdr
from app.agents.ropa.rules import purpose_rules


@pytest.mark.parametrize(
    "column,expected_category",
    [
        ("email", pdr.CATEGORY_CONTACT),
        ("email_address", pdr.CATEGORY_CONTACT),
        ("customer_email", pdr.CATEGORY_CONTACT),
        ("phone_number", pdr.CATEGORY_CONTACT),
        ("first_name", pdr.CATEGORY_IDENTITY),
        ("date_of_birth", pdr.CATEGORY_IDENTITY),
        ("aadhaar_number", pdr.CATEGORY_GOVERNMENT_ID),
        ("passport", pdr.CATEGORY_GOVERNMENT_ID),
        ("ip_address", pdr.CATEGORY_ONLINE_ID),
        ("device_id", pdr.CATEGORY_ONLINE_ID),
        ("postal_code", pdr.CATEGORY_LOCATION),
        ("latitude", pdr.CATEGORY_LOCATION),
        ("ifsc", pdr.CATEGORY_FINANCIAL),
        ("salary", pdr.CATEGORY_FINANCIAL),
        ("designation", pdr.CATEGORY_EMPLOYMENT),
        ("password_hash", pdr.CATEGORY_CREDENTIAL),
        ("blood_group", pdr.CATEGORY_HEALTH),
    ],
)
def test_classifies_real_personal_data(column, expected_category):
    result = pdr.classify_column(column, "text", table_name="users")
    assert result.category == expected_category, f"{column} misclassified"
    assert result.status == pdr.CLASSIFIED


@pytest.mark.parametrize(
    "column",
    [
        # Observed false positives on a live database.
        "agent_name",       # was -> Identity Data
        "model_name",       # was -> Identity Data
        "service_name",     # was -> Identity Data
        "name_pattern",     # was -> Identity Data
        "token_count",      # was -> Credential / Secret
        # Other entity-qualified names that must not read as a person.
        "file_name",
        "table_name",
        "vendor_name",
        "product_name",
        "campaign_name",
        # Metrics.
        "num_records",
        "response_time_ms",
        "total_size",
        # Structural columns.
        "id",
        "created_at",
        "status",
    ],
)
def test_does_not_classify_non_personal_columns(column):
    result = pdr.classify_column(column, "text", table_name="jobs")
    assert not result.is_personal_data, f"{column} must not be classified as personal data"


def test_bare_name_requires_person_context():
    """`cookies.name` is a cookie's name; `customers.name` is a person's."""
    assert not pdr.classify_column("name", "text", table_name="cookies").is_personal_data
    assert pdr.classify_column("name", "text", table_name="customers").category == pdr.CATEGORY_IDENTITY


def test_unambiguous_token_survives_entity_qualifier():
    """"customer" is an entity qualifier, but an email is still an email."""
    assert pdr.classify_column("customer_email", "text").category == pdr.CATEGORY_CONTACT


def test_exact_match_outranks_token_match():
    exact = pdr.classify_column("email", "text", table_name="users")
    token = pdr.classify_column("user_email_address_alt", "text", table_name="users")
    assert exact.confidence >= token.confidence


def test_confidence_never_reaches_one():
    """A name-based rule is evidence, not proof."""
    for column in ("email", "aadhaar", "password"):
        result = pdr.classify_column(column, "text", table_name="users")
        assert result.confidence <= pdr.CONFIDENCE_CEILING < 1.0


@pytest.mark.parametrize(
    "table,purpose,subject",
    [
        ("customers", "Customer Account Management", "Customer"),
        ("employees", "Employee Administration", "Employee"),
        ("candidates", "Recruitment", "Candidate"),
        ("attendees", "Event Attendee Management", "Attendee"),
        ("event_registrations", "Event Attendee Management", "Attendee"),
        ("payments", "Payment Processing", "Customer"),
        ("newsletter_subscribers", "Marketing Communications", "Prospect"),
    ],
)
def test_purpose_rules_match(table, purpose, subject):
    match = purpose_rules.match_table(table)
    assert match is not None, f"{table} should match"
    assert match.purpose == purpose
    assert match.data_subject == subject


@pytest.mark.parametrize("table", ["agent_runs", "knowledge_chunks", "scan_diffs", "wibble_xyz"])
def test_purpose_never_invented_for_unknown_tables(table):
    """The prompt forbids inventing a purpose -- no match must mean no guess."""
    assert purpose_rules.match_table(table) is None
