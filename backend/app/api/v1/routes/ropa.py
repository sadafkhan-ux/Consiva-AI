"""Agent 2 (Data Discovery / ROPA) API.

Two ways evidence can reach the agent:

1. `POST /sources` + `POST /sources/{id}/discover` -- Consiva connects to the
   customer's source itself using a least-privilege credential resolved from the
   environment.

2. `POST /evidence` -- an EXTERNAL integration (a customer's own adapter, e.g. on
   PrepMyEvent's side) collects metadata in their environment and posts already-
   structured DiscoveryEvidence. Consiva never receives their production database
   credentials at all. This is the integration boundary the "external source ->
   orchestrator -> agent" architecture actually needs, and it is preferred for
   third-party production systems.

Every route is authenticated and org-scoped through the SAME dependency Agent 1
uses (core/security.get_current_user), so tenancy behaves identically.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.ropa.connectors.base import registered_connectors
from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.payload import CURRENT_SCHEMA_VERSION, SourcePayload
from app.core import integration_auth
from app.core.security import CurrentUser, get_current_user
from app.db.models import RopaIntegrationKey
from app.db.repositories import audit_repository, ropa_repository
from app.db.session import get_db
from app.services import ropa_run_service

router = APIRouter(prefix="/api/v1/ropa", tags=["ropa"])

# Keys that must never be accepted in a source's non-secret `config` blob --
# a secret belongs in the environment entry named by credential_ref, never here.
_FORBIDDEN_CONFIG_KEYS = {"password", "api_key", "secret", "token", "credential", "dsn", "connection_string"}


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    connector: str
    source_type: str
    config: dict = Field(default_factory=dict)
    credential_ref: str | None = Field(
        default=None,
        description="NAME of the environment entry holding the secret -- never the secret itself",
    )

    @field_validator("connector")
    @classmethod
    def _known_connector(cls, value: str) -> str:
        if value not in registered_connectors():
            raise ValueError(f"unknown connector {value!r}; available: {registered_connectors()}")
        return value

    @field_validator("source_type")
    @classmethod
    def _known_source_type(cls, value: str) -> str:
        allowed = {"database", "api", "file", "application"}
        if value not in allowed:
            raise ValueError(f"source_type must be one of {sorted(allowed)}")
        return value

    @field_validator("config")
    @classmethod
    def _no_inline_secrets(cls, value: dict) -> dict:
        leaked = _FORBIDDEN_CONFIG_KEYS & {k.lower() for k in value}
        if leaked:
            raise ValueError(
                f"config must not contain secrets {sorted(leaked)}; "
                "put the secret in the environment and reference it with credential_ref"
            )
        return value


class DataSourceResponse(BaseModel):
    id: uuid.UUID
    name: str
    connector: str
    source_type: str
    config: dict
    credential_ref: str | None
    enabled: bool
    last_verified_at: str | None = None


# Bounds on a single pushed payload. An external adapter is a machine caller, so
# these are the practical DoS ceiling -- a malformed or hostile adapter cannot
# make the agent chew through an unbounded schema.
MAX_TABLES_PER_PUSH = 2_000
MAX_COLUMNS_PER_PUSH = 50_000


class EvidenceIngest(SourcePayload):
    """The versioned wire contract, plus this backend's size ceilings.

    Inherits schema_version / correlation_id / generated_at validation from
    SourcePayload so the contract lives in exactly one place.
    """

    @field_validator("evidence")
    @classmethod
    def _within_size_limits(cls, value: DiscoveryEvidence) -> DiscoveryEvidence:
        if len(value.tables) > MAX_TABLES_PER_PUSH:
            raise ValueError(f"too many tables in one push (max {MAX_TABLES_PER_PUSH})")
        if len(value.columns) > MAX_COLUMNS_PER_PUSH:
            raise ValueError(f"too many columns in one push (max {MAX_COLUMNS_PER_PUSH})")
        return value


class IntegrationKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    expires_at: datetime | None = None


class IntegrationKeyCreated(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    api_key: str = Field(description="Shown ONCE. Never retrievable again -- store it now.")
    scopes: list[str]


class RunResponse(BaseModel):
    id: uuid.UUID
    source_name: str
    ingest_mode: str
    status: str
    tables_scanned: int
    columns_scanned: int
    personal_data_elements: int
    overall_confidence: float | None
    summary: dict
    error: str | None


class DecisionRequest(BaseModel):
    decision: str
    reason: str | None = Field(default=None, max_length=2000)
    edited_payload: dict | None = None

    @field_validator("decision")
    @classmethod
    def _known_decision(cls, value: str) -> str:
        if value not in ("approved", "rejected", "edited"):
            raise ValueError("decision must be 'approved', 'rejected' or 'edited'")
        return value


def _run_response(run) -> RunResponse:
    return RunResponse(
        id=run.id, source_name=run.source_name, ingest_mode=run.ingest_mode, status=run.status,
        tables_scanned=run.tables_scanned, columns_scanned=run.columns_scanned,
        personal_data_elements=run.personal_data_elements,
        overall_confidence=float(run.overall_confidence) if run.overall_confidence is not None else None,
        summary=run.summary, error=run.error,
    )


@router.get("/connectors")
async def list_connectors(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"connectors": registered_connectors()}


@router.post("/sources", response_model=DataSourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source(
    payload: DataSourceCreate,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> DataSourceResponse:
    source = await ropa_repository.create_data_source(
        db, org_id=uuid.UUID(user.org_id), name=payload.name, connector=payload.connector,
        source_type=payload.source_type, config=payload.config, credential_ref=payload.credential_ref,
    )
    await db.commit()
    return DataSourceResponse(
        id=source.id, name=source.name, connector=source.connector, source_type=source.source_type,
        config=source.config, credential_ref=source.credential_ref, enabled=source.enabled,
    )


@router.get("/sources", response_model=list[DataSourceResponse])
async def list_sources(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[DataSourceResponse]:
    rows = await ropa_repository.list_data_sources(db, uuid.UUID(user.org_id))
    return [
        DataSourceResponse(
            id=r.id, name=r.name, connector=r.connector, source_type=r.source_type, config=r.config,
            credential_ref=r.credential_ref, enabled=r.enabled,
            last_verified_at=r.last_verified_at.isoformat() if r.last_verified_at else None,
        )
        for r in rows
    ]


@router.post("/sources/{source_id}/discover", response_model=RunResponse)
async def discover_source(
    source_id: uuid.UUID,
    idempotency_key: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> RunResponse:
    try:
        run = await ropa_run_service.run_discovery_for_source(
            db, org_id=uuid.UUID(user.org_id), source_id=source_id,
            user_id=uuid.UUID(user.user_id), idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await db.commit()
    return _run_response(run)


@router.post("/integration-keys", response_model=IntegrationKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_integration_key(
    payload: IntegrationKeyCreate,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> IntegrationKeyCreated:
    """Mint a service credential for an external adapter. Requires a human user
    session -- a key can never mint another key."""
    full_key, prefix, key_hash = integration_auth.generate_key()
    row = RopaIntegrationKey(
        org_id=uuid.UUID(user.org_id), name=payload.name, key_prefix=prefix, key_hash=key_hash,
        scopes=["evidence:write"], expires_at=payload.expires_at,
        created_by_user_id=uuid.UUID(user.user_id),
    )
    db.add(row)
    await db.flush()
    await audit_repository.record(
        db, org_id=uuid.UUID(user.org_id), actor_user_id=uuid.UUID(user.user_id),
        action="ropa_integration_key.created", entity_type="ropa_integration_key", entity_id=row.id,
        after={"name": payload.name, "key_prefix": prefix},  # never the key itself
    )
    await db.commit()
    return IntegrationKeyCreated(
        id=row.id, name=row.name, key_prefix=prefix, api_key=full_key, scopes=list(row.scopes)
    )


@router.post("/evidence", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def ingest_evidence(
    payload: EvidenceIngest,
    db: AsyncSession = Depends(get_db),
    principal: integration_auth.IntegrationPrincipal = Depends(
        integration_auth.get_integration_principal
    ),
) -> RunResponse:
    """Accept externally-collected evidence from an integration adapter.

    Authenticated with an INTEGRATION KEY, not a user session: the adapter runs
    inside the customer's own infrastructure (e.g. PrepMyEvent's VM) and has no
    Supabase user to act as. Consiva never holds that system's database
    credentials on this path -- only the curated metadata they choose to send.
    """
    integration_auth.require_scope(principal, "evidence:write")
    run = await ropa_run_service.ingest_pushed_evidence(
        db, org_id=uuid.UUID(principal.org_id), source_name=payload.source_name,
        evidence=payload.evidence, user_id=None,
        idempotency_key=payload.idempotency_key,
        integration_key_name=principal.name,
        correlation_id=payload.correlation_id,
        schema_version=payload.schema_version,
    )
    await db.commit()
    return _run_response(run)


@router.get("/runs", response_model=list[RunResponse])
async def list_runs(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[RunResponse]:
    """Recent discovery runs for the caller's org, newest first."""
    rows = await ropa_repository.list_runs(db, uuid.UUID(user.org_id), limit=min(limit, 200))
    return [_run_response(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> RunResponse:
    run = await ropa_repository.get_run(db, run_id, uuid.UUID(user.org_id))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return _run_response(run)


@router.get("/runs/{run_id}/records")
async def list_records(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    org_id = uuid.UUID(user.org_id)
    if await ropa_repository.get_run(db, run_id, org_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    rows = await ropa_repository.list_records_for_run(db, run_id, org_id)
    return [
        {
            "id": str(r.id), "processing_activity": r.processing_activity, "version": r.version,
            "status": r.status, "review_required": r.review_required,
            "confidence": float(r.confidence) if r.confidence is not None else None,
            "payload": r.edited_payload or r.payload,
            "supersedes_id": str(r.supersedes_id) if r.supersedes_id else None,
        }
        for r in rows
    ]


@router.get("/runs/{run_id}/findings")
async def list_findings(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    org_id = uuid.UUID(user.org_id)
    if await ropa_repository.get_run(db, run_id, org_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    rows = await ropa_repository.list_findings_for_run(db, run_id, org_id)
    return [
        {
            "id": str(f.id), "finding": f.finding, "gap_status": f.gap_status, "severity": f.severity,
            "severity_factors": f.severity_factors, "confidence": float(f.confidence) if f.confidence else None,
            "recommendation": f.recommendation, "review_status": f.review_status,
        }
        for f in rows
    ]


@router.get("/runs/{run_id}/changes")
async def list_changes(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    org_id = uuid.UUID(user.org_id)
    if await ropa_repository.get_run(db, run_id, org_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    rows = await ropa_repository.list_changes_for_run(db, run_id, org_id)
    return [
        {
            "id": str(c.id), "change_type": c.change_type, "target": c.target,
            "previous_value": c.previous_value, "current_value": c.current_value,
            "is_material": c.is_material, "review_required": c.review_required,
        }
        for c in rows
    ]


@router.post("/runs/{run_id}/promote-baseline")
async def promote_baseline(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Make this run's schema the new comparison baseline.

    Requires a HUMAN session on purpose: an automatic promotion would silently
    absorb a change nobody reviewed, which defeats the point of monitoring.
    """
    org_id = uuid.UUID(user.org_id)
    run = await ropa_repository.get_run(db, run_id, org_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if run.status != "completed":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"cannot promote a run with status {run.status!r}; only a completed run",
        )

    snapshot = run.summary.get("schema_snapshot")
    if not snapshot:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "this run has no stored schema snapshot to promote",
        )

    baseline = await ropa_repository.promote_baseline(
        db, org_id=org_id, source_name=run.source_name, run_id=run.id,
        snapshot=snapshot, user_id=uuid.UUID(user.user_id),
    )
    await audit_repository.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action="ropa_baseline.promoted", entity_type="ropa_schema_baseline", entity_id=baseline.id,
        after={"source_name": run.source_name, "run_id": str(run.id)},
    )
    await db.commit()
    return {"baseline_id": str(baseline.id), "source_name": run.source_name, "is_current": True}


@router.post("/records/{record_id}/decision")
async def decide_record(
    record_id: uuid.UUID,
    payload: DecisionRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        record = await ropa_run_service.decide_record(
            db, record_id=record_id, org_id=uuid.UUID(user.org_id), user_id=uuid.UUID(user.user_id),
            decision=payload.decision, reason=payload.reason, edited_payload=payload.edited_payload,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return {"id": str(record.id), "status": record.status, "version": record.version}


@router.post("/findings/{finding_id}/decision")
async def decide_finding(
    finding_id: uuid.UUID,
    payload: DecisionRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        finding = await ropa_run_service.decide_finding(
            db, finding_id=finding_id, org_id=uuid.UUID(user.org_id), user_id=uuid.UUID(user.user_id),
            decision=payload.decision, reason=payload.reason, edited_payload=payload.edited_payload,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return {"id": str(finding.id), "review_status": finding.review_status}
