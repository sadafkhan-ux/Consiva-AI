"""The one router the Enterprise product exposes. Registered in api/v1/router.py; every route inside answers 404 while ENTERPRISE_ENABLED is false."""

from fastapi import APIRouter

from app.enterprise.api import dashboard, policy_kb, results, scans

router = APIRouter(prefix="/api/v1/enterprise")
router.include_router(dashboard.router)
router.include_router(scans.router)
router.include_router(results.router)
router.include_router(policy_kb.router)
