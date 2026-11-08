"""The Privacy Readiness Score of one scan (app/scoring/ten_checks.py), from the scan's stored records.

Computed on demand, nothing is stored: the score is always the current methodology applied to the evidence the scan
recorded. The console (`/api/v1/consent/scans/{id}/score`), the Consent Agent API (`/api/v1/consent-agent/scans/{id}/score`)
and the PDF reports all come through here, so they cannot show different numbers.
"""

import uuid
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConsentScan, ScanScreenshot
from app.db.repositories import scan_repository, stage_repository
from app.scanner.scan_profiles import MAX_WEB_PAGES
from app.scoring import sector, ten_checks


async def score_scan(db: AsyncSession, scan: ConsentScan) -> dict:
    """The ten checks and the score for `scan`, which the caller has already loaded for its organisation."""
    stages = await stage_repository.list_stages(db, scan.id, scan.org_id)
    # The first website_scan stage, as the console reads it (its stage list is in creation order).
    meta = next((s.stage_metadata for s in stages if s.stage == "website_scan"), None)
    analysed = [s for s in stages
                if s.stage == "policy_analysis" and s.status == "completed" and (s.stage_metadata or {}).get("result")]
    policy = max(analysed, key=lambda s: s.started_at or s.completed_at).stage_metadata["result"] if analysed else None
    page_url = (await db.execute(
        select(ScanScreenshot.page_url).where(ScanScreenshot.scan_id == scan.id)
    )).scalar_one_or_none()
    scan_id, scan_url = scan.id, scan.url
    when = scan.completed_at or scan.started_at or scan.created_at

    # Give this request's connection back before the evidence is loaded on others (see scan_repository._evidence_slot).
    await db.rollback()
    evidence = await scan_repository.get_scan_evidence_summary_concurrent(scan_id)

    result = ten_checks.assess(ten_checks.build_inputs(
        evidence=evidence, meta=meta, policy_analysis=policy, screenshot_page_url=page_url,
    ))
    # Facts about the scan the reports print beside the score. None of them changes it.
    result["context"] = {
        "scanned_at": when.isoformat() if when else None,
        "page_limit": MAX_WEB_PAGES,
        "sector": asdict(sector.assess_sector({**evidence, "policy_analysis": policy} if policy else evidence, scan_url)),
    }
    return result


async def score_scan_by_id(db: AsyncSession, scan_id: uuid.UUID, org_id: uuid.UUID) -> dict | None:
    scan = await scan_repository.get_scan(db, scan_id, org_id)
    return None if scan is None else await score_scan(db, scan)
