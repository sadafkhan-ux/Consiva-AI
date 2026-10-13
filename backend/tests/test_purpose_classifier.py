"""Purpose Classifier — Phase 1.

The agent's whole value rests on one property: it must not claim a purpose mismatch
that is really an artefact of comparing two different vocabularies. Most of this file
is about that.

Context these tests encode, so they are not weakened later by someone who lacks it:

  * Consiva speaks TWO purpose vocabularies. `purpose_taxonomy` holds four codes
    (analytics, marketing, functional, other) and the Consent Agent labels trackers and
    cookies with them. A record of processing declares a BUSINESS purpose in prose
    ("Customer Account Management"). These are different kinds of statement.
  * "Customer Account Management" != "functional" as strings, but it is not a mismatch
    either. Treating string inequality as disagreement would report a finding on almost
    every item.
  * This codebase has already shipped a confident, specific, false finding -- a keyword
    match against a user's forum comment produced a high-priority finding about a
    consent banner that did not exist. `undetermined` exists so that cannot recur here.
"""

import uuid

import pytest

from app.agents.purpose.rules import reconciliation
from app.agents.purpose.services import comparison_service, retention_service
from app.agents.purpose.services.declared_service import DeclaredPurpose
from app.agents.purpose.services.observed_service import ObservedPurpose


def _observed(**over) -> ObservedPurpose:
    base = dict(
        subject_type="tracker",
        subject_ref=str(uuid.uuid4()),
        subject_label="ads.example.com",
        purpose="marketing",
        consent_states=["post_accept"],
        evidence_refs=["t1"],
    )
    base.update(over)
    return ObservedPurpose(**base)


def _declared(purpose="Marketing Communications") -> DeclaredPurpose:
    return DeclaredPurpose(purpose=purpose, source="ropa_record",
                           evidence_ref=str(uuid.uuid4()), confidence=0.9)


# ── Vocabulary reconciliation: the accuracy foundation ──────────────────────────

@pytest.mark.parametrize("business,code", [
    ("Marketing Communications", "marketing"),
    ("Customer Account Management", "functional"),
    ("Payment Processing", "functional"),
    ("Support / Ticketing", "functional"),
])
def test_known_business_purposes_map_into_the_taxonomy(business, code):
    mapping = reconciliation.to_taxonomy(business)
    assert mapping.code == code
    assert mapping.comparable


@pytest.mark.parametrize("code", ["analytics", "marketing", "functional", "other"])
def test_taxonomy_codes_map_to_themselves(code):
    mapping = reconciliation.to_taxonomy(code)
    assert mapping.code == code
    assert mapping.confidence == 1.0


@pytest.mark.parametrize("unmappable", [
    "Recruitment",
    "Vendor / Supplier Management",
    "Payroll / Compensation",
    "Event Attendee Management",
])
def test_business_purposes_with_no_honest_equivalent_are_not_forced(unmappable):
    """These have no taxonomy equivalent. Mapping them to 'other' would make every
    comparison against a genuinely-other tracker look aligned."""
    mapping = reconciliation.to_taxonomy(unmappable)
    assert mapping.code is None
    assert not mapping.comparable


def test_nothing_unmapped_is_swept_into_other():
    """'other' means examined and does not fit -- not a dumping ground."""
    assert reconciliation.to_taxonomy("Some Bespoke Internal Process").code != "other"


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_an_absent_purpose_maps_to_nothing(empty):
    assert reconciliation.to_taxonomy(empty).code is None


def test_free_text_hints_are_low_confidence_and_not_comparable():
    """A substring match is how this codebase previously produced a false finding.
    It may inform a mapping; it must not carry a mismatch claim on its own."""
    mapping = reconciliation.to_taxonomy("quarterly advertising spend review")
    assert mapping.code == "marketing"
    assert mapping.confidence < 0.7
    assert not mapping.comparable


def test_mapping_comparable_boundary_is_inclusive_of_0_7():
    """0.7 is the stated floor. The check must be `>=`, not `>`, or a mapping that
    rests on a known phrase at exactly the floor would be wrongly excluded."""
    assert reconciliation.Mapping("marketing", 0.7, "boundary").comparable is True


def test_mapping_just_below_0_7_is_not_comparable():
    assert reconciliation.Mapping("marketing", 0.69, "just under").comparable is False


def test_a_multi_purpose_string_does_not_get_silently_split():
    """"Marketing and analytics" carries a substring hint for both codes. The mapping
    must not quietly pick one -- it is a hint-level match whichever hint wins, so it
    stays below the comparable floor either way."""
    mapping = reconciliation.to_taxonomy("marketing and analytics")
    assert not mapping.comparable


def test_a_multi_purpose_declaration_cannot_produce_a_mismatch():
    alignment, _, _ = reconciliation.compare("marketing and analytics", "analytics")
    assert alignment == reconciliation.UNDETERMINED


@pytest.mark.parametrize("typo", ["Markting Communications", "Analtyics", "Advertizing"])
def test_a_typo_in_a_known_business_purpose_is_not_guessed(typo):
    """A near-miss on a known phrase gets no special-cased fuzzy match -- it is just
    another unmapped string, exactly like a purpose nobody has ever written down."""
    mapping = reconciliation.to_taxonomy(typo)
    assert not mapping.comparable


# ── Comparison: mismatch only when both sides are solid ─────────────────────────

def test_the_same_purpose_in_two_vocabularies_is_aligned_not_a_mismatch():
    """The single most important assertion here. 'Marketing Communications' and
    'marketing' are the same statement in different vocabularies."""
    alignment, _, _ = reconciliation.compare("Marketing Communications", "marketing")
    assert alignment == "aligned"


def test_identical_purposes_agree_even_outside_the_taxonomy():
    """Found live. A table declared "Event Attendee Management" and observed as
    "Event Attendee Management" came back undetermined, because that purpose has no
    taxonomy equivalent -- the comparison was routing an exact match through a
    translator it did not need."""
    alignment, confidence, _ = reconciliation.compare(
        "Event Attendee Management", "Event Attendee Management"
    )
    assert alignment == "aligned"
    assert confidence == 1.0


