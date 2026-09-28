"""Turns a read schema into observed purposes, one per table.

Per table, not per column, because a purpose is a property of what a table is FOR.
A `customers` table holding an email column does not have an email purpose; it has a
customer-account purpose, and the email is evidence of it.

This is the input path that makes the declared-vs-observed comparison actually work.
The consent-scan path produces subjects like `fonts.gstatic.com`; declarations describe
subjects like `attendees.email`. Those two universes never intersect, so every
comparison against a web scan is `undetermined` by construction -- correct, but never
useful. A database schema produces table names, which is the same universe declarations
are written in.
"""

from __future__ import annotations

import re

from app.agents.purpose.connectors.structured import StructuredSchema, Table
from app.agents.purpose.services.observed_service import ObservedPurpose

# Agent 2's rules, used directly rather than forked. The development plan required
# reuse; on the branch this agent was first written against, app/agents/ropa/ carried
# no code, so a local copy existed with a header saying the two must be reconciled if
# the branches ever met. They have. The two fixes that copy carried -- rule ordering
# and qualified names -- are now in the shared module, covered by its own tests, and
# this file has no rules of its own.
from app.agents.ropa.rules import purpose_rules

# Column-name fragments that indicate personal data. Used to decide whether a table is
# worth assessing at all -- a table of currency codes has a purpose, but not one that
# raises a data-protection question.
#
# TWO CLASSES, BECAUSE ONE WAS MEASURABLY TOO NOISY
#
# These were all plain substrings. `ip` is a substring of `description`, `equipment`,
# `municipality`, `recipient` and `shipDate`; `name` is a substring of `filename` and
# `hostname`. Measured against a real OpenAPI spec, an `Order` schema was assessed on
# the strength of `shipDate` -- and the evidence line then told a reviewer that a
# shipping date was why the table looked personal, which is worse than not flagging it.
#
# So a hint is now either long enough to be unambiguous as a substring, or it must
# match a WHOLE token. It is still deliberately over-inclusive in the safe direction:
# `name` as a token still matches a pet's name, and that costs one extra `undetermined`
# assessment, which is the correct price.
_PERSONAL_SUBSTRINGS = (
    "email", "phone", "mobile", "address", "birth", "gender", "passport",
    "aadhaar", "ssn", "nationalid", "national_id", "postcode", "salary",
    "account_number", "card",
)

# Short or ambiguous. Matched against tokens, never as a substring.
_PERSONAL_TOKENS = (
    "ip", "zip", "name", "dob", "pan", "bank",
)

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]+")


def _tokens(column_name: str) -> set[str]:
    """Split a column name the way a reader would: `shipDate` -> {ship, date},
    `ip_address` -> {ip, address}, `first_name` -> {first, name}.

    camelCase matters as much as snake_case here -- an OpenAPI spec names properties
    `firstName` and `shipDate`, and splitting only on underscores would leave both as
    single opaque tokens.
    """
    spaced = _CAMEL.sub(" ", column_name)
    return {t.lower() for t in _NON_ALNUM.split(spaced) if t}


def holds_personal_data(table: Table) -> list[str]:
    """Column names suggesting personal data. Empty means none were recognised.

    Still imprecise, and still deliberately so in one direction only: a column called
    `contact_details` is missed entirely, and a pet's `name` is flagged. It is used
    only to decide whether to ASSESS a table, never to assert that a column holds
    personal data -- an over-inclusive filter costs a harmless extra assessment, while
    an assertion built on the same match would be a claim about someone's data.
    """
    hits = []
    for column in table.columns:
        lowered = column.name.lower()
        if any(hint in lowered for hint in _PERSONAL_SUBSTRINGS):
            hits.append(column.name)
            continue
        if _tokens(column.name) & set(_PERSONAL_TOKENS):
            hits.append(column.name)
    return hits


def observe(schema: StructuredSchema) -> list[ObservedPurpose]:
    """One observed purpose per table that plausibly holds personal data.

    Tables with no personal-data indication are skipped rather than assessed as
    `Unknown`. A hundred `undetermined` rows about lookup tables would bury the handful
    of real ones, and this agent's output is only useful if a reviewer reads it.
    """
    observed: list[ObservedPurpose] = []

    # A bare table name is only a safe alias when exactly one table in the source has
    # it. Two schemas both holding `customers` makes a declaration written against
    # `customers` ambiguous, and attributing it to both would put one organisation's
    # stated purpose on a table it was never written about.
    bare_counts: dict[str, int] = {}
    for table in schema.tables:
        bare_counts[table.name.lower()] = bare_counts.get(table.name.lower(), 0) + 1

    for table in schema.tables:
        personal_columns = holds_personal_data(table)
        if not personal_columns:
            continue

        match = purpose_rules.match_table(table.qualified)
        observed.append(ObservedPurpose(
            subject_type="table",
            subject_ref=f"{schema.source_name}:{table.qualified}",
            subject_label=table.qualified,
            # None when unrecognised. The comparison layer turns that into
            # `undetermined`, never into a guessed purpose.
            purpose=match.purpose if match else None,
            # A database table has no consent state -- consent is a property of a
            # browser interaction. Left empty so no consent finding can be raised from
            # a source that cannot evidence one.
            consent_states=[],
            vendor=None,
            # Every personal-data column that justified assessing this table, so a
            # reviewer can see what the judgement rested on.
            evidence_refs=[f"{table.qualified}.{c}" for c in personal_columns],
            lookup_aliases=(
                [table.name] if table.qualified != table.name
                and bare_counts.get(table.name.lower(), 0) == 1 else []
            ),
        ))

    return observed


def index_declared_by_table(declared: dict) -> dict:
    """Re-key declared purposes so a table-based subject can find them.

    Declarations name subjects as `attendees.email` -- table-qualified columns. An
    observed subject is the table, `attendees`. Without this, the two never match and
    every comparison is undetermined for a reason that has nothing to do with the data.

    Both the original keys and the derived table-level keys are kept: a declaration
    that genuinely names a bare table should still match directly.
    """
    extended = dict(declared)
    for key, value in declared.items():
        if "." in key:
            table = key.rsplit(".", 1)[0].strip().lower()
            # First declaration wins. Two declarations disagreeing about one table is
            # a real conflict, and silently overwriting would hide it behind whichever
            # row happened to be read last.
            if table and table not in extended:
                extended[table] = value
    return extended
