"""n8n Workflows page: each automation as a native flowchart with real per-node telemetry.

Structure and counts come from GET /analytics/n8n-workflows, which reads only durations and statuses
from n8n's execution store. The page never links to or embeds the n8n editor. Sample inputs are
synthetic examples defined by the API, labelled as such.
"""

from __future__ import annotations

import json

import streamlit as st

from ui.api import ApiError, NexusFlowClient
from ui.components import ago, esc, html_block, md, pct

STATE = {"healthy": ("ok", "Healthy"), "degraded": ("fail", "Failures in window"), "idle": ("idle", "No runs in window"),
         "unknown": ("idle", "Telemetry unavailable")}
NODE_STATE = {"ok": "All runs succeeded", "fail": "Has failed runs", "idle": "Not run in this window",
              "unknown": "Telemetry unavailable"}


def duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1:
        return "<1 ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.1f} s" if ms < 60_000 else f"{ms / 60_000:.1f} min"


def lanes(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Arrange a workflow for display: one lane per entry point, following the longest path, with every
    other outgoing edge attached to its parent as a branch. Each node appears exactly once."""
    by_name = {n["name"]: n for n in nodes}
    outgoing: dict[str, list[dict]] = {n["name"]: [] for n in nodes}
    targets = set()
    for e in edges:
        outgoing[e["source"]].append(e)
        targets.add(e["target"])
    placed: set[str] = set()

    def longest(name: str) -> list[str]:
        best: list[str] = []
        for e in sorted(outgoing[name], key=lambda e: e["branch"] == "no"):   # ties follow the "yes" side
            if e["target"] not in placed:
                path = longest(e["target"])
                if len(path) > len(best):
                    best = path
        return [name] + best

    def chain(name: str) -> dict:
        """A branch: the node, the nodes that follow it, and the node it rejoins, if any."""
        names, rejoin = [], None
        current = name
        while True:
            names.append(current)
            placed.add(current)
            following = outgoing[current]
            if not following:
                break
            if following[0]["target"] in placed:
                rejoin = following[0]["target"]
                break
            current = following[0]["target"]
        return {"nodes": [by_name[n] for n in names], "rejoins": rejoin}

    entries = sorted((n["name"] for n in nodes if n["name"] not in targets),
                     key=lambda n: len(longest(n)), reverse=True)
    result = []
    for entry in entries:
        path = longest(entry)
        placed.update(path)
        steps = []
        for i, name in enumerate(path):
            following = path[i + 1] if i + 1 < len(path) else None
            steps.append({"node": by_name[name], "branches": [], "_following": following})
        result.append({"entry": by_name[entry], "steps": steps, "joins": None})
        last = outgoing[path[-1]]
        if last:
            result[-1]["joins"] = last[0]["target"]
    for lane in result:                     # after all main paths, so a branch can't claim another lane's node
        for step in lane["steps"]:
            for e in outgoing[step["node"]["name"]]:
                if e["target"] == step["_following"]:
                    continue
                if e["target"] in placed:
                    step["branches"].append({"label": e["branch"], "nodes": [], "rejoins": e["target"]})
                else:
                    step["branches"].append({"label": e["branch"], **chain(e["target"])})
    for lane in result:
        for step in lane["steps"]:
            del step["_following"]
    return result


def _node(n: dict, small: bool = False) -> str:
    state = n["state"]
    rate = "—" if n["success_rate"] is None else f'{n["success_rate"]:.0f}%'
    return (f'<div class="nf-wf-node {state}{" small" if small else ""}" title="{esc(NODE_STATE[state])}">'
            f'<div class="nf-wf-role"><span class="nf-dot {state if state != "unknown" else "idle"}"></span>'
            f'<span>{esc(n["role"])}</span></div><div class="nf-wf-name">{esc(n["name"])}</div>'
            f'<div class="nf-wf-stats"><span>{n["runs"]:,} runs</span><span>{rate}</span>'
            f'<span>{esc(duration(n["avg_ms"]))}</span></div></div>')


def _branch(b: dict) -> str:
    label = f'<span class="nf-wf-branch-label">{esc(b["label"].title())}</span>' if b["label"] else ""
    body = "".join(_node(n, small=True) for n in b["nodes"])
    if b["rejoins"]:
        verb = "then" if b["nodes"] else "skips to"
        body += f'<span class="nf-wf-rejoin">{verb} {esc(b["rejoins"])}</span>'
    return f'<div class="nf-wf-branch">{label}{body}</div>'


def flowchart(workflow: dict) -> str:
    out = []
    for lane in lanes(workflow["nodes"], workflow["edges"]):
        steps = []
        for i, step in enumerate(lane["steps"]):
            arrow = '<span class="nf-wf-arrow" aria-hidden="true">→</span>' if i < len(lane["steps"]) - 1 else ""
            steps.append(f'<li class="nf-wf-step"><div class="nf-wf-col">{_node(step["node"])}'
                         f'{"".join(_branch(b) for b in step["branches"])}</div>{arrow}</li>')
        joins = f'<li class="nf-wf-joins">→ joins {esc(lane["joins"])}</li>' if lane["joins"] else ""
        out.append(f'<div class="nf-wf-lane"><div class="nf-wf-lane-title">Entry: {esc(lane["entry"]["name"])}</div>'
                   f'<ol class="nf-wf-flow" aria-label="Steps from {esc(lane["entry"]["name"])}">'
                   f'{"".join(steps)}{joins}</ol></div>')
    return "".join(out)


def _stat(label: str, value: str) -> str:
    return f'<div><div class="nf-fact-label">{esc(label)}</div><div class="nf-wf-stat">{esc(value)}</div></div>'


def card_header(w: dict) -> str:
    tone, label = STATE[w["state"]]
    published = {True: "Published", False: "Not published", None: "Publish state unknown"}[w["published"]]
    running = [_stat("Running", str(w["running"]))] if w["running"] else []
    return (f'<div class="nf-card-head"><div class="nf-panel-title">{esc(w["name"])}</div>'
            f'<div class="nf-wf-pills"><span class="nf-pill idle">{esc(published)}</span>'
            f'<span class="nf-pill {tone}"><span class="nf-dot {tone}"></span>{esc(label)}</span></div></div>'
            f'<div class="nf-panel-sub">{esc(w["summary"])}</div><div class="nf-wf-stats-row">'
            + _stat("Executions", f'{w["executions"]:,}') + _stat("Failed", f'{w["failed"]:,}')
            + _stat("Success rate", pct(w["success_rate"])) + _stat("Avg latency", duration(w["avg_latency_ms"]))
            + _stat("P95 latency", duration(w["p95_latency_ms"]))
            + _stat("Last run", ago(w["last_run_at"]) or "—") + "".join(running) + "</div>")


def totals(workflows: list[dict]) -> str:
    done = sum(w["succeeded"] + w["failed"] for w in workflows)
    ok = sum(w["succeeded"] for w in workflows)
    weighted = sum((w["avg_latency_ms"] or 0) * (w["succeeded"] + w["failed"]) for w in workflows)
    live = sum(bool(w["published"]) for w in workflows)
    cells = [("Published workflows", f"{live} of {len(workflows)}"),
             ("Executions", f'{sum(w["executions"] for w in workflows):,}'),
             ("Success rate", pct(100 * ok / done) if done else "—"),
             ("Avg latency", duration(weighted / done) if done else "—")]
    return '<div class="nf-wf-totals">' + "".join(
        f'<div class="nf-glass nf-kpi"><div class="nf-kpi-label">{esc(k)}</div><div class="nf-kpi-value">{esc(v)}</div></div>'
        for k, v in cells) + "</div>"


def render(client: NexusFlowClient) -> None:
    lead, pick = st.columns([4, 1], vertical_alignment="center")
    with pick:
        window = st.segmented_control("Window", ["24h", "7d", "30d"], default="7d", key="wf_window",
                                      label_visibility="collapsed") or "7d"
    try:
        data = client.n8n_workflows(window)
    except ApiError as exc:
        st.error(md(f"Couldn't load the workflows: {exc.detail}. Error code {exc.code}."))
        return
    note = "Counts, success rates and latency are measured per node by n8n."
    if data["window_exceeds_retention"]:
        note += f' n8n keeps run data for {data["retention_hours"] // 24} days, so older runs aren\'t counted.'
    lead.markdown(f'<div class="nf-panel-sub nf-wf-note">{esc(note)}</div>', unsafe_allow_html=True)
    if not data["telemetry_available"]:
        st.warning(md("n8n's execution data can't be reached right now, so these flowcharts show structure only."))

    html_block(totals(data["workflows"]))
    for w in data["workflows"]:
        with st.container(key=f"card_wf_{w['key']}"):
            html_block(card_header(w) + flowchart(w))
            with st.expander("Sample input (synthetic)"):
                st.code(json.dumps(w["sample_input"], indent=2), language="json")
