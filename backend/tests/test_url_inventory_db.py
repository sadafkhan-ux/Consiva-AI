"""The URL inventory against a real database: persistence, constraints, the filtered read, row-level security and tenant isolation.

Like test_scan_result_persistence_ordering.py it builds its own synthetic organisation, website and scan from fresh ids and rolls everything back, so it
commits nothing and cannot touch a real row.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.exceptions import NotFoundError
from app.core.security import CurrentUser
from app.db.repositories import scan_repository
from app.rules.consent_rules import RulesClassification
from app.scanner.schemas import ConsentSignalRecord, PageRecord, ScanResult
from app.scanner.url_inventory import UrlInventoryRecord
from app.scanner.url_normalize import normalize_url
from tests.live_db import read_only_dsn

DSN = read_only_dsn()
pytestmark = pytest.mark.skipif(not DSN, reason="no database configured; this test needs a real DB")

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)


def _rec(path, *, status="PENDING", depth=1, source="link", sources=None, http=None, final=None, title=None, ctype=None, reason=None, failure=None):
    url = f"https://example.test{path}"
    return UrlInventoryRecord(
        domain="example.test", original_url=url, normalized_url=normalize_url(url), discovery_source=source, discovery_sources=sources or [source],
        crawl_depth=depth, scan_status=status, status_reason=reason, failure_reason=failure, http_status=http, final_url=final, page_title=title, content_type=ctype,
        discovered_at=NOW, last_updated_at=NOW, scan_started_at=NOW if status not in ("PENDING", "EXCLUDED") else None,
        scan_completed_at=NOW if status in ("COMPLETED", "FAILED", "PARTIALLY_SCANNED") else None,
    )


def _result(records):
    return ScanResult(
        domain="example.test", root_url="https://example.test/", scanner_version="test",
        pages=[PageRecord(local_id="page-0", url="https://example.test/", title="Home", http_status=200, discovered_via="seed")],
        consent_signals=ConsentSignalRecord(mechanism_type="none"), url_inventory=records,
    )


RECORDS = [
    _rec("/", status="COMPLETED", depth=0, source="seed", http=200, final="https://example.test/", title="Home", ctype="text/html"),
    _rec("/about", status="COMPLETED", depth=1, sources=["sitemap", "link"], source="sitemap", http=200, title="About", ctype="text/html"),
    _rec("/gone", status="FAILED", depth=1, http=404, failure="HTTP 404"),
    _rec("/file.pdf", status="PARTIALLY_SCANNED", depth=2, http=200, ctype="application/pdf", reason="not an HTML page (application/pdf); no page text was extracted"),
    _rec("/private/x", status="EXCLUDED", depth=1, reason="robots_disallowed"),
    _rec("/later", status="PENDING", depth=3, reason="page_limit"),
]


@pytest.fixture
async def seeded():
    engine = create_async_engine(DSN, connect_args={"timeout": 10})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    org_id, website_id, scan_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as db:
        try:
            await db.execute(text("INSERT INTO websites (id, org_id, domain) VALUES (:id, :o, 'example.test')"), {"id": website_id, "o": org_id})
            await db.execute(text("INSERT INTO consent_scans (id, org_id, website_id, url, status) VALUES (:id, :o, :w, 'https://example.test/', 'pending')"),
                             {"id": scan_id, "o": org_id, "w": website_id})
            await db.flush()
            await scan_repository.save_scan_result(db, scan_id=scan_id, scan_result=_result(RECORDS), classification=RulesClassification(cookies=[], trackers=[], third_party_services=[]))
            await db.flush()
            yield db, org_id, scan_id
        finally:
            await db.rollback()
            await engine.dispose()


async def test_every_record_is_stored_with_its_fields_and_the_page_it_belongs_to(seeded):
    db, _org, scan_id = seeded
    rows = (await db.execute(text("SELECT * FROM scan_url_inventory WHERE scan_id = :s ORDER BY crawl_depth, original_url"), {"s": scan_id})).mappings().all()
    assert len(rows) == 6
    home = next(r for r in rows if r["original_url"] == "https://example.test/")
    assert (home["scan_status"], home["crawl_depth"], home["page_title"], home["http_status"], home["content_type"]) == ("COMPLETED", 0, "Home", 200, "text/html")
    assert home["website_page_id"] is not None                      # linked to the page row of the same address, for later modules to attach to
    about = next(r for r in rows if r["original_url"].endswith("/about"))
    assert about["discovery_sources"] == ["sitemap", "link"] and about["website_page_id"] is None   # never navigated as its own page record here
    assert next(r for r in rows if r["original_url"].endswith("/gone"))["failure_reason"] == "HTTP 404"
    assert next(r for r in rows if r["original_url"].endswith("/later"))["scan_status"] == "PENDING"


async def test_the_summary_counts_every_status_over_the_whole_inventory(seeded):
    db, _org, scan_id = seeded
    got = await scan_repository.get_url_inventory(db, scan_id, limit=2)
    assert got["summary"] == {"pending": 1, "in_progress": 0, "completed": 2, "failed": 1, "partially_scanned": 1, "excluded": 1, "total": 6}
    assert got["total"] == 6 and len(got["rows"]) == 2           # the page is a slice; the summary is not


@pytest.mark.parametrize("kw,expected", [
    ({"status": "FAILED"}, {"/gone"}),
    ({"status": "completed"}, {"/", "/about"}),
    ({"http_status": 404}, {"/gone"}),
    ({"source": "sitemap"}, {"/about"}),
    ({"source": "link"}, {"/about", "/gone", "/file.pdf", "/private/x", "/later"}),
    ({"depth": 1}, {"/about", "/gone", "/private/x"}),
    ({"q": "ABOUT"}, {"/about"}),
    ({"q": "private"}, {"/private/x"}),
    ({"failure": "robots"}, {"/private/x"}),
    ({"failure": "404"}, {"/gone"}),
    ({"failure": "page_limit"}, {"/later"}),
    ({"status": "COMPLETED", "depth": 1}, {"/about"}),
    ({"q": "nothing-matches"}, set()),
])
async def test_the_filters_select_the_right_rows(seeded, kw, expected):
    db, _org, scan_id = seeded
    got = await scan_repository.get_url_inventory(db, scan_id, **kw)
    paths = {r.original_url.replace("https://example.test", "") or "/" for r in got["rows"]}
    assert paths == expected and got["total"] == len(expected)


async def test_a_percent_or_underscore_in_a_search_is_matched_literally(seeded):
    db, _org, scan_id = seeded
    assert (await scan_repository.get_url_inventory(db, scan_id, q="%"))["total"] == 0
    assert (await scan_repository.get_url_inventory(db, scan_id, q="_"))["total"] == 0


async def test_paging_walks_the_whole_inventory_without_repeats(seeded):
    db, _org, scan_id = seeded
    seen = []
    for offset in (0, 2, 4):
        seen += [r.original_url for r in (await scan_repository.get_url_inventory(db, scan_id, limit=2, offset=offset))["rows"]]
    assert len(seen) == len(set(seen)) == 6


async def test_a_scan_with_no_inventory_returns_zero_counts_and_no_rows(seeded):
    db, _org, _scan = seeded
    got = await scan_repository.get_url_inventory(db, uuid.uuid4())
    assert got["summary"]["total"] == 0 and got["rows"] == [] and got["total"] == 0


# ── constraints ──────────────────────────────────────────────────────────────────────────────────────

async def test_the_same_url_cannot_be_recorded_twice_for_one_scan(seeded):
    db, _org, scan_id = seeded
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(text("INSERT INTO scan_url_inventory (scan_id, domain, original_url, normalized_url, discovery_source, crawl_depth) "
                                  "VALUES (:s, 'example.test', 'https://example.test/about', 'https://example.test/about', 'link', 1)"), {"s": scan_id})


@pytest.mark.parametrize("cols,vals", [
    ("scan_status", "'WORKING'"),                                   # not a status
    ("crawl_depth", "-1"),                                          # depth is never negative
    ("scan_status", "'FAILED'"),                                    # a failure must say why
    ("scan_status", "'EXCLUDED'"),                                  # an exclusion must say why
])
async def test_the_database_refuses_an_invalid_record(seeded, cols, vals):
    db, _org, scan_id = seeded
    base = {"scan_status": "'PENDING'", "crawl_depth": "1"}
    base[cols] = vals
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(text(f"INSERT INTO scan_url_inventory (scan_id, domain, original_url, normalized_url, discovery_source, crawl_depth, scan_status) "
                                  f"VALUES (:s, 'example.test', 'https://example.test/bad-{uuid.uuid4().hex[:6]}', 'https://example.test/bad-{uuid.uuid4().hex[:6]}', 'link', {base['crawl_depth']}, {base['scan_status']})"),
                             {"s": scan_id})


async def test_removing_a_scan_removes_its_inventory(seeded):
    db, _org, scan_id = seeded
    await db.execute(text("DELETE FROM consent_scans WHERE id = :s"), {"s": scan_id})
    assert (await db.execute(text("SELECT count(*) FROM scan_url_inventory WHERE scan_id = :s"), {"s": scan_id})).scalar_one() == 0


# ── tenant isolation and row-level security ──────────────────────────────────────────────────────────

async def test_rls_is_enabled_and_forced_on_the_inventory_and_has_a_tenant_policy(seeded):
    db, _org, _scan = seeded
    row = (await db.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'scan_url_inventory'"))).one()
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True)
    policy = (await db.execute(text("SELECT count(*) FROM pg_policies WHERE tablename = 'scan_url_inventory' AND policyname = 'tenant_isolation'"))).scalar_one()
    assert policy == 1


async def test_the_inventory_endpoint_answers_for_the_owning_organisation_and_not_found_for_another(seeded):
    from app.api.v1.routes import consent_scans as routes

    db, org_id, scan_id = seeded
    owner = CurrentUser(user_id=str(uuid.uuid4()), org_id=str(org_id), role="admin")
    other = CurrentUser(user_id=str(uuid.uuid4()), org_id=str(uuid.uuid4()), role="admin")
    ok = await routes.get_scan_url_inventory(scan_id, status=None, http_status=None, source=None, depth=None, q=None, failure=None, limit=100, offset=0, user=owner, db=db)
    assert ok.summary.total == 6 and ok.total == 6 and len(ok.urls) == 6
    assert {u.scan_status for u in ok.urls} == {"COMPLETED", "FAILED", "PARTIALLY_SCANNED", "EXCLUDED", "PENDING"}
    with pytest.raises(NotFoundError):                              # another organisation gets 404, never "exists but forbidden"
        await routes.get_scan_url_inventory(scan_id, status=None, http_status=None, source=None, depth=None, q=None, failure=None, limit=100, offset=0, user=other, db=db)


async def test_the_four_tables_that_enabled_rls_without_forcing_it_are_now_forced(seeded):
    db, _org, _scan = seeded
    rows = (await db.execute(text("SELECT relname, relforcerowsecurity FROM pg_class WHERE relname IN ('dsr_evidence_pushes','scan_screenshots','llm_usage_events','report_pdfs')"))).all()
    assert len(rows) == 4 and all(forced for _name, forced in rows)
