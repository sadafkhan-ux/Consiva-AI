"""LangGraph state for the ROPA agent. Structured (typed Pydantic fields), not
a giant text blob -- each node reads/writes only the fields it owns, and the
final state maps 1:1 onto RopaAgentOutput's sections (schemas/output.py) for
assembly by ropa_service.py.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.output import AccessFinding, DataFlowMapping, RetentionFinding
from app.agents.ropa.schemas.ropa import (
    ChangeDetectionEntry,
    DataSubjectMapping,
    HumanReviewItem,
    PersonalDataElement,
    ProcessingActivity,
    PurposeMapping,
    RiskGapFinding,
    RopaRecord,
    VendorProcessor,
)

RopaRunStatus = Literal[
    "pending",
    "discovering",
    "classifying",
    "analyzing",
    "generating_ropa",
    "needs_review",
    "completed",
    "failed",
]


class AgentState(BaseModel):
    # -- run identity, set once at graph entry --
    source: str | None = None
    discovery_run_id: str
    organization_id: str

    # -- evidence, set by discovery_service.py --
    discovery_evidence: DiscoveryEvidence | None = None

    # -- classification stage, set by classification_service.py --
    personal_data_inventory: list[PersonalDataElement] = Field(default_factory=list)
    classifications: list[PersonalDataElement] = Field(default_factory=list)
    data_subjects: list[DataSubjectMapping] = Field(default_factory=list)

    # -- purpose stage, set by purpose_service.py --
    purposes: list[PurposeMapping] = Field(default_factory=list)

    # -- processing-activity / dataflow / vendor stages --
    processing_activities: list[ProcessingActivity] = Field(default_factory=list)
    data_flows: list[DataFlowMapping] = Field(default_factory=list)
    vendors: list[VendorProcessor] = Field(default_factory=list)

    # -- retention / access stages --
    retention: list[RetentionFinding] = Field(default_factory=list)
    access: list[AccessFinding] = Field(default_factory=list)

    # -- risk stage, set by risk_service.py --
    risk_findings: list[RiskGapFinding] = Field(default_factory=list)

    # -- final ROPA records, set by ropa_service.py --
    ropa_records: list[RopaRecord] = Field(default_factory=list)

    # -- human review + baseline change detection --
    review_items: list[HumanReviewItem] = Field(default_factory=list)
    changes: list[ChangeDetectionEntry] = Field(default_factory=list)

    # -- run bookkeeping --
    errors: list[str] = Field(default_factory=list)
    status: RopaRunStatus = "pending"
