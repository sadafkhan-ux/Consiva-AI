"""The incident vocabulary: every status, type, severity, confidence level and error
code Agent 4 is allowed to use, in one place.

Plain module constants rather than Python enums, matching Agents 2 and 3: these are
written to `text` columns with CHECK constraints and read back as strings, so keeping
both sides on the same literals makes a typo a test failure here rather than a
constraint violation in production.

THE CONFIDENCE VOCABULARY IS THE POINT OF THIS AGENT
----------------------------------------------------
Agent 3 deals in facts: a record either matched or it did not. An incident does not
work that way. At the moment someone raises "we think customer data was accessed",
almost nothing is known, and the entire value of the agent is in NOT flattening that
uncertainty into a yes or a no. So every substantive finding -- was personal data
involved, how many people, was it exfiltrated -- carries a confidence level, and the
system is built so that "probable" can never quietly become "confirmed" just because
it was written down.
"""

from __future__ import annotations

# ── Incident status (prompt §32) ────────────────────────────────────────────────
REPORTED = "reported"
VALIDATING = "validating"
INVESTIGATING = "investigating"
IMPACT_ASSESSMENT = "impact_assessment"
RISK_ASSESSMENT = "risk_assessment"
REVIEW_REQUIRED = "review_required"
RESPONSE_PENDING = "response_pending"
APPROVAL_REQUIRED = "approval_required"
APPROVED = "approved"
RESPONDING = "responding"
VERIFYING = "verifying"
COMMUNICATION_PENDING = "communication_pending"
CLOSURE_REVIEW = "closure_review"
CLOSED = "closed"

# Exception states.
REJECTED = "rejected"
FAILED = "failed"
ESCALATED = "escalated"
PARTIALLY_COMPLETED = "partially_completed"
CANCELLED = "cancelled"

HAPPY_PATH_STATUSES = (
    REPORTED, VALIDATING, INVESTIGATING, IMPACT_ASSESSMENT, RISK_ASSESSMENT,
    REVIEW_REQUIRED, RESPONSE_PENDING, APPROVAL_REQUIRED, APPROVED, RESPONDING,
    VERIFYING, COMMUNICATION_PENDING, CLOSURE_REVIEW, CLOSED,
)
EXCEPTION_STATUSES = (REJECTED, FAILED, ESCALATED, PARTIALLY_COMPLETED, CANCELLED)
ALL_STATUSES = frozenset(HAPPY_PATH_STATUSES + EXCEPTION_STATUSES)

# An incident in one of these is finished. `rejected` means the alert was investigated
# and found not to be an incident -- which is a real, valuable outcome, not a failure.
TERMINAL_STATUSES = frozenset({CLOSED, REJECTED, CANCELLED, PARTIALLY_COMPLETED})

# ── Incident types (§9) ─────────────────────────────────────────────────────────
TYPE_UNCLASSIFIED = "unclassified"
TYPE_UNAUTHORIZED_ACCESS = "unauthorized_access"
TYPE_DATA_EXPOSURE = "data_exposure"
TYPE_DATA_LEAKAGE = "data_leakage"
TYPE_CREDENTIAL_COMPROMISE = "credential_compromise"
TYPE_MALWARE = "malware_ransomware"
TYPE_ACCIDENTAL_DISCLOSURE = "accidental_disclosure"
TYPE_LOST_DEVICE = "lost_stolen_device"
TYPE_THIRD_PARTY = "third_party_incident"
TYPE_MISCONFIGURATION = "misconfiguration"
TYPE_INSIDER = "insider_incident"
TYPE_OTHER = "other"

INCIDENT_TYPES = frozenset({
    TYPE_UNCLASSIFIED, TYPE_UNAUTHORIZED_ACCESS, TYPE_DATA_EXPOSURE, TYPE_DATA_LEAKAGE,
    TYPE_CREDENTIAL_COMPROMISE, TYPE_MALWARE, TYPE_ACCIDENTAL_DISCLOSURE,
    TYPE_LOST_DEVICE, TYPE_THIRD_PARTY, TYPE_MISCONFIGURATION, TYPE_INSIDER, TYPE_OTHER,
})
# `unclassified` is the pre-classification default, never a classification OUTCOME --
# a classifier that cannot decide returns `other`, which routes to human review rather
# than leaving an incident looking untriaged.
CLASSIFIABLE_TYPES = INCIDENT_TYPES - {TYPE_UNCLASSIFIED}

