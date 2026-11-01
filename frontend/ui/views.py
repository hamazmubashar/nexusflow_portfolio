"""The four operator screens. Each takes the API client and renders one page."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from ui import orchestration
from ui.api import ApiError, NexusFlowClient
from ui.components import (
    ago, answer_html, chip, empty, esc, facts, html_block, kpi, md, money, pct, rate_row,
)

FONT = "Instrument Sans, system-ui, sans-serif"
INK, INK_FAINT, GRID = "#0E1A2B", "#8593A6", "#EEF1F6"
SERIES = {"successful": "#14B8A6", "failed": "#E5484D", "human_review": "#E8A317"}
STATUS_COLOURS = {"active": "#12B886", "idle": "#B8C2D0", "in_review": "#E8A317", "error": "#E5484D"}
FEED_TONE = {"REJECTED": "breach", "FAILED": "breach", "QUEUED_OFFLINE": "wait", "WAITING_FOR_APPROVAL": "wait"}


def show_error(exc: ApiError, what: str) -> None:
    st.error(md(f"Couldn't load {what}: {exc.detail}. Error code {exc.code}."), icon=None)


# --------------------------------------------------------------------------- revenue & SLA (former overview)

def revenue(client: NexusFlowClient) -> None:
    try:
        d = client.dashboard(activity_limit=12)
    except ApiError as exc:
        return show_error(exc, "the dashboard")
    rev, cust, appr, sla, agents, auto = (d[k] for k in ("revenue", "customers", "approvals", "sla", "agents", "automation"))

    delta = float(rev["delta_vs_baseline"])
    delta_note = ("on the $12.4M baseline" if delta == 0 else
                  f'<span class="{"up" if delta > 0 else "down"}">{money(delta)}</span> vs the {money(rev["baseline_arr"], True)} baseline')
    pending = appr["pending"]
    html_block('<div class="nf-kpis">'
               + kpi("Total ARR", money(rev["active_arr"], compact=True),
                     f'{delta_note}<br>{money(rev["mrr"], True)} MRR across {rev["active_contracts"]} active contracts',
                     hero=True)
               + kpi("Active accounts", str(cust["active_customers"]),
                     f'{cust["by_status"].get("DELINQUENT", 0)} delinquent of {cust["total_customers"]}')
               + kpi("Pending approvals", str(pending),
                     (f'{money(appr["pending_monthly_impact"])}/mo waiting on a decision' if pending
                      else "Nothing waiting on you"), attention=pending > 0)
               + kpi("SLA compliance", pct(sla["compliance_rate_pct"]),
                     f'{sla["resolved_within_sla"]} of {sla["resolved_tickets"]} resolved in time')
               + kpi("Active agent runs", str(agents["active_runs"]),
                     f'{agents["total_runs"]} LangGraph runs, {agents["seats_provisioned"]} seats provisioned')
               + "</div>")
    breach_note = (f'{sla["open_breached"]} open tickets past their resolution SLA, '
                   f'{sla["open_at_risk"]} more above 75% of budget')
    health_panel = ('<div class="nf-glass"><div class="nf-panel-title">Automation health</div>'
                    f'<div class="nf-panel-sub">{esc(breach_note)}</div>'
                    + rate_row("n8n deliveries", auto["n8n_dispatches"]["success_rate_pct"],
                               f'{auto["n8n_dispatches"]["delivered"]} delivered')
                    + rate_row("Workflow runs completed", auto["workflow_runs"]["success_rate_pct"],
                               f'{auto["workflow_runs"]["completed"]} runs')
                    + rate_row("Inbound events accepted", auto["inbound_events"]["accept_rate_pct"],
                               f'{auto["inbound_events"]["rejected"]} rejected')
                    + "</div>")
    left, right = st.columns([1.6, 1], gap="medium")
    with left:
        html_block(health_panel)
        with st.container(key="glass_arr"):
            html_block('<div class="nf-panel-title">ARR by plan</div>'
                       '<div class="nf-panel-sub">Annual recurring revenue; active contracts in brackets</div>')
            plans = sorted(rev["by_plan"], key=lambda p: float(p["arr"]))
            fig = go.Figure(go.Bar(
                x=[float(p["arr"]) for p in plans], orientation="h",
                y=[f'{p["plan_tier"].title()} ({p["contracts"]})' for p in plans],
                marker=dict(color=["#2EC4B6", "#4E93DA", "#6C63FF"][: len(plans)], line=dict(width=0)),
                text=[money(p["arr"], True) for p in plans],
                textposition="outside", cliponaxis=False, textfont=dict(color="#0E1A2B", size=13),
                hovertemplate="%{y}: $%{x:,.0f}<extra></extra>"))
            fig.update_layout(height=230, margin=dict(l=0, r=8, t=4, b=0), paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)", font=dict(family=FONT, color="#0E1A2B", size=13),
                              xaxis=dict(showgrid=False, visible=False,
                                         range=[0, max(float(p["arr"]) for p in plans) * 1.25]),
                              yaxis=dict(showgrid=False),
                              bargap=0.38, showlegend=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    with right:
        html_block('<div class="nf-glass"><div class="nf-panel-title">Recent activity</div>'
                   '<div class="nf-panel-sub">Webhooks, n8n runs and agent decisions</div>'
                   + _feed(d["recent_activity"]) + "</div>")


# --------------------------------------------------------------------------- approvals

def approvals(client: NexusFlowClient, me: dict) -> None:
    flash = st.session_state.pop("approval_flash", None)
    if flash:
        (st.success if flash["ok"] else st.error)(md(flash["text"]))
    try:
        queue = client.pending_approvals()
    except ApiError as exc:
        return show_error(exc, "the approval queue")
    if not queue["approvals"]:
        return empty("No approvals waiting.",
                     "Seat expansions over $500/month, or with negotiated contract terms, will appear here for review.")

    can_decide = "MANAGER" in me.get("effective_roles", [])
    for ap in queue["approvals"]:
        card, aid = ap["card"], ap["approval_id"]
        mine = ap["assigned_approver_id"] == me.get("subject")
        with st.container(key=f"approval_{aid.replace('-', '')}"):
            html_block(f'<div class="nf-approval-title" role="heading" aria-level="2">{esc(card["title"])}</div>'
                       f'<p class="nf-panel-sub">Assigned to {esc(card["approver"]["name"])} '
                       f'({esc(card["approver"]["employee_id"])}), expires {esc(ago_future(ap["expires_at"]))}</p>')
            html_block("".join(chip(r["text"], "wait") for r in card["reasons"]))
            html_block(f'<p class="nf-formula">{esc(card["summary"])}</p>')
            html_block(facts([(f["label"], friendly_time(f["value"]) if f["label"] == "Expires" else f["value"])
                              for f in card["facts"]]))

            review = card.get("contract_review")
            if review or card.get("contract_documents"):
                with st.expander("Contract review", expanded=False):
                    if review and review.get("answer"):
                        html_block(f'<p class="nf-panel-sub">Advisory only: the copilot can flag terms, '
                                   f'never approve. {"Grounded in the documents." if review.get("grounded") else "Not verified as grounded."}</p>'
                                   f'<div class="nf-answer">{answer_html(review["answer"])}</div>')
                        for c in review.get("citations", []):
                            tone, label = ("ok", "Quote verified") if c.get("quote_verified") else ("breach", "Quote not found")
                            html_block(f'<div class="nf-quote">{esc(plain_quote(c["quote"]))}</div>'
                                       f'{chip(c["doc_id"] + ", " + c["section"])}{chip(label, tone)}')
                    else:
                        st.write(md("The copilot couldn't review these documents. Read them before deciding: "
                                    + ", ".join(card.get("contract_documents", []))))

            if not (can_decide and mine):
                st.info(md(f"Only {card['approver']['name']} can decide this request."))
                continue
            comment = st.text_area("Comment", key=f"comment_{aid}", height=80,
                                   placeholder="Required when rejecting. Saved with the decision.")
            signature = st.text_input("Type your name to sign", key=f"sig_{aid}", value=me.get("name") or "")
            approve_col, reject_col, _ = st.columns([1.3, 1.3, 2.4])
            with approve_col:
                approve = st.button("Approve", key=f"approve_{aid}", type="primary", use_container_width=True)
            with reject_col, st.container(key=f"reject_{aid.replace('-', '')}"):
                reject = st.button("Reject", key=f"rejectbtn_{aid}", use_container_width=True)
            if approve or reject:
                action = "APPROVE" if approve else "REJECT"
                if action == "REJECT" and not comment.strip():
                    st.warning("Add a comment before rejecting. It goes into the audit trail.")
                    continue
                decide(client, me, ap, action, comment, signature)


def friendly_time(ts: str) -> str:
    from datetime import datetime

    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return ts


def ago_future(ts: str) -> str:
    from datetime import datetime, timezone

    then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    hours = (then - datetime.now(timezone.utc)).total_seconds() / 3600
    return f"in {hours:.0f} h" if hours >= 1 else f"in {max(hours * 60, 0):.0f} min"


def decide(client: NexusFlowClient, me: dict, ap: dict, action: str, comment: str, signature: str) -> None:
    with st.spinner("Recording your decision…"):
        try:
            run = client.approve_or_reject(ap["approval_id"], action, me["subject"], comment.strip() or None,
                                           signature.strip() or None)
        except ApiError as exc:
            st.session_state["approval_flash"] = {"ok": False, "text": f"Decision not recorded: {exc.detail} ({exc.code})."}
            st.rerun()
    state = run["state"]
    if state == "EXECUTED":
        ex = run["execution"]
        text = (f"Approved {run['request_id']}. Seats {ex['customer_seats'][0]} to {ex['customer_seats'][1]}, "
                f"ARR {money(ex['contract_arr'][0])} to {money(ex['contract_arr'][1])}.")
    elif state == "REJECTED":
        text = f"Rejected {run['request_id']}. Nothing was provisioned."
    else:
        text = f"{run['request_id']} is {state}. {(run.get('error') or {}).get('message', '')}"
    st.session_state["approval_flash"] = {"ok": state in ("EXECUTED", "REJECTED"), "text": text}
    st.cache_data.clear()
    st.rerun()


# --------------------------------------------------------------------------- copilot

EXAMPLES = [
    "What is the Enterprise P1 initial response and resolution SLA?",
    "CUST-0040 wants 50 more seats. Does that need Finance approval?",
    "Can we mark CUST-0011 as churned if they say payment was sent?",
]


def copilot(client: NexusFlowClient) -> None:
    ex_cols = st.columns(len(EXAMPLES))
    for col, q in zip(ex_cols, EXAMPLES):
        if col.button(q, key=f"ex_{hash(q)}", use_container_width=True):
            st.session_state["copilot_q"] = q
    with st.form("copilot_form", border=False):
        question = st.text_area("Ask about SOPs, contracts and account history", key="copilot_q", height=96,
                                placeholder="For example: when does a P2 ticket escalate to the VP of Customer Success?")
        c1, c2, _ = st.columns([1.4, 1, 2.6])
        mode = c1.radio("Mode", ["Answer with citations", "Show sources only"], horizontal=False, key="copilot_mode",
                        label_visibility="collapsed")
        top_k = c2.slider("Sources", 3, 10, 6)
        submitted = st.form_submit_button("Ask copilot", type="primary")
    if not submitted:
        return
    if len(question.strip()) < 3:
        st.warning("Type a question of at least a few words.")
        return
    if mode == "Show sources only":
        return _sources(client, question, top_k)
    with st.spinner("Searching the documents and checking every quote…"):
        try:
            r = client.ask(question, top_k)
        except ApiError as exc:
            return show_error(exc, "an answer")
    tone = "ok" if r["grounded"] and not r["insufficient_context"] else ("wait" if r["insufficient_context"] else "breach")
    status = ("Grounded in the documents" if tone == "ok" else
              "Not enough in the documents to answer" if tone == "wait" else "Not verified as grounded")
    with st.container(key="glass_answer"):
        html_block(chip(status, tone) + chip(f'Confidence {r["confidence"]}') + chip(r["model"]))
        html_block(f'<div class="nf-answer">{answer_html(r["answer"])}</div>')
    if r["citations"]:
        html_block('<div class="nf-panel-title">Sources quoted</div>'
                   '<div class="nf-panel-sub">Each quote is checked word for word against the source text.</div>')
        for i, c in enumerate(r["citations"]):  # a source can be cited more than once
            verified = c["quote_verified"]
            with st.container(key=f"glass_cite_{i}"):
                html_block(f'<div class="nf-source-head"><div><span class="nf-cite-ref">{esc(c["source_id"])}</span> '
                           f'<strong>{esc(c["doc_id"])}</strong>, {esc(c["section"])}</div>'
                           f'<div>{chip("Quote verified" if verified else "Quote not found in source", "ok" if verified else "breach")}'
                           f'{chip("Policy" if c["authority"] == "governing" else "Correspondence")}</div></div>'
                           f'<div class="nf-quote">{esc(plain_quote(c["quote"]))}</div>'
                           f'<div class="nf-kpi-note">{esc(c["source_file"])}</div>')
    notes = [w for w in r["warnings"] if not w.startswith("engine:")]
    if notes:
        with st.expander("Verification notes"):
            for w in notes:
                st.write(md(w))


def plain_quote(text: str) -> str:
    """Drop markdown emphasis/backticks for display (verification already ignores them)."""
    return text.replace("**", "").replace("`", "")


def _sources(client: NexusFlowClient, query: str, top_k: int) -> None:
    with st.spinner("Searching…"):
        try:
            r = client.search(query, top_k)
        except ApiError as exc:
            return show_error(exc, "search results")
    html_block(f'<div class="nf-panel-sub">{r["count"]} passages, ranked by combined semantic and keyword match '
               f'({esc(r["embedding_model"])})</div>')
    for i, h in enumerate(r["results"], 1):
        with st.container(key=f"glass_hit_{i}"):
            ranks = f'semantic #{h["semantic_rank"] or "–"}, keyword #{h["keyword_rank"] or "–"}'
            html_block(f'<div class="nf-source-head"><strong>{esc(h["doc_id"])}, {esc(h["section"])}</strong>'
                       f'<div>{chip(ranks)}{chip("Policy" if h["authority"] == "governing" else "Correspondence")}</div></div>')
            with st.expander("Passage"):
                st.text(h["content"])


# --------------------------------------------------------------------------- diagnostics

LABELS = {"database": "PostgreSQL", "pgvector": "pgvector", "n8n": "n8n", "gemini": "Gemini API"}


def diagnostics(client: NexusFlowClient, health: dict, me: dict) -> None:
    if st.button("Check again", key="recheck"):
        st.cache_data.clear()
        st.rerun()
    comps = health.get("components", {})
    cards = []
    for key, label in LABELS.items():
        c = comps.get(key, {"status": "unknown"})
        tone = {"ok": "ok", "degraded": "wait", "not_configured": "wait"}.get(c["status"], "breach")
        lines = []
        if c.get("latency_ms") is not None:
            lines.append(f'{c["latency_ms"]:.0f} ms')
        if key == "pgvector" and c.get("extension_version"):
            lines.append(f'v{c["extension_version"]}, {sum(c.get("indexed_chunks", {}).values())} chunks')
        if c.get("detail"):
            lines.append(c["detail"])
        cards.append(kpi(label, {"ok": "Healthy", "degraded": "Degraded", "down": "Down",
                                 "not_configured": "Not set up"}.get(c["status"], "Unknown"),
                         chip(c["status"], tone) + "<br>" + esc(", ".join(lines)), status=True))
    html_block('<div class="nf-kpis">' + "".join(cards) + "</div>")
    left, right = st.columns(2, gap="medium")
    with left:
        html_block('<div class="nf-glass"><div class="nf-panel-title">API</div>'
                   + facts([("Overall", health.get("status", "unknown")), ("Version", health.get("version", "")),
                            ("Environment", health.get("environment", "")),
                            ("Uptime", f'{health.get("uptime_seconds", 0) / 60:.0f} min'),
                            ("n8n dispatch mode", health.get("dispatch_mode", "")),
                            ("Endpoint", client.base_url)]) + "</div>")
    with right:
        html_block('<div class="nf-glass"><div class="nf-panel-title">Signed in as</div>'
                   + facts([("Name", me.get("name") or me.get("subject", "")), ("Employee", me.get("subject", "")),
                            ("Roles", ", ".join(me.get("roles", []))),
                            ("Can also act as", ", ".join(r for r in me.get("effective_roles", []) if r not in me.get("roles", [])) or "nothing else"),
                            ("Auth", me.get("auth", ""))]) + "</div>")
    with st.expander("Raw metrics"):
        try:
            st.json(client.metrics())
        except ApiError as exc:
            show_error(exc, "metrics")


# --------------------------------------------------------------------------- operations control panel

FEED_KIND = {"event.inbound": "Webhook received", "event.outbound": "Sent to n8n", "event.callback": "n8n run",
             "agent.transition": "Agent decision"}
OK_STATES = {"PROCESSED", "DELIVERED", "EXECUTED", "MOCKED", "APPROVED", "CALCULATING_IMPACT",
             "ANALYZING_CONTRACT", "INITIATED"}
WAIT_STATES = {"QUEUED_OFFLINE", "WAITING_FOR_APPROVAL"}
REASON_SHORT = {"MONTHLY_IMPACT_OVER_THRESHOLD": "Over $500/mo", "NEGOTIATED_CONTRACT_TERMS": "Negotiated terms",
                "REQUEST_TYPE_NOT_PURCHASE": "Needs clarification", "CONTRACT_REVIEW_UNAVAILABLE": "Review unavailable",
                "CONTRACT_CORPUS_UNAVAILABLE": "Corpus offline"}
STATUS_LABEL = {"active": "Active", "idle": "Idle", "in_review": "In review", "error": "Error"}
STATUS_TONE = {"active": "ok", "idle": "idle", "in_review": "wait", "error": "fail"}


def compact(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.1f}"


def delta(value: float | None, window: str, points: bool = False) -> str:
    if value is None:
        return f'<span class="nf-delta flat">no data for previous {esc(window)}</span>'
    tone = "up" if value > 0 else "down" if value < 0 else "flat"
    unit = " pts" if points else "%"
    return f'<span class="nf-delta {tone}">{value:+.1f}{unit}</span><span>vs previous {esc(window)}</span>'


def overview(client: NexusFlowClient, me: dict) -> None:
    flash = st.session_state.pop("approval_flash", None)
    if flash:
        (st.success if flash["ok"] else st.error)(md(flash["text"]))
    lead, pick = st.columns([4, 1], vertical_alignment="center")
    with pick:
        window = st.segmented_control("Window", ["24h", "7d", "30d"], default="24h", key="ops_window",
                                      label_visibility="collapsed") or "24h"
    try:
        ops = client.operations(window)
        activity = client.activity(limit=30)
        queue = client.pending_approvals()
    except ApiError as exc:
        return show_error(exc, "the control panel")
    lead.markdown(f'<div class="nf-panel-sub" style="margin:0">Automation since '
                  f'{esc(friendly_time(ops["window_start"]))}, compared with the {esc(window)} before it.</div>',
                  unsafe_allow_html=True)

    k = ops["kpis"]
    minutes = ops["assumed_minutes_saved"]
    est_title = "Estimate: " + ", ".join(f"{m} min per {key.replace('_', ' ')} run" for key, m in minutes.items())
    sr = k["success_rate"]
    html_block('<div class="nf-kpis">'
               + _kpi("Active workflows", f'{k["active_workflows"]["value"]}<small> of {k["active_workflows"]["of_total"]}</small>',
                      delta(k["active_workflows"]["delta_pct"], window))
               + _kpi("Total executions", compact(k["total_executions"]["value"]),
                      delta(k["total_executions"]["delta_pct"], window))
               + _kpi("Success rate", pct(sr["value"]), delta(sr["delta_points"], window, points=True))
               + _kpi("Time saved", f'{compact(k["time_saved_hours"]["value"])}h',
                      delta(k["time_saved_hours"]["delta_pct"], window),
                      badge_html=f'<span class="nf-est" title="{esc(est_title)}">est.</span>')
               + _kpi("Active AI agents", f'{k["active_agents"]["value"]}<small> of {k["active_agents"]["of_total"]}</small>',
                      delta(k["active_agents"]["delta_pct"], window))
               + "</div>")

    focus = _focus_run(client)
    if focus:   # full-width bands: side by side, the decision card was ~300px taller than the tracker
        with st.container(key="card_tracker"):
            html_block(orchestration.tracker(focus))
        with st.container(key="card_decision"):
            html_block(orchestration.decision(focus))

    left, right = st.columns([2, 1], gap="medium")
    with left, st.container(key="card_executions"):
        total = sum(p["successful"] + p["failed"] + p["human_review"] for p in ops["series"])
        html_block(f'<div class="nf-card-head"><div class="nf-panel-title">Workflow execution overview</div>'
                   f'<div class="nf-card-meta">{total} executions, {ops["bucket_hours"]} h buckets</div></div>')
        st.plotly_chart(_execution_chart(ops["series"]), use_container_width=True, config={"displayModeBar": False})
    with right, st.container(key="card_activity"):
        html_block('<div class="nf-card-head"><div class="nf-panel-title">Recent activity</div>'
                   f'<div class="nf-card-meta">latest {len(activity["items"])}</div></div>' + _feed(activity["items"]))

    c1, c2, c3 = st.columns(3, gap="medium")
    with c1, st.container(key="card_workflows"):
        html_block('<div class="nf-card-head"><div class="nf-panel-title">Top performing workflows</div>'
                   f'<div class="nf-card-meta">last {esc(window)}</div></div>' + _workflow_table(ops["workflows"]))
    with c2, st.container(key="card_queue"):
        _review_queue(client, me, queue)
    with c3, st.container(key="card_agents"):
        dist = ops["agent_status"]
        html_block('<div class="nf-card-head"><div class="nf-panel-title">AI agent status</div>'
                   f'<div class="nf-card-meta">{len(ops["agents"])} agents</div></div>')
        st.plotly_chart(_agent_donut(dist, len(ops["agents"])), use_container_width=True,
                        config={"displayModeBar": False})
        html_block('<div class="nf-status-counts">' + "".join(
            f'<span class="nf-pill {STATUS_TONE[k]}"><span class="nf-dot {STATUS_TONE[k]}"></span>'
            f'{dist.get(k, 0)} {STATUS_LABEL[k].lower()}</span>' for k in ("active", "in_review", "error", "idle"))
            + "</div>" + _agent_legend(ops["agents"]))


def _kpi(label: str, value_html: str, note_html: str, badge_html: str = "") -> str:
    return (f'<div class="nf-glass nf-kpi"><div class="nf-kpi-label"><span>{esc(label)}</span>{badge_html}</div>'
            f'<div class="nf-kpi-value">{value_html}</div><div class="nf-kpi-note">{note_html}</div></div>')


def _execution_chart(series: list[dict]) -> go.Figure:
    xs = [p["t"] for p in series]
    fig = go.Figure()
    for key, name, fill in (("successful", "Successful", "tozeroy"), ("human_review", "Human review", None),
                            ("failed", "Failed", None)):
        colour = SERIES[key]
        fig.add_trace(go.Scatter(
            x=xs, y=[p[key] for p in series], name=name, mode="lines+markers", line=dict(color=colour, width=2.2,
                                                                                           shape="spline", smoothing=0.6),
            marker=dict(size=5, color=colour), fill=fill,
            fillcolor="rgba(20,184,166,0.12)" if fill else None,
            hovertemplate=f"{name}: %{{y}}<extra></extra>"))
    fig.update_layout(height=320, margin=dict(l=4, r=8, t=36, b=8, autoexpand=True), paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", font=dict(family=FONT, color=INK, size=12),
                      hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=12)),
                      xaxis=dict(showgrid=False, linecolor=GRID, tickfont=dict(color=INK_FAINT), automargin=True),
                      yaxis=dict(gridcolor=GRID, zeroline=False, rangemode="tozero", tickfont=dict(color=INK_FAINT),
                                 tickformat=",d"))
    return fig


def _feed(items: list[dict]) -> str:
    if not items:
        return '<div class="nf-empty">No events yet. Webhooks, n8n runs and agent decisions stream in here.</div>'
    rows = []
    for i in items:
        tone = "fail" if i["status"] in ("REJECTED", "FAILED") else "wait" if i["status"] in WAIT_STATES else (
            "ok" if i["status"] in OK_STATES else "idle")
        rows.append(f'<li><span class="nf-dot {tone}"></span><span>{esc(i["summary"].replace("_", " "))}'
                    f'<span class="nf-feed-kind">{esc(FEED_KIND.get(i["kind"], i["kind"]))}</span></span>'
                    f'<time datetime="{esc(i["at"])}">{esc(ago(i["at"]))}</time></li>')
    return f'<ul class="nf-feed">{"".join(rows)}</ul>'


def _workflow_table(workflows: list[dict]) -> str:
    if not workflows:
        return '<div class="nf-empty">No workflow runs in this window.</div>'
    body = "".join(
        f'<tr><td><span class="wf-name">{esc(w["name"])}</span><span class="wf-kind">{esc(w["kind"])}'
        f'{", " + str(w["human_review"]) + " reviewed" if w["human_review"] else ""}</span></td>'
        f'<td class="num"><span class="nf-meter" aria-hidden="true"><span style="width:{w["success_rate"]:.0f}%"></span>'
        f'</span>{w["success_rate"]:.1f}%</td><td class="num">{w["executions"]:,}</td></tr>'
        for w in workflows)
    return ('<table class="nf-table"><thead><tr><th>Workflow</th><th class="num">Success</th>'
            f'<th class="num">Runs</th></tr></thead><tbody>{body}</tbody></table>')


def _review_queue(client: NexusFlowClient, me: dict, queue: dict) -> None:
    items = queue["approvals"]
    tone = "wait" if items else "ok"
    html_block('<div class="nf-card-head"><div class="nf-panel-title">Human review queue</div>'
               f'<span class="nf-pill {tone}"><span class="nf-dot {tone}"></span>{len(items)} pending</span></div>')
    if not items:
        html_block('<div class="nf-empty">Nothing waiting. Requests over $500/month or with negotiated terms '
                   'land here.</div>')
    can_decide = "MANAGER" in me.get("effective_roles", [])
    for ap in items[:4]:
        aid = ap["approval_id"]
        reasons = "".join(chip(REASON_SHORT.get(r, r), "wait") for r in ap["reasons"])
        with st.container(key=f"queue_{aid.replace('-', '')}"):
            html_block(f'<div class="nf-queue-item"><div><div class="nf-queue-title">{esc(ap["customer_id"])}, '
                       f'+{ap["proposed_seats"]} seats</div><div class="nf-queue-meta">{esc(ap["request_id"])}, '
                       f'expires {esc(ago_future(ap["expires_at"]))}</div></div>'
                       f'<div class="nf-queue-amount">{esc(money(ap["monthly_cost_impact"]))}/mo</div></div>{reasons}')
            if not (can_decide and ap["assigned_approver_id"] == me.get("subject")):
                html_block(f'<div class="nf-queue-meta">Assigned to {esc(ap["assigned_approver_id"])}</div>')
                continue
            a, r, d = st.columns(3, gap="small")
            if a.button("Approve", key=f"qapprove_{aid}", type="primary", use_container_width=True):
                decide(client, me, ap, "APPROVE", "", me.get("name") or "")
            with r, st.container(key=f"qreject_{aid.replace('-', '')}"), st.popover("Reject", use_container_width=True):
                note = st.text_input("Reason for rejecting", key=f"qnote_{aid}",
                                     placeholder="Saved to the audit trail")
                if st.button("Reject request", key=f"qrejectgo_{aid}", disabled=not note.strip()):
                    decide(client, me, ap, "REJECT", note, me.get("name") or "")
            d.button("Details", key=f"qopen_{aid}", use_container_width=True,
                     on_click=lambda: st.session_state.update(page="Approvals", page_top="Approvals"))
    st.button(f"Open approvals ({len(items)})" if len(items) > 4 else "Open approvals", key="queue_all",
              use_container_width=True,
              on_click=lambda: st.session_state.update(page="Approvals", page_top="Approvals"))


def _agent_donut(dist: dict, total: int) -> go.Figure:
    keys = [k for k in ("active", "in_review", "error", "idle")]
    fig = go.Figure(go.Pie(
        labels=[STATUS_LABEL[k] for k in keys], values=[dist.get(k, 0) for k in keys], hole=0.7, sort=False,
        marker=dict(colors=[STATUS_COLOURS[k] for k in keys], line=dict(color="#FFFFFF", width=2)),
        textinfo="none", hovertemplate="%{label}: %{value}<extra></extra>"))
    fig.update_layout(height=150, margin=dict(l=0, r=0, t=0, b=0), paper_bgcolor="rgba(0,0,0,0)", showlegend=False,
                      annotations=[dict(text=f"<b>{total}</b><br><span style='font-size:11px;color:{INK_FAINT}'>agents</span>",
                                        showarrow=False, font=dict(family=FONT, size=20, color=INK))])
    return fig


def _agent_legend(agents: list[dict]) -> str:
    rows = "".join(   # one line per agent; kind and detail live in the tooltip
        f'<li title="{esc(a["kind"])}: {esc(a["detail"])}"><span class="nf-dot {STATUS_TONE[a["status"]]}"></span>'
        f'<span class="agent-name">{esc(a["name"])}</span><span class="agent-runs">{a["runs"]}</span>'
        f'<span class="nf-pill {STATUS_TONE[a["status"]]}">{esc(STATUS_LABEL[a["status"]])}</span></li>'
        for a in agents)
    return f'<ul class="nf-legend">{rows}</ul>'


# --------------------------------------------------------------------------- operations

def _focus_run(client: NexusFlowClient, run_id: str | None = None) -> dict | None:
    """The run to explain: the one asked for, else the newest still in flight, else the newest of all."""
    try:
        if run_id:
            return client.run(run_id)
        runs = client.governance_runs(limit=1, active_only=True)["runs"] or client.governance_runs(limit=1)["runs"]
        return client.run(runs[0]["run_id"]) if runs else None
    except ApiError as exc:
        show_error(exc, "the governance run")
        return None


def operations(client: NexusFlowClient) -> None:
    lead, pick = st.columns([4, 1], vertical_alignment="center")
    with pick:
        window = st.segmented_control("Window", ["24h", "7d", "30d"], default="24h", key="board_window",
                                      label_visibility="collapsed") or "24h"
    try:
        data = client.workflow_runs(window, limit=20)
        runs = client.governance_runs(limit=30)["runs"]
    except ApiError as exc:
        return show_error(exc, "workflow runs")
    c = data["counts"]
    lead.markdown(f'<div class="nf-panel-sub" style="margin:0">{c["running"]} running, {c["waiting"]} waiting on a '
                  f'person, {c["failed"]} failed, {c["completed"] + c["rejected"]} finished in the last {esc(window)}.</div>',
                  unsafe_allow_html=True)
    with st.container(key="card_board"):
        html_block('<div class="nf-card-head"><div class="nf-panel-title">Active workflow queue</div>'
                   f'<div class="nf-card-meta">items needing a person first</div></div>' + orchestration.board(data))

    if not runs:
        return empty("No governance runs yet.", "Seat requests sent to the agent appear here with their full pipeline.")
    labels = {r["run_id"]: f'{r["request_id"]}, {r["customer_id"]}: {r["state"].replace("_", " ").lower()}'
              for r in runs}
    chosen = st.selectbox("Inspect a governance run", list(labels), format_func=labels.get, key="ops_run")
    run = _focus_run(client, chosen)
    if not run:
        return
    with st.container(key="card_tracker_ops"):
        html_block(orchestration.tracker(run))
        with st.expander("State history"):
            st.dataframe([{"at": h["at"], "to": h["to"], "reason": h["reason"]} for h in run["history"]],
                         hide_index=True, use_container_width=True)
    with st.container(key="card_decision_ops"):
        html_block(orchestration.decision(run))
