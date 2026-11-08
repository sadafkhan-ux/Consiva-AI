"""SQLAlchemy models for every table in docs/architecture §G.

`org_id` columns are plain (unconstrained) UUIDs, not a hard FK: tenancy/organization
management is a platform-level concern that spans all future agents, not something the
Consent Agent owns. An `organizations` table is assumed to exist (or be added) elsewhere;
see docs/architecture §P.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings

EMBED_DIM = get_settings().nvidia_embed_dimensions


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())


class Website(Base):
    __tablename__ = "websites"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConsentScan(Base):
    __tablename__ = "consent_scans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    website_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("websites.id"), nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    scanner_version: Mapped[str | None] = mapped_column(String, nullable=True)
    authorized_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Added by migration 0022 for the /consent-agent integration API. All three are
    # nullable/defaulted and read by nothing that predates them, so the console flow
    # is unchanged.
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    scan_options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Added by migration 0032: a per-scan override for the FIXED external-partner
    # webhook (app/services/agent_webhook_service.py), for a shared agent instance
    # serving more than one environment -- see that migration's header. Distinct
    # from webhook_url above, which is Consiva's own older signing scheme.
    agent_callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_callback_secret_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebsitePage(Base):
    __tablename__ = "website_pages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discovered_via: Mapped[str | None] = mapped_column(String, nullable=True)
    final_url: Mapped[str | None] = mapped_column(String, nullable=True)       # migration 0046
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)    # migration 0046


class ScanScreenshot(Base):
    """The homepage preview of one scan (migration 0040). One row per scan; an image only when status is "ok"."""

    __tablename__ = "scan_screenshots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id", ondelete="CASCADE"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    mime: Mapped[str | None] = mapped_column(String, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ReportPdf(Base):
    """A pre-generated PDF report (migration 0043). One row per scan and report type; valid only while its fingerprint matches the scan's current one."""

    __tablename__ = "report_pdfs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id", ondelete="CASCADE"), nullable=False)
    variant: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    pdf: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ConsentForm(Base):
    __tablename__ = "consent_forms"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    page_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("website_pages.id"), nullable=True)
    selector: Mapped[str | None] = mapped_column(String, nullable=True)
    fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    purpose_guess: Mapped[str | None] = mapped_column(String, nullable=True)
    submit_url: Mapped[str | None] = mapped_column(String, nullable=True)


class Cookie(Base):
    __tablename__ = "cookies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    path: Mapped[str | None] = mapped_column(String, nullable=True)
    expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_first_party: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    set_by_tracker_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trackers.id"), nullable=True)
    # Provenance of `category`/`vendor` above — master prompt §4.
    source: Mapped[str] = mapped_column(String, nullable=False, default="rule")
    # Which of the three scan passes this cookie was observed in — a set, not a
    # single value, since the same cookie can persist across states.
    consent_states: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    # As the browser reported them; NULL on scans made before migration 0027 recorded them.
    secure: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    http_only: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    same_site: Mapped[str | None] = mapped_column(String, nullable=True)
    # Where/when each pass first saw it -- scanner/schemas.py CookieObservation.
    observations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class Tracker(Base):
    __tablename__ = "trackers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    page_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("website_pages.id"), nullable=True)
    script_src: Mapped[str] = mapped_column(String, nullable=False)
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False, default="rule")
    consent_states: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)


