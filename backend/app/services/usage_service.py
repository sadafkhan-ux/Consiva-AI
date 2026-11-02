"""The token usage of one scan, for the usage panel in the console (never for the report or the PDF).

What it reads, in order of preference:
  1. `llm_usage_events` (migration 0041/0042): one row per LLM call, written as each response arrives, so the numbers move while a scan runs. Source "measured".
  2. For a scan made before that table existed: the usage the stage records kept (agent_run_stages.metadata: the analysis call's provider usage block and the
     policy analysis's token counts). Real provider numbers, but with no request ids, durations or page lists. Source "stage_records", and the panel says so.
  3. Neither: source "none". Nothing is invented; every figure is then unavailable.

What it will not do: estimate token counts, split a call that read several pages between those pages, or count a deterministic stage as an LLM call. A stage that
is confirmed not to call a model reports zero LLM tokens; a model stage with no recorded usage reports "unavailable" (None), never zero.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select

from app.db.models import AgentRunStage, ConsentScan, LlmUsageEvent
from app.llm import usage_tracking

# stage -> (label, kind). kind: "llm" calls a language model; "embedding" calls the embedding model (tokens are measured but it is not an LLM call);
# "non_llm" is ordinary code (browser, database, rules, scoring) and is confirmed to use no LLM tokens.
STAGES: dict[str, tuple[str, str]] = {
    "url_validation": ("URL validation", "non_llm"),
    "website_scan": ("Website scan (browser)", "non_llm"),
    "classification": ("Cookie and tracker classification", "non_llm"),
    "data_structuring": ("Saving the scan evidence", "non_llm"),
    "policy_analysis": ("Privacy policy analysis", "llm"),
    "normalize_evidence": ("Loading the evidence", "non_llm"),
    "rules_check": ("Compliance rules check", "non_llm"),
    "rag_retrieval": ("Regulatory provision retrieval", "embedding"),
    "llm_analysis": ("Compliance reasoning, findings and recommendations", "llm"),
    "output_validation": ("Output validation", "non_llm"),
    "findings_generated": ("Saving findings", "non_llm"),
    "rule_findings_generated": ("Saving rule-based findings", "non_llm"),
    "human_review_gate": ("Human review", "non_llm"),
    "audit_saved": ("Audit log", "non_llm"),
}
ORDER = list(STAGES)

NOT_LLM_NOTES = [
    "Not LLM, so zero tokens: browser navigation, the consent test, cookie and request capture, database work, the compliance rules and the DPDP score.",
    "Report and PDF generation use no LLM: the page is drawn from stored data and printed by a browser.",
]
_RUNNING_SCAN = ("queued", "pending", "running")


def _num(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


# ── the events a scan has ─────────────────────────────────────────────────────────────────────────────────────────


def _event_from_row(e) -> dict:
    return {
        "stage": e.stage, "agent": e.agent, "provider": e.provider, "model": e.model, "status": e.status, "attempts": e.attempts, "input_tokens": e.input_tokens,
        "output_tokens": e.output_tokens, "cache_read_tokens": e.cache_read_tokens, "cache_write_tokens": e.cache_write_tokens, "reasoning_tokens": e.reasoning_tokens,
        "duration_ms": e.duration_ms, "stored_cost": None if e.cost_usd is None else float(e.cost_usd), "page_urls": list(e.page_urls or []), "request_ids": list(e.request_ids or []),
        "error_type": e.error_type, "at": e.created_at,
    }


def events_from_stage_records(stages: list[dict]) -> list[dict]:
    """Usage for a scan made before the events table: what the stage records kept. Provider-reported numbers only; nothing estimated."""
    out: list[dict] = []
    for s in stages:
        meta = s.get("meta") or {}
        if s["stage"] == "llm_analysis":
            att = (meta.get("llm_usage") or {}).get("usage_by_attempt") or []
            if not att:
                continue
            summary = usage_tracking.summarise_usage(att)
            out.append({
                "stage": "llm_analysis", "agent": "consent", "provider": meta.get("provider") or "unknown", "model": meta.get("model") or "unknown",
                "status": "success" if s["status"] == "completed" else "failed", "attempts": len(att), "input_tokens": summary["input_tokens"], "output_tokens": summary["output_tokens"],
                "cache_read_tokens": summary["cache_read_tokens"], "cache_write_tokens": summary["cache_write_tokens"], "reasoning_tokens": summary["reasoning_tokens"],
                "duration_ms": s.get("duration_ms"), "stored_cost": None, "page_urls": [], "request_ids": summary["request_ids"], "error_type": None, "at": s.get("started_at"),
            })
        elif s["stage"] == "policy_analysis":
            model = meta.get("policy_model")
            if not model or model.get("tokens_in") is None:
                continue
            out.append({
                "stage": "policy_analysis", "agent": "consent-policy", "provider": model.get("provider") or "unknown", "model": model.get("model") or "unknown", "status": "success",
                "attempts": 1, "input_tokens": _num(model.get("tokens_in")), "output_tokens": _num(model.get("tokens_out")), "cache_read_tokens": 0, "cache_write_tokens": 0,
                "reasoning_tokens": None, "duration_ms": s.get("duration_ms"), "stored_cost": None, "page_urls": [], "request_ids": [], "error_type": None, "at": s.get("started_at"),
            })
    return out


def _cost_of(e: dict) -> dict | None:
    """The price components for one call, from its own tokens and the verified price of its model; None when that model has no known price."""
    provider = "claude" if e["provider"] == "claude" else e["provider"]
    return usage_tracking.cost_components(
        provider, e["model"], input_tokens=e["input_tokens"], output_tokens=e["output_tokens"], cache_read_tokens=e["cache_read_tokens"], cache_write_tokens=e["cache_write_tokens"],
    )


def _sum(events: list[dict]) -> dict:
    inp = sum(e["input_tokens"] for e in events)
    out = sum(e["output_tokens"] for e in events)
    cr = sum(e["cache_read_tokens"] for e in events)
    cw = sum(e["cache_write_tokens"] for e in events)
    reasoning = [e["reasoning_tokens"] for e in events if e["reasoning_tokens"] is not None]
    comps = [c for c in (_cost_of(e) for e in events) if c is not None]
    return {
        "calls": len(events), "failed_calls": sum(1 for e in events if e["status"] != "success"), "input_tokens": inp, "output_tokens": out, "cache_read_tokens": cr,
        "cache_write_tokens": cw, "total_tokens": inp + out + cr + cw, "reasoning_tokens": sum(reasoning) if reasoning else None,
        "priced_calls": len(comps), "unpriced_calls": len(events) - len(comps),
        "cost": sum(c["total"] for c in comps) if comps else None,
        "cost_input": sum(c["input"] for c in comps) if comps else None, "cost_cache_read": sum(c["cache_read"] for c in comps) if comps else None,
        "cost_cache_write": sum(c["cache_write"] for c in comps) if comps else None, "cost_output": sum(c["output"] for c in comps) if comps else None,
    }


# ── one scan ──────────────────────────────────────────────────────────────────────────────────────────────────────


ANALYSIS_STAGES = frozenset({"llm_analysis", "policy_analysis"})


def build_usage(scan: dict, stages: list[dict], event_rows: list) -> dict:
    """`scan`: id, url, status, created_at, completed_at. `stages`: dicts with stage, status, started_at, completed_at, duration_ms, error, meta (see loaders below).
    `event_rows`: LlmUsageEvent rows (or objects with the same attributes)."""
    if event_rows:
        events, source = [_event_from_row(e) for e in event_rows], "measured"
    else:
        events = events_from_stage_records(stages)
        source = "stage_records" if events else "none"

    by_stage_rows: dict[str, list[dict]] = defaultdict(list)
    for s in stages:
        by_stage_rows[s["stage"]].append(s)
    by_stage_events: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_stage_events[e["stage"] or "unknown"].append(e)

    stage_out = []
    for name in [n for n in ORDER if n in by_stage_rows or n in by_stage_events] + [n for n in by_stage_events if n not in STAGES]:
        label, kind = STAGES.get(name, (name.replace("_", " ").capitalize(), "llm"))
        rows = by_stage_rows.get(name, [])
        evs = by_stage_events.get(name, [])
        status = "running" if any(r["status"] == "running" for r in rows) else ("failed" if rows and rows[-1]["status"] == "failed" else "completed")
        sums = _sum(evs)
        entry = {
            "stage": name, "label": label, "kind": kind, "status": status, "runs": len(rows), "duration_ms": sum(_num(r.get("duration_ms")) for r in rows) or None,
            "models": sorted({f"{e['provider']}/{e['model']}" for e in evs}), "note": None, **sums,
        }
        if kind == "non_llm":
            entry.update(input_tokens=0, output_tokens=0, total_tokens=0, cache_read_tokens=0, cache_write_tokens=0, reasoning_tokens=0, cost=0.0, unpriced_calls=0, priced_calls=0)
            entry["note"] = "Not an LLM stage: zero tokens."
        elif kind == "embedding":
            req = sum(_num((r.get("meta") or {}).get("embed_requests")) for r in rows)
            tok = [(r.get("meta") or {}).get("embed_tokens") for r in rows if (r.get("meta") or {}).get("embed_tokens") is not None]
            entry.update(input_tokens=0, output_tokens=0, total_tokens=0, cache_read_tokens=0, cache_write_tokens=0, reasoning_tokens=0, cost=None, calls=0,
                         embedding_requests=req if tok else None, embedding_tokens=sum(_num(t) for t in tok) if tok else None)
            entry["note"] = ("Embedding model, not an LLM: no LLM tokens. Embedding tokens: " + (f"{sum(_num(t) for t in tok):,} measured." if tok else "not recorded for this scan."))
        else:
            if not evs:
                for k in ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "cost"):
                    entry[k] = None
                entry["note"] = ("Waiting for the model's reply: usage is recorded when it arrives." if status == "running"
                                 else "No usage was recorded for this stage." if status == "completed" else "Failed before any usage was recorded.")
            elif source == "stage_records":
                entry["note"] = "From the stage record (made before per-call tracking): no request ids or page list."
        reused = _reused_from(rows) if kind == "llm" else None
        if reused is not None:
            # The identical policy text and question were already answered for this site, so the model was not called: zero tokens is a fact, not a gap.
            for k in ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens"):
                entry[k] = entry[k] or 0
            entry["cost"] = entry["cost"] if entry["cost"] is not None else 0.0
            entry["note"] = "Reused the saved analysis of an identical policy text" + (f" from scan {reused[:8]}" if reused != "earlier" else "") + ": no model call, zero tokens."
        entry["reused_from_scan"] = None if reused in (None, "earlier") else reused
        entry["reused"] = reused is not None
        entry["error"] = next((r["error"] for r in reversed(rows) if r.get("error")), None)
        stage_out.append(entry)

    # pages: a call is attributed to a page only when it read exactly that one page
    attributed: dict[str, list[dict]] = defaultdict(list)
    shared: dict[tuple, dict] = {}
    unattributed: list[dict] = []
    for e in events:
        urls = e["page_urls"]
        if len(urls) == 1:
            attributed[urls[0]].append(e)
        elif len(urls) > 1:
            key = (e["stage"], tuple(sorted(urls)))
            shared.setdefault(key, {"stage": e["stage"], "urls": sorted(urls), "events": []})["events"].append(e)
        else:
            unattributed.append(e)
    pages = {
        "attributed": sorted(
            [{"url": u, **{k: v for k, v in _sum(es).items() if k in ("calls", "input_tokens", "output_tokens", "total_tokens", "cost")}} for u, es in attributed.items()],
            key=lambda r: -r["total_tokens"]),
        "shared": [
            {"label": f"{STAGES.get(g['stage'], (g['stage'], ''))[0]}: read {len(g['urls'])} pages together", "stage": g["stage"], "page_urls": g["urls"], "reason": "One model call read all of these pages, so its tokens cannot be split between them.",
             **{k: v for k, v in _sum(g["events"]).items() if k in ("calls", "input_tokens", "output_tokens", "total_tokens", "cost")}} for g in shared.values()],
        "unattributed": ({"label": "Not attributed to a page", "reason": "The pages these calls read were not recorded.",
                          **{k: v for k, v in _sum(unattributed).items() if k in ("calls", "input_tokens", "output_tokens", "total_tokens", "cost")}} if unattributed else None),
    }

    totals = _sum(events)
    running = scan["status"] in _RUNNING_SCAN or any(s["status"] == "running" for s in stages)
    state = "running" if running else ("failed" if scan["status"] in ("failed", "cancelled") else "completed")
    current = next((STAGES.get(s["stage"], (s["stage"], ""))[0] for s in reversed(stages) if s["status"] == "running"), None)
    embed_tokens = [(s.get("meta") or {}).get("embed_tokens") for s in stages if s["stage"] == "rag_retrieval" and (s.get("meta") or {}).get("embed_tokens") is not None]
    website = next((s for s in stages if s["stage"] == "website_scan" and s["status"] == "completed"), None)
    notes = list(NOT_LLM_NOTES)
    if source == "stage_records":
        notes.append("This scan was made before per-call tracking. The totals come from the usage the stage records kept; request ids, durations and page lists were not recorded.")
    # Whether the AI analysis ever started for this scan: a scan that was only crawled has no usage to record, which is not the same as usage that was lost.
    analysis_run = any(s["stage"] in ANALYSIS_STAGES for s in stages)
    if source == "none":
        notes.append("No model usage was recorded for this scan." if analysis_run else "No AI analysis was run for this scan, so it used no model tokens.")
    if totals["unpriced_calls"]:
        notes.append(f"{totals['unpriced_calls']} call(s) used a model with no verified price: their tokens are counted, their cost is unavailable.")
    return {
        "scan_id": str(scan["id"]), "url": scan["url"], "domain": _domain(scan["url"]), "scan_status": scan["status"], "state": state, "current_stage": current,
        "created_at": scan.get("created_at"), "completed_at": scan.get("completed_at"),
        "pages_scanned": _num(((website or {}).get("meta") or {}).get("pages_scanned")) if website else None,
        "source": source, "analysis_run": analysis_run, "totals": {**totals, "embedding_tokens": sum(_num(t) for t in embed_tokens) if embed_tokens else None},
        "cost": {"currency": usage_tracking.CURRENCY, "total": totals["cost"], "input": totals["cost_input"], "cache_read": totals["cost_cache_read"], "cache_write": totals["cost_cache_write"],
                 "output": totals["cost_output"], "priced_calls": totals["priced_calls"], "unpriced_calls": totals["unpriced_calls"], "complete": totals["unpriced_calls"] == 0 and totals["calls"] > 0,
                 "price_source": usage_tracking.PRICE_SOURCE if totals["priced_calls"] else None},
        "stages": stage_out, "pages": pages, "notes": notes, "calls": _call_rows(events), "errors": _errors(scan, stages, events),
        # What was saved for this scan, so the panel can say whether its results can be opened. Read from the stage records, never assumed.
        "results": {
            "evidence": any(s["stage"] == "website_scan" and s["status"] == "completed" for s in stages),
            "analysis": any(s["stage"] == "llm_analysis" and s["status"] == "completed" for s in stages),
            "findings": any(s["stage"] in ("findings_generated", "rule_findings_generated") and s["status"] == "completed" for s in stages),
            "audit": any(s["stage"] == "audit_saved" and s["status"] == "completed" for s in stages),
        },
    }


def _reused_from(rows: list[dict]) -> str | None:
    """The scan whose saved policy analysis this stage reused ('earlier' when the record does not say which), or None when the model was really called."""
    for r in rows:
        meta = r.get("meta") or {}
        if (meta.get("policy_model") or {}).get("reused"):
            return meta.get("reused_from") or "earlier"
    return None


def _call_rows(events: list[dict]) -> list[dict]:
    """One row per recorded model call, in the order they were made: the breakdown behind every number in the panel. Only what was recorded."""
    out = []
    for e in sorted(events, key=lambda x: (x["at"] is None, x["at"])):
        comps = _cost_of(e)
        label = STAGES.get(e["stage"] or "", (e["stage"] or "Unknown stage", ""))[0]
        out.append({
            "stage": e["stage"] or "unknown", "label": label, "agent": e["agent"], "provider": e["provider"], "model": e["model"], "status": e["status"], "attempts": e["attempts"],
            "input_tokens": e["input_tokens"], "output_tokens": e["output_tokens"], "cache_read_tokens": e["cache_read_tokens"], "cache_write_tokens": e["cache_write_tokens"],
            "reasoning_tokens": e["reasoning_tokens"], "total_tokens": e["input_tokens"] + e["output_tokens"] + e["cache_read_tokens"] + e["cache_write_tokens"],
            "duration_ms": e["duration_ms"], "cost": None if comps is None else comps["total"], "page_urls": e["page_urls"],
            "error_type": e["error_type"], "at": e["at"],
        })
    return out


def _errors(scan: dict, stages: list[dict], events: list[dict]) -> list[dict]:
    """Everything that went wrong and was recorded: the scan's own error, each failed stage, each failed model call."""
    out = []
    if scan.get("error"):
        out.append({"where": "Scan", "stage": None, "message": str(scan["error"])[:1000]})
    for s in stages:
        if s["status"] == "failed" or (s.get("error") and s["status"] != "completed"):
            out.append({"where": STAGES.get(s["stage"], (s["stage"], ""))[0], "stage": s["stage"], "message": str(s.get("error") or "The stage failed; no message was recorded.")[:1000]})
    for e in events:
        if e["status"] != "success":
            out.append({"where": STAGES.get(e["stage"] or "", (e["stage"] or "Model call", ""))[0], "stage": e["stage"], "message": f"Model call failed: {e['error_type'] or 'no error type recorded'}."})
    return out


