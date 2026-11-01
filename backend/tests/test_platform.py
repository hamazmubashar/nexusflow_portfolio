"""n8n Workflows / AI Agents data: telemetry parsing, redaction, structure, and API access control."""

import json
from datetime import datetime, timezone

import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.security import issue_jwt
from app.main import app
from app.schemas.analytics import AgentsResponse, WorkflowsResponse
from app.services.analytics import platform

SECRET = "v1.c2VjcmV0LWNsYWltcw.c2VjcmV0LXNpZw"


def stored_execution(node_payload: str = SECRET) -> str:
    """n8n's "flatted" format: objects hold string indexes into the flat array."""
    return json.dumps([
        {"resultData": "1"},                                     # 0
        {"runData": "2", "lastNodeExecuted": "9"},               # 1
        {"Hook": "3", "Check": "5"},                             # 2
        ["4"],                                                   # 3
        {"startTime": 1, "executionTime": 2, "executionStatus": "7", "data": "8"},   # 4
        ["6"],                                                   # 5
        {"startTime": 2, "executionTime": 40, "executionStatus": "10", "data": "8"},  # 6
        "success",                                               # 7
        {"main": node_payload},                                  # 8
        "Check",                                                 # 9
        "error",                                                 # 10
    ])


def test_node_runs_returns_only_name_duration_and_status():
    runs = platform.node_runs(stored_execution())
    assert runs == [("Hook", 2, "success"), ("Check", 40, "error")]
    assert SECRET not in repr(runs)


@pytest.mark.parametrize("value", [
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln", "Bearer abc.def-ghi", "sha256=" + "ab" * 32, SECRET,
    "AIzaSyA1234567890abcdefghijk", "postgresql://nexusflow:hunter2@localhost/db",
])
def test_sanitize_strips_credential_shapes_inside_text(value):
    out = platform.sanitize({"detail": f"before {value} after", "nested": [value]})
    assert value not in json.dumps(out) and "before" in out["detail"] and platform.REDACTED in out["nested"][0]


@pytest.mark.parametrize("key", ["Authorization", "approver_signature", "X-NexusFlow-Signature", "api_key", "token",
                                 "GEMINI_API_KEY", "password"])
def test_sanitize_redacts_sensitive_keys(key):
    assert platform.sanitize({key: "anything"}) == {key: platform.REDACTED}


def test_sanitize_leaves_ordinary_values_alone():
    data = {"name": "Verify signature", "runs": 3, "at": datetime(2026, 1, 1, tzinfo=timezone.utc), "ok": None}
    assert platform.sanitize(data) == data


@pytest.mark.parametrize("spec", platform.WORKFLOWS, ids=lambda s: s["key"])
def test_structure_exposes_no_node_parameters(spec):
    structure = platform.load_structure(spec["file"])
    assert all(set(n) == {"name", "role"} for n in structure["nodes"])
    assert all(set(e) == {"source", "target", "branch"} for e in structure["edges"])
    assert {n["role"] for n in structure["nodes"]} & {"Webhook trigger"} and any(
        n["role"] == "HMAC SHA-256" for n in structure["nodes"])


@pytest.mark.parametrize("spec", platform.WORKFLOWS, ids=lambda s: s["key"])
def test_sample_inputs_are_synthetic_and_carry_no_signature(spec):
    sample = spec["sample_input"]
    assert sample["headers"]["X-NexusFlow-Signature"] == platform.REDACTED
    assert platform.sanitize(sample) == sample and "SAMPLE" in json.dumps(sample) or "TK-0000" in json.dumps(sample)


def test_real_workflows_map_to_expected_roles():
    roles = {n["name"]: n["role"] for s in platform.WORKFLOWS for n in platform.load_structure(s["file"])["nodes"]}
    assert roles["Evaluate $500 threshold"] == "Rule engine" and roles["Ask RAG copilot"] == "AI copilot"
    assert roles["Evaluated?"] == "Decision"
    assert roles["Submit decision"] == "Governance agent" and roles["Post to channel"] == "Notification dispatch"
    assert roles["Reject: bad signature"] == "Response" and roles["Weekdays 09:00 Chicago"] == "Schedule trigger"