# ── Severity (§10) ──────────────────────────────────────────────────────────────
SEV_LOW = "low"
SEV_MEDIUM = "medium"
SEV_HIGH = "high"
SEV_CRITICAL = "critical"

SEVERITIES = (SEV_LOW, SEV_MEDIUM, SEV_HIGH, SEV_CRITICAL)
SEVERITY_ORDER = {SEV_LOW: 0, SEV_MEDIUM: 1, SEV_HIGH: 2, SEV_CRITICAL: 3}

# ── Confidence (§12) ────────────────────────────────────────────────────────────
# The distinction the whole agent is arranged around. Ordered so code can ask
# "is this at least probable?" without hard-coding comparisons.
CONFIRMED = "confirmed"
PROBABLE = "probable"
POSSIBLE = "possible"
UNKNOWN = "unknown"

CONFIDENCE_LEVELS = (CONFIRMED, PROBABLE, POSSIBLE, UNKNOWN)
CONFIDENCE_ORDER = {UNKNOWN: 0, POSSIBLE: 1, PROBABLE: 2, CONFIRMED: 3}

# Only a human may assert CONFIRMED. No rule, model or heuristic promotes a finding to
# confirmed on its own -- "we are certain personal data was taken" is a statement an
# organisation makes, with a name against it, not one a system infers.
MACHINE_ASSERTABLE_CONFIDENCE = frozenset({PROBABLE, POSSIBLE, UNKNOWN})


def at_least(level: str, minimum: str) -> bool:
    """Whether `level` is at least as certain as `minimum`."""
    return CONFIDENCE_ORDER.get(level, 0) >= CONFIDENCE_ORDER.get(minimum, 0)


# ── Incident sources (§7) ───────────────────────────────────────────────────────
SOURCE_MANUAL = "manual"
SOURCE_SECURITY_ALERT = "security_alert"
SOURCE_SIEM = "siem"
SOURCE_APPLICATION_MONITORING = "application_monitoring"
SOURCE_DATABASE_MONITORING = "database_monitoring"
SOURCE_ACCESS_ANOMALY = "access_anomaly"
SOURCE_EMPLOYEE_REPORT = "employee_report"
SOURCE_VENDOR_NOTIFICATION = "vendor_notification"
SOURCE_CUSTOMER_COMPLAINT = "customer_complaint"
SOURCE_SECURITY_TEAM = "security_team"
SOURCE_INTEGRATION = "authorized_integration"

INCIDENT_SOURCES = frozenset({
    SOURCE_MANUAL, SOURCE_SECURITY_ALERT, SOURCE_SIEM, SOURCE_APPLICATION_MONITORING,
    SOURCE_DATABASE_MONITORING, SOURCE_ACCESS_ANOMALY, SOURCE_EMPLOYEE_REPORT,
    SOURCE_VENDOR_NOTIFICATION, SOURCE_CUSTOMER_COMPLAINT, SOURCE_SECURITY_TEAM,
    SOURCE_INTEGRATION,
})

# ── Evidence (§11) ──────────────────────────────────────────────────────────────
EV_SECURITY_LOG = "security_log"
EV_ACCESS_LOG = "access_log"
EV_AUTH_EVENT = "authentication_event"
EV_DATABASE_EVENT = "database_event"
EV_APPLICATION_LOG = "application_log"
EV_SYSTEM_ALERT = "system_alert"
EV_INCIDENT_REPORT = "incident_report"
EV_SOURCE_METADATA = "source_metadata"
EV_ROPA_CONTEXT = "ropa_context"
EV_EXTERNAL = "external_evidence"
EV_OBSERVATION = "human_observation"

EVIDENCE_KINDS = frozenset({
    EV_SECURITY_LOG, EV_ACCESS_LOG, EV_AUTH_EVENT, EV_DATABASE_EVENT,
    EV_APPLICATION_LOG, EV_SYSTEM_ALERT, EV_INCIDENT_REPORT, EV_SOURCE_METADATA,
    EV_ROPA_CONTEXT, EV_EXTERNAL, EV_OBSERVATION,
})

# Evidence the SYSTEM produced about itself (a ROPA lookup, source metadata) as
# opposed to evidence about the outside world. Kept distinct because a finding
# supported only by our own metadata is not corroborated by anything that happened.
DERIVED_EVIDENCE_KINDS = frozenset({EV_ROPA_CONTEXT, EV_SOURCE_METADATA})

