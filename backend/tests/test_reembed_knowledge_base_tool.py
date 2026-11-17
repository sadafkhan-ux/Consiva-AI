"""tools/reembed_knowledge_base.py: the two things that must hold without a database or an API call -- the pgvector literal and the refusal to write without a backup."""
import subprocess
import sys
from pathlib import Path

from tools import reembed_knowledge_base as tool


def test_the_vector_literal_is_a_pgvector_text_value():
    assert tool._vector_literal([0.5, -1, 2.25]) == "[0.5,-1.0,2.25]"


def test_apply_and_restore_refuse_to_run_without_a_backup_file():
    root = Path(__file__).resolve().parents[1]
    for flag in ("--apply", "--restore"):
        out = subprocess.run([sys.executable, "-m", "tools.reembed_knowledge_base", flag], cwd=root, capture_output=True, text=True)
        assert out.returncode == 2 and "--backup-file is required" in out.stderr, out.stderr
