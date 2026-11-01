"""Governance agent runs + HITL approval queue.

POST /api/v1/agents/governance/runs           start a run          OPERATOR+ user or n8n service
GET  /api/v1/agents/governance/runs/{run_id}  run state            OPERATOR+ user or n8n service
GET  /api/v1/approvals/pending                operator queue       OPERATOR+ user or n8n service
GET  /api/v1/approvals/{approval_id}          one queue item       OPERATOR+ user or n8n service
POST /api/v1/approvals/{approval_id}/decision-token  <=10 min token  the assigned approver (MANAGER JWT)
POST /api/v1/approvals/{token}/action         decision             MANAGER+ user deciding as themself,
                                                                   or the n8n relay (HMAC) for the approver
The signed single-use token stays the credential for *which* approval; the role guard
controls *who* may present it. A user can only decide as the employee in their JWT, and the
token binds the assigned approver, so ADMIN cannot override another person's approval.

n8n is notified (approval.requested / approval.finalized) in background tasks after the
database commit, so a slow or offline n8n never blocks or rolls back a decision.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from functools import lru_cache
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.api.v1.copilot import get_copilot
from app.api.v1.webhooks import dispatch_and_log, get_dispatcher, parse_event
from app.core.db import get_conn
from app.core.errors import APIError, problem_response, problem_responses
from app.core.security import Principal, Role, require_role
from app.schemas.governance import (
    ApprovalActionRequest, ApprovalView, GovernanceRunList, GovernanceRunView, PendingApprovals,
)
from app.schemas.webhooks import SeatRequestEvent
from app.services import events
from app.services.agents import explain
from app.services.agents.governance_agent import GovernanceAgent, GovernanceError, public_approval
from app.services.n8n.dispatcher import N8nDispatcher

log = logging.getLogger("nexusflow.approvals")
agents_router = APIRouter(prefix="/agents/governance", tags=["agents"])
approvals_router = APIRouter(prefix="/approvals", tags=["approvals"])

reader = require_role(Role.OPERATOR, Role.SERVICE)
decider = require_role(Role.MANAGER, Role.SERVICE)
AGENT_ERRORS = problem_responses(401, 403, 404, 409, 410, 422, 500)


@lru_cache
def get_governance_agent() -> GovernanceAgent:
    return GovernanceAgent(clause_reviewer=lambda question: get_copilot().ask(question, top_k=6))


def redact(payload: dict) -> dict:
    """Copy of an n8n event that is safe to persist: no approval token anywhere."""
    safe = copy.deepcopy(payload)
    data = safe.get("data", {})
    if "approval_token" in data:
        data["approval_token"] = "[redacted]"
        data["callback"]["url"] = data["callback"]["url"].rsplit("/approvals/", 1)[0] + "/approvals/[redacted]/action"
    return safe


def schedule(background: BackgroundTasks, dispatcher: N8nDispatcher, notifications, subject_id: str) -> None:
    for path, payload in notifications:
        background.add_task(dispatch_and_log, dispatcher, path, payload, subject_id, redact(payload))


def governance_problem(request: Request, exc: GovernanceError) -> JSONResponse:
    """Returned (not raised) so FastAPI still runs the BackgroundTasks holding n8n notifications."""
    extra = {"run": GovernanceRunView.from_run(exc.run).model_dump(mode="json")} if exc.run is not None else {}
    return problem_response(request, exc.status_code, exc.code, exc.message, **extra)


# --------------------------------------------------------------------------- agent runs

@agents_router.post("/runs", status_code=201, response_model=GovernanceRunView, responses=AGENT_ERRORS)
async def start_run(
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(reader),
    agent: GovernanceAgent = Depends(get_governance_agent),
    dispatcher: N8nDispatcher = Depends(get_dispatcher),
):
    """Evaluate a seat request end to end; pauses in WAITING_FOR_APPROVAL when a human is required."""
    event = parse_event(SeatRequestEvent, await request.body())
    source = event.source if principal.kind == "service" else f"{event.source}:{principal.subject}"
    try:
        run = await run_in_threadpool(agent.start, event.data, source)
    except GovernanceError as exc:
        schedule(background, dispatcher, exc.notifications, event.data.request_id)
        return governance_problem(request, exc)
    schedule(background, dispatcher, run.notifications, run.request_id)
    view = GovernanceRunView.from_run(run)
    if run.duplicate:
        return JSONResponse(status_code=200, content=view.model_dump(mode="json"))
    return view


@agents_router.get("/runs", response_model=GovernanceRunList, responses=problem_responses(401, 403, 422))
def list_runs(limit: int = Query(20, ge=1, le=100), active_only: bool = Query(False),
              _: Principal = Depends(reader), agent: GovernanceAgent = Depends(get_governance_agent)):
    """Most recently updated runs first, each with its pipeline progress and decision headline."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT run_id FROM governance_runs "
                    "WHERE (NOT %s OR state NOT IN ('EXECUTED', 'REJECTED', 'FAILED')) "
                    "ORDER BY updated_at DESC LIMIT %s", (active_only, limit))
        ids = [str(r["run_id"]) for r in cur.fetchall()]
    runs = [explain.summary(GovernanceRunView.from_run(agent.load(i))) for i in ids]
    return GovernanceRunList(count=len(runs), runs=runs)


