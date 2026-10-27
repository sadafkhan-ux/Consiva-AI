import asyncio
import base64
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentRun,
    AgentRunStage,
    ConsentForm,
    ConsentScan,
    ConsentSignal,
    Cookie,
    Policy,
    ScanScreenshot,
    ThirdPartyService,
    Tracker,
    Website,
    WebsitePage,
)
from app.rules.consent_observation import observed_signal
from app.rules.consent_rules import RulesClassification
from app.rules.cookie_purposes import describe_cookie, lifetime_days
from app.scanner.schemas import ScanResult, ScreenshotRecord
from app.scanner.url_normalize import selection_reason

logger = logging.getLogger(__name__)


async def count_scans_since(db: AsyncSession, *, org_id: uuid.UUID, since: datetime) -> int:
    result = await db.execute(
        select(func.count()).select_from(ConsentScan).where(ConsentScan.org_id == org_id, ConsentScan.created_at >= since)
    )
    return result.scalar_one()


async def count_queued_scans(db: AsyncSession, *, org_id: uuid.UUID) -> int:
    """Scans of one organisation still waiting for a worker."""
    result = await db.execute(select(func.count()).select_from(ConsentScan).where(ConsentScan.org_id == org_id, ConsentScan.status == "queued"))
    return result.scalar_one()


async def get_website_by_id(db: AsyncSession, website_id: uuid.UUID) -> Website | None:
    """Trusted-internal lookup (no org_id filter) -- callers already have the org_id
    from the row that referenced this website_id (e.g. a ScanSchedule), so this isn't
    a user-facing tenant-isolation boundary the way get_scan()/get_finding() are."""
    return await db.get(Website, website_id)


async def get_or_create_website(db: AsyncSession, *, org_id: uuid.UUID, domain: str) -> Website:
    result = await db.execute(select(Website).where(Website.org_id == org_id, Website.domain == domain))
    website = result.scalar_one_or_none()
    if website is None:
        website = Website(org_id=org_id, domain=domain)
        db.add(website)
        await db.flush()
    return website


async def create_scan(
    db: AsyncSession, *, org_id: uuid.UUID, website_id: uuid.UUID, url: str, authorized_by_user_id: uuid.UUID
) -> ConsentScan:
    scan = ConsentScan(
        org_id=org_id,
        website_id=website_id,
        url=url,
        status="pending",
        authorized_by_user_id=authorized_by_user_id,
    )
    db.add(scan)
    await db.flush()
    return scan


async def get_scan(db: AsyncSession, scan_id: uuid.UUID, org_id: uuid.UUID) -> ConsentScan | None:
    """Scoped to `org_id` deliberately: this backend connects to Postgres directly
    (not through Supabase's PostgREST layer), so `auth.jwt()` is never populated and
    the RLS policies in migrations/0001_init.sql do NOT apply to these queries — tenant
    isolation for this service has to be enforced here, in the query itself, not
    assumed from the database. See docs/architecture §P."""
    result = await db.execute(select(ConsentScan).where(ConsentScan.id == scan_id, ConsentScan.org_id == org_id))
    return result.scalar_one_or_none()


