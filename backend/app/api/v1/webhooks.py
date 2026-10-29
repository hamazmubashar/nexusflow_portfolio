"""Webhook & event router: n8n / external systems -> FastAPI.

Every POST is HMAC-verified over the raw body *before* it is parsed (see
app.core.security), validated against its Pydantic event schema, de-duplicated on
event_id, and recorded in automation_events. Replaying an event_id returns the
stored response with HTTP 200 and `duplicate: true` instead of re-running side effects.

Outbound: a ticket that needs P1/P2 handling or copilot triage is dispatched to the
n8n P1 workflow in a background task after the response is sent, so the caller
never waits on n8n (or on the Gemini call that workflow makes).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app.core.db import get_conn
from app.core.errors import problem_response, problem_responses
from app.core.security import hmac_scheme, require_signature
from app.schemas.webhooks import (
    CallbackAck, DelinquentInvoiceEvent, DelinquentInvoiceResult, OverdueInvoice,
    SeatRequestDecision, SeatRequestEvent, TicketEscalationEvent, TicketEscalationResult,
    WorkflowCallbackEvent,
)
from app.services import events, sla
from app.services.collections import CollectionsError, list_overdue, summarize_delinquency
from app.services.n8n.dispatcher import P1_TICKET_ESCALATION, N8nDispatcher
from app.services.seats import SeatRequestError, evaluate_seat_request

log = logging.getLogger("nexusflow.webhooks")
# Service-to-service only: every route verifies the HMAC signature itself (require_signature);
# the router-level dependency just documents the scheme in OpenAPI.
router = APIRouter(prefix="/webhooks", tags=["webhooks"], dependencies=[Depends(hmac_scheme)])

ERRORS = problem_responses(401, 404, 409, 422)


def get_dispatcher(request: Request) -> N8nDispatcher:
    return request.app.state.dispatcher


def parse_event[T: BaseModel](model: type[T], body: bytes) -> T:
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail=jsonable_encoder(exc.errors(include_url=False, include_context=False)))


def reject(event, code: str, message: str, http_status: int, subject_id: str | None = None):
    """Log a rejected event in its own transaction (the main one rolls back), then raise."""
    error = {"code": code, "message": message}
    events.record_standalone(event_id=event.event_id, event_type=event.event_type, direction="inbound",
                             source=event.source, subject_id=subject_id, status="REJECTED",
                             payload=event.model_dump(mode="json"), response=error)
    raise HTTPException(http_status, detail=error)


def duplicate_response(request: Request, prior: dict) -> JSONResponse:
    if prior["status"] == "REJECTED":
        return problem_response(request, 409, "DUPLICATE_REJECTED_EVENT",
                                "This event_id was already received and rejected; send a new event_id to retry",
                                original=prior["response"])
    return JSONResponse(status_code=200, content={**prior["response"], "duplicate": True})


async def dispatch_and_log(dispatcher: N8nDispatcher, workflow_path: str, payload: dict, subject_id: str,
                           log_payload: dict | None = None) -> None:
    """Send to n8n, then audit. `log_payload` replaces what is stored when the sent one holds secrets."""
    try:
        result = await dispatcher.dispatch(workflow_path, payload)
        outcome, response = result.outcome, result.model_dump(mode="json")
    except Exception as exc:  # background task: never let it die silently
        log.exception("dispatch to %s failed", workflow_path)
        outcome, response = "FAILED", {"error": f"{type(exc).__name__}: {exc}"}
    await asyncio.to_thread(
        events.record_standalone, event_id=UUID(payload["event_id"]), event_type=payload["event_type"],
        direction="outbound", source="nexusflow-api", subject_id=subject_id, status=outcome,
        payload=log_payload if log_payload is not None else payload, response=response,
    )


def _contacts_by_role(cur, roles: list[str]) -> list[dict]:
    if not roles:
        return []
    cur.execute(
        "SELECT employee_id, first_name || ' ' || last_name AS name, role, email FROM employees "
        "WHERE role = ANY(%s) AND status = 'ACTIVE' ORDER BY array_position(%s, role), employee_id",
        (roles, roles),
    )
    seen, out = set(), []
    for r in cur.fetchall():  # first active holder of each role
        if r["role"] not in seen:
            seen.add(r["role"])
            out.append(r)
    return out


# --------------------------------------------------------------------------- tickets

@router.post("/tickets/escalate", status_code=202, response_model=TicketEscalationResult, responses=ERRORS)
def escalate_ticket(
    request: Request,
    background: BackgroundTasks,
    body: bytes = Depends(require_signature),
    dispatcher: N8nDispatcher = Depends(get_dispatcher),
):
    """Evaluate a ticket against SOP-01 SLA clocks; dispatch P1/P2 (or forced) triage to n8n."""
    event = parse_event(TicketEscalationEvent, body)
    d = event.data
    as_of = d.as_of or datetime.now(timezone.utc)

    with get_conn() as conn, conn.cursor() as cur:
        if prior := events.find_inbound(cur, event.event_id):
            return duplicate_response(request, prior)
        cur.execute(
            """SELECT t.*, c.company_name, k.plan_tier,
                      e.employee_id AS a_id, e.first_name || ' ' || e.last_name AS a_name, e.role AS a_role, e.email AS a_email
               FROM support_tickets t
               JOIN customers c USING (customer_id)
               LEFT JOIN contracts k ON k.customer_id = t.customer_id AND k.status = 'ACTIVE'
               LEFT JOIN employees e ON e.employee_id = t.assigned_employee_id
               WHERE t.ticket_id = %s""",
            (d.ticket_id,),
        )
        t = cur.fetchone()
        if t is None:
            conn.rollback()
            reject(event, "TICKET_NOT_FOUND", f"{d.ticket_id} does not exist", 404, d.ticket_id)
        if t["plan_tier"] is None:
            conn.rollback()
            reject(event, "NO_ACTIVE_CONTRACT", f"{t['customer_id']} has no ACTIVE contract to derive SLA terms", 409,
                   d.ticket_id)

        effective = sla.more_severe(t["severity"], d.reported_severity)
        is_open = t["resolved_at"] is None
        clock_end = as_of if is_open else t["resolved_at"]
        sla_status = sla.evaluate_sla(effective, t["plan_tier"], t["created_at"], clock_end)
        level, reason, roles = sla.escalation_level(effective, sla_status["resolution_budget_consumed_pct"], is_open)
        if is_open and sla_status["breached"]:
            reason += "; resolution SLA breached"
        requires_triage = is_open and (effective in ("P1", "P2") or d.request_copilot)

        result = TicketEscalationResult(
            event_id=event.event_id, ticket_id=t["ticket_id"], customer_id=t["customer_id"],
            company_name=t["company_name"], plan_tier=t["plan_tier"], recorded_severity=t["severity"],
            effective_severity=effective, status=t["status"], is_open=is_open,
            assigned_to=({"employee_id": t["a_id"], "name": t["a_name"], "role": t["a_role"], "email": t["a_email"]}
                         if t["a_id"] else None),
            sla=sla_status,
            escalation={"level": level, "reason": reason, "notify": _contacts_by_role(cur, roles)},
            requires_copilot_triage=requires_triage,
            dispatch="scheduled" if requires_triage else "not_required",
        )
        if not events.record(cur, event_id=event.event_id, event_type=event.event_type, direction="inbound",
                             source=event.source, subject_id=d.ticket_id, status="PROCESSED",
                             payload=event.model_dump(mode="json"), response=result.model_dump(mode="json")):
            conn.rollback()
            return duplicate_response(request, events.find_inbound(cur, event.event_id))

    if requires_triage:
        question = (
            f"{effective} support ticket {t['ticket_id']} for {t['company_name']} ({t['customer_id']}, "
            f"{t['plan_tier']} plan): \"{t['subject']}\". {d.summary or ''} "
            f"What are the initial response and resolution SLA targets, the escalation steps and the "
            f"SOP procedure that apply to this ticket?"
        ).replace("  ", " ")
        outbound = {
            "event_id": str(uuid4()), "event_type": "ticket.escalation_required",
            "occurred_at": datetime.now(timezone.utc).isoformat(), "source": "nexusflow-api",
            "data": {"correlation_id": str(event.event_id), "subject": t["subject"], "summary": d.summary,
                     "copilot_question": question, **result.model_dump(mode="json", exclude={"event_id"})},
        }
        background.add_task(dispatch_and_log, dispatcher, P1_TICKET_ESCALATION, outbound, t["ticket_id"])
    return result


# --------------------------------------------------------------------------- seats

@router.post("/seats/request", status_code=201, response_model=SeatRequestDecision, responses=ERRORS)
def request_seats(request: Request, body: bytes = Depends(require_signature)):
    """Evaluate a seat expansion against SOP-02 (30-day aggregate, $500/month threshold). Never provisions."""
    event = parse_event(SeatRequestEvent, body)
    d = event.data
    try:
        with get_conn() as conn, conn.cursor() as cur:
            if prior := events.find_inbound(cur, event.event_id):
                return duplicate_response(request, prior)
            decision = evaluate_seat_request(cur, d)
            approver = None
            if decision["requires_finance_approval"]:
                approver = (_contacts_by_role(cur, ["Finance Director"]) or [None])[0]
            result = SeatRequestDecision(event_id=event.event_id, approver=approver, **decision)
            if not events.record(cur, event_id=event.event_id, event_type=event.event_type, direction="inbound",
                                 source=event.source, subject_id=d.request_id, status="PROCESSED",
                                 payload=event.model_dump(mode="json"), response=result.model_dump(mode="json")):
                conn.rollback()
                return duplicate_response(request, events.find_inbound(cur, event.event_id))
    except SeatRequestError as exc:
        reject(event, exc.code, exc.message, exc.status_code, d.request_id)
    if result.duplicate:  # same request_id resubmitted under a new event_id
        return JSONResponse(status_code=200, content=result.model_dump(mode="json"))
    return result


# --------------------------------------------------------------------------- invoices

@router.post("/invoices/delinquent", response_model=DelinquentInvoiceResult, responses=ERRORS,
             response_model_by_alias=True)
def delinquent_invoice(request: Request, body: bytes = Depends(require_signature)):
    """Account summary + collection-notice draft for an overdue invoice. Drafts are never sent."""
    event = parse_event(DelinquentInvoiceEvent, body)
    d = event.data
    as_of = d.as_of or datetime.now(timezone.utc).date()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            if prior := events.find_inbound(cur, event.event_id):
                return duplicate_response(request, prior)
            summary = summarize_delinquency(cur, d.invoice_id, d.customer_id, as_of, d.draft_notice)
            result = DelinquentInvoiceResult(event_id=event.event_id, **summary)
            if not events.record(cur, event_id=event.event_id, event_type=event.event_type, direction="inbound",
                                 source=event.source, subject_id=d.invoice_id, status="PROCESSED",
                                 payload=event.model_dump(mode="json"),
                                 response=result.model_dump(mode="json", by_alias=True)):
                conn.rollback()
                return duplicate_response(request, events.find_inbound(cur, event.event_id))
    except CollectionsError as exc:
        reject(event, exc.code, exc.message, exc.status_code, d.invoice_id)
    return result


class OverdueList(BaseModel):
    as_of: date
    count: int
    invoices: list[OverdueInvoice]


@router.get("/invoices/overdue", response_model=OverdueList, responses=problem_responses(401))
def overdue_invoices(as_of: date | None = Query(None), _: bytes = Depends(require_signature)):
    """Polling source for the reconciliation workflow (signature covers the empty body)."""
    as_of = as_of or datetime.now(timezone.utc).date()
    with get_conn() as conn, conn.cursor() as cur:
        rows = list_overdue(cur, as_of)
    return OverdueList(as_of=as_of, count=len(rows), invoices=rows)


# --------------------------------------------------------------------------- n8n callbacks

@router.post("/n8n/callback", response_model=CallbackAck, responses=problem_responses(401, 422))
def workflow_callback(body: bytes = Depends(require_signature)):
    """n8n reports what a workflow run did (alert sent, approval requested, notice drafted)."""
    event = parse_event(WorkflowCallbackEvent, body)
    with get_conn() as conn, conn.cursor() as cur:
        recorded = events.record(
            cur, event_id=event.event_id, event_type=event.event_type, direction="callback", source=event.source,
            subject_id=event.data.subject_id,
            status="PROCESSED" if event.event_type == "workflow.completed" else "FAILED",
            payload=event.model_dump(mode="json"),
        )
    return CallbackAck(event_id=event.event_id, recorded=recorded, duplicate=not recorded)
