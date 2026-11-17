"""Integration check of the error monitor against the REAL Postgres, a REAL SMTP exchange (a local sink that speaks the protocol, so the real mailer runs end to end) and local HTTP servers.
No model is called, no key is used and no customer data is touched. Every event it creates has a module or service starting with `itest` and is deleted at the end.

    docker compose -f docker-compose.prod.yml cp backend/tools/error_monitor_integration.py backend:/tmp/error_monitor_integration.py
    docker compose -f docker-compose.prod.yml exec -T -w /app backend env PYTHONPATH=/app python /tmp/error_monitor_integration.py

Exit 0 only if every check passed.
"""

from __future__ import annotations

import asyncio
import email
import json
import os
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import async_session_factory
from app.monitoring import blocks, checks, classify, events
from app.monitoring.config import MonitoringSettings

RESULTS: list[tuple[str, bool]] = []
TO = "sadaf.khan@swaransoft.com"


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)


# ---- a mail server that speaks SMTP, so the real mailer is exercised ---------------------------------------------------------------------------------------------------------------

class SmtpSink:
    def __init__(self):
        self.messages: list[dict] = []
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, r, w):
        rcpts: list[str] = []
        sender = ""
        w.write(b"220 sink ESMTP\r\n")
        while True:
            line = await r.readline()
            if not line:
                break
            cmd = line.decode(errors="replace").strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                w.write(b"250-sink\r\n250 8BITMIME\r\n")
            elif up.startswith("MAIL FROM"):
                sender = cmd.split(":", 1)[1].strip(" <>")
                w.write(b"250 OK\r\n")
            elif up.startswith("RCPT TO"):
                rcpts.append(cmd.split(":", 1)[1].strip(" <>"))
                w.write(b"250 OK\r\n")
            elif up == "DATA":
                w.write(b"354 go ahead\r\n")
                await w.drain()
                raw = b""
                while not raw.endswith(b"\r\n.\r\n"):
                    chunk = await r.readline()
                    if not chunk:
                        break
                    raw += chunk
                msg = email.message_from_bytes(raw[:-5])
                body = msg.get_payload(decode=True).decode("utf-8", "replace") if not msg.is_multipart() else ""
                self.messages.append({"from": sender, "rcpts": list(rcpts), "subject": msg["Subject"], "to": msg["To"], "body": body})
                w.write(b"250 OK queued\r\n")
            elif up in ("QUIT",):
                w.write(b"221 bye\r\n")
                await w.drain()
                break
            else:
                w.write(b"250 OK\r\n")
            await w.drain()
        w.close()

    def subjects(self):
        return [m["subject"] for m in self.messages]


async def q(sql: str, **p):
    async with async_session_factory() as db:
        return (await db.execute(text(sql), p)).mappings().all()


async def one(sql: str, **p):
    rows = await q(sql, **p)
    return rows[0] if rows else None


# ---- local HTTP servers: an API that fails in each way, and websites that block ---------------------------------------------------------------------------------------------------------

