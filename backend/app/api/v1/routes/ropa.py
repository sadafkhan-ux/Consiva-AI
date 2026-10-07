"""Agent 2 (Data Discovery / ROPA) API.

Two ways evidence can reach the agent:

1. `POST /sources` + `POST /sources/{id}/discover` -- Consiva connects to the
   customer's source itself using a least-privilege credential, resolved either
   from the environment (`credential_ref`) or from an encrypted value set via
   `POST /sources/{id}/credential` (migration 0031, for self-service onboarding
   without an operator editing the environment).

2. `POST /evidence` -- an EXTERNAL integration (a customer's own adapter, e.g. on
   PrepMyEvent's side) collects metadata in their environment and posts already-
   structured DiscoveryEvidence. Consiva never receives their production database
   credentials at all. This is the integration boundary the "external source ->
   orchestrator -> agent" architecture actually needs, and it is preferred for
   third-party production systems.

Source management (`/sources`, `/sources/test-connection`, `/sources/{id}/discover`,
`/sources/{id}/credential`, `/integration-keys`) stays human-only, through the SAME dependency Agent 1 uses
(core/security.get_current_user) -- these mint or spend credentials, which must
stay a person's decision.

The read endpoints (`/runs`, `/runs/{id}`, `/records`, `/findings`, `/changes`,
`/classifications`) and baseline promotion (`/runs/{id}/promote-baseline`) accept EITHER a human
session OR a service key holding the `evidence:read` scope
(core/integration_auth.get_ropa_reader), so an adapter that pushed evidence via
`/evidence` can read its own results back -- and promote them -- without a user
session. Either way the connection is scoped to the caller's own organisation
before any row is read or written (migration 0018), so a key can only ever
touch what it already organisation-scoped a write to.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.ropa.connectors import factory as connector_factory
from app.agents.ropa.connectors.base import ConnectorError, registered_connectors
from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.payload import SourcePayload
from app.core import integration_auth
from app.core.security import CurrentUser, get_current_user
from app.db.models import RopaIntegrationKey
from app.db.repositories import audit_repository, ropa_repository
from app.db.session import get_db
from app.services import agent_webhook_service, ropa_run_service

router = APIRouter(prefix="/api/v1/ropa", tags=["ropa"])

# Keys that must never be accepted in a source's non-secret `config` blob --
# a secret belongs in the environment entry named by credential_ref, never here.
_FORBIDDEN_CONFIG_KEYS = {"password", "api_key", "secret", "token", "credential", "dsn", "connection_string"}
_KNOWN_SOURCE_TYPES = {"database", "api", "file", "application"}


def _check_known_connector(value: str) -> str:
    if value not in registered_connectors():
        raise ValueError(f"unknown connector {value!r}; available: {registered_connectors()}")
    return value


def _check_no_inline_secrets(value: dict) -> dict:
    leaked = _FORBIDDEN_CONFIG_KEYS & {k.lower() for k in value}
    if leaked:
        raise ValueError(
            f"config must not contain secrets {sorted(leaked)}; "
            "put the secret in the environment and reference it with credential_ref, "
            "or (for a connection test only) in the separate `secret` field"
        )
    return value


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    connector: str
    source_type: str
    config: dict = Field(default_factory=dict)
    credential_ref: str | None = Field(
        default=None,
        description="NAME of the environment entry holding the secret -- never the secret itself",
    )

    _check_connector = field_validator("connector")(_check_known_connector)
    _check_secrets = field_validator("config")(_check_no_inline_secrets)

    @field_validator("source_type")
    @classmethod
    def _known_source_type(cls, value: str) -> str:
        if value not in _KNOWN_SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {sorted(_KNOWN_SOURCE_TYPES)}")
        return value


class TestConnectionRequest(BaseModel):
    """Tests a configuration BEFORE it is saved -- connector/config/credential
    need not correspond to any stored RopaDataSource row. `secret` is used
    exactly once, to attempt a connection, and is never persisted or logged
    (see connectors/factory.py's raw_secret precedence)."""

    connector: str
    config: dict = Field(default_factory=dict)
    credential_ref: str | None = None
    secret: str | None = Field(
        default=None, max_length=10_000,
        description="Raw credential value, used once to test the connection. Never stored.",
    )

    _check_connector = field_validator("connector")(_check_known_connector)
    _check_secrets = field_validator("config")(_check_no_inline_secrets)


class TestConnectionResponse(BaseModel):
    ok: bool
    message: str


class DataSourceResponse(BaseModel):
    id: uuid.UUID
    name: str
    connector: str
    source_type: str
    config: dict
    credential_ref: str | None
    # Never the ciphertext itself -- just whether one has been set, so a
    # console can show "credential configured" without ever being able to
    # leak or re-derive it.
    has_stored_credential: bool = False
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

    # Route-level only -- deliberately NOT on SourcePayload, which the external
    # adapter SDK (ropa_integration/) also builds against. This pair is for
    # Consiva's own first-party caller (the one that knows which environment
    # started this run) to override the fixed agent webhook destination for
    # this run only; see DiscoverRequest's docstring above.
    callback_url: str | None = Field(default=None, max_length=2000)
    callback_secret: str | None = Field(default=None, max_length=500)

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
    expires_at: datetime | None = Field(
        default=None,
        description="When the key stops working. Null means it does not expire -- "
                    "which is the point of a service key, and why revoking one has to "
                    "stay possible.",
    )
    scopes: list[str] | None = Field(
        default=None,
        description="What this key may do. Defaults to ['evidence:write'] (the ROPA "
                    "adapter). Use ['consent:scan'] for a key that drives the Consent "
                    "Agent's integration API.",
    )

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, value: list[str] | None) -> list[str] | None:
        """Refuse an unrecognised scope at mint time.

        A typo'd scope is stored happily and then matches nothing, so the key fails
        every request with a 403 that names a scope the operator believes they granted.
        Failing here, once, with the valid list in hand, is the cheaper place to find
        out.
        """
        if value is None:
            return None
        unknown = sorted(set(value) - set(integration_auth.KNOWN_SCOPES))
        if unknown:
            raise ValueError(
                f"unknown scope(s) {unknown}; valid scopes are "
                f"{list(integration_auth.KNOWN_SCOPES)}"
            )
        return value


