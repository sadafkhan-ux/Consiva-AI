"""Thin CLI around analyse().

    python -m app.gap_analyser.cli https://example.com
    python -m app.gap_analyser.cli --no-llm https://a.com https://b.com
    python -m app.gap_analyser.cli --compact https://example.com
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.gap_analyser.analyser import analyse_async


async def _run(urls: list[str], use_llm: bool) -> list[dict]:
    # Sequential on purpose: each analysis drives a real browser and waits out a
    # settle window, so running them in parallel mainly produces flaky timing.
    return [await analyse_async(u, use_llm=use_llm) for u in urls]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gap-analyser", description=__doc__)
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip the three soft descriptive fields entirely")
    parser.add_argument("--compact", action="store_true",
                        help="one summary line per URL instead of full JSON")
    args = parser.parse_args(argv)

    results = asyncio.run(_run(args.urls, use_llm=not args.no_llm))

    if args.compact:
        for r in results:
            score = r["consent_gap_score"]
            print(f"[{r['page_status']:>10}] {score!s:>4}  {r['url']}")
            if r.get("consent_gap_summary"):
                print(f"             {r['consent_gap_summary']}")
    else:
        json.dump(results if len(results) > 1 else results[0], sys.stdout,
                  indent=2, ensure_ascii=False)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
