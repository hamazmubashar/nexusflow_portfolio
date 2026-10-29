# Governance agent: LangGraph StateGraph

Generated from `backend/app/services/agents/langgraph_governance.py` (`mermaid()`). Dashed edges are conditional.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	analyze_contract(analyze_contract)
	check_rag_compliance(check_rag_compliance)
	calculate_impact(calculate_impact)
	wait_for_approval(wait_for_approval)
	execute_mutation(execute_mutation)
	__end__([<p>__end__</p>]):::last
	__start__ -.-> analyze_contract;
	__start__ -.-> execute_mutation;
	analyze_contract -.-> __end__;
	analyze_contract -.-> check_rag_compliance;
	calculate_impact -.-> __end__;
	calculate_impact -.-> execute_mutation;
	calculate_impact -.-> wait_for_approval;
	check_rag_compliance --> calculate_impact;
	execute_mutation --> __end__;
	wait_for_approval --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

| Node | What it does | Persisted state after it |
|---|---|---|
| `analyze_contract` | Validates customer, contract and status; loads plan, price, term, seats | `ANALYZING_CONTRACT`, or `REJECTED` on a rule violation |
| `check_rag_compliance` | Finds customer-specific MSA/SOW chunks by entity ID (no LLM), then asks the copilot to review them *outside* any DB transaction | `CALCULATING_IMPACT` |
| `calculate_impact` | SOP-02 math in `Decimal`: 30-day aggregate, MRR/ARR delta, tier range, HITL reasons | `APPROVED`, `REJECTED`, or still `CALCULATING_IMPACT` with reasons |
| `wait_for_approval` | Opens the HITL item, issues the single-use token, queues the n8n card; the graph run ends here | `WAITING_FOR_APPROVAL` |
| `execute_mutation` | The only node that changes seats/ARR: one locked transaction, never twice per request | `EXECUTED` (or `FAILED`, nothing applied) |

The checkpoint is `governance_runs`. There's no LangGraph checkpointer, because a second copy of run state could disagree with the database the money moves in. An approval re-enters the same compiled graph: the entry router sends a persisted `APPROVED` run straight to `execute_mutation`. Routing reads only the persisted state and the deterministic HITL reasons. The copilot's review can add a reason, never remove one.
