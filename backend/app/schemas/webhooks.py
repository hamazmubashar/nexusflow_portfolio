"""Pydantic v2 schemas for every webhook event and response.

All events share one signed envelope: {event_id, event_type, occurred_at, source, data}.
event_id is the idempotency key; event_type is a closed Literal per endpoint so a
payload can't be replayed against the wrong route.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer

Severity = Literal["P1", "P2", "P3", "P4"]
PlanTier = Literal["STARTER", "GROWTH", "ENTERPRISE"]

TICKET_ID = r"^TK-\d{4}$"
CUSTOMER_ID = r"^CUST-\d{4}$"
CONTRACT_ID = r"^CTR-\d{4}$"
INVOICE_ID = r"^INV-20\d{2}-\d{4}$"
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EventEnvelope(StrictModel):
    event_id: UUID = Field(default_factory=uuid4)
    event_type: str
    occurred_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = Field(min_length=1, max_length=64, examples=["n8n", "itsm", "nexusflow-api"])
    data: Any


# --------------------------------------------------------------------------- tickets

class TicketEscalationData(StrictModel):
    ticket_id: str = Field(pattern=TICKET_ID)
    reported_severity: Severity | None = Field(
        None, description="Severity proposed by the reporter/agent; may raise but never lower the recorded one")
    summary: str | None = Field(None, max_length=2000, description="Free-text context passed to the RAG copilot")
    request_copilot: bool = Field(False, description="Force copilot triage even for P3/P4")
    as_of: AwareDatetime | None = Field(None, description="Evaluation time; defaults to now")


class TicketEscalationEvent(EventEnvelope):
    event_type: Literal["ticket.created", "ticket.updated", "ticket.sla_check"]
    data: TicketEscalationData


class Contact(BaseModel):
    employee_id: str
    name: str
    role: str
    email: str


class SlaStatus(BaseModel):
    clock: Literal["24x7", "coverage", "business"]
    response_target_minutes: int
    resolution_target_minutes: int
    elapsed_eligible_minutes: int
    resolution_budget_consumed_pct: float
    response_due_at: datetime
    resolution_due_at: datetime
    breached: bool


class EscalationDecision(BaseModel):
    level: Literal["NONE", "ON_CALL", "SUPPORT_MANAGER", "VP_ENGINEERING",
                   "VP_CUSTOMER_SUCCESS", "EXECUTIVE_CHAIR"]
    reason: str
    notify: list[Contact]


class TicketEscalationResult(BaseModel):
    event_id: UUID
    ticket_id: str
    customer_id: str
    company_name: str
    plan_tier: PlanTier
    recorded_severity: Severity
    effective_severity: Severity
    status: str
    is_open: bool
    assigned_to: Contact | None
    sla: SlaStatus
    escalation: EscalationDecision
    requires_copilot_triage: bool
    dispatch: Literal["scheduled", "not_required"]
    duplicate: bool = False


# --------------------------------------------------------------------------- seats

class SeatRequestData(StrictModel):
    request_id: str = Field(pattern=r"^SRQ-[A-Za-z0-9-]{4,40}$")
    customer_id: str = Field(pattern=CUSTOMER_ID)
    contract_id: str = Field(pattern=CONTRACT_ID)
    added_seats: int = Field(gt=0, le=10000)
    request_type: Literal["PURCHASE", "RESERVATION", "TEMPORARY_ACCESS", "UNSPECIFIED"] = "UNSPECIFIED"
    requester_email: str = Field(pattern=EMAIL)
    requested_at: AwareDatetime
    desired_activation_date: date | None = None
    justification: str | None = Field(None, max_length=2000)


class SeatRequestEvent(EventEnvelope):
    event_type: Literal["seat.requested"]
    data: SeatRequestData


SeatStatus = Literal["PENDING_PLAN_AMENDMENT", "PENDING_CLARIFICATION",
                     "PENDING_FINANCE_APPROVAL", "PENDING_CUSTOMER_AUTHORIZATION"]


class SeatCalculation(BaseModel):
    annual_unit_price: Decimal
    monthly_equivalent: Decimal          # this request alone
    window_start: datetime               # exclusive
    window_end: datetime                 # inclusive (= requested_at)
    prior_requests_in_window: list[str]
    aggregate_seats_30d: int
    aggregate_monthly_30d: Decimal
    threshold_monthly: Decimal
    committed_seats: int
    projected_seats: int
    tier_seat_range: tuple[int, int]

    @field_serializer("annual_unit_price", "monthly_equivalent", "aggregate_monthly_30d", "threshold_monthly")
    def _money(self, v: Decimal) -> str:
        # Display only: the threshold test runs on the exact Decimal (services/seats.py)
        return str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class SeatRequestDecision(BaseModel):
    event_id: UUID
    request_id: str
    customer_id: str
    contract_id: str
    company_name: str
    plan_tier: PlanTier
    added_seats: int
    status: SeatStatus
    requires_finance_approval: bool
    requires_plan_amendment: bool
    requires_clarification: bool
    requires_customer_authorization: bool
    approver: Contact | None
    calculation: SeatCalculation
    notes: list[str]
    duplicate: bool = False


# --------------------------------------------------------------------------- invoices

class DelinquentInvoiceData(StrictModel):
    invoice_id: str = Field(pattern=INVOICE_ID)
    customer_id: str | None = Field(None, pattern=CUSTOMER_ID, description="Optional cross-check")
    as_of: date | None = None
    draft_notice: bool = True


class DelinquentInvoiceEvent(EventEnvelope):
    event_type: Literal["invoice.delinquent", "invoice.overdue_poll"]
    data: DelinquentInvoiceData


class OverdueInvoice(BaseModel):
    invoice_id: str
    customer_id: str
    company_name: str
    amount: Decimal
    outstanding: Decimal
    due_date: date
    days_overdue: int
    invoice_status: str
    customer_status: str

    @field_serializer("amount", "outstanding")
    def _money(self, v: Decimal) -> str:
        return str(v)


class AccountSummary(BaseModel):
    customer_id: str
    company_name: str
    domain: str
    tier: str
    customer_status: str
    csm: Contact
    contract_id: str
    plan_tier: PlanTier
    contract_status: str
    annual_recurring_revenue: Decimal
    contract_end_date: date
    auto_renew: bool
    total_outstanding: Decimal
    overdue_invoices: list[OverdueInvoice]
    open_tickets_by_severity: dict[str, int]

    @field_serializer("annual_recurring_revenue", "total_outstanding")
    def _money(self, v: Decimal) -> str:
        return str(v)


class CollectionNoticeDraft(BaseModel):
    status: Literal["DRAFT_FOR_REVIEW"] = "DRAFT_FOR_REVIEW"
    to: str
    from_: str = Field(serialization_alias="from")
    cc: list[str]
    subject: str
    body: str
    illustrative_cure_deadline: date
    requires_review_by: Contact


class DelinquentInvoiceResult(BaseModel):
    event_id: UUID
    invoice: OverdueInvoice
    account: AccountSummary
    notice: CollectionNoticeDraft | None
    recommended_actions: list[str]
    guardrails: list[str]
    duplicate: bool = False


# --------------------------------------------------------------------------- n8n callbacks

class WorkflowCallbackData(StrictModel):
    workflow: Literal["p1_ticket_escalation", "seat_expansion_trigger", "delinquent_invoice_reconciliation",
                      "hitl_approval_notification"]
    correlation_id: UUID | None = Field(None, description="event_id of the event that started the run")
    execution_id: str | None = None
    action: str = Field(min_length=1, max_length=120)
    subject_id: str | None = None
    outcome: dict[str, Any] = Field(default_factory=dict)


class WorkflowCallbackEvent(EventEnvelope):
    event_type: Literal["workflow.completed", "workflow.failed"]
    data: WorkflowCallbackData


class CallbackAck(BaseModel):
    event_id: UUID
    recorded: bool
    duplicate: bool = False


class ErrorDetail(BaseModel):
    code: str
    message: str
