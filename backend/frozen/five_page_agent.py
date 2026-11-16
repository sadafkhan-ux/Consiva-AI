"""The frozen 5-page Privacy Policy Scan Agent: which files make it up, and a fingerprint of each.

The owner has declared this agent stable and production-ready: its scanning, scoring, findings, recommendations and PDF report are not to be changed, refactored or extended without
their explicit approval. tests/test_five_page_agent_frozen.py compares every file below with `five_page_agent.manifest.json` and fails, naming the file, when one differs.

Changing the agent with the owner's approval:
  1. Make the approved change.
  2. Run the full backend and frontend suites.
  3. Run `python frozen/five_page_agent.py` from the backend folder to write a new manifest, and commit it with the change.
A fingerprint ignores line endings (a CRLF checkout is the same file as an LF one).

Enterprise work reads from the scanner (app/enterprise imports its URL, robots and browser helpers) and must not edit these files.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
MANIFEST = Path(__file__).with_name("five_page_agent.manifest.json")

#: (root, glob) pairs, relative to the repository root. Tests are not frozen: a test may be added without changing the agent.
FROZEN = (
    ("backend", "app/scanner/**/*.py"),            # the crawl: the five-page profile, page choice, consent testing
    ("backend", "app/scoring/**/*.py"),            # the score, the findings and the requirements reading
    ("backend", "app/rules/**/*.py"),              # the consent and DPDP rules
    ("backend", "app/policy/**/*.py"),             # the privacy-policy analysis
    ("backend", "app/llm/**/*.py"),                # the narrative model calls
    ("backend", "app/rag/**/*.py"),                # the retrieved legal context
    ("backend", "app/services/scan_service.py"),
    ("backend", "app/services/assessment_service.py"),
    ("backend", "app/services/consent_agent_api_service.py"),
    ("backend", "app/services/report_cache_service.py"),
    ("backend", "frozen/five_page_agent.py"),      # this file: the guard cannot be loosened quietly
    ("frontend", "src/report/**/*.ts"),            # the report, the 2-page summary and the PDF
    ("frontend", "src/report/**/*.tsx"),
    ("frontend", "src/report/**/*.css"),
)


def _is_test(path: Path) -> bool:
    return ".test." in path.name


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def collect() -> dict[str, str]:
    """Repository-relative path -> fingerprint, for every frozen file that exists here (the frontend is absent inside the backend container)."""
    found: dict[str, str] = {}
    for root, pattern in FROZEN:
        base = REPO / root
        if not base.is_dir():
            continue
        for p in sorted(base.glob(pattern)):
            if p.is_file() and "__pycache__" not in p.parts and not _is_test(p):
                found[p.relative_to(REPO).as_posix()] = fingerprint(p)
    return found


def write_manifest() -> int:
    files = collect()
    MANIFEST.write_text(json.dumps({"frozen_files": len(files), "files": files}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return len(files)


if __name__ == "__main__":
    print(f"wrote {MANIFEST.name}: {write_manifest()} files")
    sys.exit(0)
