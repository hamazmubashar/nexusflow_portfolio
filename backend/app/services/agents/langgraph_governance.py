"""Commercial Governance agent as a LangGraph StateGraph.

    START ─(entry)─┬─> analyze_contract ──> check_rag_compliance ──> calculate_impact ─(decision)─┬─> execute_mutation ─> END
                   │        └─(rule violation)─> END                                              ├─> wait_for_approval ─> END  (HITL pause)
                   │                                                                              └─(rejected)─> END
                   └─(run already APPROVED: human decision)─> execute_mutation ─> END

Design choices:
  * Nodes are thin: each runs one deterministic step of GovernanceAgent (governance_agent.py) inside its
    own committed transaction, so `governance_runs` is updated at every node boundary.
  * governance_runs *is* the checkpoint. We deliberately don't add a LangGraph checkpointer: a second
    copy of run state could disagree with the database that the money moves in. The HITL pause ends the
    graph run at wait_for_approval; an approval re-enters the same compiled graph, and the entry router
    sends a persisted APPROVED run straight to execute_mutation. Restarts lose nothing.
  * Routing reads only persisted run.state and the deterministic HITL reasons: no LLM output can choose
    an edge. The copilot's contract review (check_rag_compliance) can add a HITL reason, never remove one.
"""

from __future__ import annotations

import json
import operator
from functools import lru_cache
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.core.db import get_conn
from app.schemas.webhooks import SeatRequestData
from app.services.agents.governance_agent import GovernanceAgent, RunView, State

ENGINE = "langgraph"
NODES = ("analyze_contract", "check_rag_compliance", "calculate_impact", "wait_for_approval", "execute_mutation")


class GovernanceGraphState(TypedDict, total=False):
    run: RunView                         # mutable view of the governance_runs row (persisted by every node)
    data: SeatRequestData | None         # the seat request (None when resuming after a human decision)
    approved_by: str                     # "agent" for auto-approval, else the approver's employee id
    hitl: dict[str, Any] | None          # {impact, reasons, price} from calculate_impact when a human is needed
    trace: Annotated[list[str], operator.add]  # nodes visited, in order (observability + tests)


def _agent(config: RunnableConfig) -> GovernanceAgent:
    return config["configurable"]["agent"]


# --------------------------------------------------------------------------- nodes

def analyze_contract(state: GovernanceGraphState, config: RunnableConfig) -> dict:
    agent, run, data = _agent(config), state["run"], state["data"]
    agent._step(run, lambda cur: agent._analyze_contract(cur, run, data))
    return {"trace": ["analyze_contract"]}


def check_rag_compliance(state: GovernanceGraphState, config: RunnableConfig) -> dict:
    agent, run, data = _agent(config), state["run"], state["data"]
    found: dict = {}
    agent._step(run, lambda cur: found.update(agent._find_contract_documents(cur, data)))
    try:
        clause_check = agent._review_contract_documents(run, data, found)   # no transaction held here
    except Exception as exc:  # _review_contract_documents already traps reviewer errors; this is belt and braces
        agent._fail(run, exc)
        raise
    agent._step(run, lambda cur: agent._record_compliance(cur, run, clause_check))
    return {"trace": ["check_rag_compliance"]}


def calculate_impact(state: GovernanceGraphState, config: RunnableConfig) -> dict:
    agent, run, data = _agent(config), state["run"], state["data"]
    out: dict = {}
    agent._step(run, lambda cur: out.update(agent._calculate_impact(cur, run, data) or {}))
    return {"trace": ["calculate_impact"], "hitl": out or None, "approved_by": "agent"}


def wait_for_approval(state: GovernanceGraphState, config: RunnableConfig) -> dict:
    """HITL pause: open the approval item, emit the n8n notification, and end this graph run."""
    agent, run, data, hitl = _agent(config), state["run"], state["data"], state["hitl"]
    agent._step(run, lambda cur: agent._open_approval(cur, run, data, hitl["impact"], hitl["reasons"], hitl["price"]))
    return {"trace": ["wait_for_approval"]}


def execute_mutation(state: GovernanceGraphState, config: RunnableConfig) -> dict:
    """The only node that changes seats/ARR (one locked transaction that also flips the run to EXECUTED)."""
    agent, run = _agent(config), state["run"]
    agent._step(run, lambda cur: agent._execute(cur, run, approved_by=state["approved_by"]))
    return {"trace": ["execute_mutation"]}


# --------------------------------------------------------------------------- routing (deterministic)

def route_entry(state: GovernanceGraphState) -> str:
    run = state["run"]
    if run.state == State.INITIATED:
        return "analyze_contract"
    if run.state == State.APPROVED:
        return "execute_mutation"
    raise ValueError(f"governance graph can't start from state {run.state}")


def route_after_analysis(state: GovernanceGraphState) -> str:
    return END if state["run"].state == State.REJECTED else "check_rag_compliance"


def route_decision(state: GovernanceGraphState) -> str:
    run = state["run"]
    if run.state == State.APPROVED:
        return "execute_mutation"
    if run.state == State.REJECTED:
        return END
    if run.state == State.CALCULATING_IMPACT and state.get("hitl"):
        return "wait_for_approval"
    raise ValueError(f"no route out of calculate_impact for state {run.state}")


def build_graph() -> StateGraph:
    g = StateGraph(GovernanceGraphState)
    for name, fn in zip(NODES, (analyze_contract, check_rag_compliance, calculate_impact,
                                wait_for_approval, execute_mutation)):
        g.add_node(name, fn)
    g.add_conditional_edges(START, route_entry, ["analyze_contract", "execute_mutation"])
    g.add_conditional_edges("analyze_contract", route_after_analysis, ["check_rag_compliance", END])
    g.add_edge("check_rag_compliance", "calculate_impact")
    g.add_conditional_edges("calculate_impact", route_decision, ["execute_mutation", "wait_for_approval", END])
    g.add_edge("wait_for_approval", END)
    g.add_edge("execute_mutation", END)
    return g


@lru_cache
def compiled_graph():
    return build_graph().compile()


def run_graph(agent: GovernanceAgent, run: RunView, data: SeatRequestData | None = None,
              approved_by: str = "agent") -> GovernanceGraphState:
    """Invoke the graph for a new run (data given) or to execute an APPROVED run (human decision)."""
    run.context.setdefault("engine", ENGINE)
    initial: GovernanceGraphState = {"run": run, "data": data, "approved_by": approved_by, "hitl": None, "trace": []}
    final = compiled_graph().invoke(initial, config={"configurable": {"agent": agent}})
    run.context["graph_trace"] = run.context.get("graph_trace", []) + final["trace"]
    if run.run_id:  # persist the trace next to the run (no state change)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE governance_runs SET context = jsonb_set(context, '{graph_trace}', %s::jsonb), "
                        "updated_at = now() WHERE run_id = %s",
                        (json.dumps(run.context["graph_trace"]), run.run_id))
    return final


def mermaid() -> str:
    """Mermaid diagram of the compiled graph (docs/governance_langgraph.md is generated from this)."""
    return compiled_graph().get_graph().draw_mermaid()
