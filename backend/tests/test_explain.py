"""Pipeline + reasoning explainer: pure functions over persisted run fields (no DB, no LLM)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.agents.explain import explanation, governance_priority, pipeline

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def hist(*states):
    return [{"from": None, "to": s, "at": (NOW - timedelta(minutes=10 - i)).isoformat(), "reason": "r"}
            for i, s in enumerate(states)]


CONTRACT = {"plan_tier": "ENTERPRISE", "annual_unit_price": "140.00", "days_remaining_in_term": 161,
            "committed_seats": 1500, "tier_seat_range": [1000, 10000], "company_name": "Nexus Client 0071 LLC"}


def impact(seats=43, monthly="501.67", raw="501.6666666666666666666666667", status="PENDING_FINANCE_APPROVAL"):
    return {"added_seats": seats, "aggregate_seats_30d": seats, "aggregate_monthly_30d": raw,
            "aggregate_monthly_30d_display": monthly, "monthly_cost_impact_display": monthly,
            "threshold_monthly": "500.00", "projected_seats": 1500 + seats, "prior_requests_in_window": [],
            "annual_arr_impact": "6020.00", "formula": f"{seats} × $140.00 ÷ 12 = ${monthly}/month",
            "seat_request_status": status}


CLEAN_CLAUSES = {"corpus_indexed": True, "documents": [], "review": None}
TRACE_ALL = ["analyze_contract", "check_rag_compliance", "calculate_impact"]
APPROVAL = {"assigned_approver_id": "EMP-0082", "expires_at": (NOW + timedelta(hours=71)).isoformat()}


def statuses(p):
    return [s["status"] for s in p["steps"]]


def check_status(e, label_start):
    return next(c["status"] for c in e["checks"] if c["label"].startswith(label_start))


def test_waiting_for_approval_over_threshold():
    ctx = {"contract": CONTRACT, "clause_check": CLEAN_CLAUSES, "impact": impact(),
           "decision": {"outcome": "HITL_REQUIRED", "reasons": ["MONTHLY_IMPACT_OVER_THRESHOLD"]},
           "graph_trace": TRACE_ALL + ["wait_for_approval"]}
    h = hist("INITIATED", "ANALYZING_CONTRACT", "CALCULATING_IMPACT", "WAITING_FOR_APPROVAL")
    p = pipeline("WAITING_FOR_APPROVAL", ctx, h, APPROVAL, now=NOW)
    assert statuses(p) == ["complete", "complete", "complete", "in_progress", "pending"]
    assert p["progress_pct"] == 60 and p["current_step"] == "Manager approval" and not p["finished"]
    assert "EMP-0082" in p["eta"] and "71 h" in p["eta"]
    e = explanation("WAITING_FOR_APPROVAL", ctx, APPROVAL)
    assert e["headline"] == "Paused for manager approval: 30-day total over $500/month"
    assert check_status(e, "$500/month") == "flag" and e["flags"] == 1 and e["failures"] == 0
    assert (e["rules_evaluated"], e["rules_total"]) == (7, 8)   # copilot review not needed
    assert e["decision_basis"] == "deterministic" and e["advisory_confidence"] is None
    assert [a["label"] for a in e["actions"]] == ["Seat request recorded as PENDING_FINANCE_APPROVAL",
                                                  "Approval opened for EMP-0082"]
    assert governance_priority(ctx) == "high"


def test_auto_approved_and_executed():
    ctx = {"contract": CONTRACT, "clause_check": CLEAN_CLAUSES,
           "impact": impact(5, "58.33", "58.3333333333333333333333333", "APPROVED"),
           "decision": {"outcome": "AUTO_APPROVED", "reasons": []}, "graph_trace": TRACE_ALL + ["execute_mutation"],
           "execution": {"customer_seats": [1500, 1505], "contract_arr": ["210000.00", "210700.00"]}}
    h = hist("INITIATED", "ANALYZING_CONTRACT", "CALCULATING_IMPACT", "APPROVED", "EXECUTED")
    p = pipeline("EXECUTED", ctx, h, None, now=NOW)
    assert statuses(p) == ["complete", "complete", "complete", "skipped", "complete"]
    assert p["progress_pct"] == 100 and p["finished"] and p["eta"] is None
    e = explanation("EXECUTED", ctx, None)
    assert e["headline"] == "Approved by the agent and provisioned" and e["flags"] == 0
    assert "Provisioned 1,500 to 1,505 seats" in e["actions"][-1]["label"]
    assert governance_priority(ctx) == "normal"


@pytest.mark.parametrize("outcome,step_status,headline", [
    ("REJECTED", "rejected", "Rejected by EMP-0082: nothing provisioned"),
    ("EXPIRED", "expired", "Approval expired without a decision: nothing provisioned"),
])
def test_human_rejection_and_expiry(outcome, step_status, headline):
    ctx = {"contract": CONTRACT, "clause_check": CLEAN_CLAUSES, "impact": impact(),
           "decision": {"outcome": "HITL_REQUIRED", "reasons": ["MONTHLY_IMPACT_OVER_THRESHOLD"]},
           "human_decision": {"outcome": outcome, "by": "EMP-0082" if outcome == "REJECTED" else None,
                              "at": NOW.isoformat()},
           "graph_trace": TRACE_ALL + ["wait_for_approval"]}
    h = hist("INITIATED", "ANALYZING_CONTRACT", "CALCULATING_IMPACT", "WAITING_FOR_APPROVAL", "REJECTED")
    p = pipeline("REJECTED", ctx, h, APPROVAL, now=NOW)
    assert statuses(p) == ["complete", "complete", "complete", step_status, "skipped"]
    e = explanation("REJECTED", ctx, APPROVAL)
    assert e["headline"] == headline and "No seats or ARR changed" in [a["label"] for a in e["actions"]]


def test_rule_violation_during_analysis():
    ctx = {"decision": {"outcome": "REJECTED", "by": "agent", "code": "CUSTOMER_NOT_ACTIVE",
                        "message": "CUST-0011 is DELINQUENT"}, "graph_trace": ["analyze_contract"]}
    h = hist("INITIATED", "ANALYZING_CONTRACT", "REJECTED")
    p = pipeline("REJECTED", ctx, h, None, now=NOW)
    assert statuses(p) == ["rejected", "skipped", "skipped", "skipped", "skipped"] and p["progress_pct"] == 100
    e = explanation("REJECTED", ctx, None)
    assert e["headline"] == "Rejected by the agent: CUST-0011 is DELINQUENT"
    assert check_status(e, "Contract is active") == "pass"      # validated before account standing
    assert check_status(e, "Customer account") == "fail" and e["failures"] == 1


def test_plan_amendment_rejected_at_financial_delta():
    ctx = {"contract": {**CONTRACT, "plan_tier": "STARTER", "tier_seat_range": [50, 249]},
           "clause_check": CLEAN_CLAUSES, "impact": {**impact(200), "projected_seats": 300},
           "decision": {"outcome": "REJECTED", "by": "agent", "code": "PLAN_AMENDMENT_REQUIRED",
                        "message": "Projected 300 seats exceeds the STARTER range 50-249"},
           "graph_trace": TRACE_ALL}
    h = hist("INITIATED", "ANALYZING_CONTRACT", "CALCULATING_IMPACT", "REJECTED")
    p = pipeline("REJECTED", ctx, h, None, now=NOW)
    assert statuses(p) == ["complete", "complete", "rejected", "skipped", "skipped"]
    assert check_status(explanation("REJECTED", ctx, None), "Seats stay within") == "fail"


def test_failed_execution_marks_provisioning_failed():
    ctx = {"contract": CONTRACT, "clause_check": CLEAN_CLAUSES, "impact": impact(),
           "decision": {"outcome": "HITL_REQUIRED", "reasons": ["MONTHLY_IMPACT_OVER_THRESHOLD"]},
           "human_decision": {"outcome": "APPROVED", "by": "EMP-0082", "at": NOW.isoformat()},
           "graph_trace": TRACE_ALL + ["wait_for_approval"]}
    h = hist("INITIATED", "ANALYZING_CONTRACT", "CALCULATING_IMPACT", "WAITING_FOR_APPROVAL", "APPROVED", "FAILED")
    p = pipeline("FAILED", ctx, h, APPROVAL, now=NOW)
    assert statuses(p) == ["complete", "complete", "complete", "complete", "failed"]
    e = explanation("FAILED", ctx, APPROVAL, {"message": "contract NONRENEWING"})
    assert e["headline"].startswith("Failed safely: contract NONRENEWING")


def test_negotiated_terms_with_review_reports_advisory_confidence_only():
    clause = {"corpus_indexed": True, "documents": [{"source_file": "legal/SOW_Enterprise_Client_Sample.md"}],
              "review": {"answer": "x", "grounded": True, "confidence": "high", "citations": []}}
    ctx = {"contract": CONTRACT, "clause_check": clause, "impact": impact(10, "116.67", "116.6666666666666666666666667"),
           "decision": {"outcome": "HITL_REQUIRED", "reasons": ["NEGOTIATED_CONTRACT_TERMS"]}}
    e = explanation("WAITING_FOR_APPROVAL", ctx, APPROVAL)
    assert check_status(e, "No customer-specific") == "flag" and check_status(e, "Copilot contract review") == "pass"
    assert e["advisory_confidence"] == "high" and (e["rules_evaluated"], e["rules_total"]) == (8, 8)
    assert governance_priority(ctx) == "high"
    # nothing in the explanation pretends to be a probability
    assert "%" not in str(e)


def test_review_unavailable_is_a_flag_not_an_error():
    clause = {"corpus_indexed": True, "documents": [{"source_file": "legal/SOW.md"}],
              "review": {"question": "q", "error": "RuntimeError: All Gemini models failed"}}
    ctx = {"contract": CONTRACT, "clause_check": clause, "impact": impact(10, "116.67", "116.67"),
           "decision": {"outcome": "HITL_REQUIRED", "reasons": ["NEGOTIATED_CONTRACT_TERMS",
                                                               "CONTRACT_REVIEW_UNAVAILABLE"]}}
    e = explanation("WAITING_FOR_APPROVAL", ctx, APPROVAL)
    assert check_status(e, "Copilot contract review") == "flag" and e["advisory_confidence"] is None
    assert "copilot contract review unavailable" in e["headline"]
