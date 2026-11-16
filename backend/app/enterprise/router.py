"""The one router the Enterprise product exposes. Registered in api/v1/router.py; every route inside answers 404 while ENTERPRISE_ENABLED is false."""

from fastapi import APIRouter

from app.enterprise.api import results, scans

router = APIRouter(prefix="/api/v1/enterprise")
router.include_router(scans.router)
router.include_router(results.router)
