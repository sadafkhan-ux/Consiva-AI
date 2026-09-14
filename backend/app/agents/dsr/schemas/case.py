"""The DSR case vocabulary: every status, request type, operation and error code the
agent is allowed to use, in one place.

These are plain module constants rather than Python enums because they are written
to `text` columns with CHECK constraints (migrations/0011_dsr_agent.sql) and read
back as strings. Keeping both sides on the same literal strings means a typo is a
test failure here, not a constraint violation in production -- `test_dsr_lifecycle`
asserts these sets match the migration's CHECK lists exactly.
"""

from __future__ import annotations

from typing import Literal

# ── Case status (prompt §12) ─────────────────────────────────────────────────────
RECEIVED = "received"
IDENTITY_PENDING = "identity_pending"
IDENTITY_VERIFIED = "identity_verified"
CLASSIFIED = "classified"
SEARCHING = "searching"
SEARCH_COMPLETED = "search_completed"
REVIEW_REQUIRED = "review_required"
APPROVAL_REQUIRED = "approval_required"
APPROVED = "approved"
EXECUTING = "executing"
EXECUTION_VERIFIED = "execution_verified"
RESPONSE_PENDING = "response_pending"
COMPLETED = "completed"

# Exception states.
FAILED = "failed"
REJECTED = "rejected"
PARTIALLY_COMPLETED = "partially_completed"
ESCALATED = "escalated"
CANCELLED = "cancelled"
EXPIRED = "expired"

HAPPY_PATH_STATUSES = (
    RECEIVED, IDENTITY_PENDING, IDENTITY_VERIFIED, CLASSIFIED, SEARCHING,
    SEARCH_COMPLETED, REVIEW_REQUIRED, APPROVAL_REQUIRED, APPROVED, EXECUTING,
    EXECUTION_VERIFIED, RESPONSE_PENDING, COMPLETED,
)
EXCEPTION_STATUSES = (FAILED, REJECTED, PARTIALLY_COMPLETED, ESCALATED, CANCELLED, EXPIRED)
ALL_STATUSES = frozenset(HAPPY_PATH_STATUSES + EXCEPTION_STATUSES)

# A case in one of these is finished. Nothing may transition out of them, and the
# SLA sweep stops counting against them.
TERMINAL_STATUSES = frozenset({COMPLETED, REJECTED, CANCELLED, EXPIRED, PARTIALLY_COMPLETED})

# ── Request types (§14) ──────────────────────────────────────────────────────────
UNCLASSIFIED = "unclassified"
ACCESS = "access"
CORRECTION = "correction"
DELETION = "deletion"
EXPORT = "export"
INFORMATION = "information"
OTHER = "other"

REQUEST_TYPES = frozenset({UNCLASSIFIED, ACCESS, CORRECTION, DELETION, EXPORT, INFORMATION, OTHER})
# The types the classifier may choose. `unclassified` is the pre-classification
# default and is never a classification OUTCOME -- a classifier that cannot decide
# returns `other` with low confidence, which routes to human review rather than
# leaving the case looking unprocessed.
CLASSIFIABLE_TYPES = frozenset({ACCESS, CORRECTION, DELETION, EXPORT, INFORMATION, OTHER})

RequestType = Literal["access", "correction", "deletion", "export", "information", "other"]

# Types that change data at the source, and therefore always require an approved
# action plan before anything is executed.
MUTATING_TYPES = frozenset({CORRECTION, DELETION})

# ── Identity verification (§13) ──────────────────────────────────────────────────
IDV_PENDING = "pending"
IDV_IN_PROGRESS = "in_progress"
IDV_VERIFIED = "verified"
IDV_FAILED = "failed"
IDV_EXPIRED = "expired"
IDV_MANUALLY_VERIFIED = "manually_verified"

IDV_STATUSES = frozenset({
    IDV_PENDING, IDV_IN_PROGRESS, IDV_VERIFIED, IDV_FAILED, IDV_EXPIRED, IDV_MANUALLY_VERIFIED,
})
# The only two states that let a sensitive search run.
IDV_SATISFIED = frozenset({IDV_VERIFIED, IDV_MANUALLY_VERIFIED})

IDV_METHODS = frozenset({"email_challenge", "manual", "external"})

# ── Action operations (§21) ──────────────────────────────────────────────────────
OP_DISCLOSE = "disclose"
OP_UPDATE_FIELD = "update_field"
OP_ANONYMIZE_FIELD = "anonymize_field"
OP_DELETE_RECORD = "delete_record"
OP_RETAIN = "retain"
OP_NO_OP = "no_op"

OPERATIONS = frozenset({
    OP_DISCLOSE, OP_UPDATE_FIELD, OP_ANONYMIZE_FIELD, OP_DELETE_RECORD, OP_RETAIN, OP_NO_OP,
})
# Operations that write to the customer's source. These are the only ones that need
# a write credential, an approval, and post-execution verification.
MUTATING_OPERATIONS = frozenset({OP_UPDATE_FIELD, OP_ANONYMIZE_FIELD, OP_DELETE_RECORD})

