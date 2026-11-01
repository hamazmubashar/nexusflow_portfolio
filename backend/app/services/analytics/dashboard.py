"""Mission-control KPIs for the executive dashboard (Stage 6 UI) and /api/v1/metrics.

Everything is computed live from the relational tables plus the automation ledgers; nothing
is cached or hard-coded except the $12.4M ARR baseline the snapshot was generated against
(reported next to the live figure, so drift from approved expansions is visible).

SLA compliance applies the same SOP-01 clocks as the escalation webhook (app.services.sla),
using each ticket's recorded severity and the customer's current ACTIVE plan.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services import sla

ARR_BASELINE = Decimal("12400000.00")
CENTS = Decimal("0.01")


def _money(v) -> str:
    return str(Decimal(v or 0).quantize(CENTS))


def _rate(ok: int, total: int) -> float | None:
    return round(100.0 * ok / total, 1) if total else None


def arr_summary(cur) -> dict:
    cur.execute("""SELECT plan_tier, count(*) AS contracts, sum(annual_recurring_revenue) AS arr
                   FROM contracts WHERE status = 'ACTIVE' GROUP BY plan_tier ORDER BY plan_tier""")
    by_plan = cur.fetchall()
    total = sum((r["arr"] for r in by_plan), Decimal("0"))
    return {
        "active_arr": _money(total), "baseline_arr": _money(ARR_BASELINE),
        "delta_vs_baseline": _money(total - ARR_BASELINE), "mrr": _money(total / 12),
        "active_contracts": sum(r["contracts"] for r in by_plan),
        "by_plan": [{"plan_tier": r["plan_tier"], "contracts": r["contracts"], "arr": _money(r["arr"])} for r in by_plan],
    }


def customer_summary(cur) -> dict:
    cur.execute("""SELECT c.status, c.tier, EXISTS (SELECT 1 FROM contracts k WHERE k.customer_id = c.customer_id
                                                  AND k.status = 'ACTIVE') AS has_active_contract
                   FROM customers c""")
    rows = cur.fetchall()
    return {
        "total_customers": len(rows),
        "active_customers": sum(r["has_active_contract"] for r in rows),   # customers under an ACTIVE contract
        "by_status": dict(sorted(Counter(r["status"] for r in rows).items())),
        "by_tier": dict(sorted(Counter(r["tier"] for r in rows).items())),
    }


def approvals_summary(cur, now: datetime) -> dict:
    cur.execute("""SELECT count(*) FILTER (WHERE status = 'PENDING' AND expires_at > %(now)s) AS pending,
                          count(*) FILTER (WHERE status = 'PENDING' AND expires_at <= %(now)s) AS pending_expired,
                          coalesce(sum(monthly_cost_impact) FILTER (WHERE status = 'PENDING' AND expires_at > %(now)s), 0)
                              AS pending_monthly_impact,
                          min(created_at) FILTER (WHERE status = 'PENDING' AND expires_at > %(now)s) AS oldest_pending,
                          count(*) FILTER (WHERE status = 'APPROVED') AS approved,
                          count(*) FILTER (WHERE status = 'REJECTED') AS rejected,
                          count(*) FILTER (WHERE status = 'EXPIRED') AS expired
                   FROM approval_requests""", {"now": now})
    r = cur.fetchone()
    oldest = r["oldest_pending"]
    return {
        "pending": r["pending"], "awaiting_expiry_sweep": r["pending_expired"],
        "pending_monthly_impact": _money(r["pending_monthly_impact"]),
        "oldest_pending_age_hours": round((now - oldest).total_seconds() / 3600, 1) if oldest else None,
        "decided": {"approved": r["approved"], "rejected": r["rejected"], "expired": r["expired"]},
    }


def sla_summary(cur, now: datetime) -> dict:
    cur.execute("""SELECT t.ticket_id, t.severity, t.created_at, t.resolved_at, k.plan_tier
                   FROM support_tickets t
                   JOIN contracts k ON k.customer_id = t.customer_id AND k.status = 'ACTIVE'""")
    resolved = Counter()
    compliant = Counter()
    open_total = open_breached = open_at_risk = 0
    for t in cur.fetchall():
        if t["resolved_at"] is not None:
            due = sla.evaluate_sla(t["severity"], t["plan_tier"], t["created_at"], t["resolved_at"])["resolution_due_at"]
            resolved[t["severity"]] += 1
            compliant[t["severity"]] += t["resolved_at"] <= due
        else:
            s = sla.evaluate_sla(t["severity"], t["plan_tier"], t["created_at"], now)
            open_total += 1
            open_breached += s["breached"]
            open_at_risk += (not s["breached"]) and s["resolution_budget_consumed_pct"] >= 75
    total_resolved = sum(resolved.values())
    return {
        "compliance_rate_pct": _rate(sum(compliant.values()), total_resolved),
        "resolved_tickets": total_resolved,
        "resolved_within_sla": sum(compliant.values()),
        "by_severity": {sev: {"resolved": resolved[sev], "within_sla": compliant[sev],
                              "compliance_rate_pct": _rate(compliant[sev], resolved[sev])}
                        for sev in ("P1", "P2", "P3", "P4")},
        "open_tickets": open_total, "open_breached": open_breached, "open_at_risk": open_at_risk,
        "as_of": now.isoformat(),
    }


def agent_summary(cur) -> dict:
    cur.execute("SELECT state, count(*) AS n FROM governance_runs GROUP BY state")
    by_state = {r["state"]: r["n"] for r in cur.fetchall()}
    cur.execute("""SELECT count(*) AS n, coalesce(sum(jsonb_array_length(history)), 0) AS transitions,
                          coalesce(sum((context->'execution'->>'arr_delta')::numeric), 0) AS arr_added,
                          coalesce(sum((context->'impact'->>'added_seats')::int)
                                   FILTER (WHERE state = 'EXECUTED'), 0) AS seats_added
                   FROM governance_runs""")
    r = cur.fetchone()
    finished = sum(by_state.get(s, 0) for s in ("EXECUTED", "REJECTED", "FAILED"))
    cur.execute("SELECT coalesce(context->>'engine', 'native') AS engine, count(*) AS n FROM governance_runs GROUP BY 1")
    engines = {r["engine"]: r["n"] for r in cur.fetchall()}
    return {
        "total_runs": r["n"], "active_runs": r["n"] - finished, "by_engine": dict(sorted(engines.items())),
        "state_transitions": r["transitions"], "by_state": dict(sorted(by_state.items())),
        "seats_provisioned": r["seats_added"], "arr_added": _money(r["arr_added"]),
        "failure_rate_pct": _rate(by_state.get("FAILED", 0), finished),
    }


def automation_summary(cur) -> dict:
    cur.execute("SELECT direction, event_type, status, count(*) AS n FROM automation_events "
                "WHERE event_type <> 'copilot.ask' GROUP BY direction, event_type, status")
    rows = cur.fetchall()
    by = defaultdict(Counter)
    for r in rows:
        by[r["direction"]][r["status"]] += r["n"]
    callbacks = Counter()
    for r in rows:
        if r["direction"] == "callback":
            callbacks[r["event_type"]] += r["n"]
    out = by["outbound"]
    real_dispatches = out["DELIVERED"] + out["FAILED"] + out["QUEUED_OFFLINE"]   # mock sends excluded
    inbound = by["inbound"]
    return {
        "total_events": sum(r["n"] for r in rows),
        "n8n_dispatches": {"delivered": out["DELIVERED"], "failed": out["FAILED"],
                           "queued_offline": out["QUEUED_OFFLINE"], "mocked": out["MOCKED"],
                           "success_rate_pct": _rate(out["DELIVERED"], real_dispatches)},
        "workflow_runs": {"completed": callbacks["workflow.completed"], "failed": callbacks["workflow.failed"],
                          "success_rate_pct": _rate(callbacks["workflow.completed"],
                                                    callbacks["workflow.completed"] + callbacks["workflow.failed"])},
        "inbound_events": {"processed": inbound["PROCESSED"], "rejected": inbound["REJECTED"],
                           "accept_rate_pct": _rate(inbound["PROCESSED"], inbound["PROCESSED"] + inbound["REJECTED"])},
    }


def _describe(r: dict) -> str:
    p = r["payload"] or {}
    data = p.get("data") or {}
    if r["direction"] == "callback":
        return f"n8n {data.get('workflow', '?')}: {data.get('action', r['event_type'])}"
    if r["event_type"] == "copilot.ask":
        return f"Copilot answered for {p.get('asked_by')}" + (" (grounded)" if p.get("grounded") else " (not grounded)")
    if r["event_type"] == "approval.decision":
        return f"{p.get('approver_id')} {p.get('action')} → {p.get('final_state') or p.get('error')}"
    arrow = "→ n8n" if r["direction"] == "outbound" else "← " + r["source"]
    return f"{r['event_type']} {arrow}"


def activity_feed(cur, limit: int = 20) -> list[dict]:
    cur.execute("""SELECT created_at, direction, event_type, source, subject_id, status, payload
                   FROM automation_events ORDER BY created_at DESC, id DESC LIMIT %s""", (limit,))
    items = [{"at": r["created_at"], "kind": f"event.{r['direction']}", "type": r["event_type"],
              "subject_id": r["subject_id"], "status": r["status"], "summary": _describe(r)} for r in cur.fetchall()]
    cur.execute("""SELECT g.request_id, g.customer_id, h->>'to' AS state, h->>'reason' AS reason,
                          (h->>'at')::timestamptz AS at
                   FROM governance_runs g, jsonb_array_elements(g.history) h
                   ORDER BY (h->>'at')::timestamptz DESC LIMIT %s""", (limit,))
    items += [{"at": r["at"], "kind": "agent.transition", "type": r["state"], "subject_id": r["request_id"],
               "status": r["state"], "summary": f"{r['request_id']} ({r['customer_id']}) → {r['state']}: {r['reason']}"}
              for r in cur.fetchall()]
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:limit]


def build_dashboard(cur, now: datetime | None = None, activity_limit: int = 20) -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "generated_at": now,
        "revenue": arr_summary(cur),
        "customers": customer_summary(cur),
        "approvals": approvals_summary(cur, now),
        "sla": sla_summary(cur, now),
        "agents": agent_summary(cur),
        "automation": automation_summary(cur),
        "recent_activity": activity_feed(cur, activity_limit),
    }


# =========================================================================== operations control panel

# The six automations the platform runs, as the dashboard shows them.
AGENTS = [
    ("p1_ticket_escalation", "P1 escalation", "n8n workflow"),
    ("seat_expansion_trigger", "Seat expansion intake", "n8n workflow"),
    ("delinquent_invoice_reconciliation", "Invoice reconciliation", "n8n workflow"),
    ("hitl_approval_notification", "Approval notifications", "n8n workflow"),
    ("governance_agent", "Governance agent", "LangGraph agent"),
    ("rag_copilot", "RAG copilot", "LangChain chain"),
]
N8N_WORKFLOWS = [key for key, _, kind in AGENTS if kind == "n8n workflow"]

# Estimated manual minutes one successful run replaces. Stated assumptions, not measurements:
# they are returned with every response so the UI can label "time saved" as an estimate.
ASSUMED_MINUTES_SAVED = {
    "p1_ticket_escalation": 20,              # triage, SLA lookup, finding the SOP, paging
    "seat_expansion_trigger": 15,            # pricing, 30-day aggregation, approval routing
    "delinquent_invoice_reconciliation": 25, # account summary + drafting a collection notice
    "hitl_approval_notification": 5,         # preparing and sending an approval request
    "governance_agent": 15,                  # contract check + impact calculation
    "rag_copilot": 10,                       # searching SOPs/contracts for an answer
}

WINDOWS = {"24h": (24, 1), "7d": (24 * 7, 6), "30d": (24 * 30, 24)}   # window hours, bucket hours

EXECUTIONS_SQL = """
SELECT created_at AS at, payload->'data'->>'workflow' AS workflow,
       CASE WHEN event_type = 'workflow.failed' THEN 'failed' ELSE 'successful' END AS outcome
