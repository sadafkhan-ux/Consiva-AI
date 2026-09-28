"""Deterministic table -> (processing purpose, data subject) rules.

The ROPA prompt is explicit (§7, §8): a purpose or data subject that cannot be
established from evidence must NOT be invented. So this module deliberately has
no fallback guess -- `match_table()` returns None for anything it doesn't
recognize, and the caller is responsible for emitting "Unknown" with
review_required=True rather than picking the nearest-looking option.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONFIDENCE_EXACT = 0.9
CONFIDENCE_TOKEN = 0.7


@dataclass(frozen=True)
class PurposeRule:
    rule_id: str
    purpose: str
    data_subject: str | None
    exact: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()


RULES: tuple[PurposeRule, ...] = (
    # ORDERED BY SPECIFICITY, and the order is load-bearing.
    #
    # `match_table` returns on the FIRST rule whose tokens intersect the name, so a
    # broad rule placed early swallows the narrow one. Measured before this was fixed:
    # `customer_payments` and `demo_customer.orders` both came back "Customer Account
    # Management", because the customer rule was first and `customer` is a token in
    # both. A payments table is a payments table; recording it in a processing register
    # as account management misstates the purpose, which is the one thing this module
    # exists not to do.
    #
    # So: anything qualified by WHAT IS DONE (payroll, payments, recruitment, support)
    # precedes anything qualified by WHOSE data it is (employee, customer, user), and
    # the two most generic subject rules come last.
    #
    # Exact matches are unaffected by order -- they are checked in a separate pass
    # first, and no two rules claim the same exact name.
    PurposeRule(
        rule_id="PR-004",
        purpose="Payroll / Compensation",
        data_subject="Employee",
        exact=("payroll", "salaries", "compensation", "payslips"),
        # "salaries" is listed as a token as well as an exact name because the
        # singulariser strips one trailing "s" and cannot bridge y -> ies: "salaries"
        # becomes "salarie", which never meets "salary". Without it `employee_salaries`
        # falls through to the employee rule and a payroll table is recorded as general
        # employee administration. Real stemming would fix this case and break
        # "address" and "status", so the irregular plural is spelled out instead.
        tokens=("payroll", "payslip", "salary", "salaries", "compensation"),
    ),
    PurposeRule(
        rule_id="PR-005",
        purpose="Recruitment",
        data_subject="Candidate",
        exact=("candidates", "applicants", "applications", "job_applications", "resumes"),
        tokens=("candidate", "applicant", "resume"),
    ),
    PurposeRule(
        rule_id="PR-007",
        purpose="Payment Processing",
        data_subject="Customer",
        exact=("payments", "payment", "orders", "order", "invoices", "invoice",
               "transactions", "transaction", "billing", "subscriptions"),
        tokens=("payment", "invoice", "transaction", "billing", "order"),
    ),
    PurposeRule(
        rule_id="PR-010",
        purpose="Support / Ticketing",
        data_subject="Customer",
        exact=("tickets_support", "support_tickets", "complaints", "enquiries",
               "inquiries", "contact_requests", "feedback"),
        # "support" and "helpdesk" qualify the table explicitly; "ticket" deliberately
        # does NOT appear here. A bare `tickets` table is genuinely ambiguous -- event
        # tickets and support tickets are both ordinary meanings -- and PR-006 already
        # claims that exact name. Claiming it here too would make the answer depend on
        # rule order rather than on evidence.
        tokens=("complaint", "enquiry", "inquiry", "feedback", "support", "helpdesk"),
    ),
    PurposeRule(
        rule_id="PR-006",
        purpose="Event Attendee Management",
        data_subject="Attendee",
        exact=("attendees", "attendee", "registrations", "registration", "bookings",
               "booking", "tickets", "ticket", "rsvps", "guest_list", "participants"),
        tokens=("attendee", "registration", "booking", "ticket", "rsvp", "participant"),
    ),
    PurposeRule(
        rule_id="PR-008",
        purpose="Marketing Communications",
        data_subject="Prospect",
        exact=("leads", "lead", "subscribers", "subscriber", "newsletter",
               "newsletter_subscribers", "campaigns", "marketing_contacts", "prospects"),
        tokens=("lead", "subscriber", "newsletter", "campaign", "prospect"),
    ),
    PurposeRule(
        rule_id="PR-009",
        purpose="Vendor / Supplier Management",
        data_subject="Vendor Contact",
        exact=("vendors", "vendor", "suppliers", "supplier", "partners", "processors"),
        tokens=("vendor", "supplier", "partner"),
    ),
    PurposeRule(
        rule_id="PR-003",
        purpose="Employee Administration",
        data_subject="Employee",
        exact=("employees", "employee", "staff", "hr_records", "employment"),
        tokens=("employee", "staff"),
    ),
    PurposeRule(
        rule_id="PR-001",
        purpose="Customer Account Management",
        data_subject="Customer",
        exact=("customers", "customer", "clients", "client", "customer_profiles"),
        tokens=("customer", "client"),
    ),
    PurposeRule(
        rule_id="PR-002",
        purpose="User Account Management",
        data_subject="User",
        exact=("users", "user", "accounts", "account", "profiles", "user_profiles", "members"),
        tokens=("user", "account", "profile", "member"),
    ),
)

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class PurposeMatch:
    rule_id: str
    purpose: str
    data_subject: str | None
    confidence: float
    matched_on: str


def match_table(table_name: str) -> PurposeMatch | None:
    """Map a table name to a purpose + data subject, or None when no rule
    applies. None means "Unknown, needs human review" -- never a guess.

    Accepts a bare name, `schema.table`, or `table.column`. Qualified names are not a
    curiosity: a structured source reports `demo_customer.orders`, and matching that
    whole string as one token put `customer` in the token set and classified an orders
    table as customer account management.
    """
    cleaned = (table_name or "").strip().lower()
    if not cleaned:
        return None

    parts = [p for p in cleaned.split(".") if p]
    if len(parts) >= 2:
        # Ambiguous by construction: `a.b` is either schema.table or table.column, and
        # nothing in the string says which. Try the exact-match pass on both halves
        # before falling back to tokens, so a definite hit on either wins over a
        # token coincidence in the other.
        for candidate in (parts[-1], parts[-2]):
            hit = _match_exact(candidate)
            if hit is not None:
                return hit
        for candidate in (parts[-1], parts[-2]):
            hit = _match_tokens(candidate)
            if hit is not None:
                return hit
        return None

    return _match_exact(cleaned) or _match_tokens(cleaned)


def _singular(token: str) -> str:
    """Crude de-pluralisation, deliberately.

    Strips one trailing "s" from a word long enough for that to mean something, and
    never from a word ending "ss" -- that guard is what keeps "address" intact, where
    real stemming would fold it to "addres".

    It is not general. "status" still becomes "statu", because it ends "us" and not
    "ss". That is left alone rather than special-cased: the output only matters when it
    collides with a rule token, no rule claims "statu", and a list of hand-patched
    irregular words is a thing nobody maintains. Proper stemming would fix this case
    and break others, which is the trade this function exists to refuse.
    """
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _match_exact(name: str) -> PurposeMatch | None:
    for rule in RULES:
        if name in rule.exact:
            return PurposeMatch(rule.rule_id, rule.purpose, rule.data_subject,
                                CONFIDENCE_EXACT, name)
    return None


def _match_tokens(name: str) -> PurposeMatch | None:
    tokens = {_singular(t) for t in _TOKEN_SPLIT.split(name) if t}
    for rule in RULES:
        hit = tokens & {_singular(tok) for tok in rule.tokens}
        if hit:
            return PurposeMatch(rule.rule_id, rule.purpose, rule.data_subject,
                                CONFIDENCE_TOKEN, min(hit))
    return None
