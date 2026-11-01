"""Stage 5: OpenAPI contract, health/metrics/analytics shapes, RBAC, RFC 7807 errors, middleware."""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.api import health as health_module
from app.api.v1.copilot import get_retriever
from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.core.security import issue_jwt, jwt_key, sign_headers
from app.main import app
from app.schemas.analytics import DashboardResponse, HealthResponse, MetricsResponse
from app.services.n8n.dispatcher import N8nDispatcher
from app.services.rag.retriever import RetrievedChunk

SETTINGS = get_settings()
DSN = SETTINGS.psycopg2_dsn
PROBLEM = "application/problem+json"


def _db_available() -> bool:
    try:
        psycopg2.connect(DSN, connect_timeout=2).close()
        return True
    except psycopg2.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not (_db_available() and SETTINGS.webhook_signing_secret),
                                reason="needs PostgreSQL and WEBHOOK_SIGNING_SECRET")


def query(sql, params=()):
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def bearer(subject="EMP-0057", roles=("OPERATOR",), **kw) -> dict:
    return {"Authorization": f"Bearer {issue_jwt(subject, list(roles), **kw)}"}


OPERATOR = bearer("EMP-0057", ["OPERATOR"])
MANAGER = bearer("EMP-0082", ["MANAGER"])
ADMIN = bearer("EMP-0001", ["ADMIN"])


