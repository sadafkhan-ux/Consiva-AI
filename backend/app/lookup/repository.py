"""DB access for the cookie lookup table. Matching logic itself lives in matcher.py
so rules/consent_rules.py can stay a pure function — this module only does I/O."""

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CookieLookup
from app.lookup.matcher import LookupEntry

# load_all() was being refetched in full (2,266 rows) on every single scan -- live-
# measured at ~1.4s -- for reference data that only changes when a reviewer confirms an
# unknown classification (upsert_human_confirmed, called rarely, from review_service).
# A short TTL cache trades a few minutes of eventual consistency for cutting that cost
# to ~0 on every scan but the first per cache window; not a permanent/unbounded cache,
# so a human correction still shows up for new scans within _CACHE_TTL_SECONDS.
_CACHE_TTL_SECONDS = 300
_cache: list[LookupEntry] | None = None
_cache_loaded_at: float = 0.0


async def load_all(db: AsyncSession) -> list[LookupEntry]:
    global _cache, _cache_loaded_at
    now = time.monotonic()
    if _cache is not None and (now - _cache_loaded_at) < _CACHE_TTL_SECONDS:
        return _cache

    result = await db.execute(select(CookieLookup))
    entries = [
        LookupEntry(
            name_pattern=row.name_pattern,
            is_prefix_pattern=row.is_prefix_pattern,
            category=row.category,
            vendor=row.vendor,
            domain_pattern=row.domain_pattern,
        )
        for row in result.scalars().all()
    ]
    _cache = entries
    _cache_loaded_at = now
    return entries


async def upsert_human_confirmed(
    db: AsyncSession, *, name_pattern: str, category: str, vendor: str | None = None
) -> CookieLookup:
    """Build Plan Component 3: "store the human-confirmed classification back into
    the lookup table so the system learns." Called from review_service when a
    reviewer corrects an unknown/incorrect cookie classification."""
    existing = await db.execute(
        select(CookieLookup).where(CookieLookup.is_prefix_pattern.is_(False), CookieLookup.name_pattern == name_pattern)
    )
    row = existing.scalars().first()
    if row:
        row.category = category
        row.vendor = vendor or row.vendor
        row.source = "human_confirmed"
    else:
        row = CookieLookup(
            name_pattern=name_pattern, is_prefix_pattern=False, vendor=vendor,
            category=category, source="human_confirmed",
        )
        db.add(row)
    await db.flush()
    global _cache
    _cache = None  # invalidate so the next load_all() in this process sees the correction immediately
    return row
