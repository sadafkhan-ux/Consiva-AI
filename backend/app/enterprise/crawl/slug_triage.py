"""Ask the model which of a site's addresses probably hold a policy.

The crawler's own patterns know /privacy, /cookie-policy, /terms. They do not know /p/12, /trust/dp, /legal-centre/data or a translated address. The model is given the addresses (paths only, numbered)
and answers, as JSON, which probably hold a privacy policy, a cookie policy, terms or a grievance page, and how sure it is. An address may hold several kinds (/legal can hold all four).

An address is only a clue. A flagged address is read EARLY (crawl priority 0) and the page's own content then decides what it is; nothing is classified from the address alone. Addresses under a blog or
news section are not sent. A failure to ask or to answer costs nothing but the hint: the crawl goes on.
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel
from sqlalchemy import text

from app.db.session import async_session_factory
from app.enterprise.config import get_enterprise_settings
from app.enterprise.crawl import scope

logger = logging.getLogger(__name__)

BATCH = 400
Kind = Literal["privacy_policy", "cookie_policy", "terms", "grievance"]


class SlugItem(BaseModel):
    n: int
    kinds: list[Kind]
    confidence: Literal["high", "medium", "low"]


class SlugAnswer(BaseModel):
    items: list[SlugItem]


SYSTEM = (
    "You are given numbered web page addresses (paths only) from one company's website. You cannot see the pages. For each address that probably holds a legal or privacy document, say which kinds it "
    "probably holds: privacy_policy, cookie_policy, terms, grievance (a grievance officer, complaints or data protection officer page). An address can hold more than one kind: a /legal page may hold "
    "all of them. Addresses may be in any language or be opaque (/p/12). Return ONLY addresses that probably hold at least one of these kinds, with your confidence. Do not return addresses for "
    "products, services, blog posts, news, careers, or general pages. Answer with JSON like {\"items\": [{\"n\": 3, \"kinds\": [\"privacy_policy\"], \"confidence\": \"high\"}]}."
)


def build_user(paths: list[str]) -> str:
    return "Addresses:\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(paths, start=1))


def _path(url: str) -> str:
    u = urlparse(url)
    return (u.path or "/") + (f"?{u.query}" if u.query else "")


async def triage(scan_id: uuid.UUID, *, llm=None) -> dict:
    """Read the pending addresses of a scan that no pattern ranks first, flag the ones that look like policies, and raise their crawl priority. Returns what it did."""
    settings = get_enterprise_settings()
    out = {"asked": 0, "flagged": 0, "calls": 0}
    if not settings.slug_triage or settings.slug_triage_max_urls == 0:
        return out
    from app.enterprise.analysis.llm import call_structured, default_llm
    llm = llm or default_llm(scan_id=None, agent="enterprise_slugs")
    remaining = settings.slug_triage_max_urls
    while remaining > 0:
        async with async_session_factory() as db:
            rows = (await db.execute(text(
                "select id, original_url from ent_frontier where scan_id = :s and status = 'pending' and priority = 2 and slug_kinds is null order by depth, discovered_at limit :n"),
                {"s": scan_id, "n": min(BATCH, remaining)})).all()
            if not rows:
                break
            ask = [r for r in rows if not scope.is_article_path(r.original_url)]
            skip = [r.id for r in rows if scope.is_article_path(r.original_url)]
            hits: dict[int, SlugItem] = {}
            if ask:
                try:
                    answer = await call_structured(llm, SYSTEM, build_user([_path(r.original_url) for r in ask]), SlugAnswer)
                    hits = {i.n: i for i in answer.parsed.items if 1 <= i.n <= len(ask)}            # type: ignore[attr-defined]
                    out["calls"] += 1
                except Exception as exc:                                  # no model, no key, a bad answer: the hint is lost, the crawl is not
                    logger.warning("enterprise scan %s: the addresses could not be read by the model (%s)", scan_id, type(exc).__name__)
                    for r in rows:
                        await db.execute(text("update ent_frontier set slug_kinds = '{}' where id = :i"), {"i": r.id})
                    await db.commit()
                    return out
            for i, r in enumerate(ask, start=1):
                h = hits.get(i)
                useful = h is not None and bool(h.kinds) and h.confidence != "low"
                # A flagged privacy policy is read with the privacy policies (0); a flagged cookie, terms or grievance page with the other legal pages (1).
                new_priority = (0 if "privacy_policy" in h.kinds else 1) if useful else None
                await db.execute(text("update ent_frontier set slug_kinds = :k, priority = coalesce(:p, priority) where id = :i and status = 'pending'"),
                                 {"k": list(h.kinds) if useful else [], "p": new_priority, "i": r.id})
                out["flagged"] += 1 if useful else 0
            for rid in skip:
                await db.execute(text("update ent_frontier set slug_kinds = '{}' where id = :i"), {"i": rid})
            await db.commit()
            out["asked"] += len(ask)
            remaining -= len(rows)
    return out


async def triage_safe(scan_id: uuid.UUID) -> None:
    try:
        result = await triage(scan_id)
        if result["asked"]:
            logger.info("enterprise scan %s: addresses read by the model: %s", scan_id, result)
    except Exception:
        logger.warning("enterprise scan %s: address triage failed", scan_id, exc_info=True)
