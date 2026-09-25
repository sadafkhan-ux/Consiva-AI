import logging
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import InvalidUrlError, RateLimitExceededError, ScanAuthorizationError
from app.db.models import ConsentScan
from app.db.repositories import scan_repository
from app.db.session import async_session_factory
from app.jobs import queue
from app.lookup import repository as lookup_repository
from app.observability.stage_tracker import track_stage
from app.rules.consent_rules import classify_scan
from app.scanner.crawler import run_scan_isolated
from app.scanner.url_safety import assert_safe_url
from app.services import audit_service

logger = logging.getLogger(__name__)


async def request_scan(
    db: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, url: str, authorized: bool,
    enqueue_job: bool = True,
) -> ConsentScan:
    """Creates the scan record and enqueues the crawl job. `authorized` is a required,
    explicit self-attestation from the caller that they own/are permitted to scan this
    domain — there's no domain-verification flow yet (docs/architecture §P), so this is
    the minimal stopgap rather than silently scanning arbitrary third-party sites."""
    if not authorized:
        raise ScanAuthorizationError(
            "Scanning requires an explicit authorization attestation for this domain."
        )

    domain = urlparse(url).netloc
    if not domain:
        raise InvalidUrlError(f"Could not parse a domain from url: {url}")

    settings = get_settings()
    recent_count = await scan_repository.count_scans_since(
        db, org_id=org_id, since=datetime.now(UTC) - timedelta(days=1)
    )
    if recent_count >= settings.scanner_max_scans_per_org_per_day:
        raise RateLimitExceededError(
            f"Scan rate limit reached ({settings.scanner_max_scans_per_org_per_day}/day for this org)."
        )

    website = await scan_repository.get_or_create_website(db, org_id=org_id, domain=domain)

    # Master reference §12 Critical: the self-attestation checkbox alone is an
    # unverified client-supplied boolean. When this flag is on, the domain must ALSO
    # have passed DNS-TXT ownership verification (websites.verified_at, see
    # services/verification_service.py). Off by default so the development demo flow
    # keeps working; flip on before any production pilot.
    if settings.scanner_require_domain_verification and website.verified_at is None:
        raise ScanAuthorizationError(
            f"Domain {domain} has not passed ownership verification. Get your TXT record from "
            "GET /api/v1/consent/websites/verification-token, publish it in the domain's DNS, "
            "then POST /api/v1/consent/websites/verify."
        )

    scan = await scan_repository.create_scan(
        db, org_id=org_id, website_id=website.id, url=url, authorized_by_user_id=user_id
    )
    await audit_service.record(
        db, org_id=org_id, actor_user_id=user_id, action="scan.created",
        entity_type="consent_scan", entity_id=scan.id,
    )
    await db.commit()  # scan.id must be durable before the tracked stage below references it

    try:
        async with track_stage(scan.id, "url_validation") as meta:
            await assert_safe_url(url)  # SSRF guard — rejects private/loopback/link-local/metadata addresses
            meta["domain"] = domain
    except Exception as exc:
        async with async_session_factory() as validation_db:
            await scan_repository.mark_scan_failed(validation_db, scan.id, str(exc))
            await validation_db.commit()
        raise

    # `enqueue_job=False` is for a caller that will queue its own job covering this
    # scan -- specifically the integration API, whose `consent_api_chain` job runs the
    # crawl AND the analysis as one unit.
    #
    # Without this the scan ran TWICE: request_scan queued a "scan" job and the chain
    # job crawled again, which was not a theoretical risk -- it was measured on the
    # first end-to-end call, which produced 2 crawls, 17 stages and 6 findings for a
    # 3-finding site. Everything downstream was correct; it was just done twice, at
    # double the browser time and double the LLM spend.
    if enqueue_job:
        async with async_session_factory() as queue_db:
            await queue.enqueue(
                queue_db, org_id=org_id, job_type="scan",
                payload={"scan_id": str(scan.id), "url": url},
            )
            await queue_db.commit()

    return scan