def test_exact_matching_ignores_case_and_surrounding_space():
    alignment, _, _ = reconciliation.compare("  Recruitment ", "recruitment")
    assert alignment == "aligned"


def test_a_genuine_disagreement_is_a_mismatch():
    alignment, confidence, reason = reconciliation.compare("Customer Account Management", "marketing")
    assert alignment == "mismatch"
    assert confidence > 0
    assert "functional" in reason and "marketing" in reason


@pytest.mark.parametrize("declared,observed", [
    ("Recruitment", "marketing"),        # declared unmappable
    ("Marketing Communications", None),  # observed missing
    (None, "analytics"),                 # declared missing
    (None, None),                        # neither
])
def test_an_unmappable_side_yields_undetermined_never_a_mismatch(declared, observed):
    alignment, _, _ = reconciliation.compare(declared, observed)
    assert alignment == reconciliation.UNDETERMINED


def test_comparison_confidence_is_bounded_by_the_weaker_side():
    """A comparison is only as sound as its shakier half."""
    _, confidence, _ = reconciliation.compare("Marketing Communications", "marketing")
    assert confidence <= 0.9


# ── The finding this agent exists to produce ────────────────────────────────────

def test_marketing_firing_before_consent_is_a_high_severity_finding():
    """Neither half is new -- trackers.category and trackers.consent_states have both
    been stored for a long time. Nothing in the platform compared them."""
    item = _observed(purpose="marketing", consent_states=["pre_consent"])
    result = comparison_service.assess(item, {}, consent_obtained=None)
    assert result.finding is not None
    assert result.finding.finding_type == comparison_service.WITHOUT_CONSENT
    assert result.finding.severity == "high"


def test_the_same_applies_after_the_visitor_rejects():
    item = _observed(purpose="analytics", consent_states=["post_reject"])
    result = comparison_service.assess(item, {}, consent_obtained=True)
    assert result.finding.finding_type == comparison_service.WITHOUT_CONSENT


def test_functional_items_are_not_flagged_for_firing_early():
    """Strictly necessary processing is the standard carve-out. Flagging every session
    cookie would bury the findings that matter."""
    item = _observed(purpose="functional", consent_states=["pre_consent"])
    result = comparison_service.assess(item, {}, consent_obtained=None)
    assert result.finding is None or result.finding.finding_type != comparison_service.WITHOUT_CONSENT


def test_marketing_after_a_confirmed_accept_is_not_flagged():
    item = _observed(purpose="marketing", consent_states=["post_accept"])
    result = comparison_service.assess(item, {}, consent_obtained=True)
    assert result.finding is None or result.finding.finding_type != comparison_service.WITHOUT_CONSENT


def test_a_consent_violation_outranks_a_mismatch_on_the_same_item():
    """Both facts are true; the stronger, more actionable one is reported."""
    item = _observed(purpose="marketing", consent_states=["pre_consent"])
    result = comparison_service.assess(
        item, {"ads.example.com": _declared("Customer Account Management")}, consent_obtained=None
    )
    assert result.finding.finding_type == comparison_service.WITHOUT_CONSENT


def test_every_high_severity_finding_requires_review():
    """Forced by the finding, not by the caller -- the guarantee must not depend on
    anyone remembering a flag."""
    item = _observed(purpose="marketing", consent_states=["pre_consent"])
    assert comparison_service.assess(item, {}, None).finding.review_required is True


def test_a_subject_seen_in_both_pre_and_post_consent_states_still_violates():
    """Occurrences grouped across passes can carry more than one state. The presence
    of ANY unconsented state is enough -- it must not be diluted by a later, separate
    accepted state on the same merged entry."""
    item = _observed(purpose="marketing", consent_states=["pre_consent", "post_accept"])
    result = comparison_service.assess(item, {}, consent_obtained=True)
    assert result.finding.finding_type == comparison_service.WITHOUT_CONSENT


def test_a_genuine_mismatch_with_proper_consent_is_reported_as_mismatch_not_a_violation():
    """The severity ordering the other way around: with consent fine, a real
    declared/observed disagreement surfaces as the mismatch it is, never inflated
    into a consent violation that never happened."""
    item = _observed(purpose="marketing", consent_states=["post_accept"])
    result = comparison_service.assess(
        item, {"ads.example.com": _declared("Customer Account Management")},
        consent_obtained=True,
    )
    assert result.alignment == "mismatch"
    assert result.finding.finding_type == comparison_service.MISMATCH
    assert result.finding.severity == "medium"


# ── Nothing is invented ─────────────────────────────────────────────────────────

def test_no_declared_purpose_means_no_mismatch_is_claimed():
    item = _observed(purpose="marketing", consent_states=["post_accept"])
    result = comparison_service.assess(item, {}, consent_obtained=True)
    assert result.alignment == reconciliation.UNDETERMINED
    assert result.declared_purpose is None
    assert result.finding is None or result.finding.finding_type != comparison_service.MISMATCH


def test_an_absent_declaration_is_reported_as_a_documentation_gap_not_misuse():
    item = _observed(purpose="analytics", consent_states=["post_accept"])
    result = comparison_service.assess(item, {}, consent_obtained=True)
    assert result.finding.finding_type == comparison_service.UNDECLARED
    assert result.finding.severity == "low"
    assert "not evidence that the use is improper" in result.finding.description


def test_declarations_are_matched_exactly_never_fuzzily():
    """Fuzzy-matching a vendor host to an activity name would manufacture the very
    declaration the comparison then judges."""
    item = _observed(subject_label="ads.example.com")
    result = comparison_service.assess(item, {"ads.example.co": _declared()}, True)
    assert result.declared_purpose is None


def test_an_observed_purpose_with_no_category_is_undetermined_not_mismatch():
    """The observed side can be unclassified (a null `category` from the Consent
    Agent) while a declaration still exists for that subject. That is a missing
    half, not a disagreement, and must not be reported as one."""
    item = _observed(purpose=None, consent_states=["post_accept"])
    result = comparison_service.assess(
        item, {"ads.example.com": _declared("Customer Account Management")},
        consent_obtained=True,
    )
    assert result.alignment == reconciliation.UNDETERMINED
    assert result.finding is None or result.finding.finding_type != comparison_service.MISMATCH


