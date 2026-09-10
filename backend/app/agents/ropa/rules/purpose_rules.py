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
    PurposeRule(
        rule_id="PR-003",
        purpose="Employee Administration",
        data_subject="Employee",
        exact=("employees", "employee", "staff", "hr_records", "employment"),
        tokens=("employee", "staff"),
    ),
    PurposeRule(
        rule_id="PR-004",
        purpose="Payroll / Compensation",
        data_subject="Employee",
        exact=("payroll", "salaries", "compensation", "payslips"),
        tokens=("payroll", "payslip", "salary"),
    ),
    PurposeRule(
        rule_id="PR-005",
        purpose="Recruitment",
        data_subject="Candidate",
        exact=("candidates", "applicants", "applications", "job_applications", "resumes"),
        tokens=("candidate", "applicant", "resume"),
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
        rule_id="PR-007",
        purpose="Payment Processing",
        data_subject="Customer",
        exact=("payments", "payment", "orders", "order", "invoices", "invoice",
               "transactions", "transaction", "billing", "subscriptions"),
        tokens=("payment", "invoice", "transaction", "billing", "order"),
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
        rule_id="PR-010",
        purpose="Support / Ticketing",
        data_subject="Customer",
        exact=("tickets_support", "support_tickets", "complaints", "enquiries",
               "inquiries", "contact_requests", "feedback"),
        tokens=("complaint", "enquiry", "inquiry", "feedback"),
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
    applies. None means "Unknown, needs human review" -- never a guess."""
    normalized = table_name.strip().lower()

    for rule in RULES:
        if normalized in rule.exact:
            return PurposeMatch(rule.rule_id, rule.purpose, rule.data_subject,
                                CONFIDENCE_EXACT, normalized)

    tokens = {t for t in _TOKEN_SPLIT.split(normalized) if t}
    # Singularize a trailing "s" so "event_attendees" matches the "attendee" token.
    tokens |= {t[:-1] for t in tokens if t.endswith("s") and len(t) > 3}

    for rule in RULES:
        hit = tokens & set(rule.tokens)
        if hit:
            return PurposeMatch(rule.rule_id, rule.purpose, rule.data_subject,
                                CONFIDENCE_TOKEN, sorted(hit)[0])
    return None
