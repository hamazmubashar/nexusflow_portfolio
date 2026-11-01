"""Offline tests for the n8n Workflows and AI Agents pages: layout, escaping and credential safety."""

import json
import re
from pathlib import Path

import pytest

from views import ai_agents, n8n_workflows

WORKFLOW_FILES = sorted((Path(__file__).resolve().parents[2] / "n8n_workflows").glob("*.json"))
CREDENTIAL = re.compile(r"eyJ[\w-]{6,}\.|Bearer\s+\w|sha256=[0-9a-f]{8}|AIza[\w-]{8}|\bv1\.[\w-]{8,}\.")


def structure(path: Path) -> tuple[list[dict], list[dict]]:
    raw = json.loads(path.read_text())
    kinds = {n["name"]: n["type"].rsplit(".", 1)[-1] for n in raw["nodes"]}
    nodes = [{"name": n["name"], "role": "Transform", "state": "ok", "runs": 3, "errors": 0, "success_rate": 100.0,
              "avg_ms": 2.5} for n in raw["nodes"]]
    edges = [{"source": s, "target": t["node"], "branch": ("yes" if port == 0 else "no") if kinds[s] == "if" else None}
             for s, ports in raw["connections"].items() for port, outs in enumerate(ports.get("main", [])) for t in outs]
    return nodes, edges


def placed_names(result: list[dict]) -> list[str]:
    names = []
    for lane in result:
        for step in lane["steps"]:
            names.append(step["node"]["name"])
            for branch in step["branches"]:
                names += [n["name"] for n in branch["nodes"]]
    return names


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.stem)
def test_every_node_of_the_real_workflows_appears_exactly_once(path):
    nodes, edges = structure(path)
    assert sorted(placed_names(n8n_workflows.lanes(nodes, edges))) == sorted(n["name"] for n in nodes)


def test_second_entry_point_gets_its_own_lane_and_reject_is_a_branch():
    hitl = next(p for p in WORKFLOW_FILES if "hitl" in p.name)
    result = n8n_workflows.lanes(*structure(hitl))
    assert {lane["entry"]["name"] for lane in result} == {"Approval event received", "Approval decision relay"}
    valid = next(s for s in result[0]["steps"] if s["node"]["name"] == "Signature valid?")
    assert [(b["label"], b["nodes"][0]["name"]) for b in valid["branches"]] == [("no", "Reject: bad signature")]


def test_shared_entry_path_is_shown_as_joining_the_main_lane():
    invoices = next(p for p in WORKFLOW_FILES if "invoice" in p.name)
    result = n8n_workflows.lanes(*structure(invoices))
    assert result[0]["entry"]["name"] == "Delinquency alert received" and result[0]["joins"] is None
    assert {lane["joins"] for lane in result[1:]} == {"Build delinquency event", "Sign overdue poll"}


def test_duration_formats():
    assert [n8n_workflows.duration(v) for v in (None, 0, 12.4, 47364, 120100)] == ["—", "<1 ms", "12 ms", "47.4 s", "2.0 min"]


def workflow(**over) -> dict:
    nodes = [{"name": "Hook <in>", "role": "Webhook trigger", "state": "ok", "runs": 12, "errors": 0, "success_rate": 100.0,
              "avg_ms": 0.3},
             {"name": "Post alert", "role": "Notification dispatch", "state": "idle", "runs": 0, "errors": 0,
              "success_rate": None, "avg_ms": None}]
    return {"key": "k", "n8n_id": "x", "name": "Flow", "summary": "s", "published": True, "state": "healthy", "executions": 12,
            "succeeded": 12, "failed": 0, "running": 0, "success_rate": 100.0, "avg_latency_ms": 40.0, "p95_latency_ms": 90.0,
            "last_run_at": None, "nodes": nodes, "edges": [{"source": "Hook <in>", "target": "Post alert", "branch": None}],
            "sample_input": {}, **over}


def test_flowchart_shows_state_and_measured_numbers_and_escapes_names():
    html = n8n_workflows.flowchart(workflow())
    assert "Hook &lt;in&gt;" in html and "<in>" not in html
    assert 'nf-wf-node ok' in html and 'nf-wf-node idle' in html
    assert "12 runs" in html and "100%" in html and "&lt;1 ms" in html
    assert "0 runs" in html and "—" in html                 # never run: no invented rate or latency


def test_page_markup_has_no_editor_link_or_iframe():
    html = n8n_workflows.card_header(workflow()) + n8n_workflows.flowchart(workflow())
    assert "<iframe" not in html and "<a " not in html and "5679" not in html and "5678" not in html


def test_totals_weight_latency_by_finished_runs():
    html = n8n_workflows.totals([workflow(), workflow(succeeded=0, failed=4, avg_latency_ms=1000.0, executions=4,
                                                      published=False)])
    assert "1 of 2" in html and "16" in html and "75.0%" in html and "280 ms" in html


AGENT = {"key": "a", "name": "Contract <Analyzer>", "engine": "LangGraph node", "summary": "s", "state": "in_review",
         "state_detail": "1 approval waiting", "uses_llm": False, "models": [], "model_note": "No model.",
         "tools": [{"name": "lookup", "description": "reads"}], "constraints": ["Rule one"], "runs": 1,
         "last_run_at": None, "traces": [{"at": "2026-01-01T00:00:00+00:00", "title": "T", "detail": "D", "tone": "wait"}]}


def test_agent_card_shows_state_tools_rules_model_and_trace():
    html = ai_agents.card(AGENT)
    assert "Contract &lt;Analyzer&gt;" in html and "In review" in html and "No LLM" in html
    for text in ("lookup", "Rule one", "No model.", "Recent execution traces"):
        assert text in html


def test_agent_with_a_model_and_no_runs():
    html = ai_agents.card({**AGENT, "uses_llm": True, "models": ["gemini-x"], "traces": [], "state": "idle"})
    assert "Uses LLM" in html and "gemini-x" in html and "No runs in this window." in html and "Idle" in html


def test_rendered_markup_contains_nothing_shaped_like_a_credential():
    html = n8n_workflows.flowchart(workflow()) + n8n_workflows.card_header(workflow()) + ai_agents.card(AGENT)
    assert not CREDENTIAL.search(html)