async def list_recent_scans(db: AsyncSession, org_id: uuid.UUID, limit: int = 20) -> list[ConsentScan]:
    """The organisation's most recent scans, newest first. Scoped to `org_id` in the query for the same reason as get_scan."""
    result = await db.execute(
        select(ConsentScan).where(ConsentScan.org_id == org_id).order_by(ConsentScan.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def latest_scan_for_website(db: AsyncSession, *, org_id: uuid.UUID, website_id: uuid.UUID) -> ConsentScan | None:
    """The most recent scan of one website in one organisation, whatever its status."""
    result = await db.execute(
        select(ConsentScan).where(ConsentScan.org_id == org_id, ConsentScan.website_id == website_id).order_by(ConsentScan.created_at.desc()).limit(1)
    )
    return result.scalars().first()


async def previous_completed_scan(db: AsyncSession, scan: ConsentScan) -> ConsentScan | None:
    """The completed scan of the same website, in the same organisation, that came just before `scan`. None when there is none."""
    result = await db.execute(
        select(ConsentScan)
        .where(ConsentScan.org_id == scan.org_id, ConsentScan.website_id == scan.website_id, ConsentScan.status == "completed",
               ConsentScan.created_at < scan.created_at, ConsentScan.id != scan.id)
        .order_by(ConsentScan.created_at.desc()).limit(1)
    )
    return result.scalars().first()


async def mark_scan_running(db: AsyncSession, scan_id: uuid.UUID) -> None:
    scan = await db.get(ConsentScan, scan_id)
    if scan:
        scan.status = "running"
        scan.started_at = datetime.now(UTC)
        # A retry of a job whose previous attempt failed lands here again. Leaving that attempt's
        # error and completion time on the row made a scan that was running look failed.
        scan.error = None
        scan.completed_at = None
        await db.flush()


async def mark_scan_failed(db: AsyncSession, scan_id: uuid.UUID, error: str) -> None:
    scan = await db.get(ConsentScan, scan_id)
    if scan:
        scan.status = "failed"
        scan.error = error
        scan.completed_at = datetime.now(UTC)
        await db.flush()


def _parse_expiry(expiry_iso: str | None) -> datetime | None:
    """CookieRecord.expiry (scanner/schemas.py) is a plain ISO-8601 string --
    cookie_detector.py builds it via datetime.isoformat() from Playwright's raw
    Unix-timestamp `expires` field, deliberately kept as a string on the scanner-side
    Pydantic model so that module has no reason to import SQLAlchemy's DB types.
    Cookie.expiry (db/models.py) is a real `DateTime(timezone=True)` column, and
    asyncpg's direct parameter binding does NOT coerce a str into a timestamp itself
    (confirmed live: `asyncpg.exceptions.DataError: ... expected a datetime.date or
    datetime.datetime instance, got 'str'` -- this path was never exercised earlier in
    testing because the sites used before had zero cookies). Parse back here, at the
    one place a scanner-side string crosses into a DB-side datetime column."""
    return datetime.fromisoformat(expiry_iso) if expiry_iso else None


async def save_scan_result(
    db: AsyncSession, *, scan_id: uuid.UUID, scan_result: ScanResult, classification: RulesClassification
) -> dict[str, int]:
    """Persists every evidence table for one scan, resolving the scanner's local_id
    cross-references (e.g. cookie -> tracker) into real foreign keys as it goes.

    Page/tracker ids are generated client-side (uuid.uuid4()) rather than left to the
    DB's server_default, so page_id_by_local/tracker_id_by_local can be built
    immediately at object-construction time. Rows are still batched per table (one
    `add_all` + one `flush` for all pages, then all trackers) instead of one flush per
    row -- but the two flushes themselves are NOT optional. None of these models have
    an ORM `relationship()` declared between them (confirmed: grepped db/models.py),
    only plain FK *columns* -- so SQLAlchemy's unit-of-work has no dependency graph to
    topologically sort by and provides no ordering guarantee across unrelated mapped
    classes in one flush. An earlier version of this function assumed otherwise and
    shipped a real bug (ConsentForm rows occasionally flushed before the WebsitePage
    row they reference, raising a live ForeignKeyViolationError) -- caught by testing
    against a real 25-page site, not assumed safe. Pages/trackers are flushed
    explicitly before anything that references them is even constructed, closing that
    gap for good rather than hoping insert order cooperates. Still a large win over
    the original one-flush-per-row version: 25 pages + 67 trackers was 92 round trips
    before, 2 now."""
    page_id_by_local: dict[str, uuid.UUID] = {}
    tracker_id_by_local: dict[str, uuid.UUID] = {}

    page_rows = []
    for page in scan_result.pages:
        row = WebsitePage(
            id=uuid.uuid4(), scan_id=scan_id, url=page.url, title=page.title,
            http_status=page.http_status, discovered_via=page.discovered_via,
        )
        page_id_by_local[page.local_id] = row.id
        page_rows.append(row)
    db.add_all(page_rows)
    if page_rows:
        await db.flush()

    tracker_rows = []
    for tracker in classification.trackers:
        row = Tracker(
            id=uuid.uuid4(), scan_id=scan_id,
            page_id=page_id_by_local.get(tracker.page_local_id) if tracker.page_local_id else None,
            script_src=tracker.script_src, vendor=tracker.vendor, category=tracker.category,
            source=tracker.source or "rule", consent_states=tracker.consent_states,
        )
        tracker_id_by_local[tracker.local_id] = row.id
        tracker_rows.append(row)
    db.add_all(tracker_rows)
    if tracker_rows:
        await db.flush()

    for cookie in classification.cookies:
        db.add(Cookie(
            scan_id=scan_id, name=cookie.name, domain=cookie.domain, path=cookie.path,
            expiry=_parse_expiry(cookie.expiry), is_first_party=cookie.is_first_party,
            category=cookie.category, vendor=cookie.vendor,
            source=cookie.source or "rule", consent_states=cookie.consent_states,
            secure=cookie.secure, http_only=cookie.http_only, same_site=cookie.same_site,
            observations=[o.model_dump() for o in cookie.observations],
            set_by_tracker_id=tracker_id_by_local.get(cookie.set_by_tracker_local_id)
            if cookie.set_by_tracker_local_id else None,
        ))

    for form in scan_result.forms:
        db.add(ConsentForm(
            scan_id=scan_id, page_id=page_id_by_local.get(form.page_local_id),
            selector=form.selector, fields=[f.model_dump() for f in form.fields],
            purpose_guess=form.purpose_guess, submit_url=form.action_url,
        ))

    for service in classification.third_party_services:
        db.add(ThirdPartyService(
            scan_id=scan_id, service_name=service.service_name, category=service.category,
            domains=service.domains, detection_method=service.detection_method,
        ))

    for policy in scan_result.policies:
        db.add(Policy(
            scan_id=scan_id, url=policy.url, policy_type=policy.policy_type,
            extracted_text_ref=policy.extracted_text_ref,
        ))

    signal = scan_result.consent_signals
    db.add(ConsentSignal(
        scan_id=scan_id, mechanism_type=signal.mechanism_type, cmp_vendor=signal.cmp_vendor,
        has_reject_all=signal.has_reject_all, has_granular_choices=signal.has_granular_choices,
        evidence=signal.evidence,
    ))

    await _save_screenshot(db, scan_id, scan_result.screenshot)

    scan = await db.get(ConsentScan, scan_id)
    if scan:
        scan.status = "completed"
        scan.completed_at = datetime.now(UTC)
        # The scanner always stamps this on its own ScanResult (crawler.py's
        # SCANNER_VERSION) -- it was simply never copied onto the consent_scans row,
        # so scan.scanner_version stayed NULL forever and every API/report response
        # displayed "-" for every scan, not just old ones.
        scan.scanner_version = scan_result.scanner_version

    await db.flush()

    # Returned, and recorded by the caller, because a silent gap between "what the
    # scanner found" and "what the database holds" is exactly the discrepancy an
    # external validation raised twice against this pipeline (881 trackers reported by
    # the website_scan stage vs 828 rows readable afterwards, on two independent runs).
    # No cause was reproducible from the code path -- it is lossless end to end when
    # traced on a live crawl, there is only one writer, no deleter, and no unique
    # constraint -- so the useful thing is to stop the next occurrence being a mystery:
    # both numbers now travel in the stage record, side by side, attributable per table.
    # Counted from the TABLE, not from len(the list we handed SQLAlchemy).
    #
    # The first version of this returned len(tracker_rows) and reported no gap at all
    # on a scan where the scanner found 906 trackers, 906 objects were constructed and
    # added -- and 857 rows existed afterwards. A metric that reports what we intended
    # to write cannot detect a write that did not happen, which is the entire failure
    # this was added to surface.
    await db.flush()
    written: dict[str, int] = {}
    for key, model in (
        ("pages", WebsitePage), ("trackers", Tracker), ("cookies", Cookie),
        ("forms", ConsentForm), ("policies", Policy),
        ("third_party_services", ThirdPartyService),
    ):
        written[key] = await db.scalar(
            select(func.count()).select_from(model).where(model.scan_id == scan_id)
        ) or 0
    return written


async def _save_screenshot(db: AsyncSession, scan_id: uuid.UUID, rec: ScreenshotRecord | None) -> None:
    """Keeps the homepage preview with the scan. Never fails the scan: a preview that cannot be stored is logged and the scan result is saved
    without it (the report then says no preview is available). It runs inside its own savepoint so a problem here cannot undo the evidence."""
    if rec is None:
        return
    try:
        image = None
        if rec.status == "ok":
            image = base64.b64decode(rec.image_b64 or "", validate=True)
            if not image or not rec.width or not rec.height:
                raise ValueError("an 'ok' preview without an image or its size")
        async with db.begin_nested():
            db.add(ScanScreenshot(
                scan_id=scan_id, status=rec.status, reason=rec.reason, mime=rec.mime if image else None, width=rec.width if image else None,
                height=rec.height if image else None, http_status=rec.http_status, page_url=rec.page_url, page_title=(rec.page_title or "")[:300] or None,
                image=image,
            ))
            await db.flush()
    except Exception as exc:   # noqa: BLE001 -- any failure here must leave the scan result intact
        logger.warning("Could not store the homepage preview for scan %s: %s", scan_id, exc)


async def get_screenshot(db: AsyncSession, scan_id: uuid.UUID) -> ScanScreenshot | None:
    """The homepage preview row of a scan, or None for a scan made before previews existed."""
    result = await db.execute(select(ScanScreenshot).where(ScanScreenshot.scan_id == scan_id))
    return result.scalar_one_or_none()


async def get_scan_counts(db: AsyncSession, scan_id: uuid.UUID) -> dict:
    """Cheap evidence counts for a scan status response — avoids pulling full content
    just to answer "how much did we find" (see get_scan_evidence_summary for that)."""
    counted = {
        "pages": WebsitePage, "forms": ConsentForm, "cookies": Cookie,
        "trackers": Tracker, "third_party_services": ThirdPartyService, "policies": Policy,
    }
    counts = {}
    for key, model in counted.items():
        result = await db.execute(select(func.count()).select_from(model).where(model.scan_id == scan_id))
        counts[key] = result.scalar_one()
    return counts


def _cookie_detail(c) -> dict:
    """The audit columns of a cookie, for the evidence API's cookie inventory only.
    Kept out of the agent's evidence dict on purpose: prompts.py's cookie compactor
    reads `expiry`, so adding these there would change what the model is shown."""
    first_seen = next((o.get("observed_at") for o in (c.observations or []) if isinstance(o, dict) and o.get("observed_at")), None)
    about = describe_cookie(c.name, c.category)
    return {
        "path": c.path, "expiry": c.expiry.isoformat() if c.expiry else None,
        "secure": c.secure, "http_only": c.http_only, "same_site": c.same_site,
        "observations": c.observations or [],
        # What the cookie is for, in the report's cookie inventory: from the provider's own description of a recognised cookie, never invented
        # (rules/cookie_purposes.py). `lifetime_days` is None for a session cookie, or when the time it was first seen was not recorded.
        "provider": about["provider"], "purpose": about["purpose"], "purpose_source": about["purpose_source"],
        "is_session": c.expiry is None, "lifetime_days": lifetime_days(c.expiry, first_seen),
    }


def _serialize_evidence(
    pages: list, forms: list, cookies: list, trackers: list, services: list, policies: list, signals: list,
    *, include_cookie_detail: bool = False,
) -> dict:
    page_url_by_id = {p.id: p.url for p in pages}
    return {
        # http_status and discovered_via say whether a page loaded and how it was found ("seed", "link", "sitemap"), or that the
        # attempt did not succeed ("failed", "timeout"): the scan report lists the pages from these.
        "pages": [
            {"id": str(p.id), "url": p.url, "title": p.title, "http_status": p.http_status, "discovered_via": p.discovered_via,
             # Why the page was chosen, worked out from its address and how it was found (url_normalize.selection_reason): a read of what is stored, never a stored claim.
             # Only in the API's detailed view, so the agent's own evidence is unchanged.
             **({"selection_reason": selection_reason(p.url, p.discovered_via)} if include_cookie_detail else {})}
            for p in pages
        ],
        "forms": [
            {"id": str(f.id), "selector": f.selector, "fields": f.fields, "purpose_guess": f.purpose_guess,
             # Where the form posts to, for the security signals of the report. Only in the API's detailed view, so the agent's own evidence is unchanged.
             # and the page it was on, so the five-page readiness review can tie a form to its page.
             **({"submit_url": f.submit_url, "page_url": page_url_by_id.get(f.page_id)} if include_cookie_detail else {})}
            for f in forms
        ],
        "cookies": [
            {"id": str(c.id), "name": c.name, "domain": c.domain, "category": c.category, "vendor": c.vendor,
             "is_first_party": c.is_first_party, "source": c.source, "consent_states": c.consent_states,
             **(_cookie_detail(c) if include_cookie_detail else {})}
            for c in cookies
        ],
        "trackers": [
            {"id": str(t.id), "script_src": t.script_src, "vendor": t.vendor, "category": t.category,
             "source": t.source, "consent_states": t.consent_states,
             # The page the request/script was seen on, so a finding can say WHERE. None when not recorded.
             "page_url": page_url_by_id.get(t.page_id)}
            for t in trackers
        ],
        "third_party_services": [
            {"id": str(s.id), "service_name": s.service_name, "category": s.category, "domains": s.domains}
            for s in services
        ],
        "policies": [{"id": str(p.id), "url": p.url, "policy_type": p.policy_type} for p in policies],
        "consent_signals": [
            observed_signal({"mechanism_type": s.mechanism_type, "cmp_vendor": s.cmp_vendor,
             "has_reject_all": s.has_reject_all, "has_granular_choices": s.has_granular_choices,
             # accept_interaction/reject_interaction/confidence/detection_source live
             # here -- previously persisted but never read back out, so
             # evaluate_consent_rules() (and therefore the LLM) had no way to know
             # whether Accept/Reject were ever actually automatable on this scan.
             "evidence": s.evidence})
            for s in signals
        ],
    }


_evidence_slots: dict[int, asyncio.Semaphore] = {}


def _evidence_slot() -> asyncio.Semaphore:
    """At most this many evidence queries run at once in this process, whoever asked: two connections fewer than the pool can hold, so loading evidence can never
    take the connection that a request, a job or the maintenance loop needs next. (An unbounded fan-out of seven queries per caller, together with each request
    already holding a connection of its own, exhausted a pool of five with five simultaneous callers and failed every one of them after 30 seconds.)"""
    from app.config import get_settings

    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _evidence_slots:
        s = get_settings()
        _evidence_slots[loop_id] = asyncio.Semaphore(max(1, s.db_pool_size + s.db_max_overflow - 2))
    return _evidence_slots[loop_id]


async def get_scan_evidence_summary_concurrent(scan_id: uuid.UUID, *, include_cookie_detail: bool = False) -> dict:
    """Same output as get_scan_evidence_summary(), but fetches all 7 evidence tables
    concurrently on their own short-lived sessions instead of sequentially on one --
    live-measured at ~2.5s sequential vs ~1 round-trip's worth concurrent for the
    agent's normalize node, which is on the hot path of every single analyze run.
    A plain AsyncSession can't run concurrent queries on itself (SQLAlchemy raises),
    so this needs its own session per table rather than reusing one — safe here since
    each query is a single fast read-only SELECT, not a long-held connection."""
    from app.db.session import async_session_factory

    models = [WebsitePage, ConsentForm, Cookie, Tracker, ThirdPartyService, Policy, ConsentSignal]

    async def _rows(model):
        # Bounded: seven of these run at once for every caller, and under many users the pool is the scarce thing. Without the bound, a few simultaneous callers took
        # every connection between them and the rest waited out the pool timeout (see _evidence_slot).
        async with _evidence_slot():
            async with async_session_factory() as db:
                result = await db.execute(select(model).where(model.scan_id == scan_id))
                return result.scalars().all()

    pages, forms, cookies, trackers, services, policies, signals = await asyncio.gather(
        *(_rows(model) for model in models)
    )
    return _serialize_evidence(
        pages, forms, cookies, trackers, services, policies, signals,
        include_cookie_detail=include_cookie_detail,
    )


async def get_policy_analysis_result(scan_id: uuid.UUID) -> dict | None:
    """The result of reading this scan's privacy and cookie policy text (stage `policy_analysis`), exactly as stored, or None when there is none yet. It was
    produced once, by the analysis; reading it costs no model call."""
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        row = (await db.execute(
            select(AgentRunStage.stage_metadata).where(AgentRunStage.scan_id == scan_id, AgentRunStage.stage == "policy_analysis", AgentRunStage.status == "completed")
            .order_by(AgentRunStage.completed_at.desc()).limit(1)
        )).first()
    result = (row[0] or {}).get("result") if row else None
    return result if isinstance(result, dict) else None


async def get_scan_evidence_for_score(scan_id: uuid.UUID) -> dict:
    """The evidence the DPDP score is computed from: the scan's persisted evidence, plus the policy analysis result when the analysis has run. Every place that
    scores a scan uses this one function, so the dashboard, the report and the backend record can never score different inputs."""
    import asyncio

    evidence, policy = await asyncio.gather(
        get_scan_evidence_summary_concurrent(scan_id, include_cookie_detail=True), get_policy_analysis_result(scan_id))
    return {**evidence, "policy_analysis": policy} if policy else evidence


async def get_scan_evidence_summary(db: AsyncSession, scan_id: uuid.UUID) -> dict:
    """Reassembles one scan's persisted evidence into a plain JSON-able dict for the
    agent's normalize node. Row ids (not the scanner's ephemeral local_ids) are what
    findings cite from this point on, since they're what a reviewer can actually look up."""

    async def _rows(model):
        result = await db.execute(select(model).where(model.scan_id == scan_id))
        return result.scalars().all()

    pages = await _rows(WebsitePage)
    forms = await _rows(ConsentForm)
    cookies = await _rows(Cookie)
    trackers = await _rows(Tracker)
    services = await _rows(ThirdPartyService)
    policies = await _rows(Policy)
    signals = await _rows(ConsentSignal)

    return _serialize_evidence(pages, forms, cookies, trackers, services, policies, signals)


async def latest_agent_run(db: AsyncSession, scan_id: uuid.UUID) -> AgentRun | None:
    """The most recent analysis run for a scan, or None if one was never started.

    Exists so a caller can tell "analysis ran and found nothing" apart from "analysis
    has not run". Those are the same response without it -- status `completed`, zero
    findings -- and the first reads as a clean bill of health.
    """
    result = await db.execute(
        select(AgentRun)
        .where(AgentRun.scan_id == scan_id)
        .order_by(AgentRun.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
