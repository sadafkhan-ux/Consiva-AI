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
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
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


# ── Agent 2: Data Discovery / ROPA (migrations/0007_ropa_agent.sql) ──────────────


class RopaDataSource(Base):
    """An authorized external source this org may discover against.

    `credential_ref` is the NAME of an environment/secret-store entry, never the
    secret itself -- see 0007's header for why. Nothing in this row is sensitive,
    so it is safe to return from the API and safe in a database dump.
    """

    __tablename__ = "ropa_data_sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    connector: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    credential_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    credential_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RopaDiscoveryRun(Base):
    __tablename__ = "ropa_discovery_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ropa_data_sources.id"), nullable=True, index=True
    )
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    ingest_mode: Mapped[str] = mapped_column(String, nullable=False, default="connector")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    tables_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    columns_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    personal_data_elements: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overall_confidence: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RopaRecordRow(Base):
    """One version of one ROPA record. A re-run supersedes rather than overwrites,
    so an approved record is never silently rewritten (ROPA prompt §20)."""

    __tablename__ = "ropa_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    discovery_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ropa_discovery_runs.id"), nullable=False, index=True
    )
    processing_activity: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    edited_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ropa_records.id"), nullable=True)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RopaSchemaBaseline(Base):
    """The promoted schema fingerprint a source's future runs are diffed against
    (migrations/0009). Advances only on an explicit human promotion, never
    automatically -- the same rule as ScanSchedule.baseline_scan_id."""

    __tablename__ = "ropa_schema_baselines"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    discovery_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ropa_discovery_runs.id"), nullable=False
    )
    schema_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    promoted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RopaSchemaChange(Base):
    __tablename__ = "ropa_schema_changes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    discovery_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ropa_discovery_runs.id"), nullable=False, index=True
    )
    baseline_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ropa_schema_baselines.id"), nullable=True
    )
    change_type: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str] = mapped_column(String, nullable=False)
    previous_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_material: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RopaIntegrationKey(Base):
    """Service credential for an external integration adapter. `key_hash` is a
    SHA-256 digest; the key itself exists only in the operator's hands (see
    migrations/0008_ropa_integration_keys.sql)."""

    __tablename__ = "ropa_integration_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String, nullable=False)
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RopaFinding(Base):
    __tablename__ = "ropa_findings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    discovery_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ropa_discovery_runs.id"), nullable=False, index=True
    )
    finding: Mapped[str] = mapped_column(Text, nullable=False)
    gap_status: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    severity_factors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    related_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    confidence: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    edited_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── Agent 3 (DSR Fulfillment) ────────────────────────────────────────────────────
# Mirrors migrations/0011_dsr_agent.sql. Read that file's header first: it explains
# why there is no dsr_case_events table (the timeline is audit_logs), why sources are
# not re-registered here (they point at ropa_data_sources), and why a DSR write needs
# its own credential rather than reusing Agent 2's read-only connector contract.


