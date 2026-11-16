"""The Enterprise tables (migration 0050). Declared on the application's existing declarative Base, so they are part of the same metadata and the same session."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base, _uuid_pk

SCAN_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
FRONTIER_STATUSES = ("pending", "in_progress", "done", "failed", "excluded")
EVIDENCE_KINDS = ("page_html", "page_text", "network", "cookie", "form", "document_text", "screenshot", "header", "consent")


class EntScan(Base):
    __tablename__ = "ent_scans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    website_url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    options: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    urls_not_recorded: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))      # migration 0057
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    # migration 0055: the analysis phase is tracked apart from the crawl (`status`), and a scan can name the one it repeats
    analysis_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'not_started'"))
    analysis_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    analysis_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    analysis_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ent_scans.id", ondelete="SET NULL"), nullable=True)


class EntFrontier(Base):
    __tablename__ = "ent_frontier"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ent_scans.id", ondelete="CASCADE"), nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    discovery_source: Mapped[str] = mapped_column(Text, nullable=False)
    discovery_sources: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    # migration 0055: how a document was read, and whether the page or document has been analysed
    extraction_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'not_started'"))
    # migration 0056: 0 for the pages a privacy assessment needs first (policies, terms, grievance, contact), 1 for the rest
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # migration 0057: what was extracted from the page, how it was classified, and how much of it was analysed
    page_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunks_created: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunks_analysed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class EntEvidence(Base):
    __tablename__ = "ent_evidence"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ent_scans.id", ondelete="CASCADE"), nullable=False)
    frontier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ent_frontier.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    detection_method: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class EntRegistryVersion(Base):
    __tablename__ = "ent_registry_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    version: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    approved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class EntRegistryProvision(Base):
    __tablename__ = "ent_registry_provisions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ent_registry_versions.id", ondelete="CASCADE"), nullable=False)
    instrument: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    phrase: Mapped[str] = mapped_column(Text, nullable=False)
    official_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    commences: Mapped[date] = mapped_column(Date, nullable=False)


# ---- the assessment layer (migration 0055) ----------------------------------------------------------------------------------------------------------------------------

ANALYSIS_STATUSES = ("not_started", "running", "completed", "partial", "failed")
URL_ANALYSIS_STATUSES = ("not_started", "done", "failed", "not_applicable")
EXTRACTION_STATUSES = ("extracted", "ocr_extracted", "partly_extracted", "unreadable", "encrypted", "unsupported", "too_large")
ASSESSMENT_STATES = ("met", "partly_met", "not_met", "not_assessed", "not_applicable", "needs_records", "human_review")
CHUNK_LEVELS = ("page", "category", "cross_page", "mapping", "final")
CHUNK_STATUSES = ("pending", "done", "failed", "skipped")


class EntAssessment(Base):
    __tablename__ = "ent_assessments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ent_scans.id", ondelete="CASCADE"), nullable=False)
    parameter_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    confidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    assessor: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    catalogue_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'current'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class EntScoreResult(Base):
    __tablename__ = "ent_score_results"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ent_scans.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    catalogue_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage: Mapped[int] = mapped_column(Integer, nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class EntFinding(Base):
    __tablename__ = "ent_findings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ent_scans.id", ondelete="CASCADE"), nullable=False)
    finding_key: Mapped[str] = mapped_column(Text, nullable=False)
    parameter_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    legal_refs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    affected_urls: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    affected_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    expected: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    observed: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    confidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    human_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    verification_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'unverified'"))
    schedule_item: Mapped[int | None] = mapped_column(Integer, nullable=True)
    penalty_ceiling_cr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class EntAnalysisChunk(Base):
    __tablename__ = "ent_analysis_chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ent_scans.id", ondelete="CASCADE"), nullable=False)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ent_evidence.id", ondelete="SET NULL"), nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    level: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class EntReport(Base):
    __tablename__ = "ent_reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ent_scans.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    score_result_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ent_score_results.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ready'"))
    pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class EntAuditEvent(Base):
    __tablename__ = "ent_audit_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ent_scans.id", ondelete="CASCADE"), nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