class IntegrationKeyCreated(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    api_key: str = Field(description="Shown ONCE. Never retrievable again -- store it now.")
    scopes: list[str]


class IntegrationKeySummary(BaseModel):
    """A key as it can safely be shown afterwards.

    Deliberately has no `api_key` field and never could: only a SHA-256 hash was
    stored, so the key is unrecoverable by design rather than by omission. The prefix
    distinguishes two keys without being usable as one.
    """

    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    enabled: bool
    created_at: datetime
    last_used_at: datetime | None = Field(
        default=None,
        description="Null means this key has never authenticated a request -- which "
                    "usually means an integration that was configured and then never "
                    "finished, and a credential that can be revoked at no cost.",
    )
    expires_at: datetime | None = None
    revoked_at: datetime | None = Field(
        default=None, description="Set means the key is permanently dead."
    )


class RunResponse(BaseModel):
    id: uuid.UUID
    data_source_id: uuid.UUID | None
    source_name: str
    ingest_mode: str
    status: str
    tables_scanned: int
    columns_scanned: int
    personal_data_elements: int
    overall_confidence: float | None
    summary: dict
    error: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


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
        id=run.id, data_source_id=run.data_source_id, source_name=run.source_name,
        ingest_mode=run.ingest_mode, status=run.status,
        tables_scanned=run.tables_scanned, columns_scanned=run.columns_scanned,
        personal_data_elements=run.personal_data_elements,
        overall_confidence=float(run.overall_confidence) if run.overall_confidence is not None else None,
        summary=run.summary, error=run.error,
        created_at=run.created_at.isoformat(),
        started_at=run.started_at.isoformat() if run.started_at else None,
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
    )


@router.get("/connectors")
async def list_connectors(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"connectors": registered_connectors()}