@agents_router.get("/runs/{run_id}", response_model=GovernanceRunView, responses=AGENT_ERRORS)
def get_run(request: Request, run_id: UUID, _: Principal = Depends(reader),
            agent: GovernanceAgent = Depends(get_governance_agent)):
    try:
        return GovernanceRunView.from_run(agent.load(str(run_id)))
    except GovernanceError as exc:
        return governance_problem(request, exc)


# --------------------------------------------------------------------------- HITL queue

@approvals_router.get("/pending", response_model=PendingApprovals, responses=problem_responses(401, 403))
def pending_approvals(
    background: BackgroundTasks,
    _: Principal = Depends(reader),
    agent: GovernanceAgent = Depends(get_governance_agent),
    dispatcher: N8nDispatcher = Depends(get_dispatcher),
):
    expired = agent.expire_stale()
    schedule(background, dispatcher, expired, "approval-queue")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM approval_requests WHERE status = 'PENDING' AND expires_at > %s "
                    "ORDER BY expires_at, created_at", (agent.now(),))
        rows = [public_approval(r) for r in cur.fetchall()]
    return PendingApprovals(count=len(rows), expired_now=len(expired), approvals=rows)


@approvals_router.get("/{approval_id}", response_model=ApprovalView, responses=problem_responses(401, 403, 404, 422))
def approval_detail(approval_id: UUID, _: Principal = Depends(reader)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM approval_requests WHERE approval_id = %s", (str(approval_id),))
        row = cur.fetchone()
    if row is None:
        raise APIError(404, "APPROVAL_NOT_FOUND", f"approval {approval_id} does not exist")
    return public_approval(row)


class DecisionToken(BaseModel):
    approval_id: str
    token: str
    expires_at: str
    method: str
    action_url: str


@approvals_router.post("/{approval_id}/decision-token", response_model=DecisionToken,
                       responses=problem_responses(401, 403, 404, 409, 410, 422))
def decision_token(request: Request, approval_id: UUID, background: BackgroundTasks,
                   principal: Principal = Depends(require_role(Role.MANAGER)),
                   agent: GovernanceAgent = Depends(get_governance_agent),
                   dispatcher: N8nDispatcher = Depends(get_dispatcher)):
    """For the signed-in assigned approver only: a <=10 min token to use with POST /{token}/action.

    Lets an operator UI decide without the n8n-delivered link token, which is never exposed by the API.
    """
    if principal.kind != "user":
        raise APIError(403, "USER_REQUIRED", "decision tokens are issued to signed-in approvers only")
    try:
        minted = agent.issue_session_token(str(approval_id), principal.subject)
    except GovernanceError as exc:
        schedule(background, dispatcher, exc.notifications, "approval-queue")
        return governance_problem(request, exc)
    _audit("PROCESSED", None, {"action": "SESSION_TOKEN_ISSUED", "approval_id": minted["approval_id"],
                               "approver_id": principal.subject, "expires_at": minted["expires_at"]})
    return DecisionToken(**minted)


@approvals_router.post("/{token}/action", response_model=GovernanceRunView, responses=AGENT_ERRORS)
async def approval_action(
    token: str,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(decider),
    agent: GovernanceAgent = Depends(get_governance_agent),
    dispatcher: N8nDispatcher = Depends(get_dispatcher),
):
    """Approve or reject. Validates role, identity, token signature, expiry and single use, then resumes the run."""
    action = parse_event(ApprovalActionRequest, await request.body())
    audit = {"action": action.action, "approver_id": action.approver_id, "comment": action.comment,
             "approver_signature": action.approver_signature, "principal": principal.subject,
             "auth": principal.auth}
    if principal.kind == "user" and action.approver_id != principal.subject:
        await run_in_threadpool(_audit, "REJECTED", None, {**audit, "error": "APPROVER_IDENTITY_MISMATCH"})
        raise APIError(403, "APPROVER_IDENTITY_MISMATCH",
                       f"{principal.subject} cannot decide as {action.approver_id}")
    try:
        run = await run_in_threadpool(agent.decide, token, action.action, action.approver_id, action.comment,
                                      action.approver_signature)
    except GovernanceError as exc:
        schedule(background, dispatcher, exc.notifications, "approval-action")
        await run_in_threadpool(_audit, "REJECTED", None, {**audit, "error": exc.code})
        return governance_problem(request, exc)
    schedule(background, dispatcher, run.notifications, run.request_id)
    await run_in_threadpool(_audit, "PROCESSED", run.request_id, {**audit, "final_state": run.state.value})
    return GovernanceRunView.from_run(run)


def _audit(status: str, subject_id: str | None, payload: dict) -> None:
    """Every decision attempt, successful or not, lands in automation_events (never the token)."""
    events.record_standalone(event_id=uuid4(), event_type="approval.decision", direction="inbound",
                             source="approver", subject_id=subject_id, status=status,
                             payload={**payload, "at": datetime.now(timezone.utc).isoformat()})
