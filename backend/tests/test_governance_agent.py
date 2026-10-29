"""Stage 4: Commercial Governance agent, HITL approval queue and approval tokens.

Runs the agent through the real API (TestClient) against live Postgres with:
  * the n8n dispatcher in mock mode (notifications are captured, never sent),
  * a fake clause reviewer standing in for the Stage 2 copilot (no Gemini calls),
  * a controllable clock, to expire approvals without waiting.
Every seat count / ARR the tests change is snapshotted and restored afterwards.
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.api.v1.approvals import get_governance_agent
from app.core.config import get_settings
from app.core.security import issue_jwt, sign_headers
from app.main import app
from app.services.agents import approval_tokens
from app.services.agents.governance_agent import (
    TRANSITIONS, GovernanceAgent, InvalidTransition, RunView, State,
)
from app.services.n8n.dispatcher import N8nDispatcher, canonical_json

SETTINGS = get_settings()
DSN = SETTINGS.psycopg2_dsn
RUNS = "/api/v1/agents/governance/runs"
PREFIX = "SRQ-GOV-"


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
        return cur.fetchall() if cur.description else None


# --------------------------------------------------------------------------- offline: state machine + tokens


def test_transition_table_matches_spec():
    assert set(TRANSITIONS) == set(State)
    assert {s for s, nxt in TRANSITIONS.items() if not nxt} == {State.REJECTED, State.EXECUTED, State.FAILED}
    # money only moves after approval
    assert [s for s, nxt in TRANSITIONS.items() if State.EXECUTED in nxt] == [State.APPROVED]
    assert State.APPROVED in TRANSITIONS[State.WAITING_FOR_APPROVAL]
    assert State.EXECUTED not in TRANSITIONS[State.WAITING_FOR_APPROVAL]


def test_invalid_transition_is_refused():
    agent = GovernanceAgent()
    run = RunView(run_id="x", request_id="SRQ-x", customer_id="c", contract_id="k", state=State.WAITING_FOR_APPROVAL,
                  context={}, history=[])
    with pytest.raises(InvalidTransition):
        agent._transition(None, run, State.EXECUTED, "skip the human")


KEY = b"k" * 32
FUTURE = datetime.now(timezone.utc) + timedelta(hours=1)


def test_token_roundtrip_binds_claims():
    token = approval_tokens.issue("a-1", "SRQ-1", "EMP-0082", FUTURE, key=KEY)
    claims = approval_tokens.verify(token, key=KEY)
    assert (claims.approval_id, claims.request_id, claims.approver_id) == ("a-1", "SRQ-1", "EMP-0082")
    assert approval_tokens.issue("a-1", "SRQ-1", "EMP-0082", FUTURE, key=KEY) != token  # random nonce


@pytest.mark.parametrize("mutate,code", [
    (lambda t: t[:-2] + ("AA" if t[-2:] != "AA" else "BB"), "TOKEN_INVALID_SIGNATURE"),
    (lambda t: t.split(".")[0] + "." + approval_tokens._b64(b'{"aid":"a-2","rid":"SRQ-1","apr":"EMP-0082",'
                                                              b'"exp":9999999999,"n":"x"}') + "." + t.split(".")[2],
     "TOKEN_INVALID_SIGNATURE"),
    (lambda t: "v2" + t[2:], "TOKEN_MALFORMED"),
    (lambda t: "garbage", "TOKEN_MALFORMED"),
])
def test_token_tampering_is_detected(mutate, code):
    token = approval_tokens.issue("a-1", "SRQ-1", "EMP-0082", FUTURE, key=KEY)
    with pytest.raises(approval_tokens.TokenError) as err:
        approval_tokens.verify(mutate(token), key=KEY)
    assert err.value.code == code and err.value.claims is None


def test_token_wrong_key_and_expiry():
    token = approval_tokens.issue("a-1", "SRQ-1", "EMP-0082", FUTURE, key=KEY)
    with pytest.raises(approval_tokens.TokenError, match="signature"):
        approval_tokens.verify(token, key=b"other" * 8)
    with pytest.raises(approval_tokens.TokenError) as err:
        approval_tokens.verify(token, key=KEY, now=FUTURE + timedelta(seconds=1))
    assert err.value.code == "TOKEN_EXPIRED" and err.value.claims.approval_id == "a-1"


def test_derived_token_key_is_domain_separated():
    assert approval_tokens.signing_key() != SETTINGS.webhook_signing_secret.encode()


# --------------------------------------------------------------------------- db fixtures


class Clock:
    def __init__(self):
        self.t = datetime.now(timezone.utc)

    def __call__(self):
        return self.t


class FakeReviewer:
    def __init__(self, fail=False):
        self.questions, self.fail = [], fail

    def __call__(self, question):
        self.questions.append(question)
        if self.fail:
            raise RuntimeError("All Gemini models failed")
        cite = SimpleNamespace(doc_id="SOW_Enterprise_Client_Sample", section="4. Change control",
                               quote="no seats are provisioned", quote_verified=True)
        return SimpleNamespace(answer="The SOW requires human Finance approval above $500 [S1].", grounded=True,
                               insufficient_context=False, confidence="high", model="fake", citations=[cite])


def customers(plan: str, exclude_docs=True, seats=None) -> list[tuple[str, str]]:
    rows = query(
        """SELECT c.customer_id, k.contract_id FROM customers c
           JOIN contracts k ON k.customer_id = c.customer_id AND k.status = 'ACTIVE'
           WHERE c.status = 'ACTIVE' AND k.plan_tier = %s AND (%s::int IS NULL OR c.total_seats = %s)
           ORDER BY c.customer_id""", (plan, seats, seats))
    with_docs = {r[0] for r in query(
        "SELECT DISTINCT jsonb_array_elements_text(metadata->'doc_entities') FROM document_chunks WHERE doc_type = 'legal'")}
    return [r for r in rows if not (exclude_docs and (r[0] in with_docs or r[1] in with_docs))]


TEST_STARTED = datetime.now(timezone.utc)


def cleanup():
    # audit rows the tests produce: notifications for test requests and approver decision attempts
    query(f"DELETE FROM automation_events WHERE subject_id LIKE '{PREFIX}%%' "
          "OR subject_id IN ('approval-action', 'approval-queue') "
          "OR (event_type = 'approval.decision' AND source = 'approver' AND created_at >= %s)", (TEST_STARTED,))
    query(f"DELETE FROM approval_requests WHERE request_id LIKE '{PREFIX}%%'")
    query(f"DELETE FROM governance_runs WHERE request_id LIKE '{PREFIX}%%'")
    query(f"DELETE FROM seat_requests WHERE request_id LIKE '{PREFIX}%%'")


@pytest.fixture(scope="module")
def snapshot():
    cleanup()
    seats = dict(query("SELECT customer_id, total_seats FROM customers"))
    arr = dict(query("SELECT contract_id, annual_recurring_revenue FROM contracts"))
    status = dict(query("SELECT contract_id, status FROM contracts"))
    yield
    cleanup()
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        for cid, n in seats.items():
            cur.execute("UPDATE customers SET total_seats = %s WHERE customer_id = %s AND total_seats <> %s", (n, cid, n))
        for kid, v in arr.items():
            cur.execute("UPDATE contracts SET annual_recurring_revenue = %s, status = %s WHERE contract_id = %s "
                        "AND (annual_recurring_revenue <> %s OR status <> %s)", (v, status[kid], kid, v, status[kid]))
    [(total,)] = query("SELECT sum(annual_recurring_revenue) FROM contracts WHERE status = 'ACTIVE'")
    assert total == Decimal("12400000.00"), "ARR baseline was not restored"


@pytest.fixture
def env(snapshot):
    clock, reviewer = Clock(), FakeReviewer()
    agent = GovernanceAgent(clause_reviewer=reviewer, now=clock)
    app.state.dispatcher = N8nDispatcher.from_settings(mode="mock")
    app.dependency_overrides[get_governance_agent] = lambda: agent
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, agent=agent, clock=clock, reviewer=reviewer,
                              outbox=app.state.dispatcher.outbox)
    app.dependency_overrides.clear()
    del app.state.dispatcher


def seat_event(cust, ctr, seats, when=None, kind="PURCHASE", rid=None):
    return {"event_id": str(uuid.uuid4()), "event_type": "seat.requested", "source": "pytest", "data": {
        "request_id": rid or f"{PREFIX}{uuid.uuid4().hex[:10]}", "customer_id": cust, "contract_id": ctr,
        "added_seats": seats, "request_type": kind, "requester_email": "it-admin@client.example",
        "requested_at": (when or datetime.now(timezone.utc)).isoformat()}}


def post_signed(client, path, payload):
    body = canonical_json(payload)
    return client.post(path, content=body,
                       headers={"content-type": "application/json", **sign_headers(SETTINGS.webhook_signing_secret, body)})


def get_signed(client, path):
    return client.get(path, headers=sign_headers(SETTINGS.webhook_signing_secret, b""))


def act(client, token, action="APPROVE", approver="EMP-0082", comment=None, **extra):
    """Decide as `approver`, authenticated by a MANAGER JWT for that same employee (Stage 5 RBAC)."""
    body = {"action": action, "approver_id": approver, **({"comment": comment} if comment else {}), **extra}
    jwt = issue_jwt(approver, ["MANAGER"])
    return client.post(f"/api/v1/approvals/{token}/action", json=body, headers={"Authorization": f"Bearer {jwt}"})


def account(cust, ctr):
    [(seats,)] = query("SELECT total_seats FROM customers WHERE customer_id = %s", (cust,))
    [(arr,)] = query("SELECT annual_recurring_revenue FROM contracts WHERE contract_id = %s", (ctr,))
    return seats, arr


def requested(outbox) -> list[dict]:
    return [e.json for e in outbox if e.json["event_type"] == "approval.requested"]


def hitl_run(env, cust, ctr, seats=43):
    r = post_signed(env.client, RUNS, seat_event(cust, ctr, seats))
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "WAITING_FOR_APPROVAL"
    return r.json(), requested(env.outbox)[-1]["data"]["approval_token"]


# --------------------------------------------------------------------------- sub-$500 auto approval


def test_sub_threshold_request_is_auto_approved_and_executed(env):
    cust, ctr = customers("STARTER")[0]
    seats_before, arr_before = account(cust, ctr)
    r = post_signed(env.client, RUNS, seat_event(cust, ctr, 5))
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["state"] == "EXECUTED" and run["approval"] is None
    assert [h["to"] for h in run["history"]] == [
        "INITIATED", "ANALYZING_CONTRACT", "CALCULATING_IMPACT", "APPROVED", "EXECUTED"]
    assert run["decision"]["outcome"] == "AUTO_APPROVED"
    assert run["impact"]["monthly_cost_impact_display"] == "250.00" and run["impact"]["annual_arr_impact"] == "3000.00"
    assert account(cust, ctr) == (seats_before + 5, arr_before + Decimal("3000.00"))
    assert run["execution"]["customer_seats"] == [seats_before, seats_before + 5]
    [(status,)] = query("SELECT status FROM seat_requests WHERE request_id = %s", (run["request_id"],))
    assert status == "ACTIVATED" and env.outbox == [] and env.reviewer.questions == []


def test_exactly_500_is_not_escalated(env):
    cust, ctr = customers("STARTER")[1]
    run = post_signed(env.client, RUNS, seat_event(cust, ctr, 10)).json()
    assert run["state"] == "EXECUTED" and run["impact"]["aggregate_monthly_30d_display"] == "500.00"


# --------------------------------------------------------------------------- over-$500 HITL pause


def test_over_threshold_pauses_without_mutation(env):
    cust, ctr = customers("ENTERPRISE")[0]
    before = account(cust, ctr)
    run, token = hitl_run(env, cust, ctr, 43)
    assert account(cust, ctr) == before                     # nothing provisioned
    ap = run["approval"]
    assert ap["status"] == "PENDING" and ap["assigned_approver_id"] == "EMP-0082"
    assert ap["reasons"] == ["MONTHLY_IMPACT_OVER_THRESHOLD"]
    assert (ap["monthly_cost_impact"], ap["annual_arr_impact"]) == ("501.67", "6020.00")
    [(sr_status,)] = query("SELECT status FROM seat_requests WHERE request_id = %s", (run["request_id"],))
    assert sr_status == "PENDING_FINANCE_APPROVAL"

    sent = requested(env.outbox)[-1]["data"]
    assert sent["callback"]["url"].endswith(f"/api/v1/approvals/{token}/action")
    assert sent["card"]["approver"]["employee_id"] == "EMP-0082"
    assert any(f["label"] == "ARR impact" and f["value"] == "+$6020.00" for f in sent["card"]["facts"])
    # the token exists only in the notification: DB keeps its hash, the audit log a redacted copy
    [(stored_hash,)] = query("SELECT token_hash FROM approval_requests WHERE approval_id = %s", (ap["approval_id"],))
    assert stored_hash == hashlib.sha256(token.encode()).hexdigest()
    leaks = query("SELECT count(*) FROM automation_events WHERE payload::text LIKE %s", (f"%{token[:40]}%",))
    leaks += query("SELECT count(*) FROM approval_requests WHERE card::text LIKE %s", (f"%{token[:40]}%",))
    leaks += query("SELECT count(*) FROM governance_runs WHERE context::text LIKE %s", (f"%{token[:40]}%",))
    assert all(n == 0 for (n,) in leaks)
    [(logged,)] = query("SELECT payload->'data'->>'approval_token' FROM automation_events "
                        "WHERE direction = 'outbound' AND payload->'data'->>'approval_id' = %s", (ap["approval_id"],))
    assert logged == "[redacted]"


def test_pending_queue_lists_item_without_secrets(env):
    cust, ctr = customers("ENTERPRISE")[1]
    run, token = hitl_run(env, cust, ctr)
    r = get_signed(env.client, "/api/v1/approvals/pending")
    assert r.status_code == 200
    item = next(a for a in r.json()["approvals"] if a["approval_id"] == run["approval"]["approval_id"])
    assert item["card"]["title"].startswith("Seat expansion approval")
    assert token not in r.text and "token_hash" not in r.text
    assert env.client.get("/api/v1/approvals/pending").status_code == 401


# --------------------------------------------------------------------------- approval + resumption


def test_approval_resumes_from_db_and_executes_once(env):
    cust, ctr = customers("ENTERPRISE")[2]
    seats_before, arr_before = account(cust, ctr)
    run, token = hitl_run(env, cust, ctr, 43)

    # a brand-new agent instance (e.g. after an API restart) resumes purely from persisted state
    restarted = GovernanceAgent(clause_reviewer=None, now=env.clock)
    app.dependency_overrides[get_governance_agent] = lambda: restarted
    r = act(env.client, token, comment="Budget confirmed with CSM", approver_signature="Parker Kim")
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["state"] == "EXECUTED" and done["run_id"] == run["run_id"]
    assert [h["to"] for h in done["history"]][-3:] == ["WAITING_FOR_APPROVAL", "APPROVED", "EXECUTED"]
    assert done["approval"]["status"] == "APPROVED" and done["approval"]["decided_by"] == "EMP-0082"
    assert account(cust, ctr) == (seats_before + 43, arr_before + Decimal("6020.00"))
    [(sr,)] = query("SELECT status FROM seat_requests WHERE request_id = %s", (run["request_id"],))
    assert sr == "ACTIVATED"
    [(sig,)] = query("SELECT approver_signature FROM approval_requests WHERE approval_id = %s",
                     (run["approval"]["approval_id"],))
    assert sig == "Parker Kim"

    final = [e.json for e in env.outbox if e.json["event_type"] == "approval.finalized"][-1]["data"]
    assert final["decision"] == "APPROVED" and final["final_state"] == "EXECUTED"
    assert final["execution"]["arr_delta"] == "6020.00"

    # double approval: refused, nothing applied twice
    again = act(env.client, token)
    assert again.status_code == 409 and again.json()["code"] == "ALREADY_DECIDED"
    assert act(env.client, token, "REJECT", comment="changed my mind").status_code == 409
    assert account(cust, ctr) == (seats_before + 43, arr_before + Decimal("6020.00"))
    audit = query("SELECT status, payload->>'error' FROM automation_events WHERE event_type = 'approval.decision' "
                  "AND created_at > now() - interval '1 minute' ORDER BY id DESC LIMIT 3")
    assert ("REJECTED", "ALREADY_DECIDED") in audit and ("PROCESSED", None) in audit


def test_rejection_requires_comment_and_changes_nothing(env):
    cust, ctr = customers("ENTERPRISE")[3]
    before = account(cust, ctr)
    run, token = hitl_run(env, cust, ctr)
    r = act(env.client, token, "REJECT")
    assert r.status_code == 422 and r.json()["code"] == "COMMENT_REQUIRED"
    r = act(env.client, token, "REJECT", comment="No signed amendment yet")
    assert r.status_code == 200 and r.json()["state"] == "REJECTED"
    assert r.json()["human_decision"]["comment"] == "No signed amendment yet"
    assert account(cust, ctr) == before
    [(sr,)] = query("SELECT status FROM seat_requests WHERE request_id = %s", (run["request_id"],))
    assert sr == "REJECTED"


def test_token_and_approver_checks(env):
    cust, ctr = customers("ENTERPRISE")[4]
    run, token = hitl_run(env, cust, ctr)
    r = act(env.client, token, approver="EMP-0084")
    assert r.status_code == 403 and r.json()["code"] == "APPROVER_MISMATCH"
    r = act(env.client, token[:-3] + "AAA")
    assert r.status_code == 401 and r.json()["code"] == "TOKEN_INVALID_SIGNATURE"
    forged = approval_tokens.issue(str(uuid.uuid4()), run["request_id"], "EMP-0082",
                                   env.clock() + timedelta(hours=1))  # validly signed, never issued for a row
    r = act(env.client, forged)
    assert r.status_code == 404 and r.json()["code"] == "TOKEN_NOT_RECOGNIZED"
    body = canonical_json({"action": "APPROVE", "approver_id": "EMP-0082"})
    r = env.client.post(f"/api/v1/approvals/{token}/action", content=body,
                        headers={"content-type": "application/json", **sign_headers("wrong", body)})
    assert r.status_code == 401 and r.json()["code"] == "INVALID_SIGNATURE"
    assert act(env.client, token, action="MAYBE").status_code == 422
    # still pending after all of that
    [(status,)] = query("SELECT status FROM approval_requests WHERE approval_id = %s", (run["approval"]["approval_id"],))
    assert status == "PENDING"


def test_expired_token_is_rejected_and_run_closed(env):
    cust, ctr = customers("ENTERPRISE")[5]
    before = account(cust, ctr)
    run, token = hitl_run(env, cust, ctr)
    env.clock.t += timedelta(hours=SETTINGS.approval_token_ttl_hours, seconds=1)
    r = act(env.client, token)
    assert r.status_code == 410 and r.json()["code"] == "TOKEN_EXPIRED"
    assert account(cust, ctr) == before
    state = env.client.get(f"{RUNS}/{run['run_id']}",
                           headers=sign_headers(SETTINGS.webhook_signing_secret, b"")).json()
    assert state["state"] == "REJECTED" and state["approval"]["status"] == "EXPIRED"
    [(sr,)] = query("SELECT status FROM seat_requests WHERE request_id = %s", (run["request_id"],))
    assert sr == "EXPIRED"
    final = [e.json for e in env.outbox if e.json["event_type"] == "approval.finalized"][-1]["data"]
    assert final["decision"] == "EXPIRED" and final["final_state"] == "REJECTED"
    assert act(env.client, token).status_code == 410  # stays dead


def test_pending_queue_sweeps_expired_items(env):
    cust, ctr = customers("ENTERPRISE")[6]
    run, _ = hitl_run(env, cust, ctr)
    env.clock.t += timedelta(hours=SETTINGS.approval_token_ttl_hours + 1)
    r = get_signed(env.client, "/api/v1/approvals/pending").json()
    assert r["expired_now"] >= 1
    assert run["approval"]["approval_id"] not in {a["approval_id"] for a in r["approvals"]}
    [(status,)] = query("SELECT status FROM approval_requests WHERE approval_id = %s", (run["approval"]["approval_id"],))
    assert status == "EXPIRED"


# --------------------------------------------------------------------------- contract analysis + RAG


def test_negotiated_sow_forces_hitl_even_below_threshold(env):
    # CUST-0040 has a customer-specific SOW in the corpus
    r = post_signed(env.client, RUNS, seat_event("CUST-0040", "CTR-0040", 10))
    run = r.json()
    assert run["state"] == "WAITING_FOR_APPROVAL"
    assert run["impact"]["monthly_cost_impact_display"] == "116.67"
    assert run["approval"]["reasons"] == ["NEGOTIATED_CONTRACT_TERMS"]
    assert run["clause_check"]["documents"][0]["source_file"] == "legal/SOW_Enterprise_Client_Sample.md"
    assert "CUST-0040" in env.reviewer.questions[0]
    assert run["approval"]["card"]["contract_review"]["citations"][0]["doc_id"] == "SOW_Enterprise_Client_Sample"


def test_failed_contract_review_adds_reason_not_approval(env):
    env.agent.reviewer = FakeReviewer(fail=True)
    run = post_signed(env.client, RUNS, seat_event("CUST-0040", "CTR-0040", 50)).json()
    assert run["state"] == "WAITING_FOR_APPROVAL"
    assert run["approval"]["reasons"] == [
        "MONTHLY_IMPACT_OVER_THRESHOLD", "NEGOTIATED_CONTRACT_TERMS", "CONTRACT_REVIEW_UNAVAILABLE"]
    assert "error" in run["clause_check"]["review"]


def test_rule_violations_are_rejected_by_agent(env):
    r = post_signed(env.client, RUNS, seat_event("CUST-0011", "CTR-0011", 5)).json()
    assert r["state"] == "REJECTED" and r["decision"]["code"] == "CUSTOMER_NOT_ACTIVE"
    cust, ctr = customers("STARTER", seats=100)[2]
    before = account(cust, ctr)
    r = post_signed(env.client, RUNS, seat_event(cust, ctr, 200)).json()
    assert r["state"] == "REJECTED" and r["decision"]["code"] == "PLAN_AMENDMENT_REQUIRED"
    assert account(cust, ctr) == before
    other, _ = customers("STARTER")[3]
    r = post_signed(env.client, RUNS, seat_event(other, ctr, 5)).json()
    assert r["state"] == "REJECTED" and r["decision"]["code"] == "CONTRACT_CUSTOMER_MISMATCH"


def test_vague_request_type_needs_human(env):
    cust, ctr = customers("STARTER")[4]
    run = post_signed(env.client, RUNS, seat_event(cust, ctr, 2, kind="RESERVATION")).json()
    assert run["state"] == "WAITING_FOR_APPROVAL" and run["approval"]["reasons"] == ["REQUEST_TYPE_NOT_PURCHASE"]


def test_rolling_window_counts_activated_seats_for_threshold(env):
    cust, ctr = customers("ENTERPRISE")[7]
    seats_before, _ = account(cust, ctr)
    first = post_signed(env.client, RUNS, seat_event(cust, ctr, 30)).json()
    assert first["state"] == "EXECUTED"                      # $350/month
    second = post_signed(env.client, RUNS, seat_event(cust, ctr, 20)).json()
    assert second["state"] == "WAITING_FOR_APPROVAL"         # 30 + 20 = $583.33/month
    assert second["impact"]["aggregate_seats_30d"] == 50
    assert second["impact"]["prior_requests_in_window"] == [first["request_id"]]
    # activated seats are already in total_seats: not double counted for the tier check
    assert second["impact"]["projected_seats"] == seats_before + 30 + 20


def test_request_id_idempotency(env):
    cust, ctr = customers("STARTER")[5]
    ev = seat_event(cust, ctr, 3)
    first = post_signed(env.client, RUNS, ev)
    again = post_signed(env.client, RUNS, {**ev, "event_id": str(uuid.uuid4())})
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert again.json()["run_id"] == first.json()["run_id"]
    changed = {**ev, "data": {**ev["data"], "added_seats": 4}}
    r = post_signed(env.client, RUNS, changed)
    assert r.status_code == 409 and r.json()["code"] == "REQUEST_ID_REUSED"


# --------------------------------------------------------------------------- failure safety


def test_execution_failure_leaves_no_partial_state(env):
    cust, ctr = customers("ENTERPRISE")[8]
    before = account(cust, ctr)
    run, token = hitl_run(env, cust, ctr)
    query("UPDATE contracts SET status = 'NONRENEWING' WHERE contract_id = %s", (ctr,))  # changes after analysis
    try:
        r = act(env.client, token)
    finally:
        query("UPDATE contracts SET status = 'ACTIVE' WHERE contract_id = %s", (ctr,))
    assert r.status_code == 200
    done = r.json()
    assert done["state"] == "FAILED" and "NONRENEWING" in done["error"]["message"]
    assert done["approval"]["status"] == "APPROVED"           # the human decision is still on record
    assert account(cust, ctr) == before
    [(sr,)] = query("SELECT status FROM seat_requests WHERE request_id = %s", (run["request_id"],))
    assert sr == "APPROVED"                                   # approved, never activated
    final = [e.json for e in env.outbox if e.json["event_type"] == "approval.finalized"][-1]["data"]
    assert final["final_state"] == "FAILED"


def test_unexpected_error_marks_run_failed(env, monkeypatch):
    def boom(*a, **k):
        raise ValueError("approver directory unavailable")

    monkeypatch.setattr(env.agent, "_open_approval", boom)
    cust, ctr = customers("ENTERPRISE")[9]
    r = post_signed(env.client, RUNS, seat_event(cust, ctr, 43))
    assert r.status_code == 500 and r.json()["code"] == "AGENT_FAILED"
    run = r.json()["run"]
    assert run["state"] == "FAILED" and run["error"]["type"] == "ValueError"
    [(state,)] = query("SELECT state FROM governance_runs WHERE run_id = %s", (run["run_id"],))
    assert state == "FAILED"


# --------------------------------------------------------------------------- e2e: agent <-> n8n <-> API

import asyncio  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402

from app.schemas.webhooks import SeatRequestData  # noqa: E402
from app.services.n8n.dispatcher import APPROVAL_DECISION_RELAY  # noqa: E402


def _stack_ready() -> bool:
    try:
        h = httpx.get("http://127.0.0.1:8010/health", timeout=2).json()
        return all(h["components"][c]["status"] == "ok" for c in ("database", "n8n"))
    except (httpx.HTTPError, ValueError, KeyError):
        return False


def _callback(correlation_id: str, timeout_s: float = 20):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rows = query("SELECT payload FROM automation_events WHERE direction = 'callback' "
                     "AND payload->'data'->>'correlation_id' = %s", (correlation_id,))
        if rows:
            return rows[0][0]
        time.sleep(0.5)
    raise AssertionError(f"no n8n callback for {correlation_id}")


@pytest.mark.e2e
@pytest.mark.skipif(not _stack_ready(), reason="uvicorn on :8010 + n8n not both up")
def test_e2e_hitl_round_trip_through_n8n(snapshot):
    cust, ctr = customers("ENTERPRISE")[10]
    seats_before, arr_before = account(cust, ctr)
    agent = GovernanceAgent(clause_reviewer=FakeReviewer())
    data = SeatRequestData(request_id=f"{PREFIX}{uuid.uuid4().hex[:10]}", customer_id=cust, contract_id=ctr,
                           added_seats=43, request_type="PURCHASE", requester_email="it-admin@client.example",
                           requested_at=datetime.now(timezone.utc))
    run = agent.start(data, source="pytest-e2e")
    assert run.state == State.WAITING_FOR_APPROVAL
    [(path, requested_event)] = run.notifications

    # 1. API -> n8n: approval card
    live = N8nDispatcher.from_settings(mode="live")
    sent = asyncio.run(live.dispatch(path, requested_event))
    assert sent.outcome == "DELIVERED" and sent.status_code == 202
    card_cb = _callback(requested_event["event_id"])
    assert card_cb["data"]["action"] == "approval_card_sent"
    assert requested_event["data"]["approval_token"] not in json.dumps(card_cb)

    # 2. approver -> n8n relay -> API: signed decision; API executes and notifies n8n again
    decision = {"event_id": str(uuid.uuid4()), "event_type": "approval.decision", "source": "approver-ui",
                "data": {"approval_token": requested_event["data"]["approval_token"], "action": "APPROVE",
                         "approver_id": "EMP-0082", "comment": "e2e", "approver_signature": "Parker Kim"}}
    body = canonical_json(decision)
    r = httpx.post(f"{SETTINGS.n8n_webhook_url}/{APPROVAL_DECISION_RELAY}", content=body, timeout=60,
                   headers={"content-type": "application/json", **sign_headers(SETTINGS.webhook_signing_secret, body)})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["state"] == "EXECUTED"
    assert account(cust, ctr) == (seats_before + 43, arr_before + Decimal("6020.00"))

    rows = []
    deadline = time.time() + 20
    while time.time() < deadline and not rows:
        rows = query("SELECT payload->>'event_id', status FROM automation_events WHERE direction = 'outbound' "
                     "AND event_type = 'approval.finalized' AND payload->'data'->>'request_id' = %s", (data.request_id,))
        time.sleep(0.5)
    [(final_event_id, status)] = rows
    assert status == "DELIVERED"
    outcome_cb = _callback(final_event_id)
    assert outcome_cb["data"]["action"] == "approval_outcome_notified"
    assert outcome_cb["data"]["outcome"]["final_state"] == "EXECUTED"

    # replaying the relayed approval is refused end to end
    r = httpx.post(f"{SETTINGS.n8n_webhook_url}/{APPROVAL_DECISION_RELAY}", content=body, timeout=60,
                   headers={"content-type": "application/json", **sign_headers(SETTINGS.webhook_signing_secret, body)})
    assert r.status_code == 409 and r.json()["result"]["code"] == "ALREADY_DECIDED"