def test_a_declared_purpose_with_no_matching_observed_item_produces_no_assessment():
    """A declaration for a table nothing was ever observed about is not an absence
    this agent can report on through this path -- there is no observed row to attach
    a finding to, so none is manufactured for it. The structured path assesses only
    what it actually read, never every declared key."""
    from app.agents.purpose.connectors.structured import Column, StructuredSchema, Table
    from app.agents.purpose.services import structured_service

    schema = StructuredSchema(source_name="db", tables=[
        Table("public", "customers", [Column("email", "text")]),
    ])
    declared = structured_service.index_declared_by_table({
        "customers.email": _declared("Customer Account Management"),
        "ghost_table.email": _declared("Marketing Communications"),
    })
    observed = structured_service.observe(schema)
    assert [o.subject_label for o in observed] == ["customers"]
    results = [comparison_service.assess(o, declared, None) for o in observed]
    assert len(results) == 1, "a declaration with nothing observed must not synthesise a row"


# ── No fabrication: a violation claim always traces to an observed state ────────

def test_a_third_party_service_roll_up_can_never_trigger_a_consent_violation():
    """A `third_party_service` row carries no consent state of its own -- observed_
    service leaves it empty rather than inheriting one from a member tracker. Even
    though its purpose may require consent, there is no timing evidence to accuse it
    with, so no violation can be raised for it."""
    item = _observed(subject_type="third_party_service", purpose="marketing", consent_states=[])
    result = comparison_service.assess(item, {}, consent_obtained=None)
    assert result.finding is None or result.finding.finding_type != comparison_service.WITHOUT_CONSENT


@pytest.mark.parametrize("purpose", ["marketing", "analytics"])
def test_empty_consent_states_never_fabricates_a_violation(purpose):
    """`fired_without_consent` is computed purely from the states list; an empty list
    contains nothing that could be `pre_consent` or `post_reject`, so the property is
    false by construction. There is no code path from "no evidence" to "violation"."""
    item = _observed(purpose=purpose, consent_states=[])
    assert item.fired_without_consent is False
    result = comparison_service.assess(item, {}, consent_obtained=None)
    assert result.finding is None or result.finding.finding_type != comparison_service.WITHOUT_CONSENT


# ── Retention: a prompt, never a verdict ────────────────────────────────────────

def test_a_long_lived_marketing_cookie_is_flagged_for_review():
    item = _observed(subject_type="cookie", purpose="marketing", expiry_days=730)
    status, note = retention_service.evaluate(item)
    assert status == retention_service.REVIEW_REQUIRED
    assert "not a finding that it is excessive" in note


def test_a_short_lived_cookie_is_within_expectation():
    item = _observed(subject_type="cookie", purpose="marketing", expiry_days=30)
    assert retention_service.evaluate(item)[0] == retention_service.WITHIN_EXPECTATION


def test_a_session_cookie_needs_no_review():
    item = _observed(subject_type="cookie", purpose="marketing", expiry_days=0)
    assert retention_service.evaluate(item)[0] == retention_service.WITHIN_EXPECTATION


def test_functional_cookies_have_no_purpose_derived_expectation():
    """A session or security cookie has no retention expectation this module could
    assert, so it does not pretend to one."""
    item = _observed(subject_type="cookie", purpose="functional", expiry_days=3650)
    assert retention_service.evaluate(item)[0] == retention_service.NOT_EVALUATED


def test_a_tracker_has_no_expiry_to_evaluate():
    """A tracker is a network request. Borrowing a lifetime from its cookies would
    attribute a property it does not have."""
    assert retention_service.evaluate(_observed(subject_type="tracker"))[0] == \
        retention_service.NOT_EVALUATED


def test_an_unparseable_expiry_is_unknown_not_a_finding():
    item = _observed(subject_type="cookie", purpose="marketing", expiry_days=None)
    status, note = retention_service.evaluate(item)
    assert status == retention_service.UNKNOWN
    assert "gap in the evidence" in note


def test_retention_at_exactly_the_395_day_threshold_is_within_expectation():
    """The check is strictly `>` the threshold, so the boundary day itself is still
    within expectation, not already a review trigger."""
    item = _observed(subject_type="cookie", purpose="analytics", expiry_days=395)
    assert retention_service.evaluate(item)[0] == retention_service.WITHIN_EXPECTATION


def test_retention_one_day_past_the_threshold_is_review_required():
    item = _observed(subject_type="cookie", purpose="analytics", expiry_days=396)
    assert retention_service.evaluate(item)[0] == retention_service.REVIEW_REQUIRED


# ── Grouping: one finding per thing to act on ───────────────────────────────────

def test_identical_hosts_collapse_into_one_observation():
    """Measured on a real scan before this existed: 48 findings covering 13 distinct
    subjects, fonts.gstatic.com alone accounting for 18. A reviewer facing that stops
    reading."""
    from app.agents.purpose.services.observed_service import _group

    rows = [_observed(subject_ref=str(uuid.uuid4()), evidence_refs=[f"t{i}"]) for i in range(18)]
    grouped = _group(rows)
    assert len(grouped) == 1
    assert grouped[0].occurrences == 18
    assert len(grouped[0].evidence_refs) == 18, "every underlying row must stay citable"


def test_the_same_host_in_different_consent_states_is_not_merged():
    """A host seen only after Accept and one seen before consent are different
    compliance facts. Merging them would erase the finding."""
    from app.agents.purpose.services.observed_service import _group

    grouped = _group([
        _observed(consent_states=["pre_consent"]),
        _observed(consent_states=["post_accept"]),
    ])
    assert len(grouped) == 2