# ── Requester selections (email-first flow) ──────────────────────────────────────
# What a person chooses for ONE discovered record, as opposed to `request_type`,
# which is what they asked for overall. The two are different questions: someone can
# open a case saying "delete my data" and then, looking at what was actually found,
# decide to keep their order history. The selection wins, because it is the more
# specific and more recent statement of what they want.
#
# These are UI-facing verbs deliberately kept separate from the OPERATIONS the engine
# executes, so the wording shown to a person can change without touching the
# execution vocabulary the connector and the audit trail depend on.
SELECT_DELETE = "delete"
SELECT_KEEP = "keep"
SELECT_CORRECT = "correct"
SELECT_EXPORT = "export"
SELECT_REVIEW = "review"

SELECTIONS = frozenset({SELECT_DELETE, SELECT_KEEP, SELECT_CORRECT, SELECT_EXPORT, SELECT_REVIEW})

OPERATION_FOR_SELECTION = {
    SELECT_DELETE: OP_DELETE_RECORD,
    SELECT_KEEP: OP_RETAIN,
    SELECT_CORRECT: OP_UPDATE_FIELD,
    SELECT_EXPORT: OP_DISCLOSE,
    # "Have a human look at this before deciding" is not an operation against the
    # source -- it is an explicit refusal to act yet, which is why it maps to no_op
    # and still demands approval.
    SELECT_REVIEW: OP_NO_OP,
}

# Selections that always need a human decision before anything proceeds, regardless
# of what the constraint engine says.
SELECTIONS_REQUIRING_REVIEW = frozenset({SELECT_DELETE, SELECT_CORRECT, SELECT_REVIEW})


# ── Domain error codes (§38) ─────────────────────────────────────────────────────
# Every non-happy outcome lands on one of these. A case that stopped without one of
# these in `error_code` is a bug -- see lifecycle.fail(), which requires it.
ERR_IDENTITY_REQUIRED = "IDENTITY_REQUIRED"
ERR_IDENTITY_FAILED = "IDENTITY_FAILED"
ERR_SOURCE_NOT_AUTHORIZED = "SOURCE_NOT_AUTHORIZED"
ERR_CONNECTOR_UNAVAILABLE = "CONNECTOR_UNAVAILABLE"
ERR_CONNECTOR_TIMEOUT = "CONNECTOR_TIMEOUT"
ERR_SEARCH_FAILED = "SEARCH_FAILED"
ERR_NO_MATCH = "NO_MATCH"
ERR_MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
ERR_POLICY_REVIEW_REQUIRED = "POLICY_REVIEW_REQUIRED"
ERR_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
ERR_ACTION_BLOCKED = "ACTION_BLOCKED"
ERR_ACTION_FAILED = "ACTION_FAILED"
ERR_ACTION_PARTIAL = "ACTION_PARTIAL"
ERR_VERIFICATION_FAILED = "VERIFICATION_FAILED"
ERR_SLA_BREACH = "SLA_BREACH"

ERROR_CODES = frozenset({
    ERR_IDENTITY_REQUIRED, ERR_IDENTITY_FAILED, ERR_SOURCE_NOT_AUTHORIZED,
    ERR_CONNECTOR_UNAVAILABLE, ERR_CONNECTOR_TIMEOUT, ERR_SEARCH_FAILED, ERR_NO_MATCH,
    ERR_MULTIPLE_MATCHES, ERR_POLICY_REVIEW_REQUIRED, ERR_APPROVAL_REQUIRED,
    ERR_ACTION_BLOCKED, ERR_ACTION_FAILED, ERR_ACTION_PARTIAL, ERR_VERIFICATION_FAILED,
    ERR_SLA_BREACH,
})

# ── Audit actions (§35) ──────────────────────────────────────────────────────────
# Written through the SHARED audit_service with entity_type="dsr_request", so the
# whole case timeline is one indexed query against audit_logs.
AUDIT_ENTITY = "dsr_request"

AUDIT_CREATED = "dsr.created"
AUDIT_IDENTITY_STARTED = "dsr.identity_verification_started"
AUDIT_IDENTITY_VERIFIED = "dsr.identity_verified"
AUDIT_IDENTITY_FAILED = "dsr.identity_failed"
AUDIT_CLASSIFIED = "dsr.request_classified"
AUDIT_SEARCH_STARTED = "dsr.search_started"
AUDIT_SEARCH_COMPLETED = "dsr.search_completed"
AUDIT_RESULTS_FOUND = "dsr.results_found"
AUDIT_REVIEW_REQUESTED = "dsr.review_requested"
AUDIT_ACTION_PLANNED = "dsr.action_planned"
AUDIT_APPROVED = "dsr.approved"
AUDIT_REJECTED = "dsr.rejected"
AUDIT_ACTION_STARTED = "dsr.action_started"
AUDIT_ACTION_EXECUTED = "dsr.action_executed"
AUDIT_ACTION_VERIFIED = "dsr.action_verified"
AUDIT_RESPONSE_GENERATED = "dsr.response_generated"
AUDIT_RESPONSE_SENT = "dsr.response_sent"
AUDIT_COMPLETED = "dsr.completed"
AUDIT_FAILED = "dsr.failed"
AUDIT_ESCALATED = "dsr.escalated"
AUDIT_PARTIALLY_COMPLETED = "dsr.partially_completed"
AUDIT_STATUS_CHANGED = "dsr.status_changed"
AUDIT_SLA_BREACHED = "dsr.sla_breached"
