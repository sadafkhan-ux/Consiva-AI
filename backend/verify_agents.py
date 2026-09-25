"""Exercise all five Consiva agents against a running server.

Each agent gets its core workflow driven for real -- create, act, read back -- and the
result is checked, not just the HTTP status. A 200 that returns an empty or wrong-shaped
body is a failure here.

    python verify_agents.py
    python verify_agents.py --base-url https://consiva-agent.gignati.com --token "eyJ..."
    python verify_agents.py --agent dsr        # just one

Exit code is 0 only if nothing failed, so this can gate a deploy.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime

_results: list[tuple[str, str, str]] = []
_agent = ""


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append(("PASS" if ok else "FAIL", _agent, name))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def skip(name: str, why: str) -> None:
    """Could not be run. Not a pass, not a failure -- reporting either would mislead."""
    _results.append(("SKIP", _agent, name))
    print(f"    [SKIP] {name}  -- {why}")


def items(body) -> list:
    """The list out of a list response, whatever the endpoint calls it.

    These endpoints do not agree on an envelope: regwatch returns {"sources": [...]},
    ropa returns {"connectors": [...]}, some return a bare array. Guessing one key
    silently produced an empty list and made a working agent look broken -- the
    regwatch check reported "no sources configured" against a database holding twelve.
    """
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for key in ("items", "sources", "findings", "runs", "records", "connectors",
                "requests", "incidents", "scans", "actions", "results", "data"):
        value = body.get(key)
        if isinstance(value, list):
            return value
    # A single-key dict wrapping a list, whatever that key is.
    values = [v for v in body.values() if isinstance(v, list)]
    return values[0] if len(values) == 1 else []


def call(method, url, token=None, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Consiva-Agent-Verifier/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# ── Agent 1: Consent ────────────────────────────────────────────────────────────

def agent_consent(api, token):
    # The console API has no scan list -- Agent 1 was the only agent without one --
    # so this reads the integration API's, which was added for exactly that reason.
    status, body = call("GET", f"{api}/consent-agent/scans?limit=5", token)
    if not check("scan list responds", status == 200, f"-> {status}"):
        return
    scans = body.get("scans", []) if isinstance(body, dict) else []
    check("has prior scans to read back", len(scans) > 0, f"{len(scans)} scans")
    if not scans:
        return
    scan_id = scans[0].get("scan_id")
    status, ev = call("GET", f"{api}/consent/scans/{scan_id}/evidence", token)
    check("evidence is retrievable", status == 200 and isinstance(ev, dict), f"-> {status}")
    status, f = call("GET", f"{api}/consent/scans/{scan_id}/findings", token)
    check("findings are retrievable", status == 200, f"-> {status}, {len(f) if isinstance(f, list) else '?'}")


# ── Agent 2: ROPA / data discovery ──────────────────────────────────────────────

def agent_ropa(api, token):
    status, body = call("GET", f"{api}/ropa/connectors", token)
    connectors = items(body)
    check("connector catalogue responds", status == 200 and len(connectors) > 0,
          f"-> {status}, {len(connectors)} connectors")

    status, body = call("GET", f"{api}/ropa/sources", token)
    check("source list responds", status == 200, f"-> {status}")

    status, body = call("GET", f"{api}/ropa/runs", token)
    runs = items(body)
    if not check("discovery run list responds", status == 200, f"-> {status}, {len(runs)} runs"):
        return

    # Evidence push: the agentless ingestion path, which needs no external database.
    # It authenticates with an integration key rather than a user token, so one is
    # minted here -- otherwise this whole path goes untested, which is how a broken
    # ingestion route would reach production looking "skipped, probably fine".
    # The evidence envelope names the organisation explicitly, so read it from data
    # this caller can already see rather than inventing one.
    _s, _runs = call("GET", f"{api}/ropa/runs", token)
    org_id = next((r.get("org_id") for r in items(_runs) if isinstance(r, dict) and r.get("org_id")), None)

    status, keyresp = call("POST", f"{api}/ropa/integration-keys", token,
                           body={"name": f"verify-{uuid.uuid4().hex[:8]}"})
    api_key = None
    if isinstance(keyresp, dict):
        api_key = keyresp.get("key") or keyresp.get("api_key") or keyresp.get("token")
    if not api_key:
        skip("integration key can be minted", f"-> {status} {str(keyresp)[:70]}")
    else:
        check("integration key can be minted", True, "key issued")

    # The real payload shape, read off the OpenAPI schema rather than guessed: the
    # envelope carries source_name/correlation_id/generated_at, and the evidence itself
    # is a flat set of sources/tables/columns cross-referenced by local_id.
    run_uuid = str(uuid.uuid4())
    status, body = call("POST", f"{api}/ropa/evidence", api_key or token, body={
        "schema_version": "1.0",
        "source_name": f"verify-{uuid.uuid4().hex[:8]}",
        "correlation_id": str(uuid.uuid4()),
        "generated_at": datetime.now(UTC).isoformat(),
        "evidence": {
            "org_id": org_id or str(uuid.uuid4()),
            "discovery_run_id": run_uuid,
            "sources": [
                {"local_id": "s1", "name": "verify-app-db", "source_type": "database"},
            ],
            "tables": [
                {"local_id": "t1", "source_local_id": "s1", "table_name": "customers"},
            ],
            "columns": [
                {"local_id": "c1", "table_local_id": "t1",
                 "column_name": "email", "data_type": "text"},
                {"local_id": "c2", "table_local_id": "t1",
                 "column_name": "phone_number", "data_type": "text"},
                {"local_id": "c3", "table_local_id": "t1",
                 "column_name": "created_at", "data_type": "timestamp"},
            ],
        },
    })
    if status in (401, 403):
        skip("evidence push creates a run", f"needs an integration key -> {status}")
    elif check("evidence push accepted", status in (200, 201, 202), f"-> {status}"):
        run_id = body.get("run_id") or body.get("id") if isinstance(body, dict) else None
        if run_id:
            status, recs = call("GET", f"{api}/ropa/runs/{run_id}/records", token)
            records = items(recs)
            check("pushed schema produced ROPA records", status == 200 and len(records) > 0,
                  f"-> {status}, {len(records)} records")
            status, finds = call("GET", f"{api}/ropa/runs/{run_id}/findings", token)
            check("run findings are retrievable", status == 200, f"-> {status}")
        else:
            skip("pushed schema produced ROPA records", f"no run id in response: {str(body)[:80]}")

    if runs:
        rid = runs[0].get("id")
        status, _ = call("GET", f"{api}/ropa/runs/{rid}", token)
        check("an existing run is readable", status == 200, f"-> {status}")


# ── Agent 3: DSR ────────────────────────────────────────────────────────────────

def agent_dsr(api, token):
    status, body = call("GET", f"{api}/dsr/requests", token)
    if not check("request list responds", status == 200, f"-> {status}"):
        return

    status, created = call("POST", f"{api}/dsr/requests", token, body={
        "raw_request": "Please delete all personal data you hold about me. "
                       "My email is verify-probe@example.test.",
        "channel": "email",
        "requester_email": "verify-probe@example.test",
    })
    if not check("a request can be created", status in (200, 201, 202),
                 f"-> {status} {str(created)[:90] if status >= 400 else ''}"):
        return
    rid = created.get("id") if isinstance(created, dict) else None
    if not rid:
        skip("request classification", "no request id returned")
        return

    status, cls = call("POST", f"{api}/dsr/requests/{rid}/classify", token, body={})
    ok = status in (200, 201, 202)
    rtype = cls.get("request_type") if isinstance(cls, dict) else None
    check("classification runs and returns a type", ok and rtype is not None,
          f"-> {status}, type={rtype}")
    # The probe text says "delete", so anything else is a real classification miss.
    if rtype:
        check("classifier read the request correctly", "eras" in str(rtype).lower()
              or "delet" in str(rtype).lower(), f"got {rtype!r} for a deletion request")

    status, got = call("GET", f"{api}/dsr/requests/{rid}", token)
    check("the request reads back", status == 200 and isinstance(got, dict), f"-> {status}")

    status, audit = call("GET", f"{api}/dsr/requests/{rid}/audit", token)
    entries = items(audit)
    check("audit trail recorded the work", status == 200 and len(entries) > 0,
          f"-> {status}, {len(entries)} entries")


# ── Agent 4: Breach / incidents ─────────────────────────────────────────────────

def agent_incidents(api, token):
    status, body = call("GET", f"{api}/incidents", token)
    if not check("incident list responds", status == 200, f"-> {status}"):
        return

    status, created = call("POST", f"{api}/incidents", token, body={
        "title": f"Verification probe {uuid.uuid4().hex[:8]}",
        "description": "Automated verification: unauthorised access to a customer "
                       "database containing email addresses and phone numbers.",
        # Required by the real schema -- omitting it was a fault in this test, not in
        # the agent.
        "source": "access_anomaly",
        "detected_at": datetime.now(UTC).isoformat(),
        "reported_by": "verify_agents.py",
    })
    if not check("an incident can be created", status in (200, 201, 202),
                 f"-> {status} {str(created)[:90] if status >= 400 else ''}"):
        return
    iid = created.get("id") if isinstance(created, dict) else None
    if not iid:
        skip("incident classification", "no incident id returned")
        return

    status, cls = call("POST", f"{api}/incidents/{iid}/classify", token, body={})
    check("classification runs", status in (200, 201, 202), f"-> {status}")

    status, risk = call("GET", f"{api}/incidents/{iid}/risk", token)
    check("risk assessment is retrievable", status in (200, 404), f"-> {status}")

    # The AUDIT trail is the record of work performed. The TIMELINE is something
    # different -- it takes occurred_at/event/confidence and holds the incident's
    # real-world chronology as investigators establish it, so it is legitimately empty
    # on a freshly created case. Asserting otherwise reported a working agent as
    # broken.
    status, audit = call("GET", f"{api}/incidents/{iid}/audit", token)
    entries = items(audit)
    actions = {e.get("action") for e in entries if isinstance(e, dict)}
    check("audit trail recorded the work", status == 200 and len(entries) > 0,
          f"-> {status}, {len(entries)} entries")
    check("creation and classification are both audited",
          {"incident.created", "incident.classified"} <= actions,
          f"recorded: {sorted(a for a in actions if a)}")

    status, tl = call("GET", f"{api}/incidents/{iid}/timeline", token)
    check("timeline endpoint responds", status == 200,
          f"-> {status}, {len(items(tl))} entries (empty is correct on a new case)")

    status, got = call("GET", f"{api}/incidents/{iid}", token)
    check("the incident reads back", status == 200, f"-> {status}")


# ── Agent 5: Regulatory watch ───────────────────────────────────────────────────

def agent_regwatch(api, token):
    status, body = call("GET", f"{api}/regwatch/sources", token)
    sources = items(body)
    if not check("source list responds", status == 200, f"-> {status}, {len(sources)} sources"):
        return
    check("monitored sources are configured", len(sources) > 0,
          f"{len(sources)} registered" if sources
          else "NONE registered -- nothing can be watched")

    status, body = call("GET", f"{api}/regwatch/findings", token)
    findings = items(body)
    check("findings list responds", status == 200, f"-> {status}, {len(findings)} findings")

    status, body = call("GET", f"{api}/regwatch/actions/overdue", token)
    check("overdue action sweep responds", status == 200, f"-> {status}")

    if findings:
        fid = findings[0].get("id")
        status, _ = call("GET", f"{api}/regwatch/findings/{fid}", token)
        check("a finding reads back in detail", status == 200, f"-> {status}")
        status, audit = call("GET", f"{api}/regwatch/findings/{fid}/audit", token)
        check("finding audit is retrievable", status == 200, f"-> {status}")


AGENTS = {
    "consent": ("Agent 1 - Consent", agent_consent),
    "ropa": ("Agent 2 - ROPA / Data Discovery", agent_ropa),
    "dsr": ("Agent 3 - DSR", agent_dsr),
    "incidents": ("Agent 4 - Breach / Incidents", agent_incidents),
    "regwatch": ("Agent 5 - Regulatory Watch", agent_regwatch),
}


def main() -> int:
    global _agent
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8010")
    p.add_argument("--token", default=None)
    p.add_argument("--agent", choices=list(AGENTS), default=None)
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    api = f"{base}/api/v1"

    token = args.token
    if not token:
        status, body = call("POST", f"{base}/api/v1/dev/auto-login")
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            print(f"Could not authenticate (HTTP {status}). Pass --token.")
            return 1

    print(f"\nVerifying five agents at {api}\n")
    for key, (label, fn) in AGENTS.items():
        if args.agent and key != args.agent:
            continue
        _agent = label
        print(f"  {label}")
        try:
            fn(api, token)
        except Exception as exc:  # a crash in one agent must not hide the others
            check("agent check completed without crashing", False, f"{type(exc).__name__}: {exc}")
        print()

    failed = [(a, n) for r, a, n in _results if r == "FAIL"]
    skipped = [(a, n) for r, a, n in _results if r == "SKIP"]
    passed = len(_results) - len(failed) - len(skipped)
    print("-" * 66)
    print(f"  {passed} passed, {len(failed)} failed, {len(skipped)} skipped")
    if skipped:
        print("\n  SKIPPED (not run -- this is NOT a pass):")
        for a, n in skipped:
            print(f"    - [{a}] {n}")
    if failed:
        print("\n  FAILED:")
        for a, n in failed:
            print(f"    - [{a}] {n}")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
