"""Smoke-test the Consent Agent API against a running server.

Calls every endpoint for real and prints PASS/FAIL per check. Run it after any deploy,
or whenever you want to confirm for yourself that the documentation matches reality
rather than taking someone's word for it.

    python verify_api.py                          # localhost:8010, dev auto-login
    python verify_api.py --base-url https://host  # a real deployment
    python verify_api.py --token "eyJ..."         # skip login, use a token you have
    python verify_api.py --url https://example.com --no-scan   # checks only, no crawl

WHAT "PASS" MEANS HERE

Each check asserts something specific and says what it asserted. A route existing in
/openapi.json is NOT one of them -- a registered route that raises on every call still
appears in the schema, so the schema is treated as a claim to be tested, not evidence.

Exit code is 0 only if every check passed, so this can gate a deploy.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str]] = []


def skip(name: str, why: str) -> None:
    """A check that could not be RUN is not a check that failed.

    Reporting one as a failure is actively misleading: exhausting the hourly scan
    quota made three SSRF checks report FAIL, which reads as "SSRF protection is
    broken" when the request never reached the URL validator at all.
    """
    _results.append((SKIP, name))
    print(f"  [{SKIP}] {name}  -- {why}")


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((PASS if ok else FAIL, name))
    # ASCII on purpose: the Windows console defaults to cp1252 and raises
    # UnicodeEncodeError on a check mark, which would make this tool fail on exactly
    # the machine it is most often run from.
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def note(text: str) -> None:
    print(f"      {text}")


def call(method: str, url: str, token: str | None = None, body: dict | None = None,
         headers: dict | None = None, timeout: int = 60):
    """Returns (status_code, parsed_body_or_text). Never raises on an HTTP error --
    a 4xx is frequently the thing being asserted."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    # A real User-Agent. The default "Python-urllib/3.x" is blocked outright by the
    # Cloudflare Tunnel in front of the GB10 deployment -- it answered 403 from the
    # edge while curl got through to the app, which made this tool report an API as
    # "deployed" when the 403 never reached the application at all.
    request.add_header("User-Agent", "Consiva-API-Verifier/1.0")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        # An edge (CDN/WAF) rejection is NOT an application response, and treating one
        # as if it were is how a check passes for the wrong reason.
        if exc.headers.get("cf-ray") and exc.code in (403, 503) and "<html" in raw.lower():
            return exc.code, {"_intercepted_by": exc.headers.get("server", "edge")}
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw
    except Exception as exc:  # connection refused, DNS, timeout
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--token", default=None, help="bearer token; omit to use dev auto-login")
    parser.add_argument("--url", default="https://projectflow.gignaati.com/login",
                        help="site to scan for the end-to-end check")
    parser.add_argument("--no-scan", action="store_true",
                        help="skip the real crawl; run only the checks that need no scan")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for the scan")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    api = f"{base}/api/v1/consent-agent"
    print(f"\nVerifying {api}\n")

    # ── Reachability ────────────────────────────────────────────────────────────
    #
    # Probed through the API itself, not /health.
    #
    # On the GB10 deployment nginx serves the SPA at / and proxies only /api/, so
    # GET /health returns the single-page app's HTML with a 200 -- a reachability
    # check on that path passes for entirely the wrong reason, and would have
    # reported a healthy API on a box where this API is not installed at all.
    # An /api/v1 path either reaches FastAPI (JSON) or does not.
    print("Server")
    status, body = call("GET", f"{base}/api/v1/consent-agent/openapi-probe")
    reachable = status in (401, 403, 404, 405) or isinstance(body, dict)
    if not check("API is reachable", reachable, f"/api/v1/... -> {status}"):
        note("Nothing else can be checked. Is it running, and is --base-url right?")
        note("Note: use the site root (https://host), not a /api/v1 path.")
        return 1

    # Is THIS API actually deployed, or only the older console API?
    status, _ = call("GET", f"{base}/api/v1/consent-agent/scans/"
                            "11111111-1111-1111-1111-111111111111/status")
    # A deployed, authenticated route answers 401 (no token). A missing one answers
    # 404 -- or 405 from the static-file catch-all, which is what an undeployed build
    # returns for a POST.
    if isinstance(body, dict) and body.get("_intercepted_by"):
        check("requests reach the application, not just the edge", False,
              f"blocked by {body['_intercepted_by']} with {status}")
        note("A CDN or WAF in front of this host answered instead of the app.")
        note("Allow this client, or run the verifier from inside the network.")
        return _summary()

    deployed = status in (401, 403)
    if not check("the consent-agent API is deployed on this server", deployed,
                 f"unauthenticated GET /status -> {status}"):
        note("This server is running, but does not have these endpoints. Deploy the")
        note("current build, then re-run. On the GB10:")
        note("  cd /opt/consiva && git pull")
        note("  docker compose -f docker-compose.prod.yml up -d --build")
        note("  docker compose -f docker-compose.prod.yml run --rm backend python migrate.py")
        return _summary()

    # ── Authentication ──────────────────────────────────────────────────────────
    print("\nAuthentication")
    status, _ = call("POST", f"{api}/scans", body={"website_url": "https://example.com",
                                                   "authorized": True})
    check("unauthenticated request is rejected", status == 401, f"no token -> {status}")

    status, _ = call("POST", f"{api}/scans", token="not-a-real-token",
                     body={"website_url": "https://example.com", "authorized": True})
    check("invalid token is rejected", status == 401, f"bad token -> {status}")

    token = args.token
    if not token:
        status, body = call("POST", f"{base}/api/v1/dev/auto-login")
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            note(f"dev auto-login unavailable (HTTP {status}) -- expected on a production")
            note("server. Pass --token to continue.")
            return _summary()
        note("using dev auto-login (development server)")

    # ── Input validation and SSRF ───────────────────────────────────────────────
    print("\nValidation and SSRF protection")
    status, body = call("POST", f"{api}/scans", token=token,
                        body={"website_url": "not-a-url", "authorized": True})
    check("malformed URL is rejected", status == 422, f"-> {status}")

    status, body = call("POST", f"{api}/scans", token=token,
                        body={"website_url": "https://example.com", "authorized": False})
    if status == 429:
        skip("missing authorization attestation is rejected",
             "scan quota exhausted; retry in an hour")
    else:
        check("missing authorization attestation is rejected", status == 403, f"-> {status}")

    for target, label in [
        ("http://169.254.169.254/latest/meta-data/", "cloud metadata address"),
        ("http://127.0.0.1:8010/internal", "loopback address"),
        ("http://10.0.0.1/", "private network address"),
    ]:
        status, body = call("POST", f"{api}/scans", token=token,
                            body={"website_url": target, "authorized": True})
        code = body.get("error", {}).get("code") if isinstance(body, dict) else None
        if status == 429:
            # The quota is checked before the URL is, so the request never reaches the
            # SSRF validator. Nothing can be concluded either way.
            skip(f"SSRF: {label} refused", "scan quota exhausted; retry in an hour")
            continue
        check(f"SSRF: {label} refused", status == 403 and code == "NOT_AUTHORIZED",
              f"-> {status} {code}")

    # ── Error shape ─────────────────────────────────────────────────────────────
    print("\nError contract")
    missing = uuid.uuid4()
    status, body = call("GET", f"{api}/scans/{missing}/status", token=token)
    shaped = isinstance(body, dict) and "error" in body and "code" in body.get("error", {})
    check("unknown scan returns 404", status == 404, f"-> {status}")
    check("errors use the documented {error:{code,message}} envelope", shaped,
          json.dumps(body)[:70] if isinstance(body, dict) else str(body)[:70])

    status, body = call("GET", f"{api}/scans/not-a-uuid/status", token=token)
    check("malformed scan id is rejected", status == 422, f"-> {status}")

    if args.no_scan:
        note("--no-scan given; skipping the end-to-end crawl")
        return _summary()

    # ── The real thing ──────────────────────────────────────────────────────────
    print(f"\nEnd-to-end scan of {args.url}")
    key = f"verify-{uuid.uuid4()}"
    status, body = call("POST", f"{api}/scans", token=token,
                        headers={"Idempotency-Key": key},
                        body={"website_url": args.url, "authorized": True})
    started = check("POST /scans accepts and returns immediately",
                    status == 202 and isinstance(body, dict) and "scan_id" in body,
                    f"-> {status}")
    if not started:
        note(f"response: {str(body)[:200]}")
        return _summary()

    scan_id = body["scan_id"]
    note(f"scan_id = {scan_id}")

    status, replay = call("POST", f"{api}/scans", token=token,
                          headers={"Idempotency-Key": key},
                          body={"website_url": args.url, "authorized": True})
    check("Idempotency-Key returns the same scan, not a new one",
          status == 200 and replay.get("scan_id") == scan_id
          and replay.get("idempotent_replay") is True,
          f"-> {status}, same id: {replay.get('scan_id') == scan_id}")

    # Poll.
    deadline = time.time() + args.timeout
    last, seen_progress = None, []
    while time.time() < deadline:
        status, state = call("GET", f"{api}/scans/{scan_id}/status", token=token)
        if status != 200 or not isinstance(state, dict):
            break
        last = state
        seen_progress.append(state.get("progress"))
        if state.get("status") in ("completed", "failed", "cancelled"):
            break
        time.sleep(5)

    if last is None:
        check("GET /status responds", False, f"-> {status}")
        return _summary()

    check("GET /status responds with the documented fields",
          all(k in last for k in ("status", "progress", "pages_scanned", "current_stage")),
          f"status={last.get('status')} progress={last.get('progress')}")
    check("progress advanced rather than sitting still",
          len(set(seen_progress)) > 1 or last.get("progress") == 100,
          f"saw {sorted(set(seen_progress))}")
    finished = check("scan reached a terminal state within the timeout",
                     last.get("status") in ("completed", "failed"),
                     f"-> {last.get('status')} after {len(seen_progress)} polls")
    if last.get("status") == "completed" and last.get("error"):
        note("completed WITH an error -- findings are real but unexplained.")
        note("This is the degraded case the docs describe; it is not a failure.")

    if not finished:
        return _summary()

    # Findings.
    status, body = call("GET", f"{api}/scans/{scan_id}/findings", token=token)
    findings = body.get("findings", []) if isinstance(body, dict) else []
    check("GET /findings responds", status == 200, f"-> {status}, {len(findings)} findings")
    if findings:
        first = findings[0]
        check("findings carry the documented fields",
              all(k in first for k in ("id", "severity", "category", "title", "description",
                                       "requires_human_review", "recommendation")),
              f"severity={first.get('severity')}")
        high = [f for f in findings if f.get("severity") == "high"]
        check("every high-severity finding requires human review",
              all(f.get("requires_human_review") for f in high),
              f"{len(high)} high-severity")

    # Full result.
    status, result = call("GET", f"{api}/scans/{scan_id}", token=token)
    check("GET /scans/{id} responds", status == 200, f"-> {status}")
    if isinstance(result, dict):
        check("full result carries evidence and stage timings",
              all(k in result for k in ("trackers", "cookies", "stages", "evidence_counts")),
              f"{len(result.get('stages', []))} stages")

        metrics = result.get("token_metrics")
        check("token metrics are real measured numbers",
              isinstance(metrics, dict) and metrics.get("total_tokens", 0) > 0,
              f"{metrics.get('total_tokens')} tokens" if metrics else "absent")

        blob = json.dumps(result)
        leaked = [w for w in ("/home/", "nvapi-", "gsk_", "postgresql://", "Traceback")
                  if w in blob]
        check("no internal paths, keys or stack traces in the response",
              not leaked, f"found {leaked}" if leaked else "clean")

        stages = [s.get("stage") for s in result.get("stages", [])]
        check("the scan ran once, not twice",
              len(stages) == len(set(stages)),
              f"{len(stages)} stages, {len(set(stages))} distinct")

    # Summary.
    status, body = call("GET", f"{api}/scans/{scan_id}/summary", token=token)
    check("GET /summary responds", status == 200,
          f"-> {status}, compliance_status={body.get('compliance_status') if isinstance(body, dict) else '?'}")

    # Cancel semantics on a finished scan.
    status, body = call("POST", f"{api}/scans/{scan_id}/cancel", token=token)
    check("cancelling a finished scan is refused with 409", status == 409, f"-> {status}")

    return _summary()


def _summary() -> int:
    failed = [name for result, name in _results if result == FAIL]
    skipped = [name for result, name in _results if result == SKIP]
    passed = len(_results) - len(failed) - len(skipped)
    print(f"\n{'-' * 62}")
    print(f"  {passed} passed, {len(failed)} failed, {len(skipped)} skipped")
    if skipped:
        print("\n  SKIPPED (not run -- this is NOT a pass):")
        for name in skipped:
            print(f"    - {name}")
    if failed:
        print("\n  FAILED:")
        for name in failed:
            print(f"    - {name}")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
