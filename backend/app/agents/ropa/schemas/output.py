"""Structured ROPA agent output contract (prompt §21-§22). Validated against
this on every LLM call -- malformed or ungrounded output is a hard failure,
handled the same way consent_agent's validate_output.py handles
ConsentAnalysisResponse, not silently accepted.

Return ONLY the 16 sections listed in the prompt's §22; no markdown, no
commentary outside the schema, no fields the schema doesn't define.
"""

from pydantic import BaseModel, Field

from app.agents.ropa.schemas.ropa import (
    ChangeDetectionEntry,
    ConfidenceSummary,
    DataSubjectMapping,
    HumanReviewItem,
    PersonalDataElement,
    ProcessingActivity,
    PurposeMapping,
    RiskGapFinding,
    RopaRecord,
    VendorProcessor,
)


class DiscoverySummary(BaseModel):
    sources_scanned: int = 0
    tables_scanned: int = 0
    columns_scanned: int = 0
    personal_data_elements_found: int = 0
    notes: str | None = None


class DataFlowMapping(BaseModel):
    processing_activity: str
    path: list[str] = Field(description="ordered node names, e.g. ['CRM', 'PostgreSQL', 'Email Provider']")
    evidence: list[str] = Field(default_factory=list)
    review_required: bool = False


class RetentionFinding(BaseModel):
    target: str
    retention: str = "Unknown"
    evidence: list[str] = Field(default_factory=list)
    review_required: bool = False


class AccessFinding(BaseModel):
    target: str
    owner: str = "Unknown"
    access_roles: list[str] = Field(default_factory=list)
    access_status: str = "Unknown"
    evidence: list[str] = Field(default_factory=list)
    review_required: bool = False


class RopaAgentOutput(BaseModel):
    """The 16 required output sections (prompt §22), in order."""

    discovery_summary: DiscoverySummary
    personal_data_inventory: list[PersonalDataElement] = Field(default_factory=list)
    classifications: list[PersonalDataElement] = Field(default_factory=list)
    data_subject_mappings: list[DataSubjectMapping] = Field(default_factory=list)
    purpose_mappings: list[PurposeMapping] = Field(default_factory=list)
    processing_activities: list[ProcessingActivity] = Field(default_factory=list)
    data_flows: list[DataFlowMapping] = Field(default_factory=list)
    processors_and_vendors: list[VendorProcessor] = Field(default_factory=list)
    retention_findings: list[RetentionFinding] = Field(default_factory=list)
    access_findings: list[AccessFinding] = Field(default_factory=list)
    risk_and_gap_findings: list[RiskGapFinding] = Field(default_factory=list)
    ropa_records: list[RopaRecord] = Field(default_factory=list)
    human_review_items: list[HumanReviewItem] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    change_detection: list[ChangeDetectionEntry] = Field(default_factory=list)
    confidence_summary: ConfidenceSummary