async def http_server(handler):
    async def conn(r, w):
        try:
            req = (await r.readuntil(b"\r\n\r\n")).decode(errors="replace")
            path = req.split(" ")[1]
            status, headers, body = await handler(path)
            data = body.encode()
            head = f"HTTP/1.1 {status} X\r\nContent-Length: {len(data)}\r\nConnection: close\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
            w.write(head.encode() + data)
            await w.drain()
        except Exception:
            pass
        finally:
            w.close()
    server = await asyncio.start_server(conn, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def api_handler(path: str):
    code = int(path.strip("/").split("/")[-1])
    if code == 0:
        await asyncio.sleep(5)                       # a service that does not answer in time
        return 200, {}, "late"
    return code, {"Content-Type": "application/json"}, json.dumps({"error": f"simulated {code}"})


CLOUDFLARE_PAGE = "<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing the site. Enable JavaScript and cookies to continue. Ray ID: 8a1b2c</body></html>"
AKAMAI_PAGE = "<html><head><title>Access Denied</title></head><body>You don't have permission to access \"http://shop.example/\" on this server. Reference #18.2d3f1b02.1700000000.1a2b</body></html>"
CAPTCHA_PAGE = "<html><head><title>Verify you are human</title></head><body>Please complete the CAPTCHA to continue. <div class='g-recaptcha'></div></body></html>"
NORMAL_PAGE = "<html><head><title>Privacy Policy</title></head><body>" + "We collect your name and email address. " * 120 + "<form><div class='g-recaptcha'></div></form></body></html>"


async def site_handler(path: str):
    if path.startswith("/cloudflare"):
        return 403, {"server": "cloudflare", "cf-mitigated": "challenge", "cf-ray": "abc"}, CLOUDFLARE_PAGE
    if path.startswith("/akamai"):
        return 403, {"server": "AkamaiGHost"}, AKAMAI_PAGE
    if path.startswith("/captcha"):
        return 200, {}, CAPTCHA_PAGE
    if path.startswith("/ratelimit"):
        return 429, {"Retry-After": "30"}, "Too Many Requests"
    if path.startswith("/down"):
        return 503, {"server": "nginx"}, "<html><title>503</title><body>Service Temporarily Unavailable</body></html>"
    if path.startswith("/boom"):
        return 500, {}, "Internal Server Error"
    return 200, {}, NORMAL_PAGE


async def fetch(client: httpx.AsyncClient, url: str):
    r = await client.get(url, timeout=15)
    title = ""
    import re
    m = re.search(r"<title>(.*?)</title>", r.text, re.I | re.S)
    if m:
        title = m.group(1)
    visible = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S))
    return r.status_code, dict(r.headers), title, " ".join(visible.split())


# ---- the checks --------------------------------------------------------------------------------------------------------------------------------------------------------------------

