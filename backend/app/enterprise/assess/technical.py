"""Technical parameters TS1 to TS6, assessed from stored evidence by fixed rules. No model is involved: every result is a count of what the browser and the server showed.

These are the six SUPPORTING parameters of the catalogue. Rule 6(1) of the DPDP Rules names no website control, so they are technical signals that relate to the duty of reasonable
security safeguards (s.8(5)); a failure here is a technical observation, never a finding that the Act was breached.

The rules below are the first reading of each parameter's pass, partial and fail. They are objective, but they have not yet been reviewed parameter by parameter (see
docs/ENTERPRISE_SCORING_METHOD.md), and they are written here, in one place, so that review can change them.

  TS1  HTTPS and no mixed content   met: every crawled page's final address is https and none made a request in clear text.  not met: any page not https, or any clear-text request.
                                    not assessed: no page was read, or the evidence predates mixed-content recording.
  TS2  Valid certificate, over 30 days left   met: the certificate verified and expires more than 30 days after the assessment date. not met: it did not verify, or 30 days or fewer.
                                    not assessed: the server could not be reached or was not probed.
  TS3  TLS 1.2 or 1.3               met: the negotiated protocol is TLS 1.2 or 1.3. not met: anything older. Older versions are not probed. not assessed: not probed.
  TS4  Five protective headers      HSTS, CSP, X-Content-Type-Options, Referrer-Policy, Permissions-Policy; a header counts only if every crawled page sent it.
                                    met: all five. partly met: three or four. not met: two or fewer. not assessed: no page was read.
  TS5  Cookie attributes            every distinct cookie (name, domain, path) is Secure and has a SameSite value. HttpOnly is not required: analytics cookies are read by script by design.
                                    met: all. partly met: at least half. not met: fewer. not applicable: no cookie was observed.
  TS6  Redirects stay on HTTPS      not met: any redirect chain that goes from an https address to an http one. met: redirects were observed and none did. not applicable: none observed.
"""

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import urlparse

from app.enterprise.scoring.engine import (
    MET, NOT_APPLICABLE, NOT_ASSESSED, NOT_MET, PARTLY, Assessment,
)

Ev = tuple[str, dict]          # (evidence id, decoded content)
FIVE_HEADERS = ("strict-transport-security", "content-security-policy", "x-content-type-options", "referrer-policy", "permissions-policy")
MAX_REFS = 25


def _refs(items: list[Ev]) -> tuple[str, ...]:
    return tuple(f"ev:{i}" for i, _ in items[:MAX_REFS])


def _ts1(pages: list[Ev], networks: list[Ev]) -> Assessment:
    if not pages:
        return Assessment("TS1", NOT_ASSESSED, note="no page was read")
    plain = [e for e in pages if not e[1]["security"]["https"]]
    if plain:
        return Assessment("TS1", NOT_MET, _refs(plain), note=f"{len(plain)} of {len(pages)} pages were served over http")
    if any("insecure_requests" not in n for _, n in networks) or not networks:
        return Assessment("TS1", NOT_ASSESSED, note="every page was https, but mixed content was not recorded in this evidence")
    bad = [e for e in networks if e[1]["insecure_requests"] > 0]
    if bad:
        return Assessment("TS1", NOT_MET, _refs(bad), note=f"{len(bad)} pages made requests in clear text, e.g. {bad[0][1].get('insecure_examples', [''])[0]}")
    return Assessment("TS1", MET, _refs(pages), note=f"all {len(pages)} pages were https and none made a clear-text request")


def _ts2(tls: Ev | None, as_of: date) -> Assessment:
    if tls is None or not tls[1].get("reachable"):
        return Assessment("TS2", NOT_ASSESSED, note="the server's certificate could not be read")
    ref = (f"ev:{tls[0]}",)
    t = tls[1]
    if not t.get("verified"):
        return Assessment("TS2", NOT_MET, ref, note=f"the certificate did not verify: {t.get('verify_error')}")
    days = (datetime.fromisoformat(t["not_after"]).date() - as_of).days
    if days > 30:
        return Assessment("TS2", MET, ref, note=f"valid until {t['not_after'][:10]} ({days} days after {as_of})")
    return Assessment("TS2", NOT_MET, ref, note=f"expires {t['not_after'][:10]}, {days} days after {as_of}")


def _ts3(tls: Ev | None) -> Assessment:
    if tls is None or not tls[1].get("negotiated"):
        return Assessment("TS3", NOT_ASSESSED, note="the protocol version could not be read")
    version = tls[1]["negotiated"]
    ok = version in ("TLSv1.2", "TLSv1.3")
    return Assessment("TS3", MET if ok else NOT_MET, (f"ev:{tls[0]}",), note=f"{version} negotiated; TLS 1.3 {'offered' if tls[1].get('tls13_supported') else 'not offered'}")


def _ts4(pages: list[Ev]) -> Assessment:
    if not pages:
        return Assessment("TS4", NOT_ASSESSED, note="no page was read")
    present = [h for h in FIVE_HEADERS if all(h in e[1]["security"]["present"] for e in pages)]
    missing = [h for h in FIVE_HEADERS if h not in present]
    state = MET if len(present) == 5 else PARTLY if len(present) >= 3 else NOT_MET
    return Assessment("TS4", state, _refs(pages), note=f"{len(present)} of 5 headers on every page" + (f"; missing on at least one page: {', '.join(missing)}" if missing else ""))


def _ts5(cookies: list[Ev]) -> Assessment:
    seen: dict[str, dict] = {}
    for _, e in cookies:
        for c in e.get("cookies", []):
            seen[c["identity"]] = c
    if not seen:
        return Assessment("TS5", NOT_APPLICABLE, note="no cookie was observed on any crawled page")
    ok = [c for c in seen.values() if c["secure"] and c["same_site"] != "unspecified"]
    state = MET if len(ok) == len(seen) else PARTLY if len(ok) * 2 >= len(seen) else NOT_MET
    return Assessment("TS5", state, _refs(cookies), note=f"{len(ok)} of {len(seen)} distinct cookies are Secure and set SameSite")


def _ts6(pages: list[Ev]) -> Assessment:
    chains = [e for e in pages if e[1].get("redirect_chain")]
    if not chains:
        return Assessment("TS6", NOT_APPLICABLE, note="no redirect was observed")
    downgrades = []
    for e in chains:
        urls = [h["url"] for h in e[1]["redirect_chain"]] + [e[1]["page_url"]]
        if any(urlparse(a).scheme == "https" and urlparse(b).scheme == "http" for a, b in zip(urls, urls[1:])):
            downgrades.append(e)
    if downgrades:
        return Assessment("TS6", NOT_MET, _refs(downgrades), note=f"{len(downgrades)} redirect chains moved from https to http")
    return Assessment("TS6", MET, _refs(chains), note=f"{len(chains)} redirect chains were observed and none left https")


def assess_technical(*, pages: list[Ev], cookies: list[Ev], networks: list[Ev], tls: Ev | None, as_of: date) -> list[Assessment]:
    """`pages` are the per-page response records, `cookies` and `networks` the per-page cookie and request records, `tls` the one TLS probe."""
    return [_ts1(pages, networks), _ts2(tls, as_of), _ts3(tls), _ts4(pages), _ts5(cookies), _ts6(pages)]
