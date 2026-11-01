"""Generate the NexusFlow Autopilot n8n workflow exports (n8n 2.x import format).

    python n8n_workflows/build_workflows.py        # writes the three *_workflow.json files

The signing/verification JavaScript is shared, so all workflows speak exactly the
same wire protocol as app.core.security:
    X-NexusFlow-Timestamp + X-NexusFlow-Signature: sha256=HMAC(secret, "<ts>.<raw body>")
Secrets never appear in these files: Code nodes read NEXUSFLOW_WEBHOOK_SECRET and
NEXUSFLOW_API_BASE_URL from the n8n container environment (docker-compose.yml).

Import + publish (run from the repo root; ids: nxfP1TicketEscal1, nxfSeatExpansion1, nxfDelinquentInv1,
nxfHitlApproval1):
    docker exec nexusflow-n8n n8n import:workflow --separate --input=/workflows
    docker exec nexusflow-n8n n8n publish:workflow --id=<id>     # for each id below
    docker restart nexusflow-n8n
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent
NS = uuid.UUID("7c0f3a5e-2b4d-4e8a-9f61-0d5c3b2a1e90")  # stable ids across rebuilds
TOLERANCE_S = 300

# --------------------------------------------------------------------------- shared JavaScript

VERIFY_JS = f"""// Verify the NexusFlow HMAC-SHA256 signature over the exact raw request body.
const crypto = require('crypto');
const item = $input.first();
const headers = item.json.headers || {{}};
const ts = String(headers['x-nexusflow-timestamp'] || '');
const sig = String(headers['x-nexusflow-signature'] || '').replace(/^sha256=/, '');
const raw = item.binary && item.binary.data
  ? (await this.helpers.getBinaryDataBuffer(0, 'data')).toString('utf8')
  : '';
