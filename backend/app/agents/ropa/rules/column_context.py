"""Context signals the classifier reasons over, separated from the rules
themselves so each can be tested and extended on its own.

The previous engine matched a token anywhere in a column name and stopped
there, which is why `otp_expires_at` read as a credential and `email_source`
read as an email address. A token says what a word IS; it says nothing about
what the column MEANS. These helpers supply the missing half: where the token
sits, what the column's type is, and what kind of table it lives in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Table context ───────────────────────────────────────────────────────────────
# A table about people makes generic columns meaningful (`leads.title` is a job
# title); a table about business objects makes the same column meaningless
# (`campaigns.title` is a campaign's title). Context is evidence, never a verdict.

PERSON = "person"
OPERATIONAL = "operational"
UNKNOWN = "unknown"

_PERSON_TABLES = frozenset({
    "users", "user", "customers", "customer", "employees", "employee", "staff",
    "leads", "lead", "contacts", "contact", "candidates", "candidate",
    "applicants", "attendees", "attendee", "participants", "members", "member",
    "patients", "patient", "subscribers", "subscriber", "profiles", "people",
    "persons", "accounts", "recipients", "guests", "pending_signups",
    "campaign_leads", "event_attendees", "connected_inboxes",
})

_OPERATIONAL_TABLES = frozenset({
    "campaigns", "campaign", "outreach_campaigns", "events", "event", "products",
    "product", "settings", "configuration", "config", "logs", "log",
    "audit_logs", "jobs", "queues", "schedules", "migrations", "sessions",
    "templates", "workflows", "integrations", "webhooks", "tags", "categories",
})

# Tables whose rows are ABOUT a person even though the table names an artefact:
# a generated email row belongs to one lead, a transaction to one user. Their
# free-text and identifier columns therefore deserve person-adjacent treatment.
_PERSON_ADJACENT_TABLES = frozenset({
    "generated_emails", "emails", "email_events", "credit_transactions",
    "transactions", "payments", "invoices", "orders", "messages", "notes",
})


def resolve_table_context(table_name: str | None, *, has_person_relationship: bool = False) -> str:
    """PERSON / OPERATIONAL / UNKNOWN for one table.

    `has_person_relationship` comes from discovered foreign keys: a table that
    references a people table holds rows about people even when its own name
    describes an artefact. That is why `email_events.url` is behavioural data
    about a lead rather than a generic URL.
    """
    if not table_name:
        return UNKNOWN
    name = table_name.strip().lower()

    if name in _PERSON_TABLES:
        return PERSON
    if name in _PERSON_ADJACENT_TABLES:
        return PERSON if has_person_relationship else UNKNOWN
    if name in _OPERATIONAL_TABLES:
        return OPERATIONAL
    if has_person_relationship:
        return PERSON

    # Fall back to the singular/plural stem so `customer_profiles` reads as a
    # person table without needing every variant enumerated.
    stem = {t for t in re.split(r"[^a-z0-9]+", name) if t}
    if stem & _PERSON_TABLES:
        return PERSON
    if stem & _OPERATIONAL_TABLES:
        return OPERATIONAL
    return UNKNOWN


def person_tables() -> frozenset[str]:
    """Exposed so relationship resolution can ask whether an FK target is a
    people table without importing the private set."""
    return _PERSON_TABLES | _PERSON_ADJACENT_TABLES


# ── Column shape ────────────────────────────────────────────────────────────────

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
# camelCase carries no separator, so `emailAddress` would arrive as a single
# unmatched token. Insert a boundary before running the normal split.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _to_snake(raw: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", raw.strip()).lower()

# Suffixes that describe something ABOUT a value rather than being the value.
# `email_source` is where an address came from; `linkedin_url_kind` is which
# sort of URL it is. Neither is personal data on its own.
PROVENANCE_SUFFIXES = (
    "_source", "_kind", "_type", "_status", "_method", "_provider", "_origin",
    "_channel", "_format", "_version", "_flag", "_reason",
)

# Suffixes that make a column a timestamp regardless of the rest of its name.
# This is the fix for `otp_expires_at` and `token_expires_at`.
TEMPORAL_SUFFIXES = (
    "_at", "_on", "_date", "_time", "_timestamp", "_expires", "_expiry", "_until",
)

METRIC_SUFFIXES = (
    "_count", "_total", "_num", "_size", "_length", "_len", "_score", "_rate",
    "_ratio", "_percent", "_pct", "_ms", "_seconds", "_index", "_position",
)
METRIC_PREFIXES = ("num_", "count_", "total_", "avg_", "max_", "min_", "sum_")

# Past-participle suffixes that make a NUMERIC column a tally of events rather
# than the thing being tallied: `emails_sent` counts sends, it is not an email.
# Only applied to numeric types -- `clicked_at` is a timestamp, not a counter.
TALLY_SUFFIXES = (
    "_sent", "_opened", "_clicked", "_delivered", "_bounced", "_replied",
    "_failed", "_approved", "_generated", "_processed", "_skipped",
)

# Suffixes marking a column as free-form content. `full_email_text` holds a
# message body; it is personal data, but it is not a contact identifier.
CONTENT_SUFFIXES = ("_text", "_body", "_content", "_message", "_notes", "_description")

# Free-text columns can hold anything a human typed, including personal data
# nobody intended to store there. In a person table they are a review item, not
# a silent drop -- that silence is how `campaign_leads.notes` disappeared.
FREE_TEXT_NAMES = frozenset({
    "notes", "note", "comments", "comment", "description", "remarks", "message",
    "content", "body", "text", "full_text", "details", "summary", "bio",
    "about", "feedback", "reason", "answer", "response",
})

# Columns whose meaning flips entirely with table context.
CONTEXT_DEPENDENT = frozenset({
    "title", "name", "url", "location", "address", "company", "picture",
    "image", "avatar", "link", "handle", "label", "subject",
})

_TEMPORAL_TYPES = ("timestamp", "date", "time", "datetime")
_NUMERIC_TYPES = ("int", "numeric", "decimal", "float", "double", "real", "serial")
_TEXTUAL_TYPES = ("text", "char", "varchar", "citext", "string")
_BOOLEAN_TYPES = ("bool",)
_UUID_TYPES = ("uuid",)


@dataclass(frozen=True)
class ColumnShape:
    """Everything derivable from the column itself, computed once."""

    raw: str
    normalized: str
    compact: str
    tokens: frozenset[str]
    head: str                 # first token -- usually the subject of the name
    tail: str                 # last token -- usually the qualifier
    data_type: str
    is_temporal: bool
    is_numeric: bool
    is_textual: bool
    is_boolean: bool
    is_uuid: bool
    has_provenance_suffix: bool
    has_temporal_suffix: bool
    is_metric_shaped: bool
    is_tally: bool
    is_content: bool
    is_free_text_name: bool
    is_context_dependent: bool
    notes: tuple[str, ...] = field(default=())


def describe(column_name: str, data_type: str | None) -> ColumnShape:
    normalized = _to_snake(column_name)
    tokens = tuple(t for t in _TOKEN_SPLIT.split(normalized) if t)
    lowered = (data_type or "").lower()

    return ColumnShape(
        raw=column_name,
        normalized=normalized,
        compact=normalized.replace("_", ""),
        tokens=frozenset(tokens),
        head=tokens[0] if tokens else "",
        tail=tokens[-1] if tokens else "",
        data_type=lowered,
        is_temporal=any(t in lowered for t in _TEMPORAL_TYPES),
        is_numeric=any(t in lowered for t in _NUMERIC_TYPES),
        is_textual=any(t in lowered for t in _TEXTUAL_TYPES),
        is_boolean=any(t in lowered for t in _BOOLEAN_TYPES),
        is_uuid=any(t in lowered for t in _UUID_TYPES),
        has_provenance_suffix=normalized.endswith(PROVENANCE_SUFFIXES),
        has_temporal_suffix=normalized.endswith(TEMPORAL_SUFFIXES),
        is_metric_shaped=normalized.endswith(METRIC_SUFFIXES) or normalized.startswith(METRIC_PREFIXES),
        # A tally only counts when the column is actually numeric.
        is_tally=normalized.endswith(TALLY_SUFFIXES)
                 and any(t in lowered for t in _NUMERIC_TYPES),
        is_content=normalized in FREE_TEXT_NAMES or normalized.endswith(CONTENT_SUFFIXES),
        is_free_text_name=normalized in FREE_TEXT_NAMES,
        is_context_dependent=normalized in CONTEXT_DEPENDENT,
    )
