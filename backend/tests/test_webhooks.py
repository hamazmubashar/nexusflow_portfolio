"""Stage 3: webhooks, signing, dispatcher and n8n workflow exports.

Tiers:
  * offline  - signing, schemas, SLA clocks, dispatcher (httpx.MockTransport), workflow JSON
  * db       - every webhook route through FastAPI's TestClient against live Postgres, with the
               dispatcher in mock mode (nothing reaches n8n)
  * e2e      - real round trips FastAPI <-> n8n. Needs `docker compose up -d`, the workflows
               imported + published, and uvicorn on :8010. Skipped otherwise.
               The P1 test also calls Gemini, so it is marked `live` too.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg2
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.copilot import get_copilot
from app.core.config import REPO_ROOT, get_settings
from app.core.security import (
    SIGNATURE_HEADER, TIMESTAMP_HEADER, SignatureError, compute_signature, sign_headers, verify_signature,
)
from app.main import app
from app.schemas.webhooks import SeatRequestEvent, TicketEscalationEvent
from app.services import sla
from app.services.n8n.dispatcher import (
    APPROVAL_DECISION_RELAY, APPROVAL_EVENTS, DELINQUENT_INVOICE, P1_TICKET_ESCALATION, SEAT_REQUEST, N8nDispatcher, canonical_json,
)

SETTINGS = get_settings()
SECRET = SETTINGS.webhook_signing_secret
DSN = SETTINGS.psycopg2_dsn
WORKFLOW_DIR = REPO_ROOT / "n8n_workflows"
WORKFLOW_FILES = sorted(WORKFLOW_DIR.glob("*_workflow.json"))
API = "/api/v1/webhooks"
PYTEST_EVENT_IDS: list[str] = []


def _db_available() -> bool:
    try:
        psycopg2.connect(DSN, connect_timeout=2).close()
        return True
    except psycopg2.OperationalError:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="PostgreSQL not reachable at DATABASE_URL")
pytestmark = pytest.mark.skipif(not SECRET, reason="WEBHOOK_SIGNING_SECRET not set in .env")


def query(sql, params=()):
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else None


def event(event_type: str, data: dict, event_id: str | None = None) -> dict:
    eid = event_id or str(uuid.uuid4())
    PYTEST_EVENT_IDS.append(eid)
    return {"event_id": eid, "event_type": event_type, "occurred_at": datetime.now(timezone.utc).isoformat(),
            "source": "pytest", "data": data}


def signed_post(client, path: str, payload, *, secret=SECRET, ts=None, raw: bytes | None = None):
    body = raw if raw is not None else canonical_json(payload)
    headers = {"content-type": "application/json", **sign_headers(secret, body, ts)}
    return client.post(path, content=body, headers=headers)


# --------------------------------------------------------------------------- offline: signing


def test_signature_roundtrip_and_tamper_detection():
    body = canonical_json({"a": 1, "name": "Nexus Client 0040 LLC", "note": "é"})
    headers = sign_headers(SECRET, body, timestamp=1_800_000_000)
    verify_signature(SECRET, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], body, 300, now=1_800_000_100)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_signature(SECRET, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], body + b" ", 300,
                         now=1_800_000_100)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_signature(SECRET, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], body, 300,
                         now=1_800_000_301)
    with pytest.raises(SignatureError, match="missing"):
        verify_signature(SECRET, None, None, body, 300)
    with pytest.raises(SignatureError, match="not configured"):
        verify_signature("", "1", "sha256=x", body, 300)


def test_signature_binds_timestamp():
    body = b"{}"
    assert compute_signature(SECRET, "100", body) != compute_signature(SECRET, "101", body)


def test_canonical_json_is_compact_utf8():
    assert canonical_json({"b": "é", "a": [1, 2]}) == '{"b":"é","a":[1,2]}'.encode("utf-8")


# --------------------------------------------------------------------------- offline: schemas


def test_event_schema_rejects_bad_payloads():
    good = {"event_type": "ticket.sla_check", "source": "itsm", "data": {"ticket_id": "TK-0139"}}
    TicketEscalationEvent.model_validate(good)
    for bad in (
        {**good, "event_type": "seat.requested"},                         # wrong route
        {**good, "data": {"ticket_id": "TICKET-1"}},                      # bad id
        {**good, "data": {"ticket_id": "TK-0139", "reported_severity": "P0"}},
        {**good, "data": {"ticket_id": "TK-0139", "as_of": "2026-10-28T09:00:00"}},  # naive datetime
        {**good, "unexpected": True},                                     # extra field
    ):
        with pytest.raises(ValidationError):
            TicketEscalationEvent.model_validate(bad)


def test_seat_schema_bounds():
    base = {"event_type": "seat.requested", "source": "crm", "data": {
        "request_id": "SRQ-TEST-0001", "customer_id": "CUST-0040", "contract_id": "CTR-0040",
        "added_seats": 1, "requester_email": "a@b.example", "requested_at": "2026-10-28T10:00:00Z"}}
    SeatRequestEvent.model_validate(base)
    for field, value in (("added_seats", 0), ("added_seats", 10001), ("requester_email", "nope"),
                         ("request_id", "REQ-1"), ("request_type", "MAYBE")):
        with pytest.raises(ValidationError):
            SeatRequestEvent.model_validate({**base, "data": {**base["data"], field: value}})


# --------------------------------------------------------------------------- offline: SLA clocks


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_enterprise_p1_is_24x7_four_hours():
    created = utc("2026-10-26T09:00:00Z")
    s = sla.evaluate_sla("P1", "ENTERPRISE", created, utc("2026-10-26T11:00:00Z"))
    assert s["clock"] == "24x7" and s["response_target_minutes"] == 15
    assert s["resolution_due_at"] == utc("2026-10-26T13:00:00Z")
    assert s["resolution_budget_consumed_pct"] == 50.0 and not s["breached"]


def test_growth_p1_coverage_clock_skips_weekend_across_dst_end():
    # Fri 2026-10-30 22:00 CDT: 2 coverage hours Friday, weekend skipped, DST ends Sun Nov 1,
    # remaining 6 hours Monday from 00:00 CST -> 06:00 CST = 12:00 UTC
    created = utc("2026-10-31T03:00:00Z")
    assert sla.clock_for("P1", "GROWTH") == "coverage"
    assert sla.add_eligible_minutes(created, 8 * 60, "coverage") == utc("2026-11-02T12:00:00Z")


def test_starter_business_clock_skips_thanksgiving():
    # Wed Nov 25 10:00 CST: 7h Wed, Thu Nov 26 holiday, 8h Fri, last 1h Mon Nov 30 -> 10:00 CST
    created = utc("2026-11-25T16:00:00Z")
    assert sla.add_eligible_minutes(created, sla.RESOLUTION_MINUTES["P1"]["STARTER"], "business") == \
        utc("2026-11-30T16:00:00Z")
    assert sla.eligible_minutes(created, utc("2026-11-27T23:00:00Z"), "business") == 15 * 60


def test_severity_can_only_be_raised():
    assert sla.more_severe("P3", "P1") == "P1"
    assert sla.more_severe("P1", "P3") == "P1"
    assert sla.more_severe("P2", None) == "P2"


@pytest.mark.parametrize("severity,pct,level", [
    ("P1", 49.9, "ON_CALL"), ("P1", 50, "VP_ENGINEERING"), ("P1", 75, "EXECUTIVE_CHAIR"),
    ("P2", 49.9, "NONE"), ("P2", 50, "SUPPORT_MANAGER"), ("P2", 75, "VP_CUSTOMER_SUCCESS"),
    ("P3", 99, "NONE"),
])
def test_escalation_thresholds(severity, pct, level):
    assert sla.escalation_level(severity, pct, is_open=True)[0] == level
    assert sla.escalation_level(severity, pct, is_open=False)[0] == "NONE"


# --------------------------------------------------------------------------- offline: dispatcher


def run(coro):
    return asyncio.run(coro)


def dispatcher(handler, mode="live", **kw) -> N8nDispatcher:
    return N8nDispatcher(base_url="http://n8n.test/webhook", secret=SECRET, mode=mode, backoff_s=0,
                         transport=httpx.MockTransport(handler), **kw)


SAMPLE = {"event_id": "11111111-1111-1111-1111-111111111111", "event_type": "ticket.escalation_required",
          "source": "nexusflow-api", "data": {"ticket_id": "TK-0139", "note": "é"}}


def test_dispatch_delivers_signed_payload():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        body = request.content
        verify_signature(SECRET, request.headers[TIMESTAMP_HEADER], request.headers[SIGNATURE_HEADER], body, 300)
        seen["json"] = json.loads(body)
        seen["event_id"] = request.headers["X-NexusFlow-Event-Id"]
        return httpx.Response(202, json={"accepted": True})

    result = run(dispatcher(handler).dispatch(P1_TICKET_ESCALATION, SAMPLE))
    assert result.outcome == "DELIVERED" and result.ok and result.attempts == 1
    assert result.status_code == 202 and result.response_body == {"accepted": True}
    assert seen["url"] == "http://n8n.test/webhook/nexusflow/p1-ticket-escalation"
    assert seen["json"] == SAMPLE and seen["event_id"] == SAMPLE["event_id"]


def test_dispatch_retries_5xx_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": 1})

    result = run(dispatcher(handler).dispatch(SEAT_REQUEST, SAMPLE))
    assert result.outcome == "DELIVERED" and result.attempts == 3


def test_dispatch_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404, text="webhook not registered")

    result = run(dispatcher(handler).dispatch(SEAT_REQUEST, SAMPLE))
    assert result.outcome == "FAILED" and result.attempts == 1 and len(calls) == 1
    assert result.response_body == "webhook not registered"


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_dispatch_offline_auto_queues_and_live_fails(exc):
    def handler(request):
        raise exc

    auto = dispatcher(handler, mode="auto", max_attempts=3)
    result = run(auto.dispatch(DELINQUENT_INVOICE, SAMPLE))
    assert result.outcome == "QUEUED_OFFLINE" and not result.ok and result.attempts == 3
    assert len(auto.outbox) == 1 and auto.outbox[0].json == SAMPLE
    assert type(exc).__name__ in result.error

    live = dispatcher(handler, mode="live", max_attempts=2)
    result = run(live.dispatch(DELINQUENT_INVOICE, SAMPLE))
    assert result.outcome == "FAILED" and result.attempts == 2 and live.outbox == []


def test_dispatch_mock_mode_never_touches_network():
    def handler(request):
        raise AssertionError("network used in mock mode")

    mock = dispatcher(handler, mode="mock")
    result = run(mock.dispatch(P1_TICKET_ESCALATION, SAMPLE))
    assert result.outcome == "MOCKED" and result.ok and result.status_code == 202
    entry = mock.outbox[0]
    verify_signature(SECRET, entry.headers[TIMESTAMP_HEADER], entry.headers[SIGNATURE_HEADER], entry.body, 300)


def test_dispatch_refuses_unsigned():
    with pytest.raises(RuntimeError, match="WEBHOOK_SIGNING_SECRET"):
        run(N8nDispatcher(base_url="http://x", secret="", mode="mock").dispatch(SEAT_REQUEST, SAMPLE))


# --------------------------------------------------------------------------- offline: workflow exports

ALLOWED_NODE_TYPES = {
    "n8n-nodes-base.webhook", "n8n-nodes-base.code", "n8n-nodes-base.if", "n8n-nodes-base.respondToWebhook",
    "n8n-nodes-base.httpRequest", "n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.executeWorkflowTrigger",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_workflow_files_exist():
    assert [p.name for p in WORKFLOW_FILES] == [
        "delinquent_invoice_reconciliation_workflow.json",
        "hitl_approval_notification_workflow.json",
        "p1_ticket_escalation_workflow.json",
        "seat_expansion_trigger_workflow.json",
    ]


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.stem)
def test_workflow_structure_is_valid(path):
    wf = load(path)
    assert {"id", "name", "nodes", "connections", "settings"} <= wf.keys()
    assert wf["settings"]["executionOrder"] == "v1"
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names)), "duplicate node names"
    assert len({n["id"] for n in wf["nodes"]}) == len(names), "duplicate node ids"
    for node in wf["nodes"]:
        assert node["type"] in ALLOWED_NODE_TYPES, node["type"]
        assert isinstance(node["typeVersion"], (int, float)) and len(node["position"]) == 2
    for src, conns in wf["connections"].items():
        assert src in names
        for branch in conns["main"]:
            for c in branch:
                assert c["node"] in names and c["type"] == "main"
    # every node except triggers is reachable from some connection
    targets = {c["node"] for conns in wf["connections"].values() for b in conns["main"] for c in b}
    triggers = {n["name"] for n in wf["nodes"] if n["type"].endswith(("webhook", "scheduleTrigger", "executeWorkflowTrigger"))}
    assert set(names) - triggers <= targets


def test_webhook_paths_match_dispatcher_and_are_verified():
    paths = {}
    for path in WORKFLOW_FILES:
        wf = load(path)
        for node in wf["nodes"]:
            if node["type"] == "n8n-nodes-base.webhook":
                p = node["parameters"]
                assert p["httpMethod"] == "POST" and p["responseMode"] == "responseNode"
                assert p["options"]["rawBody"] is True and node["webhookId"]
                assert p["path"] not in paths
                paths[p["path"]] = path.name
                first = wf["connections"][node["name"]]["main"][0][0]["node"]
                verify = next(n for n in wf["nodes"] if n["name"] == first)
                assert "timingSafeEqual" in verify["parameters"]["jsCode"]
    assert set(paths) == {P1_TICKET_ESCALATION, SEAT_REQUEST, DELINQUENT_INVOICE,
                          APPROVAL_EVENTS, APPROVAL_DECISION_RELAY}


def test_backend_calls_are_signed_and_use_env_base_url():
    for path in WORKFLOW_FILES:
        for node in load(path)["nodes"]:
            if node["type"] == "n8n-nodes-base.httpRequest" and "NEXUSFLOW_API_BASE_URL" in node["parameters"]["url"]:
                headers = {h["name"] for h in node["parameters"]["headerParameters"]["parameters"]}
                assert {"X-NexusFlow-Timestamp", "X-NexusFlow-Signature"} <= headers
                # error bodies are application/problem+json, which n8n's autodetect would leave unparsed
                assert node["parameters"]["options"]["response"]["response"]["responseFormat"] == "json"
                assert node["parameters"]["url"].startswith("={{ $env.NEXUSFLOW_API_BASE_URL }}/api/v1/")


def test_workflows_contain_no_secrets():
    for path in WORKFLOW_FILES:
        text = path.read_text()
        assert SECRET not in text
        if SETTINGS.gemini_api_key.strip():
            assert SETTINGS.gemini_api_key.strip() not in text
        assert "localhost:8010" not in text and "127.0.0.1" not in text


def test_exports_match_generator():
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_workflows", WORKFLOW_DIR / "build_workflows.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for filename, factory in mod.WORKFLOWS.items():
        assert load(WORKFLOW_DIR / filename) == json.loads(json.dumps(factory().export())), \
            f"{filename} is stale: run python n8n_workflows/build_workflows.py"


# --------------------------------------------------------------------------- db: endpoints (mock dispatch)


@pytest.fixture(scope="module")
def client():
    if not _db_available():
        pytest.skip("PostgreSQL not reachable")
    query("DELETE FROM seat_requests WHERE request_id LIKE 'SRQ-TEST-%%'")
    app.state.dispatcher = N8nDispatcher.from_settings(mode="mock")
    with TestClient(app) as c:
        yield c
    del app.state.dispatcher
    app.dependency_overrides.clear()
    query("DELETE FROM seat_requests WHERE request_id LIKE 'SRQ-TEST-%%'")
    if PYTEST_EVENT_IDS:
        query("DELETE FROM automation_events WHERE event_id::text = ANY(%s) "
              "OR payload->'data'->>'correlation_id' = ANY(%s)", (PYTEST_EVENT_IDS, PYTEST_EVENT_IDS))


@pytest.fixture
def outbox(client):
    box = app.state.dispatcher.outbox
    box.clear()
    return box


def customer_on(plan: str, seats: int | None = None, offset: int = 0) -> tuple[str, str, int]:
    rows = query(
        "SELECT c.customer_id, k.contract_id, c.total_seats FROM customers c "
        "JOIN contracts k ON k.customer_id = c.customer_id AND k.status = 'ACTIVE' "
        "WHERE c.status = 'ACTIVE' AND k.plan_tier = %s AND (%s::int IS NULL OR c.total_seats = %s) "
        "ORDER BY c.customer_id", (plan, seats, seats))
    return rows[offset]


def seat_data(cust, ctr, seats, when, rid=None, kind="PURCHASE"):
    return {"request_id": rid or f"SRQ-TEST-{uuid.uuid4().hex[:10]}", "customer_id": cust, "contract_id": ctr,
            "added_seats": seats, "request_type": kind, "requester_email": "it-admin@client.example",
            "requested_at": when}


# ---- tickets


@needs_db
def test_ticket_p1_escalation_is_dispatched_signed(client, outbox):
    ev = event("ticket.sla_check", {"ticket_id": "TK-0139", "reported_severity": "P1",
                                    "as_of": "2026-10-26T11:30:00Z", "summary": "API sync failing"})
    r = signed_post(client, f"{API}/tickets/escalate", ev)
    assert r.status_code == 202, r.text
    body = r.json()
    assert (body["recorded_severity"], body["effective_severity"], body["plan_tier"]) == ("P3", "P1", "ENTERPRISE")
    assert body["sla"]["clock"] == "24x7" and body["sla"]["resolution_budget_consumed_pct"] == 62.5
    assert body["escalation"]["level"] == "VP_ENGINEERING"
    assert {c["employee_id"] for c in body["escalation"]["notify"]} == {"EMP-0001", "EMP-0059"}
    assert body["dispatch"] == "scheduled" and body["assigned_to"]["employee_id"] == "EMP-0069"

    assert len(outbox) == 1
    sent = outbox[0]
    assert sent.url.endswith("/nexusflow/p1-ticket-escalation")
    verify_signature(SECRET, sent.headers[TIMESTAMP_HEADER], sent.headers[SIGNATURE_HEADER], sent.body, 300)
    assert sent.json["event_type"] == "ticket.escalation_required"
    assert sent.json["data"]["correlation_id"] == ev["event_id"]
    assert "TK-0139" in sent.json["data"]["copilot_question"]
    [(status,)] = query("SELECT status FROM automation_events WHERE direction = 'outbound' "
                        "AND payload->'data'->>'correlation_id' = %s", (ev["event_id"],))
    assert status == "MOCKED"


@needs_db
def test_ticket_replay_returns_stored_result_without_redispatch(client, outbox):
    ev = event("ticket.sla_check", {"ticket_id": "TK-0139", "reported_severity": "P1",
                                    "as_of": "2026-10-26T11:30:00Z"})
    first = signed_post(client, f"{API}/tickets/escalate", ev)
    outbox.clear()
    again = signed_post(client, f"{API}/tickets/escalate", ev)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert again.json()["sla"] == first.json()["sla"] and outbox == []


@needs_db
def test_closed_ticket_is_not_dispatched(client, outbox):
    r = signed_post(client, f"{API}/tickets/escalate", event("ticket.updated", {"ticket_id": "TK-0002"}))
    assert r.status_code == 202
    body = r.json()
    assert body["is_open"] is False and body["escalation"]["level"] == "NONE"
    assert body["dispatch"] == "not_required" and outbox == []


@needs_db
def test_p3_dispatched_only_when_copilot_requested(client, outbox):
    [(tid,)] = query("SELECT ticket_id FROM support_tickets WHERE severity = 'P3' AND resolved_at IS NULL "
                     "ORDER BY ticket_id LIMIT 1")
    r = signed_post(client, f"{API}/tickets/escalate", event("ticket.updated", {"ticket_id": tid}))
    assert r.json()["dispatch"] == "not_required"
    r = signed_post(client, f"{API}/tickets/escalate",
                    event("ticket.updated", {"ticket_id": tid, "request_copilot": True}))
    assert r.json()["dispatch"] == "scheduled" and len(outbox) == 1


@needs_db
def test_unknown_ticket_is_rejected_and_logged(client):
    ev = event("ticket.created", {"ticket_id": "TK-9999"})
    r = signed_post(client, f"{API}/tickets/escalate", ev)
    assert r.status_code == 404 and r.json()["code"] == "TICKET_NOT_FOUND"
    [(status,)] = query("SELECT status FROM automation_events WHERE event_id = %s", (ev["event_id"],))
    assert status == "REJECTED"
    replay = signed_post(client, f"{API}/tickets/escalate", ev)
    assert replay.status_code == 409 and replay.json()["code"] == "DUPLICATE_REJECTED_EVENT"
    assert replay.json()["original"]["code"] == "TICKET_NOT_FOUND"


@needs_db
@pytest.mark.parametrize("path", ["/tickets/escalate", "/seats/request", "/invoices/delinquent", "/n8n/callback"])
def test_every_route_requires_valid_signature(client, path):
    payload = event("ticket.created", {"ticket_id": "TK-0139"})
    assert signed_post(client, API + path, payload, secret="wrong-secret").status_code == 401
    assert signed_post(client, API + path, payload, ts=int(time.time()) - 3600).status_code == 401
    r = client.post(API + path, content=canonical_json(payload), headers={"content-type": "application/json"})
    assert r.status_code == 401 and r.json()["code"] == "INVALID_SIGNATURE"


@needs_db
def test_schema_errors_return_422(client):
    bad_type = event("seat.requested", {"ticket_id": "TK-0139"})
    assert signed_post(client, f"{API}/tickets/escalate", bad_type).status_code == 422
    assert signed_post(client, f"{API}/tickets/escalate", None, raw=b"{not json").status_code == 422


# ---- seats


@needs_db
@pytest.mark.parametrize("plan,seats,approval,monthly", [
    ("ENTERPRISE", 42, False, "490.00"), ("ENTERPRISE", 43, True, "501.67"),
    ("STARTER", 10, False, "500.00"), ("STARTER", 11, True, "550.00"),
    ("GROWTH", 20, False, "500.00"), ("GROWTH", 21, True, "525.00"),
])
def test_seat_threshold_boundaries(client, plan, seats, approval, monthly):
    # one customer per case, distinct month, so the 30-day windows never overlap
    offset = {42: 0, 43: 1, 10: 0, 11: 1, 20: 0, 21: 1}[seats]
    cust, ctr, _ = customer_on(plan, offset=offset)
    when = f"2026-0{1 + offset * 3}-15T12:00:00Z"
    r = signed_post(client, f"{API}/seats/request", event("seat.requested", seat_data(cust, ctr, seats, when)))
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["requires_finance_approval"] is approval
    assert d["calculation"]["aggregate_monthly_30d"] == monthly
    assert d["status"] == ("PENDING_FINANCE_APPROVAL" if approval else "PENDING_CUSTOMER_AUTHORIZATION")
    assert (d["approver"] or {}).get("employee_id") == ("EMP-0082" if approval else None)
    if monthly == "500.00":
        assert any("Exactly $500.00" in n for n in d["notes"])


@needs_db
def test_split_request_is_aggregated_over_30_days(client):
    cust, ctr, _ = customer_on("ENTERPRISE", offset=2)
    first = signed_post(client, f"{API}/seats/request",
                        event("seat.requested", seat_data(cust, ctr, 30, "2026-06-01T10:00:00Z")))
    assert first.json()["requires_finance_approval"] is False
    second = signed_post(client, f"{API}/seats/request",
                         event("seat.requested", seat_data(cust, ctr, 20, "2026-06-08T10:00:00Z")))
    d = second.json()
    assert d["requires_finance_approval"] is True and d["calculation"]["aggregate_seats_30d"] == 50
    assert d["calculation"]["aggregate_monthly_30d"] == "583.33"
    assert d["calculation"]["prior_requests_in_window"] == [first.json()["request_id"]]
    # Jul 2: Jun 1 has left the (Jun 2, Jul 2] window, Jun 8 has not
    third = signed_post(client, f"{API}/seats/request",
                        event("seat.requested", seat_data(cust, ctr, 5, "2026-07-02T10:00:00Z")))
    assert third.json()["calculation"]["aggregate_seats_30d"] == 25  # 20 (Jun 8) + 5


@needs_db
def test_tier_crossing_and_vague_requests_are_held(client):
    cust, ctr, seats = customer_on("STARTER", seats=100, offset=3)
    r = signed_post(client, f"{API}/seats/request",
                    event("seat.requested", seat_data(cust, ctr, 150, "2026-08-01T10:00:00Z")))
    assert r.json()["status"] == "PENDING_PLAN_AMENDMENT" and r.json()["requires_plan_amendment"] is True
    cust, ctr, _ = customer_on("ENTERPRISE", offset=3)
    r = signed_post(client, f"{API}/seats/request",
                    event("seat.requested", seat_data(cust, ctr, 50, "2026-08-01T10:00:00Z", kind="RESERVATION")))
    d = r.json()
    assert d["status"] == "PENDING_CLARIFICATION" and d["requires_finance_approval"] is True


@needs_db
def test_seat_request_validation_errors(client):
    cust, ctr, _ = customer_on("ENTERPRISE", offset=4)
    other_cust, _, _ = customer_on("ENTERPRISE", offset=5)
    r = signed_post(client, f"{API}/seats/request",
                    event("seat.requested", seat_data(other_cust, ctr, 5, "2026-09-01T10:00:00Z")))
    assert r.status_code == 422 and r.json()["code"] == "CONTRACT_CUSTOMER_MISMATCH"
    r = signed_post(client, f"{API}/seats/request",
                    event("seat.requested", seat_data("CUST-0011", "CTR-0011", 5, "2026-09-01T10:00:00Z")))
    assert r.status_code == 409 and r.json()["code"] == "CUSTOMER_NOT_ACTIVE"


@needs_db
def test_seat_request_id_idempotency(client):
    cust, ctr, _ = customer_on("ENTERPRISE", offset=6)
    data = seat_data(cust, ctr, 5, "2026-09-15T10:00:00Z")
    assert signed_post(client, f"{API}/seats/request", event("seat.requested", data)).status_code == 201
    same = signed_post(client, f"{API}/seats/request", event("seat.requested", data))
    assert same.status_code == 200 and same.json()["duplicate"] is True
    changed = signed_post(client, f"{API}/seats/request", event("seat.requested", {**data, "added_seats": 6}))
    assert changed.status_code == 409 and changed.json()["code"] == "REQUEST_ID_REUSED"
    [(n,)] = query("SELECT count(*) FROM seat_requests WHERE request_id = %s", (data["request_id"],))
    assert n == 1


# ---- invoices


@needs_db
def test_delinquent_invoice_summary_and_draft(client):
    r = signed_post(client, f"{API}/invoices/delinquent",
                    event("invoice.delinquent", {"invoice_id": "INV-2026-0011", "as_of": "2026-10-28"}))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["invoice"]["outstanding"] == "150000.00" and d["invoice"]["days_overdue"] == 42
    assert d["account"]["customer_status"] == "DELINQUENT" and d["account"]["contract_status"] == "ACTIVE"
    assert d["account"]["csm"]["employee_id"] == "EMP-0062"
    n = d["notice"]
    assert n["status"] == "DRAFT_FOR_REVIEW" and n["to"] == "accounts-payable@client0011.example"
    assert n["from"] == "rowan.patel.0084@nexusflow.example" and n["illustrative_cure_deadline"] == "2026-11-12"
    assert "$150,000.00" in n["body"] and "INV-2026-0011" in n["subject"]
    assert n["requires_review_by"]["employee_id"] == "EMP-0082"
    assert any("CHURNED" in g for g in d["guardrails"])


@needs_db
def test_delinquent_invoice_errors(client):
    r = signed_post(client, f"{API}/invoices/delinquent",
                    event("invoice.delinquent", {"invoice_id": "INV-2026-0001", "as_of": "2026-10-28"}))
    assert r.status_code == 409 and r.json()["code"] == "INVOICE_NOT_DELINQUENT"
    r = signed_post(client, f"{API}/invoices/delinquent",
                    event("invoice.delinquent", {"invoice_id": "INV-2026-0011", "customer_id": "CUST-0040"}))
    assert r.status_code == 422 and r.json()["code"] == "INVOICE_CUSTOMER_MISMATCH"
    r = signed_post(client, f"{API}/invoices/delinquent", event("invoice.delinquent", {"invoice_id": "INV-1999-0001"}))
    assert r.status_code == 422  # pattern requires INV-20YY-NNNN


@needs_db
def test_overdue_poll_is_signed_get(client):
    headers = sign_headers(SECRET, b"")
    r = client.get(f"{API}/invoices/overdue", params={"as_of": "2026-10-28"}, headers=headers)
    assert r.status_code == 200
    d = r.json()
    assert d["count"] == 6 and "INV-2026-0011" in {i["invoice_id"] for i in d["invoices"]}
    assert client.get(f"{API}/invoices/overdue").status_code == 401


# ---- callbacks + copilot


@needs_db
def test_workflow_callback_is_recorded_once(client):
    ev = event("workflow.completed", {"workflow": "seat_expansion_trigger", "action": "finance_approval_requested",
                                      "subject_id": "SRQ-TEST-cb", "outcome": {"status": "PENDING_FINANCE_APPROVAL"}})
    r = signed_post(client, f"{API}/n8n/callback", ev)
    assert r.status_code == 200 and r.json() == {"event_id": ev["event_id"], "recorded": True, "duplicate": False}
    again = signed_post(client, f"{API}/n8n/callback", ev)
    assert again.json()["duplicate"] is True
    bad = event("workflow.completed", {"workflow": "unknown_flow", "action": "x"})
    assert signed_post(client, f"{API}/n8n/callback", bad).status_code == 422


@needs_db
def test_copilot_endpoint_signed_and_maps_llm_outage_to_503(client):
    from app.services.rag.copilot import CopilotResponse

    class FakeCopilot:
        def __init__(self, fail=False):
            self.fail = fail

        def ask(self, question, top_k=6):
            if self.fail:
                raise RuntimeError("All Gemini models failed: m: 503 UNAVAILABLE")
            return CopilotResponse(question=question, answer="Follow SOP-01 [S1].", citations=[],
                                   insufficient_context=False, confidence="low", grounded=False, model="fake")

    app.dependency_overrides[get_copilot] = lambda: FakeCopilot()
    r = signed_post(client, "/api/v1/copilot/ask", {"question": "P1 SLA?", "top_k": 3})
    assert r.status_code == 200 and r.json()["model"] == "fake"
    assert signed_post(client, "/api/v1/copilot/ask", {"question": "P1 SLA?"}, secret="x").status_code == 401
    app.dependency_overrides[get_copilot] = lambda: FakeCopilot(fail=True)
    r = signed_post(client, "/api/v1/copilot/ask", {"question": "P1 SLA?"})
    assert r.status_code == 503 and r.json()["code"] == "LLM_UNAVAILABLE"
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- e2e: FastAPI <-> n8n

BACKEND = "http://127.0.0.1:8010"


def _e2e_ready() -> bool:
    try:
        ok = httpx.get(f"{BACKEND}/health", timeout=2).json()
        return all(ok["components"][c]["status"] == "ok" for c in ("database", "n8n"))
    except (httpx.HTTPError, ValueError, KeyError):
        return False


needs_stack = pytest.mark.skipif(not _e2e_ready(), reason="uvicorn on :8010 + n8n not both up")


def n8n_post(path: str, payload: dict, **kw) -> httpx.Response:
    body = canonical_json(payload)
    return httpx.post(f"{SETTINGS.n8n_webhook_url}/{path}", content=body, timeout=kw.get("timeout", 60),
                      headers={"content-type": "application/json", **sign_headers(SECRET, body)})


def wait_for_callback(correlation_id: str, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rows = query("SELECT payload FROM automation_events WHERE direction = 'callback' "
                     "AND payload->'data'->>'correlation_id' = %s", (correlation_id,))
        if rows:
            return rows[0][0]
        time.sleep(1)
    raise AssertionError(f"no n8n callback for {correlation_id} within {timeout_s}s")


@pytest.mark.e2e
@needs_db
@needs_stack
def test_e2e_seat_workflow_round_trip():
    query("DELETE FROM seat_requests WHERE request_id LIKE 'SRQ-E2E-%%'")
    cust, ctr, _ = customer_on("ENTERPRISE", offset=7)
    ev = {"event_id": str(uuid.uuid4()), "event_type": "seat.requested", "source": "crm",
          "data": seat_data(cust, ctr, 50, datetime.now(timezone.utc).isoformat(), rid=f"SRQ-E2E-{uuid.uuid4().hex[:8]}")}
    r = n8n_post(SEAT_REQUEST, ev)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"]["requires_finance_approval"] is True and body["callback_status"] == 200
    assert body["approval_request"]["type"] == "FINANCE_SEAT_APPROVAL"
    assert "$583.33/month" in body["approval_request"]["calculation"]
    cb = wait_for_callback(ev["event_id"], 10)
    assert cb["data"]["action"] == "finance_approval_requested"
    # n8n rejects a forged request before touching the backend
    forged = httpx.post(f"{SETTINGS.n8n_webhook_url}/{SEAT_REQUEST}", content=canonical_json(ev), timeout=30,
                        headers={"content-type": "application/json", TIMESTAMP_HEADER: str(int(time.time())),
                                 SIGNATURE_HEADER: "sha256=" + "0" * 64})
    assert forged.status_code == 401 and forged.json()["reason"] == "signature mismatch"
    query("DELETE FROM seat_requests WHERE request_id LIKE 'SRQ-E2E-%%'")


@pytest.mark.e2e
@needs_db
@needs_stack
def test_e2e_delinquent_workflow_drafts_and_logs():
    ev = {"event_id": str(uuid.uuid4()), "event_type": "invoice.delinquent", "source": "erp",
          "data": {"invoice_id": "INV-2026-0011", "as_of": "2026-10-28"}}
    r = n8n_post(DELINQUENT_INVOICE, ev)
    assert r.status_code == 202 and r.json()["accepted"] is True
    cb = wait_for_callback(ev["event_id"], 20)
    assert cb["event_type"] == "workflow.completed" and cb["data"]["action"] == "collection_notice_drafted"
    assert cb["data"]["outcome"]["notice"]["status"] == "DRAFT_FOR_REVIEW"
    assert cb["data"]["outcome"]["notice"]["illustrative_cure_deadline"] == "2026-11-12"


@pytest.mark.e2e
@pytest.mark.live
@needs_db
@needs_stack
def test_e2e_p1_escalation_backend_to_n8n_to_copilot_and_back():
    ev = {"event_id": str(uuid.uuid4()), "event_type": "ticket.sla_check", "source": "itsm",
          "data": {"ticket_id": "TK-0139", "reported_severity": "P1", "summary": "API inventory sync failing."}}
    body = canonical_json(ev)
    r = httpx.post(f"{BACKEND}{API}/tickets/escalate", content=body, timeout=30,
                   headers={"content-type": "application/json", **sign_headers(SECRET, body)})
    assert r.status_code == 202 and r.json()["dispatch"] == "scheduled"
    deadline = time.time() + 30
    rows = []
    while time.time() < deadline and not rows:
        rows = query("SELECT status, response FROM automation_events WHERE direction = 'outbound' "
                     "AND payload->'data'->>'correlation_id' = %s", (ev["event_id"],))
        time.sleep(0.5)
    assert rows and rows[0][0] == "DELIVERED" and rows[0][1]["status_code"] == 202
    cb = wait_for_callback(ev["event_id"], 240)  # Gemini + model fallback can be slow
    alert = cb["data"]["outcome"]["alert"]
    assert alert["generic"]["ticket_id"] == "TK-0139" and alert["generic"]["severity"] == "P1"
    assert alert["slack"]["blocks"][0]["type"] == "header" and alert["discord"]["embeds"]
    assert cb["data"]["outcome"]["copilot_ok"] is True
    assert any(s.startswith("SOP-01") for s in alert["generic"]["sources"])
