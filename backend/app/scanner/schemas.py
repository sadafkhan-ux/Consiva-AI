"""Structured scan evidence — the ONLY thing that crosses from the scanner into the
rest of the pipeline. Raw HTML/DOM never leaves this module (docs/architecture §5/§I).

Every record carries a `local_id` (stable within one scan, e.g. "cookie-3") so rules
and LLM output can cite specific evidence before anything has a database UUID.
"""

from typing import Literal

from pydantic import BaseModel, Field


class PageRecord(BaseModel):
    local_id: str
    url: str
    title: str | None = None
    http_status: int | None = None
    # "seed" | "link" | "sitemap" (how it was discovered, page loaded successfully) --
    # or, when the fetch itself did not succeed (never silently dropped from `pages`,
    # per the scanner-hardening audit's section 4): "robots_disallowed" | "failed" |
    # "timeout". http_status is None in all of the failure cases.
    discovered_via: str | None = None


class FormField(BaseModel):
    name: str
    field_type: str
    required: bool = False


class FormRecord(BaseModel):
    local_id: str
    page_local_id: str
    selector: str
    action_url: str | None = None
    method: str | None = None
    fields: list[FormField] = Field(default_factory=list)
    purpose_guess: str | None = None  # set by form_detector heuristics, not the LLM


class CookieRecord(BaseModel):
    local_id: str
    name: str
    domain: str | None = None
    path: str | None = None
    expiry: str | None = None  # ISO timestamp or None (session cookie)
    is_first_party: bool | None = None
    set_by_tracker_local_id: str | None = None
    # category/vendor/source are filled in later by rules/consent_rules.py, not the scanner
    category: str | None = None
    vendor: str | None = None
    source: str | None = None  # "lookup" | "rule" | "llm_interpretation" | "human_confirmed"
    # Which of the three scan passes (pre_consent/post_accept/post_reject) this cookie
    # was observed in — set by the crawler, not classification.
    consent_states: list[str] = Field(default_factory=list)


class TrackerRecord(BaseModel):
    local_id: str
    page_local_id: str | None = None
    script_src: str
    vendor: str | None = None
    category: str | None = None
    source: str | None = None
    consent_states: list[str] = Field(default_factory=list)
    # Playwright's own request classification ("script"/"xhr"/"image"/"font"/...) --
    # scan-time-only signal for classify_tracker()'s static-asset heuristic, not
    # persisted to the DB (Tracker has no such column; nothing downstream needs it
    # once classification has consumed it into vendor/category).
    resource_type: str | None = None


class ThirdPartyServiceRecord(BaseModel):
    local_id: str
    service_name: str
    category: str | None = None
    domains: list[str] = Field(default_factory=list)
    detection_method: str  # "script_src" | "dns" | "cmp_signature" | ...


class PolicyRecord(BaseModel):
    local_id: str
    url: str
    policy_type: Literal["privacy_policy", "cookie_policy", "terms", "other"]
    extracted_text_ref: str | None = None  # pointer into evidence store, not full text


class ConsentSignalRecord(BaseModel):
    mechanism_type: Literal["banner", "cmp", "none", "unknown"]
    cmp_vendor: str | None = None
    has_reject_all: bool | None = None
    has_granular_choices: bool | None = None
    evidence: dict = Field(default_factory=dict)


class ScanResult(BaseModel):
    domain: str
    root_url: str
    scanner_version: str
    pages: list[PageRecord] = Field(default_factory=list)
    forms: list[FormRecord] = Field(default_factory=list)
    cookies: list[CookieRecord] = Field(default_factory=list)
    trackers: list[TrackerRecord] = Field(default_factory=list)
    third_party_services: list[ThirdPartyServiceRecord] = Field(default_factory=list)
    policies: list[PolicyRecord] = Field(default_factory=list)
    consent_signals: ConsentSignalRecord
    # Real, observed scan-run diagnostics (retry counts, scroll steps, page failure
    # counts by kind) -- never fabricated, surfaced by scan_service.py into the
    # existing per-stage `agent_run_stages.metadata` via track_stage(). Not evidence
    # about the target site; operational data about how this particular scan ran.
    scan_diagnostics: dict = Field(default_factory=dict)
