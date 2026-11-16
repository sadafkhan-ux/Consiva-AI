"""The background job for an Enterprise scan. Registered with the worker under the job type `enterprise_scan` (jobs/worker.py)."""

from __future__ import annotations

import logging
import uuid

from app.enterprise import pipeline
from app.enterprise.crawl import runner

logger = logging.getLogger(__name__)
JOB_TYPE = "enterprise_scan"


async def run_enterprise_scan(payload: dict) -> None:
    """Crawl one scan, then analyse it. Calling it again for the same scan resumes it, so a job the worker re-runs after a crash is safe: the crawl carries on from its frontier, and the
    analysis continues by chunk. A scan that was cancelled or whose crawl failed is not analysed."""
    scan_id, org_id = uuid.UUID(payload["scan_id"]), uuid.UUID(payload["org_id"])
    status = await runner.run(scan_id, org_id)
    if status == "completed":
        await pipeline.run_analysis_phase(scan_id, org_id)
    else:
        logger.info("enterprise scan %s ended as %s: no analysis", scan_id, status)