def _domain(url: str) -> str:
    return url.replace("https://", "").replace("http://", "").split("/")[0].split("?")[0]


def summary_of(u: dict) -> dict:
    """One line of the history."""
    t = u["totals"]
    return {"scan_id": u["scan_id"], "url": u["url"], "domain": u["domain"], "status": u["state"], "created_at": u["created_at"], "completed_at": u["completed_at"],
            "pages_scanned": u["pages_scanned"], "calls": t["calls"], "failed_calls": t["failed_calls"], "input_tokens": t["input_tokens"], "output_tokens": t["output_tokens"],
            "total_tokens": t["total_tokens"], "cost": u["cost"]["total"], "cost_complete": u["cost"]["complete"], "currency": u["cost"]["currency"], "source": u["source"], "analysis_run": u["analysis_run"],
            "errors": u["errors"], "results": u["results"], "reused_analysis": any(s.get("reused") for s in u["stages"])}


# ── loading ───────────────────────────────────────────────────────────────────────────────────────────────────────


def _stage_dict(r) -> dict:
    return {
        "scan_id": r.scan_id, "stage": r.stage, "status": r.status, "started_at": r.started_at, "completed_at": getattr(r, "completed_at", None), "duration_ms": r.duration_ms, "error": r.error,
        "meta": {"llm_usage": r.llm_usage, "provider": r.provider, "model": r.model, "embed_tokens": r.embed_tokens, "embed_requests": r.embed_requests,
                 "pages_scanned": r.pages_scanned, "policy_model": r.policy_model, "reused_from": r.reused_from},
    }


