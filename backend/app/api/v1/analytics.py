"""Executive analytics (Stage 6 dashboard) and operational metrics. Read-only, OPERATOR+."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from app.core.db import get_conn
from app.core.errors import problem_responses
from app.core.security import Principal, Role, get_current_user, require_role
from typing import Literal

from app.schemas.analytics import (
    ActivityResponse, AgentsResponse, DashboardResponse, MetricsResponse, OperationsResponse, SlaBlock, WhoAmI,
    WorkflowBoard, WorkflowsResponse,
)
from app.services.analytics import dashboard, platform

router = APIRouter(prefix="/analytics", tags=["analytics"])
system_router = APIRouter(tags=["system"])
viewer = require_role(Role.OPERATOR, Role.SERVICE)
AUTH_ERRORS = problem_responses(401, 403, 422)


@router.get("/dashboard", response_model=DashboardResponse, responses=AUTH_ERRORS)
def get_dashboard(activity_limit: int = Query(20, ge=0, le=100), _: Principal = Depends(viewer)):
    """Mission-control snapshot: revenue, customers, HITL queue, SLA, agent and automation KPIs, activity."""
    with get_conn() as conn, conn.cursor() as cur:
        return dashboard.build_dashboard(cur, activity_limit=activity_limit)


@router.get("/operations", response_model=OperationsResponse, responses=AUTH_ERRORS)
def get_operations(window: Literal["24h", "7d", "30d"] = Query("24h"), _: Principal = Depends(viewer)):
    """Control-panel data: KPIs vs the previous window, executions over time (successful / failed /
    human review), per-workflow stats and agent status. Time saved is an estimate (see assumed_minutes_saved)."""
    with get_conn() as conn, conn.cursor() as cur:
        return dashboard.operations_summary(cur, window)


@router.get("/workflow-runs", response_model=WorkflowBoard, responses=AUTH_ERRORS)
def get_workflow_runs(window: Literal["24h", "7d", "30d"] = Query("24h"), limit: int = Query(12, ge=1, le=50),
                      _: Principal = Depends(viewer)):
    """Queue board: governance agent runs + n8n workflow runs, items needing a person first."""
    from app.api.v1.approvals import get_governance_agent
    from app.schemas.governance import GovernanceRunView
    from app.services.agents import explain

    agent = get_governance_agent()
    since = datetime.now(timezone.utc) - timedelta(hours=dashboard.WINDOWS[window][0])
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT run_id FROM governance_runs WHERE updated_at > %s "
                    "OR state NOT IN ('EXECUTED', 'REJECTED', 'FAILED') ORDER BY updated_at DESC LIMIT %s",
                    (since, limit))
        ids = [str(r["run_id"]) for r in cur.fetchall()]
    rows = [explain.summary(GovernanceRunView.from_run(agent.load(i))) for i in ids]
    with get_conn() as conn, conn.cursor() as cur:
        items = dashboard.workflow_board(cur, rows, window, limit)
    counts = {s: sum(i["state"] == s for i in items) for s in ("running", "waiting", "completed", "rejected", "failed")}
    return WorkflowBoard(window=window, counts=counts, items=items)


@router.get("/n8n-workflows", response_model=WorkflowsResponse, responses=AUTH_ERRORS)
def get_n8n_workflows(window: Literal["24h", "7d", "30d"] = Query("7d"), _: Principal = Depends(viewer)):
    """Workflow pipelines with per-node counts, success rates and latency from n8n's execution store.
    Only durations and statuses are read from stored runs; payloads never leave n8n."""
    return platform.workflows_summary(window)


@router.get("/ai-agents", response_model=AgentsResponse, responses=AUTH_ERRORS)
def get_ai_agents(window: Literal["24h", "7d", "30d"] = Query("7d"), _: Principal = Depends(viewer)):
    """The platform's six agents: live state, tools, rule constraints, models and sanitized recent traces."""
    with get_conn() as conn, conn.cursor() as cur:
        return platform.agents_summary(cur, window)


@router.get("/activity", response_model=ActivityResponse, responses=AUTH_ERRORS)
def get_activity(limit: int = Query(50, ge=1, le=200), _: Principal = Depends(viewer)):
    with get_conn() as conn, conn.cursor() as cur:
        items = dashboard.activity_feed(cur, limit)
    return ActivityResponse(count=len(items), items=items)


@router.get("/sla", response_model=SlaBlock, responses=AUTH_ERRORS)
def get_sla(_: Principal = Depends(viewer)):
    """SOP-01 resolution compliance over resolved tickets, plus open breached / at-risk counts."""
    with get_conn() as conn, conn.cursor() as cur:
        return dashboard.sla_summary(cur, datetime.now(timezone.utc))


@system_router.get("/metrics", response_model=MetricsResponse, responses=AUTH_ERRORS)
def get_metrics(_: Principal = Depends(viewer)):
    """Aggregated operational KPIs for monitoring (a compact subset of the dashboard)."""
    d = None
    with get_conn() as conn, conn.cursor() as cur:
        d = dashboard.build_dashboard(cur, activity_limit=0)
    auto, agents = d["automation"], d["agents"]
    return MetricsResponse(
        generated_at=d["generated_at"],
        arr={"active": d["revenue"]["active_arr"], "baseline": d["revenue"]["baseline_arr"],
             "delta": d["revenue"]["delta_vs_baseline"]},
        active_customers=d["customers"]["active_customers"],
        pending_approvals=d["approvals"]["pending"],
        executions={
            "governance_runs_total": agents["total_runs"], "governance_runs_active": agents["active_runs"],
            "governance_runs_by_state": agents["by_state"],
            "n8n_dispatches_total": sum(auto["n8n_dispatches"][k] for k in ("delivered", "failed", "queued_offline")),
            "workflow_runs_total": auto["workflow_runs"]["completed"] + auto["workflow_runs"]["failed"],
        },
        rates={
            "n8n_dispatch_success_pct": auto["n8n_dispatches"]["success_rate_pct"],
            "workflow_success_pct": auto["workflow_runs"]["success_rate_pct"],
            "inbound_accept_pct": auto["inbound_events"]["accept_rate_pct"],
            "agent_failure_pct": agents["failure_rate_pct"],
            "sla_compliance_pct": d["sla"]["compliance_rate_pct"],
        },
    )


@system_router.get("/auth/me", response_model=WhoAmI, responses=problem_responses(401))
def whoami(principal: Principal = Depends(get_current_user)):
    """The authenticated principal as the API sees it (roles after hierarchy expansion)."""
    return WhoAmI(**principal.model_dump(mode="json"))
