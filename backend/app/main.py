import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agents.consent_agent.graph import close_graph_resources, get_compiled_graph
from app.api.v1.router import api_router
from app.config import get_settings
from app.core.exceptions import ConsivaError

logging.basicConfig(level=get_settings().log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
if get_settings().app_env == "development":
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(ConsivaError)
async def consiva_error_handler(request: Request, exc: ConsivaError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


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
