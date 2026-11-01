"""Commercial Governance & Compliance agent: seat expansions under SOP-02 + MSA/SOW, with HITL.

A deterministic, persisted state machine (no LLM decides anything), orchestrated as a LangGraph
StateGraph in langgraph_governance.py; this module holds the node logic and persistence:

    INITIATED -> ANALYZING_CONTRACT -> CALCULATING_IMPACT -+-> APPROVED -> EXECUTED
                                                           +-> WAITING_FOR_APPROVAL -> APPROVED -> EXECUTED
                                                           |                        \\-> REJECTED (human or expiry)
                                                           \\-> REJECTED (rule violation)
    any non-terminal state -> FAILED (unexpected error; nothing half-applied)

Every transition is committed to governance_runs before the next step runs, so a run
can be inspected or resumed after a restart. Financial safety rules:
  * Money is Decimal end to end; the $500 test is `aggregate_monthly_30d > 500` on exact values.
  * Seats/ARR change only in EXECUTED, in one transaction that also flips the run state,
    with the seat_request, contract and customer rows locked, and never twice for one request.
  * A human is required whenever any HITL reason applies. The Stage 2 copilot is consulted
    when customer-specific contract documents exist, but its answer is advisory: it is
    attached to the approval card and can never remove a HITL reason or approve anything.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Callable
from uuid import uuid4

import psycopg2
import psycopg2.errors

from app.core.config import Settings, get_settings
from app.core.db import get_conn
from app.schemas.webhooks import SeatRequestData
from app.services.agents import approval_tokens
from app.services.n8n.dispatcher import APPROVAL_EVENTS as APPROVAL_EVENTS_PATH
from app.services.seats import ANNUAL_UNIT_PRICE, SEAT_RANGE, SeatRequestError, evaluate_seat_request, payload_hash

log = logging.getLogger("nexusflow.governance")

CENTS = Decimal("0.01")


class State(StrEnum):
    INITIATED = "INITIATED"
    ANALYZING_CONTRACT = "ANALYZING_CONTRACT"
    CALCULATING_IMPACT = "CALCULATING_IMPACT"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


TRANSITIONS: dict[State, set[State]] = {
    State.INITIATED: {State.ANALYZING_CONTRACT, State.FAILED},
    State.ANALYZING_CONTRACT: {State.CALCULATING_IMPACT, State.REJECTED, State.FAILED},
    State.CALCULATING_IMPACT: {State.WAITING_FOR_APPROVAL, State.APPROVED, State.REJECTED, State.FAILED},
    State.WAITING_FOR_APPROVAL: {State.APPROVED, State.REJECTED, State.FAILED},
    State.APPROVED: {State.EXECUTED, State.FAILED},
    State.REJECTED: set(),
    State.EXECUTED: set(),
    State.FAILED: set(),
}
TERMINAL = {s for s, nxt in TRANSITIONS.items() if not nxt}

# Why a human must decide (any one is sufficient)
REASON_TEXT = {
    "MONTHLY_IMPACT_OVER_THRESHOLD": "Aggregate 30-day monthly equivalent exceeds $500.00 (SOP-02 §1)",
    "REQUEST_TYPE_NOT_PURCHASE": "Request is not an explicit purchase; written authorization/clarification needed (SOP-02 §5)",
    "NEGOTIATED_CONTRACT_TERMS": "Customer-specific contract documents exist (SOW/order form); terms need human review",
    "CONTRACT_REVIEW_UNAVAILABLE": "Copilot review of the customer's contract documents failed",
    "CONTRACT_CORPUS_UNAVAILABLE": "Contract corpus is not indexed, so negotiated terms can't be ruled out",
}


class GovernanceError(Exception):
    """Maps to an HTTP error; may carry notifications that must still be sent (e.g. expiry)."""

    def __init__(self, status_code: int, code: str, message: str, notifications: list | None = None,
                 run: "RunView | None" = None):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message
        self.notifications = notifications or []
        self.run = run


class InvalidTransition(Exception):
    pass


@dataclass
class RunView:
    run_id: str
    request_id: str
    customer_id: str
    contract_id: str
    state: State
    context: dict
    history: list[dict]
    error: dict | None = None
    approval: dict | None = None
    duplicate: bool = False
    # (workflow_path, payload) pairs for the caller to dispatch to n8n after commit
    notifications: list[tuple[str, dict]] = field(default_factory=list)


ClauseReviewer = Callable[[str], Any]  # question -> CopilotResponse-like (answer, grounded, citations, ...)


def money(v: Decimal) -> str:
    return str(Decimal(v).quantize(CENTS, rounding=ROUND_HALF_UP))


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))


class GovernanceAgent:
    def __init__(self, clause_reviewer: ClauseReviewer | None = None, settings: Settings | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sweep_scope: str | None = None):
        self.settings = settings or get_settings()
        self.reviewer = clause_reviewer
        self.now = now
        # SQL LIKE pattern limiting expire_stale() to matching request_ids. Production leaves it None (sweep
        # everything); tests that move the clock forward set it so they can't expire real pending approvals.
        self.sweep_scope = sweep_scope

    # ------------------------------------------------------------------ persistence

    def _transition(self, cur, run: RunView, to: State, reason: str, **context) -> None:
        if to not in TRANSITIONS[run.state]:
            raise InvalidTransition(f"{run.state} -> {to} is not allowed")
        run.history.append({"from": run.state.value, "to": to.value, "at": self.now().isoformat(), "reason": reason})
        run.state = to
        run.context.update(_jsonable(context))
        cur.execute(
            "UPDATE governance_runs SET state = %s, context = %s::jsonb, history = %s::jsonb, error = %s::jsonb, "
            "updated_at = now() WHERE run_id = %s",
            (to.value, json.dumps(run.context), json.dumps(run.history),
             json.dumps(run.error) if run.error else None, run.run_id),
        )

    def _step(self, run: RunView, fn) -> None:
        """Run fn(cur) in its own transaction; on an unexpected error record FAILED and re-raise."""
        try:
            with get_conn() as conn, conn.cursor() as cur:
                fn(cur)
        except (GovernanceError, InvalidTransition):
            raise
        except Exception as exc:
            log.exception("governance run %s failed in %s", run.run_id, run.state)
            self._fail(run, exc)
            raise GovernanceError(500, "AGENT_FAILED", f"{type(exc).__name__}: {exc}", run=run) from exc

    def _fail(self, run: RunView, exc: Exception) -> None:
        run.error = {"type": type(exc).__name__, "message": str(exc), "state": run.state.value}
        with get_conn() as conn, conn.cursor() as cur:
            # reload: the failed transaction rolled back, so the persisted state is authoritative
            cur.execute("SELECT state, history, context FROM governance_runs WHERE run_id = %s", (run.run_id,))
            row = cur.fetchone()
            run.state, run.history, run.context = State(row["state"]), row["history"], row["context"]
            if run.state not in TERMINAL:
                self._transition(cur, run, State.FAILED, f"{type(exc).__name__}: {exc}")

    def load(self, run_id: str) -> RunView:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM governance_runs WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise GovernanceError(404, "RUN_NOT_FOUND", f"governance run {run_id} does not exist")
            return self._view(cur, row)

    def _view(self, cur, row: dict, duplicate: bool = False) -> RunView:
        cur.execute("SELECT * FROM approval_requests WHERE run_id = %s", (row["run_id"],))
        approval = cur.fetchone()
        return RunView(run_id=str(row["run_id"]), request_id=row["request_id"], customer_id=row["customer_id"],
                       contract_id=row["contract_id"], state=State(row["state"]), context=row["context"],
                       history=row["history"], error=row["error"],
                       approval=public_approval(approval) if approval else None, duplicate=duplicate)

    # ------------------------------------------------------------------ entry point

    def start(self, data: SeatRequestData, source: str = "api") -> RunView:
        input_json = data.model_dump(mode="json")
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM governance_runs WHERE request_id = %s", (data.request_id,))
            existing = cur.fetchone()
            if existing:
                if payload_hash(existing["input"]) != payload_hash(input_json):
                    raise GovernanceError(409, "REQUEST_ID_REUSED",
                                          f"{data.request_id} was already submitted with different details")
                return self._view(cur, existing, duplicate=True)
            cur.execute("SELECT 1 FROM customers WHERE customer_id = %s", (data.customer_id,))
            if cur.fetchone() is None:
                raise GovernanceError(404, "CUSTOMER_NOT_FOUND", f"{data.customer_id} does not exist")
            run_id = str(uuid4())
            history = [{"from": None, "to": State.INITIATED.value, "at": self.now().isoformat(),
                        "reason": f"seat request received from {source}"}]
            try:
                cur.execute(
                    "INSERT INTO governance_runs (run_id, request_id, customer_id, contract_id, state, input, history) "
                    "VALUES (%s, %s, %s, %s, 'INITIATED', %s::jsonb, %s::jsonb)",
                    (run_id, data.request_id, data.customer_id, data.contract_id, json.dumps(input_json),
                     json.dumps(history)),
                )
            except psycopg2.errors.UniqueViolation:  # concurrent submit of the same request_id
                conn.rollback()
                cur.execute("SELECT * FROM governance_runs WHERE request_id = %s", (data.request_id,))
                return self._view(cur, cur.fetchone(), duplicate=True)

        run = RunView(run_id=run_id, request_id=data.request_id, customer_id=data.customer_id,
                      contract_id=data.contract_id, state=State.INITIATED, context={}, history=history)
        from app.services.agents.langgraph_governance import run_graph  # graph imports this module

        run_graph(self, run, data=data)
        return run

    # ------------------------------------------------------------------ ANALYZING_CONTRACT

    def _analyze_contract(self, cur, run: RunView, data: SeatRequestData) -> None:
        self._transition(cur, run, State.ANALYZING_CONTRACT, "loading contract terms and negotiated clauses",
                         engine=run.context.get("engine", "native"))
        cur.execute(
            """SELECT k.contract_id, k.customer_id, k.plan_tier, k.status AS contract_status, k.start_date, k.end_date,
                      k.annual_recurring_revenue, k.auto_renew, c.company_name, c.tier, c.status AS customer_status,
                      c.total_seats
               FROM contracts k JOIN customers c ON c.customer_id = k.customer_id WHERE k.contract_id = %s""",
            (data.contract_id,),
        )
        k = cur.fetchone()
        violation = None
        if k is None:
            violation = ("CONTRACT_NOT_FOUND", f"{data.contract_id} does not exist")
        elif k["customer_id"] != data.customer_id:
            violation = ("CONTRACT_CUSTOMER_MISMATCH", f"{data.contract_id} belongs to {k['customer_id']}")
        elif k["contract_status"] != "ACTIVE":
            violation = ("CONTRACT_NOT_ACTIVE", f"{data.contract_id} is {k['contract_status']}")
        elif k["customer_status"] != "ACTIVE":
            violation = ("CUSTOMER_NOT_ACTIVE",
                         f"{data.customer_id} is {k['customer_status']}; Finance must clear the account first")
        if violation:
            self._transition(cur, run, State.REJECTED, violation[1],
                             decision={"outcome": "REJECTED", "by": "agent", "code": violation[0],
                                       "message": violation[1]})
            return

        as_of = data.requested_at.date()
        contract = {
            "plan_tier": k["plan_tier"], "annual_unit_price": money(ANNUAL_UNIT_PRICE[k["plan_tier"]]),
            "contract_status": k["contract_status"], "term": [k["start_date"], k["end_date"]],
            "days_remaining_in_term": (k["end_date"] - as_of).days, "auto_renew": k["auto_renew"],
            "annual_recurring_revenue": money(k["annual_recurring_revenue"]), "committed_seats": k["total_seats"],
            "customer_tier": k["tier"], "company_name": k["company_name"],
            "tier_seat_range": SEAT_RANGE[k["plan_tier"]],
        }
        run.context["contract"] = _jsonable(contract)  # persisted with the next transition

    # ------------------------------------------------------------------ RAG compliance (MSA/SOW)

    def _find_contract_documents(self, cur, data: SeatRequestData) -> dict:
        """Customer-specific legal documents, found deterministically via chunk metadata (no LLM, no network)."""
        cur.execute("SELECT to_regclass('document_chunks') IS NOT NULL AS ok")
        indexed = cur.fetchone()["ok"]
        if indexed:
            cur.execute("SELECT count(*) AS n FROM document_chunks WHERE doc_type = 'legal'")
            indexed = cur.fetchone()["n"] > 0
        if not indexed:
            return {"corpus_indexed": False, "documents": [], "review": None}
        cur.execute(
            """SELECT DISTINCT ON (source_file) source_file, doc_id, title FROM document_chunks
               WHERE doc_type = 'legal' AND metadata->'doc_entities' ?| %s ORDER BY source_file""",
            ([data.customer_id, data.contract_id],),
        )
        return {"corpus_indexed": True, "documents": _jsonable(cur.fetchall()), "review": None}

    def _review_contract_documents(self, run: RunView, data: SeatRequestData, result: dict) -> dict:
        """Advisory copilot review. Runs outside any DB transaction: a Gemini call can take a minute."""
        k = run.context["contract"]
        if result["documents"] and self.reviewer is not None:
            question = (
                f"For {k['company_name']} ({data.customer_id}, contract {data.contract_id}, {k['plan_tier']} plan), "
                f"do the customer's SOW or contract documents contain negotiated terms that change seat pricing, "
                f"seat-expansion approval, change control or amendment requirements for adding "
                f"{data.added_seats} seats? Quote the exact terms."
            )
            try:
                resp = self.reviewer(question)
                result["review"] = {
                    "question": question, "answer": resp.answer, "grounded": resp.grounded,
                    "insufficient_context": resp.insufficient_context, "confidence": resp.confidence,
                    "model": resp.model,
                    "citations": [{"doc_id": c.doc_id, "section": c.section, "quote": c.quote,
                                   "quote_verified": c.quote_verified} for c in resp.citations],
                }
            except Exception as exc:  # advisory only: a failed review forces a human, never blocks the run
                log.warning("contract clause review failed: %s", exc)
                result["review"] = {"question": question, "error": f"{type(exc).__name__}: {exc}"}
        return result

    def _record_compliance(self, cur, run: RunView, clause_check: dict) -> None:
        self._transition(cur, run, State.CALCULATING_IMPACT, "contract and negotiated clauses analysed",
                         contract=run.context["contract"], clause_check=clause_check)

    # ------------------------------------------------------------------ CALCULATING_IMPACT

    def _calculate_impact(self, cur, run: RunView, data: SeatRequestData) -> dict | None:
        """Deterministic SOP-02 math + HITL reasons. Returns {impact, reasons, price} when a human is needed."""
        try:
            ev = evaluate_seat_request(cur, data)  # validates, locks the customer, aggregates 30 days, records
        except SeatRequestError as exc:
            self._transition(cur, run, State.REJECTED, exc.message,
                             decision={"outcome": "REJECTED", "by": "agent", "code": exc.code, "message": exc.message})
            return
        if ev["duplicate"]:  # recorded earlier (e.g. via /webhooks/seats/request): only pending ones may proceed
            cur.execute("SELECT status FROM seat_requests WHERE request_id = %s", (data.request_id,))
            current = cur.fetchone()["status"]
            if not current.startswith("PENDING_"):
                msg = f"{data.request_id} is already {current}; it can't be re-evaluated"
                self._transition(cur, run, State.REJECTED, msg, decision={
                    "outcome": "REJECTED", "by": "agent", "code": "SEAT_REQUEST_ALREADY_FINAL", "message": msg})
                return
        calc = ev["calculation"]
        price = Decimal(str(calc["annual_unit_price"]))
        arr_before = Decimal(run.context["contract"]["annual_recurring_revenue"])
        monthly = Decimal(str(calc["monthly_equivalent"]))
        aggregate = Decimal(str(calc["aggregate_monthly_30d"]))
        arr_delta = (price * data.added_seats).quantize(CENTS)
        impact = {
            "added_seats": data.added_seats,
            "monthly_cost_impact": str(monthly), "monthly_cost_impact_display": money(monthly),
            "aggregate_seats_30d": calc["aggregate_seats_30d"],
            "aggregate_monthly_30d": str(aggregate), "aggregate_monthly_30d_display": money(aggregate),
            "prior_requests_in_window": calc["prior_requests_in_window"],
            "threshold_monthly": money(Decimal(str(calc["threshold_monthly"]))),
            "annual_arr_impact": money(arr_delta),
            "arr_before": money(arr_before), "arr_after": money(arr_before + arr_delta),
            "mrr_before": money(arr_before / 12), "mrr_after": money((arr_before + arr_delta) / 12),
            "projected_seats": calc["projected_seats"],
            "formula": f"{data.added_seats} × ${money(price)} ÷ 12 = ${money(monthly)}/month",
            "seat_request_status": ev["status"],
        }

        if ev["requires_plan_amendment"]:
            lo, hi = run.context["contract"]["tier_seat_range"]
            msg = f"Projected {calc['projected_seats']} seats exceeds the {ev['plan_tier']} range {lo}-{hi}: a signed plan amendment is required first"
            self._set_seat_status(cur, data.request_id, "REJECTED")
            self._transition(cur, run, State.REJECTED, msg, impact=impact,
                             decision={"outcome": "REJECTED", "by": "agent", "code": "PLAN_AMENDMENT_REQUIRED",
                                       "message": msg})
            return

        reasons = []
        if ev["requires_finance_approval"]:
            reasons.append("MONTHLY_IMPACT_OVER_THRESHOLD")
        if ev["requires_clarification"]:
            reasons.append("REQUEST_TYPE_NOT_PURCHASE")
        clause = run.context["clause_check"]
        if not clause["corpus_indexed"]:
            reasons.append("CONTRACT_CORPUS_UNAVAILABLE")
        elif clause["documents"]:
            reasons.append("NEGOTIATED_CONTRACT_TERMS")
            if clause["review"] is None or "error" in clause["review"]:
                reasons.append("CONTRACT_REVIEW_UNAVAILABLE")

        if not reasons:
            self._set_seat_status(cur, data.request_id, "APPROVED")
            self._transition(cur, run, State.APPROVED, "within $500/month threshold; no HITL conditions",
                             impact=impact, decision={"outcome": "AUTO_APPROVED", "by": "agent", "reasons": []})
            return
        return {"impact": impact, "reasons": reasons, "price": price}

    def _set_seat_status(self, cur, request_id: str, status: str) -> None:
        cur.execute("UPDATE seat_requests SET status = %s WHERE request_id = %s", (status, request_id))

    # ------------------------------------------------------------------ WAITING_FOR_APPROVAL

    def _open_approval(self, cur, run: RunView, data: SeatRequestData, impact: dict, reasons: list[str],
                       price: Decimal) -> None:
        cur.execute(
            "SELECT employee_id, first_name || ' ' || last_name AS name, role, email FROM employees "
            "WHERE role = %s AND status = 'ACTIVE' ORDER BY employee_id LIMIT 1",
            (self.settings.governance_approver_role,),
        )
        approver = cur.fetchone()
        if approver is None:
            raise RuntimeError(f"no ACTIVE employee with role {self.settings.governance_approver_role!r}")
        approval_id = str(uuid4())
        expires_at = self.now() + timedelta(hours=self.settings.approval_token_ttl_hours)
        token = approval_tokens.issue(approval_id, data.request_id, approver["employee_id"], expires_at)
        contract = run.context["contract"]
        card = {
            "title": f"Seat expansion approval: {contract['company_name']} (+{data.added_seats} seats)",
            "summary": f"{impact['formula']}; 30-day aggregate ${impact['aggregate_monthly_30d_display']}/month "
                       f"vs ${impact['threshold_monthly']} threshold. ARR ${impact['arr_before']} -> ${impact['arr_after']}.",
            "facts": [
                {"label": "Request", "value": data.request_id},
                {"label": "Customer", "value": f"{contract['company_name']} ({data.customer_id})"},
                {"label": "Contract", "value": f"{data.contract_id} · {contract['plan_tier']} · ${contract['annual_unit_price']}/seat/yr"},
                {"label": "Seats", "value": f"{contract['committed_seats']} -> {contract['committed_seats'] + data.added_seats}"},
                {"label": "Monthly impact", "value": f"${impact['monthly_cost_impact_display']} (30-day aggregate ${impact['aggregate_monthly_30d_display']})"},
                {"label": "ARR impact", "value": f"+${impact['annual_arr_impact']}"},
                {"label": "Requested by", "value": f"{data.requester_email} ({data.request_type})"},
                {"label": "Expires", "value": expires_at.isoformat()},
            ],
            "reasons": [{"code": r, "text": REASON_TEXT[r]} for r in reasons],
            "contract_review": run.context["clause_check"].get("review"),
            "contract_documents": [d["source_file"] for d in run.context["clause_check"]["documents"]],
            "approver": approver,
        }
        cur.execute(
            """INSERT INTO approval_requests (approval_id, run_id, request_id, customer_id, contract_id, proposed_seats,
                   plan_tier, annual_unit_price, monthly_cost_impact, aggregate_monthly_30d, annual_arr_impact, reasons,
                   assigned_approver_id, token_hash, expires_at, card)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb)""",
            (approval_id, run.run_id, data.request_id, data.customer_id, data.contract_id, data.added_seats,
             contract["plan_tier"], price, Decimal(impact["monthly_cost_impact"]),
             Decimal(impact["aggregate_monthly_30d"]), Decimal(impact["annual_arr_impact"]), json.dumps(reasons),
             approver["employee_id"], approval_tokens.token_hash(token), expires_at, json.dumps(_jsonable(card))),
        )
        self._transition(cur, run, State.WAITING_FOR_APPROVAL, "; ".join(REASON_TEXT[r] for r in reasons),
                         impact=impact, decision={"outcome": "HITL_REQUIRED", "by": "agent", "reasons": reasons},
                         approval_id=approval_id)
        cur.execute("SELECT * FROM approval_requests WHERE approval_id = %s", (approval_id,))
        run.approval = public_approval(cur.fetchone())

        callback = f"{self.settings.api_public_base_url.rstrip('/')}/api/v1/approvals/{token}/action"
        run.notifications.append((APPROVAL_EVENTS_PATH, self._event("approval.requested", {
            "approval_id": approval_id, "run_id": run.run_id, "request_id": data.request_id,
            "customer_id": data.customer_id, "contract_id": data.contract_id, "state": run.state.value,
            "approval_token": token, "expires_at": expires_at.isoformat(),
            "callback": {"method": "POST", "url": callback,
                         "body": {"action": "APPROVE | REJECT", "approver_id": approver["employee_id"],
                                  "comment": "optional for APPROVE, required for REJECT",
                                  "approver_signature": "optional typed-name attestation"}},
            "card": _jsonable(card),
        })))

    # ------------------------------------------------------------------ human decision

    def decide(self, token: str, action: str, approver_id: str, comment: str | None = None,
               approver_signature: str | None = None) -> RunView:
        now = self.now()
        try:
            claims = approval_tokens.verify(token, now=now)
        except approval_tokens.TokenError as exc:
            if exc.code == "TOKEN_EXPIRED" and exc.claims:
                notes = self._expire(exc.claims.approval_id)
                raise GovernanceError(410, exc.code, exc.message, notifications=notes) from None
            raise GovernanceError(401, exc.code, exc.message) from None
        if approver_id != claims.approver_id:
            raise GovernanceError(403, "APPROVER_MISMATCH", f"this approval is assigned to {claims.approver_id}")
        if action == "REJECT" and not (comment and comment.strip()):
            raise GovernanceError(422, "COMMENT_REQUIRED", "a rejection needs a comment for the audit trail")

        with get_conn() as conn, conn.cursor() as cur:
            if claims.kind == "session":  # short-lived, minted for the signed-in assigned approver
                cur.execute("SELECT * FROM approval_requests WHERE approval_id = %s FOR UPDATE", (claims.approval_id,))
            else:
                cur.execute("SELECT * FROM approval_requests WHERE token_hash = %s FOR UPDATE",
                            (approval_tokens.token_hash(token),))
            ap = cur.fetchone()
            if (ap is None or str(ap["approval_id"]) != claims.approval_id
                    or ap["request_id"] != claims.request_id or ap["assigned_approver_id"] != claims.approver_id):
                raise GovernanceError(404, "TOKEN_NOT_RECOGNIZED", "no approval matches this token")
            if ap["status"] != "PENDING":
                raise GovernanceError(409, "ALREADY_DECIDED",
                                      f"approval is already {ap['status']} (decided {ap['decided_at']})")
            if ap["expires_at"] <= now:  # DB expiry is authoritative even if the token claim drifted
                conn.rollback()
                notes = self._expire(str(ap["approval_id"]))
                raise GovernanceError(410, "TOKEN_EXPIRED", "approval request has expired", notifications=notes)
            cur.execute("SELECT * FROM governance_runs WHERE run_id = %s FOR UPDATE", (ap["run_id"],))
            run = self._view(cur, cur.fetchone())
            status = "APPROVED" if action == "APPROVE" else "REJECTED"
            cur.execute(
                "UPDATE approval_requests SET status = %s, decided_at = %s, decided_by = %s, decision_comment = %s, "
                "approver_signature = %s WHERE approval_id = %s AND status = 'PENDING'",
                (status, now, approver_id, comment, approver_signature, ap["approval_id"]),
            )
            if cur.rowcount != 1:  # belt and braces: the row lock should make this impossible
                raise GovernanceError(409, "ALREADY_DECIDED", "approval was decided concurrently")
            self._set_seat_status(cur, run.request_id, status)
            decision = {"outcome": status, "by": approver_id, "at": now.isoformat(), "comment": comment,
                        "approver_signature": approver_signature}
            self._transition(cur, run, State(status), f"{status.lower()} by {approver_id}", human_decision=decision)

        if run.state == State.APPROVED:
            try:
                from app.services.agents.langgraph_governance import run_graph

                run_graph(self, run, approved_by=approver_id)  # resumes at execute_mutation
            except GovernanceError:
                pass  # run is FAILED and recorded; still notify below
        run = self._reload_into(run)
        run.notifications.append((APPROVAL_EVENTS_PATH, self._finalized_event(run)))
        return run

    SESSION_TOKEN_TTL = timedelta(minutes=10)

    def issue_session_token(self, approval_id: str, approver_id: str) -> dict:
        """Mint a <=10 min decision token for the assigned approver (caller has verified their JWT)."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM approval_requests WHERE approval_id = %s", (approval_id,))
            ap = cur.fetchone()
        if ap is None:
            raise GovernanceError(404, "APPROVAL_NOT_FOUND", f"approval {approval_id} does not exist")
        if ap["assigned_approver_id"] != approver_id:
            raise GovernanceError(403, "NOT_ASSIGNED_APPROVER", f"this approval is assigned to {ap['assigned_approver_id']}")
        if ap["status"] != "PENDING":
            raise GovernanceError(409, "ALREADY_DECIDED", f"approval is already {ap['status']}")
        now = self.now()
        if ap["expires_at"] <= now:
            raise GovernanceError(410, "TOKEN_EXPIRED", "approval request has expired",
                                  notifications=self._expire(str(ap["approval_id"])))
        expires = min(now + self.SESSION_TOKEN_TTL, ap["expires_at"])
        token = approval_tokens.issue(str(ap["approval_id"]), ap["request_id"], approver_id, expires, kind="session")
        return {"approval_id": str(ap["approval_id"]), "token": token, "expires_at": expires.isoformat(),
                "method": "POST",
                "action_url": f"{self.settings.api_public_base_url.rstrip('/')}/api/v1/approvals/{token}/action"}

    def _reload_into(self, run: RunView) -> RunView:
        fresh = self.load(run.run_id)
        fresh.notifications = run.notifications
        return fresh

    def _expire(self, approval_id: str) -> list:
        """Mark one PENDING approval EXPIRED (idempotent) and reject its run."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE approval_requests SET status = 'EXPIRED' WHERE approval_id = %s AND status = 'PENDING' "
                        "RETURNING run_id, request_id", (approval_id,))
            row = cur.fetchone()
            if row is None:
                return []
            self._set_seat_status(cur, row["request_id"], "EXPIRED")
            cur.execute("SELECT * FROM governance_runs WHERE run_id = %s FOR UPDATE", (row["run_id"],))
            run = self._view(cur, cur.fetchone())
            self._transition(cur, run, State.REJECTED, "approval request expired without a decision",
                             human_decision={"outcome": "EXPIRED", "by": None, "at": self.now().isoformat()})
        run = self.load(run.run_id)
        return [(APPROVAL_EVENTS_PATH, self._finalized_event(run))]

    def expire_stale(self) -> list:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT approval_id FROM approval_requests WHERE status = 'PENDING' AND expires_at <= %s "
                        "AND (%s::text IS NULL OR request_id LIKE %s)", (self.now(), self.sweep_scope, self.sweep_scope))
            ids = [str(r["approval_id"]) for r in cur.fetchall()]
        return [n for aid in ids for n in self._expire(aid)]

    # ------------------------------------------------------------------ EXECUTED

    def _execute(self, cur, run: RunView, approved_by: str) -> None:
        """The only place seats/ARR change. Same transaction as the EXECUTED transition."""
        cur.execute("SELECT * FROM seat_requests WHERE request_id = %s FOR UPDATE", (run.request_id,))
        sr = cur.fetchone()
        if sr["status"] == "ACTIVATED":
            raise RuntimeError(f"{run.request_id} is already ACTIVATED; refusing to provision twice")
        if sr["status"] != "APPROVED":
            raise RuntimeError(f"{run.request_id} is {sr['status']}, not APPROVED")
        cur.execute("SELECT * FROM contracts WHERE contract_id = %s FOR UPDATE", (sr["contract_id"],))
        contract = cur.fetchone()
        cur.execute("SELECT * FROM customers WHERE customer_id = %s FOR UPDATE", (sr["customer_id"],))
        customer = cur.fetchone()
        if contract["status"] != "ACTIVE" or customer["status"] != "ACTIVE":
            raise RuntimeError(f"contract {contract['status']} / customer {customer['status']} changed since analysis")
        lo, hi = SEAT_RANGE[contract["plan_tier"]]
        seats_after = customer["total_seats"] + sr["added_seats"]
        if seats_after > hi:
            raise RuntimeError(f"{seats_after} seats would exceed the {contract['plan_tier']} range {lo}-{hi}")

        arr_delta = (Decimal(sr["annual_unit_price"]) * sr["added_seats"]).quantize(CENTS)
        arr_after = contract["annual_recurring_revenue"] + arr_delta
        cur.execute("UPDATE customers SET total_seats = %s WHERE customer_id = %s", (seats_after, customer["customer_id"]))
        cur.execute("UPDATE contracts SET annual_recurring_revenue = %s WHERE contract_id = %s",
                    (arr_after, contract["contract_id"]))
        self._set_seat_status(cur, run.request_id, "ACTIVATED")
        execution = {
            "executed_at": self.now().isoformat(), "approved_by": approved_by,
            "customer_seats": [customer["total_seats"], seats_after],
            "contract_arr": [money(contract["annual_recurring_revenue"]), money(arr_after)],
            "arr_delta": money(arr_delta), "mrr_delta": money(arr_delta / 12),
            "note": "Seats co-terminate with the existing contract; first-year invoice proration is not generated here (SOP-02 §3.5).",
        }
        self._transition(cur, run, State.EXECUTED, f"provisioned {sr['added_seats']} seats", execution=execution)

    # ------------------------------------------------------------------ n8n events

    def _event(self, event_type: str, data: dict) -> dict:
        return {"event_id": str(uuid4()), "event_type": event_type, "occurred_at": self.now().isoformat(),
                "source": "nexusflow-api", "data": data}

    def _finalized_event(self, run: RunView) -> dict:
        ap = run.approval or {}
        return self._event("approval.finalized", {
            "approval_id": ap.get("approval_id"), "run_id": run.run_id, "request_id": run.request_id,
            "customer_id": run.customer_id, "contract_id": run.contract_id,
            "decision": ap.get("status"), "decided_by": ap.get("decided_by"), "comment": ap.get("decision_comment"),
            "final_state": run.state.value, "execution": run.context.get("execution"), "error": run.error,
        })


def public_approval(row: dict) -> dict:
    """Approval row safe to return to operators: never includes the token or its hash."""
    return _jsonable({
        "approval_id": row["approval_id"], "run_id": row["run_id"], "request_id": row["request_id"],
        "customer_id": row["customer_id"], "contract_id": row["contract_id"], "plan_tier": row["plan_tier"],
        "proposed_seats": row["proposed_seats"],
        "monthly_cost_impact": money(row["monthly_cost_impact"]),
        "aggregate_monthly_30d": money(row["aggregate_monthly_30d"]),
        "annual_arr_impact": money(row["annual_arr_impact"]),
        "reasons": row["reasons"], "status": row["status"], "assigned_approver_id": row["assigned_approver_id"],
        "expires_at": row["expires_at"], "created_at": row["created_at"], "decided_at": row["decided_at"],
        "decided_by": row["decided_by"], "decision_comment": row["decision_comment"], "card": row["card"],
    })
