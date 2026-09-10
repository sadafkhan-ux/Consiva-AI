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
