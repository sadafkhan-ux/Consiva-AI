"""Error events: record every important failure of both agents in one place, email an administrator once, and say when it recovers.

  record_error(...)      a failure happened. Same problem again = the same event with a growing count (no new row, no new email until the cooldown passes). Never raises.
  record_success(...)    a call to the service worked again: its open events are resolved and ONE "recovered" email is sent for each that had been announced.
  record_block(...)      the website blocked the scanner (BOT_BLOCKED / ACCESS_BLOCKED): its own event, emailed at once.

Email goes through the existing alert mailer (app.monitoring.mailer: the same SMTP settings and the same "sent only when the server accepted it" honesty). Nothing that looks like a key, token or password is
stored or sent (app.monitoring.scrub), and no request payload is ever recorded: only the status, a scrubbed one-line message and where it happened.
If the database itself is the problem, the event cannot be stored: the administrator is emailed directly (once per cooldown, from memory) and the failure is logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text

from app.monitoring import blocks, classify, mailer
from app.monitoring.config import MonitoringSettings, get_monitoring_settings
from app.monitoring.context import request_id_var
from app.monitoring.scrub import scrub

logger = logging.getLogger(__name__)

NEW, REMINDER, RECOVERED = "new", "reminder", "recovered"
AGENTS = ("enterprise", "five_page", "platform")
#: A still-failing event that is open and announced reminds again only after this many minutes (ALERT_EVENT_COOLDOWN_MINUTES).
DEFAULT_COOLDOWN_MINUTES = 60
_FALLBACK_SENT: dict[str, float] = {}
_LIVE_CACHE: dict[str, tuple[float, bool]] = {}
_LIVE_TTL = 20.0
_background: set[asyncio.Task] = set()

COLUMNS = ("id, error_id, fingerprint, agent, module, service, grouping, category, severity, http_status, message, detail, scan_id, website_url, request_id, temporary, suggested_steps, status, "
           "occurrences, first_seen_at, last_seen_at, resolved_at, last_recovery_at, acknowledged_by, acknowledged_at, notify_status, notify_count, last_notified_at")


def _cooldown_minutes() -> int:
    import os
    try:
        return max(1, int(os.environ.get("ALERT_EVENT_COOLDOWN_MINUTES", DEFAULT_COOLDOWN_MINUTES)))
    except ValueError:
        return DEFAULT_COOLDOWN_MINUTES


def _extra_email_statuses() -> tuple[int, ...]:
    import os
    return tuple(int(s) for s in re.findall(r"\d{3}", os.environ.get("ALERT_EVENT_EMAIL_STATUSES", "")))


def clean_url(url: str | None) -> str | None:
    """The address without its query string, fragment or credentials: a query can carry a token."""
    if not url:
        return None
    try:
        u = urlparse(str(url).strip())
        if not u.scheme or not u.hostname:
            return scrub(url, 300)
        port = f":{u.port}" if u.port else ""
        return f"{u.scheme}://{u.hostname}{port}{u.path or ''}"[:300]
    except Exception:
        return scrub(url, 300)


def error_id() -> str:
    return f"ERR-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def fingerprint(agent: str, module: str, service: str | None, category: str, status: int | None, website_url: str | None = None) -> str:
    host = (urlparse(website_url).hostname or "") if website_url and category in ("bot_blocked", "access_blocked") else ""
    raw = "|".join((agent, module, service or "", category, str(status or ""), host))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


# ---- the email ---------------------------------------------------------------------------------------------------------------------------------------------------------

def compose(kind: str, ev: dict, settings: MonitoringSettings) -> tuple[str, str]:
    """(subject, body). Plain text, one fact per line. It says what failed, where, when, whether it can clear by itself, and what to do."""
    agent = {"enterprise": "Enterprise agent", "five_page": "Five-page privacy policy agent", "platform": "Platform"}.get(ev["agent"], ev["agent"])
    title = ev.get("title") or ev["category"].replace("_", " ")
    tag = {NEW: ev["severity"].upper(), REMINDER: "STILL FAILING", RECOVERED: "RECOVERED"}[kind]
    subject = f"[Consiva] {tag}: {title} ({agent})"
    when = lambda d: f"{d:%d %b %Y %H:%M:%S} UTC" if d else "-"            # noqa: E731
    lines = []
    if kind == RECOVERED:
        lines += [f"{title} has recovered.", "", f"Error ID: {ev['error_id']}", f"Service: {ev.get('service') or '-'}", f"Application / agent: {agent}", f"First seen: {when(ev['first_seen_at'])}",
                  f"Recovered: {when(ev.get('resolved_at'))}", f"Times it occurred: {ev['occurrences']}"]
    else:
        blocked = ev["category"] in ("bot_blocked", "access_blocked")
        if blocked:
            lines += [blocks.USER_MESSAGE, ""]
        lines += [
            f"Application / agent: {agent}",
            f"Affected module: {ev['module']}",
            f"API / service: {ev.get('service') or '-'}",
            f"Error type: {ev['category']}",
            f"HTTP status: {ev['http_status'] if ev.get('http_status') else '-'}",
            f"Error message: {ev['message']}",
            f"Website URL: {ev.get('website_url') or '-'}",
            f"Scan ID: {ev.get('scan_id') or '-'}",
            f"Request ID: {ev.get('request_id') or '-'}",
            f"Time: {when(ev['last_seen_at'] if kind == REMINDER else ev['first_seen_at'])}",
            f"Error ID: {ev['error_id']}",
            f"Seen {ev['occurrences']} time(s) since {when(ev['first_seen_at'])}.",
            "",
            "Temporary: " + ("yes, it may clear by itself" if ev.get("temporary") else "NO, an administrator has to act"),
            "",
            f"What to do: {ev.get('suggested_steps') or 'Open the dashboard for the details.'}",
        ]
        if blocked:
            lines += ["", blocks.EXPLANATION]
    lines += ["", f"Dashboard: {settings.dashboard_url.rsplit('#', 1)[0]}#/dpdp", "", "This message was sent automatically by the Consiva error monitor. It does not contain keys, tokens or request data."]
    return subject, "\n".join(lines)


async def _email(settings: MonitoringSettings, kind: str, ev: dict) -> tuple[str, str | None]:
    subject, body = compose(kind, ev, settings)
    return await mailer.send(settings, subject, body)


async def _mark(session_factory, ev_id, status: str, reason: str | None, counted: bool) -> None:
    note = status if not reason else f"{status}: {scrub(reason, 160)}"
    try:
        async with session_factory() as db:
            await db.execute(text(
                "update error_events set notify_status = :s, last_notified_at = now(), notify_count = notify_count + :n where id = :i"), {"s": note, "n": 1 if (counted and status == mailer.SENT) else 0, "i": ev_id})
            await db.commit()
    except Exception:
        logger.exception("error monitor: could not record the outcome of an email")


async def _notify(session_factory, settings: MonitoringSettings, kind: str, ev: dict) -> None:
    try:
        status, reason = await _email(settings, kind, ev)
        if status == mailer.FAILED:
            logger.warning("error monitor: email for %s failed: %s", ev["error_id"], reason)
        await _mark(session_factory, ev["id"], status, reason, True)
    except Exception:
        logger.exception("error monitor: sending an email failed")


def _dispatch(coro, wait: bool):
    """Run `coro` now (wait) or in the background, so a request is never held up by a slow mail server."""
    if wait:
        return coro
    try:
        task = asyncio.get_running_loop().create_task(coro)
        _background.add(task)
        task.add_done_callback(_background.discard)
    except RuntimeError:
        coro.close()
    return None


# ---- recording --------------------------------------------------------------------------------------------------------------------------------------------------------

_UPSERT = text("""
    insert into error_events (error_id, fingerprint, agent, module, service, grouping, category, severity, http_status, message, detail, scan_id, website_url, request_id, temporary, suggested_steps)
    values (:eid, :fp, :agent, :module, :service, :grouping, :category, :severity, :status, :message, :detail, :scan_id, :url, :rid, :temporary, :steps)
    on conflict (fingerprint) where status in ('open', 'acknowledged') do update set
        occurrences = error_events.occurrences + 1, last_seen_at = now(), message = excluded.message, detail = excluded.detail,
        scan_id = coalesce(excluded.scan_id, error_events.scan_id), request_id = coalesce(excluded.request_id, error_events.request_id),
        severity = case when excluded.severity = 'critical' then 'critical' else error_events.severity end
    returning """ + COLUMNS + ", (xmax = 0) as inserted")


async def record_error(*, agent: str, module: str, exc: BaseException | None = None, status: int | None = None, service: str | None = None, message: str | None = None,
                       scan_id: str | uuid.UUID | None = None, website_url: str | None = None, request_id: str | None = None, classified: classify.Classified | None = None,
                       upstream: bool = True, email: bool = True, wait: bool = False, session_factory=None, settings: MonitoringSettings | None = None) -> dict | None:
    """Record one failure. Never raises: monitoring must not make a failure worse."""
    try:
        return await _record(agent, module, exc, status, service, message, scan_id, website_url, request_id, classified, upstream, email, wait, session_factory, settings)
    except Exception:
        logger.exception("error monitor: could not record an error")
        return None


async def _record(agent, module, exc, status, service, message, scan_id, website_url, request_id, classified, upstream, email, wait, session_factory, settings) -> dict | None:
    if session_factory is None:
        from app.db.session import async_session_factory as session_factory
    settings = settings or get_monitoring_settings()
    if agent not in AGENTS:
        agent = "platform"
    if classified is None:
        classified = classify.classify_exception(exc, service=service, module=module, upstream=upstream) if exc is not None else classify.classify_status(status or 500, service=service, upstream=upstream)
        if exc is not None and status is None:
            status = classify.status_of(exc)
    rid = request_id or request_id_var.get() or None
    msg = scrub(message or (f"{type(exc).__name__}: {exc}" if exc is not None else f"HTTP {status}"), 500)
    detail = scrub(f"{type(exc).__name__}: {exc}", 1500) if exc is not None else None
    url = clean_url(website_url)
    fp = fingerprint(agent, module, service, classified.category, status, url)
    params = {"eid": error_id(), "fp": fp, "agent": agent, "module": scrub(module, 120), "service": service, "grouping": classified.grouping, "category": classified.category, "severity": classified.severity,
              "status": status, "message": msg, "detail": detail, "scan_id": str(scan_id) if scan_id else None, "url": url, "rid": scrub(rid, 80) if rid else None,
              "temporary": classified.temporary, "steps": classified.steps}
    want_email = email and classify.should_email(classified, extra_statuses=_extra_email_statuses(), status=status)
    try:
        async with session_factory() as db:
            row = (await db.execute(_UPSERT, params)).mappings().one()
            await db.commit()
    except Exception as db_exc:                                         # the database cannot take the event: tell the administrator directly
        logger.exception("error monitor: the event could not be stored (%s)", type(db_exc).__name__)
        await _fallback_email(settings, params, classified, want_email)
        return None
    ev = dict(row)
    ev["title"] = classified.title
    inserted = bool(ev.pop("inserted"))
    now = datetime.now(timezone.utc)
    due = False
    if want_email and ev["status"] == "open":
        last = ev.get("last_notified_at")
        due = last is None or (now - last).total_seconds() >= _cooldown_minutes() * 60
        if inserted or due:
            kind = NEW if inserted else REMINDER
            if wait:
                await _notify(session_factory, settings, kind, ev)
            else:
                _dispatch(_notify(session_factory, settings, kind, ev), False)
    elif not want_email and inserted:
        try:
            async with session_factory() as db:
                await db.execute(text("update error_events set notify_status = 'not_needed' where id = :i"), {"i": ev["id"]})
                await db.commit()
        except Exception:
            pass
    _LIVE_CACHE[service or module] = (time.monotonic(), True)
    return {k: ev[k] for k in ("id", "error_id", "category", "grouping", "severity", "occurrences", "status")} | {"inserted": inserted, "emailed": bool(want_email and (inserted or due))}


async def _fallback_email(settings: MonitoringSettings, params: dict, classified: classify.Classified, want_email: bool) -> None:
    """The event table is unreachable (usually because the database is). One direct email per fingerprint per cooldown, from memory."""
    if not want_email:
        return
    now = time.monotonic()
    if now - _FALLBACK_SENT.get(params["fp"], -1e9) < _cooldown_minutes() * 60:
        return
    _FALLBACK_SENT[params["fp"]] = now
    ev = {**params, "error_id": "(not stored: the database is unreachable)", "http_status": params["status"], "website_url": params["url"], "request_id": params["rid"], "first_seen_at": datetime.now(timezone.utc),
          "last_seen_at": datetime.now(timezone.utc), "occurrences": 1, "suggested_steps": params["steps"], "title": classified.title}
    status, reason = await _email(settings, NEW, ev)
    logger.warning("error monitor: direct email (event store unavailable) -> %s", status if not reason else f"{status}: {reason}")


async def record_block(*, agent: str, website_url: str, verdict: blocks.BlockVerdict, module: str = "crawler", scan_id: str | uuid.UUID | None = None, wait: bool = False, session_factory=None) -> dict | None:
    """The website blocked the scanner. Always emailed (once per website per cooldown); the dashboard shows "Website blocked our scanner."."""
    kind = verdict.kind or blocks.ACCESS_BLOCKED
    vendor = f" ({verdict.vendor})" if verdict.vendor else ""
    c = classify.Classified(
        kind.lower(), classify.WEBSITE, classify.WARNING, False,
        "Nothing is wrong with the application. The website's protection refused the scanner" + vendor + ". If you own the site, ask whoever runs its firewall or bot protection to allow the scanner (by address or user agent) and scan again. "
        "The scanner never tries to get round a CAPTCHA or firewall.", True, blocks.USER_MESSAGE)
    return await record_error(agent=agent, module=module, service="website", message=f"{blocks.USER_MESSAGE} {verdict.signal[:1].upper() + verdict.signal[1:]}.", website_url=website_url, scan_id=scan_id, classified=c,
                              status=None, wait=wait, session_factory=session_factory)


# ---- recovery ---------------------------------------------------------------------------------------------------------------------------------------------------------

async def _has_live(session_factory, key: str) -> bool:
    cached = _LIVE_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < _LIVE_TTL:
        return cached[1]
    try:
        async with session_factory() as db:
            live = bool(await db.scalar(text("select exists (select 1 from error_events where (service = :k or module = :k) and status in ('open', 'acknowledged') and category not in ('bot_blocked', 'access_blocked'))"), {"k": key}))
    except Exception:
        live = False
    _LIVE_CACHE[key] = (time.monotonic(), live)
    return live


async def record_success(service: str, *, agent: str | None = None, wait: bool = False, session_factory=None, settings: MonitoringSettings | None = None) -> int:
    """A call to `service` worked: resolve its open events and email one recovery notice for each that had been announced. Cheap when nothing is open (one cached check). Never raises."""
    try:
        if session_factory is None:
            from app.db.session import async_session_factory as session_factory
        if not await _has_live(session_factory, service):
            return 0
        settings = settings or get_monitoring_settings()
        async with session_factory() as db:
            rows = (await db.execute(text(
                "update error_events set status = 'resolved', resolved_at = now(), last_recovery_at = now() where (service = :s or module = :s) and status in ('open', 'acknowledged') "
                "and category not in ('bot_blocked', 'access_blocked') and (cast(:a as text) is null or agent = :a) returning " + COLUMNS), {"s": service, "a": agent})).mappings().all()
            await db.commit()
        _LIVE_CACHE[service] = (time.monotonic(), False)
        for r in rows:
            ev = dict(r)
            ev["title"] = ev["category"].replace("_", " ")
            if (ev.get("notify_count") or 0) > 0:
                if wait:
                    await _notify(session_factory, settings, RECOVERED, ev)
                else:
                    _dispatch(_notify(session_factory, settings, RECOVERED, ev), False)
        return len(rows)
    except Exception:
        logger.exception("error monitor: could not record a recovery")
        return 0


# ---- the dashboard's side ---------------------------------------------------------------------------------------------------------------------------------------------

def _row(r: Any) -> dict:
    return dict(r)


async def list_events(db, *, agent: str | None = None, grouping: str | None = None, category: str | None = None, status: str | None = None, severity: str | None = None,
                      website: str | None = None, since: datetime | None = None, until: datetime | None = None, include_info: bool = False, offset: int = 0, limit: int = 25) -> dict:
    where, p = ["true"], {"o": offset, "l": limit}
    for col, val in (("agent", agent), ("grouping", grouping), ("category", category), ("status", status), ("severity", severity)):
        if val:
            where.append(f"{col} = :{col}")
            p[col] = val
    if not include_info and not severity:
        where.append("severity <> 'info'")
    if website:
        where.append("lower(website_url) like :w")
        p["w"] = f"%{website.strip().lower()}%"
    if since:
        where.append("last_seen_at >= :since")
        p["since"] = since
    if until:
        where.append("first_seen_at <= :until")
        p["until"] = until
    clause = " and ".join(where)
    total = int(await db.scalar(text(f"select count(*) from error_events where {clause}"), p))
    rows = (await db.execute(text(f"select {COLUMNS} from error_events where {clause} order by last_seen_at desc offset :o limit :l"), p)).mappings().all()
    return {"items": [_row(r) for r in rows], "total": total, "offset": offset, "limit": limit}


async def summary(db) -> dict:
    r = (await db.execute(text("""
        select count(*) total,
               count(*) filter (where severity = 'critical') critical,
               count(*) filter (where grouping = 'api') api,
               count(*) filter (where grouping in ('rag', 'embedding')) rag,
               count(*) filter (where grouping = 'database') database,
               count(*) filter (where category in ('bot_blocked', 'access_blocked')) bot_blocked,
               count(*) filter (where status = 'open') open_,
               count(*) filter (where status = 'acknowledged') acknowledged,
               count(*) filter (where status = 'resolved') resolved,
               max(last_seen_at) latest_error_at,
               max(last_recovery_at) last_recovery_at
        from error_events where severity <> 'info'"""))).mappings().one()
    latest = (await db.execute(text("select error_id, agent, service, category, last_seen_at, status from error_events where severity <> 'info' order by last_seen_at desc limit 1"))).mappings().first()
    out = dict(r)
    out["open"] = out.pop("open_")
    out["latest"] = dict(latest) if latest else None
    return out


async def get_event(db, ident: str) -> dict | None:
    r = (await db.execute(text(f"select {COLUMNS} from error_events where error_id = :i or id::text = :i"), {"i": ident})).mappings().first()
    return _row(r) if r else None


async def acknowledge(db, ident: str, by: str) -> bool:
    res = await db.execute(text("update error_events set status = 'acknowledged', acknowledged_by = :b, acknowledged_at = now() where (error_id = :i or id::text = :i) and status = 'open'"), {"i": ident, "b": scrub(by, 120)})
    return (res.rowcount or 0) > 0


async def resolve(db, ident: str, by: str) -> bool:
    res = await db.execute(text("update error_events set status = 'resolved', resolved_at = now(), acknowledged_by = coalesce(acknowledged_by, :b) where (error_id = :i or id::text = :i) and status in ('open', 'acknowledged')"),
                           {"i": ident, "b": scrub(by, 120)})
    return (res.rowcount or 0) > 0


# ---- bridges from places that already know about a failure ----------------------------------------------------------------------------------------------------------------

_SOURCE_SERVICE = {"anthropic_api": "claude_api", "nvidia_api": "nvidia_api", "embedding_api": "embedding_api", "database": "postgres", "job_queue": "worker", "llm_calls": "llm", "scan_failures": "crawler"}
_CHECK_KIND = {"embedding_api": ("model_unavailable", classify.EMBEDDING), "vector_db": ("vector_db_error", classify.DATABASE), "gateway": ("bad_gateway", classify.API), "database": ("database_unavailable", classify.DATABASE), "anthropic_api": ("api_key_invalid", classify.CONFIG),
               "nvidia_api": ("api_key_invalid", classify.CONFIG), "job_queue": ("worker_failure", classify.WORKER), "llm_calls": ("llm_failures", classify.LLM), "scan_failures": ("scan_failures", classify.CRAWLER)}


async def mirror_check_result(result) -> None:
    """The periodic system checks (database, Claude key, NVIDIA key, job queue) already email through system_alerts. Mirror their outcome into the events table, WITHOUT a second email, so the Error
    Monitoring screen shows API-key and database failures beside the rest, and a passing check resolves the events of that service."""
    service = _SOURCE_SERVICE.get(result.source, result.source)
    if result.ok:
        await record_success(service)
        return
    detail = scrub(f"{result.title or result.label}: {result.detail}", 400)
    category, group = _CHECK_KIND.get(result.source, ("check_failing", classify.WORKER))
    if result.source in ("anthropic_api", "nvidia_api") and not result.definitive:
        category = "provider_unavailable"
    c = classify.Classified(category, group, classify.CRITICAL if result.severity == "critical" else classify.WARNING, not result.definitive, result.action or "Open System health for details.", False,
                            result.title or result.label)
    await record_error(agent="platform", module=f"system_check:{result.source}", service=service, message=detail, classified=c, email=False)


_JOB_AGENT = {"enterprise_scan": "enterprise"}
_FIVE_PAGE_JOBS = ("scan", "analyze", "render_reports", "consent_webhook", "consent_api_chain", "scan_diff", "monitoring_diff")


async def record_job_failure(job_type: str, payload: dict | None, exc: BaseException, *, wait: bool = False) -> None:
    """A background job raised. The worker already records the job as failed and retries or gives up; this adds the event (and, for a scan the website blocked, the block). Never raises."""
    try:
        payload = payload or {}
        agent = _JOB_AGENT.get(job_type) or ("five_page" if job_type in _FIVE_PAGE_JOBS else "platform")
        scan_id, url = payload.get("scan_id"), payload.get("url") or payload.get("website_url")
        if not url and scan_id and agent == "five_page":                 # the five-page jobs carry only the scan id: the address is on the scan row
            url = await _scan_url(scan_id)
        verdict = blocks.detect_block_from_error(exc) if job_type in ("scan", "enterprise_scan", "consent_api_chain") else blocks.NOT_BLOCKED
        if verdict:
            await record_block(agent=agent, website_url=url or "", verdict=verdict, module=f"worker:{job_type}", scan_id=scan_id, wait=wait)
            return
        await record_error(agent=agent, module=f"worker:{job_type}", exc=exc, service="worker", scan_id=scan_id, website_url=url, upstream=False, wait=wait)
    except Exception:
        logger.exception("error monitor: could not record a job failure")


async def _scan_url(scan_id) -> str | None:
    try:
        from app.db.session import async_session_factory
        async with async_session_factory() as db:
            return await db.scalar(text("select url from consent_scans where id = cast(:i as uuid)"), {"i": str(scan_id)})
    except Exception:
        return None


_RUN_WATERMARK: dict[str, datetime] = {}


async def record_agent_run_failures(session_factory=None) -> int:
    """Five-page analyses that ended FAILED. The agent graph catches its own errors, so the job still ends "done" and the worker never sees an exception; the reason is in the run record. It is classified
    (a provider answering 410, a rejected key, a timeout) and recorded as an event of the five-page agent, once per run. Read-only on the five-page tables: the scanner and the agent are not changed."""
    if session_factory is None:
        from app.db.session import async_session_factory as session_factory
    since = _RUN_WATERMARK.get("since") or (datetime.now(timezone.utc) - timedelta(minutes=15))   # the watermark is in memory: after a restart look back only a little, or a failure that was fixed hours ago is announced again
    async with session_factory() as db:
        rows = (await db.execute(text(
            "select r.id, r.scan_id, r.error, r.completed_at, s.url from agent_runs r join consent_scans s on s.id = r.scan_id "
            "where r.status = 'failed' and r.completed_at > :since order by r.completed_at"), {"since": since})).mappings().all()
    done = 0
    for r in rows:
        msg = r["error"] or "the analysis failed"
        low = msg.lower()
        service = "embedding_api" if ("retriev" in low or "embed" in low) else ("llm" if ("llm" in low or "model" in low or "anthropic" in low or "nvidia" in low) else None)
        await record_error(agent="five_page", module="five_page.agent_run", exc=RuntimeError(msg), service=service, scan_id=r["scan_id"], website_url=r["url"], upstream=True)
        _RUN_WATERMARK["since"] = r["completed_at"]
        done += 1
    return done