def test_a_group_keeps_the_longest_lived_expiry():
    """If any copy of a cookie persists for two years, the group persists for two
    years -- taking the first would understate retention."""
    from app.agents.purpose.services.observed_service import _group

    grouped = _group([
        _observed(subject_type="cookie", expiry_days=30),
        _observed(subject_type="cookie", expiry_days=730),
    ])
    assert len(grouped) == 1
    assert grouped[0].expiry_days == 730


def test_a_grouped_finding_says_how_many_requests_it_covers():
    item = _observed(purpose="marketing", consent_states=["pre_consent"])
    item.occurrences = 12
    finding = comparison_service.assess(item, {}, None).finding
    assert "12 requests" in finding.description


def test_conflicting_categories_for_the_same_subject_are_kept_separate_not_merged():
    """Two trackers sharing a host but genuinely disagreeing about category (one
    analytics, one marketing) are a real conflict in the evidence, not noise to
    collapse. Purpose is part of the grouping key, so both survive as distinct
    entries rather than one silently winning."""
    from app.agents.purpose.services.observed_service import _group

    grouped = _group([
        _observed(purpose="analytics", consent_states=["post_accept"]),
        _observed(purpose="marketing", consent_states=["post_accept"]),
    ])
    assert len(grouped) == 2
    assert {g.purpose for g in grouped} == {"analytics", "marketing"}


def test_each_side_of_a_conflicting_pair_is_judged_on_its_own_merits():
    """Grouping keeps both rows distinct; comparison does not pick a winner between
    them either -- each is assessed independently against the same declaration, so
    one can align while the other mismatches."""
    declared = {"ads.example.com": _declared("Marketing Communications")}
    analytics_result = comparison_service.assess(
        _observed(purpose="analytics", consent_states=["post_accept"]), declared, True
    )
    marketing_result = comparison_service.assess(
        _observed(purpose="marketing", consent_states=["post_accept"]), declared, True
    )
    assert analytics_result.alignment == "mismatch"
    assert marketing_result.alignment == "aligned"


# ── Structured sources: the input that makes comparison resolvable ──────────────

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


def test_only_tables_with_personal_data_are_assessed():
    """A hundred undetermined rows about lookup tables would bury the real ones."""
    from app.agents.purpose.connectors.structured import Column, StructuredSchema, Table
    from app.agents.purpose.services import structured_service

    schema = StructuredSchema(source_name="db", tables=[
        Table("public", "customers", [Column("email", "text"), Column("id", "uuid")]),
        Table("public", "currency_codes", [Column("code", "text"), Column("rate", "numeric")]),
    ])
    observed = structured_service.observe(schema)
    assert [o.subject_label for o in observed] == ["customers"]
    assert observed[0].evidence_refs == ["customers.email"]


def test_a_table_carries_no_consent_state():
    """Consent is a property of a browser interaction. A database table cannot
    evidence one, so no consent finding may be raised from this source."""
    from app.agents.purpose.connectors.structured import Column, StructuredSchema, Table
    from app.agents.purpose.services import structured_service

    schema = StructuredSchema(source_name="db", tables=[
        Table("public", "subscribers", [Column("email", "text")]),
    ])
    observed = structured_service.observe(schema)[0]
    assert observed.consent_states == []
    assert comparison_service.assess(observed, {}, None).finding.finding_type != (
        comparison_service.WITHOUT_CONSENT)


def test_declared_purposes_are_rekeyed_so_tables_can_match_them():
    """Declarations name `attendees.email`; observations name `attendees`. Without
    this they never meet and every comparison is undetermined for a reason that has
    nothing to do with the data."""
    from app.agents.purpose.services import structured_service

    declared = {"attendees.email": _declared("Event Attendee Management")}
    extended = structured_service.index_declared_by_table(declared)
    assert "attendees" in extended
    assert extended["attendees"].purpose == "Event Attendee Management"


def test_rekeying_does_not_let_one_declaration_overwrite_another():
    """Two declarations disagreeing about one table is a real conflict; silently
    overwriting would hide it behind whichever row was read last."""
    from app.agents.purpose.services import structured_service

    declared = {
        "attendees.email": _declared("Event Attendee Management"),
        "attendees.phone": _declared("Marketing Communications"),
    }
    extended = structured_service.index_declared_by_table(declared)
    assert extended["attendees"].purpose == "Event Attendee Management"


def test_a_table_mismatching_its_declaration_is_found():
    """The end-to-end point of the structured path."""
    from app.agents.purpose.connectors.structured import Column, StructuredSchema, Table
    from app.agents.purpose.services import structured_service

    schema = StructuredSchema(source_name="db", tables=[
        Table("public", "subscribers", [Column("email", "text")]),
    ])
    observed = structured_service.observe(schema)[0]
    declared = structured_service.index_declared_by_table(
        {"subscribers.email": _declared("Customer Account Management")}
    )
    result = comparison_service.assess(observed, declared, None)
    assert result.alignment == "mismatch"
    assert result.finding.finding_type == comparison_service.MISMATCH


def test_a_schema_qualified_table_still_finds_a_bare_declaration():
    """Found live: declarations name `customers`, a postgres read names
    `demo_customer.customers`. Same table, blocked by the schema qualifier."""
    from app.agents.purpose.connectors.structured import Column, StructuredSchema, Table
    from app.agents.purpose.services import structured_service

    schema = StructuredSchema(source_name="db", tables=[
        Table("demo_customer", "customers", [Column("email", "text")]),
    ])
    observed = structured_service.observe(schema)[0]
    assert observed.subject_label == "demo_customer.customers"
    assert observed.lookup_aliases == ["customers"]

    declared = structured_service.index_declared_by_table(
        {"customers.email": _declared("Customer Account Management")}
    )
    assert comparison_service.assess(observed, declared, None).alignment == "aligned"


def test_an_ambiguous_bare_name_gets_no_alias():
    """Two schemas both holding `customers` makes a bare declaration ambiguous.
    Attributing it to both would put a stated purpose on a table it was never about."""
    from app.agents.purpose.connectors.structured import Column, StructuredSchema, Table
    from app.agents.purpose.services import structured_service

    schema = StructuredSchema(source_name="db", tables=[
        Table("eu", "customers", [Column("email", "text")]),
        Table("us", "customers", [Column("email", "text")]),
    ])
    for observed in structured_service.observe(schema):
        assert observed.lookup_aliases == []


