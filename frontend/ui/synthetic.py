"""Sample mode: what the console shows when no API is connected, such as on Streamlit Community Cloud.

SampleClient has the same methods as NexusFlowClient and serves ui/sample_data.json, a capture of the local
API over the repo's synthetic seed data (scripts/capture_sample_data.py). Timestamps are moved forward on
load so the data stays recent, and the Overview's time series is generated from a fixed seed so every window
has something to show. The console labels all of it as sample data on every page. Sample mode is read-only
and holds no tokens.

It turns on only when the API is down and either no API URL was configured or NEXUSFLOW_ENV is "production".
An explicit URL on a developer's machine keeps showing "API offline", so an outage there is never hidden.
"""

from __future__ import annotations

import copy
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ui.api import ApiError, configured_api_url

DATA_FILE = Path(__file__).with_name("sample_data.json")
QUESTIONS = (
    "What is the Enterprise P1 initial response and resolution SLA?",
    "CUST-0040 wants 50 more seats. Does that need Finance approval?",
    "Can we mark CUST-0011 as churned if they say payment was sent?",
)
CREDENTIAL = re.compile(r"\beyJ[\w-]{6,}\.[\w-]{6,}|\bBearer\s+\w|\bsha256=[0-9a-f]{8}|\bAIza[\w-]{8}|\bv1\.[\w-]{8,}\.[\w-]{8,}")
WINDOW_HOURS = {"24h": (24, 1), "7d": (168, 6), "30d": (720, 24)}      # window hours, bucket hours
WINDOW_SCALE = {"24h": 1 / 7, "7d": 1.0, "30d": 1.0}                   # n8n keeps 7 days, so 30d matches 7d
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_EVENT_KEYS = ("at", "updated_at", "last_run_at")
SAMPLE_ME = {"subject": "DEMO-VISITOR", "name": "Demo visitor", "kind": "user", "roles": ["OPERATOR"],
             "effective_roles": ["OPERATOR"], "auth": "sample"}


def should_use(health: dict) -> bool:
    if health.get("status") != "down":
        return False
    return not configured_api_url() or os.environ.get("NEXUSFLOW_ENV", "").lower() == "production"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _walk(value, fn, key: str = ""):
    if isinstance(value, dict):
        return {k: _walk(v, fn, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, fn, key) for v in value]
    return fn(value, key)


def _latest_event(data: dict) -> datetime:
    latest = [_parse(data["captured_at"]) - timedelta(days=30)]

    def collect(value, key):
        if key in _EVENT_KEYS and isinstance(value, str) and _TIMESTAMP.match(value):
            latest.append(_parse(value))
        return value

    _walk(data, collect)
    return max(latest)


def load(now: datetime | None = None) -> dict:
    """The dataset with every timestamp moved so its newest event happened a few minutes ago."""
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    shift = (now or datetime.now(timezone.utc)) - timedelta(minutes=5) - _latest_event(data)

    def move(value, key):
        if isinstance(value, str) and _TIMESTAMP.match(value):
            return (_parse(value) + shift).isoformat()
        return value

    return _walk(data, move)


def not_available(what: str) -> ApiError:
    return ApiError(0, "SAMPLE_MODE", f"{what} needs a connected API, and this deployment is showing sample data")


