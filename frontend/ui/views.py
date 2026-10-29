"""The four operator screens. Each takes the API client and renders one page."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from ui.api import ApiError, NexusFlowClient
from ui.components import (
    ago, answer_html, chip, empty, esc, facts, html_block, kpi, md, money, pct, rate_row,
)

FONT = "Instrument Sans, system-ui, sans-serif"
FEED_TONE = {"REJECTED": "breach", "FAILED": "breach", "QUEUED_OFFLINE": "wait", "WAITING_FOR_APPROVAL": "wait"}


def show_error(exc: ApiError, what: str) -> None:
    st.error(md(f"Couldn't load {what}: {exc.detail}. Error code {exc.code}."), icon=None)


# --------------------------------------------------------------------------- overview

def overview(client: NexusFlowClient) -> None:
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
        st.write("")
        with st.container(key="glass_arr"):
            html_block('<div class="nf-panel-title">ARR by plan</div>'
                       '<div class="nf-panel-sub">Annual recurring revenue; active contracts in brackets</div>')
            plans = sorted(rev["by_plan"], key=lambda p: float(p["arr"]))
            fig = go.Figure(go.Bar(
                x=[float(p["arr"]) for p in plans], orientation="h",
                y=[f'{p["plan_tier"].title()} ({p["contracts"]})' for p in plans],
                marker=dict(color=["#2EC4B6", "#4E93DA", "#6C63FF"][: len(plans)], line=dict(width=0)),
                text=[money(p["arr"], True) for p in plans],
                textposition="outside", cliponaxis=False, textfont=dict(color="#E8EEF6", size=13),
                hovertemplate="%{y}: $%{x:,.0f}<extra></extra>"))
            fig.update_layout(height=230, margin=dict(l=0, r=8, t=4, b=0), paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)", font=dict(family=FONT, color="#E8EEF6", size=13),
                              xaxis=dict(showgrid=False, visible=False,
                                         range=[0, max(float(p["arr"]) for p in plans) * 1.25]),
                              yaxis=dict(showgrid=False),
                              bargap=0.38, showlegend=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    with right:
        items = d["recent_activity"]
        rows = "".join(
            f'<li><span class="nf-dot {FEED_TONE.get(i["status"], "ok")}"></span><span>{esc(i["summary"])}</span>'
            f'<time datetime="{esc(i["at"])}">{esc(ago(i["at"]))}</time></li>' for i in items)
        html_block('<div class="nf-glass"><div class="nf-panel-title">Recent activity</div>'
                   '<div class="nf-panel-sub">Webhooks, n8n runs and agent decisions</div>'
                   + (f'<ul class="nf-feed">{rows}</ul>' if items else
                      '<div class="nf-empty">No activity yet. Events appear here as n8n and the agent work.</div>')
                   + "</div>")


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
        st.write("")
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
    st.write("")
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
