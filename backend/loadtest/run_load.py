"""Put N simultaneous scan requests through the real API and queue, and check every result.

    python -m loadtest.run_load --reference          # scan each kind of fixture site ALONE and record what the scanner finds
    python -m loadtest.run_load --scans 100          # 100 scans requested at the same instant, across 10 organisations, then every result is compared with the reference

What it reports, from observation only:
  * how long the API took to accept each request, and whether it did;
  * for every scan: time waiting in the queue, time scanning, and the outcome;
  * the machine while it ran: CPU busy, memory, database connections, and how quickly the API answered an unrelated user the whole time;
  * for every scan that finished: whether what it found is exactly what the same site gave when scanned alone (pages, titles, cookies, trackers with their consent
    states, policies, forms, the consent mechanism and the Accept/Reject outcomes, the score and every requirement's status), and whether anything of another site
    leaked into it.

Test tooling only. It runs inside the load-test API container (docker-compose.loadtest.yml).
"""

import argparse
import asyncio
import json
import statistics
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import text

from app.config import get_settings
from app.core.tokens import issue_access_token
from app.db.session import async_session_factory

API = "http://127.0.0.1:8000"
REFERENCE_FILE = "/tmp/loadtest_reference.json"
RESULT_FILE = "/tmp/loadtest_result.json"
REFERENCE_SITES = [*range(10), 95, 96, 97, 98, 99]


def token_for(org: uuid.UUID) -> str:
    return issue_access_token(user_id=str(uuid.UUID(int=1)), org_id=str(org), role="member", settings=get_settings(), ttl_hours=3)[0]


def url_of(n: int) -> str:
    return f"http://site-{n:03d}.loadtest.example/"


def norm(value: str | None, n: int) -> str:
    return (value or "").replace(f"site-{n:03d}", "SITE")


def host_of(u: str) -> str:
    return urlparse(u).hostname or u


# ── what a scan found, in a form two scans can be compared by ────────────────────────────────────────────────────────────────────────────────────────

def signature(n: int, scan: dict, ev: dict | None, score: dict | None) -> dict:
    sig: dict = {"status": scan["status"]}
    if scan["status"] != "completed" or ev is None:
        sig["error"] = norm((scan.get("error") or "")[:120], n)
        return sig
    sig["pages"] = sorted((norm(p["url"], n), p.get("http_status"), p.get("discovered_via")) for p in ev["pages"])
    sig["titles"] = sorted(norm(p.get("title"), n) for p in ev["pages"])
    sig["cookies"] = sorted((c["name"], tuple(sorted(c["consent_states"])), c.get("category")) for c in ev["cookies"])
    sig["trackers"] = sorted({(host_of(t["script_src"]), tuple(sorted(t["consent_states"])), t.get("category"), t.get("vendor")) for t in ev["trackers"]})
    sig["policies"] = sorted(p["policy_type"] for p in ev["policies"])
    sig["forms"] = len(ev["forms"])
    s = (ev["consent_signals"] or [{}])[0]
    e = s.get("evidence") or {}
    sig["signal"] = (s.get("mechanism_type"), s.get("has_reject_all"), s.get("has_granular_choices"), e.get("accept_interaction"), e.get("reject_interaction"), e.get("reject_click_confirmed"))
    if score:
        r = score["readiness"]
        sig["score"] = (r["score_percent"], r["status"], r["coverage_percent"], tuple((q["id"], q["status"]) for q in score["requirements"]))
    return sig


def leaks(n: int, ev: dict) -> list[str]:
    """Anything in this scan's evidence that belongs to another site."""
    own = f"site-{n:03d}.loadtest.example"
    bad = []
    for p in ev["pages"]:
        if host_of(p["url"]) != own:
            bad.append(f"page on {host_of(p['url'])}")
        if p.get("title") and not p["title"].startswith(f"site-{n:03d}") and p.get("discovered_via") not in ("failed", "timeout"):
            bad.append(f"title {p['title'][:40]!r}")
    for pol in ev["policies"]:
        if pol.get("url") and host_of(pol["url"]) != own:
            bad.append(f"policy on {host_of(pol['url'])}")
    return bad


