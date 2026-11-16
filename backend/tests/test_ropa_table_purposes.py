"""ROPA: which business purpose a database table name maps to (app/agents/ropa/rules/purpose_rules.py). Kept when the Purpose agent was removed: this is ROPA code."""
import pytest


def test_table_names_map_to_business_purposes():
    from app.agents.ropa.rules import purpose_rules as table_purpose

    assert table_purpose.match_table("customers").purpose == "Customer Account Management"
    assert table_purpose.match_table("payroll").purpose == "Payroll / Compensation"
    assert table_purpose.match_table("attendees").purpose == "Event Attendee Management"


def test_an_unrecognised_table_is_not_guessed():
    """None, so the caller can say Unknown. A purpose invented for a table is
    indistinguishable downstream from one that was established."""
    from app.agents.ropa.rules import purpose_rules as table_purpose

    assert table_purpose.match_table("xyz_internal_widget_cache") is None
    assert table_purpose.match_table("") is None


def test_a_narrow_rule_beats_a_broad_one():
    """customer_payments is a payment table, not a customer-account table.

    This failed on the first run: the name tokenises to {customer, payments} and the
    rules read in the singular, so the payment rule missed on the plural and the
    broader customer rule won -- exactly backwards.
    """
    from app.agents.ropa.rules import purpose_rules as table_purpose

    assert table_purpose.match_table("customer_payments").purpose == "Payment Processing"


@pytest.mark.parametrize("name", ["address_book", "status_codes", "currency_rates"])
def test_de_pluralisation_does_not_invent_matches(name):
    """Real stemming would fold "address" to "addres" and "status" to "statu",
    creating matches worse than the misses they fix."""
    from app.agents.ropa.rules import purpose_rules as table_purpose

    assert table_purpose.match_table(name) is None


def test_schema_and_column_qualified_names_resolve_to_the_table():
    from app.agents.ropa.rules import purpose_rules as table_purpose

    assert table_purpose.match_table("public.customers").purpose == "Customer Account Management"
    assert table_purpose.match_table("attendees.email").purpose == "Event Attendee Management"
