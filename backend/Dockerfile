# Consiva Consent Agent — backend (FastAPI + LangGraph + Playwright scanner).
#
# Base image choice: python:3.13-slim matches the interpreter this project is developed
# against (pyproject requires-python >=3.11; the local .venv is 3.13) and keeps the
# image small, with Chromium's OS dependencies installed explicitly by Playwright's own
# `--with-deps` rather than guessed by hand.
#
# One image serves BOTH runtime processes (api and worker) — they are the same
# application, differing only in entrypoint, so docker-compose reuses this image for
# both services rather than building twice.
FROM python:3.13-slim AS base

# PYTHONUNBUFFERED so container logs stream in real time instead of sitting in a buffer
# (the worker's progress logging is the main way a running scan is observable).
# PLAYWRIGHT_BROWSERS_PATH is set to a shared absolute location rather than the default
# per-user ~/.cache/ms-playwright, because the browser is installed as root at build
# time but the container runs as a non-root user — the default path would be
# unreadable to that user and Chromium would silently fail to launch.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencies first, as their own layer: requirements change far less often than
# application code, so this layer stays cached across ordinary code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + every OS library it needs. `--with-deps` asks Playwright itself which
# packages this exact Playwright version requires, instead of hand-maintaining an
# apt list that silently rots when Playwright is upgraded. Only chromium is installed
# (the scanner never launches firefox/webkit), keeping the image meaningfully smaller.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Application code. .dockerignore keeps .env, .venv, tests, caches and logs out.
COPY app ./app
COPY migrations ./migrations
COPY data ./data
COPY migrate.py ./
# One-shot CLI, same as migrate.py: bootstraps the first organization + admin
# user, which cannot be done through the API (creating a user requires an
# authenticated admin, and none exists yet).
COPY create_user.py ./

# Non-root runtime. The browser cache must be readable (not writable) by this user;
# /app stays root-owned and read-only to the app, which is correct — the application
# writes only to the database and stdout, never to its own source tree.
RUN useradd --create-home --uid 10001 consiva \
    && chown -R consiva:consiva /ms-playwright
USER consiva

EXPOSE 8000

# The app's own /health endpoint (app/main.py). urllib is used rather than curl/wget so
# no extra package is needed in the slim image just to health-check it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else sys.exit(1)"]

# uvicorn is invoked directly rather than via app/run_server.py: that launcher exists
# solely to force a SelectorEventLoop around a Windows-only Playwright/psycopg event
# loop conflict, and it binds 127.0.0.1 (unreachable from outside a container). On
# Linux neither applies — crawler.run_scan_isolated() takes its non-Windows branch and
# runs the scan in-process, so the default loop is correct here.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