const secret = $env.NEXUSFLOW_WEBHOOK_SECRET || '';
let reason = null;
let event = null;
if (!secret) reason = 'n8n is missing NEXUSFLOW_WEBHOOK_SECRET';
else if (!ts || !sig) reason = 'missing signature headers';
else if (!/^\\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > {TOLERANCE_S}) reason = 'stale or invalid timestamp';
else {{
  const expected = Buffer.from(crypto.createHmac('sha256', secret).update(`${{ts}}.${{raw}}`).digest('hex'), 'hex');
  const given = Buffer.from(/^[0-9a-f]{{64}}$/i.test(sig) ? sig : '', 'hex');
  if (given.length !== expected.length || !crypto.timingSafeEqual(given, expected)) reason = 'signature mismatch';
}}
if (!reason) {{
  try {{ event = JSON.parse(raw); }} catch (e) {{ reason = 'body is not valid JSON'; }}
}}
return [{{ json: {{ valid: !reason, reason, event }} }}];
"""

# Prepended to every Code node that calls the FastAPI backend.
SIGN_JS = """const crypto = require('crypto');
// Envelope + signature helpers (same wire format as app/core/security.py)
const envelope = (event_type, data, event_id) => ({
  event_id: event_id || crypto.randomUUID(),
  event_type,
  occurred_at: new Date().toISOString(),
  source: 'n8n',
  data,
});
const signed = (payload) => {
  const body = payload === null ? '' : JSON.stringify(payload);
  const ts = Math.floor(Date.now() / 1000).toString();
  const signature = 'sha256=' + crypto.createHmac('sha256', $env.NEXUSFLOW_WEBHOOK_SECRET).update(`${ts}.${body}`).digest('hex');
  return { body, ts, signature, event_id: (payload && payload.event_id) || '', event_type: (payload && payload.event_type) || '' };
};
"""

# --------------------------------------------------------------------------- node builders


def _id(workflow: str, name: str) -> str:
    return str(uuid.uuid5(NS, f"{workflow}/{name}"))


class Workflow:
    def __init__(self, key: str, name: str, workflow_id: str, description: str):
        self.key, self.name, self.workflow_id, self.description = key, name, workflow_id, description
        self.nodes: list[dict] = []
        self.connections: dict[str, dict] = {}

    def add(self, name: str, type_: str, version: float, position: tuple[int, int], parameters: dict, **extra) -> str:
        node = {"id": _id(self.key, name), "name": name, "type": type_, "typeVersion": version,
                "position": list(position), "parameters": parameters, **extra}
        self.nodes.append(node)
        return name

    def connect(self, src: str, dst: str, output: int = 0) -> None:
        outputs = self.connections.setdefault(src, {"main": []})["main"]
        while len(outputs) <= output:
            outputs.append([])
        outputs[output].append({"node": dst, "type": "main", "index": 0})

    # ---- typed helpers

    def webhook(self, name, path, pos):
        return self.add(name, "n8n-nodes-base.webhook", 2.1, pos,
                        {"httpMethod": "POST", "path": path, "responseMode": "responseNode",
                         "options": {"rawBody": True}},
                        webhookId=_id(self.key, "webhook:" + path),
                        notes="Signed with X-NexusFlow-Timestamp / X-NexusFlow-Signature (HMAC-SHA256 of '<ts>.<raw body>').")

    def code(self, name, js, pos, per_item=False, notes=None):
        params = {"jsCode": js}
        if per_item:
            params = {"mode": "runOnceForEachItem", "jsCode": js}
        extra = {"notes": notes} if notes else {}
        return self.add(name, "n8n-nodes-base.code", 2, pos, params, **extra)

    def if_true(self, name, left_expr, pos):
        return self.add(name, "n8n-nodes-base.if", 2.2, pos, {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                "conditions": [{"id": _id(self.key, "cond:" + name), "leftValue": left_expr, "rightValue": "",
                                "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                "combinator": "and",
            },
            "options": {},
        })

    def respond(self, name, body_expr, code, pos):
        return self.add(name, "n8n-nodes-base.respondToWebhook", 1.4, pos, {
            "respondWith": "json", "responseBody": body_expr, "options": {"responseCode": code}})

    def signed_http(self, name, method, api_path, pos, timeout_ms=30000, notes=None):
        """HTTP Request to the backend using the signed body/headers a Code node put in $json.req."""
        params = {
            "method": method,
            "url": f"={{{{ $env.NEXUSFLOW_API_BASE_URL }}}}{api_path}",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "X-NexusFlow-Timestamp", "value": "={{ $json.req.ts }}"},
                {"name": "X-NexusFlow-Signature", "value": "={{ $json.req.signature }}"},
                {"name": "X-NexusFlow-Event-Id", "value": "={{ $json.req.event_id }}"},
                {"name": "X-NexusFlow-Event-Type", "value": "={{ $json.req.event_type }}"},
            ]},
            # responseFormat json: autodetect leaves application/problem+json error bodies unparsed
            "options": {"timeout": timeout_ms,
                        "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}}},
        }
        if method != "GET":
            params.update({"sendBody": True, "contentType": "raw", "rawContentType": "application/json",
                           "body": "={{ $json.req.body }}"})
        # continueRegularOutput: a timeout or connection error becomes an item (no statusCode) instead of
        # halting the run, so every workflow still reaches its fallback branch and its callback
        return self.add(name, "n8n-nodes-base.httpRequest", 4.2, pos, params, onError="continueRegularOutput",
                        notes=notes or "Full response + never error: downstream nodes branch on statusCode.")

    def export(self) -> dict:
        names = {n["name"] for n in self.nodes}
        for src, conns in self.connections.items():
            assert src in names, src
            for branch in conns["main"]:
                for c in branch:
                    assert c["node"] in names, c["node"]
        return {
            "id": self.workflow_id,
            "name": self.name,
            "active": False,
            "nodes": self.nodes,
            "connections": self.connections,
            "settings": {"executionOrder": "v1", "saveDataErrorExecution": "all",
                         "saveDataSuccessExecution": "all", "callerPolicy": "workflowsFromSameOwner"},
            "pinData": {},
            "meta": {"templateCredsSetupCompleted": True, "description": self.description},
            "tags": [],
        }


def callback_js(workflow: str, action_expr: str, subject_expr: str, outcome_expr: str,
                correlation_expr: str, failed_expr: str = "false") -> str:
    return SIGN_JS + f"""
const failed = {failed_expr};
const payload = envelope(failed ? 'workflow.failed' : 'workflow.completed', {{
  workflow: '{workflow}',
  correlation_id: {correlation_expr} || null,
  execution_id: String($execution.id),
  action: {action_expr},
  subject_id: {subject_expr} || null,
  outcome: {outcome_expr},
}});
return [{{ json: {{ ...$json, req: signed(payload) }} }}];
"""


REJECT_BODY = "={{ JSON.stringify({ accepted: false, reason: $json.reason }) }}"

# --------------------------------------------------------------------------- workflow 1


def p1_ticket_escalation() -> Workflow:
    wf = Workflow("p1", "NexusFlow — P1/P2 Ticket Escalation", "nxfP1TicketEscal1",
                  "FastAPI dispatches P1/P2 (or copilot-flagged) tickets here. Acks 202, asks the RAG copilot "
                  "for the governing SOP steps, formats Slack/Discord/generic alert payloads, optionally posts "
                  "to NEXUSFLOW_ALERT_WEBHOOK_URL, and reports back to /api/v1/webhooks/n8n/callback.")
    hook = wf.webhook("Escalation event received", "nexusflow/p1-ticket-escalation", (0, 300))
    verify = wf.code("Verify signature", VERIFY_JS, (220, 300))
    valid = wf.if_true("Signature valid?", "={{ $json.valid }}", (440, 300))
    reject = wf.respond("Reject: bad signature", REJECT_BODY, 401, (660, 460))
    ack = wf.respond("Ack: 202 accepted",
                     "={{ JSON.stringify({ accepted: true, event_id: $json.event.event_id, "
                     "ticket_id: $json.event.data.ticket_id }) }}", 202, (660, 200))
    build = wf.code("Build copilot request", SIGN_JS + """
