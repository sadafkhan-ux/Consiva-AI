from fastapi import APIRouter, Depends

from app.api.v1.routes import (
    actions,
    auth,
    consent_agent,
    consent_findings,
    consent_scans,
    dev,
    dsr,
    incidents,
    regwatch,
    ropa,
    websites,
)
from app.config import get_settings
from app.core.security import bind_request_scope

# Every v1 route binds its database connection to the caller's organisation before the
# handler runs, so Postgres row-level security has something to enforce (migration
# 0018). One registration rather than a parameter on all eighty-seven endpoints, and
# nothing can forget it.
api_router = APIRouter(dependencies=[Depends(bind_request_scope)])
api_router.include_router(auth.router)
api_router.include_router(consent_scans.router)
# The external integration API. Registered on the SAME router as everything
# else, so bind_request_scope above applies to it too -- an integration
# endpoint that skipped org binding would read with no RLS scope at all.
api_router.include_router(consent_agent.router)
api_router.include_router(consent_findings.router)
api_router.include_router(websites.router)
api_router.include_router(actions.router)
api_router.include_router(ropa.router)
api_router.include_router(dsr.router)
api_router.include_router(incidents.router)
api_router.include_router(regwatch.router)

# Registered ONLY in development -- outside it the route doesn't exist in FastAPI's
# routing table at all (natural 404), rather than existing and relying solely on the
# handler's own app_env check never having a bug. The handler-level check stays too,
# as defense in depth for the development environment itself.
if get_settings().app_env == "development":
    api_router.include_router(dev.router)
