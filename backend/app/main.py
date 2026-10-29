"""NexusFlow Autopilot API: single production entrypoint.

Run (from backend/):  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8010
Port 8010, not 8000: FlowPilot's n8n gateway on this machine already proxies host port 8000.

Request pipeline (outermost first):
  RequestContextMiddleware  request id (X-Request-ID in/out), JSON access log
  CORSMiddleware            browser origins from CORS_ALLOW_ORIGINS
  CrashToProblemMiddleware  unhandled exception -> 500 problem (keeps request id + CORS headers)
  exception handlers        every error -> RFC 7807 application/problem+json
  routers                   /health, /api/v1/* (see app/api/v1/router.py)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import psycopg2
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from app.api import health
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.db import apply_migrations, get_conn
from app.core.errors import CrashToProblemMiddleware, Problem, install_exception_handlers
from app.core.logging import REQUEST_ID_HEADER, RequestContextMiddleware, configure_logging
from app.services.n8n.dispatcher import N8nDispatcher

log = logging.getLogger("nexusflow.api")
API_VERSION = "1.0.0"

TAGS = [
    {"name": "system", "description": "Health, metrics and identity."},
    {"name": "copilot", "description": "RAG copilot: grounded answers and hybrid retrieval (Stage 2)."},
    {"name": "agents", "description": "Commercial Governance agent runs (Stage 4)."},
    {"name": "approvals", "description": "Human-in-the-loop approval queue and decisions (Stage 4)."},
    {"name": "webhooks", "description": "HMAC-signed n8n / external system events (Stage 3)."},
    {"name": "analytics", "description": "Executive dashboard KPIs (Stage 6 UI)."},
]


def _operation_id(route: APIRoute) -> str:
    """Stable, client-generator-friendly operationIds: <tag>_<function name>."""
    return f"{route.tags[0] if route.tags else 'default'}_{route.name}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        with get_conn() as conn, conn.cursor() as cur:
            apply_migrations(cur)
    except psycopg2.OperationalError as exc:
        log.warning("database unavailable at startup, migrations not applied", extra={"error": str(exc)})
    if not hasattr(app.state, "dispatcher"):  # tests inject their own
        app.state.dispatcher = N8nDispatcher.from_settings()
    log.info("startup complete", extra={"environment": get_settings().environment,
                                        "dispatch_mode": app.state.dispatcher.mode})
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    app = FastAPI(
        title="NexusFlow Autopilot API",
        version=API_VERSION,
        summary="IT asset, license and security automation: RAG copilot, governance agent, HITL approvals, n8n.",
        openapi_tags=TAGS,
        lifespan=lifespan,
        generate_unique_id_function=_operation_id,
    )
    # add_middleware prepends, so the last one added is outermost:
    # RequestContext -> CORS -> CrashToProblem -> (Starlette exception handlers) -> routes
    app.add_middleware(CrashToProblemMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER,
                                      "X-NexusFlow-Timestamp", "X-NexusFlow-Signature"],
                       expose_headers=[REQUEST_ID_HEADER], max_age=600)
    app.add_middleware(RequestContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(api_router)

    default_openapi = app.openapi

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = default_openapi()
        # register Problem so problem_responses()' $refs resolve even if no route uses it as a model
        schema.setdefault("components", {}).setdefault("schemas", {})["Problem"] = Problem.model_json_schema(
            ref_template="#/components/schemas/{model}")
        return schema

    app.openapi = openapi
    return app


app = create_app()