const event = $('Verify signature').first().json.event;
const question = event.data.copilot_question
  || `How should ${event.data.effective_severity} ticket ${event.data.ticket_id} be handled under SOP-01?`;
return [{ json: { event, req: signed({ question, top_k: 6 }) } }];
""", (880, 200))
    ask = wf.signed_http("Ask RAG copilot", "POST", "/api/v1/copilot/ask", (1100, 200), timeout_ms=240000,
                         notes="Gemini-backed: usually 10-30 s, but model fallback can take ~3 min in an outage. "
                               "A timeout or non-200 still produces an alert with a manual-SOP fallback.")
    fmt = wf.code("Format alert payloads", """
const ev = $('Build copilot request').first().json.event.data;
const res = $json;
const ok = res.statusCode === 200 && res.body && typeof res.body.answer === 'string';
const cop = ok ? res.body : null;
const sources = ok ? cop.citations.map(c => `${c.doc_id} — ${c.section}`) : [];
const uniqueSources = [...new Set(sources)];
const recommendation = ok
  ? cop.answer
  : `RAG copilot unavailable (${res.statusCode ? 'HTTP ' + res.statusCode : 'no response: ' + ((res.error && res.error.message) || 'timed out')}). `
    + 'Follow SOP-01 §3 manually and page on-call Support + SRE.';
const sla = ev.sla;
const icon = ev.effective_severity === 'P1' ? ':rotating_light:' : ':warning:';
const title = `${ev.effective_severity} ${ev.ticket_id} — ${ev.company_name} (${ev.customer_id}, ${ev.plan_tier})`;
const slaLine = `${sla.clock} clock · ${sla.resolution_budget_consumed_pct}% of ${sla.resolution_target_minutes} min resolution budget`
  + (sla.breached ? ' · BREACHED' : '') + ` · due ${sla.resolution_due_at}`;
const notify = ev.escalation.notify.map(c => `${c.name} (${c.role}, ${c.employee_id})`).join(', ') || 'standard queue';
const owner = ev.assigned_to ? `${ev.assigned_to.name} (${ev.assigned_to.employee_id})` : 'unassigned';

const slack = {
  text: `${icon} ${title}: ${ev.escalation.level}`,
  blocks: [
    { type: 'header', text: { type: 'plain_text', text: title.slice(0, 150) } },
    { type: 'section', fields: [
      { type: 'mrkdwn', text: `*Escalation:* ${ev.escalation.level}` },
      { type: 'mrkdwn', text: `*Status:* ${ev.status} (recorded ${ev.recorded_severity})` },
      { type: 'mrkdwn', text: `*Owner:* ${owner}` },
      { type: 'mrkdwn', text: `*Notify:* ${notify}` },
    ] },
    { type: 'section', text: { type: 'mrkdwn', text: `*SLA:* ${slaLine}\\n*Why:* ${ev.escalation.reason}` } },
    { type: 'section', text: { type: 'mrkdwn', text: `*Copilot recommendation*${ok && !cop.grounded ? ' (NOT verified as grounded)' : ''}:\\n${recommendation}`.slice(0, 2900) } },
    { type: 'context', elements: [{ type: 'mrkdwn', text: uniqueSources.length ? `Sources: ${uniqueSources.join(' · ')}` : 'No sources' }] },
  ],
};
const discord = {
  content: `${ev.effective_severity === 'P1' ? '🚨' : '⚠️'} **${title}** — ${ev.escalation.level}`,
  embeds: [{
    title: `${ev.ticket_id}: ${ev.subject || ''}`.slice(0, 256),
    description: recommendation.slice(0, 4000),
    color: ev.effective_severity === 'P1' ? 15158332 : 15105570,
    fields: [
      { name: 'SLA', value: slaLine.slice(0, 1024) },
      { name: 'Notify', value: notify.slice(0, 1024) },
      { name: 'Sources', value: (uniqueSources.join('\\n') || 'none').slice(0, 1024) },
    ],
  }],
};
const generic = {
  ticket_id: ev.ticket_id, customer_id: ev.customer_id, severity: ev.effective_severity,
  escalation_level: ev.escalation.level, breached: sla.breached, resolution_due_at: sla.resolution_due_at,
  notify: ev.escalation.notify, recommendation, sources: uniqueSources,
  copilot: ok ? { grounded: cop.grounded, confidence: cop.confidence, model: cop.model } : { error: res.statusCode },
};
return [{ json: { ticket_id: ev.ticket_id, correlation_id: ev.correlation_id, copilot_ok: ok,
                  alert: { slack, discord, generic } } }];
