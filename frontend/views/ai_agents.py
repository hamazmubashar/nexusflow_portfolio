"""AI Agents page: the six platform agents with live state, tools, rules, models and recent traces.

Data comes from GET /analytics/ai-agents. Rule-based agents are labelled "No LLM" rather than given a
model they don't use, and a failed last run shows as Error alongside Active, In review and Idle.
"""

from __future__ import annotations

import streamlit as st

from ui.api import ApiError, NexusFlowClient
from ui.components import ago, esc, html_block, md

STATE = {"active": ("ok", "Active"), "in_review": ("wait", "In review"), "idle": ("idle", "Idle"),
         "error": ("fail", "Error")}


def _list(title: str, items: str) -> str:
    return f'<div class="nf-agent-section"><div class="nf-fact-label">{esc(title)}</div>{items}</div>'


def _models(agent: dict) -> str:
    chips = "".join(f'<span class="nf-chip">{esc(m)}</span>' for m in agent["models"])
    return (f'<div class="nf-agent-models">{chips}</div>' if chips else "") \
        + f'<div class="nf-agent-note">{esc(agent["model_note"])}</div>'


def _traces(agent: dict) -> str:
    if not agent["traces"]:
        return '<div class="nf-agent-note">No runs in this window.</div>'
    rows = "".join(
        f'<li><span class="nf-dot {t["tone"]}"></span><div><div class="nf-agent-trace-title">{esc(t["title"])}'
        f'<span class="nf-card-meta">{esc(ago(t["at"]))}</span></div>'
        f'<div class="nf-agent-note">{esc(t["detail"])}</div></div></li>' for t in agent["traces"])
    return f'<ol class="nf-agent-traces">{rows}</ol>'


def card(agent: dict) -> str:
    tone, label = STATE[agent["state"]]
    llm = "Uses LLM" if agent["uses_llm"] else "No LLM"
    tools = "".join(f'<li><strong>{esc(t["name"])}</strong><span>{esc(t["description"])}</span></li>'
                    for t in agent["tools"])
    rules = "".join(f"<li>{esc(c)}</li>" for c in agent["constraints"])
    return (f'<article class="nf-glass nf-agent"><div class="nf-card-head"><div class="nf-panel-title">{esc(agent["name"])}</div>'
            f'<span class="nf-pill {tone}"><span class="nf-dot {tone}"></span>{esc(label)}</span></div>'
            f'<div class="nf-agent-meta"><span class="nf-chip">{esc(agent["engine"])}</span>'
            f'<span class="nf-chip">{esc(llm)}</span><span class="nf-card-meta">{esc(agent["state_detail"])}</span></div>'
            f'<div class="nf-panel-sub">{esc(agent["summary"])}</div>'
            + _list("Tools", f'<ul class="nf-agent-tools">{tools}</ul>')
            + _list("Rule constraints", f'<ul class="nf-agent-rules">{rules}</ul>')
            + _list("LLM models", _models(agent))
            + _list("Recent execution traces", _traces(agent))
            + "</article>")


def render(client: NexusFlowClient) -> None:
    lead, pick = st.columns([4, 1], vertical_alignment="center")
    with pick:
        window = st.segmented_control("Window", ["24h", "7d", "30d"], default="7d", key="agent_window",
                                      label_visibility="collapsed") or "7d"
    try:
        data = client.ai_agents(window)
    except ApiError as exc:
        st.error(md(f"Couldn't load the agents: {exc.detail}. Error code {exc.code}."))
        return
    counts = data["state_counts"]
    lead.markdown('<div class="nf-wf-pills">' + "".join(
        f'<span class="nf-pill {STATE[s][0]}"><span class="nf-dot {STATE[s][0]}"></span>{counts.get(s, 0)} '
        f'{esc(STATE[s][1].lower())}</span>' for s in ("active", "in_review", "idle", "error")) + "</div>",
        unsafe_allow_html=True)
    html_block('<div class="nf-agent-grid">' + "".join(card(a) for a in data["agents"]) + "</div>")