class DsrSourceAuthorization(Base):
    """Which authorized source Agent 3 may search, and whether it may write to it.

    Fails closed: the allowlists default to empty, so a source registered for Agent 2
    discovery grants Agent 3 exactly nothing until an administrator names the tables
    and columns here.
    """

    __tablename__ = "dsr_source_authorizations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ropa_data_sources.id"), nullable=False)
    searchable_tables: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    identity_tables: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    identifier_columns: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    returnable_columns: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    record_key_columns: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    allow_execution: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    write_credential_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    erasable_columns: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrRetentionRule(Base):
    """An organisation's own retention policy, as configuration.

    Consiva does not decide how long a record must be kept. `authority` records whose
    rule it is, and is shown both to the reviewer and to the data principal, because
    why an erasure was refused on policy grounds is what they are entitled to know.
    """

    __tablename__ = "dsr_retention_rules"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # NULL means "every source in this org that has this table" -- how an
    # organisation-wide policy is expressed.
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ropa_data_sources.id"), nullable=True
    )
    table_name: Mapped[str] = mapped_column(String, nullable=False)
    date_column: Mapped[str] = mapped_column(String, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    authority: Mapped[str] = mapped_column(String, nullable=False)
    applies_to_operations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrRequest(Base):
    """The DSR case. Every other Agent 3 row traces back to this one."""

    __tablename__ = "dsr_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    reference: Mapped[str] = mapped_column(String, nullable=False)
    raw_request: Mapped[str] = mapped_column(Text, nullable=False)
    request_type: Mapped[str] = mapped_column(String, nullable=False, default="unclassified")
    classification_method: Mapped[str | None] = mapped_column(String, nullable=True)
    classification_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    requester_email: Mapped[str | None] = mapped_column(String, nullable=True)
    requester_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    requester_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="received")
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sla_breached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrIdentityVerification(Base):
    """The gate before any sensitive search. `challenge_hash` is a SHA-256 digest --
    the plaintext challenge is sent to the requester and never stored, so a database
    dump cannot be replayed to pass verification."""

    __tablename__ = "dsr_identity_verifications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_requests.id"), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    challenge_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    evidence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrSearchRun(Base):
    __tablename__ = "dsr_search_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_requests.id"), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ropa_data_sources.id"), nullable=True)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    identifier_kinds: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    tables_searched: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_subject_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrEvidence(Base):
    """One match, with everything needed to justify it. The matched VALUE is not
    stored -- it is the requester's own identifier, already on dsr_requests, and
    repeating it per row multiplies PII for no benefit."""

    __tablename__ = "dsr_evidence"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_requests.id"), nullable=False, index=True)
    search_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_search_runs.id"), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ropa_data_sources.id"), nullable=True)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String, nullable=True)
    table_name: Mapped[str] = mapped_column(String, nullable=False)
    matched_column: Mapped[str] = mapped_column(String, nullable=False)
    identifier_kind: Mapped[str] = mapped_column(String, nullable=False)
    match_type: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    record_reference: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    record_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ropa_category: Mapped[str | None] = mapped_column(String, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrActionPlan(Base):
    __tablename__ = "dsr_action_plans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_requests.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    constraints_evaluated: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrAction(Base):
    __tablename__ = "dsr_actions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_requests.id"), nullable=False, index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_action_plans.id"), nullable=False, index=True)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dsr_evidence.id"), nullable=True)
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ropa_data_sources.id"), nullable=True)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    table_name: Mapped[str] = mapped_column(String, nullable=False)
    record_reference: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    operation_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expected_result: Mapped[str] = mapped_column(Text, nullable=False)
    risk: Mapped[str] = mapped_column(String, nullable=False, default="medium")
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="proposed")
    # Two audiences, two texts. `blocked_reason` is for the reviewer and names
    # configuration; `requester_explanation` is what the data subject reads.
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requester_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrApproval(Base):
    """Append-only at the database level (trigger in 0011), the same guarantee 0004
    gave Agent 1's approvals table. Kept separate from that table only because its
    finding_id is a NOT NULL FK to consent_findings."""

    __tablename__ = "dsr_approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_requests.id"), nullable=False, index=True)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dsr_action_plans.id"), nullable=True)
    action_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dsr_actions.id"), nullable=True)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrExecution(Base):
    """The idempotency ledger. The unique index on (org_id, idempotency_key) is what
    makes a double-click, an API retry or a worker restart return the previous
    verified result instead of performing a deletion twice."""

    __tablename__ = "dsr_executions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_requests.id"), nullable=False, index=True)
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_actions.id"), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    rows_affected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connector_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    verification_status: Mapped[str | None] = mapped_column(String, nullable=True)
    verification_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    executed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DsrResponse(Base):
    """`grounded_facts` is populated from dsr_evidence and dsr_executions only. A
    sentence in body_text that is not traceable to a row there is not a DSR fact."""

    __tablename__ = "dsr_responses"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dsr_requests.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    grounded_facts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    drafted_by_model: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── Agent 4 (Breach Response) ────────────────────────────────────────────────────
# Mirrors migrations/0015_breach_agent.sql. Read that file's header first: it explains
# why the timeline is separate from audit_logs, why response actions are tracked work
# by default, and why evidence is append-only.