""", (1320, 200))
    channel = wf.if_true("Alert channel configured?", "={{ !!$env.NEXUSFLOW_ALERT_WEBHOOK_URL }}", (1540, 200))
    post = wf.add("Post alert to channel", "n8n-nodes-base.httpRequest", 4.2, (1760, 100), {
        "method": "POST", "url": "={{ $env.NEXUSFLOW_ALERT_WEBHOOK_URL }}",
        "sendBody": True, "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify(($env.NEXUSFLOW_ALERT_FORMAT || 'slack') === 'discord' ? $json.alert.discord : $json.alert.slack) }}",
        "options": {"timeout": 10000, "response": {"response": {"fullResponse": True, "neverError": True}}},
    }, notes="Slack incoming webhook (default) or Discord webhook (NEXUSFLOW_ALERT_FORMAT=discord).")
    cb = wf.code("Build callback", callback_js(
        "p1_ticket_escalation",
        action_expr="'escalation_alert_formatted'",
        subject_expr="$('Format alert payloads').first().json.ticket_id",
        correlation_expr="$('Format alert payloads').first().json.correlation_id",
        outcome_expr="""{
    copilot_ok: $('Format alert payloads').first().json.copilot_ok,
    channel_delivery: $('Post alert to channel').isExecuted
      ? { attempted: true, status: $('Post alert to channel').first().json.statusCode }
      : { attempted: false, reason: 'NEXUSFLOW_ALERT_WEBHOOK_URL not set' },
    alert: $('Format alert payloads').first().json.alert,
  }""",
    ), (1980, 200))
    report = wf.signed_http("Report to NexusFlow", "POST", "/api/v1/webhooks/n8n/callback", (2200, 200))

    wf.connect(hook, verify); wf.connect(verify, valid)
    wf.connect(valid, ack, 0); wf.connect(valid, reject, 1)
    wf.connect(ack, build); wf.connect(build, ask); wf.connect(ask, fmt); wf.connect(fmt, channel)
    wf.connect(channel, post, 0); wf.connect(channel, cb, 1); wf.connect(post, cb)
    wf.connect(cb, report)
    return wf


# --------------------------------------------------------------------------- workflow 2


def seat_expansion_trigger() -> Workflow:
    wf = Workflow("seat", "NexusFlow — Seat Expansion Trigger", "nxfSeatExpansion1",
                  "Captures seat-add requests (signed), sends them to FastAPI for the SOP-02 $500/month rolling "
                  "30-day check, formats a Finance approval request or a customer-authorization request, logs the "
                  "outcome to the callback endpoint and responds synchronously with the decision.")
    hook = wf.webhook("Seat request received", "nexusflow/seat-request", (0, 300))
    verify = wf.code("Verify signature", VERIFY_JS, (220, 300))
    valid = wf.if_true("Signature valid?", "={{ $json.valid }}", (440, 300))
    reject = wf.respond("Reject: bad signature", REJECT_BODY, 401, (660, 480))
    norm = wf.code("Normalize seat request", SIGN_JS + """
// Accept a full seat.requested envelope or a bare request object from a form/CRM.
const inbound = $json.event;
const d = inbound && inbound.data ? inbound.data : inbound;
const data = {
  request_id: d.request_id || `SRQ-${crypto.randomUUID()}`,
  customer_id: d.customer_id,
  contract_id: d.contract_id,
  added_seats: Number(d.added_seats),
  request_type: d.request_type || 'UNSPECIFIED',
  requester_email: d.requester_email,
  requested_at: d.requested_at || new Date().toISOString(),
};
for (const k of ['desired_activation_date', 'justification']) if (d[k]) data[k] = d[k];
// Keep the caller's event_id so retries stay idempotent end to end.
const payload = envelope('seat.requested', data, inbound && inbound.data ? inbound.event_id : undefined);
return [{ json: { request: data, correlation_id: payload.event_id, req: signed(payload) } }];
""", (660, 200))
    evaluate = wf.signed_http("Evaluate $500 threshold", "POST", "/api/v1/webhooks/seats/request", (880, 200))
    ok = wf.if_true("Evaluated?", "={{ $json.statusCode >= 200 && $json.statusCode < 300 }}", (1100, 200))
    failed = wf.respond("Respond: rejected by backend",
                        "={{ JSON.stringify({ result: 'rejected', status: $json.statusCode, detail: $json.body.detail || $json.body }) }}",
                        "={{ $json.statusCode >= 400 && $json.statusCode < 600 ? $json.statusCode : 502 }}", (1320, 420))
    needs = wf.if_true("Finance approval required?", "={{ $json.body.requires_finance_approval }}", (1320, 200))
    finance = wf.code("Format Finance approval request", """
