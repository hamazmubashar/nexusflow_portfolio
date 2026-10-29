"""Response contracts for /health, /api/v1/metrics, /api/v1/analytics/* and /api/v1/auth/me.

Money is a decimal string ("12400000.00") so clients never round through floats; rates are
percentages with one decimal, or null when there is nothing to divide by yet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Money = str
Pct = float | None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- health

class ComponentHealth(Strict):
    status: Literal["ok", "degraded", "down", "not_configured"]
    latency_ms: float | None = None
    detail: str | None = None
    checked_at: datetime


class VectorStoreHealth(ComponentHealth):
    extension_version: str | None = None
    indexed_chunks: dict[str, int] = Field(default_factory=dict, description="chunks per embedding model")


class HealthComponents(Strict):
    database: ComponentHealth
    pgvector: VectorStoreHealth
    n8n: ComponentHealth
    gemini: ComponentHealth


class HealthResponse(Strict):
    status: Literal["ok", "degraded", "down"]
    version: str
    environment: str
    uptime_seconds: float
    dispatch_mode: str
    components: HealthComponents


# --------------------------------------------------------------------------- dashboard blocks

class PlanArr(Strict):
    plan_tier: str
    contracts: int
    arr: Money


class RevenueBlock(Strict):
    active_arr: Money
    baseline_arr: Money
    delta_vs_baseline: Money
    mrr: Money
    active_contracts: int
    by_plan: list[PlanArr]


class CustomerBlock(Strict):
    total_customers: int
    active_customers: int = Field(description="Customers with an ACTIVE contract")
    by_status: dict[str, int]
    by_tier: dict[str, int]


class DecidedCounts(Strict):
    approved: int
    rejected: int
    expired: int


class ApprovalBlock(Strict):
    pending: int
    awaiting_expiry_sweep: int
    pending_monthly_impact: Money
    oldest_pending_age_hours: float | None
    decided: DecidedCounts


class SeverityCompliance(Strict):
    resolved: int
    within_sla: int
    compliance_rate_pct: Pct


class SlaBlock(Strict):
    compliance_rate_pct: Pct
    resolved_tickets: int
    resolved_within_sla: int
    by_severity: dict[Literal["P1", "P2", "P3", "P4"], SeverityCompliance]
    open_tickets: int
    open_breached: int
    open_at_risk: int = Field(description="Open, not yet breached, >=75% of resolution budget used")
    as_of: datetime


class AgentBlock(Strict):
    total_runs: int
    active_runs: int = Field(description="Runs not yet in a terminal state (e.g. paused WAITING_FOR_APPROVAL)")
    by_engine: dict[str, int] = Field(description="Runs per orchestration engine (langgraph | native)")
    state_transitions: int
    by_state: dict[str, int]
    seats_provisioned: int
    arr_added: Money
    failure_rate_pct: Pct


class DispatchStats(Strict):
    delivered: int
    failed: int
    queued_offline: int
    mocked: int
    success_rate_pct: Pct


class WorkflowStats(Strict):
    completed: int
    failed: int
    success_rate_pct: Pct


class InboundStats(Strict):
    processed: int
    rejected: int
    accept_rate_pct: Pct


class AutomationBlock(Strict):
    total_events: int
    n8n_dispatches: DispatchStats
    workflow_runs: WorkflowStats
    inbound_events: InboundStats


class ActivityItem(Strict):
    at: datetime
    kind: Literal["event.inbound", "event.outbound", "event.callback", "agent.transition"]
    type: str
    subject_id: str | None
    status: str
    summary: str


class DashboardResponse(Strict):
    generated_at: datetime
    revenue: RevenueBlock
    customers: CustomerBlock
    approvals: ApprovalBlock
    sla: SlaBlock
    agents: AgentBlock
    automation: AutomationBlock
    recent_activity: list[ActivityItem]


class ActivityResponse(Strict):
    count: int
    items: list[ActivityItem]


# --------------------------------------------------------------------------- metrics

class MetricsArr(Strict):
    active: Money
    baseline: Money
    delta: Money


class MetricsExecutions(Strict):
    governance_runs_total: int
    governance_runs_active: int
    governance_runs_by_state: dict[str, int]
    n8n_dispatches_total: int
    workflow_runs_total: int


class MetricsRates(Strict):
    n8n_dispatch_success_pct: Pct
    workflow_success_pct: Pct
    inbound_accept_pct: Pct
    agent_failure_pct: Pct
    sla_compliance_pct: Pct


class MetricsResponse(Strict):
    generated_at: datetime
    arr: MetricsArr
    active_customers: int
    pending_approvals: int
    executions: MetricsExecutions
    rates: MetricsRates


# --------------------------------------------------------------------------- auth

class WhoAmI(Strict):
    subject: str
    name: str | None
    kind: Literal["user", "service"]
    roles: list[str]
    effective_roles: list[str]
    auth: Literal["jwt", "hmac"]
