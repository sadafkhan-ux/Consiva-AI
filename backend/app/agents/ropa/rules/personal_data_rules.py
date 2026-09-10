"""Deterministic personal-data classification for discovered columns.

Rules run BEFORE any LLM, matching the same Phase-1 discipline as
rules/consent_rules.py ("deterministic lookup, rules, human review... before any
ML"). A column that no rule matches is NOT guessed at -- it is either dropped as
non-personal (when it matches a known-operational name) or returned as Unknown
with review_required=True.

Matching is token-based, not substring-based, because substring matching is
actively wrong here: "name" is inside "filename", "hostname" and "surname", and
"id" is inside almost everything. Each column name is normalized and split into
tokens, then matched against an exact full-name list first (high confidence) and
a token list second (lower confidence).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Categories follow the ROPA prompt §5. "High-Risk" ones drive risk scoring later.
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

# Categories treated as sensitive/special by risk_service.
SENSITIVE_CATEGORIES = frozenset(
    {CATEGORY_GOVERNMENT_ID, CATEGORY_FINANCIAL, CATEGORY_HEALTH, CATEGORY_CREDENTIAL}
)

CONFIDENCE_EXACT = 0.95
CONFIDENCE_TOKEN = 0.75
# Corroboration bonus when the column's SQL type is consistent with the category
# (e.g. an "email" column typed text, not integer). Capped at 0.99 -- never 1.0,
# because a name-based rule is evidence, not proof.
CONFIDENCE_TYPE_BONUS = 0.03
CONFIDENCE_CEILING = 0.99


@dataclass(frozen=True)
class PersonalDataRule:
    rule_id: str
    category: str
    exact: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    # SQL data types that corroborate this category; empty means "any type".
    type_hints: tuple[str, ...] = field(default=())


RULES: tuple[PersonalDataRule, ...] = (
    PersonalDataRule(
        rule_id="PD-001",
        category=CATEGORY_CONTACT,
        exact=("email", "email_address", "e_mail", "contact_email", "phone", "phone_number",
               "mobile", "mobile_number", "contact_number", "telephone", "fax", "whatsapp"),
        tokens=("email", "phone", "mobile", "telephone", "whatsapp"),
        type_hints=("text", "character varying", "varchar", "citext"),
    ),
    PersonalDataRule(
        rule_id="PD-002",
        category=CATEGORY_IDENTITY,
        exact=("name", "full_name", "first_name", "last_name", "middle_name", "surname",
               "father_name", "mother_name", "date_of_birth", "dob", "birth_date", "gender",
               "age", "marital_status", "nationality", "display_name", "contact_name"),
        tokens=("firstname", "lastname", "fullname", "surname", "dob", "birthdate", "gender"),
    ),
    PersonalDataRule(
        rule_id="PD-003",
        category=CATEGORY_GOVERNMENT_ID,
        exact=("aadhaar", "aadhar", "aadhaar_number", "pan", "pan_number", "passport",
               "passport_number", "ssn", "voter_id", "driving_licence", "driving_license",
               "national_id", "tax_id", "gstin", "uid"),
        tokens=("aadhaar", "aadhar", "passport", "ssn", "gstin"),
    ),
    PersonalDataRule(
        rule_id="PD-004",
        category=CATEGORY_ONLINE_ID,
        exact=("ip", "ip_address", "device_id", "cookie_id", "session_id", "user_agent",
               "mac_address", "advertising_id", "fingerprint", "visitor_id", "client_id"),
        tokens=("ipaddress", "deviceid", "useragent", "macaddress", "fingerprint"),
    ),
    PersonalDataRule(
        rule_id="PD-005",
        category=CATEGORY_LOCATION,
        exact=("address", "address_line1", "address_line2", "street", "street_address", "city",
               "state", "postal_code", "pincode", "pin_code", "zip", "zipcode", "country",
               "latitude", "longitude", "geolocation", "location"),
        tokens=("address", "pincode", "zipcode", "latitude", "longitude", "geolocation"),
    ),
    PersonalDataRule(
        rule_id="PD-006",
        category=CATEGORY_FINANCIAL,
        exact=("account_number", "bank_account", "ifsc", "card_number", "cardholder_name",
               "cvv", "upi", "upi_id", "iban", "swift", "salary", "compensation", "income",
               "payment_method", "billing_address"),
        tokens=("ifsc", "cardnumber", "iban", "salary", "upi"),
    ),
    PersonalDataRule(
        rule_id="PD-007",
        category=CATEGORY_EMPLOYMENT,
        exact=("employee_id", "emp_id", "designation", "job_title", "department",
               "joining_date", "employment_type", "manager_id", "reporting_manager"),
        tokens=("designation", "jobtitle",),
    ),
    PersonalDataRule(
        rule_id="PD-008",
        category=CATEGORY_CREDENTIAL,
        exact=("password", "password_hash", "passwd", "secret", "api_key", "token",
               "access_token", "refresh_token", "otp", "security_answer", "private_key"),
        tokens=("password", "passwd", "apikey", "secret", "otp"),
    ),
    PersonalDataRule(
        rule_id="PD-009",
        category=CATEGORY_HEALTH,
        exact=("blood_group", "medical_history", "diagnosis", "prescription", "allergy",
               "disability", "health_condition", "medical_record_number"),
        tokens=("diagnosis", "prescription", "allergy", "disability"),
    ),
    PersonalDataRule(
        rule_id="PD-010",
        category=CATEGORY_PROFESSIONAL,
        # Deliberately excludes `company_name` / `organization_name`: a company is
        # not a natural person, and those columns are far more often a B2B entity
        # record than professional data about someone.
        exact=("linkedin", "linkedin_url", "linkedin_profile", "twitter", "twitter_handle",
               "facebook", "facebook_url", "github", "github_username", "instagram",
               "social_profile", "profile_url", "personal_website", "portfolio_url",
               "employer", "job_role"),
        tokens=("linkedin", "twitter", "facebook", "github", "instagram", "employer"),
    ),
)

# Columns that are structurally operational, never personal data on their own.
# Checked BEFORE the rules so e.g. "created_at" never trips a date rule.
_OPERATIONAL_EXACT = frozenset({
    "id", "uuid", "created_at", "updated_at", "deleted_at", "created_by", "updated_by",
    "status", "type", "version", "is_active", "is_deleted", "sort_order", "position",
    "slug", "url", "title", "description", "content", "metadata", "payload", "config",
    "count", "total", "amount", "quantity", "price", "currency", "started_at",
    "completed_at", "expires_at", "error", "notes", "tags", "locale", "timezone",
})

# Qualifiers that mean the thing being named is NOT a natural person. Without
# this guard, token matching classifies `agent_name`, `model_name`,
# `service_name` and `name_pattern` as a person's Identity Data -- all four were
# real false positives observed on a live 221-column database.
_NON_PERSON_QUALIFIERS = frozenset({
    "agent", "model", "service", "file", "filename", "table", "column", "schema",
    "database", "db", "vendor", "product", "event", "brand", "campaign", "rule",
    "job", "task", "step", "stage", "policy", "doc", "document", "org",
    "organization", "organisation", "company", "domain", "host", "hostname",
    "cookie", "tracker", "script", "pattern", "template", "type", "class",
    "category", "tag", "label", "key", "field", "attribute", "property",
    "bucket", "queue", "topic", "channel", "app", "application", "system",
    "module", "package", "repo", "repository", "branch", "environment", "env",
    "server", "node", "cluster", "region", "zone", "index", "collection",
})

# Aggregate/metric shapes are measurements, never the data itself: `token_count`
# is a number of LLM tokens, not a credential (another real false positive).
_METRIC_SUFFIXES = ("_count", "_total", "_num", "_size", "_length", "_len",
                    "_score", "_rate", "_ratio", "_percent", "_pct", "_ms", "_seconds")
_METRIC_PREFIXES = ("num_", "count_", "total_", "avg_", "max_", "min_", "sum_")

# Column names too generic to classify on their own -- they only mean a person
# when the TABLE is about people. `cookies.name` is a cookie's name;
# `customers.name` is a person's name. Same column, different meaning.
_PERSON_CONTEXT_REQUIRED = frozenset({"name", "full_name", "display_name", "age", "gender"})

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _normalize(column_name: str) -> str:
    return column_name.strip().lower()


def _tokens(normalized: str) -> set[str]:
    return {t for t in _TOKEN_SPLIT.split(normalized) if t}


@dataclass(frozen=True)
class RuleMatch:
    rule_id: str
    category: str
    confidence: float
    match_kind: str  # "exact" | "token"
    matched_on: str


def classify_column(
    column_name: str,
    data_type: str | None = None,
    *,
    person_context: bool = False,
) -> RuleMatch | None:
    """Classify one column name. Returns None when the column is known-
    operational, is a metric, is about a non-person entity, or matches no rule --
    the caller decides whether that means "not personal data" or "Unknown".

    `person_context` says whether the containing TABLE is about natural persons
    (a customers/employees/attendees table). Generic columns like `name` are only
    classified as Identity Data when that context holds.
    """
    normalized = _normalize(column_name)
    if normalized in _OPERATIONAL_EXACT or _is_metric(normalized):
        return None

    tokens = _tokens(normalized)

    if normalized in _PERSON_CONTEXT_REQUIRED and not person_context:
        return None

    lowered_type = (data_type or "").lower()

    # An explicit whole-name rule hit is the strongest evidence available, so it
    # is checked BEFORE the generic qualifier guard below -- otherwise a rule for
    # a name like "profile_url" would be discarded by its own "profile" token.
    for rule in RULES:
        if normalized in rule.exact:
            return _build(rule, CONFIDENCE_EXACT, "exact", normalized, lowered_type)

    # A qualifier like "agent"/"service"/"pattern" means the name belongs to a
    # system object, not a person -- unless the column is unambiguous on its own
    # (e.g. "customer_email" is still contact data despite the "customer" token).
    if tokens & _NON_PERSON_QUALIFIERS and not _has_unambiguous_token(normalized, tokens):
        return None

    normalized_compact = normalized.replace("_", "")

    for rule in RULES:
        # A token hit is weaker evidence than a whole-name hit: "user_email_verified"
        # is contact-adjacent, but the column itself may be a boolean flag.
        hit = tokens & set(rule.exact) or tokens & set(rule.tokens)
        if hit:
            return _build(rule, CONFIDENCE_TOKEN, "token", sorted(hit)[0], lowered_type)
        if normalized_compact in rule.tokens:
            return _build(rule, CONFIDENCE_TOKEN, "token", normalized_compact, lowered_type)

    return None


def _build(rule: PersonalDataRule, base: float, kind: str, matched_on: str, lowered_type: str) -> RuleMatch:
    confidence = base
    if rule.type_hints and any(hint in lowered_type for hint in rule.type_hints):
        confidence = min(confidence + CONFIDENCE_TYPE_BONUS, CONFIDENCE_CEILING)
    return RuleMatch(
        rule_id=rule.rule_id,
        category=rule.category,
        confidence=round(confidence, 4),
        match_kind=kind,
        matched_on=matched_on,
    )


def _is_metric(normalized: str) -> bool:
    return normalized.endswith(_METRIC_SUFFIXES) or normalized.startswith(_METRIC_PREFIXES)


# Tokens that identify personal data regardless of what else the column name
# contains -- "customer_email" and "user_phone" stay classified even though
# "customer"/"user" would otherwise read as entity qualifiers.
_UNAMBIGUOUS_TOKENS = frozenset({
    "email", "phone", "mobile", "aadhaar", "aadhar", "passport", "ssn", "pan",
    "dob", "birthdate", "password", "passwd", "ifsc", "upi", "iban", "salary",
    "latitude", "longitude", "pincode", "zipcode",
})


def _has_unambiguous_token(normalized: str, tokens: set[str]) -> bool:
    return bool(tokens & _UNAMBIGUOUS_TOKENS) or normalized.replace("_", "") in _UNAMBIGUOUS_TOKENS


def is_operational(column_name: str) -> bool:
    """True when a column is structurally operational (an id, a timestamp, a
    status) and therefore not a personal-data candidate needing human review."""
    return _normalize(column_name) in _OPERATIONAL_EXACT