def test_index_declared_by_table_normalizes_case_and_whitespace_in_the_key():
    """Rekeying must not depend on the caller having already normalised its input --
    a declaration key read with stray case or surrounding whitespace still produces
    a usable, lowercased table key."""
    from app.agents.purpose.services import structured_service

    declared = {"  Attendees.Email  ": _declared("Event Attendee Management")}
    extended = structured_service.index_declared_by_table(declared)
    assert "attendees" in extended
    assert extended["attendees"].purpose == "Event Attendee Management"


def test_lookup_matches_a_declared_purpose_regardless_of_observed_case():
    """The observed side's label case (as read from a schema) must not block a match
    against a declaration recorded in a different case."""
    item = _observed(subject_label="Ads.Example.COM")
    result = comparison_service.assess(
        item, {"ads.example.com": _declared("Marketing Communications")}, True
    )
    assert result.declared_purpose == "Marketing Communications"


# ── The connector reads shape, never contents ───────────────────────────────────

def test_csv_reads_only_the_header():
    from app.agents.purpose.connectors import structured

    schema = structured.read_csv(
        "email,full_name,phone\nalice@example.test,Alice,555-0100\n",
        source_name="export.csv",
    )
    assert [c.name for c in schema.tables[0].columns] == ["email", "full_name", "phone"]
    blob = str(schema)
    assert "alice@example.test" not in blob, "row data must never enter the schema"


def test_the_csv_table_name_drops_the_file_extension():
    from app.agents.purpose.connectors import structured

    schema = structured.read_csv("email\n", source_name="customers.csv")
    assert schema.tables[0].name == "customers"


@pytest.mark.parametrize("bad,msg", [("", "empty"), ("\n\n", "empty")])
def test_an_unusable_csv_is_refused_clearly(bad, msg):
    from app.agents.purpose.connectors import structured

    with pytest.raises(structured.ConnectorError) as exc:
        structured.read_csv(bad, source_name="x.csv")
    assert msg in str(exc.value).lower()


def test_connector_errors_never_leak_credentials():
    """An error string ends up on a run record a user can read."""
    from app.agents.purpose.connectors.structured import _safe

    leaked = "connection to postgresql://admin:hunter2@db.internal:5432/prod failed"
    assert "hunter2" not in _safe(leaked)
    assert "admin" not in _safe(leaked)
    assert "db.internal" in _safe(leaked), "the host is diagnostic and should survive"


# ── SECURITY REGRESSION: the Postgres connector has no SSRF/internal-address guard ──
#
# `rest.read_openapi` rejects a caller-supplied URL that resolves to a private,
# loopback, link-local or cloud-metadata address BEFORE making any request --
# see test_an_internal_address_is_refused_before_any_request_is_made above, which
# pins exactly that contract for the REST connector.
#
# `read_postgres` takes an equally caller-supplied `dsn` (AssessSourceRequest.dsn,
# routes/purpose.py) and applies NO equivalent check: it is handed straight to
# asyncpg.connect(). Since this backend runs inside a Docker Compose network where
# internal service hostnames resolve (the app's own `db` container, `backend`,
# `redis`, etc. -- see docker-compose.prod.yml's `consiva` network) and the
# connector's error messages are persisted on the run record and readable by the
# requesting org via GET /api/v1/purpose/assessments/{run_id} (purpose_run_service.py
# `_mark_failed` stores `str(exc)[:500]` on `run.error`), an authenticated member of
# any org can use this "schema discovery" feature to make the worker open TCP
# connections to arbitrary internal hosts/ports and read back whether the connection
# was refused, timed out, or spoke a non-Postgres protocol -- a working blind-SSRF /
# internal network reconnaissance primitive. This is the asymmetry this test pins.
#
# This test encodes the SECURE behaviour (reject before connecting, like the REST
# connector) and is expected to FAIL until `read_postgres` gains the same
# `assert_safe_url`-style guard `rest.read_openapi` already has. It must keep failing
# honestly rather than being loosened -- see the module docstring's own warning about
# findings that silently stop meaning anything.
@pytest.mark.asyncio
async def test_postgres_connector_blocks_internal_addresses(monkeypatch):
    from app.agents.purpose.connectors import structured

    attempted_connect = {"called": False}

    async def _fake_connect(dsn, timeout=15):
        attempted_connect["called"] = True
        raise OSError("connection refused -- test stub, no real network call was made")

    monkeypatch.setattr(structured.asyncpg, "connect", _fake_connect)

    with pytest.raises(structured.ConnectorError):
        await structured.read_postgres(
            "postgresql://user:pass@127.0.0.1:5432/internal", source_name="ssrf-probe"
        )

    assert not attempted_connect["called"], (
        "read_postgres dialled a loopback address instead of refusing it before any "
        "connection attempt, the same way rest.read_openapi refuses an internal URL "
        "before calling httpx. There is currently no host/IP validation on `dsn` at "
        "all -- this test documents that gap and will keep failing until one is added."
    )


# ── Summary ─────────────────────────────────────────────────────────────────────

def test_the_summary_counts_undetermined_separately_from_aligned():
    """Folding undetermined into aligned would make a run that could compare nothing
    look like a clean bill of health."""
    results = [
        comparison_service.assess(_observed(purpose="marketing"),
                                  {"ads.example.com": _declared()}, True),
        comparison_service.assess(_observed(purpose="marketing"), {}, True),
    ]
    summary = comparison_service.summarise(results)
    assert summary["aligned"] == 1
    assert summary["undetermined"] == 1
    assert summary["total"] == 2


# ── The REST connector ──────────────────────────────────────────────────────────
#
# The third input mode. It reads an OpenAPI DOCUMENT rather than calling the API,
# which is the whole design: a response body is the personal data this agent exists to
# ask questions about, and fetching it to decide whether it is held properly is a trade
# the agent must not make.