const d = $json.body;
const c = d.calculation;
const money = v => Number(v).toFixed(2);
const approval_request = {
  type: 'FINANCE_SEAT_APPROVAL',
  status: 'AWAITING_FINANCE_DECISION',
  request_id: d.request_id,
  approver: d.approver,
  customer: { customer_id: d.customer_id, company_name: d.company_name, contract_id: d.contract_id, plan_tier: d.plan_tier },
  requested_seats: d.added_seats,
  aggregate_seats_30d: c.aggregate_seats_30d,
  prior_requests_in_window: c.prior_requests_in_window,
  calculation: `${c.aggregate_seats_30d} seats × $${money(c.annual_unit_price)} ÷ 12 = $${money(c.aggregate_monthly_30d)}/month (threshold $${money(c.threshold_monthly)})`,
  decision_options: ['APPROVE', 'REJECT', 'REQUEST_CLARIFICATION'],
  approval_validity: 'Approval expires 7 calendar days after the decision if unused (SOP-02 §3.4)',
  blocking_items: [d.requires_plan_amendment && 'Signed plan amendment', d.requires_clarification && 'Written clarification of request type'].filter(Boolean),
  notes: d.notes,
};
return [{ json: { decision: d, approval_request, action: 'finance_approval_requested' } }];
""", (1540, 100))
    customer = wf.code("Format customer authorization request", """
const d = $json.body;
const c = d.calculation;
const approval_request = {
  type: 'CUSTOMER_AUTHORIZATION',
  status: d.status,
  request_id: d.request_id,
  customer: { customer_id: d.customer_id, company_name: d.company_name, contract_id: d.contract_id, plan_tier: d.plan_tier },
  requested_seats: d.added_seats,
  monthly_equivalent: Number(c.aggregate_monthly_30d).toFixed(2),
  finance_approval: 'Not required: aggregate is at or below $500.00/month',
  required_before_provisioning: ['Explicit written customer authorization',
    d.requires_plan_amendment && 'Signed plan amendment', d.requires_clarification && 'Clarify purchase vs reservation'].filter(Boolean),
  notes: d.notes,
};
return [{ json: { decision: d, approval_request, action: 'customer_authorization_requested' } }];
""", (1540, 300))
    cb = wf.code("Build callback", callback_js(
        "seat_expansion_trigger",
        action_expr="$json.action",
        subject_expr="$json.decision.request_id",
        correlation_expr="$('Normalize seat request').first().json.correlation_id",
        outcome_expr="{ status: $json.decision.status, requires_finance_approval: $json.decision.requires_finance_approval, approval_request: $json.approval_request }",
    ), (1760, 200))
    report = wf.signed_http("Report to NexusFlow", "POST", "/api/v1/webhooks/n8n/callback", (1980, 200))
    done = wf.respond("Respond: decision",
                      "={{ JSON.stringify({ result: 'evaluated', callback_status: $json.statusCode, "
                      "decision: $('Build callback').first().json.decision, "
                      "approval_request: $('Build callback').first().json.approval_request }) }}", 200, (2200, 200))

    wf.connect(hook, verify); wf.connect(verify, valid)
    wf.connect(valid, norm, 0); wf.connect(valid, reject, 1)
    wf.connect(norm, evaluate); wf.connect(evaluate, ok)
    wf.connect(ok, needs, 0); wf.connect(ok, failed, 1)
    wf.connect(needs, finance, 0); wf.connect(needs, customer, 1)
    wf.connect(finance, cb); wf.connect(customer, cb)
    wf.connect(cb, report); wf.connect(report, done)
    return wf


# --------------------------------------------------------------------------- workflow 3


def delinquent_invoice_reconciliation() -> Workflow:
    wf = Workflow("delinquent", "NexusFlow — Delinquent Invoice Reconciliation", "nxfDelinquentInv1",
                  "Weekdays 14:00 UTC (09:00 America/Chicago in CDT) polls overdue invoices, or receives a signed "
                  "delinquency alert. Each invoice goes to FastAPI for the account summary and a collection-notice "
                  "DRAFT (never sent), and the action is logged via the callback endpoint.")
    sched = wf.add("Weekdays 09:00 Chicago", "n8n-nodes-base.scheduleTrigger", 1.2, (0, 100), {
        "rule": {"interval": [{"field": "cronExpression", "expression": "0 14 * * 1-5"}]}},
        notes="Cron runs in GENERIC_TIMEZONE (UTC). 14:00 UTC = 09:00 CDT / 08:00 CST.")
    on_demand = wf.add("Run on demand", "n8n-nodes-base.executeWorkflowTrigger", 1.1, (0, -60),
                       {"inputSource": "passthrough"},
                       notes="Lets the CLI (n8n execute --id=...) or another workflow trigger a reconciliation run now.")
    poll_sign = wf.code("Sign overdue poll", SIGN_JS + """