class IncidentCase(Base):
    """The incident. Every other Agent 4 row traces back to this one.

    `initial_severity` is what the reporter claimed; `severity` is what the engine
    concluded from evidence. Keeping both means an under-reported critical incident
    stays visible rather than being overwritten by the assessment.
    """

    __tablename__ = "incident_cases"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    reference: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    reported_by: Mapped[str | None] = mapped_column(String, nullable=True)
    incident_type: Mapped[str] = mapped_column(String, nullable=False, default="unclassified")
    classification_method: Mapped[str | None] = mapped_column(String, nullable=True)
    classification_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_severity: Mapped[str | None] = mapped_column(String, nullable=True)
    severity: Mapped[str | None] = mapped_column(String, nullable=True)
    severity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity_confidence: Mapped[str | None] = mapped_column(String, nullable=True)
    # Never promoted to 'confirmed' by any rule or model -- only a named human.
    personal_data_involved: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    breach_confirmed: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String, nullable=False, default="reported")
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_breached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentEvidence(Base):
    """Append-only at the database level. An investigation whose evidence can be
    rewritten afterwards is not evidence, it is a narrative -- superseding is done by
    adding a row that references the old one, which stays."""

    __tablename__ = "incident_evidence"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    source_system: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Marks a row whose detail must never reach the UI or an export.
    contains_secrets: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Something Consiva worked out about itself rather than something that happened.
    is_derived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incident_evidence.id"), nullable=True)
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentTimelineEntry(Base):
    """What happened in the WORLD, reconstructed from evidence -- as distinct from
    audit_logs, which is what happened in Consiva. Each entry carries its own
    confidence, because "the database was read at 10:05" is a claim and how strongly
    it is believed is part of the claim."""

    __tablename__ = "incident_timeline"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str | None] = mapped_column(String, nullable=True)
    source_system: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[str] = mapped_column(String, nullable=False, default="possible")
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incident_evidence.id"), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentAffectedSystem(Base):
    __tablename__ = "incident_affected_systems"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    system_name: Mapped[str] = mapped_column(String, nullable=False)
    system_kind: Mapped[str] = mapped_column(String, nullable=False)
    component: Mapped[str | None] = mapped_column(String, nullable=True)
    # Set only where the affected system is one Consiva already knows about.
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ropa_data_sources.id"), nullable=True)
    confidence: Mapped[str] = mapped_column(String, nullable=False, default="possible")
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incident_evidence.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentAffectedData(Base):
    """`derived_from` records HOW the category was established. A ROPA lookup is a
    strong prior but is not the same as having observed the data in the incident."""

    __tablename__ = "incident_affected_data"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    affected_system_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incident_affected_systems.id"), nullable=True
    )
    data_category: Mapped[str] = mapped_column(String, nullable=False)
    table_name: Mapped[str | None] = mapped_column(String, nullable=True)
    column_name: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[str] = mapped_column(String, nullable=False, default="possible")
    derived_from: Mapped[str] = mapped_column(String, nullable=False, default="ropa_metadata")
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incident_evidence.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentAffectedSubjects(Base):
    """`count_basis` says whether a figure was counted, estimated or is unknown --
    "11,500 customers affected" and "we think roughly 11,500" are different statements
    and only one of them belongs in a regulator's inbox."""

    __tablename__ = "incident_affected_subjects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    subject_group: Mapped[str] = mapped_column(String, nullable=False)
    record_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    count_basis: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    basis_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[str] = mapped_column(String, nullable=False, default="possible")
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incident_evidence.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentRiskAssessment(Base):
    """Versioned: re-assessing as evidence arrives is normal, and the earlier
    assessment is part of the incident's history rather than something to overwrite.

    `regulatory_context` holds passages retrieved from the approved knowledge base,
    kept strictly separate from the system facts in `factors`. It is never a
    determination that a law applies -- it is what a human should read before deciding.
    """

    __tablename__ = "incident_risk_assessments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    risk_level: Mapped[str] = mapped_column(String, nullable=False)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[str] = mapped_column(String, nullable=False)
    factors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    regulatory_context: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    assessed_by: Mapped[str] = mapped_column(String, nullable=False, default="engine")
    review_status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentAction(Base):
    """`execution_mode` is the honest part of this agent: 'tracked' means a person
    performs it in a system Consiva has no connector to and attests what they did;
    'connector' is the narrow case where Agent 3's connector performs it for real."""

    __tablename__ = "incident_actions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    action_kind: Mapped[str] = mapped_column(String, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String, nullable=False, default="tracked")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    expected_result: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    affected_system_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incident_affected_systems.id"), nullable=True
    )
    risk: Mapped[str] = mapped_column(String, nullable=False, default="medium")
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="proposed")
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignee_label: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentApproval(Base):
    """Append-only at the database level. Kept separate from Agent 1's approvals table
    only because that table's finding_id is a NOT NULL FK to consent_findings."""

    __tablename__ = "incident_approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    action_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incident_actions.id"), nullable=True)
    risk_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incident_risk_assessments.id"), nullable=True
    )
    communication_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incident_communications.id"), nullable=True
    )
    subject: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentExecution(Base):
    """The idempotency ledger, and the place where "a connector confirmed it" and "a
    person said they did it" are kept apart. `verification_status` distinguishes
    `read_back` from `attested` rather than blurring them into "done"."""

    __tablename__ = "incident_executions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_actions.id"), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    rows_affected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connector_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    performed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    attestation: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str | None] = mapped_column(String, nullable=True)
    verification_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    executed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentCommunication(Base):
    """`sent_at` is only ever set when something was actually sent. No outbound
    provider is configured, so in practice a human records that they sent it -- the
    system never claims it did."""

    __tablename__ = "incident_communications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    audience: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    drafted_by_model: Mapped[str | None] = mapped_column(String, nullable=True)
    grounded_facts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncidentReport(Base):
    """`grounded_facts` is the machine-checkable set the prose was assembled from. A
    sentence not traceable to one of those rows is not an incident fact."""

    __tablename__ = "incident_reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_cases.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    grounded_facts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    drafted_by_model: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── Agent 5: Regulatory Watch (migration 0019) ──────────────────────────────────


