"""Single mount point for every versioned route: /api/v1/{copilot,agents,approvals,webhooks,analytics,...}."""

from fastapi import APIRouter

from app.api.v1 import analytics, approvals, copilot, webhooks

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(copilot.router)
api_router.include_router(approvals.agents_router)
api_router.include_router(approvals.approvals_router)
api_router.include_router(webhooks.router)
api_router.include_router(analytics.router)
api_router.include_router(analytics.system_router)
