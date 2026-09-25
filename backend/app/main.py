import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agents.consent_agent.graph import close_graph_resources, get_compiled_graph
from app.api.v1.router import api_router
from app.config import get_settings
from app.core.exceptions import ConsivaError
from app.db.privilege_check import assert_rls_is_enforceable

logging.basicConfig(level=get_settings().log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything serves a request, say out loud whether row-level security is
    # actually enforceable for this connection. It was not, for a long time, and
    # nothing anywhere reported it -- the policies simply were never consulted.
    # Refuses to boot in production; warns loudly elsewhere.
    await assert_rls_is_enforceable()
    await get_compiled_graph()  # builds the checkpointer pool + compiled graph once
    yield
    await close_graph_resources()


app = FastAPI(title="Consiva — Consent Agent", lifespan=lifespan)
app.include_router(api_router)

# Local dev only: the separate Vite frontend (frontend/) runs on its own origin
# (localhost, some port Vite picks -- not assumed fixed), so its fetch() calls to this
# API are genuinely cross-origin, unlike the static demo page which is same-origin.
# No CORS config existed before this, so this is purely additive. Gated to
# app_env=="development" -- production has no reason to accept browser-origin
# requests from arbitrary localhost ports, so nothing here weakens it.
_settings = get_settings()
if _settings.app_env == "development":
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
elif _settings.cors_origin_list:
    # Production browser clients. An explicit allow-list, never a wildcard: this API is
    # credentialed (Authorization: Bearer), and allow_origins=["*"] with
    # allow_credentials=True is rejected by browsers anyway -- so a wildcard here would
    # not even be permissive, just broken.
    #
    # Headers are listed rather than "*" so the set is visible and reviewable;
    # Idempotency-Key is included because the integration API reads it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        max_age=600,
    )


# Error codes for the integration API, derived from the exception type rather than
# from prose. An external integrator branches on these; `message` is for humans and is
# free to change wording, `code` is not.
_API_ERROR_CODES = {
    "NotFoundError": "SCAN_NOT_FOUND",
    "InvalidUrlError": "INVALID_URL",
    "ScanAuthorizationError": "NOT_AUTHORIZED",
    "ScanTimeoutError": "SCAN_TIMEOUT",
    "RateLimitExceededError": "RATE_LIMIT_EXCEEDED",
    "ScanNotReadyError": "SCAN_NOT_READY",
    "ScanConflictError": "SCAN_CONFLICT",
    "WebhookNotConfiguredError": "WEBHOOK_NOT_CONFIGURED",
    "AgentRunAlreadyInProgressError": "ANALYSIS_IN_PROGRESS",
    "LLMOutputValidationError": "ANALYSIS_FAILED",
}


def _error_code(exc: ConsivaError) -> str:
    return _API_ERROR_CODES.get(type(exc).__name__, "INTERNAL_ERROR")


@app.exception_handler(ConsivaError)
async def consiva_error_handler(request: Request, exc: ConsivaError):
    """Two shapes, one exception hierarchy.

    The console and the rest of the platform have always received {"detail": "..."} and
    the frontend reads that key, so changing it globally would break screens for a
    cosmetic gain. The /consent-agent API is a new, external contract with no such
    history, and an integrator needs a stable machine-readable `code` to branch on
    rather than pattern-matching English. So that prefix -- and only that prefix --
    gets the documented envelope.

    Neither shape carries a stack trace or an internal identifier.
    """
    if request.url.path.startswith("/api/v1/consent-agent"):
        body: dict = {"code": _error_code(exc), "message": exc.message}
        scan_id = request.path_params.get("scan_id")
        if scan_id is not None:
            body["scan_id"] = str(scan_id)
        return JSONResponse(status_code=exc.status_code, content={"error": body})
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Schema-validation failures, in the same envelope as everything else on the
    integration API.

    FastAPI's default is a list under "detail" whose entries carry a `ctx` that can
    include the offending input. That is fine for the console, which is first-party and
    already sees its own request -- but an external contract should answer in one
    documented shape, and the field path is the part an integrator can act on.
    """
    if request.url.path.startswith("/api/v1/consent-agent"):
        problems = [
            {"field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
             "message": err.get("msg", "invalid value")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"error": {
                "code": "VALIDATION_ERROR",
                "message": "The request body failed validation.",
                "fields": problems,
            }},
        )
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.get("/health")
async def health():
    return {"status": "ok"}


# Local dev/testing convenience: serves the existing static demo UI (app/static/demo/)
# at the site root, so `http://localhost:8000` alone loads it and its own relative
# fetch("/api/v1/...") calls keep hitting this same server -- no separate frontend
# process, no build step (it's plain HTML/CSS/JS, not a bundled framework app).
# MUST be the last thing registered: Starlette matches routes/mounts in registration
# order, and a Mount("/") matches every path by prefix -- registering it before the
# API router or /health would silently swallow those requests into 404s from the
# static file lookup instead of reaching the real handlers above.
_demo_dir = Path(__file__).parent / "static" / "demo"
app.mount("/", StaticFiles(directory=str(_demo_dir), html=True), name="frontend")