@pytest.fixture(scope="module")
def client():
    app.state.dispatcher = N8nDispatcher.from_settings(mode="mock")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    del app.state.dispatcher
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def spec(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    return r.json()


def assert_problem(r, status: int, code: str | None = None) -> dict:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith(PROBLEM)
    body = r.json()
    assert {"type", "title", "status", "detail", "instance", "code", "request_id"} <= body.keys()
    assert body["status"] == status and isinstance(body["detail"], str)
    assert body["type"].startswith("https://nexusflow.example/problems/")
    assert body["request_id"] == r.headers["x-request-id"]
    if code:
        assert body["code"] == code
    return body


# --------------------------------------------------------------------------- OpenAPI

EXPECTED_OPERATIONS = {
    ("get", "/health"), ("get", "/api/v1/metrics"), ("get", "/api/v1/auth/me"),
    ("post", "/api/v1/copilot/ask"), ("post", "/api/v1/copilot/search"),
    ("post", "/api/v1/agents/governance/runs"), ("get", "/api/v1/agents/governance/runs/{run_id}"),
    ("get", "/api/v1/approvals/pending"), ("get", "/api/v1/approvals/{approval_id}"),
    ("post", "/api/v1/approvals/{token}/action"),
    ("post", "/api/v1/approvals/{approval_id}/decision-token"),
    ("post", "/api/v1/webhooks/tickets/escalate"), ("post", "/api/v1/webhooks/seats/request"),
    ("post", "/api/v1/webhooks/invoices/delinquent"), ("get", "/api/v1/webhooks/invoices/overdue"),
    ("post", "/api/v1/webhooks/n8n/callback"),
    ("get", "/api/v1/analytics/dashboard"), ("get", "/api/v1/analytics/operations"),
    ("get", "/api/v1/analytics/workflow-runs"), ("get", "/api/v1/agents/governance/runs"), ("get", "/api/v1/analytics/activity"), ("get", "/api/v1/analytics/sla"),
}


def operations(spec):
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            yield method, path, op


def test_openapi_lists_every_route_under_v1(spec):
    assert spec["openapi"].startswith("3.1")
    assert spec["info"]["title"] == "NexusFlow Autopilot API" and spec["info"]["version"] == app.version
    assert {(m, p) for m, p, _ in operations(spec)} == EXPECTED_OPERATIONS
    assert all(p == "/health" or p.startswith("/api/v1/") for _, p, _ in operations(spec))


def test_operation_ids_are_unique_and_tagged(spec):
    declared = {t["name"] for t in spec["tags"]}
    ids = []
    for _, _, op in operations(spec):
        assert len(op["tags"]) == 1 and op["tags"][0] in declared
        assert op["operationId"].startswith(op["tags"][0] + "_")
        ids.append(op["operationId"])
    assert len(ids) == len(set(ids))


def test_security_schemes_and_problem_schema(spec):
    schemes = spec["components"]["securitySchemes"]
    assert schemes["bearerAuth"] == {"type": "http", "scheme": "bearer",
                                     "description": schemes["bearerAuth"]["description"]}
    assert schemes["hmacSignature"]["type"] == "apiKey" and schemes["hmacSignature"]["in"] == "header"
    assert schemes["hmacSignature"]["name"] == "X-NexusFlow-Signature"
    problem = spec["components"]["schemas"]["Problem"]
    assert set(problem["required"]) == {"type", "title", "status", "detail", "instance", "code"}


def test_protected_routes_declare_security_and_problem_errors(spec):
    for method, path, op in operations(spec):
        if path == "/health":
            assert "security" not in op
            continue
        names = {k for req in op.get("security", []) for k in req}
        if "/webhooks/" in path:
            assert names == {"hmacSignature"}, path
        else:
            assert names == {"bearerAuth", "hmacSignature"}, path
        resp401 = op["responses"]["401"]
        assert resp401["content"][PROBLEM]["schema"]["$ref"] == "#/components/schemas/Problem", path


def test_all_schema_refs_resolve(spec):
    refs = set()

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                refs.add(node["$ref"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(spec)
    schemas = spec["components"]["schemas"]
    missing = [r for r in refs if r.split("/")[-1] not in schemas]
    assert refs and missing == []


def test_success_responses_have_typed_schemas(spec):
    for method, path, op in operations(spec):
        ok = next(v for k, v in op["responses"].items() if k.startswith("2"))
        schema = ok["content"]["application/json"]["schema"]
        assert "$ref" in schema, f"{method.upper()} {path} has an untyped success response"


def test_docs_are_served(client):
    assert client.get("/docs").status_code == 200


# --------------------------------------------------------------------------- /health


def test_health_shape_and_request_id(client):
    r = client.get("/health", headers={"X-Request-ID": "trace-12345678"})
    assert r.status_code == 200
    body = HealthResponse.model_validate(r.json())
    assert body.status in ("ok", "degraded") and body.components.database.status == "ok"
    assert body.components.pgvector.extension_version
    assert r.headers["x-request-id"] == "trace-12345678"
    junk = client.get("/health", headers={"X-Request-ID": "bad id with spaces"})
    uuid.UUID(junk.headers["x-request-id"])  # replaced with a fresh UUID


def test_health_is_503_when_database_down(client, monkeypatch):
    down = {"status": "down", "detail": "OperationalError", "checked_at": datetime.now(timezone.utc)}
    monkeypatch.setattr(health_module, "check_database", lambda: (down, {**down}))
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "down"


def test_health_caches_network_probes(client, monkeypatch):
    calls = []

    async def fake_n8n():
        calls.append(1)
        return {"status": "ok", "latency_ms": 1.0, "detail": None, "checked_at": datetime.now(timezone.utc)}

    health_module._cache.clear()
    monkeypatch.setattr(health_module, "check_n8n", fake_n8n)
    client.get("/health")
    client.get("/health")
    assert len(calls) == 1
    health_module._cache.clear()


# --------------------------------------------------------------------------- /metrics + analytics


def test_metrics_requires_auth(client):
    r = client.get("/api/v1/metrics")
    assert_problem(r, 401, "AUTHENTICATION_REQUIRED")
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_metrics_shape_matches_database(client):
    r = client.get("/api/v1/metrics", headers=OPERATOR)
    assert r.status_code == 200
    m = MetricsResponse.model_validate(r.json())
    [(arr,)] = query("SELECT sum(annual_recurring_revenue) FROM contracts WHERE status = 'ACTIVE'")
    [(pending,)] = query("SELECT count(*) FROM approval_requests WHERE status = 'PENDING' AND expires_at > now()")
    assert m.arr.active == str(arr) and m.arr.baseline == "12400000.00"
    assert m.active_customers == 120 and m.pending_approvals == pending
    assert m.rates.sla_compliance_pct is None or 0 <= m.rates.sla_compliance_pct <= 100


def test_metrics_accepts_signed_service_requests(client):
    r = client.get("/api/v1/metrics", headers=sign_headers(SETTINGS.webhook_signing_secret, b""))
    assert r.status_code == 200


def test_dashboard_shape(client):
    r = client.get("/api/v1/analytics/dashboard", params={"activity_limit": 5}, headers=MANAGER)
    assert r.status_code == 200
    d = DashboardResponse.model_validate(r.json())
    assert d.customers.total_customers == 120 and d.customers.active_customers == 120
    assert d.revenue.active_contracts == 120 and len(d.revenue.by_plan) == 3
    assert d.sla.resolved_tickets > 0 and 0 <= d.sla.compliance_rate_pct <= 100
    assert len(d.recent_activity) <= 5
    assert [a.at for a in d.recent_activity] == sorted((a.at for a in d.recent_activity), reverse=True)


def test_analytics_validation_errors_are_problems(client):
    body = assert_problem(client.get("/api/v1/analytics/activity", params={"limit": 0}, headers=OPERATOR),
                          422, "VALIDATION_FAILED")
    assert body["errors"][0]["loc"] == ["query", "limit"]


def test_sla_endpoint(client):
    r = client.get("/api/v1/analytics/sla", headers=OPERATOR)
    assert r.status_code == 200 and set(r.json()["by_severity"]) == {"P1", "P2", "P3", "P4"}


# --------------------------------------------------------------------------- authentication


def test_whoami_expands_role_hierarchy(client):
    assert client.get("/api/v1/auth/me", headers=MANAGER).json()["effective_roles"] == ["MANAGER", "OPERATOR"]
    assert client.get("/api/v1/auth/me", headers=ADMIN).json()["effective_roles"] == ["ADMIN", "MANAGER", "OPERATOR"]
    svc = client.get("/api/v1/auth/me", headers=sign_headers(SETTINGS.webhook_signing_secret, b"")).json()
    assert svc == {"subject": "n8n", "name": None, "kind": "service", "roles": ["SERVICE"],
                   "effective_roles": ["SERVICE"], "auth": "hmac"}


def _raw_jwt(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    claims = {"iss": SETTINGS.jwt_issuer, "aud": SETTINGS.jwt_audience, "sub": "EMP-0082", "roles": ["MANAGER"],
              "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp())}
    claims.update(overrides)
    key = overrides.pop("_key", None) or jwt_key()
    claims.pop("_key", None)
    return {"Authorization": f"Bearer {pyjwt.encode(claims, key, algorithm='HS256')}"}


@pytest.mark.parametrize("headers,code", [
    (_raw_jwt(exp=int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp())), "TOKEN_EXPIRED"),
    (_raw_jwt(aud="someone-else"), "INVALID_TOKEN"),
    (_raw_jwt(iss="evil"), "INVALID_TOKEN"),
    (_raw_jwt(_key=b"not-our-key-not-our-key-not-our-k"), "INVALID_TOKEN"),
    (_raw_jwt(roles=["SUPERUSER"]), "INVALID_TOKEN"),
    (_raw_jwt(roles=["SERVICE"]), "INVALID_TOKEN"),        # users can't mint machine identity
    (_raw_jwt(roles=[]), "INVALID_TOKEN"),
    ({"Authorization": "Bearer not.a.jwt"}, "INVALID_TOKEN"),
    ({"X-NexusFlow-Timestamp": "1", "X-NexusFlow-Signature": "sha256=00"}, "INVALID_SIGNATURE"),
])
def test_bad_credentials_are_rejected(client, headers, code):
    assert_problem(client.get("/api/v1/auth/me", headers=headers), 401, code)


def test_issue_jwt_refuses_non_user_roles():
    with pytest.raises(ValueError):
        issue_jwt("EMP-0001", ["SERVICE"])
    with pytest.raises(ValueError):
        issue_jwt("EMP-0001", [])


# --------------------------------------------------------------------------- authorization


@pytest.mark.parametrize("method,path", [
    ("post", "/api/v1/agents/governance/runs"), ("post", f"/api/v1/approvals/v1.x.y/action"),
    ("get", "/api/v1/approvals/pending"), ("get", "/api/v1/analytics/dashboard"),
    ("post", "/api/v1/copilot/ask"), ("post", "/api/v1/copilot/search"),
    ("get", f"/api/v1/agents/governance/runs/{uuid.uuid4()}"),
])
def test_protected_routes_reject_anonymous(client, method, path):
    assert_problem(getattr(client, method)(path), 401, "AUTHENTICATION_REQUIRED")


def test_operator_cannot_decide_approvals(client):
    r = client.post("/api/v1/approvals/v1.x.y/action", headers=OPERATOR,
                    json={"action": "APPROVE", "approver_id": "EMP-0057"})
    body = assert_problem(r, 403, "INSUFFICIENT_ROLE")
    assert body["required_roles"] == ["MANAGER", "SERVICE"]


def test_manager_cannot_decide_as_someone_else(client):
    r = client.post("/api/v1/approvals/v1.x.y/action", headers=bearer("EMP-0059", ["MANAGER"]),
                    json={"action": "APPROVE", "approver_id": "EMP-0082"})
    assert_problem(r, 403, "APPROVER_IDENTITY_MISMATCH")


def test_manager_reaches_token_validation(client):
    r = client.post("/api/v1/approvals/v1.x.y/action", headers=MANAGER,
                    json={"action": "APPROVE", "approver_id": "EMP-0082"})
    assert_problem(r, 401, "TOKEN_MALFORMED")  # authorized: now the approval token itself is checked


def test_admin_inherits_manager_but_not_service(client):
    r = client.post("/api/v1/approvals/v1.x.y/action", headers=ADMIN,
                    json={"action": "APPROVE", "approver_id": "EMP-0001"})
    assert_problem(r, 401, "TOKEN_MALFORMED")
    # webhooks are machine-only: a user JWT, even ADMIN, is not an HMAC signature
    assert_problem(client.post("/api/v1/webhooks/n8n/callback", headers=ADMIN, json={}), 401, "INVALID_SIGNATURE")


def test_operator_can_start_runs_and_gets_validation_problem(client):
    body = assert_problem(client.post("/api/v1/agents/governance/runs", headers=OPERATOR, json={"nope": 1}),
                          422, "VALIDATION_FAILED")
    assert body["errors"]


def test_unknown_resources_and_bad_ids(client):
    assert_problem(client.get(f"/api/v1/approvals/{uuid.uuid4()}", headers=OPERATOR), 404, "APPROVAL_NOT_FOUND")
    assert_problem(client.get("/api/v1/approvals/not-a-uuid", headers=OPERATOR), 422, "VALIDATION_FAILED")
    assert_problem(client.get(f"/api/v1/agents/governance/runs/{uuid.uuid4()}", headers=OPERATOR), 404, "RUN_NOT_FOUND")
    assert_problem(client.get("/api/v1/agents/governance/runs/123", headers=OPERATOR), 422, "VALIDATION_FAILED")


# --------------------------------------------------------------------------- errors + middleware


def test_unknown_route_and_method_are_problems(client):
    assert_problem(client.get("/api/v1/nope"), 404, "NOT_FOUND")
    assert_problem(client.get("/api/v1/webhooks/n8n/callback"), 405, "METHOD_NOT_ALLOWED")


def test_unhandled_errors_do_not_leak_internals(client, monkeypatch):
    from app.services.analytics import dashboard

    def boom(*a, **k):
        raise RuntimeError("password=hunter2 in a stack trace")

    monkeypatch.setattr(dashboard, "build_dashboard", boom)
    r = client.get("/api/v1/analytics/dashboard", headers=OPERATOR)
    body = assert_problem(r, 500, "INTERNAL_ERROR")   # also checks X-Request-ID == body.request_id
    assert "hunter2" not in r.text and body["detail"] == "An unexpected error occurred"
    r = client.get("/api/v1/analytics/dashboard", headers={**OPERATOR, "Origin": "http://localhost:3000"})
    assert r.status_code == 500 and r.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_preflight(client):
    ok = client.options("/api/v1/metrics", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "authorization" in ok.headers["access-control-allow-headers"].lower()
    simple = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert simple.headers["access-control-expose-headers"].lower() == "x-request-id"  # browsers can read the trace id
    denied = client.options("/api/v1/metrics", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in denied.headers


def test_json_log_lines_carry_request_id():
    record = logging.makeLogRecord({"name": "nexusflow.access", "levelno": logging.INFO, "levelname": "INFO",
                                    "msg": "request", "request_id": "trace-abcdefgh", "status": 200})
    line = json.loads(JsonFormatter().format(record))
    assert line["request_id"] == "trace-abcdefgh" and line["status"] == 200 and line["msg"] == "request"


# --------------------------------------------------------------------------- copilot search


def test_copilot_search_returns_typed_hits(client):
    class FakeRetriever:
        embedder = type("E", (), {"name": "fake/embedder@768"})()

        def search(self, query, top_k=5, doc_types=None):
            return [RetrievedChunk(chunk_id=str(uuid.uuid4()), source_file="sops/SOP_01_Ticket_Escalation.md",
                                   doc_id="SOP-01", doc_type="sops", title="SOP-01", section="1. Severity rubric",
                                   chunk_index=1, content="P1 ...", metadata={"authority": "governing"},
                                   rrf_score=0.0328, semantic_rank=1, similarity=0.76, keyword_rank=1,
                                   keyword_score=0.4)]

    app.dependency_overrides[get_retriever] = lambda: FakeRetriever()
    try:
        r = client.post("/api/v1/copilot/search", headers=OPERATOR, json={"query": "P1 SLA", "top_k": 3})
    finally:
        app.dependency_overrides.pop(get_retriever, None)
    assert r.status_code == 200
    hit = r.json()["results"][0]
    assert r.json()["embedding_model"] == "fake/embedder@768"
    assert hit["doc_id"] == "SOP-01" and hit["authority"] == "governing" and hit["semantic_rank"] == 1


# --------------------------------------------------------------------------- operations control panel

from app.schemas.analytics import OperationsResponse  # noqa: E402
from app.services.analytics import dashboard as dash  # noqa: E402

FUTURE_NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)   # far from any real data


@pytest.fixture
def planted_runs():
    """Synthetic workflow callbacks: 3 in the current 24h window (one failed), 1 in the previous one."""
    def cb(hours_ago: float, workflow: str, failed: bool = False):
        query("INSERT INTO automation_events (event_id, event_type, direction, source, status, payload, created_at) "
              "VALUES (gen_random_uuid(), %s, 'callback', 'pytest-ops', 'PROCESSED', %s::jsonb, %s) RETURNING 1",
              ("workflow.failed" if failed else "workflow.completed",
               json.dumps({"data": {"workflow": workflow}}), FUTURE_NOW - timedelta(hours=hours_ago)))

    cb(2.5, "seat_expansion_trigger")
    cb(2.2, "seat_expansion_trigger")
    cb(1.1, "p1_ticket_escalation", failed=True)
    cb(30, "delinquent_invoice_reconciliation")
    yield
    query("DELETE FROM automation_events WHERE source = 'pytest-ops' RETURNING 1")


def test_operations_kpis_series_and_agents(planted_runs):
    with psycopg2.connect(DSN, cursor_factory=__import__("psycopg2.extras").extras.RealDictCursor) as conn:
        with conn.cursor() as cur:
            ops = dash.operations_summary(cur, "24h", now=FUTURE_NOW)
    OperationsResponse.model_validate(ops)
    k = ops["kpis"]
    assert k["total_executions"] == {"value": 3, "previous": 1, "delta_pct": 200.0}
    assert k["active_workflows"]["value"] == 2 and k["active_workflows"]["previous"] == 1
    assert k["success_rate"]["value"] == pytest.approx(66.7) and k["success_rate"]["delta_points"] == pytest.approx(-33.3)
    # time saved counts successful runs only: 2 seat runs x 15 min = 0.5 h (previous: 1 invoice run x 25 min)
    assert k["time_saved_hours"] == {"value": 0.5, "previous": 0.4, "delta_pct": 25.0, "estimated": True}
    assert len(ops["series"]) in (24, 25) and ops["bucket_hours"] == 1
    assert sum(p["successful"] for p in ops["series"]) == 2 and sum(p["failed"] for p in ops["series"]) == 1
    assert [w["key"] for w in ops["workflows"]] == ["seat_expansion_trigger", "p1_ticket_escalation"]
    status = {a["key"]: a["status"] for a in ops["agents"]}
    assert status["p1_ticket_escalation"] == "error" and status["seat_expansion_trigger"] == "active"
    assert status["delinquent_invoice_reconciliation"] == "idle"   # ran only in the previous window
    assert sum(ops["agent_status"].values()) == len(dash.AGENTS)


def test_operations_with_empty_previous_window_has_no_fake_deltas(planted_runs):
    with psycopg2.connect(DSN, cursor_factory=__import__("psycopg2.extras").extras.RealDictCursor) as conn:
        with conn.cursor() as cur:
            # window (-4h, +20h] holds the 3 recent runs; previous (-28h, -4h] is empty (the other run is at -30h)
            ops = dash.operations_summary(cur, "24h", now=FUTURE_NOW + timedelta(hours=20))
    k = ops["kpis"]
    assert k["total_executions"] == {"value": 3, "previous": 0, "delta_pct": None}
    assert k["success_rate"]["previous"] is None and k["success_rate"]["delta_points"] is None


def test_operations_endpoint(client):
    r = client.get("/api/v1/analytics/operations", params={"window": "7d"}, headers=OPERATOR)
    assert r.status_code == 200
    ops = OperationsResponse.model_validate(r.json())
    assert ops.window == "7d" and ops.bucket_hours == 6 and set(ops.assumed_minutes_saved) == {a[0] for a in dash.AGENTS}
    assert_problem(client.get("/api/v1/analytics/operations", params={"window": "1y"}, headers=OPERATOR),
                   422, "VALIDATION_FAILED")
    assert_problem(client.get("/api/v1/analytics/operations"), 401, "AUTHENTICATION_REQUIRED")


@pytest.fixture
def planted_dispatches():
    """P1 escalations dispatched to n8n: one closed by a callback, one in flight, one stale (no callback)."""
    def ev(direction, event_type, minutes_ago, data, status="DELIVERED"):
        query("INSERT INTO automation_events (event_id, event_type, direction, source, subject_id, status, payload, "
              "created_at) VALUES (gen_random_uuid(), %s, %s, 'pytest-ops', 'TK-0139', %s, %s::jsonb, %s) RETURNING 1",
              (event_type, direction, status, json.dumps({"data": data}), FUTURE_NOW - timedelta(minutes=minutes_ago)))

    for corr, minutes in (("c-closed", 30), ("c-inflight", 2), ("c-stale", 45)):
        ev("outbound", "ticket.escalation_required", minutes, {"correlation_id": corr, "effective_severity": "P1"})
    ev("callback", "workflow.completed", 28, {"workflow": "p1_ticket_escalation", "correlation_id": "c-closed",
                                              "action": "escalation_alert_formatted",
                                              "outcome": {"alert": {"generic": {"severity": "P1"}}}}, "PROCESSED")
    yield
    query("DELETE FROM automation_events WHERE source = 'pytest-ops' RETURNING 1")


def test_workflow_board_states_and_ordering(planted_dispatches):
    with psycopg2.connect(DSN, cursor_factory=__import__("psycopg2.extras").extras.RealDictCursor) as conn:
        with conn.cursor() as cur:
            items = dash.workflow_board(cur, [], "24h", 20, now=FUTURE_NOW)
    p1 = [i for i in items if i["workflow"] == "p1_ticket_escalation"]
    assert [i["state"] for i in p1] == ["running", "failed", "completed"]   # attention order, not time order
    running, stale, done = p1
    assert running["steps_done"] == 2 and running["current_step"] == "Copilot triage"
    assert stale["detail"] == "no callback from n8n after 10 min"
    assert done["steps_done"] == done["steps_total"] == 4 and done["detail"] == "escalation alert formatted"
    assert {i["priority"] for i in p1} == {"critical"}


def test_board_and_run_list_endpoints_shape(client):
    from app.schemas.analytics import WorkflowBoard
    from app.schemas.governance import GovernanceRunList

    r = client.get("/api/v1/analytics/workflow-runs", params={"window": "7d", "limit": 5}, headers=OPERATOR)
    assert r.status_code == 200 and len(WorkflowBoard.model_validate(r.json()).items) <= 5
    r = client.get("/api/v1/agents/governance/runs", params={"limit": 3}, headers=OPERATOR)
    assert r.status_code == 200 and GovernanceRunList.model_validate(r.json()).count <= 3
    assert_problem(client.get("/api/v1/agents/governance/runs", params={"limit": 0}, headers=OPERATOR),
                   422, "VALIDATION_FAILED")
    assert_problem(client.get("/api/v1/analytics/workflow-runs"), 401, "AUTHENTICATION_REQUIRED")