FROM automation_events WHERE direction = 'callback' AND created_at > %(since)s
UNION ALL
SELECT g.created_at, 'governance_agent',
       CASE WHEN g.state = 'FAILED' THEN 'failed'
            WHEN a.run_id IS NOT NULL THEN 'human_review'
            ELSE 'successful' END
FROM governance_runs g LEFT JOIN approval_requests a ON a.run_id = g.run_id
WHERE g.created_at > %(since)s
UNION ALL
SELECT created_at, 'rag_copilot', CASE WHEN status = 'PROCESSED' THEN 'successful' ELSE 'failed' END
FROM automation_events WHERE event_type = 'copilot.ask' AND created_at > %(since)s
"""


def _delta_pct(now_v: float, prev_v: float) -> float | None:
    return None if not prev_v else round(100.0 * (now_v - prev_v) / prev_v, 1)


def _kpis(rows: list[dict], pending_agents: set[str]) -> dict:
    total = len(rows)
    failed = sum(r["outcome"] == "failed" for r in rows)
    minutes = sum(ASSUMED_MINUTES_SAVED.get(r["workflow"], 0) for r in rows if r["outcome"] != "failed")
    workflows = {r["workflow"] for r in rows if r["workflow"] in N8N_WORKFLOWS}
    agents = {r["workflow"] for r in rows} | pending_agents
    return {"active_workflows": len(workflows), "executions": total,
            "success_rate": round(100.0 * (total - failed) / total, 1) if total else None,
            "time_saved_hours": round(minutes / 60, 1), "active_agents": len(agents & {k for k, _, _ in AGENTS})}


def operations_summary(cur, window: str = "24h", now: datetime | None = None) -> dict:
    """KPIs vs the previous window, an execution time series, per-workflow stats and agent status."""
    now = now or datetime.now(timezone.utc)
    hours, bucket = WINDOWS[window]
    start = now - timedelta(hours=hours)
    prev_start = start - timedelta(hours=hours)
    cur.execute(EXECUTIONS_SQL, {"since": prev_start})
    rows = cur.fetchall()
    current = [r for r in rows if r["at"] > start]
    previous = [r for r in rows if r["at"] <= start]

    cur.execute("SELECT count(*) AS n FROM approval_requests WHERE status = 'PENDING' AND expires_at > %s", (now,))
    pending = cur.fetchone()["n"]
    pending_agents = {"governance_agent"} if pending else set()

    k_now, k_prev = _kpis(current, pending_agents), _kpis(previous, set())
    kpis = {
        "active_workflows": {"value": k_now["active_workflows"], "previous": k_prev["active_workflows"],
                             "delta_pct": _delta_pct(k_now["active_workflows"], k_prev["active_workflows"]),
                             "of_total": len(N8N_WORKFLOWS)},
        "total_executions": {"value": k_now["executions"], "previous": k_prev["executions"],
                             "delta_pct": _delta_pct(k_now["executions"], k_prev["executions"])},
        "success_rate": {"value": k_now["success_rate"], "previous": k_prev["success_rate"],
                         "delta_points": (None if k_now["success_rate"] is None or k_prev["success_rate"] is None
                                          else round(k_now["success_rate"] - k_prev["success_rate"], 1))},
        "time_saved_hours": {"value": k_now["time_saved_hours"], "previous": k_prev["time_saved_hours"],
                             "delta_pct": _delta_pct(k_now["time_saved_hours"], k_prev["time_saved_hours"]),
                             "estimated": True},
        "active_agents": {"value": k_now["active_agents"], "previous": k_prev["active_agents"],
                          "delta_pct": _delta_pct(k_now["active_agents"], k_prev["active_agents"]),
                          "of_total": len(AGENTS)},
    }

    # time series: fixed buckets aligned to the bucket size (UTC), oldest first
    step = timedelta(hours=bucket)
    first = datetime.fromtimestamp((start.timestamp() // step.total_seconds()) * step.total_seconds(), tz=timezone.utc)
    series, t = [], first
    while t <= now:
        series.append({"t": t, "successful": 0, "failed": 0, "human_review": 0})
        t += step
    for r in current:
        idx = int((r["at"] - first) // step)
        if 0 <= idx < len(series):
            series[idx][r["outcome"]] += 1

    labels = {k: (label, kind) for k, label, kind in AGENTS}
    workflows = []
    for key, (label, kind) in labels.items():
        mine = [r for r in current if r["workflow"] == key]
        if not mine:
            continue
        failed = sum(r["outcome"] == "failed" for r in mine)
        workflows.append({"key": key, "name": label, "kind": kind, "executions": len(mine),
                          "success_rate": round(100.0 * (len(mine) - failed) / len(mine), 1),
                          "human_review": sum(r["outcome"] == "human_review" for r in mine),
                          "last_run_at": max(r["at"] for r in mine)})
    workflows.sort(key=lambda w: (-w["executions"], -w["success_rate"]))

    agents = []
    for key, (label, kind) in labels.items():
        mine = sorted((r for r in current if r["workflow"] == key), key=lambda r: r["at"])
        if mine and mine[-1]["outcome"] == "failed":
            status, detail = "error", "last run failed"
        elif key in pending_agents:
            status, detail = "in_review", f"{pending} approval{'s' if pending != 1 else ''} waiting"
        elif mine:
            status, detail = "active", f"{len(mine)} run{'s' if len(mine) != 1 else ''} in window"
        else:
            status, detail = "idle", "no runs in window"
        agents.append({"key": key, "name": label, "kind": kind, "status": status, "detail": detail,
                       "runs": len(mine), "last_run_at": mine[-1]["at"] if mine else None})
    distribution = {s: sum(a["status"] == s for a in agents) for s in ("active", "idle", "in_review", "error")}

    return {"generated_at": now, "window": window, "window_start": start, "bucket_hours": bucket,
            "kpis": kpis, "series": series, "workflows": workflows, "agents": agents,
            "agent_status": distribution, "assumed_minutes_saved": ASSUMED_MINUTES_SAVED}


# =========================================================================== workflow queue board

N8N_STEPS = {
    "p1_ticket_escalation": ["SLA evaluated", "Dispatched to n8n", "Copilot triage", "Alert formatted and logged"],
    "seat_expansion_trigger": ["Request captured", "$500 rule evaluated", "Approval request formatted", "Outcome logged"],
    "delinquent_invoice_reconciliation": ["Overdue invoice found", "Account summarised", "Notice drafted",
                                          "Action logged"],
    "hitl_approval_notification": ["Event received", "Card built", "Channel notified", "Outcome logged"],
}


def _n8n_priority(workflow: str, outcome: dict) -> str:
    if workflow == "p1_ticket_escalation":
        sev = ((outcome.get("alert") or {}).get("generic") or {}).get("severity")
        return {"P1": "critical", "P2": "high"}.get(sev, "normal")
    if workflow == "seat_expansion_trigger":
        return "high" if outcome.get("requires_finance_approval") else "normal"
    if workflow == "delinquent_invoice_reconciliation":
        owed = (outcome.get("account") or {}).get("total_outstanding") or "0"
        return "high" if Decimal(str(owed)) >= Decimal("100000") else "normal"
    return "normal"


def workflow_board(cur, governance_rows: list[dict], window: str = "24h", limit: int = 12,
                   now: datetime | None = None) -> list[dict]:
    """Running and recent workflow executions, newest activity first, anything needing a person on top.

    governance_rows: explain.summary() dicts (built by the API layer, which owns the agent).
    n8n runs come from the audit log: a callback is a finished run; a P1 dispatch without a callback yet is
    still running (n8n is waiting on the copilot).
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=WINDOWS[window][0])
    labels = {k: (label, kind) for k, label, kind in AGENTS}
    items = []
    for g in governance_rows:
        state = {"WAITING_FOR_APPROVAL": "waiting", "EXECUTED": "completed", "REJECTED": "rejected",
                 "FAILED": "failed"}.get(g["state"], "running")
        items.append({"id": g["run_id"], "workflow": "governance_agent", "name": labels["governance_agent"][0],
                      "kind": labels["governance_agent"][1], "subject": f'{g["customer_id"]}, {g["request_id"]}',
                      "state": state, "priority": g["priority"], "steps_done": g["steps_done"],
                      "steps_total": g["steps_total"], "current_step": g["current_step"], "detail": g["headline"],
                      "started_at": g["started_at"], "updated_at": g["updated_at"]})

    # every callback that could close a dispatch in the window (not just the rows shown on the board)
    cur.execute("""SELECT DISTINCT payload->'data'->>'correlation_id' AS c FROM automation_events
                   WHERE direction = 'callback' AND created_at > %s""", (since,))
    finished_correlations = {r["c"] for r in cur.fetchall() if r["c"]}
    cur.execute("""SELECT event_id::text AS id, event_type, subject_id, created_at, payload->'data' AS d
                   FROM automation_events WHERE direction = 'callback' AND created_at > %s
                   ORDER BY created_at DESC LIMIT %s""", (since, limit * 2))
    for r in cur.fetchall():
        d = r["d"] or {}
        wf = d.get("workflow")
        if wf not in N8N_STEPS:
            continue
        failed = r["event_type"] == "workflow.failed"
        steps = N8N_STEPS[wf]
        items.append({"id": r["id"], "workflow": wf, "name": labels[wf][0], "kind": labels[wf][1],
                      "subject": r["subject_id"] or "scheduled run", "state": "failed" if failed else "completed",
                      "priority": _n8n_priority(wf, d.get("outcome") or {}),
                      "steps_done": len(steps) - 1 if failed else len(steps), "steps_total": len(steps),
                      "current_step": steps[-1] if failed else None,
                      "detail": (d.get("action") or "").replace("_", " "),
                      "started_at": r["created_at"].isoformat(), "updated_at": r["created_at"].isoformat()})

    # P1 escalations handed to n8n that haven't reported back yet are still running
    cur.execute("""SELECT event_id::text AS id, subject_id, created_at, payload->'data' AS d FROM automation_events
                   WHERE direction = 'outbound' AND event_type = 'ticket.escalation_required'
                     AND status = 'DELIVERED' AND created_at > %s ORDER BY created_at DESC""", (since,))
    steps = N8N_STEPS["p1_ticket_escalation"]
    for r in cur.fetchall():
        d = r["d"] or {}
        if d.get("correlation_id") in finished_correlations:
            continue
        stale = (now - r["created_at"]).total_seconds() > 600
        items.append({"id": r["id"], "workflow": "p1_ticket_escalation", "name": labels["p1_ticket_escalation"][0],
                      "kind": labels["p1_ticket_escalation"][1], "subject": r["subject_id"],
                      "state": "failed" if stale else "running",
                      "priority": {"P1": "critical", "P2": "high"}.get(d.get("effective_severity"), "normal"),
                      "steps_done": 2, "steps_total": len(steps), "current_step": steps[2],
                      "detail": "no callback from n8n after 10 min" if stale else "n8n is waiting on the copilot",
                      "started_at": r["created_at"].isoformat(), "updated_at": r["created_at"].isoformat()})

    attention = {"waiting": 0, "running": 1, "failed": 2}
    items.sort(key=lambda i: datetime.fromisoformat(i["updated_at"]), reverse=True)
    items.sort(key=lambda i: attention.get(i["state"], 3))
    return items[:limit]