# ── Affected systems (§14) ──────────────────────────────────────────────────────
SYS_APPLICATION = "application"
SYS_DATABASE = "database"
SYS_API = "api"
SYS_CLOUD_SERVICE = "cloud_service"
SYS_STORAGE = "storage"
SYS_SERVER = "server"
SYS_VENDOR = "vendor"
SYS_OTHER = "other"

SYSTEM_KINDS = frozenset({
    SYS_APPLICATION, SYS_DATABASE, SYS_API, SYS_CLOUD_SERVICE, SYS_STORAGE,
    SYS_SERVER, SYS_VENDOR, SYS_OTHER,
})

# ── Response actions (§21) ──────────────────────────────────────────────────────
# Two kinds, and the split is the honest part of this agent.
#
# TRACKED actions are work a person does in a system Consiva has no connector to --
# disabling an account in Active Directory, rotating a credential, isolating a
# service. Consiva assigns, approves, tracks and records them, and a human attests
# what they actually did. It does NOT pretend to perform them (§50).
#
# EXECUTABLE actions are the narrow case where the action genuinely is a database
# operation on a source already authorized for DSR, and Agent 3's connector performs
# it for real with the same read-back verification.
ACT_DISABLE_ACCOUNT = "disable_account"
ACT_ROTATE_CREDENTIAL = "rotate_credential"
ACT_ISOLATE_SERVICE = "isolate_service"
ACT_PRESERVE_LOGS = "preserve_logs"
ACT_REVOKE_SESSION = "revoke_session"
ACT_PATCH_MISCONFIGURATION = "patch_misconfiguration"
ACT_INVESTIGATE = "investigate"
ACT_NOTIFY_INTERNAL = "notify_internal"
ACT_REVIEW_NOTIFICATION_DUTY = "review_notification_duty"
ACT_OTHER = "other_containment"

ACTION_KINDS = frozenset({
    ACT_DISABLE_ACCOUNT, ACT_ROTATE_CREDENTIAL, ACT_ISOLATE_SERVICE, ACT_PRESERVE_LOGS,
    ACT_REVOKE_SESSION, ACT_PATCH_MISCONFIGURATION, ACT_INVESTIGATE,
    ACT_NOTIFY_INTERNAL, ACT_REVIEW_NOTIFICATION_DUTY, ACT_OTHER,
})

EXECUTION_MODE_TRACKED = "tracked"
EXECUTION_MODE_CONNECTOR = "connector"
EXECUTION_MODES = frozenset({EXECUTION_MODE_TRACKED, EXECUTION_MODE_CONNECTOR})

# Actions that always need approval before anyone acts, because they are disruptive,
# irreversible, or visible outside the organisation.
HIGH_IMPACT_ACTIONS = frozenset({
    ACT_DISABLE_ACCOUNT, ACT_ROTATE_CREDENTIAL, ACT_ISOLATE_SERVICE,
    ACT_PATCH_MISCONFIGURATION,
})

ACTION_STATUSES = frozenset({
    "proposed", "approved", "rejected", "blocked", "in_progress", "completed",
    "failed", "skipped",
})

# ── Communications (§26) ────────────────────────────────────────────────────────
COMM_INTERNAL = "internal"
COMM_MANAGEMENT = "management"
COMM_PRIVACY_TEAM = "privacy_team"
COMM_AFFECTED_INDIVIDUAL = "affected_individual"
COMM_CUSTOMER = "customer"
COMM_VENDOR = "vendor"
COMM_REGULATOR = "regulator"

COMMUNICATION_AUDIENCES = frozenset({
    COMM_INTERNAL, COMM_MANAGEMENT, COMM_PRIVACY_TEAM, COMM_AFFECTED_INDIVIDUAL,
    COMM_CUSTOMER, COMM_VENDOR, COMM_REGULATOR,
})
# Audiences outside the organisation. A draft to one of these cannot be marked sent
# without a recorded approval, regardless of who clicks.
EXTERNAL_AUDIENCES = frozenset({
    COMM_AFFECTED_INDIVIDUAL, COMM_CUSTOMER, COMM_VENDOR, COMM_REGULATOR,
})

COMMUNICATION_STATUSES = frozenset({
    "draft", "review_required", "approved", "rejected", "sent",
})