@router.post("/sources/test-connection", response_model=TestConnectionResponse)
async def test_connection(
    payload: TestConnectionRequest,
    user: CurrentUser = Depends(get_current_user),
) -> TestConnectionResponse:
    """Tests BEFORE saving -- builds a connector from exactly what the form
    holds (never persisted) and attempts `test_connection()`: connect,
    verify least-privilege, lock read-only, then stop -- no schema read.

    Returns 200 with `ok: false` for a configuration/connection failure
    (wrong password, unreachable host, over-privileged role, ...), since that
    is an expected, actionable outcome for this endpoint, not a server error.
    A 4xx/5xx here is reserved for something actually wrong with the request
    or the server, which is exactly what FastAPI's default handling already
    gives for an unregistered connector or a malformed body.
    """
    try:
        connector = connector_factory.build_connector(
            connector=payload.connector, config=payload.config,
            credential_ref=payload.credential_ref, raw_secret=payload.secret,
        )
        await connector.test_connection()
    except ConnectorError as exc:
        return TestConnectionResponse(ok=False, message=str(exc))
    return TestConnectionResponse(ok=True, message="Connection succeeded.")


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
    await audit_repository.record(
        db, org_id=uuid.UUID(user.org_id), actor_user_id=uuid.UUID(user.user_id),
        action="ropa_data_source.created", entity_type="ropa_data_source", entity_id=source.id,
        # Non-secret config only -- credential_ref is a NAME, never the secret (see
        # DataSourceCreate's own validator and migration 0007's header).
        after={
            "name": source.name, "connector": source.connector, "source_type": source.source_type,
            "credential_ref": source.credential_ref,
        },
    )
    await db.commit()
    return DataSourceResponse(
        id=source.id, name=source.name, connector=source.connector, source_type=source.source_type,
        config=source.config, credential_ref=source.credential_ref,
        has_stored_credential=source.credential_ciphertext is not None, enabled=source.enabled,
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
            credential_ref=r.credential_ref, has_stored_credential=r.credential_ciphertext is not None,
            enabled=r.enabled,
            last_verified_at=r.last_verified_at.isoformat() if r.last_verified_at else None,
        )
        for r in rows
    ]


class SetSourceCredential(BaseModel):
    secret: str = Field(min_length=1, max_length=10_000, description="The raw credential value. "
                        "Encrypted immediately; never stored or logged in plaintext.")


@router.post("/sources/{source_id}/credential", status_code=status.HTTP_204_NO_CONTENT)
async def set_source_credential(
    source_id: uuid.UUID,
    payload: SetSourceCredential,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> None:
    """Self-service alternative to a `credential_ref` env var: encrypts
    `secret` (Fernet, ROPA_CREDENTIAL_ENCRYPTION_KEY) and stores only the
    ciphertext. Requires a human session -- minting a usable credential is the
    same class of action as minting an integration key, never a key's own job.

    Calling this again for the same source ROTATES it: the previous
    ciphertext is simply overwritten, same as changing an env var rotates the
    credential_ref path.
    """
    org_id = uuid.UUID(user.org_id)
    source = await ropa_repository.get_data_source(db, source_id, org_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"data source {source_id} not found")

    try:
        ciphertext = connector_factory.encrypt_credential(payload.secret)
    except connector_factory.CredentialEncryptionNotConfigured as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await ropa_repository.set_source_credential(db, source, ciphertext)
    await audit_repository.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action="ropa_data_source.credential_set", entity_type="ropa_data_source", entity_id=source.id,
        # The secret and its ciphertext are both deliberately absent -- this
        # audit entry proves WHEN and BY WHOM, never what was set.
        after={"name": source.name},
    )
    await db.commit()


class SetSourceEnabled(BaseModel):
    enabled: bool


