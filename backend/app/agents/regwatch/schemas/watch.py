"""Agent 5 (Regulatory Watch) vocabulary.

One module, so the strings the database constrains, the services branch on, and the
API validates are the same strings. Agents 3 and 4 do the same; the alternative is a
literal spelled three ways that drifts on the fourth.

THE VOCABULARY THAT MATTERS MOST
--------------------------------
`CONFIDENCE_LEVELS` is Agent 4's, unchanged and deliberately so. A regulatory change
raises the same kind of question an incident does -- "is this really true of us?" --
and the answer has the same four honest shapes. `MACHINE_ASSERTABLE_CONFIDENCE`
excludes CONFIRMED for the same reason: deciding that a regulation definitely applies
to this organisation is a position the organisation takes, with a name against it, and
no rule or model reaches it.
"""

from __future__ import annotations

# ── Finding lifecycle ───────────────────────────────────────────────────────────
DETECTED = "detected"
ASSESSING = "assessing"
REVIEW_REQUIRED = "review_required"
APPROVED = "approved"
DISMISSED = "dismissed"
ACTION_OPEN = "action_open"
CLOSED = "closed"
SUPERSEDED = "superseded"
FAILED = "failed"

HAPPY_PATH_STATUSES = (
    DETECTED, ASSESSING, REVIEW_REQUIRED, APPROVED, ACTION_OPEN, CLOSED,
)
EXCEPTION_STATUSES = (DISMISSED, SUPERSEDED, FAILED)
ALL_STATUSES = frozenset(HAPPY_PATH_STATUSES + EXCEPTION_STATUSES)

# Nothing leaves these. DISMISSED is terminal too: "this does not apply to us" is a
# decision a person made with a reason, and re-opening it would erase that reasoning.
# A later change to the same source produces a NEW finding rather than reviving this
# one, which keeps the record of what was decided and when.
TERMINAL_STATUSES = frozenset({CLOSED, DISMISSED, SUPERSEDED})


# ── Confidence, shared with Agent 4 ─────────────────────────────────────────────
CONFIRMED = "confirmed"
PROBABLE = "probable"
POSSIBLE = "possible"
UNKNOWN = "unknown"

CONFIDENCE_LEVELS = (CONFIRMED, PROBABLE, POSSIBLE, UNKNOWN)
CONFIDENCE_ORDER = {UNKNOWN: 0, POSSIBLE: 1, PROBABLE: 2, CONFIRMED: 3}

# What a rule or a model may assert on its own. CONFIRMED is absent on purpose.
MACHINE_ASSERTABLE_CONFIDENCE = frozenset({PROBABLE, POSSIBLE, UNKNOWN})


def at_least(level: str, minimum: str) -> bool:
    return CONFIDENCE_ORDER.get(level, 0) >= CONFIDENCE_ORDER.get(minimum, 0)


# ── Relevance ───────────────────────────────────────────────────────────────────
# Three values, not two. "Undetermined" is the honest answer when the rules could not
# decide, and it is the default -- a change is not assumed irrelevant because nothing
# matched it.
RELEVANT = "relevant"
NOT_RELEVANT = "not_relevant"
UNDETERMINED = "undetermined"
RELEVANCE_VALUES = (RELEVANT, NOT_RELEVANT, UNDETERMINED)


# ── Priority ────────────────────────────────────────────────────────────────────
PRIORITY_LOW = "low"
PRIORITY_MEDIUM = "medium"
PRIORITY_HIGH = "high"
PRIORITY_CRITICAL = "critical"
PRIORITIES = (PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH, PRIORITY_CRITICAL)


# ── Sources ─────────────────────────────────────────────────────────────────────
CONNECTOR_HTTP = "http"
CONNECTOR_RSS = "rss"
CONNECTOR_MANUAL = "manual_upload"
CONNECTORS = frozenset({CONNECTOR_HTTP, CONNECTOR_RSS, CONNECTOR_MANUAL})


# ── Collection outcomes ─────────────────────────────────────────────────────────
COLLECTION_PENDING = "pending"
COLLECTION_COLLECTING = "collecting"
COLLECTION_COLLECTED = "collected"
COLLECTION_FAILED = "failed"
COLLECTION_SKIPPED = "skipped"
COLLECTION_STATUSES = frozenset({
    COLLECTION_PENDING, COLLECTION_COLLECTING, COLLECTION_COLLECTED,
    COLLECTION_FAILED, COLLECTION_SKIPPED,
})
# The guardrail the spec ends on, as a set: a source in any of these states has NOT
# been confirmed current, and the UI must not render it as though it had.
COLLECTION_NOT_CURRENT = frozenset({
    COLLECTION_PENDING, COLLECTION_COLLECTING, COLLECTION_FAILED, COLLECTION_SKIPPED,
})


# ── Change kinds ────────────────────────────────────────────────────────────────
CHANGE_FIRST_CAPTURE = "first_capture"
CHANGE_CONTENT = "content_changed"
CHANGE_UNREACHABLE = "unreachable"
CHANGE_NONE = "no_change"
CHANGE_KINDS = frozenset({
    CHANGE_FIRST_CAPTURE, CHANGE_CONTENT, CHANGE_UNREACHABLE, CHANGE_NONE,
})
# Kinds that should produce a reviewable finding. `no_change` should not -- and
# `unreachable` SHOULD, which is the whole point: a source we could not read is a
# thing a compliance team needs to know about, not silence.
CHANGE_KINDS_RAISING_A_FINDING = frozenset({
    CHANGE_FIRST_CAPTURE, CHANGE_CONTENT, CHANGE_UNREACHABLE,
})


