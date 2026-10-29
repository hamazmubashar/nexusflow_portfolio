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

    @classmethod
    def from_run(cls, run) -> "GovernanceRunView":
        ctx = run.context or {}
        return cls(run_id=run.run_id, request_id=run.request_id, customer_id=run.customer_id,
                   contract_id=run.contract_id, state=run.state.value, decision=ctx.get("decision"),
                   contract=ctx.get("contract"), clause_check=ctx.get("clause_check"), impact=ctx.get("impact"),
                   human_decision=ctx.get("human_decision"), execution=ctx.get("execution"),
                   approval=run.approval, history=run.history, error=run.error, duplicate=run.duplicate)


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
