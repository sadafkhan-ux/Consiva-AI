"""Deterministic personal-data classification for discovered columns.

Rules run BEFORE any model, matching the same Phase-1 discipline as
rules/consent_rules.py ("deterministic lookup, rules, human review... before
any ML"). Nothing here calls an LLM.

WHY THIS IS A PRECEDENCE ENGINE AND NOT A KEYWORD LIST
------------------------------------------------------
The first version matched a token anywhere in the name and returned on the
first hit. Against a real 153-column production schema that produced a 74%
precision rate, and every error had the same shape: a token was found, but the
token was not what the column meant.

    otp_expires_at          -> "otp"      -> Credential   (it is a timestamp)
    connected_gmail_address -> "address"  -> Location     (it is an email)
    email_source            -> "email"    -> Contact      (it is provenance)
    events.location         -> "location" -> Location     (it is a venue)

It also dropped columns silently. `_OPERATIONAL_EXACT` was a global blocklist
consulted before any context, so `campaign_leads.title` -- a person's job title
-- disappeared with no review item. A silent false negative is worse than a
loud false positive: the reviewer never sees it.

So classification now evaluates several signals in a fixed, inspectable order,
and ALWAYS returns a decision. `None` is no longer a possible answer, because
`None` used to mean three different things and the caller could not tell which.

PRECEDENCE (first match wins, and every stage records why)
    1. Structural exclusion      ids, foreign keys, pure metrics
    2. Temporal exclusion        anything *_at / *_expires regardless of stem
    3. Provenance / discriminator *_source, *_kind, *_type
    4. Exact whole-name rule     the strongest positive evidence
    5. Context-dependent column  title / name / url / location, judged by table
    6. Free-text column          notes / body -- potential personal data
    7. Non-person qualifier      agent_name, service_name, campaign_name
    8. Generic token rule        weakest positive evidence
    9. Unknown                   explicit, reviewable, never dropped
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.ropa.rules import column_context as ctx

# ── Categories ──────────────────────────────────────────────────────────────────
CATEGORY_CONTACT = "Contact Data"
CATEGORY_IDENTITY = "Identity Data"
CATEGORY_GOVERNMENT_ID = "Government Identifier / High-Risk"
CATEGORY_ONLINE_ID = "Online Identifier"
CATEGORY_LOCATION = "Location Data"
CATEGORY_FINANCIAL = "Financial Data"
CATEGORY_EMPLOYMENT = "Employment Data"
CATEGORY_CREDENTIAL = "Credential / Secret"
CATEGORY_HEALTH = "Health Data"
CATEGORY_PROFESSIONAL = "Professional / Social Profile"
# Free text and behavioural signals are personal data whose SHAPE is unknown --
# they are surfaced for review rather than asserted as a specific category.
CATEGORY_FREE_TEXT = "Potential Personal Data (free text)"
CATEGORY_BEHAVIOURAL = "Behavioural Data"

SENSITIVE_CATEGORIES = frozenset(
    {CATEGORY_GOVERNMENT_ID, CATEGORY_FINANCIAL, CATEGORY_HEALTH, CATEGORY_CREDENTIAL}
)

# ── Outcome statuses ────────────────────────────────────────────────────────────
CLASSIFIED = "classified"        # a personal-data category was established
OPERATIONAL = "operational"      # deliberately not personal data, with a reason
PROVENANCE = "provenance"        # metadata describing a value, not the value
REVIEW = "review_required"       # could be personal; a human must decide
UNKNOWN = "unknown"              # no rule applied; explicit, not dropped

# ── Confidence, composed from signals rather than hardcoded per branch ──────────
BASE_EXACT = 0.90
BASE_CONTEXT = 0.82
BASE_TOKEN = 0.70
BASE_FREE_TEXT = 0.45
BONUS_TYPE_MATCH = 0.06
BONUS_TABLE_CONTEXT = 0.04
PENALTY_CONFLICT = 0.20
CONFIDENCE_CEILING = 0.99
CONFIDENCE_FLOOR = 0.10
REVIEW_THRESHOLD = 0.70


@dataclass(frozen=True)
class PersonalDataRule:
    rule_id: str
    category: str
    exact: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    type_hints: tuple[str, ...] = field(default=())


RULES: tuple[PersonalDataRule, ...] = (
    PersonalDataRule(
        rule_id="PD-001", category=CATEGORY_CONTACT,
        exact=("email", "email_address", "e_mail", "contact_email", "phone", "phone_number",
               "mobile", "mobile_number", "contact_number", "telephone", "fax", "whatsapp",
               "connected_gmail_address", "gmail_address", "recipient_email", "sender_email",
               "email_hash"),
        tokens=("email", "phone", "mobile", "telephone", "whatsapp", "gmail"),
        type_hints=("text", "character varying", "varchar", "citext"),
    ),
    PersonalDataRule(
        rule_id="PD-002", category=CATEGORY_IDENTITY,
        exact=("name", "full_name", "first_name", "last_name", "middle_name", "surname",
               "father_name", "mother_name", "date_of_birth", "dob", "birth_date", "gender",
               "age", "marital_status", "nationality", "display_name", "contact_name"),
        tokens=("firstname", "lastname", "fullname", "surname", "dob", "birthdate", "gender"),
    ),
    PersonalDataRule(
        rule_id="PD-003", category=CATEGORY_GOVERNMENT_ID,
        exact=("aadhaar", "aadhar", "aadhaar_number", "pan", "pan_number", "passport",
               "passport_number", "ssn", "voter_id", "driving_licence", "driving_license",
               "national_id", "tax_id", "gstin", "uid"),
        tokens=("aadhaar", "aadhar", "passport", "ssn", "gstin"),
    ),
    PersonalDataRule(
        rule_id="PD-004", category=CATEGORY_ONLINE_ID,
        exact=("ip", "ip_address", "device_id", "cookie_id", "session_id", "user_agent",
               "mac_address", "advertising_id", "fingerprint", "visitor_id", "client_id",
               "tracking_id"),
        tokens=("ipaddress", "deviceid", "useragent", "macaddress", "fingerprint"),
    ),
    PersonalDataRule(
        rule_id="PD-005", category=CATEGORY_LOCATION,
        exact=("address", "address_line1", "address_line2", "street", "street_address", "city",
               "state", "postal_code", "pincode", "pin_code", "zip", "zipcode", "country",
               "latitude", "longitude", "geolocation", "location", "billing_address",
               "shipping_address"),
        tokens=("pincode", "zipcode", "latitude", "longitude", "geolocation"),
    ),
    PersonalDataRule(
        rule_id="PD-006", category=CATEGORY_FINANCIAL,
        exact=("account_number", "bank_account", "ifsc", "card_number", "cardholder_name",
               "cvv", "upi", "upi_id", "iban", "swift", "salary", "compensation", "income",
               "payment_method", "razorpay_payment_id", "payment_id", "stripe_customer_id",
               "transaction_id"),
        tokens=("ifsc", "cardnumber", "iban", "upi", "razorpay"),
    ),
    PersonalDataRule(
        rule_id="PD-007", category=CATEGORY_EMPLOYMENT,
        exact=("employee_id", "emp_id", "designation", "job_title", "department",
               "joining_date", "employment_type", "manager_id", "reporting_manager",
               "seniority", "job_role"),
        tokens=("designation", "jobtitle"),
    ),
    PersonalDataRule(
        rule_id="PD-008", category=CATEGORY_CREDENTIAL,
        exact=("password", "password_hash", "passwd", "secret", "api_key", "token",
               "access_token", "refresh_token", "otp", "otp_code", "security_answer",
               "private_key", "verification_token", "unsubscribe_token", "reset_token"),
        tokens=("password", "passwd", "apikey", "secret", "otp"),
    ),
    PersonalDataRule(
        rule_id="PD-009", category=CATEGORY_HEALTH,
        exact=("blood_group", "medical_history", "diagnosis", "prescription", "allergy",
               "disability", "health_condition", "medical_record_number"),
        tokens=("diagnosis", "prescription", "allergy", "disability"),
    ),
    PersonalDataRule(
        rule_id="PD-010", category=CATEGORY_PROFESSIONAL,
        # Excludes company_name / organization_name: a company is not a natural
        # person, and those columns are usually a B2B entity record.
        exact=("linkedin", "linkedin_url", "linkedin_urn", "linkedin_profile", "twitter",
               "twitter_handle", "facebook", "facebook_url", "github", "github_username",
               "instagram", "social_profile", "profile_url", "personal_website",
               "portfolio_url", "employer", "job_role", "headline", "industry"),
        tokens=("linkedin", "twitter", "facebook", "github", "instagram", "employer"),
    ),
)

# Structural columns: identifiers, audit timestamps, flags. Not personal data on
# their own in ANY table -- unlike `title` or `notes`, whose meaning changes.
_STRUCTURAL_EXACT = frozenset({
    "id", "uuid", "created_at", "updated_at", "deleted_at", "created_by", "updated_by",
    "is_active", "is_deleted", "is_verified", "sort_order", "position", "slug",
    "version", "status", "state", "enabled", "archived", "metadata", "payload",
    "config", "settings", "currency", "locale", "timezone", "attempts", "step",
    "role", "permission", "plan", "tier",
    "sequence_step", "tags",
})

# Qualifiers meaning the thing being named is not a natural person. Without this,
# `agent_name`, `service_name` and `campaign_name` all read as someone's identity.
_NON_PERSON_QUALIFIERS = frozenset({
    "agent", "model", "service", "file", "filename", "table", "column", "schema",
    "database", "db", "vendor", "product", "event", "brand", "campaign", "rule",
    "job", "task", "step", "stage", "policy", "doc", "document", "org",
    "organization", "organisation", "company", "domain", "host", "hostname",
    "cookie", "tracker", "script", "pattern", "template", "class", "category",
    "bucket", "queue", "topic",
    "channel", "app", "application", "system", "module", "package", "repo",
    "repository", "branch", "environment", "env", "server", "node", "cluster",
    "region", "zone", "index", "collection", "outreach",
})

# Tokens that identify personal data whatever else the name contains, so
# `customer_email` survives the qualifier guard above.
_UNAMBIGUOUS_TOKENS = frozenset({
    "email", "phone", "mobile", "aadhaar", "aadhar", "passport", "ssn", "pan",
    "dob", "birthdate", "password", "passwd", "ifsc", "upi", "iban", "salary",
    "latitude", "longitude", "pincode", "zipcode", "gmail", "linkedin",
})

# Behavioural columns: meaningless alone, personal when tied to a person row.
_BEHAVIOURAL_NAMES = frozenset({"url", "referrer", "page", "clicked_url", "link"})


@dataclass(frozen=True)
class Classification:
    """Always returned. A column is never silently dropped."""

    category: str | None
    status: str
    confidence: float
    method: str
    evidence: tuple[str, ...]
    conflicts: tuple[str, ...] = ()
    rule_id: str | None = None

    @property
    def review_required(self) -> bool:
        if self.status in (REVIEW, UNKNOWN):
            return True
        if self.status == CLASSIFIED:
            return self.confidence < REVIEW_THRESHOLD or bool(self.conflicts)
        return False

    @property
    def is_personal_data(self) -> bool:
        return self.status in (CLASSIFIED, REVIEW) and self.category is not None


def classify_column(
    column_name: str,
    data_type: str | None = None,
    *,
    table_name: str | None = None,
    table_context: str | None = None,
    has_person_relationship: bool = False,
    person_context: bool | None = None,
) -> Classification:
    """Classify one column against every available signal.

    `table_context` is ctx.PERSON / OPERATIONAL / UNKNOWN. When not supplied it
    is resolved from `table_name` plus `has_person_relationship`, so a caller
    with only a name still gets contextual behaviour.

    `person_context` is the older boolean, kept so existing callers keep
    working; it is treated as a PERSON table when true.
    """
    shape = ctx.describe(column_name, data_type)
    context = _resolve_context(table_name, table_context, person_context, has_person_relationship)
    where = [f"column={shape.normalized}"]
    if table_name:
        where.append(f"table={table_name}")
    where.append(f"table_context={context}")
    if shape.data_type:
        where.append(f"type={shape.data_type}")

    for stage in (
        _stage_structural,
        _stage_temporal,
        _stage_provenance,
        _stage_exact,
        _stage_context_dependent,
        _stage_free_text,
        _stage_non_person_qualifier,
        _stage_token,
    ):
        result = stage(shape, context, tuple(where))
        if result is not None:
            return result

    if context == ctx.OPERATIONAL:
        return _operational(
            where, "unmatched_in_operational_table",
            "no personal-data rule matched, and the table holds business objects "
            "rather than people -- recorded as non-personal rather than sent to review",
        )
    if shape.is_temporal:
        return _operational(where, "temporal_type", "a timestamp column")

    return Classification(
        category=None, status=UNKNOWN, confidence=0.0, method="no_rule_matched",
        evidence=(*where, "no positive or negative rule applied"),
    )


# ── Stage 1: structural ─────────────────────────────────────────────────────────

def _stage_structural(shape: ctx.ColumnShape, context: str, where: tuple[str, ...]):
    """Identifiers, audit columns and metrics. These never become personal data
    through table context, which is what separates them from `title` or `notes`."""
    if shape.normalized in _STRUCTURAL_EXACT:
        return _operational(where, "structural_column", "a record identifier, audit column or flag")
    if shape.is_metric_shaped:
        return _operational(where, "metric_shape", "an aggregate or measurement, not the data itself")
    if shape.is_tally:
        return _operational(
            where, "event_tally",
            "a numeric count of events (sent, opened, clicked), not the data being counted",
        )
    # `*_id` foreign keys point AT a person but do not themselves describe one.
    # The relationship is evidence for other columns, not a category for this one.
    if shape.normalized.endswith("_id") and shape.is_uuid:
        return _operational(where, "foreign_key", "a reference to another row, not personal data")
    if shape.is_boolean:
        return _operational(where, "boolean_flag", "a true/false flag, not personal data")
    return None


# ── Stage 2: temporal ───────────────────────────────────────────────────────────

def _stage_temporal(shape: ctx.ColumnShape, context: str, where: tuple[str, ...]):
    """A timestamp is a timestamp whatever its stem says.

    Fixes `otp_expires_at` and `token_expires_at`, which the token engine read as
    credentials because `otp` and `token` appear in the name. When a column is
    typed as a timestamp AND named like one, no stem can make it a secret.
    """
    if shape.has_temporal_suffix and (shape.is_temporal or not shape.is_textual):
        return _operational(
            where, "temporal_column",
            "a timestamp describing when something happened, not the value itself",
        )
    return None


# ── Stage 3: provenance ─────────────────────────────────────────────────────────

def _stage_provenance(shape: ctx.ColumnShape, context: str, where: tuple[str, ...]):
    """`email_source` is where an address came from; `linkedin_url_kind` is which
    sort of URL it is. Metadata about a value is not the value."""
    if not shape.has_provenance_suffix:
        return None
    suffix = next(s for s in ctx.PROVENANCE_SUFFIXES if shape.normalized.endswith(s))
    return Classification(
        category=None, status=PROVENANCE, confidence=BASE_CONTEXT,
        method="provenance_suffix",
        evidence=(*where, f"suffix={suffix}",
                  "describes the origin, kind or state of a value rather than being one"),
    )


# ── Stage 4: exact ──────────────────────────────────────────────────────────────

def _stage_exact(shape: ctx.ColumnShape, context: str, where: tuple[str, ...]):
    """A whole-name rule hit is the strongest positive evidence available, so it
    runs before the qualifier guard -- otherwise `profile_url` would be discarded
    by its own `profile` token."""
    # Context-dependent names are deliberately deferred to stage 5, where the
    # table decides. Without this, `events.location` matches PD-005 exactly and
    # a venue becomes a person's location.
    if shape.is_context_dependent:
        return None
    for rule in RULES:
        if shape.normalized in rule.exact:
            return _positive(rule, shape, context, where, BASE_EXACT, "exact_name")
    return None


# ── Stage 5: context-dependent ──────────────────────────────────────────────────

def _stage_context_dependent(shape: ctx.ColumnShape, context: str, where: tuple[str, ...]):
    """`leads.title` is a job title. `campaigns.title` is a campaign's title.
    Same column name, opposite answers -- the table is the deciding signal.

    This stage is why `campaign_leads.title` is no longer dropped and why
    `events.location` and `outreach_campaigns.name` are no longer personal data.
    """
    if not shape.is_context_dependent:
        return None
    name = shape.normalized

    if context == ctx.PERSON:
        mapping = {
            "title": (CATEGORY_EMPLOYMENT, "PD-007", "a job title in a table about people"),
            "name": (CATEGORY_IDENTITY, "PD-002", "a person's name"),
            "location": (CATEGORY_LOCATION, "PD-005", "a person's location"),
            "address": (CATEGORY_LOCATION, "PD-005", "a person's address"),
            "company": (CATEGORY_PROFESSIONAL, "PD-010", "the person's employer"),
            "handle": (CATEGORY_PROFESSIONAL, "PD-010", "a social handle"),
            "picture": (CATEGORY_IDENTITY, "PD-002", "a photograph of the person"),
            "image": (CATEGORY_IDENTITY, "PD-002", "an image of the person"),
            "avatar": (CATEGORY_IDENTITY, "PD-002", "an image of the person"),
        }
        if name in mapping:
            category, rule_id, why = mapping[name]
            return Classification(
                category=category, status=CLASSIFIED,
                confidence=_score(BASE_CONTEXT, type_match=False, context_supports=True),
                method="table_context", rule_id=rule_id,
                evidence=(*where, "person_table=true", why),
            )
        if name in _BEHAVIOURAL_NAMES:
            return Classification(
                category=CATEGORY_BEHAVIOURAL, status=REVIEW, confidence=BASE_FREE_TEXT,
                method="behavioural_in_person_context", rule_id="PD-011",
                evidence=(*where, "person_table=true",
                          "a URL recorded against a person is behavioural data; confirm what it tracks"),
            )
        return Classification(
            category=None, status=REVIEW, confidence=BASE_FREE_TEXT,
            method="context_dependent_unresolved",
            evidence=(*where, "generic name in a person table; a reviewer must decide"),
        )

    if context == ctx.OPERATIONAL:
        return _operational(
            where, "context_dependent_operational",
            f"'{name}' describes a business object in an operational table, not a person",
        )

    # Unknown table context: do not guess in either direction.
    return Classification(
        category=None, status=REVIEW, confidence=BASE_FREE_TEXT,
        method="context_dependent_no_table_context",
        evidence=(*where, "meaning depends on the table, which could not be classified"),
    )


# ── Stage 6: free text ──────────────────────────────────────────────────────────

def _stage_free_text(shape: ctx.ColumnShape, context: str, where: tuple[str, ...]):
    """Free text can hold anything a human typed, including personal data nobody
    designed for. Silently dropping it is how `campaign_leads.notes` vanished."""
    if not shape.is_content:
        return None
    if context == ctx.PERSON:
        return Classification(
            category=CATEGORY_FREE_TEXT, status=REVIEW, confidence=BASE_FREE_TEXT,
            method="free_text_in_person_context", rule_id="PD-012",
            evidence=(*where, "person_table=true",
                      "free text about a person may contain personal data; sample and confirm"),
        )
    return _operational(
        where, "free_text_non_person_table",
        "free text outside a person-related table; review if it can hold user input",
    )


# ── Stage 7: qualifier guard ────────────────────────────────────────────────────

def _stage_non_person_qualifier(shape: ctx.ColumnShape, context: str, where: tuple[str, ...]):
    """`agent_name` / `service_name` / `campaign_goal` name system objects."""
    hits = shape.tokens & _NON_PERSON_QUALIFIERS
    if not hits:
        return None
    if shape.tokens & _UNAMBIGUOUS_TOKENS or shape.compact in _UNAMBIGUOUS_TOKENS:
        return None  # `customer_email` is still an email
    return _operational(
        where, "non_person_qualifier",
        f"qualified by {min(hits)!r}, which names a system object rather than a person",
    )


# ── Stage 8: token ──────────────────────────────────────────────────────────────

def _stage_token(shape: ctx.ColumnShape, context: str, where: tuple[str, ...]):
    """Weakest positive evidence, and the last chance before Unknown.

    Conflicts are recorded rather than resolved silently: if two rules both
    match, the first wins by precedence but the disagreement is surfaced and
    confidence is reduced, so a reviewer can see the engine was unsure.
    """
    matches: list[tuple[PersonalDataRule, str]] = []
    for rule in RULES:
        hit = shape.tokens & set(rule.exact) or shape.tokens & set(rule.tokens)
        if hit:
            matches.append((rule, min(hit)))
        elif shape.compact in rule.tokens:
            matches.append((rule, shape.compact))

    if not matches:
        return None

    rule, matched_on = matches[0]
    conflicts = tuple(
        f"{other.rule_id}:{other.category}" for other, _ in matches[1:]
    )
    return _positive(
        rule, shape, context, (*where, f"token={matched_on}"),
        BASE_TOKEN, "token", conflicts=conflicts,
    )


# ── helpers ─────────────────────────────────────────────────────────────────────

def _resolve_context(table_name, table_context, person_context, has_person_relationship) -> str:
    if table_context:
        return table_context
    if person_context:
        return ctx.PERSON
    return ctx.resolve_table_context(table_name, has_person_relationship=has_person_relationship)


def _positive(rule, shape, context, where, base, method, conflicts=()) -> Classification:
    type_match = bool(rule.type_hints and any(h in shape.data_type for h in rule.type_hints))
    context_supports = context == ctx.PERSON
    confidence = _score(base, type_match, context_supports, conflicts=bool(conflicts))
    evidence = [*where, f"rule={rule.rule_id}"]
    if type_match:
        evidence.append("data_type consistent with category")
    if context_supports:
        evidence.append("person-related table supports this category")
    if conflicts:
        evidence.append(f"other rules also matched: {', '.join(conflicts)}")
    return Classification(
        category=rule.category, status=CLASSIFIED, confidence=confidence,
        method=method, rule_id=rule.rule_id, evidence=tuple(evidence), conflicts=conflicts,
    )


def _operational(where, method, why) -> Classification:
    return Classification(
        category=None, status=OPERATIONAL, confidence=BASE_CONTEXT,
        method=method, evidence=(*where, why),
    )


def _score(base: float, type_match: bool, context_supports: bool, *, conflicts: bool = False) -> float:
    """Transparent additive scoring -- every term is visible in the evidence."""
    score = base
    if type_match:
        score += BONUS_TYPE_MATCH
    if context_supports:
        score += BONUS_TABLE_CONTEXT
    if conflicts:
        score -= PENALTY_CONFLICT
    return round(min(max(score, CONFIDENCE_FLOOR), CONFIDENCE_CEILING), 4)


# Every category this module can return for a column that IS personal data. Built
# from the constants above rather than retyped, so adding a category cannot leave this
# set behind. Exposed because Agent 4 needs to ask "is this label personal data?" of a
# category string it read back from a stored ROPA baseline, where the Classification
# object that produced it is long gone.
PERSONAL_CATEGORIES = frozenset({
    CATEGORY_CONTACT,
    CATEGORY_IDENTITY,
    CATEGORY_GOVERNMENT_ID,
    CATEGORY_ONLINE_ID,
    CATEGORY_LOCATION,
    CATEGORY_FINANCIAL,
    CATEGORY_EMPLOYMENT,
    CATEGORY_CREDENTIAL,
    CATEGORY_HEALTH,
    CATEGORY_PROFESSIONAL,
    CATEGORY_FREE_TEXT,
    CATEGORY_BEHAVIOURAL,
})


def is_personal_category(category: str | None) -> bool:
    """Whether a category NAME denotes personal data.

    The counterpart to `Classification.is_personal_data`, for callers holding only the
    stored label -- the engine's non-personal outcomes are recorded as explicit labels
    like "Not Personal Data (operational)", which must not be mistaken for a category.
    """
    return bool(category) and category in PERSONAL_CATEGORIES


def is_operational(column_name: str, data_type: str | None = None, *, table_name: str | None = None) -> bool:
    """Kept for callers that only need the boolean. Context-aware now: `title` in
    a person table is NOT operational, which the old global blocklist got wrong."""
    return classify_column(column_name, data_type, table_name=table_name).status == OPERATIONAL