# ── Impact targets ──────────────────────────────────────────────────────────────
TARGET_ROPA_RECORD = "ropa_record"
TARGET_ROPA_SOURCE = "ropa_data_source"
TARGET_CONSENT_FINDING = "consent_finding"
TARGET_CONSENT_WEBSITE = "consent_website"
TARGET_DSR_CONFIG = "dsr_configuration"
TARGET_INCIDENT = "incident_case"
TARGET_POLICY = "policy"
TARGET_CONTROL = "control"
TARGET_OTHER = "other"
IMPACT_TARGET_KINDS = frozenset({
    TARGET_ROPA_RECORD, TARGET_ROPA_SOURCE, TARGET_CONSENT_FINDING,
    TARGET_CONSENT_WEBSITE, TARGET_DSR_CONFIG, TARGET_INCIDENT,
    TARGET_POLICY, TARGET_CONTROL, TARGET_OTHER,
})

DERIVED_RULE = "rule"
DERIVED_ROPA = "ropa_metadata"
DERIVED_MODEL = "model"
DERIVED_MANUAL = "manual"
IMPACT_DERIVATIONS = frozenset({DERIVED_RULE, DERIVED_ROPA, DERIVED_MODEL, DERIVED_MANUAL})


# ── Review decisions ────────────────────────────────────────────────────────────
DECISION_APPROVE = "approved"
DECISION_REJECT = "rejected"
DECISION_EDIT = "edited"
DECISION_DISMISS = "dismissed"
DECISION_MORE_INFO = "request_more_information"
DECISION_ESCALATE = "escalated"
DECISIONS = frozenset({
    DECISION_APPROVE, DECISION_REJECT, DECISION_EDIT, DECISION_DISMISS,
    DECISION_MORE_INFO, DECISION_ESCALATE,
})
# A decision that closes something down has to say why.
DECISIONS_REQUIRING_REASON = frozenset({
    DECISION_REJECT, DECISION_DISMISS, DECISION_EDIT, DECISION_ESCALATE,
})

APPROVAL_SUBJECTS = frozenset({"finding", "baseline", "action", "impact"})


# ── Action status ───────────────────────────────────────────────────────────────
ACTION_OPEN_STATUS = "open"
ACTION_IN_PROGRESS = "in_progress"
ACTION_COMPLETED = "completed"
ACTION_CANCELLED = "cancelled"
ACTION_BLOCKED = "blocked"
ACTION_STATUSES = frozenset({
    ACTION_OPEN_STATUS, ACTION_IN_PROGRESS, ACTION_COMPLETED,
    ACTION_CANCELLED, ACTION_BLOCKED,
})


# ── Error codes ─────────────────────────────────────────────────────────────────
# UPPERCASE, matching the convention Agent 4 settled on -- a lowercase code in the
# frontend was a real bug there, so the spelling is fixed in one place here.
ERR_SOURCE_NOT_AUTHORIZED = "SOURCE_NOT_AUTHORIZED"
ERR_SOURCE_UNREACHABLE = "SOURCE_UNREACHABLE"
ERR_COLLECTION_FAILED = "COLLECTION_FAILED"
ERR_CONTENT_UNUSABLE = "CONTENT_UNUSABLE"
ERR_NO_BASELINE = "NO_BASELINE"
ERR_ASSESSMENT_FAILED = "ASSESSMENT_FAILED"
ERR_RELEVANCE_UNDETERMINED = "RELEVANCE_UNDETERMINED"
ERR_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
ERR_INVALID_SOURCE = "INVALID_SOURCE"
ERROR_CODES = frozenset({
    ERR_SOURCE_NOT_AUTHORIZED, ERR_SOURCE_UNREACHABLE, ERR_COLLECTION_FAILED,
    ERR_CONTENT_UNUSABLE, ERR_NO_BASELINE, ERR_ASSESSMENT_FAILED,
    ERR_RELEVANCE_UNDETERMINED, ERR_APPROVAL_REQUIRED, ERR_INVALID_SOURCE,
})


# ── Audit actions ───────────────────────────────────────────────────────────────
AUDIT_ENTITY = "regwatch_finding"
AUDIT_SOURCE_ENTITY = "regwatch_source"

AUDIT_SOURCE_REGISTERED = "regwatch.source_registered"
AUDIT_SOURCE_UPDATED = "regwatch.source_updated"
AUDIT_SOURCE_DISABLED = "regwatch.source_disabled"
AUDIT_COLLECTION_STARTED = "regwatch.collection_started"
AUDIT_COLLECTION_SUCCEEDED = "regwatch.collection_succeeded"
AUDIT_COLLECTION_FAILED = "regwatch.collection_failed"
AUDIT_CHANGE_DETECTED = "regwatch.change_detected"
AUDIT_FINDING_CREATED = "regwatch.finding_created"
AUDIT_RELEVANCE_ASSESSED = "regwatch.relevance_assessed"
AUDIT_IMPACT_MAPPED = "regwatch.impact_mapped"
AUDIT_INTERPRETED = "regwatch.interpreted"
AUDIT_REVIEW_REQUESTED = "regwatch.review_requested"
AUDIT_APPROVED = "regwatch.approved"
AUDIT_DISMISSED = "regwatch.dismissed"
AUDIT_BASELINE_ADVANCED = "regwatch.baseline_advanced"
AUDIT_ACTION_CREATED = "regwatch.action_created"
AUDIT_ACTION_COMPLETED = "regwatch.action_completed"
AUDIT_CLOSED = "regwatch.closed"
AUDIT_STATUS_CHANGED = "regwatch.status_changed"