class RegWatchSource(Base):
    """An approved regulatory source. Only these are ever monitored.

    `credential_ref` names an environment variable, the same secret-by-reference
    pattern Agents 2 and 3 use -- the value never lands in this table.

    `last_checked_at` and `last_success_at` are deliberately separate. A source checked
    ten minutes ago that FAILED is not the same as one that succeeded ten minutes ago,
    and a UI showing only "last checked" would present them identically -- which is
    exactly what the spec's final guardrail forbids.
    """

    __tablename__ = "regwatch_sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    connector: Mapped[str] = mapped_column(String, nullable=False, default="http")
    jurisdiction: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    authority: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=1440)
    credential_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegWatchCollection(Base):
    """One attempt to fetch a source -- including the ones that failed.

    APPEND-ONLY (trigger, migration 0019). This is the evidence every change record
    downstream rests on; if it could be rewritten afterwards, none of them would mean
    anything.
    """

    __tablename__ = "regwatch_collections"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regwatch_sources.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegWatchBaseline(Base):
    """The content a person has accepted as the reference point for a source.

    `approved_by_user_id` is NOT NULL and there is no code path around it. An agent
    that advanced its own baseline would report a change once and then absorb it.
    """

    __tablename__ = "regwatch_baselines"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regwatch_sources.id"), nullable=False)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("regwatch_collections.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    approved_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegWatchChange(Base):
    """What changed in the world, between an accepted baseline and a new collection.

    Distinct from audit_logs, which is what happened in Consiva. A change is a claim
    about a regulator's website; an audit entry is a record of our own action.
    """

    __tablename__ = "regwatch_changes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regwatch_sources.id"), nullable=False)
    from_baseline_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("regwatch_baselines.id"), nullable=True)
    to_collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("regwatch_collections.id"), nullable=False)
    change_kind: Mapped[str] = mapped_column(String, nullable=False)
    added_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    removed_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    diff_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegWatchFinding(Base):
    """The interpreted, reviewable item a compliance team acts on."""

    __tablename__ = "regwatch_findings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    change_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regwatch_changes.id"), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regwatch_sources.id"), nullable=False)
    reference: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="detected")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance: Mapped[str] = mapped_column(String, nullable=False, default="undetermined")
    relevance_confidence: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    relevance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str | None] = mapped_column(String, nullable=True)
    priority_confidence: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    grounded_facts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    drafted_by_model: Mapped[str | None] = mapped_column(String, nullable=True)
    open_questions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegWatchImpact(Base):
    """What in THIS organisation a change may touch. By reference to the other agents'
    rows, never by copying their data."""

    __tablename__ = "regwatch_impacts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regwatch_findings.id"), nullable=False)
    target_kind: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    target_label: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(String, nullable=False, default="possible")
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    derived_from: Mapped[str] = mapped_column(String, nullable=False, default="rule")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegWatchApproval(Base):
    """Append-only (trigger, migration 0019). A decision that can be edited afterwards
    is not a decision anyone can rely on."""

    __tablename__ = "regwatch_approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regwatch_findings.id"), nullable=False)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegWatchAction(Base):
    """Tracked follow-up work. Consiva does not perform regulatory work: like Agent
    4's containment, an action is carried out by a person and attested to."""

    __tablename__ = "regwatch_actions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regwatch_findings.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    expected_result: Mapped[str] = mapped_column(Text, nullable=False)
    owner_label: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    completion_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
