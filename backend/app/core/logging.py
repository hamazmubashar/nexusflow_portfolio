"""Structured logging + request-ID tracing.

Every request gets an id: the caller's X-Request-ID if it is a sane token, otherwise a new
UUID. It is stored in a contextvar (so any log line and every problem+json body carries it)
and echoed back in the X-Request-ID response header. One access-log line per request:

    {"ts": "...", "level": "INFO", "logger": "nexusflow.access", "msg": "request",
     "request_id": "...", "method": "POST", "path": "/api/v1/...", "status": 202, "duration_ms": 41}

Implemented as plain ASGI middleware so it doesn't interfere with BackgroundTasks or streaming.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
access_log = logging.getLogger("nexusflow.access")


def get_request_id() -> str | None:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    RESERVED = set(vars(logging.makeLogRecord({})))

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or get_request_id(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in self.RESERVED and k not in entry})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else
                         logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "uvicorn.access"):  # replaced by nexusflow.access
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.lower().encode(), b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else str(uuid4())
        token = _request_id.set(request_id)
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message["headers"] = [*message.get("headers", []),
                                      (REQUEST_ID_HEADER.lower().encode(), request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            access_log.info("request", extra={
                "method": scope["method"], "path": scope["path"], "status": status_holder["status"],
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            })
            _request_id.reset(token)
