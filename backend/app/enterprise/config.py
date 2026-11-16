"""Enterprise scanner settings: ENTERPRISE_* variables, read from the same backend/.env as the rest of the application.

A separate class, so the existing Settings (app/config.py) is untouched. Secrets (database, JWT, LLM keys) are NOT repeated here: they stay in their one existing place.
An out-of-range value stops the application from starting, as the CONSENT_* settings do, so a typo never silently widens a crawl.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnterpriseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ENTERPRISE_", extra="ignore")

    #: The whole Enterprise product is off until this is true. Nothing in it runs, and no route is served, while it is false.
    enabled: bool = False
    #: Most distinct URLs recorded for one scan. 0 = no cap. The default is a ceiling, not a target: a scan that reaches it says so (`stop_reason` = url_limit) and lists the rest as pending.
    max_urls: int = Field(default=100_000, ge=0, le=10_000_000)
    #: Deepest link level crawled (the entry address is 0). 0 is valid: the entry page only.
    max_depth: int = Field(default=50, ge=0, le=1_000)
    #: Pages in flight at once for one scan.
    concurrency: int = Field(default=4, ge=1, le=256)
    #: Navigation starts per second against one domain.
    per_domain_rps: float = Field(default=2.0, gt=0, le=1_000)
    #: Pause launching pages while the scan (this process and its browsers) uses more memory than this; if it stays over for five minutes the crawl stops with `memory_limit`. 0 = no budget.
    memory_budget_mb: int = Field(default=4096, ge=0, le=1_048_576)
    #: Longest a crawl may run before it stops with `time_limit` (the pages it did not reach stay pending and it can be resumed). 0 = no deadline.
    max_runtime_seconds: int = Field(default=43_200, ge=0, le=2_592_000)
    #: A claimed URL not finished within this many seconds is taken back (a crashed worker's pages), which is what makes a scan resumable.
    claim_lease_seconds: int = Field(default=300, ge=30, le=86_400)
    #: Tries for one URL before it is recorded as failed.
    max_attempts: int = Field(default=3, ge=1, le=10)
    #: Largest linked document (PDF, DOCX) fetched, in megabytes. A larger one is recorded as skipped with the reason, never cut.
    max_document_mb: int = Field(default=25, ge=1, le=500)
    #: Largest single piece of content kept as evidence, in bytes. Longer content is refused, never silently cut.
    max_evidence_bytes: int = Field(default=5_000_000, ge=1_000, le=100_000_000)


@lru_cache
def get_enterprise_settings() -> EnterpriseSettings:
    return EnterpriseSettings()
