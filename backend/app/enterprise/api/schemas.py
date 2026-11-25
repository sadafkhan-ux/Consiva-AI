"""Request and response models of the Enterprise API. Each response is derived from the Enterprise tables only."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class CreateEnterpriseScan(BaseModel):
    website_url: str = Field(..., description="Address of the site to scan; a bare domain is accepted. DNS ownership verification is required only where the deployment requires it, as for the 5-page scan.")
    authorized: bool = Field(default=False, description="Explicit attestation that you own or are permitted to scan this domain. Required.")

    @field_validator("website_url")
    @classmethod
    def _absolute(cls, value: str) -> str:
        """A bare domain ("example.com") is accepted and promoted to https://, as the consent scan does: people type what they say out loud. http:// is kept when written explicitly.
        The host must contain a dot, so a typo stays a clean 422. Whether the address is SAFE to fetch is decided later by the SSRF check, whatever is written here."""
        candidate = value.strip()
        if not candidate:
            raise ValueError("website_url must not be empty")
        if "://" not in candidate:
            candidate = f"https://{candidate}"
        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("website_url must be an http:// or https:// address, for example https://example.com")
        if not parsed.hostname or "." not in parsed.hostname:
            raise ValueError("website_url must include a full domain name, for example example.com")
        return candidate


class CreateEnterpriseScanResponse(BaseModel):
    scan_id: uuid.UUID
    status: str
    message: str
    options: dict


class UrlCounts(BaseModel):
    pending: int = 0
    in_progress: int = 0
    done: int = 0
    failed: int = 0
    excluded: int = 0
    total: int = 0


class EnterpriseScanStatus(BaseModel):
    scan_id: uuid.UUID
    website_url: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    urls: UrlCounts
    progress: int = Field(..., ge=0, le=100, description="Share of the discovered URLs that are no longer waiting. Discovery continues while the scan runs, so it can fall as links are found.")
    stop_reason: str | None
    error: str | None
    options: dict
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    data_cleared: bool = Field(False, description="True once the scan-data cleanup has deleted this scan's results; its detail routes then answer 410.")


class CoverageUrl(BaseModel):
    url: str
    depth: int
    status: str
    discovery_sources: list[str]
    http_status: int | None
    status_reason: str | None
    last_error: str | None


class CoverageResponse(BaseModel):
    scan_id: uuid.UUID
    urls: UrlCounts
    items: list[CoverageUrl]
    total: int
    limit: int
    offset: int
    note: str = "Discovered is not scanned. Pages behind a login, blocked by robots.txt, or not linked or listed anywhere cannot be reached, and the scan does not claim to have covered them."


class ActionResponse(BaseModel):
    scan_id: uuid.UUID
    status: str
    message: str
