"""Request/response models for the /consent-agent integration API.

This is a translation layer, not a second data model. Every field here is derived from
something the Consent Agent pipeline already produces -- consent_scans, agent_run_stages,
consent_findings, the evidence tables -- and reshaped into the flatter, more stable
contract an external caller wants. Nothing is invented, and nothing here is the source
of truth for anything.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ScanStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class ScanOptions(BaseModel):
    """What the caller may ask for. Every value is a REQUEST, not a guarantee: the
    server clamps each one to its configured ceiling and returns the clamped set, so a
    caller asking for 500 pages gets the maximum allowed rather than an error or a
    500-page crawl."""

    max_pages: int | None = Field(
        default=None, ge=1, le=100,
        description="Pages to crawl. Clamped to the server's configured maximum.",
    )
    scan_consent_states: bool = Field(
        default=True,
        description="Run the Accept and Reject passes. When false, only the pre-consent "
                    "baseline is collected and no post-consent finding can be produced.",
    )
    scan_before_consent: bool = Field(default=True, description="Collect the pre-consent baseline.")
    scan_after_accept: bool = Field(default=True, description="Click Accept and re-measure.")
    scan_after_reject: bool = Field(default=True, description="Click Reject and re-measure.")


class CreateScanRequest(BaseModel):
    website_url: str = Field(..., description="Absolute http(s) URL of the site to scan.")
    scan_options: ScanOptions = Field(default_factory=ScanOptions)
    webhook_url: str | None = Field(
        default=None,
        description="Optional https endpoint POSTed when the scan reaches a terminal "
                    "state. Validated against the same SSRF rules as the scan target.",
    )
    authorized: bool = Field(
        default=False,
        description="Explicit attestation that you own or are permitted to scan this "
                    "domain. Required -- the request is refused without it.",
    )

    @field_validator("website_url")
    @classmethod
    def _absolute_http_url(cls, value: str) -> str:
        """Shape only. Whether the URL is SAFE to fetch is decided by
        core.url_safety.assert_safe_url, which resolves DNS and rejects private,
        loopback, link-local and metadata addresses -- never by a string check here."""
        if not value.startswith(("http://", "https://")):
            raise ValueError("website_url must be an absolute http:// or https:// URL")
        return value


class CreateScanResponse(BaseModel):
    scan_id: uuid.UUID
    status: ScanStatus
    message: str
    idempotent_replay: bool = Field(
        default=False,
        description="True when this request matched a previous Idempotency-Key and "
                    "returned the existing scan instead of starting a new one.",
    )


class ScanListItem(BaseModel):
    """One row of the scan list. Deliberately a summary: inlining evidence would make
    a single page heavier than every other endpoint combined."""

    scan_id: uuid.UUID
    website_url: str
    status: ScanStatus
    findings_count: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class ScanListResponse(BaseModel):
    scans: list[ScanListItem]
    total: int = Field(..., description="Total matching scans, for paging.")
    limit: int
    offset: int


class ScanStatusResponse(BaseModel):
    scan_id: uuid.UUID
    website_url: str
    status: ScanStatus
    progress: int = Field(..., ge=0, le=100, description="Derived from completed pipeline stages.")
    pages_discovered: int
    pages_scanned: int
    pages_failed: int
    current_stage: str | None
    started_at: datetime | None
    completed_at: datetime | None
    error: dict[str, Any] | None = None


class EvidenceRef(BaseModel):
    type: str
    domain: str | None = None
    source: str | None = Field(
        default=None, description="Consent state the item was observed in, where known."
    )
    local_id: str | None = None


class FindingOut(BaseModel):
    id: uuid.UUID
    severity: Literal["high", "medium", "low"]
    category: str
    title: str
    description: str
    evidence: list[EvidenceRef]
    confidence: float | None = Field(
        default=None,
        description="Null when the pipeline did not record one. Never a filled-in "
                    "default -- an invented confidence is worse than none.",
    )
    status: str
    requires_human_review: bool
    recommendation: str | None = None
    dpdp_references: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class FindingsResponse(BaseModel):
    scan_id: uuid.UUID
    findings: list[FindingOut]


class ConsentStatesTested(BaseModel):
    before: bool
    accept: bool
    reject: bool


class FindingCounts(BaseModel):
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0


class SummaryResponse(BaseModel):
    scan_id: uuid.UUID
    website: str
    status: ScanStatus
    pages_scanned: int
    cookies_detected: int
    trackers_detected: int
    cmp_detected: bool
    cmp_vendor: str | None
    cmp_confidence: float | None
    consent_states_tested: ConsentStatesTested
    findings: FindingCounts
    compliance_status: Literal["compliant", "review_required", "issues_found", "unknown"]


class TokenMetrics(BaseModel):
    llm_calls: int
    rag_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    provider: str | None = None
    model: str | None = None


class StageTiming(BaseModel):
    stage: str
    status: str
    duration_ms: int | None


class ScanResultResponse(BaseModel):
    scan_id: uuid.UUID
    website_url: str
    status: ScanStatus
    scanner_version: str | None
    scan_options: dict[str, Any]
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    page_stats: dict[str, int]
    consent_mechanism: dict[str, Any] | None
    consent_states: dict[str, Any]
    evidence_counts: dict[str, int]
    cookies: list[dict[str, Any]]
    trackers: list[dict[str, Any]]
    third_party_services: list[dict[str, Any]]
    forms: list[dict[str, Any]]
    policies: list[dict[str, Any]]
    findings: list[FindingOut]
    stages: list[StageTiming]
    token_metrics: TokenMetrics | None
    errors: list[str]


class CancelResponse(BaseModel):
    scan_id: uuid.UUID
    status: ScanStatus
    message: str


class ApiErrorBody(BaseModel):
    code: str
    message: str
    scan_id: uuid.UUID | None = None


class ApiError(BaseModel):
    """The error envelope every endpoint in this router returns.

    Deliberately different from the rest of the platform's `{"detail": "..."}`: an
    external integrator needs a STABLE, machine-readable `code` to branch on, and
    `detail` alone forces them to pattern-match English prose that is free to change.
    """

    error: ApiErrorBody
