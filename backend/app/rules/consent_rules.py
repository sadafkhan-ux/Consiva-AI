"""Deterministic consent classification and rule checks — docs/architecture §10 Phase 1
("deterministic lookup, rules, human review... before any ML"). Runs before the LLM is
ever called; the LLM only reasons over what these rules already established.

Two distinct stages, matching docs/architecture §D/§E:
- `classify_scan()` runs at SCAN time (in scan_service, right after crawling): tags
  vendor/category onto raw evidence so it's immediately useful once persisted.
- `evaluate_consent_rules()` runs at ANALYZE time (in the classify_rules agent node):
  produces the actual risk findings, reading the now-persisted (already-tagged)
  evidence back out of the database as plain dicts.
"""

import tldextract
from pydantic import BaseModel, Field

from app.lookup.matcher import LookupEntry, match_cookie_lookup
from app.rules.tracker_catalog import match_vendor
from app.scanner.schemas import CookieRecord, ScanResult, ThirdPartyServiceRecord, TrackerRecord

PERSONAL_DATA_FIELD_HINTS = (
    "email", "phone", "mobile", "name", "address", "dob", "birth", "pan", "aadhaar", "passport",
)


class RuleFinding(BaseModel):
    rule_id: str  # "R-001" style, per master prompt §6
    rule_version: str = "1.0"
    category: str  # "analytics" | "marketing" | "functional" | "other"
    risk_level: str  # "low" | "medium" | "high"
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: str  # "high" | "low" — "low" routes to the LLM/RAG for a narrative + always to human review


class RulesClassification(BaseModel):
    cookies: list[CookieRecord]
    trackers: list[TrackerRecord]
    third_party_services: list[ThirdPartyServiceRecord]


def classify_cookie(cookie: CookieRecord, lookup_entries: list[LookupEntry] = ()) -> CookieRecord:
    """DB-backed lookup (Open Cookie Database) first — Build Plan Component 3, Layer 1 —
    falling back to the hardcoded vendor-signature catalog for anything the cookie
    dataset doesn't cover. `lookup_entries` defaults to empty so this stays callable
    (and unit-testable) without a DB session."""
    match = match_cookie_lookup(cookie.name, list(lookup_entries))
    if match is not None:
        return cookie.model_copy(update={"vendor": match.vendor, "category": match.category, "source": "lookup"})

    sig = match_vendor(domain=cookie.domain, cookie_name=cookie.name)
    if sig is None:
        return cookie
    return cookie.model_copy(update={"vendor": sig.vendor, "category": sig.default_category, "source": "rule"})


_STATIC_ASSET_RESOURCE_TYPES = frozenset({"image", "media", "font"})


def _is_likely_static_asset(tracker: TrackerRecord) -> bool:
    """A real live scan found the large majority of "unclassified trackers" on a real
    site were plain product-image files served from the site's own CDN bucket (no
    query string, no script/XHR behavior) -- flagged identically to an actual tracking
    script/pixel by the raw content-agnostic detector (tracker_detector.py), which
    inflated "could not classify" findings with noise rather than real risk signal.
    A REAL tracking pixel (Meta/Google Ads 1x1 gifs, analytics beacons) is also often
    an "image"-type request, so resource_type alone isn't a safe filter -- those
    consistently carry a query string (tracking ids/session params), which a plain
    static asset URL essentially never does. Requiring BOTH signals together is what
    keeps this from silently reclassifying a real disguised-as-image tracking pixel."""
    return tracker.resource_type in _STATIC_ASSET_RESOURCE_TYPES and "?" not in tracker.script_src


