"""RFC 7807 problem details for every error response.

    HTTP/1.1 409 Conflict
    Content-Type: application/problem+json
    {"type": "https://nexusflow.example/problems/already-decided", "title": "Conflict", "status": 409,
     "detail": "approval is already APPROVED ...", "instance": "/api/v1/approvals/.../action",
     "code": "ALREADY_DECIDED", "request_id": "..."}

`code` is the stable, machine-readable identifier clients should branch on; `detail` is for
humans. Extension members (`errors` for validation failures, `run` for agent failures, ...)
are added at the top level as RFC 7807 §3.2 allows. Raise APIError anywhere; existing
HTTPException(detail={"code", "message", ...}) raises are translated into the same shape.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_request_id

log = logging.getLogger("nexusflow.errors")
PROBLEM_BASE = "https://nexusflow.example/problems/"
PROBLEM_MEDIA_TYPE = "application/problem+json"


class Problem(BaseModel):
    model_config = ConfigDict(extra="allow", json_schema_extra={"additionalProperties": True})

    type: str = Field(examples=[PROBLEM_BASE + "already-decided"])
    title: str = Field(examples=["Conflict"])
    status: int = Field(ge=400, le=599)
    detail: str
    instance: str
    code: str = Field(examples=["ALREADY_DECIDED"])
    request_id: str | None = None


class APIError(Exception):
    def __init__(self, status: int, code: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


def problem_response(request: Request, status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    body = {
        "type": PROBLEM_BASE + code.lower().replace("_", "-"),
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "request_id": get_request_id(),
        **jsonable_encoder(extra),
    }
    headers = {"WWW-Authenticate": 'Bearer realm="nexusflow-api"'} if status == 401 else None
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_MEDIA_TYPE, headers=headers)


DEFAULT_CODES = {400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                 405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 410: "GONE", 422: "VALIDATION_FAILED",
                 429: "RATE_LIMITED", 500: "INTERNAL_ERROR", 503: "SERVICE_UNAVAILABLE"}


def _from_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):  # our convention: {"code", "message", ...extras}
        extra = {k: v for k, v in detail.items() if k not in ("code", "message")}
        return problem_response(request, exc.status_code, detail.get("code") or DEFAULT_CODES.get(exc.status_code, "ERROR"),
                                detail.get("message") or HTTPStatus(exc.status_code).phrase, **extra)
    if isinstance(detail, list):  # pydantic error list from parse_event
        return problem_response(request, exc.status_code, "VALIDATION_FAILED",
                                "Request body failed schema validation", errors=detail)
    return problem_response(request, exc.status_code, DEFAULT_CODES.get(exc.status_code, "ERROR"),
                            str(detail) if detail else HTTPStatus(exc.status_code).phrase)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError):
        return problem_response(request, exc.status, exc.code, exc.detail, **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return _from_http_exception(request, exc)

    @app.exception_handler(HTTPException)
    async def _fastapi_http(request: Request, exc: HTTPException):
        return _from_http_exception(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in exc.errors()]
        return problem_response(request, 422, "VALIDATION_FAILED", "Request failed schema validation", errors=errors)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Never leak internals; the request_id ties the response to the logged traceback
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return problem_response(request, 500, "INTERNAL_ERROR", "An unexpected error occurred")


def problem_responses(*statuses: int) -> dict[int, dict]:
    """OpenAPI `responses=` entries documenting problem+json for the given status codes."""
    return {s: {"model": Problem, "description": HTTPStatus(s).phrase,
                "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}}}
            for s in statuses}


class CrashToProblemMiddleware:
    """Turn an unhandled exception into a 500 problem *inside* the request-id and CORS layers.

    Starlette's own catch-all (ServerErrorMiddleware) is the outermost layer, so without this
    a 500 would lose its X-Request-ID header, its request_id and its CORS headers.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            log.exception("unhandled error on %s %s", scope.get("method"), scope.get("path"))
            if started:  # headers already sent: nothing safe to do but let the connection fail
                raise
            from starlette.requests import Request as StarletteRequest

            response = problem_response(StarletteRequest(scope), 500, "INTERNAL_ERROR", "An unexpected error occurred")
            await response(scope, receive, send)