# ── Review decisions (§22) ──────────────────────────────────────────────────────
DECISION_APPROVE = "approved"
DECISION_REJECT = "rejected"
DECISION_EDIT = "edited"
DECISION_MORE_INFO = "request_more_information"
DECISION_ESCALATE = "escalated"

DECISIONS = frozenset({
    DECISION_APPROVE, DECISION_REJECT, DECISION_EDIT, DECISION_MORE_INFO,
    DECISION_ESCALATE,
})
# Decisions that must carry a reason. Approving a high-impact containment action or
# an external communication without a recorded rationale is exactly what the audit
# trail exists to prevent.
DECISIONS_REQUIRING_REASON = frozenset({
    DECISION_REJECT, DECISION_EDIT, DECISION_MORE_INFO, DECISION_ESCALATE,
})

# ── Domain error codes (§39) ────────────────────────────────────────────────────
ERR_INVALID_INCIDENT = "INVALID_INCIDENT"
ERR_UNAUTHORIZED_ACCESS = "UNAUTHORIZED_ACCESS"
ERR_EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
ERR_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
ERR_INVESTIGATION_FAILED = "INVESTIGATION_FAILED"
ERR_IMPACT_UNKNOWN = "IMPACT_UNKNOWN"
ERR_RISK_ASSESSMENT_FAILED = "RISK_ASSESSMENT_FAILED"
ERR_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
ERR_ACTION_BLOCKED = "ACTION_BLOCKED"
ERR_ACTION_FAILED = "ACTION_FAILED"
ERR_VERIFICATION_FAILED = "VERIFICATION_FAILED"
ERR_NOTIFICATION_FAILED = "NOTIFICATION_FAILED"
ERR_SLA_BREACH = "SLA_BREACH"

ERROR_CODES = frozenset({
    ERR_INVALID_INCIDENT, ERR_UNAUTHORIZED_ACCESS, ERR_EVIDENCE_UNAVAILABLE,
    ERR_SOURCE_UNAVAILABLE, ERR_INVESTIGATION_FAILED, ERR_IMPACT_UNKNOWN,
    ERR_RISK_ASSESSMENT_FAILED, ERR_APPROVAL_REQUIRED, ERR_ACTION_BLOCKED,
    ERR_ACTION_FAILED, ERR_VERIFICATION_FAILED, ERR_NOTIFICATION_FAILED,
    ERR_SLA_BREACH,
})

# ── Audit actions (§42) ─────────────────────────────────────────────────────────
# Written through the SHARED audit_service with entity_type="incident_case", so the
# whole incident timeline is one indexed query against audit_logs.
AUDIT_ENTITY = "incident_case"

AUDIT_CREATED = "incident.created"
AUDIT_VALIDATED = "incident.validated"
AUDIT_CLASSIFIED = "incident.classified"
AUDIT_EVIDENCE_ADDED = "incident.evidence_added"
AUDIT_INVESTIGATION_STARTED = "incident.investigation_started"
AUDIT_INVESTIGATION_COMPLETED = "incident.investigation_completed"
AUDIT_TIMELINE_UPDATED = "incident.timeline_updated"
AUDIT_SYSTEMS_IDENTIFIED = "incident.affected_systems_identified"
AUDIT_DATA_IDENTIFIED = "incident.affected_data_identified"
AUDIT_SUBJECTS_ASSESSED = "incident.affected_subjects_assessed"
AUDIT_RISK_ASSESSED = "incident.risk_assessed"
AUDIT_REVIEW_REQUESTED = "incident.review_requested"
AUDIT_APPROVED = "incident.approved"
AUDIT_REJECTED = "incident.rejected"
AUDIT_ACTION_PLANNED = "incident.action_planned"
AUDIT_ACTION_STARTED = "incident.action_started"
AUDIT_ACTION_EXECUTED = "incident.action_executed"
AUDIT_ACTION_VERIFIED = "incident.action_verified"
AUDIT_COMMUNICATION_DRAFTED = "incident.communication_drafted"
AUDIT_COMMUNICATION_APPROVED = "incident.communication_approved"
AUDIT_COMMUNICATION_SENT = "incident.communication_sent"
AUDIT_REPORT_GENERATED = "incident.report_generated"
AUDIT_CLOSED = "incident.closed"
AUDIT_STATUS_CHANGED = "incident.status_changed"
AUDIT_SLA_BREACHED = "incident.sla_breached"