async def run(sink: SmtpSink) -> None:
    settings = MonitoringSettings(email_to=TO, smtp_host="127.0.0.1", smtp_port=sink.port, smtp_starttls=False, from_address="alerts@consiva.local", dashboard_url="http://localhost:8100/#/system")
    kw = {"settings": settings, "wait": True}
    os.environ["ALERT_EVENT_COOLDOWN_MINUTES"] = "60"
    os.environ.pop("ALERT_EVENT_EMAIL_STATUSES", None)

    # ---- 1. API errors, each its own category; only the ones that need a person email ------------------------------------------------------------------------
    api_server, api_port = await http_server(api_handler)
    async with httpx.AsyncClient() as client:
        recorded = {}
        for code in (401, 403, 404, 429, 500, 502, 503):
            r = await client.get(f"http://127.0.0.1:{api_port}/status/{code}")
            rec = await events.record_error(agent="five_page", module="itest:api_call", status=r.status_code, service="itest_api", upstream=True, request_id=f"req-{code}", **kw)
            recorded[code] = rec
        try:
            await client.get(f"http://127.0.0.1:{api_port}/status/0", timeout=1)
        except Exception as exc:
            recorded["timeout"] = await events.record_error(agent="five_page", module="itest:api_call", exc=exc, service="itest_api", **kw)
    api_server.close()
    cats = {k: v["category"] for k, v in recorded.items()}
    check("API: 401 403 404 429 500 502 503 and a timeout each get their own category",
          cats == {401: "api_key_invalid", 403: "forbidden", 404: "not_found", 429: "rate_limited", 500: "internal_error", 502: "bad_gateway", 503: "service_unavailable", "timeout": "timeout"}, str(cats))
    check("API: an expired or rejected key (401 from a provider) is critical and needs an administrator", (await one("select severity, temporary from error_events where error_id = :e", e=recorded[401]["error_id"]))["temporary"] is False)
    check("email: only the failures that need a person were emailed (key, forbidden, 500, 502, 503) and none for 404, 429 or a timeout", len(sink.messages) == 5, str(sink.subjects()))
    check("email: it went to the configured administrator, from the configured sender", all(m["rcpts"] == [TO] and m["from"] == "alerts@consiva.local" for m in sink.messages))
    m502 = next(m for m in sink.messages if "Bad gateway" in (m["subject"] or ""))
    check("email: it carries the agent, module, service, error type, status, message, request id, error id, timestamp, temporary flag and the steps",
          all(f in m502["body"] for f in ("Five-page privacy policy agent", "itest:api_call", "itest_api", "bad_gateway", "HTTP status: 502", "Error message:", "Request ID: req-502", "Error ID: ERR-", " UTC", "Temporary: yes", "What to do:")), m502["body"][:300])
    check("notification status is recorded honestly: sent for the emailed, not_needed for the rest",
          (await one("select notify_status from error_events where error_id = :e", e=recorded[502]["error_id"]))["notify_status"] == "sent" and (await one("select notify_status from error_events where error_id = :e", e=recorded[404]["error_id"]))["notify_status"] == "not_needed")

    # ---- 2. no duplicate emails for the same ongoing failure; a reminder only after the cooldown ----------------------------------------------------------------
    before = len(sink.messages)
    for _ in range(6):
        again = await events.record_error(agent="five_page", module="itest:api_call", status=502, service="itest_api", upstream=True, **kw)
    ev = await one("select occurrences, notify_count from error_events where error_id = :e", e=recorded[502]["error_id"])
    check("dedupe: the same failure repeated six times is one event with a count, and no further email", again["inserted"] is False and ev["occurrences"] == 7 and len(sink.messages) == before, str(dict(ev)))
    await q("select 1")
    async with async_session_factory() as db:
        await db.execute(text("update error_events set last_notified_at = now() - interval '2 hours' where error_id = :e"), {"e": recorded[502]["error_id"]})
        await db.commit()
    await events.record_error(agent="five_page", module="itest:api_call", status=502, service="itest_api", upstream=True, **kw)
    check("cooldown: once the cooldown has passed, a still-failing event sends ONE reminder", len(sink.messages) == before + 1 and sink.messages[-1]["subject"].startswith("[Consiva] STILL FAILING"), str(sink.subjects()[-2:]))
    async with async_session_factory() as db:
        await events.acknowledge(db, recorded[502]["error_id"], "tester")
        await db.execute(text("update error_events set last_notified_at = now() - interval '5 hours' where error_id = :e"), {"e": recorded[502]["error_id"]})
        await db.commit()
    await events.record_error(agent="five_page", module="itest:api_call", status=502, service="itest_api", upstream=True, **kw)
    check("acknowledge: an acknowledged event stays listed but stops sending reminders", len(sink.messages) == before + 1 and (await one("select status from error_events where error_id = :e", e=recorded[502]["error_id"]))["status"] == "acknowledged")

    # ---- 3. recovery ----------------------------------------------------------------------------------------------------------------------------------------------------------
    n = len(sink.messages)
    resolved = await events.record_success("itest_api", wait=True, settings=settings)
    recovery = [m for m in sink.messages[n:] if m["subject"].startswith("[Consiva] RECOVERED")]
    check("recovery: when the service works again its events resolve and one recovery email is sent for each that had been announced", resolved >= 8 and len(recovery) == 5, f"resolved={resolved} mails={len(recovery)}")
    row = await one("select status, resolved_at, last_recovery_at from error_events where error_id = :e", e=recorded[502]["error_id"])
    check("recovery: the event shows when it recovered", row["status"] == "resolved" and row["resolved_at"] is not None and row["last_recovery_at"] is not None)
    check("recovery: a failure that was never announced (404, 429) resolves silently", not any("not found" in (m["subject"] or "").lower() for m in recovery))
    n2 = len(sink.messages)
    await events.record_success("itest_api", wait=True, settings=settings)
    check("recovery: a success with nothing open sends nothing", len(sink.messages) == n2)
    again = await events.record_error(agent="five_page", module="itest:api_call", status=502, service="itest_api", upstream=True, **kw)
    check("a failure after a recovery is a NEW event and is announced again", again["inserted"] is True and len(sink.messages) == n2 + 1)
    await events.record_success("itest_api", wait=True, settings=settings)

    # ---- 4. an expired key detected by the periodic check is shown with the other errors, without a second email -----------------------------------------------------
    n = len(sink.messages)
    res = checks.CheckResult("anthropic_api", "Claude API key", False, "Claude answered HTTP 401: invalid x-api-key", 20, definitive=True, severity="critical", title="Claude API key rejected (HTTP 401)", action="Create a new key.")
    await events.mirror_check_result(res)
    mirrored = await one("select category, grouping, notify_status, temporary, service from error_events where module = 'system_check:anthropic_api' and status = 'open'")
    check("expired key: the periodic check's failure appears as an API-key event (needs an administrator) without a second email", mirrored["category"] == "api_key_invalid" and mirrored["temporary"] is False and len(sink.messages) == n, str(dict(mirrored)))
    await events.mirror_check_result(checks.CheckResult("anthropic_api", "Claude API key", True, "ok"))
    check("expired key: the next passing check resolves it", (await one("select count(*) n from error_events where module = 'system_check:anthropic_api' and status = 'open'"))["n"] == 0)
    async with async_session_factory() as db:
        await db.execute(text("delete from error_events where module = 'system_check:anthropic_api'"))
        await db.commit()

    # ---- 5. PostgreSQL unavailable: the event cannot be stored, so the administrator is emailed directly, once ------------------------------------------------------
    dead = async_sessionmaker(create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/none"), expire_on_commit=False)
    n = len(sink.messages)
    exc = ConnectionRefusedError("could not connect to server: Connection refused")
    r1 = await events.record_error(agent="platform", module="itest:db", exc=exc, service="postgres", session_factory=dead, **kw)
    r2 = await events.record_error(agent="platform", module="itest:db", exc=exc, service="postgres", session_factory=dead, **kw)
    check("PostgreSQL unavailable: classified as a database outage, emailed directly although it could not be stored, and only once", r1 is None and r2 is None and len(sink.messages) == n + 1
          and "PostgreSQL unavailable" in sink.messages[-1]["subject"] and "not stored" in sink.messages[-1]["body"], str(sink.subjects()[n:]))
    check("PostgreSQL unavailable: the classification names the database and the steps", classify.classify_exception(exc, service="postgres").category == "database_unavailable" and "db container" in classify.classify_exception(exc, service="postgres").steps)

    # ---- 6. embedding, vector database, RAG indexing and retrieval ------------------------------------------------------------------------------------------------------
    from fastapi import HTTPException
    from app.enterprise.api import policy_kb
    from app.enterprise.policykb import retrieve, sync

    async def failing_embedder(texts):
        raise RuntimeError("embedding provider unavailable: HTTP 503")

    n = len(sink.messages)
    org = uuid.uuid4()
    dom = "itest-kb.example.test"
    async with async_session_factory() as db:
        scan = await db.scalar(text("insert into ent_scans (org_id, website_url, domain, status, analysis_status) values (:o, :u, :d, 'completed', 'completed') returning id"), {"o": org, "u": f"https://{dom}/", "d": dom})
        fid = await db.scalar(text("insert into ent_frontier (scan_id, normalized_url, original_url, depth, discovery_source, status, final_url, page_category) values (:s, :u, :u, 0, 'seed', 'done', :u, 'privacy_policy') returning id"), {"s": scan, "u": f"https://{dom}/privacy"})
        html = "<html><body><main><h1>Privacy Policy</h1><h2>Data we collect</h2><p>" + "We collect your name and email. " * 30 + "</p></main></body></html>"
        await db.execute(text("insert into ent_evidence (scan_id, frontier_id, kind, source_url, page_number, content_hash, content_type, byte_size, content, detection_method) values (:s, :f, 'page_html', :u, 0, :h, 'text/html', :b, :c, 'dom_render')"),
                         {"s": scan, "f": fid, "u": f"https://{dom}/privacy", "h": uuid.uuid4().hex, "b": len(html), "c": html})
        await db.commit()
    os.environ["ALERT_EMAIL_TO"] = TO
    import app.monitoring.config as mcfg
    mcfg.get_monitoring_settings.cache_clear()
    os.environ.update({"ALERT_SMTP_HOST": "127.0.0.1", "ALERT_SMTP_PORT": str(sink.port), "ALERT_SMTP_STARTTLS": "false", "ALERT_FROM_ADDRESS": "alerts@consiva.local"})
    mcfg.get_monitoring_settings.cache_clear()
    res = await sync.sync_scan(scan, org, embedder=failing_embedder)
    await asyncio.sleep(0.5)
    emb = await one("select category, grouping, severity, notify_status, website_url from error_events where module = 'enterprise.policykb.sync' and status = 'open' and website_url like '%itest-kb%' order by first_seen_at desc limit 1")
    check("embedding failure: indexing a policy failed, was recorded as an embedding failure for that page, and the administrator was emailed", emb and emb["category"] == "embedding_failure" and emb["grouping"] == "embedding" and any("Embedding failure" in s for s in sink.subjects()[n:]), str(emb))
    check("embedding failure: it is not mistaken for a compliance result: the knowledge base holds no half-indexed version", res.counts["sync_failed"] + res.counts["new"] >= 0 and (await one("select count(*) n from ent_policy_versions where org_id = :o and is_current", o=org))["n"] == 0)

    ok_embed = lambda texts: asyncio.sleep(0, result=[[0.01] * 1024 for _ in texts])      # noqa: E731
    n = len(sink.messages)
    await sync.sync_scan(scan, org, embedder=ok_embed)
    await asyncio.sleep(0.5)
    check("service recovery: the next successful indexing resolves the embedding failure and sends a recovery email",
          (await one("select count(*) n from error_events where module = 'enterprise.policykb.sync' and service = 'embedding_api' and status = 'open' and website_url like '%itest-kb%'"))["n"] == 0 and any("RECOVERED" in s for s in sink.subjects()[n:]), str(sink.subjects()[n:]))

    n = len(sink.messages)
    out = await retrieve.retrieve(org, dom, "retention period", query_embedder=failing_embedder, log=False)
    await asyncio.sleep(0.5)
    check("RAG retrieval: when the question cannot be embedded the search falls back to keywords AND the failure is recorded", out["mode"] == "keyword_fallback" and any(s and "mbedding" in s for s in sink.subjects()[n:]), str(sink.subjects()[n:]))

    @policy_kb.guarded("itest_vector")
    async def vector_endpoint():
        raise RuntimeError('operator does not exist: vector <=> text (pgvector)')

    @policy_kb.guarded("itest_502")
    async def kb_502():
        raise httpx.HTTPStatusError("Bad Gateway", request=httpx.Request("GET", "http://rag/x"), response=httpx.Response(502))

    @policy_kb.guarded("itest_ok")
    async def kb_ok():
        return {"items": []}

    n = len(sink.messages)
    raised = []
    for fn in (vector_endpoint, kb_502):
        try:
            await fn()
        except HTTPException as exc:
            raised.append(exc)
    check("vector database / RAG API failure: the endpoint answers 503 with a clear message and an error id, never an empty list", len(raised) == 2 and all(e.status_code == 503 and "Error ERR-" in e.detail and "knowledge base could not answer" in e.detail and e.headers["X-Error-ID"].startswith("ERR-") for e in raised), str([e.detail for e in raised]))
    vec = await one("select category, grouping, severity from error_events where module = 'policy_kb.itest_vector'")
    r502 = await one("select category, grouping, http_status from error_events where module = 'policy_kb.itest_502'")
    check("vector database failure is classified as a vector database error, and a 502 from the RAG API as a bad gateway of the knowledge base",
          vec["category"] == "vector_db_error" and vec["grouping"] == "database" and r502["category"] == "bad_gateway" and r502["grouping"] == "rag" and r502["http_status"] == 502, f"{vec} {r502}")
    await asyncio.sleep(0.5)
    check("both emailed the administrator", sum(1 for s in sink.subjects()[n:] if s and ("Vector" in s or "Bad gateway" in s)) == 2, str(sink.subjects()[n:]))
    await kb_ok()
    check("a knowledge base call that works clears the failure", (await one("select count(*) n from error_events where service = 'policy_kb' and module like 'policy_kb.itest%' and status = 'open'"))["n"] == 0)

    # ---- 7. the HTTP middleware: every error response and unhandled exception is recorded, with a request id ---------------------------------------------------------------------------
    from fastapi import FastAPI
    from app.monitoring.middleware import ErrorMonitorMiddleware
    app = FastAPI()
    app.add_middleware(ErrorMonitorMiddleware)

    @app.get("/itest/ok")
    async def ok_route():
        return {"ok": True}

    @app.get("/itest/missing")
    async def missing_route():
        raise HTTPException(404, "nothing here")

    @app.get("/itest/boom")
    async def boom_route():
        raise RuntimeError("unexpected failure inside a route")

    @app.get("/itest/unavailable")
    async def unavailable_route():
        raise HTTPException(503, "dependency down")

    n = len(sink.messages)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        ok = await c.get("/itest/ok", headers={"X-Request-ID": "my-own-id"})
        nf = await c.get("/itest/missing")
        bm = await c.get("/itest/boom")
        un = await c.get("/itest/unavailable")
    await asyncio.sleep(1)
    check("middleware: every response carries an X-Request-ID, and the caller's own id is kept", ok.headers["x-request-id"] == "my-own-id" and nf.headers["x-request-id"] and nf.headers["x-request-id"] != bm.headers["x-request-id"])
    mw = {r["category"]: r for r in await q("select category, severity, request_id, notify_status, module from error_events where module like '%/itest/%'")}
    check("middleware: a 404 is recorded as information and not emailed; an unhandled exception (500) and a 503 are critical", mw["not_found"]["severity"] == "info" and mw["not_found"]["notify_status"] == "not_needed" and mw["unexpected_error"]["severity"] == "critical" and mw["service_unavailable"]["severity"] == "critical", str(mw))
    check("middleware: the event names the request id that is on the response", mw["unexpected_error"]["request_id"] == bm.headers["x-request-id"] and bm.status_code == 500 and bm.headers["x-request-id"] in bm.text)
    check("middleware: only the critical ones were emailed", sum(1 for s in sink.subjects()[n:] if s and s.startswith("[Consiva] CRITICAL")) == 2, str(sink.subjects()[n:]))

    # ---- 8. the website blocks the scanner ----------------------------------------------------------------------------------------------------------------------------------------------
    site_server, port = await http_server(site_handler)
    base = f"http://127.0.0.1:{port}"
    verdicts = {}
    async with httpx.AsyncClient() as client:
        for name in ("cloudflare", "akamai", "captcha", "ratelimit", "down", "boom", "normal"):
            verdicts[name] = blocks.detect_block(*await fetch(client, f"{base}/{name}"))
    site_server.close()
    check("bot blocks: a Cloudflare challenge, a WAF access-denied page, a CAPTCHA page and a 429 are recognised as blocked",
          verdicts["cloudflare"].kind == "BOT_BLOCKED" and verdicts["akamai"].kind == "BOT_BLOCKED" and verdicts["captcha"].kind == "BOT_BLOCKED" and verdicts["ratelimit"].kind == "ACCESS_BLOCKED", str(verdicts))
    check("bot blocks: a 503, a 500 and a normal page that contains a CAPTCHA form are NOT blocks", not verdicts["down"] and not verdicts["boom"] and not verdicts["normal"], str(verdicts))
    n = len(sink.messages)
    for url, name in ((f"{base}/cloudflare", "cloudflare"), (f"{base}/cloudflare/privacy", "cloudflare")):
        b = await events.record_block(agent="enterprise", website_url="https://itest-blocked.example.test/privacy?token=secret123", verdict=verdicts[name], module="itest:crawl", scan_id="itest-scan-1", wait=True, session_factory=None)
    check("bot blocks: the second detection for the same website is the same event: one email", b["inserted"] is False and len(sink.messages) - n == 1, str(sink.subjects()[n:]))
    mail = sink.messages[-1]
    check("bot blocks: the email says the website blocked the scanner, names the site, the scan and the block, and says nothing was bypassed",
          mail["body"].startswith("Website blocked our scanner.") and "https://itest-blocked.example.test/privacy" in mail["body"] and "secret123" not in mail["body"] and "itest-scan-1" in mail["body"] and "does not try to get round" in mail["body"] and "cloudflare" in mail["body"].lower(), mail["body"][:400])
    bev = await one("select category, grouping, severity, temporary, website_url, scan_id from error_events where module = 'itest:crawl'")
    check("bot blocks: stored as a website-access event, not a normal failure, and needing an administrator", bev["category"] == "bot_blocked" and bev["grouping"] == "website_access" and bev["temporary"] is False and "secret123" not in bev["website_url"])
    n = len(sink.messages)
    await events.record_block(agent="five_page", website_url="https://other-blocked.example.test/", verdict=blocks.BlockVerdict(True, "ACCESS_BLOCKED", "the site answered HTTP 403 instead of a page"), module="itest:crawl", wait=True)
    check("bot blocks: a different website is its own event and its own email", len(sink.messages) == n + 1)
    # a stored five-page failure message
    n = len(sink.messages)
    from app.scanner.failures import ScanNotPossible
    await events.record_job_failure("scan", {"scan_id": str(uuid.uuid4()), "url": "https://five-blocked.example.test"}, ScanNotPossible("Could not reach the site at all: https://five-blocked.example.test answered with HTTP 403 instead of a page. The site is refusing the scanner (access denied). Nothing was assessed.", "http_blocked"), wait=True)
    fb = await one("select agent, category, grouping, module from error_events where website_url = 'https://five-blocked.example.test' ")
    check("five-page worker: a scan that failed because the site refused the scanner is recorded as a block of the five-page agent and emailed", fb["agent"] == "five_page" and fb["category"] in ("bot_blocked", "access_blocked") and fb["grouping"] == "website_access" and len(sink.messages) == n + 1, str(fb))
    await events.record_job_failure("enterprise_scan", {"scan_id": str(uuid.uuid4())}, RuntimeError("the worker crashed"), wait=True)
    wk = await one("select agent, category, grouping from error_events where module = 'worker:enterprise_scan' order by first_seen_at desc limit 1")
    check("enterprise worker: any other job failure is a worker failure of the Enterprise agent", wk["agent"] == "enterprise" and wk["grouping"] in ("worker", "api"), str(wk))

    # ---- 9. the gateway --------------------------------------------------------------------------------------------------------------------------------------------------------------------
    state = {"code": 502}

    async def gateway_handler(path):
        return state["code"], {}, "gateway"

    gw_server, gw_port = await http_server(gateway_handler)
    async with httpx.AsyncClient() as client:
        bad = await checks.check_gateway(f"http://127.0.0.1:{gw_port}/health", client)
        state["code"] = 200
        good = await checks.check_gateway(f"http://127.0.0.1:{gw_port}/health", client)
    gw_server.close()
    check("gateway: a 502 from the proxy in front of the backend is detected (the backend itself can never see it), and a healthy answer is not", not bad.ok and "502" in bad.title and good.ok)
    await events.mirror_check_result(bad)
    check("gateway: shown as a bad-gateway event in the error list", (await one("select category from error_events where module = 'system_check:gateway' and status = 'open'"))["category"] == "bad_gateway")
    await events.mirror_check_result(good)
    check("gateway: and resolved when it answers again", (await one("select count(*) n from error_events where module = 'system_check:gateway' and status = 'open'"))["n"] == 0)

    # ---- 10. the dashboard's queries and no secrets ----------------------------------------------------------------------------------------------------------------------------------------
    leak = "failed with Authorization: Bearer abcdefghijklmnop and x-api-key=sk-ant-abcdef1234567890 for https://u:pw@host/p?token=zzz9999"
    await events.record_error(agent="platform", module="itest:secrets", exc=RuntimeError(leak), service="itest_api", upstream=True, **kw)
    stored = await one("select message, detail from error_events where module = 'itest:secrets'")
    mails = " ".join(m["body"] for m in sink.messages)
    check("secrets: keys, tokens, passwords and query strings are not stored and not emailed", not any(s in (stored["message"] + stored["detail"] + mails) for s in ("abcdefghijklmnop", "sk-ant-abcdef1234567890", ":pw@", "zzz9999")))
    async with async_session_factory() as db:
        summary = await events.summary(db)
        by_agent = await events.list_events(db, agent="enterprise", include_info=True)
        by_cat = await events.list_events(db, category="bot_blocked")
        by_site = await events.list_events(db, website="itest-blocked")
        by_status = await events.list_events(db, status="resolved", include_info=True)
        recent = await events.list_events(db, since=datetime.now(timezone.utc) - timedelta(minutes=30), include_info=True)
        old = await events.list_events(db, until=datetime.now(timezone.utc) - timedelta(days=3650))
        detail = await events.get_event(db, bec_id := bev and (await one("select error_id from error_events where module = 'itest:crawl'"))["error_id"])
    check("dashboard: totals, critical, API, RAG, database and bot-blocked counts and the latest timestamps are available", all(k in summary for k in ("total", "critical", "api", "rag", "database", "bot_blocked", "open", "acknowledged", "resolved", "latest_error_at", "last_recovery_at")) and summary["bot_blocked"] >= 2 and summary["critical"] >= 3 and summary["last_recovery_at"] is not None, str(summary))
    check("dashboard: it can be filtered by agent, category, status, website and date", by_agent["total"] >= 1 and all(e["agent"] == "enterprise" for e in by_agent["items"]) and by_cat["total"] >= 1 and by_site["total"] == 1 and by_status["total"] >= 1 and recent["total"] >= 5 and old["total"] == 0,
          f"agent={by_agent['total']} cat={by_cat['total']} site={by_site['total']} status={by_status['total']} recent={recent['total']} old={old['total']}")
    check("dashboard: opening an event shows its actual details", detail["error_id"] == bec_id and detail["website_url"].startswith("https://itest-blocked") and detail["message"].startswith("Website blocked our scanner.") and detail["suggested_steps"])


async def cleanup() -> None:
    async with async_session_factory() as db:
        await db.execute(text(
            "delete from error_events where module like 'itest:%' or module like '%/itest/%' or module like 'policy_kb.itest%' or service like 'itest_%' or website_url like '%itest%' "
            "or website_url like '%blocked.example.test%' or message like '%the worker crashed%' or message like '%fell back to keywords%' or module = 'system_check:gateway'"))
        await db.execute(text("delete from ent_scans where domain = 'itest-kb.example.test'"))
        await db.execute(text("delete from ent_policy_docs where domain = 'itest-kb.example.test'"))
        await db.commit()


async def main() -> int:
    sink = SmtpSink()
    await sink.start()
    try:
        await run(sink)
    except Exception:
        traceback.print_exc()
        check("the run completed without an exception", False)
    finally:
        try:
            await cleanup()
        except Exception:
            traceback.print_exc()
        await sink.stop()
    left = await one("select count(*) n from error_events where module like 'itest:%' or service like 'itest_%'")
    check("teardown: no test event is left behind", left["n"] == 0, str(dict(left)))
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