def _stage_query(scan_ids: list[uuid.UUID]):
    m = AgentRunStage.stage_metadata
    return (
        select(AgentRunStage.scan_id, AgentRunStage.stage, AgentRunStage.status, AgentRunStage.started_at, AgentRunStage.completed_at, AgentRunStage.duration_ms, AgentRunStage.error,
               m["llm_usage"].label("llm_usage"), m["provider"].label("provider"), m["model"].label("model"), m["embed_tokens"].label("embed_tokens"),
               m["embed_requests"].label("embed_requests"), m["pages_scanned"].label("pages_scanned"), m["result"]["model"].label("policy_model"),
               m["result"]["reused_from_scan"].as_string().label("reused_from"))
        .where(AgentRunStage.scan_id.in_(scan_ids)).order_by(AgentRunStage.created_at)
    )


def _scan_dict(s: ConsentScan) -> dict:
    return {"id": s.id, "url": s.url, "status": s.status, "created_at": s.created_at, "completed_at": s.completed_at, "error": s.error}


async def load_scan_usage(db, scan: ConsentScan) -> dict:
    stages = [_stage_dict(r) for r in (await db.execute(_stage_query([scan.id]))).all()]
    events = list((await db.execute(select(LlmUsageEvent).where(LlmUsageEvent.scan_id == scan.id).order_by(LlmUsageEvent.created_at))).scalars().all())
    return build_usage(_scan_dict(scan), stages, events)


async def load_history(db, scans: list[ConsentScan]) -> list[dict]:
    if not scans:
        return []
    ids = [s.id for s in scans]
    stages_by: dict[uuid.UUID, list[dict]] = defaultdict(list)
    for r in (await db.execute(_stage_query(ids))).all():
        stages_by[r.scan_id].append(_stage_dict(r))
    events_by: dict[uuid.UUID, list] = defaultdict(list)
    for e in (await db.execute(select(LlmUsageEvent).where(LlmUsageEvent.scan_id.in_(ids)).order_by(LlmUsageEvent.created_at))).scalars().all():
        events_by[e.scan_id].append(e)
    return [summary_of(build_usage(_scan_dict(s), stages_by[s.id], events_by[s.id])) for s in scans]


def iso(x: datetime | None) -> str | None:
    return None if x is None else x.isoformat()