def test_unreachable_telemetry_reports_unknown_not_zero_success_and_hides_the_dsn():
    data = platform.workflows_summary("7d", dsn="postgresql://someuser:hunter2@127.0.0.1:9/n8n")
    WorkflowsResponse(**data)
    assert data["telemetry_available"] is False
    assert all(w["state"] == "unknown" and w["success_rate"] is None for w in data["workflows"])
    assert all(n["state"] == "unknown" for w in data["workflows"] for n in w["nodes"])
    assert "hunter2" not in json.dumps(data, default=str) and "someuser" not in json.dumps(data, default=str)


def test_workflow_view_counts_failures_and_latency():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stats = {"executions": [("success", t0, t0.replace(second=1)), ("error", t0, t0.replace(second=3)),
                            ("running", t0, None)],
             "nodes": {"Verify signature": [2, 1, 30]}}
    view = platform._workflow_view(platform.WORKFLOWS[2], stats, True, True)
    assert (view["executions"], view["succeeded"], view["failed"], view["running"]) == (3, 1, 1, 1)
    assert view["success_rate"] == 50.0 and view["avg_latency_ms"] == 2000.0 and view["state"] == "degraded"
    node = next(n for n in view["nodes"] if n["name"] == "Verify signature")
    assert (node["state"], node["runs"], node["success_rate"], node["avg_ms"]) == ("fail", 2, 50.0, 15.0)
    assert next(n for n in view["nodes"] if n["name"] == "Post alert to channel")["state"] == "idle"


def test_catalog_is_six_agents_and_rule_based_ones_claim_no_model():
    catalog = platform._agents_catalog()
    assert len(catalog) == 6 and all(a["tools"] and a["constraints"] for a in catalog)
    assert {a["key"] for a in catalog if not a["models"]} == {"governance", "contract_analyzer", "collections"}


def test_copilot_trace_keeps_allow_listed_fields_only():
    now = datetime.now(timezone.utc)
    ok = {"created_at": now, "status": "PROCESSED", "subject_id": None,
          "payload": {"question": "private question", "asked_by": "EMP-1", "grounded": True, "citations": 2, "model": "m"}}
    bad = {"created_at": now, "status": "FAILED", "subject_id": None, "payload": {"error": "boom key=AIzaSecret"}}
    assert "private question" not in json.dumps(platform._copilot_trace(ok), default=str)
    assert "boom" not in json.dumps(platform._copilot_trace(bad), default=str)


# --------------------------------------------------------------------------- API (needs PostgreSQL)

SETTINGS = get_settings()


def _db_available() -> bool:
    try:
        psycopg2.connect(SETTINGS.psycopg2_dsn, connect_timeout=2).close()
        return True
    except psycopg2.OperationalError:
        return False


needs_db = pytest.mark.skipif(not (_db_available() and SETTINGS.webhook_signing_secret),
                              reason="needs PostgreSQL and WEBHOOK_SIGNING_SECRET")
ROUTES = [("/api/v1/analytics/n8n-workflows", WorkflowsResponse), ("/api/v1/analytics/ai-agents", AgentsResponse)]


@needs_db
@pytest.mark.parametrize("path, schema", ROUTES)
def test_routes_require_a_token(path, schema):
    assert TestClient(app).get(path).status_code == 401


@needs_db
@pytest.mark.parametrize("window", ["24h", "7d", "30d"])
@pytest.mark.parametrize("path, schema", ROUTES)
def test_operator_reads_valid_responses_with_no_configured_secret(path, schema, window):
    token = issue_jwt("EMP-0001", ["OPERATOR"])
    r = TestClient(app).get(path, params={"window": window}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    schema(**r.json())
    secrets = [SETTINGS.webhook_signing_secret, SETTINGS.gemini_api_key, SETTINGS.jwt_secret,
               SETTINGS.approval_token_secret, token]
    assert not [s for s in secrets if s and s in r.text]
    assert platform.sanitize(r.json()) == r.json()
