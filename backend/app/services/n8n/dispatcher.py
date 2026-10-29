"""Async dispatcher: FastAPI -> n8n webhook triggers, HMAC-signed.

Modes (N8N_DISPATCH_MODE):
  live  - POST to n8n; transport errors, 429 and 5xx are retried with exponential
          backoff + jitter; other 4xx fail immediately (retrying won't fix them).
  mock  - nothing leaves the process; the signed request is kept in `outbox` and a
          synthetic 202 is returned. For offline tests and demos.
  auto  - live, but if n8n stays unreachable after all retries the event is reported as
          QUEUED_OFFLINE (callers log it to automation_events for replay) instead of raising.

The body is serialized once (compact JSON, UTF-8) and those exact bytes are signed and
sent, so n8n's verification over the raw body matches byte for byte.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.security import EVENT_ID_HEADER, EVENT_TYPE_HEADER, sign_headers

Mode = Literal["live", "mock", "auto"]
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Workflow webhook paths (n8n_workflows/*.json must expose exactly these)
P1_TICKET_ESCALATION = "nexusflow/p1-ticket-escalation"
SEAT_REQUEST = "nexusflow/seat-request"
DELINQUENT_INVOICE = "nexusflow/delinquent-invoice"
APPROVAL_EVENTS = "nexusflow/approval-events"        # governance agent -> HITL workflow
APPROVAL_DECISION_RELAY = "nexusflow/approval-decision"  # signed approver decisions -> API


class DispatchResult(BaseModel):
    event_id: str
    event_type: str
    workflow_path: str
    url: str
    mode: Mode
    outcome: Literal["DELIVERED", "MOCKED", "QUEUED_OFFLINE", "FAILED"]
    status_code: int | None = None
    attempts: int = 0
    duration_ms: int = 0
    response_body: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in ("DELIVERED", "MOCKED")


@dataclass
class OutboxEntry:
    url: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> dict:
        return json.loads(self.body)


def canonical_json(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


@dataclass
class N8nDispatcher:
    base_url: str
    secret: str
    mode: Mode = "auto"
    timeout_s: float = 10.0
    max_attempts: int = 3
    backoff_s: float = 0.5
    transport: httpx.AsyncBaseTransport | None = None
    outbox: list[OutboxEntry] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **overrides) -> "N8nDispatcher":
        s = settings or get_settings()
        params = dict(base_url=s.n8n_webhook_url, secret=s.webhook_signing_secret, mode=s.n8n_dispatch_mode,
                      timeout_s=s.n8n_dispatch_timeout_seconds, max_attempts=s.n8n_dispatch_max_attempts)
        params.update(overrides)
        return cls(**params)

    def build_request(self, workflow_path: str, event: BaseModel | dict) -> OutboxEntry:
        payload = event.model_dump(mode="json", by_alias=True) if isinstance(event, BaseModel) else event
        body = canonical_json(payload)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            EVENT_ID_HEADER: str(payload.get("event_id", "")),
            EVENT_TYPE_HEADER: str(payload.get("event_type", "")),
            "User-Agent": "nexusflow-autopilot/1.0",
            **sign_headers(self.secret, body),
        }
        return OutboxEntry(url=f"{self.base_url.rstrip('/')}/{workflow_path.lstrip('/')}", headers=headers, body=body)

    async def dispatch(self, workflow_path: str, event: BaseModel | dict) -> DispatchResult:
        if not self.secret:
            raise RuntimeError("WEBHOOK_SIGNING_SECRET is not configured; refusing to send unsigned events")
        req = self.build_request(workflow_path, event)
        base = dict(event_id=req.headers[EVENT_ID_HEADER], event_type=req.headers[EVENT_TYPE_HEADER],
                    workflow_path=workflow_path, url=req.url, mode=self.mode)
        started = time.monotonic()

        if self.mode == "mock":
            self.outbox.append(req)
            return DispatchResult(**base, outcome="MOCKED", status_code=202, attempts=0,
                                  response_body={"mock": True, "accepted": True})

        last_error: str | None = None
        last_status: int | None = None
        attempts = 0
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as client:
            for attempt in range(1, self.max_attempts + 1):
                attempts = attempt
                # Re-sign each attempt so retries stay inside the receiver's timestamp window
                headers = {**req.headers, **sign_headers(self.secret, req.body)}
                try:
                    resp = await client.post(req.url, content=req.body, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error, last_status = f"{type(exc).__name__}: {exc}".rstrip(": "), None
                else:
                    last_status = resp.status_code
                    if resp.is_success:
                        return DispatchResult(**base, outcome="DELIVERED", status_code=resp.status_code,
                                              attempts=attempt, duration_ms=_ms(started), response_body=_body(resp))
                    last_error = f"HTTP {resp.status_code}"
                    if resp.status_code not in RETRYABLE_STATUS:
                        return DispatchResult(**base, outcome="FAILED", status_code=resp.status_code, attempts=attempt,
                                              duration_ms=_ms(started), response_body=_body(resp), error=last_error)
                if attempt < self.max_attempts:
                    await asyncio.sleep(self.backoff_s * 2 ** (attempt - 1) * (1 + random.random() / 4))

        outcome = "QUEUED_OFFLINE" if self.mode == "auto" else "FAILED"
        if outcome == "QUEUED_OFFLINE":
            self.outbox.append(req)
        return DispatchResult(**base, outcome=outcome, status_code=last_status, attempts=attempts,
                              duration_ms=_ms(started), error=last_error)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:2000]
