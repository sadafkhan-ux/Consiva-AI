"""Live-DB regression test for a real bug: an earlier version of
scan_repository.save_scan_result batched page/tracker inserts assuming SQLAlchemy
would topologically sort them ahead of the forms/cookies/etc. that reference their
ids -- it doesn't, for these models (no ORM relationship() is declared between them,
only plain FK columns), so ConsentForm/Cookie rows could be flushed before the
WebsitePage/Tracker row they reference, raising a live
asyncpg.exceptions.ForeignKeyViolationError. Caught scanning a real 25-page site, not
by unit test -- this test exists so it's caught by CI next time instead.

Needs a real DB (FK constraints aren't enforced by a mock) -- follows the same safe,
non-os.environ-polluting Settings pattern as test_rag_retrieval_quality.py. Creates its
OWN synthetic org/website/scan (org_id/website_id/authorized_by_user_id carry no FK
constraint in db/models.py, so a fresh uuid4() is valid) rather than borrowing an
existing real scan_id -- save_scan_result mutates scan.status/completed_at as its last
step, so reusing a real scan_id would corrupt real data. Nothing this test does is ever
committed (save_scan_result itself only flushes) -- a single rollback() at the end
discards the synthetic website/scan/pages/etc. as a unit, so cleanup can't leave
partial state behind and can never touch a real, pre-existing row.
"""

import uuid
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.repositories import scan_repository
from app.rules.consent_rules import RulesClassification
from app.scanner.schemas import (
    ConsentSignalRecord,
    CookieRecord,
    FormField,
    FormRecord,
    PageRecord,
    ScanResult,
    TrackerRecord,
)

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _real_settings() -> Settings | None:
    values = dotenv_values(_ENV_FILE)
    database_url = values.get("DATABASE_URL")
    if not database_url:
        return None
    return Settings(
        nvidia_api_key="unused-in-this-test", nvidia_llm_model="unused", nvidia_embed_model="unused",
        supabase_url=values.get("SUPABASE_URL", "http://localhost"), supabase_service_role_key="unused",
        database_url=database_url, supabase_jwt_secret="unused", app_env="development",
    )


pytestmark = pytest.mark.skipif(
    _real_settings() is None, reason="DATABASE_URL not configured in backend/.env; this test needs a real DB"
)


async def test_save_scan_result_with_multiple_pages_and_cross_references():
    """Builds a ScanResult shaped like a real multi-page crawl -- several pages, a
    form and a tracker on a LATER page (not the first, so insertion order actually
    matters), and a cookie set by that tracker -- and confirms it persists with no FK
    violation. This is exactly the shape that broke live on swaransoft.com."""
    settings = _real_settings()
    assert settings is not None
    engine = create_async_engine(settings.database_url, connect_args={"timeout": 10})
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    org_id = uuid.uuid4()
    website_id = uuid.uuid4()
    scan_id = uuid.uuid4()

    async with session_factory() as db:
        try:
            await db.execute(
                text("INSERT INTO websites (id, org_id, domain) VALUES (:id, :org_id, :domain)"),
                {"id": website_id, "org_id": org_id, "domain": "example.test"},
            )
            await db.execute(
                text(
                    "INSERT INTO consent_scans (id, org_id, website_id, url, status) "
                    "VALUES (:id, :org_id, :website_id, :url, 'pending')"
                ),
                {"id": scan_id, "org_id": org_id, "website_id": website_id, "url": "https://example.test/"},
            )
            await db.flush()

            pages = [
                PageRecord(local_id="page-0", url="https://example.test/", title="Home", http_status=200, discovered_via="seed"),
                PageRecord(local_id="page-1", url="https://example.test/about", title="About", http_status=200, discovered_via="link"),
                PageRecord(local_id="page-2", url="https://example.test/contact", title="Contact", http_status=200, discovered_via="link"),
            ]
            scan_result = ScanResult(
                domain="example.test", root_url="https://example.test/", scanner_version="test",
                pages=pages,
                forms=[FormRecord(
                    local_id="form-0",
                    page_local_id="page-2",  # the LAST page, not the first -- exercises real ordering
                    selector="form#contact", fields=[FormField(name="email", field_type="email", required=True)],
                    purpose_guess="contact_or_support", action_url=None,
                )],
                consent_signals=ConsentSignalRecord(mechanism_type="none"),
            )
            tracker = TrackerRecord(
                local_id="tracker-0", page_local_id="page-1", script_src="https://cdn.example.test/analytics.js",
                vendor=None, category=None, source="rule", consent_states=["pre_consent"],
            )
            cookie = CookieRecord(
                local_id="cookie-0", name="_analytics_id", domain="example.test", path="/",
                set_by_tracker_local_id="tracker-0", source="rule", consent_states=["pre_consent"],
            )
            classification = RulesClassification(cookies=[cookie], trackers=[tracker], third_party_services=[])

            # The real assertion: this must not raise ForeignKeyViolationError.
            await scan_repository.save_scan_result(
                db, scan_id=scan_id, scan_result=scan_result, classification=classification
            )
            await db.flush()

            # Confirm the cross-references actually resolved to real, matching rows --
            # not just "didn't crash".
            r = await db.execute(text(
                "SELECT cf.page_id, wp.url FROM consent_forms cf JOIN website_pages wp ON cf.page_id = wp.id "
                "WHERE cf.scan_id = :s AND cf.selector = 'form#contact'"
            ), {"s": scan_id})
            form_row = r.first()
            assert form_row is not None
            assert form_row.url == "https://example.test/contact"

            r = await db.execute(text(
                "SELECT c.set_by_tracker_id, t.script_src FROM cookies c JOIN trackers t ON c.set_by_tracker_id = t.id "
                "WHERE c.scan_id = :s AND c.name = '_analytics_id'"
            ), {"s": scan_id})
            cookie_row = r.first()
            assert cookie_row is not None
            assert cookie_row.script_src == "https://cdn.example.test/analytics.js"
        finally:
            # Nothing above was ever committed -- rollback discards the synthetic
            # website/scan/pages/forms/trackers/cookies as one unit. No real row is
            # ever at risk since this test only ever touches ids it generated itself.
            await db.rollback()

    await engine.dispose()
