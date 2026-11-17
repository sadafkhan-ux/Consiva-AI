"""Integration check of the Enterprise policy knowledge base against the REAL Postgres (and pgvector), with a deterministic fake embedder, so no model is called and no customer data is used.

Run inside the backend container (the database is not published to the host):

    docker compose -f docker-compose.prod.yml cp backend/tools/policy_kb_integration.py backend:/tmp/policy_kb_integration.py
    docker compose -f docker-compose.prod.yml exec -T -w /app backend env PYTHONPATH=/app python /tmp/policy_kb_integration.py

Every row it creates belongs to throwaway organisations and is deleted at the end (also on failure). Exit code 0 only if every scenario passed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import traceback
import uuid

from sqlalchemy import text

from app.db.session import async_session_factory, set_org_scope
from app.enterprise.analysis.repo import SqlRepo
from app.enterprise.analysis import extract as ex
from app.enterprise.policykb import history, metrics, retrieve, sync

DIM = 1024
RESULTS: list[tuple[str, bool, str]] = []


class FakeEmbedder:
    """Deterministic vectors from the text; counts what it was asked to embed. `fail` makes every call raise (a provider or vector-store outage)."""

    def __init__(self):
        self.texts: list[str] = []
        self.fail = False

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding provider unavailable")
        self.texts.extend(texts)
        out = []
        for t in texts:
            vec = [0.0] * DIM
            for w in t.lower().split():
                h = int(hashlib.sha256(w.encode()).hexdigest(), 16)
                vec[h % DIM] += 1.0
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)


POLICY = [
    ("Information we collect", "We collect your name, email address and phone number when you contact us. We also collect device and usage data through cookies."),
    ("How we use your information", "We use personal data to respond to enquiries, to improve our services and to send updates you have asked for."),
    ("Data retention", "We retain personal data for 90 days after our last contact with you and then delete it."),
    ("Your rights", "You may access, correct or erase your personal data and withdraw consent at any time by writing to privacy@example.test."),
    ("Grievance officer", "Our Grievance Officer can be reached at grievance@example.test and will reply within 15 days."),
]


def html_for(sections, extra_nav: bool = True) -> str:
    body = "".join(f"<h2>{h}</h2><p>{p}</p>" for h, p in sections)
    nav = "<nav><a href='/'>Home</a><a href='/about'>About</a></nav><header>Site header</header>" if extra_nav else ""
    return f"<html><head><title>Privacy Policy</title><script>var x=1;</script><style>p{{color:red}}</style></head><body>{nav}<main><h1>Privacy Policy</h1>{body}</main><footer>Footer links</footer></body></html>"


async def new_scan(org: uuid.UUID, domain: str, pages: list[dict], *, stop_reason: str | None = None) -> uuid.UUID:
    """A finished scan with the given pages. Each page: url, category, html, text (optional), status ('done' or 'failed'), http_status."""
    async with async_session_factory() as db:
        scan = await db.scalar(text("insert into ent_scans (org_id, website_url, domain, status, stop_reason) values (:o, :u, :d, 'completed', :sr) returning id"),
                               {"o": org, "u": f"https://{domain}/", "d": domain, "sr": stop_reason})
        for p in pages:
            status = p.get("status", "done")
            fid = await db.scalar(text(
                "insert into ent_frontier (scan_id, normalized_url, original_url, depth, discovery_source, status, final_url, http_status, page_category, page_category_strength, category_reason, page_title, last_error, also_categories) "
                "values (:s, :u, :u, 1, 'link', :st, :u, :h, :c, 'high', :r, 'Privacy Policy', :e, :a) returning id"),
                {"s": scan, "u": p["url"], "st": status, "h": p.get("http_status", 200 if status == "done" else None), "c": p.get("category"), "r": "slug", "e": p.get("error"), "a": p.get("also")})
            for kind, content in (("page_html", p.get("html")), ("page_text", p.get("text"))):
                if content:
                    await db.execute(text(
                        "insert into ent_evidence (scan_id, frontier_id, kind, source_url, page_number, content_hash, content_type, byte_size, content, detection_method) "
                        "values (:s, :f, :k, :u, 0, :h, 'text/html', :b, :c, 'dom_render')"),
                        {"s": scan, "f": fid, "k": kind, "u": p["url"], "h": hashlib.sha256(content.encode()).hexdigest(), "b": len(content.encode()), "c": content})
        await db.commit()
    return scan


async def q(sql: str, **p):
    async with async_session_factory() as db:
        return (await db.execute(text(sql), p)).mappings().all()


async def run() -> None:
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    dom = "kb-test.example.test"
    emb = FakeEmbedder()
    set_org_scope(org_a)
    PRIV = f"https://{dom}/privacy"
    privacy_v1 = html_for(POLICY)

    # 1. a new Privacy Policy
    s1 = await new_scan(org_a, dom, [{"url": PRIV, "category": "privacy_policy", "html": privacy_v1, "text": "Privacy Policy " + " ".join(p for _, p in POLICY)}])
    r1 = await sync.sync_scan(s1, org_a, embedder=emb)
    docs = await q("select id, status, stale from ent_policy_docs where org_id = :o and domain = :d", o=org_a, d=dom)
    vers = await q("select version_no, is_current, sync_status, chunk_count, text from ent_policy_versions where org_id = :o", o=org_a)
    chunks = await q("select count(*) n, count(*) filter (where is_active) a, count(*) filter (where embedding is not null) e from ent_policy_chunks where org_id = :o", o=org_a)
    check("1 new policy: stored as version 1, current, ready", r1.counts["new"] == 1 and len(vers) == 1 and vers[0]["is_current"] and vers[0]["sync_status"] == "ready")
    check("1 new policy: every chunk active and embedded", chunks[0]["n"] > 0 and chunks[0]["n"] == chunks[0]["a"] == chunks[0]["e"] == vers[0]["chunk_count"], str(dict(chunks[0])))
    check("1 cleaning: scripts, styles, navigation and footer are not in the stored text", all(w not in vers[0]["text"] for w in ("var x=1", "color:red", "Site header", "Footer links", "Home")))
    check("1 cleaning: every section heading and paragraph is in the stored text", all(h in vers[0]["text"] and p in vers[0]["text"] for h, p in POLICY))
    doc_id = docs[0]["id"]
    first_embeds = len(emb.texts)

    # 2. unchanged policy, rescanned: no new version, no embedding call
    s2 = await new_scan(org_a, dom, [{"url": PRIV, "category": "privacy_policy", "html": privacy_v1, "text": "Privacy Policy " + " ".join(p for _, p in POLICY)}])
    r2 = await sync.sync_scan(s2, org_a, embedder=emb)
    nvers = (await q("select count(*) n from ent_policy_versions where org_id = :o", o=org_a))[0]["n"]
    check("2 unchanged: reported unchanged, still one version", r2.counts["unchanged"] == 1 and nvers == 1)
    check("2 unchanged: no embedding was requested", len(emb.texts) == first_embeds and r2.embeddings_generated == 0)

    # 12. the same scan synchronised twice is idempotent
    r2b = await sync.sync_scan(s2, org_a, embedder=emb)
    check("12 repeated sync of the same scan: unchanged again, nothing new", r2b.counts["unchanged"] == 1 and len(emb.texts) == first_embeds)

    # 3. a modified policy: new version, only the changed passage is embedded, old version kept
    modified = [(h, p.replace("90 days", "30 days")) if h == "Data retention" else (h, p) for h, p in POLICY]
    s3 = await new_scan(org_a, dom, [{"url": PRIV, "category": "privacy_policy", "html": html_for(modified), "text": "x"}])
    before = len(emb.texts)
    r3 = await sync.sync_scan(s3, org_a, embedder=emb)
    vers = await q("select version_no, is_current, chunk_count from ent_policy_versions where org_id = :o order by version_no", o=org_a)
    old_active = (await q("select count(*) n from ent_policy_chunks c join ent_policy_versions v on v.id = c.version_id where c.org_id = :o and v.version_no = 1 and c.is_active", o=org_a))[0]["n"]
    new_active = (await q("select count(*) n from ent_policy_chunks c join ent_policy_versions v on v.id = c.version_id where c.org_id = :o and v.version_no = 2 and c.is_active", o=org_a))[0]["n"]
    check("3 changed: version 2 is current, version 1 kept but not current", r3.counts["changed"] == 1 and [v["is_current"] for v in vers] == [False, True])
    check("3 changed: the old version's chunks are no longer searchable, the new version's are", old_active == 0 and new_active == vers[1]["chunk_count"])
    check("3 changed: unchanged passages reused their embeddings, only the changed passage was embedded", r3.embeddings_reused > 0 and 0 < r3.embeddings_generated < vers[1]["chunk_count"] and len(emb.texts) - before == r3.embeddings_generated,
          f"generated={r3.embeddings_generated} reused={r3.embeddings_reused} chunks={vers[1]['chunk_count']}")
    d = await history.diff_versions(org_a, doc_id, 1, 2)
    check("3 history: the diff shows the retention change", d["counts"]["changed"] + d["counts"]["added"] >= 1 and any("30 days" in (c.get("after") or c.get("text", "")) for c in d["changed"] + d["added"]) and any("retention" in c["topics"] for c in d["changed"]))
    check("3 history: affected parameters are named", "N5" in history.affected_parameters(["privacy_policy"]))

    # 4. a new cookie policy
    COOKIE = f"https://{dom}/cookie-policy"
    cookie_html = html_for([("What are cookies", "Cookies are small files stored on your device."), ("Cookies we use", "We use analytics cookies _ga and _gid that last two years.")])
    s4 = await new_scan(org_a, dom, [{"url": PRIV, "category": "privacy_policy", "html": html_for(modified), "text": "x"}, {"url": COOKIE, "category": "cookie_policy", "html": cookie_html, "text": "y"}])
    r4 = await sync.sync_scan(s4, org_a, embedder=emb)
    check("4 new cookie policy: added, the privacy policy stays unchanged", r4.counts["new"] == 1 and r4.counts["unchanged"] == 1)

    # hybrid retrieval: scoped, current-only, with sources
    res = await retrieve.retrieve(org_a, dom, "how long do you retain personal data", query_embedder=emb, parameter_id="N5")
    top = res["hits"][0] if res["hits"] else {}
    check("retrieval: hybrid, finds the retention section of the CURRENT version with its source", res["mode"] == "hybrid" and "30 days" in top.get("content", "") and top.get("version_no") == 2 and top.get("source_url") == PRIV)
    res_old = await retrieve.retrieve(org_a, dom, "retain 90 days", query_embedder=emb, log=False)
    check("retrieval: the superseded version's text is never returned", all("90 days" not in h["content"] for h in res_old["hits"]))
    emb.fail = True
    res_kw = await retrieve.retrieve(org_a, dom, "grievance officer", query_embedder=emb, log=False)
    emb.fail = False
    check("retrieval: an embedding outage falls back to keyword search and says so", res_kw["mode"] == "keyword_fallback" and any("grievance@example.test" in h["content"] for h in res_kw["hits"]))

    # 5. a removed page (404): marked removed, history kept, no longer searchable
    s5 = await new_scan(org_a, dom, [{"url": PRIV, "category": "privacy_policy", "html": html_for(modified), "text": "x"}, {"url": COOKIE, "status": "failed", "http_status": 404, "error": "404"}])
    r5 = await sync.sync_scan(s5, org_a, embedder=emb)
    cookie_doc = (await q("select status from ent_policy_docs where org_id = :o and doc_type = 'cookie_policy'", o=org_a))[0]
    cookie_versions = (await q("select count(*) n from ent_policy_versions v join ent_policy_docs d on d.id = v.doc_id where d.org_id = :o and d.doc_type = 'cookie_policy'", o=org_a))[0]["n"]
    searchable = (await q("select count(*) n from ent_policy_chunks where org_id = :o and doc_type = 'cookie_policy' and is_active", o=org_a))[0]["n"]
    check("5 removed page: document marked removed, its versions kept, its chunks no longer searchable", r5.counts["removed"] == 1 and cookie_doc["status"] == "removed" and cookie_versions == 1 and searchable == 0)

    # 6. the same page at a new address: identity kept, history recorded, no new version
    NEWPRIV = f"https://{dom}/legal/privacy-notice"
    s6 = await new_scan(org_a, dom, [{"url": NEWPRIV, "category": "privacy_policy", "html": html_for(modified), "text": "x"}])
    r6 = await sync.sync_scan(s6, org_a, embedder=emb)
    pd = (await q("select identity_key, url_history from ent_policy_docs where id = :d", d=doc_id))[0]
    nv = (await q("select count(*) n from ent_policy_versions where doc_id = :d", d=doc_id))[0]["n"]
    check("6 changed URL: same document, address history kept, no new version", r6.counts["url_changed"] == 1 and nv == 2 and pd["identity_key"].endswith("/legal/privacy-notice") and len(pd["url_history"]) == 1)

    # 7. a very long policy: every paragraph is kept, in chunks, nothing truncated
    long_sections = [(f"Section {i}", " ".join(f"Sentence {i}-{j} about how we handle personal data in context {i}." for j in range(20))) for i in range(300)]
    LONG = f"https://{dom}/long-privacy"
    s7 = await new_scan(org_a, dom, [{"url": LONG, "category": "privacy_policy", "html": html_for(long_sections), "text": "x"}])
    await sync.sync_scan(s7, org_a, embedder=emb)
    lv = (await q("select v.id, v.text_chars, v.chunk_count from ent_policy_versions v join ent_policy_docs d on d.id = v.doc_id where d.org_id = :o and d.identity_key like '%long-privacy' ", o=org_a))[0]
    joined = "\n".join(r["content"] for r in await q("select content from ent_policy_chunks where version_id = :v order by chunk_index", v=lv["id"]))
    missing = [i for i, (h, p) in enumerate(long_sections) if p not in joined or h not in joined]
    check("7 very long policy: chunked, and every section and paragraph is present in the chunks", lv["chunk_count"] > 50 and not missing and lv["text_chars"] > 100_000, f"chunks={lv['chunk_count']} chars={lv['text_chars']} missing={len(missing)}")

    # 8. multilingual
    HI = f"https://{dom}/hi/privacy"
    hindi = [("हम कौन सी जानकारी एकत्र करते हैं", "हम आपका नाम, ईमेल पता और फोन नंबर एकत्र करते हैं।"), ("आपके अधिकार", "आप अपने व्यक्तिगत डेटा तक पहुँचने और उसे मिटाने का अनुरोध कर सकते हैं।")]
    s8 = await new_scan(org_a, dom, [{"url": HI, "category": "privacy_policy", "html": html_for(hindi), "text": "x"}])
    await sync.sync_scan(s8, org_a, embedder=emb)
    hv = (await q("select text from ent_policy_versions v join ent_policy_docs d on d.id = v.doc_id where d.org_id = :o and d.identity_key like '%/hi/privacy'", o=org_a))[0]["text"]
    hs = await retrieve.retrieve(org_a, dom, "व्यक्तिगत", query_embedder=emb, log=False)
    check("8 multilingual: the Hindi text is stored in full and found by keyword", all(h in hv and p in hv for h, p in hindi) and any("व्यक्तिगत" in h["content"] for h in hs["hits"]))

    # 9. failed extraction (page read, but no text): last verified version kept, document marked stale
    s9 = await new_scan(org_a, dom, [{"url": NEWPRIV, "category": "privacy_policy", "html": "<html><body></body></html>", "text": ""}])
    r9 = await sync.sync_scan(s9, org_a, embedder=emb)
    st = (await q("select stale, stale_reason, current_version_id from ent_policy_docs where id = :d", d=doc_id))[0]
    cur = (await q("select version_no from ent_policy_versions where doc_id = :d and is_current", d=doc_id))[0]["version_no"]
    check("9 failed extraction: counted, document stale, last verified version still current", r9.counts["extraction_failed"] == 1 and st["stale"] and cur == 2 and "last verified" in st["stale_reason"], str(st["stale_reason"]))
    stale_hit = await retrieve.retrieve(org_a, dom, "retain personal data", query_embedder=emb, log=False)
    check("14 stale evidence: retrieval marks results from a stale document as stale", any(h["stale"] for h in stale_hit["hits"] if h["doc_id"] == str(doc_id)) and all(h["stale"] for h in stale_hit["hits"] if h["doc_id"] == str(doc_id)))

    # 10 and 11. embedding or vector-store failure while updating: the last verified version is untouched
    changed_again = [(h, p + " Updated wording.") if h == "Your rights" else (h, p) for h, p in modified]
    s10 = await new_scan(org_a, dom, [{"url": NEWPRIV, "category": "privacy_policy", "html": html_for(changed_again), "text": "x"}])
    emb.fail = True
    r10 = await sync.sync_scan(s10, org_a, embedder=emb)
    emb.fail = False
    cur = (await q("select version_no, is_current from ent_policy_versions where doc_id = :d order by version_no", d=doc_id))
    failed_v = [v for v in cur if v["version_no"] == 3]
    active_now = (await q("select count(*) n from ent_policy_chunks c join ent_policy_versions v on v.id = c.version_id where c.doc_id = :d and c.is_active and v.version_no = 2", d=doc_id))[0]["n"]
    check("10 embedding failure: the new version is recorded as failed and never became current", r10.counts["sync_failed"] == 1 and failed_v and not failed_v[0]["is_current"])
    check("11 outage during update: version 2 is still current with all its chunks searchable", [v["is_current"] for v in cur if v["version_no"] == 2] == [True] and active_now > 0)
    check("10 failure is a processing failure, not a finding: nothing was written to assessments", (await q("select count(*) n from ent_assessments a join ent_scans s on s.id = a.scan_id where s.org_id = :o", o=org_a))[0]["n"] == 0)
    s10b = await new_scan(org_a, dom, [{"url": NEWPRIV, "category": "privacy_policy", "html": html_for(changed_again), "text": "x"}])
    r10b = await sync.sync_scan(s10b, org_a, embedder=emb)
    check("10 recovery: the next scan, once embedding works, creates the version and clears the stale flag", r10b.counts["changed"] == 1 and not (await q("select stale from ent_policy_docs where id = :d", d=doc_id))[0]["stale"])

    # website unavailable: nothing is overwritten
    s_down = await new_scan(org_a, dom, [{"url": f"https://{dom}/", "status": "failed", "http_status": 503, "error": "503"}])
    r_down = await sync.sync_scan(s_down, org_a, embedder=emb)
    active_docs = (await q("select count(*) filter (where stale) st, count(*) n from ent_policy_docs where org_id = :o and status = 'active'", o=org_a))[0]
    check("website unavailable: every active document marked stale, none changed or removed", active_docs["st"] == active_docs["n"] > 0 and r_down.counts["removed"] == 0 and r_down.counts["changed"] == 0)

    # 15. missing and inconsistent chunks are detected, and missing embeddings repaired
    issues0 = await history.verify_consistency(org_a, dom)
    some = (await q("select id from ent_policy_chunks where org_id = :o and is_active limit 2", o=org_a))
    async with async_session_factory() as db:
        await db.execute(text("update ent_policy_chunks set embedding = null where id = :i"), {"i": some[0]["id"]})
        await db.execute(text("delete from ent_policy_chunks where id = :i"), {"i": some[1]["id"]})
        await db.commit()
    issues1 = await history.verify_consistency(org_a, dom)
    kinds = {i["issue"] for i in issues1}
    check("15 consistency: a consistent base reports nothing", issues0 == [], str(issues0))
    check("15 consistency: a deleted chunk and a missing embedding are both detected", {"chunk_count_mismatch", "missing_embedding"} <= kinds, str(kinds))
    fixed = await history.repair_embeddings(org_a, dom, embedder=emb)
    check("15 repair: the missing embedding is regenerated", fixed["fixed"] >= 1 and not any(i["issue"] == "missing_embedding" for i in await history.verify_consistency(org_a, dom)), str(fixed))

    # 13. strict isolation between clients with identical content
    set_org_scope(org_b)
    sb = await new_scan(org_b, dom, [{"url": PRIV, "category": "privacy_policy", "html": html_for(modified), "text": "x"}])
    await sync.sync_scan(sb, org_b, embedder=emb)
    b_docs = (await q("select count(*) n from ent_policy_docs where org_id = :o", o=org_b))[0]["n"]
    a_hit = await retrieve.retrieve(org_a, dom, "grievance officer", query_embedder=emb, log=False)
    b_hit = await retrieve.retrieve(org_b, dom, "grievance officer", query_embedder=emb, log=False)
    foreign_for_a = (await q("select count(*) n from ent_policy_chunks where org_id = :b and id = any(:ids)", b=org_b, ids=[uuid.UUID(h["chunk_id"]) for h in a_hit["hits"]]))[0]["n"]
    foreign_for_b = (await q("select count(*) n from ent_policy_chunks where org_id = :a and id = any(:ids)", a=org_a, ids=[uuid.UUID(h["chunk_id"]) for h in b_hit["hits"]]))[0]["n"]
    check("13 isolation: each client has its own documents and chunks for identical content", b_docs == 1 and a_hit["hits"] and b_hit["hits"])
    check("13 isolation: a search never returns another client's chunk", foreign_for_a == 0 and foreign_for_b == 0)
    ma, mb = await metrics.policy_kb_metrics(org_a, dom), await metrics.policy_kb_metrics(org_b, dom)
    check("13 isolation: metrics are per client", mb["documents"]["active"] == 1 and ma["documents"]["active"] > 1)
    check("metrics come from stored data: embeddings generated and reused are both counted", ma["embeddings"]["generated"] > 0 and ma["embeddings"]["reused"] > 0 and ma["sync"]["unchanged"] >= 2 and ma["retrieval"]["queries"] >= 1)

    # 13b. cross-scan reuse of analysed passages: same text, prompt and model only
    repo = SqlRepo()
    set_org_scope(org_a)
    key1, key2 = ex.extractor_key("claude/model-1"), ex.extractor_key("claude/model-2")
    th = ex.text_hash(PRIV, "page_text", "some passage text")
    async with async_session_factory() as db:
        for sid in (s1, sb):
            pass
        await db.execute(text("insert into ent_analysis_chunks (scan_id, source_url, chunk_index, chunk_count, level, status, result, text_hash, extractor_key) values (:s, :u, 0, 1, 'page', 'done', cast(:r as jsonb), :h, :k)"),
                         {"s": s1, "u": PRIV, "r": json.dumps({"facts": [{"topic": "retention", "quote": "q"}]}), "h": th, "k": key1})
        await db.commit()
    hit_same = await repo.find_reusable_extraction(s2, th, key1)
    hit_other_model = await repo.find_reusable_extraction(s2, th, key2)
    hit_other_org = await repo.find_reusable_extraction(sb, th, key1)
    hit_other_text = await repo.find_reusable_extraction(s2, ex.text_hash(PRIV, "page_text", "different"), key1)
    check("reuse: the same text, prompt and model of the same client is reused", hit_same is not None and hit_same["facts"][0]["topic"] == "retention")
    check("reuse: a different model, prompt, text or client is NOT reused (a changed framework or model re-reads)", hit_other_model is None and hit_other_org is None and hit_other_text is None and key1 != key2)


async def embedded_and_triage_check() -> None:
    """A page holding several policies is stored as several documents; the model's reading of addresses changes crawl priority; nothing is dropped as a duplicate."""
    from app.enterprise.analysis import embedded as emb
    from app.enterprise.crawl import slug_triage
    org = uuid.uuid4()
    set_org_scope(org)
    dom = "kb-test.example.test"
    page = ("<html><body><main><h1>Legal</h1><h2>Privacy Policy</h2><p>" + "We collect your name and email address to answer enquiries. " * 20 +
            "</p><h2>Cookie Policy</h2><p>" + "We use analytics cookies that last two years. " * 20 + "</p><h2>Grievance Officer</h2><p>" + "Write to grievance@example.test. We reply within 15 days. " * 10 + "</p></main></body></html>")
    kb_emb = FakeEmbedder()
    # the same text served at two addresses is kept at both (no duplicate rule), and each is its own document
    s = await new_scan(org, dom, [{"url": f"https://{dom}/legal", "category": "privacy_policy", "also": ["cookie_policy", "grievance"], "html": page, "text": "x"},
                                  {"url": f"https://{dom}/cookie-policy", "category": "cookie_policy", "html": page, "text": "x"}])
    r = await sync.sync_scan(s, org, embedder=kb_emb)
    ids = {d["identity_key"].split(dom)[1]: d["doc_type"] for d in await q("select identity_key, doc_type from ent_policy_docs where org_id = :o", o=org)}
    check("embedded: one page holding privacy, cookie and grievance becomes three documents (plus the cookie page itself)", ids == {"/legal": "privacy_policy", "/legal#cookie_policy": "cookie_policy", "/legal#grievance": "grievance", "/cookie-policy": "cookie_policy"}, str(ids))
    ck = (await q("select v.text from ent_policy_versions v join ent_policy_docs d on d.id = v.doc_id where d.org_id = :o and d.identity_key like '%#cookie_policy'", o=org))[0]["text"]
    check("embedded: the cookie document holds the cookie section and nothing of the grievance section", "analytics cookies" in ck and "grievance@example.test" not in ck)
    check("embedded: nothing was dropped as a duplicate", r.counts["new"] == 4 and r.errors == [], str(r.counts))
    r2 = await sync.sync_scan(s, org, embedder=kb_emb)
    check("embedded: a rescan finds all four unchanged", r2.counts["unchanged"] == 4)
    async with async_session_factory() as db:
        await db.execute(text("update ent_policy_docs set doc_type = 'terms' where org_id = :o and identity_key like '%/cookie-policy'"), {"o": org})
        await db.commit()
    await sync.sync_scan(s, org, embedder=kb_emb)
    check("embedded: an unchanged document's type is corrected from the latest classification", (await q("select doc_type from ent_policy_docs where org_id = :o and identity_key like '%/cookie-policy'", o=org))[0]["doc_type"] == "cookie_policy")

    # the model's reading of addresses
    async with async_session_factory() as db:
        scan = await db.scalar(text("insert into ent_scans (org_id, website_url, domain, status) values (:o, :u, :d, 'running') returning id"), {"o": org, "u": f"https://{dom}/", "d": dom})
        for path in ("/p/12", "/blog/privacy-tips", "/products/widget", "/trust/dp"):
            await db.execute(text("insert into ent_frontier (scan_id, normalized_url, original_url, depth, discovery_source, status, priority) values (:s, :u, :u, 1, 'link', 'pending', 1)"), {"s": scan, "u": f"https://{dom}{path}"})
        await db.commit()
    asked: list[str] = []

    async def fake_llm(system, user, schema):
        asked.append(user)
        return {"items": [{"n": 1, "kinds": ["privacy_policy", "cookie_policy"], "confidence": "high"}, {"n": 3, "kinds": ["terms"], "confidence": "low"}]}

    out = await slug_triage.triage(scan, llm=fake_llm)
    rows = {r["original_url"].split(dom)[1]: r for r in await q("select original_url, priority, slug_kinds from ent_frontier where scan_id = :s", s=scan)}
    check("slugs: the addresses are sent to the model as numbered paths, blog paths are not", len(asked) == 1 and "1. /p/12" in asked[0] and "/blog/" not in asked[0], asked[0] if asked else "")
    check("slugs: an address the model flagged is read first and keeps both kinds it suggested", rows["/p/12"]["priority"] == 0 and rows["/p/12"]["slug_kinds"] == ["privacy_policy", "cookie_policy"])
    check("slugs: a low-confidence flag changes nothing", rows["/trust/dp"]["priority"] == 1 and rows["/trust/dp"]["slug_kinds"] == [] and out["flagged"] == 1)
    check("slugs: addresses already asked about are not asked again", (await slug_triage.triage(scan, llm=fake_llm))["asked"] == 0 and len(asked) == 1)

    async def broken_llm(system, user, schema):
        raise RuntimeError("no model")
    async with async_session_factory() as db:
        scan2 = await db.scalar(text("insert into ent_scans (org_id, website_url, domain, status) values (:o, :u, :d, 'running') returning id"), {"o": org, "u": f"https://{dom}/", "d": dom})
        await db.execute(text("insert into ent_frontier (scan_id, normalized_url, original_url, depth, discovery_source, status, priority) values (:s, :u, :u, 1, 'link', 'pending', 1)"), {"s": scan2, "u": f"https://{dom}/x"})
        await db.commit()
    res = await slug_triage.triage(scan2, llm=broken_llm)
    check("slugs: a model failure costs only the hint, never the crawl", res["flagged"] == 0 and (await q("select priority from ent_frontier where scan_id = :s", s=scan2))[0]["priority"] == 1)
    async with async_session_factory() as db:
        await db.execute(text("delete from ent_scans where org_id = :o"), {"o": org})
        await db.execute(text("delete from ent_policy_docs where org_id = :o"), {"o": org})
        await db.commit()


async def cleanup(orgs: list[uuid.UUID]) -> None:
    async with async_session_factory() as db:
        for o in orgs:
            await db.execute(text("delete from ent_policy_retrievals where org_id = :o"), {"o": o})
            await db.execute(text("delete from ent_policy_sync_log where org_id = :o"), {"o": o})
            await db.execute(text("delete from ent_policy_docs where org_id = :o"), {"o": o})
            await db.execute(text("delete from ent_scans where org_id = :o"), {"o": o})
        await db.commit()


async def main() -> int:
    orgs: list[uuid.UUID] = []
    # the organisations are created inside run(); collect them by scanning the scans table by domain afterwards
    try:
        await run()
        await embedded_and_triage_check()
    except Exception:
        traceback.print_exc()
        check("the run completed without an exception", False)
    finally:
        async with async_session_factory() as db:
            rows = (await db.execute(text("select distinct org_id from ent_scans where domain = 'kb-test.example.test'"))).all()
        await cleanup([r[0] for r in rows])
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
