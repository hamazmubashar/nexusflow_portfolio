"""Explain a governance run: its pipeline progress and the reasoning behind its decision.

Pure functions over a run's persisted fields (state, context, history, approval): no database,
no LLM. Everything shown is something the agent actually evaluated or did, so the UI's
"reasoning" can be audited against governance_runs.

Confidence: the decision itself is deterministic (rules over exact Decimal math), so we report
rule coverage, not a probability. The only model-generated confidence is the copilot's rating of
its advisory contract review, reported separately and labelled as advisory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

PIPELINE = [
    ("analyze_contract", "Contract analysis", "Contract, account standing and plan terms"),
    ("check_rag_compliance", "Policy verification", "Customer-specific MSA/SOW terms (RAG)"),
    ("calculate_impact", "Financial delta", "30-day seat total, MRR and ARR impact"),
    ("wait_for_approval", "Manager approval", "Human-in-the-loop decision"),
    ("execute_mutation", "Provisioning", "Seats and ARR updated"),
]
TERMINAL = {"EXECUTED", "REJECTED", "FAILED"}
DONE = {"complete", "rejected", "failed", "expired", "skipped"}

REASON_SHORT = {
    "MONTHLY_IMPACT_OVER_THRESHOLD": "30-day total over $500/month",
    "REQUEST_TYPE_NOT_PURCHASE": "not an explicit purchase",
    "NEGOTIATED_CONTRACT_TERMS": "customer-specific contract terms",
    "CONTRACT_REVIEW_UNAVAILABLE": "copilot contract review unavailable",
    "CONTRACT_CORPUS_UNAVAILABLE": "contract corpus not indexed",
}
CONTRACT_CODES = {"CONTRACT_NOT_FOUND", "CONTRACT_CUSTOMER_MISMATCH", "CONTRACT_NOT_ACTIVE"}


def _at(history: list[dict], *states: str, after: str | None = None) -> str | None:
    """Timestamp of the first transition into one of `states` (optionally after entering `after`)."""
    seen_after = after is None
    for h in history:
        if not seen_after and h["to"] == after:
            seen_after = True
            continue
        if seen_after and h["to"] in states:
            return h["at"]
    return None


def _fmt_money(v) -> str:
    return f"${Decimal(str(v)):,.2f}"


def pipeline(state: str, context: dict, history: list[dict], approval: dict | None,
             now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    trace = context.get("graph_trace") or []
    decision = context.get("decision") or {}
    human = context.get("human_decision") or {}
    hitl = decision.get("outcome") == "HITL_REQUIRED"
    code = decision.get("code")
    analysed_at = _at(history, "CALCULATING_IMPACT")
    calculated_at = _at(history, "WAITING_FOR_APPROVAL", "APPROVED", "REJECTED", after="CALCULATING_IMPACT")

    status: dict[str, str] = {}
    at: dict[str, str | None] = {k: None for k, _, _ in PIPELINE}

    # contract analysis + policy verification
    if state == "REJECTED" and analysed_at is None and code:      # rule violation during analysis
        status["analyze_contract"] = "rejected"
        at["analyze_contract"] = _at(history, "REJECTED")
        for k in ("check_rag_compliance", "calculate_impact", "wait_for_approval", "execute_mutation"):
            status[k] = "skipped"
    else:
        status["analyze_contract"] = "complete" if analysed_at or "analyze_contract" in trace else "in_progress"
        at["analyze_contract"] = analysed_at
        status["check_rag_compliance"] = ("complete" if analysed_at or "check_rag_compliance" in trace
                                          else "pending" if status["analyze_contract"] != "complete" else "in_progress")
        at["check_rag_compliance"] = analysed_at

        # financial delta
        if calculated_at:
            plan_rejected = state == "REJECTED" and code and not hitl and not human
            status["calculate_impact"] = "rejected" if plan_rejected else "complete"
            at["calculate_impact"] = calculated_at
        else:
            status["calculate_impact"] = "in_progress" if analysed_at else "pending"

        # manager approval
        outcome = human.get("outcome")
        if status["calculate_impact"] == "rejected":
            status["wait_for_approval"] = status["execute_mutation"] = "skipped"
        elif not hitl:
            status["wait_for_approval"] = "skipped" if calculated_at else "pending"
        elif outcome == "APPROVED":
            status["wait_for_approval"], at["wait_for_approval"] = "complete", human.get("at")
        elif outcome == "REJECTED":
            status["wait_for_approval"], at["wait_for_approval"] = "rejected", human.get("at")
        elif outcome == "EXPIRED":
            status["wait_for_approval"], at["wait_for_approval"] = "expired", human.get("at")
        elif state == "WAITING_FOR_APPROVAL":
            status["wait_for_approval"] = "in_progress"
        else:
            status["wait_for_approval"] = "pending"

        # provisioning
        if "execute_mutation" not in status:
            if state == "EXECUTED":
                status["execute_mutation"], at["execute_mutation"] = "complete", _at(history, "EXECUTED")
            elif status["wait_for_approval"] in ("rejected", "expired") or (state == "REJECTED"):
                status["execute_mutation"] = "skipped"
            elif state == "APPROVED":
                status["execute_mutation"] = "in_progress"
            else:
                status["execute_mutation"] = "pending"

    if state == "FAILED":  # the first step that didn't finish is the one that failed
        for k, _, _ in PIPELINE:
            if status.get(k) in ("in_progress", "pending"):
                status[k], at[k] = "failed", _at(history, "FAILED")
                break
        for k, _, _ in PIPELINE:
            if status.get(k) in ("in_progress", "pending"):
                status[k] = "skipped"

    steps = [{"key": k, "name": name, "description": desc, "status": status[k], "at": at[k]} for k, name, desc in PIPELINE]
    applicable = [s for s in steps if s["status"] != "skipped"]
    finished = state in TERMINAL
    done = sum(s["status"] in DONE for s in applicable)
    progress = 100 if finished else round(100 * done / max(len(applicable), 1))
    current = next((s for s in steps if s["status"] in ("in_progress", "failed")), None)

    eta = None
    if state == "WAITING_FOR_APPROVAL" and approval:
        expires = datetime.fromisoformat(str(approval["expires_at"]).replace("Z", "+00:00"))
        hours = max((expires - now).total_seconds() / 3600, 0)
        eta = (f"Waiting on {approval['assigned_approver_id']}; the remaining steps finish within seconds of a "
               f"decision. The request expires in {hours:.0f} h.")
    elif state == "APPROVED":
        eta = "Provisioning now; this completes within seconds."
    return {"steps": steps, "progress_pct": progress, "current_step": current["name"] if current else None,
            "finished": finished, "eta": eta}


def explanation(state: str, context: dict, approval: dict | None, error: dict | None = None) -> dict:
    decision = context.get("decision") or {}
    human = context.get("human_decision") or {}
    contract = context.get("contract") or {}
    impact = context.get("impact") or {}
    clause = context.get("clause_check") or {}
    execution = context.get("execution") or {}
    code = decision.get("code")
    reasons = decision.get("reasons") or []
    checks: list[dict] = []

    def check(label, status, detail):
        checks.append({"label": label, "status": status, "detail": detail})

    # 1-2: contract and account
    if code in CONTRACT_CODES:
        check("Contract is active and belongs to the customer", "fail", decision.get("message", code))
    elif contract:
        check("Contract is active and belongs to the customer", "pass",
              f"{contract.get('plan_tier', '').title()} plan, {contract.get('days_remaining_in_term', '?')} days left in term")
    elif code == "CUSTOMER_NOT_ACTIVE":  # the agent validates the contract before account standing
        check("Contract is active and belongs to the customer", "pass", "Contract checked before account standing")
    else:
        check("Contract is active and belongs to the customer", "skipped", "Not reached")
    if code == "CUSTOMER_NOT_ACTIVE":
        check("Customer account is in good standing", "fail", decision.get("message", code))
    elif contract:
        check("Customer account is in good standing", "pass", "Account ACTIVE")
    else:
        check("Customer account is in good standing", "skipped", "Not reached")

    # 3-5: the money (only once impact was calculated)
    if impact:
        lo, hi = contract.get("tier_seat_range", [None, None])
        within = code != "PLAN_AMENDMENT_REQUIRED"
        check("Seats stay within the plan's range", "pass" if within else "fail",
              f"{impact['projected_seats']:,} seats vs {contract.get('plan_tier', '').title()} range {lo:,}-{hi:,}"
              if lo is not None else f"{impact['projected_seats']:,} seats projected")
        prior = len(impact.get("prior_requests_in_window", []))
        check("Rolling 30-day seat total checked", "pass",
              f"{impact['aggregate_seats_30d']} seats in the window, including {prior} earlier request"
              f"{'' if prior == 1 else 's'}")
        over = Decimal(impact["aggregate_monthly_30d"]) > Decimal(impact["threshold_monthly"])
        check("$500/month approval threshold", "flag" if over else "pass",
              f"{_fmt_money(impact['aggregate_monthly_30d_display'])}/month vs {_fmt_money(impact['threshold_monthly'])} "
              f"({impact['formula']})")
    else:
        for label in ("Seats stay within the plan's range", "Rolling 30-day seat total checked",
                      "$500/month approval threshold"):
            check(label, "skipped", "Not reached")

    # 6: request type
    if not impact and not reasons:
        check("Request is an explicit purchase", "skipped", "Not reached")
    elif "REQUEST_TYPE_NOT_PURCHASE" in reasons:
        check("Request is an explicit purchase", "flag", "Reservation or unclear request: needs written authorisation")
    else:
        check("Request is an explicit purchase", "pass", "PURCHASE")

    # 7-8: negotiated terms + copilot review (advisory)
    review = clause.get("review")
    if not clause:
        check("No customer-specific contract terms", "skipped", "Not reached")
        check("Copilot contract review", "skipped", "Not reached")
    elif not clause.get("corpus_indexed"):
        check("No customer-specific contract terms", "flag", "Contract corpus not indexed: can't rule terms out")
        check("Copilot contract review", "skipped", "No corpus to review")
    elif clause.get("documents"):
        docs = ", ".join(d["source_file"].split("/")[-1] for d in clause["documents"])
        check("No customer-specific contract terms", "flag", f"Found {docs}")
        if not review or "error" in review:
            check("Copilot contract review", "flag", "Review unavailable: a person must read the documents")
        else:
            check("Copilot contract review", "pass" if review.get("grounded") else "flag",
                  f"{'Grounded' if review.get('grounded') else 'Not grounded'}, {review.get('confidence')} "
                  "confidence (advisory: can't approve or remove a reason)")
    else:
        check("No customer-specific contract terms", "pass", "No SOW or order form for this customer")
        check("Copilot contract review", "skipped", "Not needed")

    # headline
    if state == "EXECUTED":
        by = "the agent" if decision.get("outcome") == "AUTO_APPROVED" else human.get("by", "a manager")
        headline = f"Approved by {by} and provisioned"
    elif state == "REJECTED" and human.get("outcome") == "REJECTED":
        headline = f"Rejected by {human.get('by')}: nothing provisioned"
    elif state == "REJECTED" and human.get("outcome") == "EXPIRED":
        headline = "Approval expired without a decision: nothing provisioned"
    elif state == "REJECTED":
        headline = f"Rejected by the agent: {decision.get('message', code)}"
    elif state == "WAITING_FOR_APPROVAL":
        headline = "Paused for manager approval: " + ", ".join(REASON_SHORT.get(r, r) for r in reasons)
    elif state == "FAILED":
        headline = f"Failed safely: {(error or {}).get('message', 'unexpected error')}; nothing half-applied"
    else:
        headline = f"In progress: {state.replace('_', ' ').lower()}"

    # actions the agent actually took
    actions = []
    if impact:
        actions.append({"label": f"Seat request recorded as {impact['seat_request_status']}", "status": "done"})
    if approval:
        actions.append({"label": f"Approval opened for {approval['assigned_approver_id']}", "status": "done"})
    if human.get("outcome") in ("APPROVED", "REJECTED"):
        actions.append({"label": f"{human['outcome'].title()} by {human.get('by')}", "status": "done"})
    if human.get("outcome") == "EXPIRED":
        actions.append({"label": "Approval expired", "status": "done"})
    if execution:
        actions.append({"label": f"Provisioned {execution['customer_seats'][0]:,} to {execution['customer_seats'][1]:,} "
                                 f"seats; ARR ${execution['contract_arr'][0]} to ${execution['contract_arr'][1]}",
                        "status": "done"})
    if state == "REJECTED" and not execution:
        actions.append({"label": "No seats or ARR changed", "status": "done"})

    evaluated = [c for c in checks if c["status"] != "skipped"]
    return {
        "headline": headline,
        "checks": checks,
        "rules_evaluated": len(evaluated),
        "rules_total": len(checks),
        "flags": sum(c["status"] == "flag" for c in checks),
        "failures": sum(c["status"] == "fail" for c in checks),
        "decision_basis": "deterministic",
        "advisory_confidence": review.get("confidence") if review and "error" not in review else None,
        "actions": actions,
    }


def governance_priority(context: dict) -> str:
    """Business priority: money over the threshold or negotiated terms make a request high priority."""
    reasons = (context.get("decision") or {}).get("reasons") or []
    impact = context.get("impact") or {}
    over = impact and Decimal(impact["aggregate_monthly_30d"]) > Decimal(impact["threshold_monthly"])
    return "high" if over or {"NEGOTIATED_CONTRACT_TERMS", "CONTRACT_REVIEW_UNAVAILABLE"} & set(reasons) else "normal"


def summary(view) -> dict:
    """Compact board row for a GovernanceRunView."""
    steps = [s for s in view.pipeline.steps if s.status != "skipped"]
    impact = view.impact or {}
    return {
        "run_id": view.run_id, "request_id": view.request_id, "customer_id": view.customer_id,
        "contract_id": view.contract_id, "state": view.state,
        "priority": governance_priority({"decision": view.decision, "impact": view.impact}),
        "headline": view.explanation.headline, "progress_pct": view.pipeline.progress_pct,
        "current_step": view.pipeline.current_step,
        "steps_done": sum(s.status in DONE for s in steps), "steps_total": len(steps),
        "monthly_impact": impact.get("aggregate_monthly_30d_display"),
        "started_at": view.history[0]["at"], "updated_at": view.history[-1]["at"],
    }