return [{ json: { req: signed(null) } }];
""", (220, 100))
    poll = wf.signed_http("Fetch overdue invoices", "GET", "/api/v1/webhooks/invoices/overdue", (440, 100))
    fan = wf.code("Fan out overdue invoices", """
const res = $input.first().json;
if (res.statusCode !== 200) throw new Error(`Overdue poll failed: HTTP ${res.statusCode}`);
return res.body.invoices.map(inv => ({ json: {
  invoice_id: inv.invoice_id, customer_id: inv.customer_id, as_of: res.body.as_of,
  trigger: 'schedule', correlation_id: null,
} }));
""", (660, 100))

    hook = wf.webhook("Delinquency alert received", "nexusflow/delinquent-invoice", (0, 420))
    verify = wf.code("Verify signature", VERIFY_JS, (220, 420))
    valid = wf.if_true("Signature valid?", "={{ $json.valid }}", (440, 420))
    reject = wf.respond("Reject: bad signature", REJECT_BODY, 401, (660, 580))
    ack = wf.respond("Ack: 202 accepted",
                     "={{ JSON.stringify({ accepted: true, event_id: $json.event.event_id }) }}", 202, (660, 420))
    norm = wf.code("Normalize alert", """
const ev = $('Verify signature').first().json.event;
const d = ev.data || ev;
return [{ json: { invoice_id: d.invoice_id, customer_id: d.customer_id || null, as_of: d.as_of || null,
                  trigger: 'webhook', correlation_id: ev.event_id || null } }];
""", (880, 420))

    build = wf.code("Build delinquency event", SIGN_JS + """
const data = { invoice_id: $json.invoice_id, draft_notice: true };
if ($json.customer_id) data.customer_id = $json.customer_id;
if ($json.as_of) data.as_of = $json.as_of;
const type = $json.trigger === 'schedule' ? 'invoice.overdue_poll' : 'invoice.delinquent';
return { json: { ctx: $json, req: signed(envelope(type, data)) } };
""", (1100, 260), per_item=True)
    summarize = wf.signed_http("Get account summary & draft notice", "POST",
                               "/api/v1/webhooks/invoices/delinquent", (1320, 260))
    log_js = callback_js(
        "delinquent_invoice_reconciliation",
        action_expr="ok ? 'collection_notice_drafted' : 'reconciliation_failed'",
        subject_expr="ctx.invoice_id",
        correlation_expr="ctx.correlation_id",
        failed_expr="!ok",
        outcome_expr="""ok ? {
    trigger: ctx.trigger,
    invoice: r.body.invoice,
    account: { customer_status: r.body.account.customer_status, contract_status: r.body.account.contract_status,
               total_outstanding: r.body.account.total_outstanding, csm: r.body.account.csm },
    notice: r.body.notice && { status: r.body.notice.status, to: r.body.notice.to, subject: r.body.notice.subject,
                               illustrative_cure_deadline: r.body.notice.illustrative_cure_deadline,
                               requires_review_by: r.body.notice.requires_review_by.employee_id },
    recommended_actions: r.body.recommended_actions,
  } : { trigger: ctx.trigger, status: r.statusCode, error: r.body.detail || r.body }""",
    )
    # per-item: unwrap the single item the helper returns
    log_js = log_js.replace("return [{ json: { ...$json, req: signed(payload) } }];",
                            "return { json: { req: signed(payload) } };")
    log = wf.code("Build action log", """
const ctx = $('Build delinquency event').item.json.ctx;
const r = $json;
const ok = r.statusCode === 200;
""" + log_js, (1540, 260), per_item=True)
    report = wf.signed_http("Log action in NexusFlow", "POST", "/api/v1/webhooks/n8n/callback", (1760, 260))

    wf.connect(sched, poll_sign); wf.connect(on_demand, poll_sign); wf.connect(poll_sign, poll); wf.connect(poll, fan); wf.connect(fan, build)
    wf.connect(hook, verify); wf.connect(verify, valid)
    wf.connect(valid, ack, 0); wf.connect(valid, reject, 1)
    wf.connect(ack, norm); wf.connect(norm, build)
    wf.connect(build, summarize); wf.connect(summarize, log); wf.connect(log, report)
    return wf


# --------------------------------------------------------------------------- workflow 4


def hitl_approval_notification() -> Workflow:
    wf = Workflow("hitl", "NexusFlow — HITL Approval Notifications", "nxfHitlApproval1",
                  "Governance agent events. approval.requested: builds the interactive approval card (Slack/Discord/"
                  "generic) and a private approver payload holding the single-use token; approval.finalized: formats "
                  "the outcome. The shared channel never receives the token. A second, signed webhook relays an "
                  "approver's decision to POST /api/v1/approvals/{token}/action. Note: like any value n8n "
                  "delivers, the token is kept in n8n execution data until EXECUTIONS_DATA_MAX_AGE prunes it.")

    hook = wf.webhook("Approval event received", "nexusflow/approval-events", (0, 300))
    verify = wf.code("Verify signature", VERIFY_JS, (220, 300))
    valid = wf.if_true("Signature valid?", "={{ $json.valid }}", (440, 300))
    reject = wf.respond("Reject: bad signature", REJECT_BODY, 401, (660, 480))
    ack = wf.respond("Ack: 202 accepted",
                     "={{ JSON.stringify({ accepted: true, event_id: $json.event.event_id, "
                     "event_type: $json.event.event_type }) }}", 202, (660, 300))
    kind = wf.if_true("Approval requested?",
                      "={{ $('Verify signature').first().json.event.event_type === 'approval.requested' }}", (880, 300))
    card = wf.code("Build approval card", """