import json as _json

import pytest as _pytest

from app.agents.purpose.connectors import rest
from app.agents.purpose.connectors.structured import ConnectorError

_SPEC = {
    "openapi": "3.0.0",
    "components": {"schemas": {
        "Customer": {
            "type": "object", "required": ["id"],
            "properties": {"id": {"type": "string"}, "email": {"type": "string"},
                           "full_name": {"type": "string"},
                           "address": {"$ref": "#/components/schemas/Address"}},
        },
        "Address": {"type": "object", "properties": {"postcode": {"type": "string"}}},
        "Order": {"type": "object",
                  "properties": {"id": {"type": "string"}, "card_last4": {"type": "string"},
                                 "items": {"type": "array", "items": {"type": "string"}}}},
        "Status": {"type": "string", "enum": ["open", "closed"]},
    }},
}


def test_an_openapi_schema_becomes_a_table_and_its_properties_become_columns():
    """Everything downstream -- the personal-data heuristic, the purpose rules, the
    comparison layer -- must work on a REST source without knowing it is one."""
    schema = rest.parse_openapi(_json.dumps(_SPEC), source_name="billing-api")
    names = {t.name for t in schema.tables}
    assert names == {"Customer", "Address", "Order"}
    customer = next(t for t in schema.tables if t.name == "Customer")
    assert {c.name for c in customer.columns} == {"id", "email", "full_name", "address"}


def test_a_schema_with_no_properties_is_skipped_not_recorded_empty():
    """`Status` is an enum. Recording it as a table with no columns would read as
    "we looked and found nothing personal" when no field was ever examined."""
    schema = rest.parse_openapi(_json.dumps(_SPEC), source_name="billing-api")
    assert "Status" not in {t.name for t in schema.tables}


def test_swagger_2_definitions_are_read_too():
    """Both are in the wild, and the only difference that matters is where the schemas
    live -- telling a customer their valid spec is unreadable would be a bug."""
    swagger = {"swagger": "2.0", "definitions": {
        "User": {"type": "object", "properties": {"email": {"type": "string"}}}}}
    schema = rest.parse_openapi(_json.dumps(swagger), source_name="legacy")
    assert [t.name for t in schema.tables] == ["User"]


def test_a_required_property_is_recorded_as_not_nullable():
    schema = rest.parse_openapi(_json.dumps(_SPEC), source_name="billing-api")
    customer = next(t for t in schema.tables if t.name == "Customer")
    by_name = {c.name: c for c in customer.columns}
    assert by_name["id"].nullable is False
    assert by_name["email"].nullable is True


def test_the_source_stays_visible_on_every_subject():
    """A REST schema `Customer` and a database table `customer` are different subjects.
    Merging them would attribute one system's declared purpose to another's data."""
    schema = rest.parse_openapi(_json.dumps(_SPEC), source_name="billing-api")
    assert all(t.qualified.startswith("openapi.") for t in schema.tables)


def test_a_reference_cycle_terminates_instead_of_recursing():
    """One hop only. A resolved node that is itself a $ref is left alone rather than
    chased, so a spec where A points at B and B points back at A cannot hang the
    worker.

    `B` IS recorded, and that is correct rather than a leak: a top-level `$ref` is an
    alias, so B genuinely has A's shape and an API exposing both names exposes both
    shapes. What this pins is that the resolution stops -- the test completing at all
    is the assertion."""
    cyclic = {"openapi": "3.0.0", "components": {"schemas": {
        "A": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/B"}}},
        "B": {"$ref": "#/components/schemas/A"},
    }}}
    schema = rest.parse_openapi(_json.dumps(cyclic), source_name="cyclic")
    assert sorted(t.name for t in schema.tables) == ["A", "B"]
    # The alias resolved to A's shape rather than to an empty object.
    assert [c.name for c in next(t for t in schema.tables if t.name == "B").columns] == ["b"]


def test_a_document_that_is_not_json_is_refused_with_a_usable_message():
    with _pytest.raises(ConnectorError) as exc:
        rest.parse_openapi("openapi: 3.0.0\npaths: {}", source_name="yaml-spec")
    assert "JSON" in str(exc.value)


def test_a_spec_describing_only_paths_is_refused_rather_than_reported_empty():
    """Paths say what the API does, not what data it holds. An empty result would look
    like a clean assessment of a source that was never actually examined."""
    with _pytest.raises(ConnectorError) as exc:
        rest.parse_openapi(_json.dumps({"openapi": "3.0.0", "paths": {"/x": {}}}),
                           source_name="pathsonly")
    assert "nothing to assess" in str(exc.value).lower()


@_pytest.mark.asyncio
async def test_an_internal_address_is_refused_before_any_request_is_made():
    """A spec URL is caller-supplied, which is exactly the SSRF shape -- an internal
    admin API would otherwise be one request away."""
    for target in ("http://169.254.169.254/latest/meta-data/",
                   "http://127.0.0.1:8000/openapi.json",
                   "http://10.0.0.5/spec.json"):
        with _pytest.raises(ConnectorError) as exc:
            await rest.read_openapi(target, source_name="ssrf-probe")
        assert "cannot be fetched" in str(exc.value), target


@_pytest.mark.asyncio
async def test_an_empty_url_is_refused_without_touching_the_network():
    with _pytest.raises(ConnectorError):
        await rest.read_openapi("   ", source_name="blank")


def test_redirects_are_not_followed():
    """A redirect lands on a URL that never passed the SSRF guard, which is the
    standard way such a check is walked around."""
    import inspect
    assert "follow_redirects=False" in inspect.getsource(rest.read_openapi)


def test_no_credential_or_response_body_can_reach_an_error_message():
    """Error strings are stored on the run record, which a user can read."""
    import inspect
    body = inspect.getsource(rest.read_openapi)
    # The header value is never interpolated into a raised message.
    assert "auth_header}" not in body
    assert "{headers" not in body
    # Nor is the response body, which on an authenticated endpoint can contain anything.
    assert "response.text}" not in body


