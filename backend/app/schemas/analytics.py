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


# --------------------------------------------------------------------------- operations control panel

class CountKpi(Strict):
    value: int
    previous: int
    delta_pct: Pct = Field(description="% change vs the previous window; null when the previous window was empty")


class OfTotalKpi(CountKpi):
    of_total: int


class RateKpi(Strict):
    value: Pct
    previous: Pct
    delta_points: float | None


class HoursKpi(Strict):
    value: float
    previous: float
    delta_pct: Pct
    estimated: Literal[True] = Field(description="Derived from assumed_minutes_saved, not measured")


class OperationsKpis(Strict):
    active_workflows: OfTotalKpi
    total_executions: CountKpi
    success_rate: RateKpi
    time_saved_hours: HoursKpi
    active_agents: OfTotalKpi


class SeriesPoint(Strict):
    t: datetime
    successful: int
    failed: int
    human_review: int


class WorkflowStat(Strict):
    key: str
    name: str
    kind: str
    executions: int
    success_rate: float
    human_review: int
    last_run_at: datetime


class AgentStatus(Strict):
    key: str
    name: str
    kind: str
    status: Literal["active", "idle", "in_review", "error"]
    detail: str
    runs: int
    last_run_at: datetime | None


class OperationsResponse(Strict):
    generated_at: datetime
    window: Literal["24h", "7d", "30d"]
    window_start: datetime
    bucket_hours: int
    kpis: OperationsKpis
    series: list[SeriesPoint]
    workflows: list[WorkflowStat]
    agents: list[AgentStatus]
    agent_status: dict[Literal["active", "idle", "in_review", "error"], int]
    assumed_minutes_saved: dict[str, int]


# --------------------------------------------------------------------------- workflow queue board

class BoardItem(Strict):
    id: str
    workflow: str
    name: str
    kind: str
    subject: str | None
    state: Literal["running", "waiting", "completed", "rejected", "failed"]
    priority: Literal["critical", "high", "normal"]
    steps_done: int
    steps_total: int
    current_step: str | None
    detail: str
    started_at: datetime
    updated_at: datetime


class WorkflowBoard(Strict):
    window: Literal["24h", "7d", "30d"]
    counts: dict[Literal["running", "waiting", "completed", "rejected", "failed"], int]
    items: list[BoardItem]


# --------------------------------------------------------------------------- n8n workflows and AI agents pages

class WorkflowNode(Strict):
    name: str
    role: str
    state: Literal["ok", "fail", "idle", "unknown"]
    runs: int
    errors: int
    success_rate: float | None
    avg_ms: float | None


class WorkflowEdge(Strict):
    source: str
    target: str
    branch: Literal["yes", "no"] | None


class WorkflowPipeline(Strict):
    key: str
    n8n_id: str
    name: str
    summary: str
    published: bool | None = Field(description="Published in n8n; null when telemetry is unavailable")
    state: Literal["healthy", "degraded", "idle", "unknown"]
    executions: int
    succeeded: int
    failed: int
    running: int
    success_rate: float | None
    avg_latency_ms: float | None
    p95_latency_ms: float | None
    last_run_at: datetime | None
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]
    sample_input: dict = Field(description="Synthetic example, not taken from a real execution")


class WorkflowsResponse(Strict):
    generated_at: datetime
    window: Literal["24h", "7d", "30d"]
    window_start: datetime
    telemetry_available: bool
    retention_hours: int
    window_exceeds_retention: bool
    workflows: list[WorkflowPipeline]


class AgentTool(Strict):
    name: str
    description: str


class AgentTrace(Strict):
    at: datetime
    title: str
    detail: str
    tone: Literal["ok", "wait", "fail", "idle"]


class AgentCard(Strict):
    key: str
    name: str
    engine: str
    summary: str
    state: Literal["active", "in_review", "idle", "error"]
    state_detail: str
    uses_llm: bool
    models: list[str]
    model_note: str
    tools: list[AgentTool]
    constraints: list[str]
    runs: int
    last_run_at: datetime | None
    traces: list[AgentTrace]


class AgentsResponse(Strict):
    generated_at: datetime
    window: Literal["24h", "7d", "30d"]
    window_start: datetime
    agents: list[AgentCard]
    state_counts: dict[Literal["active", "in_review", "idle", "error"], int]