const ev = $('Verify signature').first().json.event.data;
const c = ev.card;
const facts = c.facts.map(f => `*${f.label}:* ${f.value}`).join('\\n');
const reasons = c.reasons.map(r => `• ${r.text}`).join('\\n');
const review = c.contract_review && c.contract_review.answer
  ? `*Contract review (advisory, ${c.contract_review.grounded ? 'grounded' : 'NOT grounded'}):* ${c.contract_review.answer}`
  : (c.contract_documents.length ? '*Contract review:* unavailable — review the documents manually' : '');
// Shared channel: no token, no callback URL. Buttons carry only ids; the decision link goes to the approver privately.
const slack = {
  text: `:memo: Approval needed: ${c.title}`,
  blocks: [
    { type: 'header', text: { type: 'plain_text', text: c.title.slice(0, 150) } },
    { type: 'section', text: { type: 'mrkdwn', text: `${c.summary}\\n\\n${facts}`.slice(0, 2900) } },
    { type: 'section', text: { type: 'mrkdwn', text: `*Why a human is required:*\\n${reasons}` } },
    ...(review ? [{ type: 'section', text: { type: 'mrkdwn', text: review.slice(0, 2900) } }] : []),
    { type: 'actions', block_id: `nexusflow_approval_${ev.approval_id}`, elements: [
      { type: 'button', style: 'primary', text: { type: 'plain_text', text: 'Approve' }, action_id: 'nexusflow_approve',
        value: JSON.stringify({ approval_id: ev.approval_id, action: 'APPROVE' }) },
      { type: 'button', style: 'danger', text: { type: 'plain_text', text: 'Reject' }, action_id: 'nexusflow_reject',
        value: JSON.stringify({ approval_id: ev.approval_id, action: 'REJECT' }) },
    ] },
    { type: 'context', elements: [{ type: 'mrkdwn',
      text: `Assigned to ${c.approver.name} (${c.approver.employee_id}) · expires ${ev.expires_at} · decision link sent privately` }] },
  ],
};
const discord = {
  content: `📝 **Approval needed** — ${c.title}`,
  embeds: [{ title: ev.request_id, description: c.summary.slice(0, 4000), color: 15844367,
             fields: c.facts.slice(0, 20).map(f => ({ name: f.label, value: String(f.value).slice(0, 1024), inline: true }))
               .concat([{ name: 'Why', value: reasons.slice(0, 1024) }]) }],
};
// Private payload for the approver only (email/DM integration): the single-use decision link.
const approver_private = {
  to: c.approver.email, approver_id: c.approver.employee_id,
  subject: `[Action required] ${c.title}`,
  decision: { method: ev.callback.method, url: ev.callback.url, body_template: ev.callback.body,
              expires_at: ev.expires_at },
};
return [{ json: { kind: 'requested', approval_id: ev.approval_id, request_id: ev.request_id,
                  correlation_id: $('Verify signature').first().json.event.event_id,
                  channel: { slack, discord }, approver_private } }];
""", (1100, 200))
    outcome = wf.code("Format decision outcome", """
const ev = $('Verify signature').first().json.event.data;
const icon = { EXECUTED: '✅', REJECTED: '🚫', FAILED: '⚠️' }[ev.final_state] || 'ℹ️';
const exec = ev.execution
  ? ` Seats ${ev.execution.customer_seats.join(' → ')}, ARR $${ev.execution.contract_arr.join(' → $')}.` : '';
const who = ev.decided_by ? ` by ${ev.decided_by}` : '';
const text = `${icon} ${ev.request_id} (${ev.customer_id}): ${ev.decision}${who} → ${ev.final_state}.${exec}`
  + (ev.comment ? ` Comment: "${ev.comment}"` : '') + (ev.error ? ` Error: ${ev.error.message}` : '');
