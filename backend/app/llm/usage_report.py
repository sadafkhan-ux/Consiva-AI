"""Read-only LLM usage report from llm_usage_events (migration 0041).

    python -m app.llm.usage_report                 # every scan with a recorded call, newest first, and the totals
    python -m app.llm.usage_report --scan <id>     # one scan, call by call
    python -m app.llm.usage_report --days 7        # only calls from the last 7 days

Run it where the database owner's connection is used (the worker container): row-level security shows a restricted role nothing outside its own
organisation. It only reads. A call whose model has no known price is listed with cost "unpriced"; it is never counted as free.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import uuid

from sqlalchemy import text

from app.db.session import async_session_factory


def _fmt_cost(c) -> str:
    return "unpriced" if c is None else f"${float(c):.5f}"


async def main(scan: str | None, days: int | None) -> None:
    where, params = ["true"], {}
    if scan:
        where.append("e.scan_id = :scan")
        params["scan"] = uuid.UUID(scan)
    if days:
        where.append("e.created_at >= now() - make_interval(days => :days)")
        params["days"] = days
    clause = " and ".join(where)
    async with async_session_factory() as db:
        if scan:
            rows = (await db.execute(text(f"""select e.created_at, e.agent, e.stage, e.provider, e.model, e.status, e.attempts, e.input_tokens, e.output_tokens,
                      e.cache_read_tokens, e.cache_write_tokens, e.reasoning_tokens, e.duration_ms, e.cost_usd, e.request_ids, e.error_type
                      from llm_usage_events e where {clause} order by e.created_at"""), params)).all()
            print(f"{'time (UTC)':20} {'stage':16} {'provider/model':34} {'status':8} {'att':>3} {'input':>8} {'output':>7} {'reason':>7} {'cacheR':>7} {'ms':>7} cost")
            for r in rows:
                print(f"{r.created_at:%Y-%m-%d %H:%M:%S}  {(r.stage or r.agent):16} {(r.provider + '/' + r.model)[:34]:34} {r.status:8} {r.attempts:>3} {r.input_tokens:>8} "
                      f"{r.output_tokens:>7} {('-' if r.reasoning_tokens is None else r.reasoning_tokens):>7} {r.cache_read_tokens:>7} {r.duration_ms or 0:>7} {_fmt_cost(r.cost_usd)}"
                      + (f"  [{r.error_type}]" if r.error_type else "") + (f"  ids={','.join(r.request_ids)}" if r.request_ids else ""))
            return
        per = (await db.execute(text(f"""select e.scan_id, min(e.created_at) as first, count(*) as calls, count(*) filter (where e.status = 'failed') as failed,
                  sum(e.input_tokens) as inp, sum(e.output_tokens) as outp, sum(e.cache_read_tokens) as cread, sum(coalesce(e.reasoning_tokens, 0)) as reasoning,
                  sum(e.cost_usd) as cost, count(*) filter (where e.cost_usd is null) as unpriced, c.url
                  from llm_usage_events e join consent_scans c on c.id = e.scan_id where {clause} group by e.scan_id, c.url order by first desc"""), params)).all()
    if not per:
        print("No LLM usage events are recorded yet for this selection.")
        return
    print(f"{'first call (UTC)':20} {'scan':10} {'site':34} {'calls':>5} {'failed':>6} {'input':>8} {'output':>7} {'reason':>7} cost")
    for r in per:
        print(f"{r.first:%Y-%m-%d %H:%M:%S}  {str(r.scan_id)[:8]:10} {(r.url or '')[:34]:34} {r.calls:>5} {r.failed:>6} {r.inp:>8} {r.outp:>7} {r.reasoning:>7} "
              f"{_fmt_cost(r.cost) if not r.unpriced else _fmt_cost(r.cost) + ' + ' + str(r.unpriced) + ' unpriced call(s)'}")
    costs = [float(r.cost) for r in per if r.cost is not None and not r.unpriced]
    print(f"\n{len(per)} scans, {sum(r.calls for r in per)} calls ({sum(r.failed for r in per)} failed): input {sum(r.inp for r in per):,}, output {sum(r.outp for r in per):,} "
          f"(reasoning {sum(r.reasoning for r in per):,} of it, already inside output), cache read {sum(r.cread for r in per):,}")
    if costs:
        print(f"fully priced scans: {len(costs)}; cost per scan: min ${min(costs):.5f}, median ${statistics.median(costs):.5f}, mean ${statistics.mean(costs):.5f}, max ${max(costs):.5f}, total ${sum(costs):.4f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", help="one scan id")
    ap.add_argument("--days", type=int, help="only calls from the last N days")
    args = ap.parse_args()
    asyncio.run(main(args.scan, args.days))
