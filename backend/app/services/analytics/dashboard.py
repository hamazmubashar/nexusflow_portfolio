"""Mission-control KPIs for the executive dashboard (Stage 6 UI) and /api/v1/metrics.

Everything is computed live from the relational tables plus the automation ledgers; nothing
is cached or hard-coded except the $12.4M ARR baseline the snapshot was generated against
(reported next to the live figure, so drift from approved expansions is visible).

SLA compliance applies the same SOP-01 clocks as the escalation webhook (app.services.sla),
using each ticket's recorded severity and the customer's current ACTIVE plan.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
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
                "GROUP BY direction, event_type, status")
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