def test_the_connector_never_calls_the_api_itself():
    """The design commitment, asserted rather than trusted: this module issues exactly
    one GET, for the specification."""
    import inspect
    body = inspect.getsource(rest)
    assert body.count("client.get(") == 1
    for verb in ("client.post(", "client.put(", "client.patch(", "client.delete("):
        assert verb not in body


# ── The personal-data heuristic ─────────────────────────────────────────────────
#
# It decides which tables are worth assessing at all. Over-inclusion is the safe
# direction and costs one extra `undetermined` row; what it must not do is produce an
# EVIDENCE line that tells a reviewer a shipping date is why a table looked personal.

from app.agents.purpose.connectors.structured import Column, Table
from app.agents.purpose.services.structured_service import _tokens, holds_personal_data


def _table(*columns: str) -> Table:
    return Table(schema="public", name="t", columns=[Column(c, "text") for c in columns])


@_pytest.mark.parametrize(
    "column",
    ["shipDate", "description", "equipment", "municipality", "recipient",
     "hostname", "filename", "participants"],
)
def test_a_short_hint_no_longer_matches_as_a_bare_substring(column):
    """All of these matched before: "ip" is inside five of them and "name" inside two.

    Measured on a real OpenAPI spec -- a Petstore `Order` was assessed because of
    `shipDate`, and the evidence line said so."""
    assert holds_personal_data(_table(column)) == [], column


@_pytest.mark.parametrize(
    "column",
    ["email", "phone_number", "firstName", "lastName", "full_name", "name",
     "ip_address", "ip", "zip_code", "postcode", "aadhaar", "passport_no",
     "date_of_birth", "gender", "salary", "card_last4", "bank_account"],
)
def test_the_columns_that_matter_are_still_caught(column):
    assert holds_personal_data(_table(column)) == [column], column


def test_camel_case_is_split_like_a_reader_would():
    """An OpenAPI spec names properties `firstName` and `shipDate`. Splitting only on
    underscores leaves both as one opaque token, which is how `shipDate` escaped."""
    assert _tokens("shipDate") == {"ship", "date"}
    assert _tokens("firstName") == {"first", "name"}
    assert _tokens("ip_address") == {"ip", "address"}
    assert _tokens("IPAddress") == {"ipaddress"}


def test_over_inclusion_is_still_allowed_in_the_safe_direction():
    """A pet's `name` is flagged, and that is the correct price: it costs one extra
    `undetermined` assessment and asserts nothing about anybody's data."""
    assert holds_personal_data(_table("name")) == ["name"]


def test_a_table_of_codes_is_not_assessed_at_all():
    assert holds_personal_data(_table("currency_code", "rate", "updated_at")) == []


# ── A run must execute exactly once ─────────────────────────────────────────────
#
# Queued jobs retry up to three times, and reap_stale_jobs requeues one whose worker
# died. So a run CAN reach execution twice -- once after writing its assessments but
# before the status commit landed, and again on the retry.
#
# Measured, not imagined: seeding demo data executed a job inline while the worker also
# claimed it from the queue, and five runs ended with exactly twice the rows their own
# assessments_count claimed. A reviewer would see every finding duplicated with no way
# to tell which was real.

def test_claiming_a_run_refuses_a_second_execution():
    """`_mark_running` returns False for a run that already completed, and both entry
    points return immediately on False rather than writing a second set of rows."""
    import inspect

    from app.services import purpose_run_service as svc

    claim = inspect.getsource(svc._mark_running)
    assert 'run.status == "completed"' in claim
    assert "return False" in claim

    for entry in (svc.execute_source, svc.execute):
        body = inspect.getsource(entry)
        assert "if not await _mark_running(run_id):" in body, entry.__name__
        assert "return" in body


def test_a_retried_run_clears_what_the_failed_attempt_wrote():
    """A crash mid-write leaves partial rows. The retry must replace them, not add to
    them -- otherwise the guard above only moves the duplication one state earlier."""
    import inspect

    claim = inspect.getsource(
        __import__("app.services.purpose_run_service", fromlist=["x"])._mark_running
    )
    assert "delete(PurposeFinding)" in claim
    assert "delete(PurposeAssessment)" in claim
    # Findings reference assessments, so they have to go first.
    assert claim.index("delete(PurposeFinding)") < claim.index("delete(PurposeAssessment)")
    # And the counters are reset, or the run reports the old numbers over new rows.
    assert "assessments_count = 0" in claim
    assert "findings_count = 0" in claim


def test_both_paths_claim_through_the_same_helper():
    """The scan path used to open-code the status update, which meant it carried the
    identical gap and would have had to be fixed twice."""
    import inspect

    from app.services import purpose_run_service as svc

    body = inspect.getsource(svc.execute)
    assert 'run.status = "running"' not in body, (
        "the scan path is setting status itself again instead of claiming through "
        "_mark_running -- the double-execution guard lives in the helper"
    )


# ── API-CONTRACT REGRESSIONS: /sources/assess and /findings/{id}/decision ──────────
#
# An audit of the route and service layer found four status-code/behaviour defects:
# an invalid connector request surfaced as a bare 500 instead of a 4xx, an unknown
# `decision` value also surfaced as the wrong status (404, via NotFoundError, instead
# of a validation error), a missing-reason rejection used the base 500-status
# ConsivaError instead of the ReasonRequiredError this codebase already has for
# exactly this rule elsewhere (review_service.py), and a finding could be decided a
# second time with no conflict at all, silently overwriting the first reviewer's call.

import pytest as _pytest2  # noqa: E402

from app.core.exceptions import (  # noqa: E402
    FindingAlreadyDecidedError,
    InvalidSourceRequestError,
)


