"""Domain shapes produced by the ROPA agent's reasoning stages (prompt §5-§20):
classification, purpose/data-subject mapping, processing activities, data
flows, vendors, retention/access findings, risk/gap findings, the ROPA record
itself, human-review items, and baseline change detection.

Every material finding carries `evidence` (local_id references into the
DiscoveryEvidence supplied for the run -- prompt §4) and a `confidence` score.
Unknown values are explicit, never omitted (prompt §3 zero-hallucination
policy) -- that is why almost every optional field defaults to None/"Unknown"
rather than being left out of the schema.
"""

from typing import Literal

from pydantic import BaseModel, Field

DpaStatus = Literal[
    "Confirmed", "Missing", "Unknown", "Expired", "Not Available", "Needs Review"
]

ReviewDecision = Literal["approved", "rejected", "edited", "confirmed", "marked_unknown"]

RiskSeverity = Literal["low", "medium", "high", "critical"]

GapStatus = Literal["Potential Gap", "Requires Review", "Evidence Incomplete", "Potential Privacy Risk"]

ChangeType = Literal[
    "new_table",
    "new_field",
    "deleted_field",
    "changed_field",
    "changed_data_type",
    "new_personal_data_category",
    "new_vendor",
    "changed_processor",
    "changed_purpose",
    "changed_processing_activity",
    "changed_data_flow",
    "changed_retention",
    "changed_access",
]


class PersonalDataElement(BaseModel):
    """One discovered field classified as (or ruled out from being) personal data."""

    source: str
    table: str | None = None
    column: str
    classification: str  # e.g. "Contact Data", "Online Identifier" -- or "Unknown"
    data_subject: str = "Unknown"
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(description="local_id references into the supplied evidence")
    review_required: bool = False
    review_reason: str | None = None


class DataSubjectMapping(BaseModel):
    element_evidence: list[str] = Field(description="local_id references this mapping applies to")
    data_subject: str  # "Customer" | "Employee" | ... | "Unknown"
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    review_required: bool = False


class PurposeMapping(BaseModel):
    element_evidence: list[str] = Field(description="local_id references this mapping applies to")
    purpose: str  # "Unknown" when not established
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    review_required: bool = False


class DataFlowStep(BaseModel):
    from_node: str
    to_node: str
    evidence: list[str] = Field(default_factory=list)


class VendorProcessor(BaseModel):
    name: str
    role: str | None = None  # "processor" | "sub-processor" | "controller" | "Unknown"
    purpose: str | None = None
    data_shared: list[str] = Field(default_factory=list)
    location: str | None = None  # "Unknown" when not established (prompt §14)
    dpa_status: DpaStatus = "Unknown"
    evidence: list[str] = Field(default_factory=list)


class TransferInfo(BaseModel):
    is_international_transfer: bool | None = None
    destination_country: str | None = None  # None/"Unknown" when not established
    evidence: list[str] = Field(default_factory=list)
    review_required: bool = False


class ProcessingActivity(BaseModel):
    name: str
    description: str | None = None
    data_subjects: list[str] = Field(default_factory=list)
    personal_data_categories: list[str] = Field(default_factory=list)
    purpose: str = "Unknown"
    source_systems: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    review_required: bool = False


class RiskGapFinding(BaseModel):
    """An evidence-backed gap -- NOT a legal-violation claim (prompt §16-§17)."""

    finding: str
    status: GapStatus
    related_evidence: list[str] = Field(default_factory=list)
    severity: RiskSeverity
    severity_factors: list[str] = Field(
        description="the specific factors (sensitivity, confidence, exposure, ...) that produced this severity"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    recommendation: str | None = None
    review_required: bool = False


class RopaRecord(BaseModel):
    """One row of the Record of Processing Activities (prompt §15)."""

    processing_activity: str
    description: str | None = None
    purpose: str = "Unknown"
    data_subjects: list[str] = Field(default_factory=list)
    personal_data_categories: list[str] = Field(default_factory=list)
    data_elements: list[str] = Field(default_factory=list)
    source_systems: list[str] = Field(default_factory=list)
    storage_locations: list[str] = Field(default_factory=list)
    processors: list[VendorProcessor] = Field(default_factory=list)
    recipients: list[str] = Field(default_factory=list)
    data_flows: list[DataFlowStep] = Field(default_factory=list)
    retention: str = "Unknown"
    access_roles: list[str] = Field(default_factory=list)
    business_owner: str = "Unknown"
    transfer_information: TransferInfo | None = None
    consent_or_processing_context: str | None = None
    security_control_status: str | None = None
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    review_required: bool = False
    generated_at: str  # ISO timestamp
    source_run_id: str
    version: int = 1


class HumanReviewItem(BaseModel):
    target: str  # what this review item refers to (element/mapping/activity/record ref)
    reason: str
    evidence: list[str] = Field(default_factory=list)
    decision: ReviewDecision | None = None  # None until a human acts on it
    decided_by: str | None = None
    decided_at: str | None = None
    notes: str | None = None


class ChangeDetectionEntry(BaseModel):
    """One material difference between the supplied baseline and the current
    discovery run (prompt §20). Never a silent rewrite of an approved record."""

    change_type: ChangeType
    target: str
    previous_value: str | None = None
    current_value: str | None = None
    evidence: list[str] = Field(default_factory=list)
    review_required: bool = True


class ConfidenceSummary(BaseModel):
    overall_confidence: float = Field(ge=0.0, le=1.0)
    low_confidence_count: int = 0
    review_required_count: int = 0
    notes: str | None = None
