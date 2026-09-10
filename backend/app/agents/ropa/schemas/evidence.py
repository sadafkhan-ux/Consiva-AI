"""Structured discovery evidence -- the ONLY thing that crosses from the
discovery connectors (connectors/postgres.py, mysql.py, api.py, files.py) into
the rest of the ROPA pipeline (prompt §2). No raw DB dumps, raw HTML, secrets,
or credentials ever cross this boundary.

Every record carries a `local_id` (stable within one discovery run) so rules
and LLM output can cite specific evidence (prompt §4, evidence-first rule)
before anything has a database UUID.
"""

from typing import Literal

from pydantic import BaseModel, Field

DpaStatus = Literal[
    "Confirmed", "Missing", "Unknown", "Expired", "Not Available", "Needs Review"
]


class SourceRecord(BaseModel):
    local_id: str
    name: str
    source_type: Literal["database", "api", "file", "application"]
    connector: str | None = None  # "postgres" | "mysql" | "api" | "files"
    location: str | None = None  # host/region if known -- never a connection string


class TableRecord(BaseModel):
    local_id: str
    source_local_id: str
    schema_name: str | None = None
    table_name: str


class ColumnRecord(BaseModel):
    local_id: str
    table_local_id: str
    column_name: str
    data_type: str
    nullable: bool | None = None
    # Approved minimum sample pattern only (e.g. "###-##-####") -- never a raw value.
    sample_pattern: str | None = None
    # Trusted metadata carried in from a prior human-approved run (prompt §6: never
    # silently overwritten by a new rule/LLM pass).
    existing_classification: str | None = None
    existing_data_subject: str | None = None
    existing_purpose: str | None = None


class RelationshipRecord(BaseModel):
    """A foreign-key relationship between two discovered tables. This is the
    structural signal processing_activity_service uses to decide that two tables
    belong to the same business activity rather than guessing from names."""

    local_id: str
    from_table_local_id: str
    from_column: str
    to_table_local_id: str
    to_column: str
    constraint_name: str | None = None


class ApiEndpointRecord(BaseModel):
    local_id: str
    source_local_id: str
    method: str
    route: str
    request_fields: list[str] = Field(default_factory=list)
    response_fields: list[str] = Field(default_factory=list)


class FileRecord(BaseModel):
    local_id: str
    source_local_id: str
    file_path: str
    file_type: str
    fields: list[str] = Field(default_factory=list)


class RoleRecord(BaseModel):
    local_id: str
    name: str
    system_local_id: str | None = None
    access_level: str | None = None


class VendorRecord(BaseModel):
    local_id: str
    name: str
    role: str | None = None  # e.g. "processor" | "sub-processor" | "controller"
    integration_local_id: str | None = None  # the source/api this vendor connects to
    location: str | None = None
    # Never assumed -- must come from documented contract metadata (prompt §11).
    dpa_status: DpaStatus = "Unknown"


class BusinessMetadataRecord(BaseModel):
    """Organizational context attached to a source/table/column, supplied by the
    org rather than inferred (prompt §13)."""

    local_id: str
    subject_local_id: str  # the source/table/column local_id this annotates
    business_owner: str | None = None
    retention_policy: str | None = None
    department: str | None = None


class HumanReviewDecisionRecord(BaseModel):
    """A previously recorded human decision, replayed as trusted input so it is
    never silently overwritten by a new run (prompt §19)."""

    local_id: str
    target_local_id: str
    decision: Literal["approved", "rejected", "edited", "confirmed", "marked_unknown"]
    decided_by: str | None = None
    decided_at: str | None = None  # ISO timestamp
    notes: str | None = None


class DiscoveryEvidence(BaseModel):
    """Top-level evidence bundle for one discovery run, produced by the
    connectors and consumed by discovery_service.py / classification_service.py."""

    org_id: str
    discovery_run_id: str
    sources: list[SourceRecord] = Field(default_factory=list)
    tables: list[TableRecord] = Field(default_factory=list)
    columns: list[ColumnRecord] = Field(default_factory=list)
    relationships: list[RelationshipRecord] = Field(default_factory=list)
    api_endpoints: list[ApiEndpointRecord] = Field(default_factory=list)
    files: list[FileRecord] = Field(default_factory=list)
    roles: list[RoleRecord] = Field(default_factory=list)
    vendors: list[VendorRecord] = Field(default_factory=list)
    business_metadata: list[BusinessMetadataRecord] = Field(default_factory=list)
    human_review_decisions: list[HumanReviewDecisionRecord] = Field(default_factory=list)