# ── measuring the machine and the API while the load runs ────────────────────────────────────────────────────────────────────────────────────────

class Meter:
    def __init__(self, probe_token: str):
        self.cpu: list[float] = []
        self.mem_used_mb: list[float] = []
        self.db_conns: list[int] = []
        self.probe_ms: list[float] = []
        self.probe_errors = 0
        self.token = probe_token
        self.stop = False

    @staticmethod
    def _cpu_times():
        with open("/proc/stat") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        return sum(parts), parts[3] + parts[4]          # total, idle + iowait

    async def sample_machine(self):
        total0, idle0 = self._cpu_times()
        while not self.stop:
            await asyncio.sleep(1.0)
            total1, idle1 = self._cpu_times()
            dt, di = total1 - total0, idle1 - idle0
            self.cpu.append(100.0 * (1 - di / dt) if dt else 0.0)
            total0, idle0 = total1, idle1
            mem = {ln.split(":")[0]: int(ln.split()[1]) for ln in Path("/proc/meminfo").read_text().splitlines() if ":" in ln}
            self.mem_used_mb.append((mem["MemTotal"] - mem["MemAvailable"]) / 1024)
            try:
                async with async_session_factory() as db:
                    n = (await db.execute(text("select count(*) from pg_stat_activity where datname = current_database()"))).scalar_one()
                self.db_conns.append(int(n))
            except Exception:  # noqa: BLE001, S110 -- the sampler must not disturb the test
                pass

    async def probe_api(self, client: httpx.AsyncClient):
        """An unrelated user reading their own list, over and over: the measure of whether the load hurts anyone else."""
        while not self.stop:
            a = time.perf_counter()
            try:
                r = await client.get("/api/v1/consent/scans?limit=5", headers={"Authorization": f"Bearer {self.token}"})
                if r.status_code != 200:
                    self.probe_errors += 1
                self.probe_ms.append((time.perf_counter() - a) * 1000)
            except Exception:  # noqa: BLE001
                self.probe_errors += 1
            await asyncio.sleep(0.5)


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


# ── the scans ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def fetch_result(client, token, scan_id, n):
    h = {"Authorization": f"Bearer {token}"}
    scan = (await client.get(f"/api/v1/consent/scans/{scan_id}", headers=h)).json()
    ev = score = None
    if scan["status"] == "completed":
        ev = (await client.get(f"/api/v1/consent/scans/{scan_id}/evidence", headers=h)).json()
        score = (await client.get(f"/api/v1/consent/scans/{scan_id}/dpdp-score", headers=h)).json()
    return scan, ev, score


async def run_reference():
    org = uuid.uuid4()
    token = token_for(org)
    out = {}
    async with httpx.AsyncClient(base_url=API, timeout=120) as client:
        for n in REFERENCE_SITES:
            t0 = time.time()
            r = await client.post("/api/v1/consent/scans", json={"url": url_of(n), "authorized": True}, headers={"Authorization": f"Bearer {token}"})
            sid = r.json()["id"]
            while True:
                s = (await client.get(f"/api/v1/consent/scans/{sid}", headers={"Authorization": f"Bearer {token}"})).json()
                if s["status"] in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(1)
            scan, ev, score = await fetch_result(client, token, sid, n)
            out[str(n)] = signature(n, scan, ev, score)
            print(f"reference site-{n:03d} ({s['status']}, {time.time() - t0:.0f}s): " + (f"pages {len(out[str(n)].get('pages', []))} cookies {len(out[str(n)].get('cookies', []))} trackers {len(out[str(n)].get('trackers', []))} score {out[str(n)].get('score', ['-'])[0]}" if s["status"] == "completed" else norm(s.get("error") or "", n)[:110]))
    Path(REFERENCE_FILE).write_text(json.dumps(out, default=list, indent=1))
    print(f"reference written for {len(out)} sites")


def expected_reference(n: int) -> str:
    return str(n if n in REFERENCE_SITES else n % 10)