@router.post("/sources/{source_id}/enabled", response_model=DataSourceResponse)
async def set_source_enabled(
    source_id: uuid.UUID,
    payload: SetSourceEnabled,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> DataSourceResponse:
    """Disable a source to stop future discovery runs against it without
    deleting its history, or re-enable one. `run_discovery_for_source`
    already refuses to enqueue a job for a disabled source -- this is the
    only endpoint that flips the flag it checks."""
    org_id = uuid.UUID(user.org_id)
    source = await ropa_repository.get_data_source(db, source_id, org_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"data source {source_id} not found")

    await ropa_repository.set_source_enabled(db, source, payload.enabled)
    await audit_repository.record(
        db, org_id=org_id, actor_user_id=uuid.UUID(user.user_id),
        action="ropa_data_source.enabled_changed", entity_type="ropa_data_source", entity_id=source.id,
        after={"name": source.name, "enabled": source.enabled},
    )
    await db.commit()
    return DataSourceResponse(
        id=source.id, name=source.name, connector=source.connector, source_type=source.source_type,
        config=source.config, credential_ref=source.credential_ref,
        has_stored_credential=source.credential_ciphertext is not None, enabled=source.enabled,
        last_verified_at=source.last_verified_at.isoformat() if source.last_verified_at else None,
    )


class DiscoverRequest(BaseModel):
    """Optional body for POST /sources/{id}/discover. Both fields are only
    needed when this agent instance is shared across more than one
    environment (see agent_webhook_service.py's module docstring) -- the
    caller that knows which environment started this run supplies its own
    callback destination, overriding the fixed AGENT_WEBHOOK_URL/
    AGENT_WEBHOOK_SECRET for this run's completion/failure notification only.
    """

    callback_url: str | None = Field(default=None, max_length=2000)
    callback_secret: str | None = Field(default=None, max_length=500)


@router.post("/sources/{source_id}/discover", response_model=RunResponse)
async def discover_source(
    source_id: uuid.UUID,
    idempotency_key: str | None = None,
    payload: DiscoverRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> RunResponse:
    """Queues a discovery run and returns immediately with status="pending" --
    it does not wait for the connector to actually read the source. Poll
    `GET /runs/{id}` (or the other run endpoints) for status="discovering" ->
    "completed"/"failed"; a large customer database can take a while, and this
    endpoint never blocks on that."""
    try:
        callback_url, callback_secret_ciphertext = await agent_webhook_service.prepare_callback_override(
            payload.callback_url if payload else None, payload.callback_secret if payload else None
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    try:
        run = await ropa_run_service.run_discovery_for_source(
            db, org_id=uuid.UUID(user.org_id), source_id=source_id,
            user_id=uuid.UUID(user.user_id), idempotency_key=idempotency_key,
            callback_url=callback_url, callback_secret_ciphertext=callback_secret_ciphertext,
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
        scopes=list(payload.scopes or ["evidence:write"]), expires_at=payload.expires_at,
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


@router.get("/integration-keys", response_model=list[IntegrationKeySummary])
async def list_integration_keys(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[IntegrationKeySummary]:
    """Every service key issued for this organisation.

    Exists because a key that does not expire has to be ACCOUNTABLE instead: somebody
    must be able to answer "what credentials can reach our data, and is each one still
    being used". Without this the only answer was a query against the database.

    The key itself is not here and cannot be -- only its hash was ever stored. The
    prefix is enough to tell two keys apart and is safe to display; `last_used_at` is
    what tells you a key is dormant and can be revoked.
    """
    rows = (await db.execute(
        select(RopaIntegrationKey)
        .where(RopaIntegrationKey.org_id == uuid.UUID(user.org_id))
        .order_by(RopaIntegrationKey.created_at.desc())
    )).scalars().all()
    return [
        IntegrationKeySummary(
            id=r.id, name=r.name, key_prefix=r.key_prefix, scopes=list(r.scopes or []),
            enabled=r.enabled, created_at=r.created_at, last_used_at=r.last_used_at,
            expires_at=r.expires_at, revoked_at=r.revoked_at,
        )
        for r in rows
    ]


@router.post("/integration-keys/{key_id}/revoke", response_model=IntegrationKeySummary)
async def revoke_integration_key(
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> IntegrationKeySummary:
    """Stop a service key working, permanently.

    THE REASON A NON-EXPIRING KEY IS ALLOWED TO EXIST AT ALL

    A credential with no expiry is only acceptable while it can be withdrawn the moment
    it is suspected. Until this endpoint existed, a key could be minted and never
    turned off except by editing the database by hand -- which means in practice it
    would not have been turned off.

    Requires a human session, for the same reason minting does: a key must not be able
    to revoke another key, or an attacker holding one could disable the monitoring that
    would catch them.

    Revoking is deliberately not deleting. The row stays, so the audit trail and
    `last_used_at` still answer what that credential did and when it stopped.
    """
    row = (await db.execute(
        select(RopaIntegrationKey).where(
            RopaIntegrationKey.id == key_id,
            RopaIntegrationKey.org_id == uuid.UUID(user.org_id),
        )
    )).scalar_one_or_none()
    if row is None:
        # 404 rather than 403 for a key belonging to another organisation: the API does
        # not confirm that an id it will not show you exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Integration key {key_id} not found")

    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        row.enabled = False
        await audit_repository.record(
            db, org_id=uuid.UUID(user.org_id), actor_user_id=uuid.UUID(user.user_id),
            action="ropa_integration_key.revoked", entity_type="ropa_integration_key",
            entity_id=row.id, before={"enabled": True},
            after={"name": row.name, "key_prefix": row.key_prefix, "enabled": False},
        )
        await db.commit()
        await db.refresh(row)

    return IntegrationKeySummary(
        id=row.id, name=row.name, key_prefix=row.key_prefix, scopes=list(row.scopes or []),
        enabled=row.enabled, created_at=row.created_at, last_used_at=row.last_used_at,
        expires_at=row.expires_at, revoked_at=row.revoked_at,
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
    try:
        callback_url, callback_secret_ciphertext = await agent_webhook_service.prepare_callback_override(
            payload.callback_url, payload.callback_secret
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    run, is_new = await ropa_run_service.ingest_pushed_evidence(
        db, org_id=uuid.UUID(principal.org_id), source_name=payload.source_name,
        evidence=payload.evidence, user_id=None,
        idempotency_key=payload.idempotency_key,
        integration_key_name=principal.name,
        correlation_id=payload.correlation_id,
        schema_version=payload.schema_version,
        callback_url=callback_url, callback_secret_ciphertext=callback_secret_ciphertext,
    )
    await db.commit()
    if is_new:
        await agent_webhook_service.send_event(
            "ropa.run.completed", run_id=str(run.id),
            callback_url=run.agent_callback_url,
            callback_secret=agent_webhook_service.resolve_callback_secret(run.agent_callback_secret_ciphertext),
        )
    return _run_response(run)


@router.get("/runs", response_model=list[RunResponse])
async def list_runs(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(integration_auth.get_ropa_reader),
) -> list[RunResponse]:
    """Recent discovery runs for the caller's org, newest first."""
    rows = await ropa_repository.list_runs(db, uuid.UUID(user.org_id), limit=min(limit, 200))
    return [_run_response(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(integration_auth.get_ropa_reader),
) -> RunResponse:
    run = await ropa_repository.get_run(db, run_id, uuid.UUID(user.org_id))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return _run_response(run)


@router.get("/runs/{run_id}/records")
async def list_records(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(integration_auth.get_ropa_reader),
) -> list[dict]:
    org_id = uuid.UUID(user.org_id)
    if await ropa_repository.get_run(db, run_id, org_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    rows = await ropa_repository.list_records_for_run(db, run_id, org_id)
    return [
        {
            "id": str(r.id), "source_name": r.source_name, "processing_activity": r.processing_activity,
            "version": r.version, "status": r.status, "review_required": r.review_required,
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
    user: CurrentUser = Depends(integration_auth.get_ropa_reader),
) -> list[dict]:
    org_id = uuid.UUID(user.org_id)
    if await ropa_repository.get_run(db, run_id, org_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    rows = await ropa_repository.list_findings_for_run(db, run_id, org_id)
    return [
        {
            "id": str(f.id), "run_id": str(f.discovery_run_id), "finding": f.finding,
            "category": f.category, "gap_status": f.gap_status, "severity": f.severity,
            "severity_factors": f.severity_factors, "confidence": float(f.confidence) if f.confidence else None,
            "recommendation": f.recommendation, "review_status": f.review_status,
            "created_at": f.created_at.isoformat(),
        }
        for f in rows
    ]


@router.get("/runs/{run_id}/classifications")
async def list_classifications(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(integration_auth.get_ropa_reader),
) -> list[dict]:
    """The per-column classification trail: category, confidence, which rule
    matched, and why -- the answer to "why was this column classified this
    way", which the grouped /records view cannot provide on its own (migration
    0029)."""
    org_id = uuid.UUID(user.org_id)
    if await ropa_repository.get_run(db, run_id, org_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    rows = await ropa_repository.list_classifications_for_run(db, run_id, org_id)
    return [
        {
            "id": str(c.id), "source": c.source_name, "schema": c.schema_name,
            "table": c.table_name, "column": c.column_name,
            "classification": c.classification, "data_subject": c.data_subject,
            "confidence": float(c.confidence), "evidence": c.evidence,
            "review_required": c.review_required, "review_reason": c.review_reason,
        }
        for c in rows
    ]


@router.get("/runs/{run_id}/changes")
async def list_changes(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(integration_auth.get_ropa_reader),
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
    user: CurrentUser = Depends(integration_auth.get_ropa_reader),
) -> dict:
    """Make this run's schema the new comparison baseline.

    Accepts either a human session or a service key holding `evidence:read` --
    same as the other read endpoints (core/integration_auth.get_ropa_reader).
    An adapter that pushed a run via /evidence can promote its own result
    without a console login, the same way it can already read it back.

    This is deliberately NOT gated behind a separate, stronger scope: the
    "a person saw this" check this endpoint used to enforce by requiring a
    console session is still made before any row changes -- it has just moved
    upstream, to whoever decided the integration key was allowed to promote at
    all (minting a key with `evidence:read` is itself a human, org-scoped
    decision, same as minting one with `evidence:write`).
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
