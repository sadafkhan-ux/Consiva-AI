"""The ROPA pipeline orchestrator: connector -> evidence -> analysis -> output.

This is the single entry point the rest of the system should call. It knows the
ORDER of the stages; it does not know how any individual stage works, and it
never imports a specific connector -- sources are resolved through
connectors/base.py's registry, so adding a source never changes this file.

    Source -> Evidence -> Personal Data Detection -> Classification + Confidence
    -> Data Subject -> Purpose -> Processing Activity -> Data Flow -> Risk/Gap
    -> ROPA Record -> Human Review
"""

from __future__ import annotations

import logging

from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.output import RopaAgentOutput
from app.agents.ropa.services import (
    change_detection_service,
    classification_service,
    dataflow_service,
    processing_activity_service,
    purpose_service,
    risk_service,
    ropa_service,
)

logger = logging.getLogger(__name__)


def run_pipeline(
    evidence: DiscoveryEvidence,
    *,
    review_threshold: float = classification_service.DEFAULT_REVIEW_THRESHOLD,
    baseline_snapshot: dict | None = None,
) -> RopaAgentOutput:
    """Run every analysis stage over already-collected evidence.

    Pure and synchronous on purpose: no I/O, no network, no database. That makes
    the whole analysis chain unit-testable without a live source, and keeps
    connector failures clearly separated from analysis failures.

    `baseline_snapshot` is the previously promoted schema fingerprint for this
    source (change_detection_service.build_snapshot). When absent, change
    detection is skipped rather than reporting a first run as "all new".
    """
    elements = classification_service.classify_evidence(evidence, review_threshold=review_threshold)
    purposes, subjects = purpose_service.map_purposes_and_subjects(evidence)
    activities = processing_activity_service.build_activities(evidence, elements)
    data_flows = dataflow_service.build_data_flows(evidence, activities)
    retention = risk_service.build_retention_findings(evidence)
    access = risk_service.build_access_findings(evidence)
    findings = risk_service.detect_gaps(evidence, elements, activities, retention, access)
    records = ropa_service.build_ropa_records(evidence, activities, elements, data_flows, retention, access)
    review_items = ropa_service.collect_review_items(
        elements, purposes, subjects, activities, findings, records
    )
    changes = change_detection_service.detect_changes(
        baseline_snapshot or {}, change_detection_service.build_snapshot(evidence, elements)
    )

    logger.info(
        "ROPA pipeline complete run=%s tables=%d columns=%d personal_data=%d "
        "activities=%d ropa_records=%d review_items=%d changes=%d",
        evidence.discovery_run_id,
        len(evidence.tables),
        len(evidence.columns),
        sum(1 for e in elements if e.classification != "Unknown"),
        len(activities),
        len(records),
        len(review_items),
        len(changes),
    )

    return ropa_service.build_output(
        evidence, elements, purposes, subjects, activities,
        data_flows, retention, access, findings, records, review_items,
        changes=changes,
    )


async def discover_and_analyze(
    connector,
    *,
    org_id: str,
    source_name: str,
    review_threshold: float = classification_service.DEFAULT_REVIEW_THRESHOLD,
    baseline_snapshot: dict | None = None,
) -> RopaAgentOutput:
    """Collect evidence from any registered connector, then analyze it.

    `connector` is anything satisfying connectors/base.py's SourceConnector
    protocol -- Postgres today, a REST source tomorrow, with no change here.
    """
    evidence = await connector.discover(org_id=org_id, source_name=source_name)
    logger.info(
        "ROPA discovery complete connector=%s source=%s tables=%d columns=%d",
        getattr(connector, "connector_name", type(connector).__name__),
        source_name,
        len(evidence.tables),
        len(evidence.columns),
    )
    return run_pipeline(
        evidence, review_threshold=review_threshold, baseline_snapshot=baseline_snapshot
    )