async def execute_scan_and_persist(scan_id: uuid.UUID, url: str) -> None:
    """Called by jobs/worker.py. Owns its own DB sessions rather than one long-lived
    transaction, since the crawl itself (Playwright, network-bound) can take a while."""
    async with async_session_factory() as db:
        await scan_repository.mark_scan_running(db, scan_id)
        await db.commit()

    try:
        async with async_session_factory() as db:
            lookup_entries = await lookup_repository.load_all(db)

        async with track_stage(scan_id, "website_scan") as meta:
            scan_result = await run_scan_isolated(url)
            meta["pages_found"] = len(scan_result.pages)
            meta["cookies_found"] = len(scan_result.cookies)
            meta["trackers_found"] = len(scan_result.trackers)
            meta["forms_found"] = len(scan_result.forms)
            meta["consent_mechanism"] = scan_result.consent_signals.mechanism_type
            meta["cmp_vendor"] = scan_result.consent_signals.cmp_vendor
            meta["cmp_confidence"] = scan_result.consent_signals.evidence.get("confidence")
            meta["cmp_detection_source"] = scan_result.consent_signals.evidence.get("detection_source")
            meta["accept_interaction"] = scan_result.consent_signals.evidence.get("accept_interaction")
            meta["reject_interaction"] = scan_result.consent_signals.evidence.get("reject_interaction")
            # Real, observed retry/scroll/failure diagnostics from this specific scan
            # run (scanner-hardening audit) -- never fabricated, straight from crawler.py.
            meta.update(scan_result.scan_diagnostics)
            # Per-consent-state evidence breakdown -- cheap in-memory aggregation over
            # data already in scan_result (each record's own consent_states list), no
            # extra DB query needed. Lets a reviewer see "3 cookies pre_consent, 8 after
            # accept, 2 still after reject" without cross-referencing raw evidence rows.
            for kind, records in (("cookies", scan_result.cookies), ("trackers", scan_result.trackers)):
                by_state: dict[str, int] = {}
                for record in records:
                    for state in record.consent_states:
                        by_state[state] = by_state.get(state, 0) + 1
                meta[f"{kind}_by_consent_state"] = by_state

        async with track_stage(scan_id, "classification") as meta:
            classification = classify_scan(scan_result, lookup_entries)
            sources = [c.source for c in classification.cookies if c.source] + [
                t.source for t in classification.trackers if t.source
            ]
            meta["classified_items"] = len(sources)
            meta["by_source"] = {s: sources.count(s) for s in set(sources)}
            # The third measurement point for the tracker-count discrepancy (see
            # data_structuring's persistence_gap). website_scan records what the
            # scanner returned and data_structuring records what the table holds;
            # without this middle number a gap cannot be attributed to either
            # classification or the write. classify_scan is 1:1 by construction and the
            # ORM write was measured lossless at this volume, so the two should be
            # identical -- and on a real hubspot scan they are not, which is precisely
            # why the number needs to be recorded rather than assumed.
            meta["trackers_in"] = len(scan_result.trackers)
            meta["trackers_out"] = len(classification.trackers)
            meta["cookies_out"] = len(classification.cookies)
            if len(classification.trackers) != len(scan_result.trackers):
                meta["classification_gap"] = len(scan_result.trackers) - len(classification.trackers)
    except Exception as exc:
        logger.exception("Scan failed for %s", url)
        async with async_session_factory() as db:
            await scan_repository.mark_scan_failed(db, scan_id, str(exc))
            await db.commit()
        raise

    try:
        async with track_stage(scan_id, "data_structuring") as meta:
            async with async_session_factory() as db:
                written = await scan_repository.save_scan_result(
                    db, scan_id=scan_id, scan_result=scan_result, classification=classification
                )
                await db.commit()
            meta["policies_found"] = len(scan_result.policies)
            meta["third_party_services_found"] = len(classification.third_party_services)

            # What the scanner found vs what the database now holds, per table.
            #
            # An external validation raised the same discrepancy twice -- the
            # website_scan stage reporting 881 trackers while only 828 rows were
            # readable afterwards, and 895 vs 840 the run before -- with nothing in the
            # API able to explain where the rest went. Tracing a live crawl end to end
            # found the path lossless, there is exactly one writer, no deleter and no
            # unique constraint, so the cause is still unaccounted for. Recording both
            # numbers here means the next occurrence identifies itself and names the
            # table, instead of needing the same investigation from scratch.
            found = {
                "pages": len(scan_result.pages), "trackers": len(classification.trackers),
                "cookies": len(classification.cookies), "forms": len(scan_result.forms),
                "policies": len(scan_result.policies),
                "third_party_services": len(classification.third_party_services),
            }
            meta["rows_written"] = written
            gaps = {k: found[k] - written.get(k, 0) for k in found if found[k] != written.get(k, 0)}
            if gaps:
                meta["persistence_gap"] = gaps
                logger.warning(
                    "Scan %s: evidence found and evidence persisted disagree: %s", scan_id, gaps
                )
    except Exception as exc:
        logger.exception("Failed to persist scan result for %s", url)
        async with async_session_factory() as db:
            await scan_repository.mark_scan_failed(db, scan_id, str(exc))
            await db.commit()
        raise
