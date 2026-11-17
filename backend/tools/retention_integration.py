"""Integration check of the Enterprise retention policy and dashboard against the REAL Postgres. No model is called and no customer data is used.

    docker compose -f docker-compose.prod.yml cp backend/tools/retention_integration.py backend:/tmp/retention_integration.py
    docker compose -f docker-compose.prod.yml exec -T -w /app backend env PYTHONPATH=/app python /tmp/retention_integration.py

It builds throwaway organisations with finished scans, clears their temporary data, and checks that only temporary data went. Everything it creates is deleted at the end. Exit 0 only if all passed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import traceback
import uuid
from types import SimpleNamespace

from sqlalchemy import text

from app.db.session import async_session_factory, set_org_scope
from app.enterprise import retention
from app.enterprise.analysis.repo import SqlRepo
from app.enterprise.api import dashboard
from app.enterprise.config import get_enterprise_settings

RESULTS: list[tuple[str, bool]] = []
DOM = "retention-test.example.test"
BIG = "word " * 20000


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)


async def q(sql: str, **p):
    async with async_session_factory() as db:
        return (await db.execute(text(sql), p)).mappings().all()


async def make(org, status="completed", analysis="completed", age_hours=48, classified=True) -> uuid.UUID:
    async with async_session_factory() as db:
        sid = await db.scalar(text(
            "insert into ent_scans (org_id, website_url, domain, status, analysis_status, completed_at, analysis_completed_at) "
            "values (:o, :u, :d, :st, :an, now() - make_interval(hours => :h), now() - make_interval(hours => :h)) returning id"),
            {"o": org, "u": f"https://{DOM}/", "d": DOM, "st": status, "an": analysis, "h": age_hours})
        pages = [("/privacy", "privacy_policy"), ("/blog/a", "blog_article"), ("/services/x", "product"), ("/contact", "contact"), ("/about", "about")]
        for path, cat in pages:
            fid = await db.scalar(text(
                "insert into ent_frontier (scan_id, normalized_url, original_url, depth, discovery_source, status, final_url, page_category, analysis_status) "
                "values (:s, :u, :u, 1, 'link', 'done', :u, :c, 'done') returning id"), {"s": sid, "u": f"https://{DOM}{path}", "c": cat if classified else None})
            for kind in ("page_text", "page_html"):
                await db.execute(text(
                    "insert into ent_evidence (scan_id, frontier_id, kind, source_url, page_number, content_hash, content_type, byte_size, content, detection_method) "
                    "values (:s, :f, :k, :u, 0, :h, 'text/html', :b, :c, 'dom_render')"),
                    {"s": sid, "f": fid, "k": kind, "u": f"https://{DOM}{path}", "h": hashlib.sha256((path + kind + str(sid)).encode()).hexdigest(), "b": len(BIG), "c": BIG})
        for kind in ("cookie", "network", "form", "header", "consent"):
            await db.execute(text(
                "insert into ent_evidence (scan_id, kind, source_url, page_number, content_hash, content_type, byte_size, content, detection_method) "
                "values (:s, :k, :u, 0, :h, 'application/json', 10, '{}', 'x')"), {"s": sid, "k": kind, "u": f"https://{DOM}/", "h": hashlib.sha256(kind.encode() + str(sid).encode()).hexdigest()})
        await db.execute(text("insert into ent_reports (scan_id, version, status, pdf, sha256, byte_size, page_count) values (:s, 1, 'ready', :p, 'x', 4, 1)"), {"s": sid, "p": b"%PDF"})
        for i in range(3):
            await db.execute(text(
                "insert into ent_analysis_chunks (scan_id, source_url, chunk_index, chunk_count, level, status, input_chars, result, text_hash, extractor_key, provider, model) "
                "values (:s, :u, :i, 3, 'page', 'done', 100, cast(:r as jsonb), :h, 'k1', 'claude', 'm')"),
                {"s": sid, "u": f"https://{DOM}/privacy", "i": i, "r": json.dumps({"facts": [{"topic": "t", "quote": "q" * 30}] * 2, "raw": "x" * 500}), "h": f"hash-{i}-{sid}"})
        await db.commit()
    return sid


async def temp_rows(sid):
    return await q("select source_url, kind from ent_evidence where scan_id = :s and kind in ('page_text','page_html','document_text')", s=sid)


async def run(org, other) -> None:
    set_org_scope(org)
    fresh = await make(org, age_hours=0)
    r = await retention.purge_scan(fresh)
    check("retention: a scan inside the grace period is not cleared", r.get("skipped") == "within the retention period" and len(await temp_rows(fresh)) == 10, str(r))

    old = await make(org, age_hours=48)
    r = await retention.purge_scan(old)
    left = await temp_rows(old)
    kept_urls = {x["source_url"].split(DOM)[1] for x in left}
    check("retention: after the grace period only pages no assessment cites are cleared", kept_urls == {"/privacy", "/contact", "/about"} and r.get("evidence_rows_cleared") == 4, f"{kept_urls} {r}")
    others = await q("select kind, count(*) n from ent_evidence where scan_id = :s and kind in ('cookie','network','form','header','consent') group by kind", s=old)
    check("retention: browser observations are all kept", len(others) == 5 and all(o["n"] == 1 for o in others))
    check("retention: the stored report is not touched", (await q("select count(*) n from ent_reports where scan_id = :s and pdf is not null", s=old))[0]["n"] == 1)
    check("retention: the URL inventory is not touched", (await q("select count(*) n from ent_frontier where scan_id = :s", s=old))[0]["n"] == 5)
    chunks = await q("select result from ent_analysis_chunks where scan_id = :s", s=old)
    check("retention: passage results are compacted, their counts kept", len(chunks) == 3 and all(c["result"].get("compacted") and c["result"]["facts"] == 2 and "raw" not in c["result"] for c in chunks))
    check("retention: what was cleared is recorded on the scan", (await q("select purge_summary->>'reusable_results_cached' as n from ent_scans where id = :s", s=old))[0]["n"] == "3")

    repo = SqlRepo()
    hit = await repo.find_reusable_extraction(fresh, f"hash-0-{old}", "k1")
    check("cache: the facts of a cleared passage are still reusable by a later scan of the same client", hit is not None and hit["facts"][0]["quote"] == "q" * 30)
    foreign_asker = await make(other, age_hours=1)
    check("cache: and not by another client", await repo.find_reusable_extraction(foreign_asker, f"hash-0-{old}", "k1") is None)
    check("retention: a scan is cleared once", (await retention.purge_scan(old)).get("skipped") == "already cleared")

    unanalysed = await make(org, analysis="not_started", age_hours=48)
    check("retention: a scan that was not analysed is left alone", bool((await retention.purge_scan(unanalysed)).get("skipped")) and len(await temp_rows(unanalysed)) == 10)
    unclassified = await make(org, classified=False, age_hours=48)
    ru = await retention.purge_scan(unclassified)
    kept_u = {x["source_url"].split(DOM)[1] for x in await temp_rows(unclassified)}
    check("retention: a scan from before page classification is cleared by address, generously (policy, contact and about pages kept)", kept_u == {"/privacy", "/contact", "/about"} and ru.get("evidence_rows_cleared") == 4 and "address" in ru.get("pages_kept_by", ""), f"{kept_u} {ru}")

    foreign = await make(other, age_hours=48)
    await retention.purge_expired(org)
    check("retention: a sweep of one client never touches another client's scans", len(await temp_rows(foreign)) == 10)
    check("retention: the sweep cleared this client's expired scans", len(await temp_rows(old)) == 6)
    held = await retention.held_now(org)
    check("retention: the held-now figures count what is left", held["scans_cleared"] >= 1 and held["policy"]["grace_hours"] == get_enterprise_settings().temp_retention_hours)

    async with async_session_factory() as db:
        for status, an in (("failed", "not_started"), ("queued", "not_started"), ("running", "not_started"), ("cancelled", "not_started"), ("completed", "failed")):
            await db.execute(text("insert into ent_scans (org_id, website_url, domain, status, analysis_status, error) values (:o, :u, :d, :st, :an, :e)"),
                             {"o": org, "u": f"https://{DOM}/", "d": DOM, "st": status, "an": an, "e": "x" if status == "failed" else None})
        await db.commit()
    if not get_enterprise_settings().enabled:
        print("dashboard: Enterprise is not enabled here; the summary checks are skipped")
        return
    async with async_session_factory() as db:
        user = SimpleNamespace(org_id=str(org), role="admin", key_name=None)
        s1 = await dashboard.summary(outcome="all", q=None, offset=0, limit=50, refresh=True, user=user, db=db)
        s2 = await dashboard.summary(outcome="all", q=None, offset=0, limit=50, refresh=False, user=user, db=db)
        failed = await dashboard.summary(outcome="failed", q=None, offset=0, limit=50, refresh=True, user=user, db=db)
        pend = await dashboard.summary(outcome="pending", q=None, offset=0, limit=50, refresh=True, user=user, db=db)
    k = s1["kpis"]
    check("dashboard: scans are counted by outcome", k["scans_failed"] == 2 and k["scans_pending"] == 4 and k["scans_completed"] == 3 and k["scans_total"] == 9, str(k))
    check("dashboard: URLs discovered, crawled and analysed are summed over the scans (the cleared one included)", k["urls_discovered"] == 20 and k["urls_crawled"] == 20 and k["urls_analysed"] == 20, str(k))
    check("dashboard: the history can be filtered by outcome", failed["history"]["total"] == 2 and pend["history"]["total"] == 4 and all(i["outcome"] == "failed" for i in failed["history"]["items"]))
    check("dashboard: the second request is served from the cache, a refresh is not", s2["cached"] is True and s1["cached"] is False)
    check("dashboard: the history says whether a report exists and carries no report content", any(i["report_available"] for i in s1["history"]["items"]) and "%PDF" not in json.dumps(s1, default=str))
    check("dashboard: the last scan is the newest", s1["last_scan"] is not None and s1["last_scan"]["scan_id"] == s1["history"]["items"][0]["scan_id"])
    check("dashboard: a scan whose temporary data was cleared says so, and keeps its figures", any(i["temporary_data_cleared"] and i["urls_crawled"] == 5 for i in s1["history"]["items"]))


async def main() -> int:
    org, other = uuid.uuid4(), uuid.uuid4()
    try:
        await run(org, other)
    except Exception:
        traceback.print_exc()
        check("the run completed without an exception", False)
    finally:
        async with async_session_factory() as db:
            for o in (org, other):
                await db.execute(text("delete from ent_fact_cache where org_id = :o"), {"o": o})
                await db.execute(text("delete from ent_scans where org_id = :o"), {"o": o})
            await db.commit()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