class SampleClient:
    """Drop-in for NexusFlowClient with no network access."""

    sample = True
    base_url = "None (sample data)"
    token = ""

    def __init__(self, now: datetime | None = None):
        self.now = now or datetime.now(timezone.utc)
        self.data = load(self.now)

    def _get(self, key: str) -> dict:
        return copy.deepcopy(self.data[key])

    # ------------------------------------------------------------------ same names as NexusFlowClient

    def health(self) -> dict:
        offline = {"status": "not_configured", "detail": "Not connected in sample mode"}
        return {"status": "sample", "version": "sample", "environment": "sample", "uptime_seconds": 0,
                "dispatch_mode": "none", "components": {k: dict(offline) for k in ("database", "pgvector", "n8n", "gemini")}}

    def me(self) -> dict:
        return dict(SAMPLE_ME)

    def dashboard(self, activity_limit: int = 12) -> dict:
        d = self._get("dashboard")
        d["recent_activity"] = d["recent_activity"][:activity_limit]
        return d

    def activity(self, limit: int = 30) -> dict:
        d = self._get("activity")
        d["items"] = d["items"][:limit]
        d["count"] = len(d["items"])
        return d

    def metrics(self) -> dict:
        return self._get("metrics")

    def pending_approvals(self) -> dict:
        return self._get("pending_approvals")

    def workflow_runs(self, window: str = "24h", limit: int = 12) -> dict:
        d = self._get("workflow_runs")
        d["window"] = window
        d["items"] = d["items"][:limit]
        return d

    def governance_runs(self, limit: int = 20, active_only: bool = False) -> dict:
        runs = self._get("governance_runs")
        if active_only:
            runs = [r for r in runs if r["state"] not in ("EXECUTED", "REJECTED", "FAILED")]
        return {"runs": runs[:limit]}

    def run(self, run_id: str) -> dict:
        if run_id not in self.data["runs"]:
            raise ApiError(404, "RUN_NOT_FOUND", "That run isn't in the sample data")
        return copy.deepcopy(self.data["runs"][run_id])

    def n8n_workflows(self, window: str = "7d") -> dict:
        d, scale = self._get("n8n_workflows"), WINDOW_SCALE[window]
        hours = WINDOW_HOURS[window][0]
        d.update(window=window, window_start=(self.now - timedelta(hours=hours)).isoformat(),
                 telemetry_available=True, window_exceeds_retention=hours > d["retention_hours"])
        for w in d["workflows"]:
            for field in ("executions", "succeeded", "failed", "running"):
                w[field] = round(w[field] * scale)
            for node in w["nodes"]:
                node["runs"], node["errors"] = round(node["runs"] * scale), round(node["errors"] * scale)
                if not node["runs"]:
                    node.update(state="idle", success_rate=None, avg_ms=None, errors=0)
        return d

    def ai_agents(self, window: str = "7d") -> dict:
        d = self._get("ai_agents")
        d.update(window=window, window_start=(self.now - timedelta(hours=WINDOW_HOURS[window][0])).isoformat())
        return d

    def operations(self, window: str = "24h") -> dict:
        """The captured structure, with a series generated from a fixed seed and KPIs computed from that series."""
        d = self._get("operations")
        hours, bucket = WINDOW_HOURS[window]
        rng = random.Random(f"nexusflow-{window}")
        start = self.now - timedelta(hours=hours)
        step = timedelta(hours=bucket)
        first = datetime.fromtimestamp((start.timestamp() // step.total_seconds()) * step.total_seconds(), tz=timezone.utc)
        series, t = [], first
        while t <= self.now:
            base = rng.randint(2, 9) * bucket
            series.append({"t": t.isoformat(), "successful": base, "failed": rng.choice((0, 0, 0, 1)),
                           "human_review": rng.choice((0, 0, 1))})
            t += step
        total = sum(p["successful"] + p["failed"] + p["human_review"] for p in series)
        failed = sum(p["failed"] for p in series)
        rate = round(100.0 * (total - failed) / total, 1)
        previous, previous_rate = round(total * rng.uniform(0.82, 0.97)), round(rate - rng.uniform(0.1, 0.9), 1)
        hours_saved = round(total * 14 / 60, 1)
        previous_hours = round(hours_saved * previous / total, 1)
        pct = lambda now_v, prev_v: round(100.0 * (now_v - prev_v) / prev_v, 1)     # noqa: E731
        k = d["kpis"]
        k["active_workflows"].update(value=4, previous=4, delta_pct=0.0, of_total=4)
        k["total_executions"].update(value=total, previous=previous, delta_pct=pct(total, previous))
        k["success_rate"].update(value=rate, previous=previous_rate, delta_points=round(rate - previous_rate, 1))
        k["time_saved_hours"].update(value=hours_saved, previous=previous_hours, delta_pct=pct(hours_saved, previous_hours),
                                     estimated=True)
        k["active_agents"].update(value=6, previous=6, delta_pct=0.0, of_total=6)

        weights = {"hitl_approval_notification": 4, "seat_expansion_trigger": 3, "delinquent_invoice_reconciliation": 2,
                   "governance_agent": 2, "p1_ticket_escalation": 2, "rag_copilot": 1}
        share = total / sum(weights.values())
        labels = {a["key"]: (a["name"], a["kind"]) for a in d["agents"]}
        d["workflows"] = [{"key": key, "name": labels[key][0], "kind": labels[key][1], "executions": max(1, round(share * n)),
                           "success_rate": rate, "human_review": sum(p["human_review"] for p in series) if key == "governance_agent" else 0,
                           "last_run_at": (self.now - timedelta(minutes=5 + 7 * i)).isoformat()}
                          for i, (key, n) in enumerate(weights.items())]
        runs = {w["key"]: w["executions"] for w in d["workflows"]}
        pending = self.data["pending_approvals"]["count"]
        for a in d["agents"]:
            waiting = a["key"] == "governance_agent" and pending
            a.update(runs=runs[a["key"]], status="in_review" if waiting else "active",
                     detail=f"{pending} approvals waiting" if waiting else f'{runs[a["key"]]} runs in window',
                     last_run_at=(self.now - timedelta(minutes=9)).isoformat())
        d["agent_status"] = {s: sum(a["status"] == s for a in d["agents"]) for s in ("active", "idle", "in_review", "error")}
        d.update(generated_at=self.now.isoformat(), window=window, window_start=start.isoformat(), bucket_hours=bucket,
                 series=series)
        return d

    def ask(self, question: str, top_k: int = 6) -> dict:
        if question.strip() in self.data["answers"]:
            return copy.deepcopy(self.data["answers"][question.strip()])
        raise not_available("A new copilot question")

    def search(self, query: str, top_k: int = 6) -> dict:
        if query.strip() in self.data["searches"]:
            return copy.deepcopy(self.data["searches"][query.strip()])
        raise not_available("A new document search")

    def decision_token(self, approval_id: str) -> dict:
        raise not_available("Deciding an approval")

    def decide(self, *args, **kwargs) -> dict:
        raise not_available("Deciding an approval")

    def approve_or_reject(self, *args, **kwargs) -> dict:
        raise not_available("Deciding an approval")
