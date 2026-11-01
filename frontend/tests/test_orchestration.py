"""Offline render tests for the orchestration panels (tracker, decision card, queue board)."""

from ui import orchestration

STEPS = [("analyze_contract", "Contract analysis", "complete"), ("check_rag_compliance", "Policy verification", "complete"),
         ("calculate_impact", "Financial delta", "complete"), ("wait_for_approval", "Manager approval", "in_progress"),
         ("execute_mutation", "Provisioning", "pending")]

RUN = {
    "run_id": "r1", "request_id": "SRQ-DEMO-1", "customer_id": "CUST-0071", "state": "WAITING_FOR_APPROVAL",
    "contract": {"company_name": "Nexus <Client> 0071", "plan_tier": "ENTERPRISE", "annual_unit_price": "140.00",
                 "committed_seats": 1500},
    "impact": {"added_seats": 43, "aggregate_monthly_30d_display": "501.67", "annual_arr_impact": "6020.00"},
    "approval": {"assigned_approver_id": "EMP-0082"},
    "pipeline": {"steps": [{"key": k, "name": n, "description": "d", "status": s, "at": None} for k, n, s in STEPS],
                 "progress_pct": 60, "current_step": "Manager approval", "finished": False,
                 "eta": "Waiting on EMP-0082; expires in 72 h."},
    "explanation": {"headline": "Paused for manager approval: 30-day total over $500/month",
                    "checks": [{"label": "$500/month approval threshold", "status": "flag", "detail": "$501.67 vs $500"},
                               {"label": "Contract is active", "status": "pass", "detail": "ok"},
                               {"label": "Copilot contract review", "status": "skipped", "detail": "Not needed"}],
                    "rules_evaluated": 2, "rules_total": 3, "flags": 1, "failures": 0,
                    "decision_basis": "deterministic", "advisory_confidence": None,
                    "actions": [{"label": "Approval opened for EMP-0082", "status": "done"}]},
}


def test_tracker_shows_every_step_with_status_and_progress():
    html = orchestration.tracker(RUN)
    for name in ("Contract analysis", "Policy verification", "Financial delta", "Manager approval", "Provisioning"):
        assert name in html
    assert html.count('class="nf-step ok"') == 3 and 'class="nf-step wait"' in html
    assert 'aria-valuenow="60"' in html and "Now: Manager approval" in html and "Needs approval" in html
    assert "Nexus &lt;Client&gt; 0071" in html          # escaped, never raw HTML from data


def test_decision_card_reports_rule_coverage_not_a_probability():
    html = orchestration.decision(RUN)
    assert "2/3" in html and "Rules evaluated" in html and "no probability score" in html
    assert 'class="nf-decision wait"' in html and "Approval opened for EMP-0082" in html
    assert "%" not in html.replace("width:", "")


def test_board_rows_carry_state_priority_and_steps():
    data = {"items": [{"id": "1", "workflow": "p1_ticket_escalation", "name": "P1 escalation", "kind": "n8n workflow",
                       "subject": "TK-0139", "state": "running", "priority": "critical", "steps_done": 2,
                       "steps_total": 4, "current_step": "Copilot triage", "detail": "n8n is waiting on the copilot",
                       "started_at": "2026-10-29T10:00:00+00:00", "updated_at": "2026-10-29T10:00:00+00:00"}]}
    html = orchestration.board(data)
    assert "Running" in html and "Critical" in html and "2/4" in html and "Copilot triage: n8n is waiting" in html
    assert orchestration.board({"items": []}).startswith('<div class="nf-empty">')
