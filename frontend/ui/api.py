"""Thin client for the NexusFlow Autopilot API (FastAPI on :8010).

The UI holds no secrets: it authenticates with a short-lived user JWT passed in by run.sh
(NEXUSFLOW_API_TOKEN). Every error the API returns is RFC 7807 problem+json; it surfaces
here as ApiError with the stable `code`, so screens can say exactly what went wrong.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8010"


@dataclass
class ApiError(Exception):
    status: int
    code: str
    detail: str
    body: dict | None = None

    def __str__(self) -> str:
        return f"{self.detail} ({self.code}, HTTP {self.status})"


class NexusFlowClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or os.environ.get("NEXUSFLOW_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get("NEXUSFLOW_API_TOKEN", "")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport,
                                  headers={"User-Agent": "nexusflow-operator-ui/1.0"})

    # ------------------------------------------------------------------ plumbing

    def _request(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                 timeout: float | None = None, auth: bool = True, ok_statuses: tuple[int, ...] = ()) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
        try:
            r = self._http.request(method, path, json=json, params=params, headers=headers,
                                   timeout=timeout or httpx.USE_CLIENT_DEFAULT)
        except httpx.TimeoutException:
            raise ApiError(0, "TIMEOUT", f"The API at {self.base_url} did not answer in time") from None
        except httpx.TransportError:
            raise ApiError(0, "API_UNREACHABLE", f"Can't reach the API at {self.base_url}. Is uvicorn running?") from None
        if r.is_success or r.status_code in ok_statuses:
            return r.json()
        try:
            body = r.json()
        except ValueError:
            body = {}
        raise ApiError(r.status_code, body.get("code", f"HTTP_{r.status_code}"),
                       body.get("detail") or r.reason_phrase, body)

    # ------------------------------------------------------------------ endpoints

    def health(self) -> dict:
        return self._request("GET", "/health", auth=False, ok_statuses=(503,), timeout=10)

    def me(self) -> dict:
        return self._request("GET", "/api/v1/auth/me")

    def dashboard(self, activity_limit: int = 12) -> dict:
        return self._request("GET", "/api/v1/analytics/dashboard", params={"activity_limit": activity_limit})

    def operations(self, window: str = "24h") -> dict:
        return self._request("GET", "/api/v1/analytics/operations", params={"window": window})

    def activity(self, limit: int = 30) -> dict:
        return self._request("GET", "/api/v1/analytics/activity", params={"limit": limit})

    def metrics(self) -> dict:
        return self._request("GET", "/api/v1/metrics")

    def pending_approvals(self) -> dict:
        return self._request("GET", "/api/v1/approvals/pending")

    def decision_token(self, approval_id: str) -> dict:
        return self._request("POST", f"/api/v1/approvals/{approval_id}/decision-token")

    def decide(self, token: str, action: str, approver_id: str, comment: str | None = None,
               approver_signature: str | None = None) -> dict:
        body = {"action": action, "approver_id": approver_id}
        if comment:
            body["comment"] = comment
        if approver_signature:
            body["approver_signature"] = approver_signature
        return self._request("POST", f"/api/v1/approvals/{token}/action", json=body, timeout=60)

    def approve_or_reject(self, approval_id: str, action: str, approver_id: str, comment: str | None = None,
                          approver_signature: str | None = None) -> dict:
        """Mint a short-lived decision token for this approval, then use it on the action route."""
        minted = self.decision_token(approval_id)
        return self.decide(minted["token"], action, approver_id, comment, approver_signature)

    def ask(self, question: str, top_k: int = 6) -> dict:
        return self._request("POST", "/api/v1/copilot/ask", json={"question": question, "top_k": top_k}, timeout=180)

    def search(self, query: str, top_k: int = 6) -> dict:
        return self._request("POST", "/api/v1/copilot/search", json={"query": query, "top_k": top_k}, timeout=60)

    def governance_runs(self, limit: int = 20, active_only: bool = False) -> dict:
        return self._request("GET", "/api/v1/agents/governance/runs",
                             params={"limit": limit, "active_only": str(active_only).lower()})

    def workflow_runs(self, window: str = "24h", limit: int = 12) -> dict:
        return self._request("GET", "/api/v1/analytics/workflow-runs", params={"window": window, "limit": limit})

    def run(self, run_id: str) -> dict:
        return self._request("GET", f"/api/v1/agents/governance/runs/{run_id}")
