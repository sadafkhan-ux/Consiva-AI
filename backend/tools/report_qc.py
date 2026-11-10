"""
Check a finished report PDF before it goes to a customer.

    python tools/report_qc.py REPORT.pdf --site poolscapes.in [--variant summary] [--pages OUTDIR]

Runs the same automatic checks as the server does on every print (services/report_qc.py): page count, A4 pages, text on every page, no internal check codes, one overall score,
links only to Consiva or the scanned site, the site / IST scan date / report ID on the first page, and a cap on soft masks (blurred shadows printed as pictures).
`--pages OUTDIR` also renders every page to a PNG (needs pdftoppm from poppler) for the visual review, and prints the visual checklist. Exit code 0 = all automatic checks passed.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import report_qc

VISUAL_CHECKLIST = (
    "Icons and tiles: no rectangular patch, halo or box behind any icon",
    "Cards: same border, corner radius and spacing; nothing clipped",
    "Score ring, category bars and the total row show the same numbers",
    "Result badges and marks line up in their columns",
    "Evidence and findings are readable; no text overlaps",
    "Long web addresses break at a slash or hyphen, not mid-word",
    "The disclaimer is readable and the call to action is intact",
    "Page 2 ends inside the page; the report is exactly two pages",
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--site", help="the scanned site, e.g. poolscapes.in")
    ap.add_argument("--variant", default="summary", choices=["summary", "free", "full", "long"])
    ap.add_argument("--pages", metavar="OUTDIR", help="render each page to a PNG here for the visual review")
    a = ap.parse_args()
    data = Path(a.pdf).read_bytes()
    problems = report_qc.check_pdf(data, a.variant, domain=a.site)
    print(f"{a.pdf}  ({a.variant}{', ' + a.site if a.site else ''})")
    if problems:
        print("AUTOMATIC CHECKS: FAILED")
        for p in problems:
            print("  -", p)
    else:
        print("AUTOMATIC CHECKS: passed")
    if a.pages:
        out = Path(a.pages)
        out.mkdir(parents=True, exist_ok=True)
        if shutil.which("pdftoppm"):
            subprocess.run(["pdftoppm", "-r", "110", "-png", a.pdf, str(out / "page")], check=False)
            print(f"Pages rendered to {out} for the visual review.")
        else:
            print("pdftoppm not found: render the pages with any viewer for the visual review.")
        print("VISUAL REVIEW (a person ticks each):")
        for item in VISUAL_CHECKLIST:
            print("  [ ]", item)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
