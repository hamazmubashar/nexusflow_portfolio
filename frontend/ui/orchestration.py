"""Orchestration panels: workflow step tracker, AI decision & reasoning card, workflow queue board.

All three render API data only (GET /agents/governance/runs[/{id}], /analytics/workflow-runs).
The agent's decisions are rule-based, so the reasoning card shows rule coverage, not a made-up
probability; the copilot's own confidence appears only for its advisory contract review.
"""

from __future__ import annotations

from ui.components import ago, esc, facts, money

STEP_TONE = {"complete": "ok", "in_progress": "wait", "pending": "idle", "skipped": "skip", "rejected": "fail",
             "failed": "fail", "expired": "fail"}
STEP_LABEL = {"complete": "Complete", "in_progress": "In progress", "pending": "Pending", "skipped": "Not needed",
              "rejected": "Rejected", "failed": "Failed", "expired": "Expired"}
CHECK_ICON = {"pass": ("ok", "✓"), "flag": ("wait", "!"), "fail": ("fail", "✕"), "skipped": ("idle", "–")}
STATE_TONE = {"running": "wait", "waiting": "wait", "completed": "ok", "rejected": "idle", "failed": "fail"}
STATE_LABEL = {"running": "Running", "waiting": "Needs approval", "completed": "Completed", "rejected": "Rejected",
               "failed": "Failed"}
PRIORITY_TONE = {"critical": "fail", "high": "wait", "normal": "idle"}


def tracker(run: dict) -> str:
    p = run["pipeline"]
    impact = run.get("impact") or {}
    contract = run.get("contract") or {}
    approval = run.get("approval") or {}
    steps = []
    for i, s in enumerate(p["steps"], 1):
        tone = STEP_TONE[s["status"]]
        mark = "✓" if s["status"] == "complete" else "✕" if tone == "fail" else str(i)
        steps.append(
            f'<li class="nf-step {tone}"><div class="nf-step-rail"><span class="nf-step-mark" aria-hidden="true">'
            f'{mark}</span></div><span class="nf-step-name">{esc(s["name"])}</span>'
            f'<span class="nf-step-desc">{esc(s["description"])}</span>'
            f'<span class="nf-pill {tone if tone != "skip" else "idle"}">{esc(STEP_LABEL[s["status"]])}</span>'
            f'<span class="nf-step-when">{esc(ago(s["at"])) if s["at"] else ""}</span></li>')
    state = _board_state(run["state"])
    items = [("Customer", f'{contract.get("company_name") or run["customer_id"]} ({run["customer_id"]})'),
             ("Request", run["request_id"])]
    if contract:
        items.append(("Plan", f'{contract["plan_tier"].title()}, ${contract["annual_unit_price"]}/seat/yr'))
    if impact:
        items += [("Seats", f'{contract.get("committed_seats", 0):,} + {impact["added_seats"]}'),
                  ("Monthly impact", f'{money(impact["aggregate_monthly_30d_display"])} (30-day total)'),
                  ("ARR impact", f'+{money(impact["annual_arr_impact"])}')]
    if approval:
        items.append(("Approver", approval["assigned_approver_id"]))
    return (f'<div class="nf-card-head"><div><div class="nf-panel-title">Workflow orchestration</div>'
            f'<div class="nf-card-meta">Seat expansion governance pipeline</div></div>'
            f'<span class="nf-pill {STATE_TONE.get(state, "idle")}"><span class="nf-dot {STATE_TONE.get(state, "idle")}">'
            f'</span>{esc(STATE_LABEL.get(state, state))}</span></div>'
            f'<ol class="nf-stepper" aria-label="Pipeline steps">{"".join(steps)}</ol>'
            f'<div class="nf-progress"><div class="nf-progress-head"><span>{p["progress_pct"]}% complete</span>'
            f'<span>{"Now: " + esc(p["current_step"]) if p["current_step"] and not p["finished"] else "Finished"}</span></div>'
            f'<div class="nf-bar" role="progressbar" aria-valuenow="{p["progress_pct"]}" aria-valuemin="0" '
            f'aria-valuemax="100"><span style="width:{p["progress_pct"]}%"></span></div>'
            f'{"<div class=nf-eta>" + esc(p["eta"]) + "</div>" if p["eta"] else ""}</div>'
            + facts(items).replace('class="nf-facts"', 'class="nf-facts nf-run-facts"'))