async def run_load(total: int, orgs: int, timeout_s: float):
    reference = json.loads(Path(REFERENCE_FILE).read_text())
    org_ids = [uuid.uuid4() for _ in range(orgs)]
    tokens = {o: token_for(o) for o in org_ids}
    sites = list(range(total))
    assign = {n: org_ids[i % orgs] for i, n in enumerate(sites)}
    meter = Meter(tokens[org_ids[0]])
    limits = httpx.Limits(max_connections=400, max_keepalive_connections=100)
    async with httpx.AsyncClient(base_url=API, timeout=180, limits=limits) as client, httpx.AsyncClient(base_url=API, timeout=60) as probe_client:
        bg = [asyncio.create_task(meter.sample_machine()), asyncio.create_task(meter.probe_api(probe_client))]
        await asyncio.sleep(3)                                             # a quiet baseline for the probe
        baseline = list(meter.probe_ms)

        # ---- all requests at the same instant ----
        gate = asyncio.Event()
        post_ms: dict[int, float] = {}
        post_status: dict[int, int] = {}
        scan_ids: dict[int, str] = {}

        async def submit(n: int):
            await gate.wait()
            a = time.perf_counter()
            try:
                r = await client.post("/api/v1/consent/scans", json={"url": url_of(n), "authorized": True}, headers={"Authorization": f"Bearer {tokens[assign[n]]}"})
                post_status[n] = r.status_code
                if r.status_code in (200, 201, 202):
                    scan_ids[n] = r.json()["id"]
            except Exception as exc:  # noqa: BLE001
                post_status[n] = -1
                print(f"site-{n:03d}: request failed: {type(exc).__name__} {exc}")
            post_ms[n] = (time.perf_counter() - a) * 1000

        tasks = [asyncio.create_task(submit(n)) for n in sites]
        await asyncio.sleep(0.5)
        t_start = time.time()
        gate.set()
        await asyncio.gather(*tasks)
        print(f"{len(scan_ids)} of {total} requests accepted in {time.time() - t_start:.1f}s (API answer: median {statistics.median(post_ms.values()):.0f} ms, p95 {pct(list(post_ms.values()), .95):.0f} ms, max {max(post_ms.values()):.0f} ms; statuses { {s: list(post_status.values()).count(s) for s in set(post_status.values())} })")

        # ---- wait for every scan ----
        done: dict[int, dict] = {}
        finished_at: dict[int, float] = {}
        last_print = 0.0
        while len(done) < len(scan_ids) and time.time() - t_start < timeout_s:
            pending = [n for n in scan_ids if n not in done]
            rs = await asyncio.gather(*[client.get(f"/api/v1/consent/scans/{scan_ids[n]}", headers={"Authorization": f"Bearer {tokens[assign[n]]}"}) for n in pending], return_exceptions=True)
            for n, r in zip(pending, rs, strict=True):
                if isinstance(r, Exception) or r.status_code != 200:
                    continue
                if r.json()["status"] in ("completed", "failed", "cancelled"):
                    done[n] = r.json()
                    finished_at[n] = time.time() - t_start
            if time.time() - last_print > 15:
                last_print = time.time()
                print(f"  t+{time.time() - t_start:4.0f}s: {len(done)}/{len(scan_ids)} finished | CPU {meter.cpu[-1] if meter.cpu else 0:3.0f}% | memory {meter.mem_used_mb[-1] if meter.mem_used_mb else 0:5.0f} MB | db connections {meter.db_conns[-1] if meter.db_conns else 0} | API probe {meter.probe_ms[-1] if meter.probe_ms else 0:4.0f} ms")
            await asyncio.sleep(2)
        wall = time.time() - t_start
        meter.stop = True
        await asyncio.gather(*bg, return_exceptions=True)

        # ---- results and verification ----
        sem = asyncio.Semaphore(8)

        async def one(n):
            async with sem:
                return n, await fetch_result(client, tokens[assign[n]], scan_ids[n], n)
        fetched = dict(await asyncio.gather(*[one(n) for n in done]))

    async with async_session_factory() as db:
        rows = (await db.execute(text(
            "select s.id::text, extract(epoch from (s.started_at - s.created_at)) as queue_wait, extract(epoch from (s.completed_at - s.started_at)) as scan_secs, "
            "(select attempts from agent_jobs j where j.payload->>'scan_id' = s.id::text and j.job_type='scan' limit 1) as attempts "
            "from consent_scans s where s.id = any(cast(:ids as uuid[]))"), {"ids": list(scan_ids.values())})).all()
    timing = {r[0]: (r[1], r[2], r[3]) for r in rows}

    mismatches, leaked, outcome = [], [], {"completed": 0, "failed": 0, "other": 0, "not_finished": len(scan_ids) - len(done)}
    for n, (scan, ev, score) in fetched.items():
        outcome[scan["status"] if scan["status"] in ("completed", "failed") else "other"] += 1
        got = json.loads(json.dumps(signature(n, scan, ev, score), default=list))
        want = reference[expected_reference(n)]
        if got != want:
            diff = [k for k in set(got) | set(want) if got.get(k) != want.get(k)]
            mismatches.append((n, diff, got.get("error") or ""))
        if ev:
            leaked += [(n, x) for x in leaks(n, ev)]

    waits = [timing[scan_ids[n]][0] for n in done if scan_ids[n] in timing and timing[scan_ids[n]][0] is not None]
    scans_s = [timing[scan_ids[n]][1] for n in done if scan_ids[n] in timing and timing[scan_ids[n]][1] is not None]
    e2e = [finished_at[n] for n in done]
    retried = [n for n in done if (timing.get(scan_ids[n]) or (0, 0, 1))[2] and timing[scan_ids[n]][2] > 1]
    during = meter.probe_ms[len(baseline):]
    print("\n================ RESULT ================")
    print(f"{total} scans requested at once across {orgs} organisations; all finished: {len(done) == len(scan_ids)}; wall time {wall:.0f}s")
    print(f"outcomes: {outcome}")
    print(f"queue wait    : median {statistics.median(waits):.0f}s  p95 {pct(waits, .95):.0f}s  max {max(waits):.0f}s" if waits else "queue wait    : n/a")
    print(f"scan duration : median {statistics.median(scans_s):.0f}s  p95 {pct(scans_s, .95):.0f}s  max {max(scans_s):.0f}s" if scans_s else "scan duration : n/a")
    print(f"end to end    : median {statistics.median(e2e):.0f}s  p95 {pct(e2e, .95):.0f}s  max {max(e2e):.0f}s" if e2e else "end to end    : n/a")
    print(f"machine       : CPU busy median {statistics.median(meter.cpu):.0f}%  p95 {pct(meter.cpu, .95):.0f}%  max {max(meter.cpu):.0f}% | memory peak {max(meter.mem_used_mb):.0f} MB | db connections peak {max(meter.db_conns) if meter.db_conns else 'n/a'}")
    print(f"other users   : API answered an unrelated user in median {statistics.median(baseline):.0f} ms before, {statistics.median(during):.0f} ms during (p95 {pct(during, .95):.0f}, max {max(during):.0f}); errors {meter.probe_errors}")
    print(f"jobs retried  : {len(retried)} {retried[:10]}")
    print(f"accuracy      : {len(fetched) - len(mismatches)} of {len(fetched)} finished scans identical to the same site scanned alone; {len(mismatches)} differ")
    for n, diff, err in mismatches[:15]:
        print(f"   site-{n:03d} differs in {sorted(diff)} {err[:100]}")
    print(f"contamination : {len(leaked)} items of another site found in a scan" + (f" {leaked[:5]}" if leaked else ""))
    Path(RESULT_FILE).write_text(json.dumps({"wall": wall, "outcome": outcome, "mismatches": mismatches, "leaked": leaked, "cpu": meter.cpu, "mem": meter.mem_used_mb, "db": meter.db_conns, "probe": meter.probe_ms,
               "waits": waits, "scan_secs": scans_s, "e2e": e2e}, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", action="store_true")
    ap.add_argument("--scans", type=int, default=100)
    ap.add_argument("--orgs", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=1800)
    a = ap.parse_args()
    asyncio.run(run_reference() if a.reference else run_load(a.scans, a.orgs, a.timeout))
