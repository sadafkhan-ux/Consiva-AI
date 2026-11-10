"""Read-only facts about the running deployment, to compare one deployment with another."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.core import versions
from app.core.security import CurrentUser, get_current_user
from app.services import report_cache_service

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/version")
async def get_version(user: CurrentUser = Depends(get_current_user)):
    """The rules versions, scoring engine and approval setting this API runs, and a hash of the web app it prints reports from. The worker logs the same line when it starts."""
    return {**versions.snapshot(), "report_frontend_signature": await report_cache_service.frontend_signature(get_settings())}
