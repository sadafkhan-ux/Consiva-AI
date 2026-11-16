"""The 5-page Privacy Policy Scan Agent is frozen: production-ready, and not to be changed without the owner's explicit approval.

If this test fails, a file of the agent was edited (or added, or removed). Do not update the manifest to make the test pass. Ask the owner first; with their approval, follow the steps
in frozen/five_page_agent.py. Enterprise work must not edit these files: it may read from the scanner, never change it.
"""
import json
from pathlib import Path

from frozen import five_page_agent as frozen

MESSAGE = (
    "The 5-page agent is frozen. {what}\n"
    "Do not edit it, and do not rewrite the manifest to hide the change. Ask the owner for explicit approval first (see backend/frozen/five_page_agent.py)."
)


def _expected() -> dict[str, str]:
    return json.loads(frozen.MANIFEST.read_text(encoding="utf-8"))["files"]


def _current_and_expected() -> tuple[dict[str, str], dict[str, str]]:
    current, expected = frozen.collect(), _expected()
    if not (frozen.REPO / "frontend").is_dir():          # inside the backend container only the backend files are present
        expected = {k: v for k, v in expected.items() if not k.startswith("frontend/")}
    return current, expected


def test_the_manifest_exists_and_covers_the_agent():
    expected = _expected()
    assert len(expected) >= 100
    for needle in ("backend/app/scanner/crawler.py", "backend/app/scanner/scan_profiles.py", "backend/app/scoring/scorecard.py", "backend/app/scoring/domain_score.py",
                   "backend/app/scoring/requirements.py", "backend/app/scoring/findings.py", "backend/app/policy/analysis.py", "backend/app/llm/prompts.py",
                   "backend/app/services/scan_service.py", "backend/frozen/five_page_agent.py"):
        assert needle in expected, needle


def test_no_frozen_file_was_changed():
    current, expected = _current_and_expected()
    changed = sorted(p for p in expected if p in current and current[p] != expected[p])
    assert not changed, MESSAGE.format(what="Changed: " + ", ".join(changed))


def test_no_file_was_added_to_the_agent():
    current, expected = _current_and_expected()
    added = sorted(set(current) - set(expected))
    assert not added, MESSAGE.format(what="Added: " + ", ".join(added))


def test_no_frozen_file_was_removed():
    current, expected = _current_and_expected()
    removed = sorted(set(expected) - set(current))
    assert not removed, MESSAGE.format(what="Removed: " + ", ".join(removed))


def test_fingerprints_ignore_line_endings(tmp_path: Path):
    lf, crlf = tmp_path / "a.py", tmp_path / "b.py"
    lf.write_bytes(b"x = 1\ny = 2\n")
    crlf.write_bytes(b"x = 1\r\ny = 2\r\n")
    assert frozen.fingerprint(lf) == frozen.fingerprint(crlf)


def test_a_test_file_is_not_part_of_the_agent():
    assert frozen._is_test(Path("insights.test.ts")) and not frozen._is_test(Path("insights.ts"))
