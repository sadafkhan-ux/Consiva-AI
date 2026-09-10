from fastapi import APIRouter

from app.api.v1.routes import (
    actions,
    auth,
    consent_findings,
    consent_scans,
    dev,
    ropa,
    websites,
)
from app.config import get_settings

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(consent_scans.router)
api_router.include_router(consent_findings.router)
api_router.include_router(websites.router)
api_router.include_router(actions.router)
api_router.include_router(ropa.router)

# Registered ONLY in development -- outside it the route doesn't exist in FastAPI's
# routing table at all (natural 404), rather than existing and relying solely on the
# handler's own app_env check never having a bug. The handler-level check stays too,
# as defense in depth for the development environment itself.
if get_settings().app_env == "development":
    api_router.include_router(dev.router)
