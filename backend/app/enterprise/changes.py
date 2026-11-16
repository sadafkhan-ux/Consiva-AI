"""Change detection between two Enterprise scans of the same site: what appeared, what went, what changed.

Both scans are kept whole (their evidence, assessments, scores and reports are never overwritten), so a comparison is computed on demand from stored rows and stores nothing. A comparison
of two different sites is refused as not comparable rather than produced.
"""

from __future__ import annotations

import json
import re
import uuid
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enterprise import store
from app.enterprise.coverage import coverage_declaration

EXAMPLES = 20
CONSENT_PARAMETERS = ("C4", "C5", "C6", "C7", "C8")
_POLICY_PATH = re.compile(r"(privacy|cookie|terms|policy|policies|grievance|dpo|data-protection|legal)", re.I)
_NOT_A_POLICY = re.compile(r"/(blog|blogs|news|article|articles|insights|press|stories|magazine|post|posts)(/|$)", re.I)


def is_policy_url(url: str) -> bool:
    """A page that is a policy, by its address: it names a policy and is not an article. (The text-level classification of the analysis refines this; for change detection the address is enough.)"""
    path = urlparse(url).path
    return bool(_POLICY_PATH.search(path)) and not _NOT_A_POLICY.search(path)


async def _urls(db: AsyncSession, scan_id: uuid.UUID) -> set[str]:
    return {r.normalized_url for r in (await db.execute(text("select normalized_url from ent_frontier where scan_id = :s"), {"s": scan_id})).all()}


async def _policy_hashes(db: AsyncSession, scan_id: uuid.UUID) -> dict[str, str]:
    rows = (await db.execute(text("select source_url, content_hash from ent_evidence where scan_id = :s and kind = 'page_text' order by captured_at"), {"s": scan_id})).all()
    return {r.source_url: r.content_hash for r in rows if is_policy_url(r.source_url)}


async def _json_rows(db: AsyncSession, scan_id: uuid.UUID, kind: str) -> list[dict]:
    return [json.loads(r.content) for r in (await db.execute(text("select content from ent_evidence where scan_id = :s and kind = :k"), {"s": scan_id, "k": kind})).all()]


async def _cookie_identities(db: AsyncSession, scan_id: uuid.UUID) -> set[str]:
    return {c["identity"] for e in await _json_rows(db, scan_id, "cookie") for c in e.get("cookies", [])}


async def _trackers(db: AsyncSession, scan_id: uuid.UUID) -> set[str]:
    return {v["host"] for e in await _json_rows(db, scan_id, "network") for v in e.get("non_essential", [])}


async def _form_count(db: AsyncSession, scan_id: uuid.UUID) -> int:
    return sum(len(e.get("forms", [])) for e in await _json_rows(db, scan_id, "form"))


async def compare(db: AsyncSession, scan_id: uuid.UUID, against: uuid.UUID) -> dict:
    """What changed in `scan_id` since the earlier scan `against`. Both must belong to the caller (the route checks) and be of the same domain."""
    meta = {r.id: r for r in (await db.execute(text("select id, domain from ent_scans where id = any(:ids)"), {"ids": [scan_id, against]})).all()}
    if scan_id not in meta or against not in meta:
        return {"scan_id": str(scan_id), "against": str(against), "comparable": False, "reason": "one of the scans was not found"}
    if meta[scan_id].domain != meta[against].domain:
        return {"scan_id": str(scan_id), "against": str(against), "comparable": False, "reason": f"the scans are of different sites ({meta[against].domain} and {meta[scan_id].domain})"}

    now_urls, before_urls = await _urls(db, scan_id), await _urls(db, against)
    now_pol, before_pol = await _policy_hashes(db, scan_id), await _policy_hashes(db, against)
    now_ck, before_ck = await _cookie_identities(db, scan_id), await _cookie_identities(db, against)
    now_tr, before_tr = await _trackers(db, scan_id), await _trackers(db, against)
    now_f, before_f = await _form_count(db, scan_id), await _form_count(db, against)

    cur = {a.parameter_id: a for a in await store.load_current(db, scan_id)}
    old = {a.parameter_id: a for a in await store.load_current(db, against)}
    consent = [{"test": p, "from": old[p].state if p in old else "not_assessed", "to": cur[p].state if p in cur else "not_assessed"} for p in CONSENT_PARAMETERS
               if (old[p].state if p in old else "not_assessed") != (cur[p].state if p in cur else "not_assessed")]

    f_now = {f.finding_key: f for f in (await store.list_findings(db, scan_id, limit=1000))[0]}
    f_before = {f.finding_key: f for f in (await store.list_findings(db, against, limit=1000))[0]}
    findings = {
        "new": sorted(set(f_now) - set(f_before)), "resolved": sorted(set(f_before) - set(f_now)),
        "changed": [{"finding_key": k, "from": f_before[k].state, "to": f_now[k].state} for k in sorted(set(f_now) & set(f_before)) if f_now[k].state != f_before[k].state],
    }
    s_now, s_before = await store.latest_score(db, scan_id), await store.latest_score(db, against)
    a, b = (s_before.score if s_before else None), (s_now.score if s_now else None)
    return {
        "scan_id": str(scan_id), "against": str(against), "comparable": True,
        "urls": {"added": len(now_urls - before_urls), "removed": len(before_urls - now_urls), "added_examples": sorted(now_urls - before_urls)[:EXAMPLES], "removed_examples": sorted(before_urls - now_urls)[:EXAMPLES]},
        "policies": {
            "changed": [{"url": u, "previous_hash": before_pol[u], "current_hash": now_pol[u]} for u in sorted(set(now_pol) & set(before_pol)) if now_pol[u] != before_pol[u]],
            "added": sorted(set(now_pol) - set(before_pol)), "removed": sorted(set(before_pol) - set(now_pol)),
        },
        "cookies": {"added": sorted(now_ck - before_ck), "removed": sorted(before_ck - now_ck)},
        "trackers": {"added": sorted(now_tr - before_tr), "removed": sorted(before_tr - now_tr)},
        "forms": {"previous": before_f, "current": now_f, "added": max(now_f - before_f, 0), "removed": max(before_f - now_f, 0)},
        "consent": {"changes": consent},
        "findings": findings,
        "coverage": {"previous": await coverage_declaration(db, against), "current": await coverage_declaration(db, scan_id)},
        "score": {"previous": a, "current": b, "delta": (b - a) if (a is not None and b is not None) else None},
    }