def classify_tracker(tracker: TrackerRecord) -> TrackerRecord:
    """No script-src data exists in the (cookie-only) Open Cookie Database, so trackers
    classify via the vendor-signature catalog first, falling back to a static-asset
    heuristic (see _is_likely_static_asset) for anything the catalog doesn't cover --
    unlike a real vendor match, this doesn't identify WHO the resource belongs to, only
    WHAT KIND of request it is, so it's recorded with a distinct vendor label rather
    than pretending to be a specific vendor classification."""
    sig = match_vendor(script_src=tracker.script_src)
    if sig is not None:
        return tracker.model_copy(update={"vendor": sig.vendor, "category": sig.default_category, "source": "rule"})
    if _is_likely_static_asset(tracker):
        return tracker.model_copy(update={"vendor": "Static CDN Asset", "category": "functional", "source": "rule"})
    return tracker


def _registered_domain(url: str) -> str:
    ext = tldextract.extract(url)
    return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain


def derive_third_party_services(trackers: list[TrackerRecord]) -> list[ThirdPartyServiceRecord]:
    """Rolls up individual tracker script observations into one record per vendor (or,
    for unrecognized scripts, per registered domain) — the granularity the required
    scan output's `third_party_services` list is meant to convey."""
    grouped: dict[str, ThirdPartyServiceRecord] = {}
    for i, tracker in enumerate(trackers):
        domain = _registered_domain(tracker.script_src)
        key = tracker.vendor or domain
        if key not in grouped:
            grouped[key] = ThirdPartyServiceRecord(
                local_id=f"service-{i}",
                service_name=key,
                category=tracker.category,
                domains=[],
                detection_method="script_src",
            )
        if domain not in grouped[key].domains:
            grouped[key].domains.append(domain)
    return list(grouped.values())


def classify_scan(scan: ScanResult, lookup_entries: list[LookupEntry] = ()) -> RulesClassification:
    trackers = [classify_tracker(t) for t in scan.trackers]
    return RulesClassification(
        cookies=[classify_cookie(c, lookup_entries) for c in scan.cookies],
        trackers=trackers,
        third_party_services=derive_third_party_services(trackers),
    )


def _has_personal_data_fields(fields: list[dict]) -> bool:
    return any(
        any(hint in (field.get("name") or "").lower() for hint in PERSONAL_DATA_FIELD_HINTS)
        for field in fields
    )


