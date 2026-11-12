"""The image is built from exact pins, so a rebuild can never pull untested package versions (a rebuild once fetched a newer Playwright that needs libatomic and failed)."""
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_every_line_of_the_docker_lock_is_an_exact_pin():
    lines = [ln.strip() for ln in (BACKEND / "requirements.docker.lock.txt").read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    assert len(lines) > 40
    assert all(re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[A-Za-z0-9_.+!\-]+", ln) for ln in lines), [ln for ln in lines if "==" not in ln][:5]


def test_the_dockerfile_installs_from_the_lock_and_provides_libatomic():
    text = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install --no-cache-dir -r requirements.docker.lock.txt" in text
    assert not re.search(r"pip install[^\n]*-r requirements\.txt", text)          # the floor-pin file is never what the image is built from
    assert "libatomic1" in text and text.index("libatomic1") < text.index("playwright install")


def test_the_packages_the_app_needs_are_in_the_lock():
    lock = (BACKEND / "requirements.docker.lock.txt").read_text(encoding="utf-8").lower()
    for pkg in ("fastapi==", "playwright==", "sqlalchemy==", "asyncpg==", "defusedxml==", "httpx=="):
        assert pkg in lock, pkg