return [{ json: { kind: 'finalized', approval_id: ev.approval_id, request_id: ev.request_id,
                  correlation_id: $('Verify signature').first().json.event.event_id,
                  channel: { slack: { text }, discord: { content: text } }, approver_private: null } }];
""", (1100, 420))
    channel = wf.if_true("Alert channel configured?", "={{ !!$env.NEXUSFLOW_ALERT_WEBHOOK_URL }}", (1320, 300))
    post = wf.add("Post to channel", "n8n-nodes-base.httpRequest", 4.2, (1540, 200), {
        "method": "POST", "url": "={{ $env.NEXUSFLOW_ALERT_WEBHOOK_URL }}",
        "sendBody": True, "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify(($env.NEXUSFLOW_ALERT_FORMAT || 'slack') === 'discord' ? $json.channel.discord : $json.channel.slack) }}",
        "options": {"timeout": 10000, "response": {"response": {"fullResponse": True, "neverError": True}}},
    }, notes="Channel payload only: never contains the approval token.")
    cb = wf.code("Build callback", callback_js(
        "hitl_approval_notification",
        action_expr="$('Verify signature').first().json.event.event_type === 'approval.requested' ? 'approval_card_sent' : 'approval_outcome_notified'",
        subject_expr="$('Verify signature').first().json.event.data.request_id",
        correlation_expr="$('Verify signature').first().json.event.event_id",
        outcome_expr="""{
    approval_id: $('Verify signature').first().json.event.data.approval_id,
    event_type: $('Verify signature').first().json.event.event_type,
    final_state: $('Verify signature').first().json.event.data.final_state || null,
    approver_notified_privately: $('Verify signature').first().json.event.event_type === 'approval.requested',
    channel_delivery: $('Post to channel').isExecuted
      ? { attempted: true, status: $('Post to channel').first().json.statusCode }
      : { attempted: false, reason: 'NEXUSFLOW_ALERT_WEBHOOK_URL not set' },
  }""",
    ).replace("return [{ json: { ...$json, req: signed(payload) } }];",
              "return [{ json: { req: signed(payload) } }];  // drop card/private payload: no token in the log"),
        (1760, 300))
    report = wf.signed_http("Report to NexusFlow", "POST", "/api/v1/webhooks/n8n/callback", (1980, 300))

    relay = wf.webhook("Approval decision relay", "nexusflow/approval-decision", (0, 700))
    rverify = wf.code("Verify relay signature", VERIFY_JS, (220, 700))
    rvalid = wf.if_true("Relay signature valid?", "={{ $json.valid }}", (440, 700))
    rreject = wf.respond("Reject relay: bad signature", REJECT_BODY, 401, (660, 860))
    rsign = wf.code("Sign decision for API", SIGN_JS + """
const d = $json.event.data || $json.event;
const body = { action: d.action, approver_id: d.approver_id };
if (d.comment) body.comment = d.comment;
if (d.approver_signature) body.approver_signature = d.approver_signature;
return [{ json: { token: encodeURIComponent(d.approval_token || ''), req: signed(body) } }];
""", (660, 700))
    rcall = wf.signed_http("Submit decision", "POST", "/api/v1/approvals/{{ $json.token }}/action", (880, 700),
                           notes="Token goes in the URL path; the body is HMAC-signed so the API knows n8n relayed it.")
    rrespond = wf.respond("Respond: decision result",
                          "={{ JSON.stringify({ status: $json.statusCode, result: $json.body }) }}",
                          "={{ $json.statusCode }}", (1100, 700))

    wf.connect(hook, verify); wf.connect(verify, valid)
    wf.connect(valid, ack, 0); wf.connect(valid, reject, 1)
    wf.connect(ack, kind); wf.connect(kind, card, 0); wf.connect(kind, outcome, 1)
    wf.connect(card, channel); wf.connect(outcome, channel)
    wf.connect(channel, post, 0); wf.connect(channel, cb, 1); wf.connect(post, cb)
    wf.connect(cb, report)
    wf.connect(relay, rverify); wf.connect(rverify, rvalid)
    wf.connect(rvalid, rsign, 0); wf.connect(rvalid, rreject, 1)
    wf.connect(rsign, rcall); wf.connect(rcall, rrespond)
    return wf


WORKFLOWS = {
    "p1_ticket_escalation_workflow.json": p1_ticket_escalation,
    "seat_expansion_trigger_workflow.json": seat_expansion_trigger,
    "delinquent_invoice_reconciliation_workflow.json": delinquent_invoice_reconciliation,
    "hitl_approval_notification_workflow.json": hitl_approval_notification,
}


def main() -> None:
    for filename, factory in WORKFLOWS.items():
        data = factory().export()
        (OUT_DIR / filename).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {filename}: {len(data['nodes'])} nodes, id={data['id']}")


if __name__ == "__main__":
    main()