def evaluate_consent_rules(evidence: dict) -> list[RuleFinding]:
    """`evidence` is the dict shape produced by
    scan_repository.get_scan_evidence_summary() — real DB ids, already vendor/category
    classified. Called from agents/consent_agent/nodes/classify_rules.py."""
    findings: list[RuleFinding] = []

    cookies = evidence["cookies"]
    trackers = evidence["trackers"]
    forms = evidence["forms"]
    policy_types = {p["policy_type"] for p in evidence["policies"]}
    signals = evidence["consent_signals"][0] if evidence["consent_signals"] else None
    mechanism_type = signals["mechanism_type"] if signals else "unknown"

    non_essential = [c for c in cookies if c["category"] in ("analytics", "marketing")] + [
        t for t in trackers if t["category"] in ("analytics", "marketing")
    ]

    # R-001 / R-003 depend on the three-pass consent-state scan (crawler.py). Both stay
    # silent (not "no violation found") until an item is actually observed in that state —
    # `consent_states` defaults to [] for scans/fixtures that predate three-pass scanning.
    pre_consent_hits = [e for e in non_essential if "pre_consent" in e.get("consent_states", [])]
    if pre_consent_hits:
        findings.append(RuleFinding(
            rule_id="R-001",
            category="other",
            risk_level="high",
            summary=(
                f"{len(pre_consent_hits)} analytics/marketing cookie(s)/script(s) fired BEFORE any "
                "consent interaction — the most common real DPDP violation."
            ),
            evidence_ids=[e["id"] for e in pre_consent_hits],
            confidence="high",
        ))

    if non_essential and mechanism_type == "none":
        findings.append(RuleFinding(
            rule_id="R-002",
            category="other",
            risk_level="high",
            summary=(
                f"{len(non_essential)} analytics/marketing cookie(s)/script(s) detected but no "
                "consent banner or CMP was found on the scanned pages."
            ),
            evidence_ids=[e["id"] for e in non_essential],
            confidence="high",
        ))

    post_reject_hits = [e for e in non_essential if "post_reject" in e.get("consent_states", [])]
    if post_reject_hits:
        findings.append(RuleFinding(
            rule_id="R-003",
            category="other",
            risk_level="high",
            summary=(
                f"{len(post_reject_hits)} analytics/marketing cookie(s)/script(s) continued firing "
                "AFTER the visitor clicked Reject."
            ),
            evidence_ids=[e["id"] for e in post_reject_hits],
            confidence="high",
        ))

    for form in forms:
        if _has_personal_data_fields(form["fields"]) and mechanism_type == "none":
            findings.append(RuleFinding(
                rule_id="R-004",
                category="functional",
                risk_level="medium",
                summary=f"Form '{form['id']}' collects fields resembling personal data with no "
                        "detected consent mechanism on the page.",
                evidence_ids=[form["id"]],
                confidence="low",
            ))

    if signals and mechanism_type in ("banner", "cmp") and signals["has_reject_all"] is False:
        findings.append(RuleFinding(
            rule_id="R-005",
            category="other",
            risk_level="medium",
            summary="A consent mechanism was detected but no equally prominent 'reject all' option was found.",
            confidence="high",
        ))

    if "privacy_policy" not in policy_types:
        findings.append(RuleFinding(
            rule_id="R-006",
            category="other",
            risk_level="high",
            summary="No privacy policy page was discovered during the crawl.",
            confidence="high",
        ))

    if non_essential and "cookie_policy" not in policy_types:
        findings.append(RuleFinding(
            rule_id="R-007",
            category="other",
            risk_level="medium",
            summary="Tracking cookies/scripts were detected but no dedicated cookie policy was found.",
            evidence_ids=[e["id"] for e in non_essential],
            confidence="high",
        ))

    unclassified = [t for t in trackers if t["category"] is None] + [c for c in cookies if c["category"] is None]
    if unclassified:
        findings.append(RuleFinding(
            rule_id="R-008",
            category="other",
            risk_level="medium",
            summary=f"{len(unclassified)} script(s)/cookie(s) did not match any known vendor signature "
                    "and could not be automatically classified.",
            evidence_ids=[e["id"] for e in unclassified],
            confidence="low",
        ))

    # R-009 / R-010 depend on the three-pass scanner's interaction outcome (crawler.py
    # scan-hardening audit) -- "not just whether a mechanism exists, but whether it
    # could actually be exercised." `evidence` defaults to {} for scans/fixtures that
    # predate this being surfaced, so both stay silent rather than false-positive.
    interaction_evidence = (signals or {}).get("evidence", {}) or {}
    reject_outcome = interaction_evidence.get("reject_interaction")
    accept_outcome = interaction_evidence.get("accept_interaction")

    if mechanism_type in ("banner", "cmp") and reject_outcome in ("cmp_not_automatable", "click_failed"):
        findings.append(RuleFinding(
            rule_id="R-009",
            category="other",
            risk_level="medium",
            summary=(
                f"A consent mechanism ({mechanism_type}"
                + (f", {signals.get('cmp_vendor')}" if signals.get("cmp_vendor") else "")
                + f") was detected, but its Reject control could not be automated ({reject_outcome}) -- "
                "either a non-standard/bespoke banner, or Reject is genuinely harder to reach than Accept."
            ),
            confidence="low",  # a scanner automation limitation, not confirmed non-compliance -- routes to human review
        ))

    untested_states = [
        state for state, outcome in (("post_accept", accept_outcome), ("post_reject", reject_outcome))
        if outcome == "page_unreachable"
    ]
    if untested_states:
        findings.append(RuleFinding(
            rule_id="R-010",
            category="other",
            risk_level="medium",
            summary=(
                f"The following consent state(s) could not be tested because the page was unreachable "
                f"during that pass: {', '.join(untested_states)}. This scan's evidence for those states is "
                "incomplete, not a confirmed absence of tracking."
            ),
            confidence="low",
        ))

    return findings
