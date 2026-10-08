"""Application-level orchestration for Agent 2: run a discovery, persist the
result, and record human-review decisions.

Reuses Agent 1's infrastructure rather than duplicating it:
  * audit trail  -> db/repositories/audit_repository.record() (already generic
                    over entity_type/entity_id, so no change was needed there)
  * auth/tenancy -> callers pass the org_id from core/security.CurrentUser
  * persistence  -> db/repositories/ropa_repository
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.ropa.connectors.base import ConnectorError
from app.agents.ropa.connectors.factory import build_connector
from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.output import RopaAgentOutput
from app.agents.ropa.services import change_detection_service, discovery_service
from app.core.exceptions import DiscoveryAlreadyInProgressError
from app.db.models import RopaDiscoveryRun, RopaFinding, RopaRecordRow
from app.db.repositories import audit_repository, ropa_repository
from app.jobs import queue
from app.services import agent_webhook_service

_DECISIONS = ("approved", "rejected", "edited")


async def run_discovery_for_source(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    user_id: uuid.UUID | None,
    idempotency_key: str | None = None,
    callback_url: str | None = None,
    callback_secret_ciphertext: str | None = None,
) -> RopaDiscoveryRun:
    """VALIDATE -> QUEUE for one configured source.

    Does NOT connect to the source or run discovery itself -- it creates the
    run row (status="pending") and enqueues a `ropa_discovery` job on the
    existing agent_jobs queue, then returns immediately. The actual
    CONNECT -> READ -> TRANSFORM -> PERSIST work happens in
    `execute_queued_discovery`, which the worker runs (app/jobs/worker.py).

    This used to do that work inline, which held the HTTP request open for
    however long a customer's database took to introspect -- a large schema
    pinned a request thread (and a DB connection) for the whole scan. Moving
    it to the worker is not a new mechanism: the dispatch branch for
    'ropa_discovery' and execute_queued_discovery itself already existed and
    were already correct (including passing the schema baseline for change
    detection, which this inline path never did -- a second, smaller bug this
    consolidation also fixes); nothing enqueued a job for either to run.
    """
    if idempotency_key:
        existing = await ropa_repository.find_run_by_idempotency_key(db, org_id, idempotency_key)
        if existing is not None:
            return existing

    source = await ropa_repository.get_data_source(db, source_id, org_id)
    if source is None:
        raise ValueError(f"data source {source_id} not found for this organization")
    if not source.enabled:
        raise ValueError(f"data source {source.name!r} is disabled")

    # One run at a time per source. `idempotency_key` above dedupes a caller that
    # supplies one; this covers the caller that does not, which is the common case --
    # a double-click, or a retry after a slow response. Without it, each attempt
    # opened another connection to the customer's production database and read their
    # whole schema again, and Consiva is a guest on that system. "pending" (this
    # function's own output, before the worker ever picks it up) is one of the two
    # statuses this checks for, so a second request queued while the first is still
    # sitting in the queue is caught just as reliably as one already mid-discovery.
    in_flight = await ropa_repository.find_active_run_for_source(db, org_id, source.id)
    if in_flight is not None:
        raise DiscoveryAlreadyInProgressError(
            f"discovery is already running for {source.name!r} (run {in_flight.id}); "
            "wait for it to finish, or read its result"
        )

    run = await ropa_repository.create_run(
        db, org_id=org_id, source_name=source.name, data_source_id=source.id,
        ingest_mode="connector", requested_by_user_id=user_id, idempotency_key=idempotency_key,
        callback_url=callback_url, callback_secret_ciphertext=callback_secret_ciphertext,
    )
    await audit_repository.record(
        db, org_id=org_id, actor_user_id=user_id, action="ropa_discovery.requested",
        entity_type="ropa_discovery_run", entity_id=run.id,
        after={"source": source.name, "connector": source.connector},
    )
    await queue.enqueue(
        db, org_id=org_id, job_type="ropa_discovery",
        payload={"run_id": str(run.id), "org_id": str(org_id)},
    )
    return run


async def ingest_pushed_evidence(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    source_name: str,
    evidence: DiscoveryEvidence,
    user_id: uuid.UUID | None,
    idempotency_key: str | None = None,
    integration_key_name: str | None = None,
    correlation_id: str | None = None,
    schema_version: str | None = None,
    callback_url: str | None = None,
    callback_secret_ciphertext: str | None = None,
) -> tuple[RopaDiscoveryRun, bool]:
    """Accept evidence collected by an EXTERNAL integration, then VALIDATE -> QUEUE it.

    This is the safer integration boundary for a third party like PrepMyEvent:
    their adapter reads their own database and posts structured evidence, so
    Consiva never holds their production credentials at all. The evidence has
    already been validated against the DiscoveryEvidence schema by FastAPI before
    reaching here.

    Does NOT run the analysis pipeline itself -- it creates the run row
    (status="pending") and enqueues a `ropa_evidence_analysis` job on the same
    agent_jobs queue connector-based discovery uses, then returns immediately.
    The actual CLASSIFY -> ENRICH -> ANALYZE -> PERSIST work happens in
    execute_queued_evidence_analysis, which the worker runs.

    This used to run inline, on the premise (run_pipeline's own docstring: "no
    I/O, no network, no database") that evidence-push had nothing worth
    queuing for. Wiring LLM enrichment into this path (a real network call
    that can hang or retry for minutes against a slow/unavailable model, per
    an observed incident) broke that premise without anyone moving this off
    the request path to match -- exactly the bug run_discovery_for_source's
    own docstring describes fixing for the connector flow, now the same fix
    for the same reason here.

    Returns (run, is_new): `is_new` is False for an idempotency-key replay that
    short-circuited to an already-queued/completed run -- the caller uses this
    to decide whether this POST should enqueue anything at all, so a retried
    POST doesn't re-run (or re-announce) a push that already started once.
    """
    if idempotency_key:
        existing = await ropa_repository.find_run_by_idempotency_key(db, org_id, idempotency_key)
        if existing is not None:
            return existing, False

    run = await ropa_repository.create_run(
        db, org_id=org_id, source_name=source_name, ingest_mode="evidence_push",
        requested_by_user_id=user_id, idempotency_key=idempotency_key,
        callback_url=callback_url, callback_secret_ciphertext=callback_secret_ciphertext,
    )
    await audit_repository.record(
        db, org_id=org_id, actor_user_id=user_id, action="ropa_evidence.ingested",
        entity_type="ropa_discovery_run", entity_id=run.id,
        after={
            "source": source_name,
            "tables": len(evidence.tables),
            "columns": len(evidence.columns),
            # Attributes a machine-authenticated run to its key, since there is
            # no user to attribute it to.
            "integration_key": integration_key_name,
            # Threads the sender's id into the audit trail so one push can be
            # traced across the adapter's logs, this API and the run.
            "correlation_id": correlation_id,
            "schema_version": schema_version,
        },
    )
    await queue.enqueue(
        db, org_id=org_id, job_type="ropa_evidence_analysis",
        payload={
            "run_id": str(run.id), "org_id": str(org_id),
            # The evidence travels in the job payload, not re-read from anywhere
            # else: unlike a connector run, there is no live source to re-query
            # later -- this IS the data, already validated and size-capped by
            # EvidenceIngest before this function was ever called.
            "evidence": evidence.model_dump(mode="json"),
        },
    )
    return run, True


async def execute_queued_evidence_analysis(run_id: uuid.UUID, org_id: uuid.UUID, evidence_payload: dict) -> None:
    """Worker entry point for a queued evidence-push analysis (job_type
    'ropa_evidence_analysis').

    Opens its own session because the worker has no request scope -- same
    pattern as execute_queued_discovery. Any failure here (including one the
    LLM enrichment call itself didn't swallow -- enrich_elements already falls
    back to rules-only on its own errors, so this is for analysis/persistence
    failures beyond that) marks the run failed and notifies the partner rather
    than leaving it stuck at "analyzing" forever, the same regression
    execute_queued_discovery's own failure branch guards against for the
    connector path.
    """
    from app.db.session import async_session_factory

    evidence = DiscoveryEvidence.model_validate(evidence_payload)

    async with async_session_factory() as db:
        run = await ropa_repository.get_run(db, run_id, org_id)
        if run is None:
            raise ValueError(f"ROPA run {run_id} not found for org {org_id}")

        try:
            run.status = "analyzing"
            await db.flush()
            baseline = await ropa_repository.get_current_baseline_snapshot(db, org_id, run.source_name)
            output = await discovery_service.run_pipeline_enriched(
                evidence, baseline_snapshot=baseline, run_id=str(run.id)
            )
        except Exception as exc:  # noqa: BLE001 -- any analysis failure must fail the run, not strand it
            await ropa_repository.fail_run(db, run, f"{type(exc).__name__}: {exc}")
            await audit_repository.record(
                db, org_id=org_id, actor_user_id=None, action="ropa_evidence.failed",
                entity_type="ropa_discovery_run", entity_id=run.id, after={"error": str(exc)},
            )
            await db.commit()
            await agent_webhook_service.send_event(
                "ropa.run.failed", run_id=str(run.id), error=f"{type(exc).__name__}: {exc}"[:500],
                callback_url=run.agent_callback_url,
                callback_secret=agent_webhook_service.resolve_callback_secret(
                    run.agent_callback_secret_ciphertext
                ),
            )
            raise

        await _persist_and_audit(db, run, output, org_id=org_id, user_id=None, evidence=evidence)
        await ropa_repository.persist_changes(db, run, output.change_detection, source_name=run.source_name)
        await db.commit()
        await agent_webhook_service.send_event(
            "ropa.run.completed", run_id=str(run.id),
            callback_url=run.agent_callback_url,
            callback_secret=agent_webhook_service.resolve_callback_secret(run.agent_callback_secret_ciphertext),
        )


async def execute_queued_discovery(run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Worker entry point for a queued connector discovery (job_type
    'ropa_discovery').

    Opens its own session because the worker has no request scope -- same
    pattern as analysis_service.run_analysis.
    """
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        run = await ropa_repository.get_run(db, run_id, org_id)
        if run is None:
            raise ValueError(f"ROPA run {run_id} not found for org {org_id}")
        if run.data_source_id is None:
            raise ValueError(f"ROPA run {run_id} has no data source to connect to")

        source = await ropa_repository.get_data_source(db, run.data_source_id, org_id)
        if source is None:
            raise ValueError(f"data source {run.data_source_id} not found")

        try:
            connector = build_connector(
                connector=source.connector, config=source.config, credential_ref=source.credential_ref,
                credential_ciphertext=source.credential_ciphertext,
            )
            run.status = "discovering"
            await db.flush()
            baseline = await ropa_repository.get_current_baseline_snapshot(db, org_id, source.name)
            output = await discovery_service.discover_and_analyze(
                connector, org_id=str(org_id), source_name=source.name, baseline_snapshot=baseline,
                run_id=str(run.id), enrich=True,
            )
        except (ConnectorError, ValueError) as exc:
            await ropa_repository.fail_run(db, run, f"{type(exc).__name__}: {exc}")
            await audit_repository.record(
                db, org_id=org_id, actor_user_id=None, action="ropa_discovery.failed",
                entity_type="ropa_discovery_run", entity_id=run.id, after={"error": str(exc)},
            )
            await db.commit()
            await agent_webhook_service.send_event(
                "ropa.run.failed", run_id=str(run.id), error=f"{type(exc).__name__}: {exc}"[:500],
                callback_url=run.agent_callback_url,
                callback_secret=agent_webhook_service.resolve_callback_secret(
                    run.agent_callback_secret_ciphertext
                ),
            )
            raise

        await ropa_repository.mark_source_verified(db, source)
        await _persist_and_audit(db, run, output, org_id=org_id, user_id=None)
        await ropa_repository.persist_changes(db, run, output.change_detection, source_name=source.name)
        await db.commit()
        await agent_webhook_service.send_event(
            "ropa.run.completed", run_id=str(run.id),
            callback_url=run.agent_callback_url,
            callback_secret=agent_webhook_service.resolve_callback_secret(run.agent_callback_secret_ciphertext),
        )


async def _persist_and_audit(
    db: AsyncSession,
    run: RopaDiscoveryRun,
    output: RopaAgentOutput,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    evidence: DiscoveryEvidence | None = None,
) -> None:
    snapshot = (
        change_detection_service.build_snapshot(evidence, output.classifications)
        if evidence is not None
        else None
    )
    records, findings = await ropa_repository.persist_output(
        db, run, output, schema_snapshot=snapshot
    )
    await audit_repository.record(
        db, org_id=org_id, actor_user_id=user_id, action="ropa_discovery.completed",
        entity_type="ropa_discovery_run", entity_id=run.id,
        after={
            "ropa_records": len(records),
            "findings": len(findings),
            "personal_data_elements": run.personal_data_elements,
        },
    )


async def decide_record(
    db: AsyncSession,
    *,
    record_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    decision: str,
    reason: str | None = None,
    edited_payload: dict | None = None,
) -> RopaRecordRow:
    """Approve / reject / edit a ROPA record -- the same three actions Agent 1's
    review_service exposes for consent findings, now available for ROPA."""
    if decision not in _DECISIONS:
        raise ValueError(f"decision must be one of {_DECISIONS}")
    if decision == "edited" and not edited_payload:
        raise ValueError("an edit decision requires edited_payload")

    record = await ropa_repository.get_record(db, record_id, org_id)
    if record is None:
        raise ValueError(f"ROPA record {record_id} not found for this organization")
    if record.status == "superseded":
        raise ValueError("cannot decide on a superseded record; act on the current version")

    before = {"status": record.status, "review_required": record.review_required}
    record.status = "approved" if decision in ("approved", "edited") else "rejected"
    record.review_required = False
    record.decided_by_user_id = user_id
    record.decided_at = datetime.now(UTC)
    record.decision_reason = reason
    if edited_payload:
        record.edited_payload = edited_payload
    record.updated_at = datetime.now(UTC)
    await db.flush()

    await audit_repository.record(
        db, org_id=org_id, actor_user_id=user_id, action=f"ropa_record.{decision}",
        entity_type="ropa_record", entity_id=record.id,
        before=before, after={"status": record.status, "reason": reason},
    )
    return record


async def decide_finding(
    db: AsyncSession,
    *,
    finding_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    decision: str,
    reason: str | None = None,
    edited_payload: dict | None = None,
) -> RopaFinding:
    if decision not in _DECISIONS:
        raise ValueError(f"decision must be one of {_DECISIONS}")
    if decision == "edited" and not edited_payload:
        raise ValueError("an edit decision requires edited_payload")

    finding = await ropa_repository.get_finding(db, finding_id, org_id)
    if finding is None:
        raise ValueError(f"ROPA finding {finding_id} not found for this organization")

    before = {"review_status": finding.review_status}
    finding.review_status = decision
    finding.decided_by_user_id = user_id
    finding.decided_at = datetime.now(UTC)
    finding.decision_reason = reason
    if edited_payload:
        finding.edited_payload = edited_payload
    await db.flush()

    await audit_repository.record(
        db, org_id=org_id, actor_user_id=user_id, action=f"ropa_finding.{decision}",
        entity_type="ropa_finding", entity_id=finding.id,
        before=before, after={"review_status": decision, "reason": reason},
    )
    return finding
