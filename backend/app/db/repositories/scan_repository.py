import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ConsentForm,
    ConsentScan,
    ConsentSignal,
    Cookie,
    Policy,
    ThirdPartyService,
    Tracker,
    Website,
    WebsitePage,
)
from app.rules.consent_rules import RulesClassification
from app.scanner.schemas import ScanResult


async def count_scans_since(db: AsyncSession, *, org_id: uuid.UUID, since: datetime) -> int:
    result = await db.execute(
        select(func.count()).select_from(ConsentScan).where(ConsentScan.org_id == org_id, ConsentScan.created_at >= since)
    )
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


async def mark_scan_running(db: AsyncSession, scan_id: uuid.UUID) -> None:
    scan = await db.get(ConsentScan, scan_id)
    if scan:
        scan.status = "running"
        scan.started_at = datetime.now(UTC)
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
) -> None:
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

    scan = await db.get(ConsentScan, scan_id)
    if scan:
        scan.status = "completed"
        scan.completed_at = datetime.now(UTC)

    await db.flush()


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


def _serialize_evidence(
    pages: list, forms: list, cookies: list, trackers: list, services: list, policies: list, signals: list
) -> dict:
    return {
        "pages": [{"id": str(p.id), "url": p.url, "title": p.title} for p in pages],
        "forms": [
            {"id": str(f.id), "selector": f.selector, "fields": f.fields, "purpose_guess": f.purpose_guess}
            for f in forms
        ],
        "cookies": [
            {"id": str(c.id), "name": c.name, "domain": c.domain, "category": c.category, "vendor": c.vendor,
             "is_first_party": c.is_first_party, "source": c.source, "consent_states": c.consent_states}
            for c in cookies
        ],
        "trackers": [
            {"id": str(t.id), "script_src": t.script_src, "vendor": t.vendor, "category": t.category,
             "source": t.source, "consent_states": t.consent_states}
            for t in trackers
        ],
        "third_party_services": [
            {"id": str(s.id), "service_name": s.service_name, "category": s.category, "domains": s.domains}
            for s in services
        ],
        "policies": [{"id": str(p.id), "url": p.url, "policy_type": p.policy_type} for p in policies],
        "consent_signals": [
            {"mechanism_type": s.mechanism_type, "cmp_vendor": s.cmp_vendor,
             "has_reject_all": s.has_reject_all, "has_granular_choices": s.has_granular_choices,
             # accept_interaction/reject_interaction/confidence/detection_source live
             # here -- previously persisted but never read back out, so
             # evaluate_consent_rules() (and therefore the LLM) had no way to know
             # whether Accept/Reject were ever actually automatable on this scan.
             "evidence": s.evidence}
            for s in signals
        ],
    }


async def get_scan_evidence_summary_concurrent(scan_id: uuid.UUID) -> dict:
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
        async with async_session_factory() as db:
            result = await db.execute(select(model).where(model.scan_id == scan_id))
            return result.scalars().all()

    pages, forms, cookies, trackers, services, policies, signals = await asyncio.gather(
        *(_rows(model) for model in models)
    )
    return _serialize_evidence(pages, forms, cookies, trackers, services, policies, signals)


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