def _board_state(state: str) -> str:
    return {"WAITING_FOR_APPROVAL": "waiting", "EXECUTED": "completed", "REJECTED": "rejected",
            "FAILED": "failed"}.get(state, "running")


def decision(run: dict) -> str:
    e = run["explanation"]
    coverage = round(100 * e["rules_evaluated"] / max(e["rules_total"], 1))
    tone = "fail" if e["failures"] else "wait" if e["flags"] else "ok"
    checks = "".join(
        f'<li class="{CHECK_ICON[c["status"]][0]}"><span class="nf-check" aria-label="{esc(c["status"])}">'
        f'{CHECK_ICON[c["status"]][1]}</span><span><span class="nf-check-label">{esc(c["label"])}</span>'
        f'<span class="nf-check-detail">{esc(c["detail"])}</span></span></li>' for c in e["checks"])
    actions = "".join(f'<li><span class="nf-dot ok"></span>{esc(a["label"])}</li>' for a in e["actions"]) or \
        '<li><span class="nf-dot idle"></span>No actions yet</li>'
    advisory = (f'Copilot contract review: {esc(e["advisory_confidence"])} confidence (advisory)'
                if e["advisory_confidence"] else "No copilot review used in this decision")
    return (f'<div class="nf-card-head"><div class="nf-panel-title">AI decision and reasoning</div>'
            f'<span class="nf-card-meta">Governance agent</span></div>'
            f'<div class="nf-decision-body"><div class="nf-decision-main">'
            f'<div class="nf-decision {tone}"><span class="nf-dot {tone}"></span>{esc(e["headline"])}</div>'
            f'<ul class="nf-checks">{checks}</ul></div>'
            f'<aside class="nf-decision-aside"><div class="nf-basis"><div class="nf-ring" style="--pct:{coverage}" '
            f'role="img" aria-label="{e["rules_evaluated"]} of {e["rules_total"]} rules evaluated">'
            f'<span>{e["rules_evaluated"]}/{e["rules_total"]}</span></div>'
            f'<div class="nf-basis-text"><strong>Rules evaluated</strong><br>Deterministic decision: every outcome '
            f'follows from these rules, so there is no probability score.<br><span class="nf-card-meta">'
            f'{advisory}</span></div></div>'
            f'<div class="nf-panel-title nf-actions-title">Automated actions</div><ul class="nf-actions">{actions}</ul>'
            f'</aside></div>')


def board(data: dict) -> str:
    if not data["items"]:
        return '<div class="nf-empty">No workflow runs in this window.</div>'
    rows = []
    for i in data["items"]:
        pct = round(100 * i["steps_done"] / max(i["steps_total"], 1))
        st_tone = STATE_TONE[i["state"]]
        rows.append(
            f'<tr><td><span class="wf-name">{esc(i["name"])}</span><span class="wf-kind">{esc(i["kind"])}</span></td>'
            f'<td>{esc(i["subject"] or "")}</td>'
            f'<td><span class="nf-pill {st_tone}"><span class="nf-dot {st_tone}"></span>{esc(STATE_LABEL[i["state"]])}</span></td>'
            f'<td><span class="nf-pill {PRIORITY_TONE[i["priority"]]}">{esc(i["priority"].title())}</span></td>'
            f'<td class="num"><span class="nf-meter" aria-hidden="true"><span style="width:{pct}%"></span></span>'
            f'{i["steps_done"]}/{i["steps_total"]}</td>'
            f'<td class="nf-board-detail">{esc(i["current_step"] + ": " if i["current_step"] and i["state"] == "running" else "")}'
            f'{esc(i["detail"])}</td><td class="num nf-card-meta">{esc(ago(i["updated_at"]))}</td></tr>')
    return ('<div class="nf-table-scroll"><table class="nf-table nf-board"><thead><tr><th>Workflow</th><th>Subject</th>'
            '<th>State</th><th>Priority</th><th class="num">Steps</th><th>Current step</th><th class="num">Updated</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
