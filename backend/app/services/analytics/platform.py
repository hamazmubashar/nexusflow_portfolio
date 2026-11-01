"""Data behind the console's "n8n Workflows" and "AI Agents" pages.

Workflow structure comes from the exported JSON in n8n_workflows/ (names, types and connections only;
node parameters are never read). Telemetry comes from n8n's own execution store, opened read-only.
Those stored executions hold full payloads, approval tokens included, so the parser below keeps three
fields per node run (duration, status, node name) and drops everything else.

Agents are a fixed catalog plus live state and traces from governance_runs and automation_events.
Traces are built from allow-listed fields, and every response goes through sanitize() as a second line
of defence. Nothing here is invented: a node or agent with no runs reports zero, not a placeholder.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from statistics import quantiles

import psycopg2
import psycopg2.extensions
import psycopg2.extras

from app.core.config import REPO_ROOT, get_settings
from app.services.analytics.dashboard import WINDOWS

WORKFLOW_DIR = REPO_ROOT / "n8n_workflows"
RETENTION_HOURS = 168          # EXECUTIONS_DATA_MAX_AGE in docker-compose.yml: older runs are pruned by n8n
MAX_EXECUTIONS = 1000
REDACTED = "[redacted]"

# --------------------------------------------------------------------------- redaction

_SENSITIVE_KEY = re.compile(r"authorization|token|secret|password|passwd|api[-_]?key|signature|credential|cookie", re.I)
_SENSITIVE_VALUE = [
    re.compile(r"\beyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]+"),            # JWT
    re.compile(r"\bBearer\s+[\w.~+/=-]+", re.I),
    re.compile(r"\bsha256=[0-9a-f]{16,}", re.I),                 # webhook signature
    re.compile(r"\bv1\.[\w-]{8,}\.[\w-]{8,}"),                   # approval token
    re.compile(r"\bAIza[\w-]{20,}"),                             # Google API key
    re.compile(r"\bsk-[\w-]{16,}"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I),  # URL with embedded password
]


def sanitize(value):
    """Copy of `value` with sensitive keys and anything shaped like a credential replaced."""
    if isinstance(value, dict):
        return {k: REDACTED if isinstance(k, str) and _SENSITIVE_KEY.search(k) else sanitize(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        for pattern in _SENSITIVE_VALUE:
            value = pattern.sub(REDACTED, value)
    return value


# --------------------------------------------------------------------------- workflow catalog

def _envelope(event_type: str, source: str, data: dict) -> dict:
    return {"headers": {"X-NexusFlow-Timestamp": "1767225600", "X-NexusFlow-Signature": REDACTED},
            "body": {"event_id": "00000000-0000-4000-8000-000000000001", "event_type": event_type,
                     "occurred_at": "2026-01-01T00:00:00Z", "source": source, "data": data}}


WORKFLOWS = [
    {"key": "delinquent_invoice_reconciliation", "file": "delinquent_invoice_reconciliation_workflow.json",
     "n8n_id": "nxfDelinquentInv1", "name": "Invoice Reconciliation",
     "summary": "Finds overdue invoices on a schedule or from a signed alert, then builds the account summary and "
                "a collection notice draft. The draft is never sent.",
     "sample_input": _envelope("invoice.delinquent", "n8n", {
         "invoice_id": "INV-SAMPLE-0001", "customer_id": "CUST-0000", "as_of": "2026-01-01", "draft_notice": True})},
    {"key": "seat_expansion_trigger", "file": "seat_expansion_trigger_workflow.json",
     "n8n_id": "nxfSeatExpansion1", "name": "Seat Expansion Intake",
     "summary": "Takes a signed seat request, applies the $500/month rule over the rolling 30 days and formats the "
                "approval request. It never provisions seats.",
     "sample_input": _envelope("seat.requested", "n8n", {
         "request_id": "SRQ-SAMPLE-0001", "customer_id": "CUST-0000", "contract_id": "CTR-0000", "added_seats": 5,
         "request_type": "PURCHASE", "requester_email": "requester@example.com",
         "requested_at": "2026-01-01T00:00:00Z"})},
    {"key": "p1_ticket_escalation", "file": "p1_ticket_escalation_workflow.json",
     "n8n_id": "nxfP1TicketEscal1", "name": "P1 Escalation",
     "summary": "Receives a P1 or P2 escalation from the API, asks the RAG copilot for triage and formats the alert. "
                "The alert posts only if a channel is configured.",
     "sample_input": _envelope("ticket.escalation_required", "nexusflow-api", {
         "ticket_id": "TK-0000", "customer_id": "CUST-0000", "effective_severity": "P1",
         "summary": "Sample: authentication outage reported by the customer"})},
    {"key": "hitl_approval_notification", "file": "hitl_approval_notification_workflow.json",
     "n8n_id": "nxfHitlApproval1", "name": "Approval Notifications",
     "summary": "Posts the governance agent's approval card, without its token, and relays signed decisions back to "
                "the agent through a second entry point.",
     "sample_input": _envelope("approval.requested", "nexusflow-api", {
         "approval_id": "00000000-0000-4000-8000-000000000002", "run_id": "00000000-0000-4000-8000-000000000003",
         "request_id": "SRQ-SAMPLE-0001", "customer_id": "CUST-0000", "contract_id": "CTR-0000",
         "state": "WAITING_FOR_APPROVAL"})},
]

_TRIGGERS = {"webhook": "Webhook trigger", "scheduleTrigger": "Schedule trigger",
             "executeWorkflowTrigger": "Manual trigger"}
_NAMED_ROLES = [("Evaluate ", "Rule engine"), ("Ask RAG", "AI copilot"), ("Post", "Notification dispatch"),
                ("Submit decision", "Governance agent"), ("Report to", "Audit callback"), ("Log action", "Audit callback"),
                ("Get account", "Rule engine"), ("Fetch", "API call")]


def node_role(name: str, node_type: str) -> str:
    kind = node_type.rsplit(".", 1)[-1]
    if kind in _TRIGGERS:
        return _TRIGGERS[kind]
    if kind == "respondToWebhook":
        return "Response"
    lowered = name.lower()
    if "signature" in lowered or lowered.startswith("sign "):
        return "HMAC SHA-256"
    for prefix, role in _NAMED_ROLES:
        if name.startswith(prefix):
            return role
    return {"if": "Decision", "httpRequest": "API call"}.get(kind, "Transform")


@lru_cache
def load_structure(filename: str) -> dict:
    """Nodes and edges of one exported workflow. Parameters and credentials are deliberately not copied."""
    raw = json.loads((WORKFLOW_DIR / filename).read_text(encoding="utf-8"))
    kinds = {n["name"]: n["type"].rsplit(".", 1)[-1] for n in raw["nodes"]}
    nodes = [{"name": n["name"], "role": node_role(n["name"], n["type"])} for n in raw["nodes"]]
    edges = []
    for source, ports in raw["connections"].items():
        for port, outputs in enumerate(ports.get("main", [])):
            branch = ("yes" if port == 0 else "no") if kinds.get(source) == "if" else None
            edges += [{"source": source, "target": t["node"], "branch": branch} for t in outputs]
    return {"nodes": nodes, "edges": edges}


# --------------------------------------------------------------------------- n8n telemetry

def node_runs(data: str) -> list[tuple[str, int, str]]:
    """(node, execution ms, status) per node run from one stored execution ("flatted" JSON: values are
    indexes into a flat array). Reads only the run-data index, so payload values are never touched."""
    table = json.loads(data)
    result_data = table[int(table[0]["resultData"])]
    run_data = table[int(result_data["runData"])]
    runs = []
    for node, tasks_at in run_data.items():
        for task_at in table[int(tasks_at)]:
            task = table[int(task_at)]
            status_at = task.get("executionStatus")
            runs.append((node, int(task.get("executionTime") or 0),
                         table[int(status_at)] if status_at is not None else "unknown"))
    return runs


def _n8n_dsn() -> str:
    settings = get_settings()
    return psycopg2.extensions.make_dsn(settings.psycopg2_dsn, dbname=settings.n8n_db_name)


def fetch_telemetry(since: datetime, dsn: str | None = None) -> tuple[dict[str, dict], dict[str, bool]]:
    """Per-workflow executions and node stats since `since`, and each workflow's published flag."""
    conn = psycopg2.connect(dsn or _n8n_dsn(), connect_timeout=3, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute('SELECT id, active FROM workflow_entity')
            published = {r["id"]: r["active"] for r in cur.fetchall()}
            cur.execute('''SELECT e."workflowId" AS workflow, e.status, e."startedAt" AS started, e."stoppedAt" AS stopped,
                                  d.data
                           FROM execution_entity e LEFT JOIN execution_data d ON d."executionId" = e.id
                           WHERE e."startedAt" > %s AND e."deletedAt" IS NULL
                           ORDER BY e."startedAt" DESC LIMIT %s''', (since, MAX_EXECUTIONS))
            rows = cur.fetchall()
    finally:
        conn.close()

    stats: dict[str, dict] = defaultdict(lambda: {"executions": [], "nodes": defaultdict(lambda: [0, 0, 0])})
    for row in rows:
        wf = stats[row["workflow"]]
        wf["executions"].append((row["status"], row["started"], row["stopped"]))
        try:
            runs = node_runs(row["data"]) if row["data"] else []
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            continue    # format we can't read: the run still counts, and the row's contents are never logged
        for node, ms, status in runs:
            bucket = wf["nodes"][node]
            bucket[0] += 1
            bucket[1] += status != "success"
            bucket[2] += ms
    return stats, published


def _node_view(node: dict, bucket: list[int] | None) -> dict:
    runs, errors, total_ms = bucket or (0, 0, 0)
    return {**node, "runs": runs, "errors": errors,
            "success_rate": round(100.0 * (runs - errors) / runs, 1) if runs else None,
            "avg_ms": round(total_ms / runs, 1) if runs else None}


def _workflow_view(spec: dict, stats: dict | None, published: bool | None, available: bool) -> dict:
    structure = load_structure(spec["file"])
    executions = (stats or {}).get("executions", [])
    done = [e for e in executions if e[0] in ("success", "error", "crashed", "canceled")]
    failed = sum(e[0] != "success" for e in done)
    latencies = sorted((e[2] - e[1]).total_seconds() * 1000 for e in done if e[1] and e[2])
    nodes = []
    for node in structure["nodes"]:
        bucket = (stats or {}).get("nodes", {}).get(node["name"])
        view = _node_view(node, bucket)
        view["state"] = ("unknown" if not available else "idle" if not view["runs"]
                         else "fail" if view["errors"] else "ok")
        nodes.append(view)
    state = ("unknown" if not available else "idle" if not done else "degraded" if failed else "healthy")
    return {"key": spec["key"], "n8n_id": spec["n8n_id"], "name": spec["name"], "summary": spec["summary"],
            "published": published, "state": state, "executions": len(executions), "succeeded": len(done) - failed,
            "failed": failed, "running": len(executions) - len(done),
            "success_rate": round(100.0 * (len(done) - failed) / len(done), 1) if done else None,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "p95_latency_ms": round(quantiles(latencies, n=20, method="inclusive")[18], 1) if len(latencies) > 1
            else (round(latencies[0], 1) if latencies else None),
            "last_run_at": max((e[1] for e in executions), default=None),
            "nodes": nodes, "edges": structure["edges"], "sample_input": spec["sample_input"]}


def workflows_summary(window: str = "7d", now: datetime | None = None, dsn: str | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    hours = WINDOWS[window][0]
    since = now - timedelta(hours=hours)
    try:
        stats, published = fetch_telemetry(since, dsn)
        available = True
    except psycopg2.Error:      # the driver's message can name the host and user, so it isn't passed on
        stats, published, available = {}, {}, False
    workflows = [_workflow_view(s, stats.get(s["n8n_id"]), published.get(s["n8n_id"]), available) for s in WORKFLOWS]
    return sanitize({
        "generated_at": now, "window": window, "window_start": since, "telemetry_available": available,
        "retention_hours": RETENTION_HOURS, "window_exceeds_retention": hours > RETENTION_HOURS,
        "workflows": workflows,
    })


# --------------------------------------------------------------------------- agent catalog

def _agents_catalog() -> list[dict]:
    s = get_settings()
    chat = [s.gemini_chat_model] + [m.strip() for m in s.gemini_chat_fallback_models.split(",") if m.strip()]
    embed = s.local_embedding_model if s.embedding_provider == "local" else s.gemini_embedding_model
    return [
        {"key": "governance", "name": "Governance State Machine", "engine": "LangGraph StateGraph", "models": [],
         "summary": "Runs each seat request through contract checks, the policy review and the financial impact, "
                    "then executes it or stops for a person.",
         "model_note": "No model of its own. The advisory contract review comes from the Policy Verifier.",
         "tools": [("analyze_contract", "Loads the contract and negotiated clauses"),
                   ("check_rag_compliance", "Runs the policy review of customer documents"),
                   ("calculate_impact", "Computes the 30-day total, MRR and ARR change"),
                   ("wait_for_approval", "Opens an approval and ends the graph until a decision"),
                   ("execute_mutation", "Updates seats and ARR in one locked transaction")],
         "constraints": ["Routing is deterministic: a model can't remove a review reason or approve anything",
                         "Over $500/month on the rolling 30-day total always goes to a person (SOP-02)",
                         "Only the assigned approver can decide; ADMIN can't decide for them",
                         "Seats and ARR change only in EXECUTED, and never twice for one request"]},
        {"key": "copilot", "name": "SOP RAG Copilot", "engine": f"LangChain LCEL ({s.copilot_engine})",
         "models": chat + [embed], "summary": "Answers questions from SOPs, contracts and correspondence, citing the "
                                              "passages it used.",
         "model_note": "First chat model is primary, the others are fallbacks; the last is for embeddings.",
         "tools": [("HybridRetriever.search", "pgvector cosine search plus full-text, merged with RRF (k=60)"),
                   ("Structured answer", "Model output parsed into a fixed schema with citations"),
                   ("Quote verification", "Checks every cited quote against the source text")],
         "constraints": ["Answers only from retrieved passages, and says so when they aren't enough",
                         "A quote that isn't found verbatim is marked unverified",
                         "Advisory only: it can't approve, reject or change anything"]},
        {"key": "contract_analyzer", "name": "Contract Analyzer", "engine": "LangGraph node (rule-based)", "models": [],
         "summary": "Checks that the contract is active, belongs to the customer, and that the request stays within "
                    "the plan.", "model_note": "No model: exact checks against contract records.",
         "tools": [("Contract lookup", "Reads the contract, plan terms and unit price"),
                   ("Account standing check", "Reads the customer's account status"),
                   ("Plan range check", "Compares the new seat total with the plan's range")],
         "constraints": ["Missing, inactive or mismatched contracts are rejected",
                         "Delinquent accounts are rejected until Finance clears them",
                         "A request that crosses a plan tier is rejected"]},
        {"key": "policy_verifier", "name": "Policy Verifier", "engine": "LangGraph node + copilot", "models": chat,
         "summary": "Looks for customer-specific MSA and SOW terms, and has the copilot review them for the approver.",
         "model_note": "Uses the copilot's chat models for the review.",
         "tools": [("Customer document lookup", "Finds the customer's own legal documents in the corpus"),
                   ("Copilot contract review", "Adds an advisory review with cited clauses to the approval card")],
         "constraints": ["Customer-specific documents always require a person",
                         "A failed review or an unindexed corpus adds a review reason; it never passes silently",
                         "The review's confidence is advisory and doesn't affect routing"]},
        {"key": "sla_triage", "name": "SLA Escalation Triage", "engine": "Rules + n8n + copilot", "models": chat,
         "summary": "Applies SOP-01 clocks to tickets and hands P1 and P2 escalations to n8n for copilot triage.",
         "model_note": "The SLA decision is rule-based. Only the triage note uses a model.",
         "tools": [("evaluate_sla", "Computes response and resolution clocks for the plan"),
                   ("escalation_level", "Maps severity and budget used to who gets notified"),
                   ("N8nDispatcher", "Sends the signed escalation event to n8n")],
         "constraints": ["SLA clocks use America/Chicago business and coverage hours (SOP-01)",
                         "Only P1, P2 or explicitly requested tickets go to copilot triage",
                         "Alerts post only when a channel is configured"]},
        {"key": "collections", "name": "Collections Drafter", "engine": "Rule-based service", "models": [],
         "summary": "Summarises a delinquent account and prepares a notice for Finance to review.",
         "model_note": "No model: the notice comes from a fixed template.",
         "tools": [("list_overdue", "Lists overdue invoices for the n8n schedule"),
                   ("summarize_delinquency", "Builds the account summary and notice draft")],
         "constraints": ["The notice is a draft and is never sent automatically",
                         "Notice terms follow MSA §6, including the 15-day cure period"]},
    ]


_TRANSITION_TONE = {"FAILED": "fail", "WAITING_FOR_APPROVAL": "wait", "REJECTED": "idle"}


def _trace(at, title: str, detail: str, tone: str = "ok") -> dict:
    return {"at": at, "title": title, "detail": detail[:220], "tone": tone}


def _governance_traces(runs: list[dict], since: datetime) -> list[dict]:
    traces = []
    for r in runs:
        for h in r["history"]:
            at = datetime.fromisoformat(h["at"])
            if at > since:
                traces.append(_trace(at, h["to"].replace("_", " ").capitalize(),
                                     f'{r["request_id"]}: {h.get("reason") or ""}',
                                     _TRANSITION_TONE.get(h["to"], "ok")))
    return traces


def _contract_traces(runs: list[dict]) -> list[dict]:
    traces = []
    for r in runs:
        contract, decision = r["context"].get("contract"), r["context"].get("decision") or {}
        if contract:
            traces.append(_trace(r["updated_at"], f'{r["customer_id"]}: checks passed',
                                 f'{contract["plan_tier"].title()} plan, {contract.get("committed_seats", 0):,} committed '
                                 f'seats ({r["request_id"]})'))
        elif "analyze_contract" in r["context"].get("graph_trace", []):
            traces.append(_trace(r["updated_at"], f'{r["customer_id"]}: rejected',
                                 decision.get("message") or decision.get("code") or "Failed a contract check", "idle"))
    return traces


def _policy_traces(runs: list[dict]) -> list[dict]:
    traces = []
    for r in runs:
        clause = r["context"].get("clause_check")
        if clause is None:
            continue
        documents, review = clause.get("documents") or [], clause.get("review") or {}
        if not documents:
            traces.append(_trace(r["updated_at"], f'{r["customer_id"]}: no customer documents',
                                 "Standard terms apply, so no review was needed", "idle"))
        elif "error" in review or not review:
            traces.append(_trace(r["updated_at"], f'{r["customer_id"]}: review unavailable',
                                 f"{len(documents)} document(s) found; routed to a person without a review", "wait"))
        else:
            traces.append(_trace(r["updated_at"], f'{r["customer_id"]}: documents reviewed',
                                 f"{len(documents)} document(s); {review.get('confidence', 'unrated')} confidence "
                                 "(advisory); routed to a person", "wait"))
    return traces


def _copilot_trace(e: dict) -> dict:
    p = e["payload"] or {}
    if e["status"] != "PROCESSED":
        return _trace(e["created_at"], "Call failed", "The model call failed, so no answer was returned", "fail")
    title = ("Not enough context" if p.get("insufficient_context") else
             "Grounded answer" if p.get("grounded") else "Answer not verified as grounded")
    return _trace(e["created_at"], title, f'{p.get("citations", 0)} citation(s), {p.get("model") or "model unknown"}',
                  "ok" if p.get("grounded") else "wait")


def _sla_trace(e: dict) -> dict:
    d = (e["payload"] or {}).get("data") or {}
    copilot = ", copilot triage requested" if d.get("request_copilot") else ""
    severity = d.get("reported_severity") or "recorded severity"
    return _trace(e["created_at"], f'{d.get("ticket_id") or e["subject_id"]}: SLA check',
                  f"Evaluated at {severity}{copilot}", "ok" if e["status"] == "PROCESSED" else "fail")


def _collections_trace(e: dict) -> dict:
    d = (e["payload"] or {}).get("data") or {}
    outcome = "Notice drafted, not sent" if d.get("draft_notice") else "Account summarised"
    customer = f' ({d["customer_id"]})' if d.get("customer_id") else ""
    return _trace(e["created_at"], f'{d.get("invoice_id") or e["subject_id"]}{customer}',
                  outcome, "ok" if e["status"] == "PROCESSED" else "fail")


def _events(cur, event_type: str, since: datetime, limit: int = 200) -> list[dict]:
    cur.execute("""SELECT created_at, subject_id, status, payload FROM automation_events
                   WHERE event_type = %s AND created_at > %s AND source <> 'pytest'
                   ORDER BY created_at DESC, id DESC LIMIT %s""", (event_type, since, limit))
    return cur.fetchall()


def agents_summary(cur, window: str = "7d", now: datetime | None = None, trace_limit: int = 5) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=WINDOWS[window][0])
    cur.execute("""SELECT request_id, customer_id, state, context, history, created_at, updated_at FROM governance_runs
                   WHERE updated_at > %s ORDER BY updated_at DESC LIMIT 100""", (since,))
    runs = cur.fetchall()
    cur.execute("""SELECT count(*) AS n FROM approval_requests WHERE status = 'PENDING' AND expires_at > %s""", (now,))
    pending = cur.fetchone()["n"]
    cur.execute("""SELECT count(*) AS n FROM governance_runs g JOIN approval_requests a ON a.run_id = g.run_id
                   WHERE a.status = 'PENDING' AND a.expires_at > %s
                     AND jsonb_array_length(coalesce(g.context->'clause_check'->'documents', '[]'::jsonb)) > 0""", (now,))
    pending_documents = cur.fetchone()["n"]

    copilot_events = _events(cur, "copilot.ask", since)
    sla_events = _events(cur, "ticket.sla_check", since)
    invoice_events = _events(cur, "invoice.delinquent", since)
    contract_traces, policy_traces = _contract_traces(runs), _policy_traces(runs)
    live = {
        "governance": (len(runs), _governance_traces(runs, since), pending, runs and runs[0]["state"] == "FAILED"),
        "copilot": (len(copilot_events), [_copilot_trace(e) for e in copilot_events], 0,
                    copilot_events and copilot_events[0]["status"] != "PROCESSED"),
        "contract_analyzer": (len(contract_traces), contract_traces, 0, False),
        "policy_verifier": (len(policy_traces), policy_traces, pending_documents, False),
        "sla_triage": (len(sla_events), [_sla_trace(e) for e in sla_events], 0,
                       sla_events and sla_events[0]["status"] != "PROCESSED"),
        "collections": (len(invoice_events), [_collections_trace(e) for e in invoice_events], 0,
                        invoice_events and invoice_events[0]["status"] != "PROCESSED"),
    }

    agents = []
    for spec in _agents_catalog():
        count, traces, waiting, last_failed = live[spec["key"]]
        traces = sorted(traces, key=lambda t: t["at"], reverse=True)
        if last_failed:
            state, detail = "error", "Most recent run failed"
        elif waiting:
            state, detail = "in_review", f"{waiting} approval{'s' if waiting != 1 else ''} waiting on a person"
        elif count:
            state, detail = "active", f"{count} run{'s' if count != 1 else ''} in this window"
        else:
            state, detail = "idle", "No runs in this window"
        agents.append({"key": spec["key"], "name": spec["name"], "engine": spec["engine"], "summary": spec["summary"],
                       "state": state, "state_detail": detail, "uses_llm": bool(spec["models"]),
                       "models": spec["models"], "model_note": spec["model_note"],
                       "tools": [{"name": n, "description": d} for n, d in spec["tools"]],
                       "constraints": spec["constraints"], "runs": count,
                       "last_run_at": traces[0]["at"] if traces else None, "traces": traces[:trace_limit]})
    return sanitize({"generated_at": now, "window": window, "window_start": since, "agents": agents,
                     "state_counts": {s: sum(a["state"] == s for a in agents)
                                      for s in ("active", "in_review", "idle", "error")}})