class CookieLookup(Base):
    """DB-backed lookup table seeded from the Open Cookie Database (Build Plan
    Component 3, Layer 1) — replaces rules/tracker_catalog.py's hardcoded tuples as
    the source of truth. See app/lookup/loader.py."""

    __tablename__ = "cookie_lookup"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name_pattern: Mapped[str] = mapped_column(String, nullable=False, index=True)
    is_prefix_pattern: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    domain_pattern: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, default="open_cookie_database")
    raw_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PurposeTaxonomy(Base):
    """Fixed category list owned by the compliance team, stored as data rather than
    a hardcoded type — Build Plan Component 3."""

    __tablename__ = "purpose_taxonomy"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ThirdPartyService(Base):
    __tablename__ = "third_party_services"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    service_name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    domains: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    detection_method: Mapped[str | None] = mapped_column(String, nullable=True)


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String, nullable=False)
    policy_type: Mapped[str] = mapped_column(String, nullable=False)
    extracted_text_ref: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConsentSignal(Base):
    __tablename__ = "consent_signals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    mechanism_type: Mapped[str] = mapped_column(String, nullable=False)
    cmp_vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    has_reject_all: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_granular_choices: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(String, nullable=False, default="consent_agent")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    llm_provider: Mapped[str | None] = mapped_column(String, nullable=True, default="nvidia")
    llm_model: Mapped[str | None] = mapped_column(String, nullable=True)
    langgraph_thread_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConsentFinding(Base):
    __tablename__ = "consent_findings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id"), nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    risk_level: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False, default="medium")
    finding_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    dpdp_reference: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    # Technical/diagnostic detail (provider errors, ungrounded citation ids) that must
    # never reach a customer via finding_text -- see migrations/0036 and
    # create_rule_findings.py. Deliberately absent from every customer-facing API
    # schema (FindingOut / consent_scans.py's FindingResponse).
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConsentRecommendation(Base):
    __tablename__ = "consent_recommendations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_findings.id"), nullable=False, index=True)
    recommendation_text: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str | None] = mapped_column(String, nullable=True)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_findings.id"), nullable=False, index=True)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_ref: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String, nullable=True)
    is_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_documents.id"), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM), nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class WebhookDelivery(Base):
    """One attempt to notify an external system that a scan finished.

    Separate from audit_logs on purpose: that records what a PERSON did to a tenant's
    data, and an HTTP POST to a customer's endpoint is neither. Retry scheduling stays
    with the existing job queue (agent_jobs.attempts + run_after backoff); this is the
    durable record of what was sent and what came back, which the queue row loses once
    the job is done."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String, nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentJob(Base):
    """Queue table backing the polling worker (docs/architecture §B). Not in the
    original §G list — a direct, necessary consequence of that background-job
    design rather than a new architectural decision."""

    __tablename__ = "agent_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String, nullable=False)  # "scan" | "analyze"
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Action(Base):
    """Action Module (Build Plan Component 8). One row per tracked follow-up from an
    approved finding — a task, a notification record, or a consent-config change that
    must pass through staged -> live as two separately-audited steps."""

    __tablename__ = "actions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_findings.id"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String, nullable=False)  # "task" | "notification" | "config_change"
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignee_label: Mapped[str | None] = mapped_column(String, nullable=True)
    config_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScanSchedule(Base):
    """Continuous Monitoring (Build Plan Component 9): one row per website under a
    recurring re-scan schedule. `baseline_scan_id` only advances when a reviewer
    explicitly promotes a scan to baseline — never automatically."""

    __tablename__ = "scan_schedules"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    website_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("websites.id"), nullable=False, unique=True)
    interval_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    baseline_scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("consent_scans.id"), nullable=True)
    last_triggered_scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("consent_scans.id"), nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScanDiff(Base):
    """One row per (baseline, new) comparison actually performed — persisted so "what
    changed and when" is itself part of the permanent, reconstructable record."""

    __tablename__ = "scan_diffs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    website_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("websites.id"), nullable=False, index=True)
    baseline_scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False)
    new_scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False)
    added: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    removed: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    changed: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    has_material_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRunStage(Base):
    """Stage-level timing/status for the demo UI's live pipeline view and latency
    measurement — see migrations/0003_agent_run_stages.sql for why this is keyed by
    scan_id rather than agent_run_id."""

    __tablename__ = "agent_run_stages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id"), nullable=False, index=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Python attribute name differs from the DB column ("metadata") because
    # DeclarativeBase reserves `.metadata` for the ORM's own MetaData object.
    stage_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── First-party auth (migrations/0010_local_auth.sql) ────────────────────────────


class Organization(Base):
    """The tenant every org_id column across this schema refers to.

    Deliberately NOT wired as a foreign key from those columns yet: rows already
    reference org ids that predate this table (see 0010's header)."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Where this organisation operates (migration 0020). Not read by anything in the
    # Consent Agent today; kept because the column exists in the schema.
    jurisdictions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """`password_hash` is a bcrypt hash (app/core/passwords.py) and must never
    appear in an API response model."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LlmUsageEvent(Base):
    """One LLM call group made for a scan, as the provider reported it (migration 0041). See app/llm/usage_tracking.py."""

    __tablename__ = "llm_usage_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consent_scans.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    agent: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    request_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default=text("'{}'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    fallback_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'USD'"))
    price_source: Mapped[str | None] = mapped_column(String, nullable=True)
    page_urls: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