@_pytest2.mark.parametrize(
    "kwargs",
    [
        dict(connector="not-a-real-connector"),
        dict(connector="postgres", dsn=None),
        dict(connector="postgres", dsn="   "),
        dict(connector="csv", csv_content=None),
        dict(connector="rest", spec_url=None),
    ],
)
async def test_an_invalid_source_assessment_request_is_a_client_error_not_a_500(kwargs):
    """These are all malformed requests from the caller -- they must never reach the
    base ConsivaError's default status_code=500. Validation happens before any `db`
    use, so a real session isn't needed to exercise it."""
    from app.services import purpose_run_service as svc

    with _pytest2.raises(InvalidSourceRequestError) as exc:
        await svc.request_source_assessment(
            db=None, org_id=uuid.uuid4(), user_id=uuid.uuid4(),
            source_name="some source", **kwargs,
        )
    assert exc.value.status_code == 422


def test_an_unknown_decision_value_is_rejected_by_request_validation():
    """`decision` is a closed set (approved | rejected | dismissed). A bad value must
    fail Pydantic validation (-> HTTP 422) rather than reach the handler at all, where
    it previously raised NotFoundError (-> HTTP 404) -- the wrong status for a
    malformed request, and easy to confuse with "the finding doesn't exist"."""
    from pydantic import ValidationError

    from app.api.v1.routes.purpose import DecisionRequest

    with _pytest2.raises(ValidationError):
        DecisionRequest(decision="not-a-real-decision")

    # The three real values still construct cleanly.
    for value in ("approved", "rejected", "dismissed"):
        assert DecisionRequest(decision=value).decision == value


class _FakeScalars:
    def __init__(self, obj):
        self._obj = obj

    def first(self):
        return self._obj


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalars(self):
        return _FakeScalars(self._obj)


class _FakeFindingSession:
    """Just enough of AsyncSession for decide_finding's query + audit_service.record +
    commit/refresh -- no real database, so these tests stay as fast as the rest of the
    suite."""

    def __init__(self, finding):
        self._finding = finding
        self.committed = False

    async def execute(self, _stmt):
        return _FakeResult(self._finding)

    def add(self, _row):
        pass

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        pass


def _fake_user(org_id: uuid.UUID, user_id: uuid.UUID):
    from types import SimpleNamespace

    return SimpleNamespace(org_id=str(org_id), user_id=str(user_id))


async def test_rejecting_without_a_reason_raises_the_existing_reason_required_error():
    """Not a bare ConsivaError (-> 500): this exact rule already has a dedicated 422
    exception elsewhere in the codebase (review_service.py's ReasonRequiredError), and
    the decision endpoint must use it too."""
    from app.api.v1.routes.purpose import DecisionRequest, decide_finding
    from app.core.exceptions import ReasonRequiredError
    from app.db.models import PurposeFinding

    org_id = uuid.uuid4()
    finding = PurposeFinding(
        id=uuid.uuid4(), org_id=org_id, assessment_id=uuid.uuid4(),
        finding_type="mismatch", severity="medium", title="t", description="d",
        status="pending",
    )
    db = _FakeFindingSession(finding)

    with _pytest2.raises(ReasonRequiredError) as exc:
        await decide_finding(
            finding.id, DecisionRequest(decision="rejected", reason="  "),
            user=_fake_user(org_id, uuid.uuid4()), db=db,
        )
    assert exc.value.status_code == 422
    assert not db.committed


async def test_a_finding_cannot_be_decided_twice():
    """Two reviewers racing, or a stale UI re-submitting, must conflict (409) rather
    than silently overwrite the first decision -- the same contract
    FindingAlreadyDecidedError already enforces for Consent findings."""
    from app.api.v1.routes.purpose import DecisionRequest, decide_finding
    from app.db.models import PurposeFinding

    org_id = uuid.uuid4()
    finding = PurposeFinding(
        id=uuid.uuid4(), org_id=org_id, assessment_id=uuid.uuid4(),
        finding_type="mismatch", severity="medium", title="t", description="d",
        status="approved",  # already decided
    )
    db = _FakeFindingSession(finding)

    with _pytest2.raises(FindingAlreadyDecidedError) as exc:
        await decide_finding(
            finding.id, DecisionRequest(decision="rejected", reason="changed my mind"),
            user=_fake_user(org_id, uuid.uuid4()), db=db,
        )
    assert exc.value.status_code == 409
    assert not db.committed
    assert finding.status == "approved", "the already-decided finding must be untouched"


# ── RESOURCE-EXHAUSTION REGRESSIONS: per-org rate limit + bounded request fields ───
#
# Unbounded assessment submissions let one org occupy every slot in the single shared
# worker pool with requests that are slow to fail (a connector timeout each), starving
# every other org's queued work -- the same shape of risk scanner_max_scans_per_org_
# per_day already guards against for consent scans.

class _FakeCountSession:
    """Just enough of AsyncSession for `_assert_within_daily_rate_limit`'s COUNT query
    -- returns a fixed count, never touches a real database."""

    def __init__(self, count: int):
        self._count = count

    async def execute(self, _stmt):
        from types import SimpleNamespace

        return SimpleNamespace(scalar_one=lambda: self._count)


async def test_the_daily_assessment_rate_limit_is_enforced():
    from app.config import get_settings
    from app.core.exceptions import RateLimitExceededError
    from app.services import purpose_run_service as svc

    settings = get_settings()
    org_id = uuid.uuid4()

    # One under the limit: must not raise.
    db = _FakeCountSession(settings.purpose_max_assessments_per_org_per_day - 1)
    await svc._assert_within_daily_rate_limit(db, org_id=org_id)

    # At the limit: must raise -- this is a >=, not a strict >, check.
    db = _FakeCountSession(settings.purpose_max_assessments_per_org_per_day)
    with _pytest2.raises(RateLimitExceededError) as exc:
        await svc._assert_within_daily_rate_limit(db, org_id=org_id)
    assert exc.value.status_code == 429


async def test_an_oversized_source_assessment_field_is_rejected_by_request_validation():
    from pydantic import ValidationError

    from app.api.v1.routes.purpose import AssessSourceRequest

    with _pytest2.raises(ValidationError):
        AssessSourceRequest(
            source_name="s", connector="postgres", dsn="x" * 2049,
        )
    # A field at/under its bound still constructs cleanly.
    AssessSourceRequest(source_name="s", connector="postgres", dsn="x" * 2048)
