"""Pydantic v2 schemas for the governance agent and the HITL approval queue."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AgentState = Literal["INITIATED", "ANALYZING_CONTRACT", "CALCULATING_IMPACT", "WAITING_FOR_APPROVAL",
                     "APPROVED", "REJECTED", "EXECUTED", "FAILED"]


class ApprovalView(BaseModel):
    approval_id: str
    run_id: str
    request_id: str
    customer_id: str
    contract_id: str
    plan_tier: str
    proposed_seats: int
    monthly_cost_impact: str
    aggregate_monthly_30d: str
    annual_arr_impact: str
    reasons: list[str]
    status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"]
    assigned_approver_id: str
    expires_at: str
    created_at: str
    decided_at: str | None
    decided_by: str | None
    decision_comment: str | None
    card: dict[str, Any]


StepStatus = Literal["complete", "in_progress", "pending", "skipped", "rejected", "failed", "expired"]


class PipelineStep(BaseModel):
    key: str
    name: str
    description: str
    status: StepStatus
    at: str | None


class Pipeline(BaseModel):
    steps: list[PipelineStep]
    progress_pct: int
    current_step: str | None
    finished: bool
    eta: str | None = Field(description="Plain-language time to completion; null when finished")


class ReasoningCheck(BaseModel):
    label: str
    status: Literal["pass", "flag", "fail", "skipped"]
    detail: str


class ReasoningAction(BaseModel):
    label: str
    status: Literal["done"]


class Explanation(BaseModel):
    headline: str
    checks: list[ReasoningCheck]
    rules_evaluated: int
    rules_total: int
    flags: int
    failures: int
    decision_basis: Literal["deterministic"]
    advisory_confidence: Literal["high", "medium", "low"] | None = Field(
        description="The copilot's own rating of its advisory contract review; the decision itself is rule-based")
    actions: list[ReasoningAction]


class GovernanceRunView(BaseModel):
    run_id: str
    request_id: str
    customer_id: str
    contract_id: str
    state: AgentState
    decision: dict[str, Any] | None = None
    contract: dict[str, Any] | None = None
    clause_check: dict[str, Any] | None = None
    impact: dict[str, Any] | None = None
    human_decision: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    approval: ApprovalView | None = None
    history: list[dict[str, Any]]
    error: dict[str, Any] | None = None
    duplicate: bool = False
    pipeline: Pipeline
    explanation: Explanation

    @classmethod
    def from_run(cls, run) -> "GovernanceRunView":
        from app.services.agents import explain

        ctx = run.context or {}
        state = run.state.value
        return cls(run_id=run.run_id, request_id=run.request_id, customer_id=run.customer_id,
                   contract_id=run.contract_id, state=state, decision=ctx.get("decision"),
                   contract=ctx.get("contract"), clause_check=ctx.get("clause_check"), impact=ctx.get("impact"),
                   human_decision=ctx.get("human_decision"), execution=ctx.get("execution"),
                   approval=run.approval, history=run.history, error=run.error, duplicate=run.duplicate,
                   pipeline=explain.pipeline(state, ctx, run.history, run.approval),
                   explanation=explain.explanation(state, ctx, run.approval, run.error))


class GovernanceRunSummary(BaseModel):
    run_id: str
    request_id: str
    customer_id: str
    contract_id: str
    state: AgentState
    priority: Literal["critical", "high", "normal"]
    headline: str
    progress_pct: int
    current_step: str | None
    steps_done: int
    steps_total: int
    monthly_impact: str | None
    started_at: str
    updated_at: str


class GovernanceRunList(BaseModel):
    count: int
    runs: list[GovernanceRunSummary]


class ApprovalActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["APPROVE", "REJECT"]
    approver_id: str = Field(pattern=r"^EMP-\d{4}$", description="Must be the assigned approver")
    comment: str | None = Field(None, max_length=2000, description="Required when rejecting")
    approver_signature: str | None = Field(None, max_length=200, description="Typed-name attestation, stored verbatim")


class PendingApprovals(BaseModel):
    count: int
    expired_now: int
    approvals: list[ApprovalView]
