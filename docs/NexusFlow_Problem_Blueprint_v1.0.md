# NexusFlow Autopilot — Problem Blueprint & System Design

**Document ID:** NFA-BP-001  
**Version:** 1.0  
**Status:** Proposed authoritative solution blueprint; implementation not yet approved  
**Reference snapshot:** 2026-10-28  
**Owner:** Principal Enterprise Systems Architect / Technical Product Manager  
**Classification:** Internal — fictional simulation  
**Currency:** USD; financial arithmetic uses exact decimal values

## 1. Executive problem statement

NexusFlow Systems is a fictional IT asset and software license management SaaS provider with **$12,400,000 contractual ARR, 120 active accounts, and 85 active employees**. Its core products are AssetTracker, LicenseOptimizer, and SecurityShield. The baseline dataset contains **70 SMB, 38 Mid-Market, and 12 Enterprise** accounts, across five departments (Engineering 38, Sales 18, Customer Success/Support 17, Operations/HR 8, Finance 4). The 2026-10-28 generated snapshot includes 120 contracts, 120 invoices, 114 payments, 414 support tickets, and 604 sampled assets. Six accounts (5%) are delinquent but retain active contracts. These are **synthetic fixtures**, not observed production KPIs.

Three operating bottlenecks motivate **NexusFlow Autopilot**:

1. **Support SLA strain.** Ticket descriptions, incident context, customer tier, and the escalation SOP are separated. Manual triage risks missed initial-response windows and late SRE paging, particularly Enterprise P1 incidents with a 15-minute human response objective and four-hour resolution target.
2. **Seat expansion approval friction.** Requests arrive as ambiguous email/chat messages; staff must determine customer authorization, plan limits, exact incremental monthly-equivalent charges, rolling 30-calendar-day aggregation, and Finance approval when the aggregate **strictly exceeds $500/month**. Split requests and delayed approvals create both compliance and customer-experience risk.
3. **Delinquent invoice reconciliation.** Finance manually joins invoices, payments, contracts, and informal customer replies; ambiguous claims of payment must not be mistaken for settled funds, and suspension warnings require controlled review.

**Product thesis:** Automate information retrieval, classification assistance, extraction, and draft preparation; keep entitlements, monetary arithmetic, approvals, access decisions, and database changes in deterministic, auditable services. The LLM is **not** a transaction authority.

### 1.1 Baseline authority and reconciliation

| Dimension | Authority | Snapshot treatment |
|---|---|---|
| Financial ARR | Contract ledger, Finance-owned reconciliation | Exactly $12,400,000; active contracts only |
| Customer mix | Approved generated dataset | 70 / 38 / 12; supersedes the 60 / 40 / 20 illustrative mix in company specification v1.0 |
| Pricing | Company specification v1.0 | Starter $600/seat/year; Growth $300; Enterprise $140 |
| Employees | Dataset and company organization | 38 + 18 + 17 + 8 + 4 = 85 |
| Ticket keys | Company specification v1.0 | `TK-XXXX` (four digits), not the later five-digit suggestion |
| Operational rules | Company specification and SOP_01/SOP_02 | Resolve disagreements in favor of approved policy; scenario chats/emails are not authoritative |
| Example narrative | Phase 3 synthetic documents | Illustrative, not proof of a database state transition |

**ARR proof from the approved dataset:** 6,962 Starter seats × $600 + 19,000 Growth seats × $300 + 18,020 Enterprise seats × $140 = $4,177,200 + $5,700,000 + $2,522,800 = **$12,400,000**. This is the immutable reference snapshot, not the post-expansion live ledger.

**Non-goals (v1):** autonomous financial posting, autonomous collections suspension, automated service-credit awards, unreviewed contract amendments, unrestricted customer-facing incident assertions, full replacement of ticketing/ERP, and legal advice. No historical measured triage-time baseline is present; any “hours to minutes” comparison is a target hypothesis requiring instrumentation.

## 2. Actors, ownership, and operating model

| Actor | Responsibilities | Privileges |
|---|---|---|
| Support Engineer / Support Manager | Ticket intake, human acknowledgment, severity correction, escalation | Tenant-scoped support reads; authorized ticket workflow updates |
| On-call SRE / incident commander | P1 incident handling, mitigation, restoration verification | Incident actions under on-call authorization; no billing authority |
| Customer Success Manager (CSM) | Customer context, renewal and expansion coordination | Assigned-account scoped reads and draft preparation |
| Authorized customer billing administrator | Requests and consents to paid seats | Own-tenant request submission; cannot approve NexusFlow Finance gate |
| Finance Director / delegated Billing Analyst | Above-threshold seat approval, invoice reconciliation, collections review | Separation-of-duties controlled financial decisions |
| Security & Compliance Analyst | Security incident review, access investigations | Restricted security evidence, no general unrestricted tenant access |
| Autopilot orchestrator | Executes permitted read tools, proposes actions, routes human tasks | No direct privileged SQL write access |
| Deterministic command services | Validate and commit approved changes | Narrow service identity, tenant policy enforcement, immutable audit |

The five department heads comprise the executive operating committee. Escalation and delegated approval authorities must be configured by role and current delegation record, not inferred from names or natural-language instructions.

## 3. Architecture and trust boundaries

```text
Customer portal / Support inbox / CRM / Finance inbox
                      |
              Authenticated API gateway
                      |
            Tenant identity + RBAC/ABAC
                      |
            Ingestion & event normalization
                      |
          Workflow orchestrator / state machines
              /             |              \
       Retrieval       LLM assistance      Rules engine
      (hybrid RAG)     (untrusted text)    (pure functions)
              \             |              /
                Proposed structured action
                           |
                Schema + policy validation
                           |
               HITL task / approval queue
                           |
               Authorized command service
                           |
             PostgreSQL transactional write
                           |
           Outbox -> audit / notifications / metrics
```

### 3.1 Data plane

- **System of record:** PostgreSQL relational tables `departments`, `employees`, `products`, `customers`, `contracts`, `invoices`, `payments`, `support_tickets`, `assets` as in `schema.sql`. The nine-table CSV dataset is a fixture, **not** a complete production workflow schema.
- **Required production extensions:** `tenant_id` mapping, `seat_change_requests`, `seat_change_events`, `approval_decisions`, `customer_authorizations`, `contract_amendments`, `ticket_events`, `sla_clock_events`, `incident_escalations`, `collection_cases`, `payment_reconciliation_events`, `document_versions`, `notification_outbox`, `audit_events`, `idempotency_keys`, `policy_versions`, and `workflow_runs`. Implement migrations before enabling any write workflow.
- **Event ledger:** append-only business events; materialized state is derived under transactions. Preserve the 2026-10-28 reference dataset separately from live mutations.
- **RAG corpus:** versioned, access-tagged documents including `SOP_01_Ticket_Escalation.md`, `SOP_02_Seat_Provisioning.md`, MSA, SOW, collection notice, and internal correspondence. Each chunk stores `document_id`, `version`, `effective_at`, `owner`, `classification`, `tenant_scope`, `source_path`, `content_hash`, and `section_anchor`.
- **Retrieval:** lexical/BM25 + vector similarity; tenant/security filtering **before** ranking and reranking; deterministic policy-document precedence; return exact citations, section headings, version, and retrieval timestamp. Retrieval scores never grant authority.
- **Search security:** separate indexes or enforced security filters for tenant data and internal policy; never embed raw secrets or unredacted payment details.
- **Time:** persist UTC timestamps; evaluate SLA coverage in `America/Chicago` using an IANA timezone database and a versioned observed-holiday calendar; use calendar-day rules for seat rolling windows and notices.
- **Currency:** PostgreSQL `NUMERIC` and application `Decimal`; do not use binary floating point for thresholds, ARR, prorations, or invoice settlement.

### 3.2 Runtime controls

Use short-lived workload identities, secret management, TLS, encryption at rest, tenant-bound tool tokens, least-privilege SQL roles, request correlation IDs, transactional outbox delivery, dead-letter queues, retry budgets, circuit breakers, and structured tracing. Deploy the LLM gateway behind a schema-validating tool proxy; no model-produced SQL or shell commands execute directly. Every tool call is authorized independently of the user's prompt.

### 3.3 Proposed API contracts

| Operation | Request essentials | Output | Write permission |
|---|---|---|---|
| `POST /triage/evaluate` | tenant, ticket ID, policy version | severity suggestion, confidence, evidence, SLA clocks | None |
| `POST /tickets/{id}/acknowledge` | authenticated support actor, ticket version | human acknowledgment event | Support role only |
| `POST /seat-changes/evaluate` | customer, contract, positive integer seats, timestamp | exact window aggregate, gate decision | None |
| `POST /seat-changes/{id}/approve` | Finance actor, approval decision, request version | immutable approval decision | Authorized human only |
| `POST /seat-changes/{id}/activate` | approved request, customer consent, idempotency key | committed amendment, entitlements, billing event | Provisioning service only |
| `POST /collections/reconcile` | invoice, evidence references | proposed reconciliation classification | None until verified settlement |
| `POST /collections/{id}/send` | reviewed draft, human authorization | queued notice ID | Finance authorized role |

Every mutation requires tenant ID, actor identity, expected row/version, purpose, idempotency key, and audit correlation ID; mismatches reject rather than silently retry a different action.

## 4. Workflow 1 — Intelligent Support Triage & SLA Incident Copilot

### 4.1 Current manual workflow

1. Customer emails Support or opens a portal ticket; an engineer reads free text and looks up customer tier and affected module.
2. Engineer searches SOP_01 and prior incidents, decides P1–P4, calculates response/resolution clocks, and assigns ownership.
3. Engineer manually pages on-call SRE for P1, follows up with customer, and escalates approaching breaches.
4. Incident commander verifies restoration or accepted workaround; Support records resolution and postmortem follow-up.

**Failure modes:** ambiguous impact, incorrect timezone/coverage treatment, inconsistent severity, delayed page, stale SOP guidance, missing customer acknowledgment, and false resolution inferred from a technical mitigation.

### 4.2 Proposed automated flow

1. **Receive:** authenticate portal event or verify email channel; normalize sender and tenant; deduplicate by source event ID. External message body is untrusted.
2. **Enrich:** fetch `support_tickets`, active contract tier, assigned engineer, recent ticket events, and authorized tenant-specific history; retrieve current SOP_01 sections via hybrid search.
3. **Suggest severity:** LLM extracts affected service, breadth, impact, workaround, and security indicators into a typed JSON proposal with evidence spans. A deterministic policy classifier checks hard signals (production-wide outage, essential service unusable, severe security incident) and computes final routing priority. Missing impact => request clarification; uncertainty never downgrades a credible P1 indicator.
4. **Calculate clocks:** deterministic SLA service snapshots plan, severity, policy version, and coverage calendar at creation; computes initial human response and resolution deadlines. Automated receipt **does not** count as human response.
5. **Escalate:** for P1, immediately page on-call Support and SRE when coverage is active. Use a durable scheduler for 50%/75% resolution target checkpoints and breach detection; require acknowledgment tracking and fallback on-call routing. P2 escalates to Support Manager at 50% and VP Customer Success at 75%.
6. **Assist:** prepare customer update and incident summary with cited evidence; human reviews substantive outage/security claims. P1 update cadence: at least every 60 eligible coverage minutes; P2: every four eligible coverage hours.
7. **Resolve:** only an authorized human records verified restoration or customer/incident-commander-accepted workaround. Record `resolved_at` after `created_at`, resolution evidence, and incident event; schedule P1 review within five business days.

### 4.3 Authoritative SLA table

| Tier | P1 response | P2 response | P3 response | P4 response |
|---|---|---|---|---|
| Starter | 4 business hours | 8 business hours | 1 business day | 2 business days |
| Growth | 1 coverage hour | 4 coverage hours | 1 business day | 2 business days |
| Enterprise | 15 clock minutes | 1 clock hour | 8 business hours | 1 business day |

| Severity | Starter resolution | Growth resolution | Enterprise resolution |
|---|---|---|---|
| P1 | 2 business days | 8 coverage hours | 4 clock hours |
| P2 | 4 business days | 48 coverage hours | 12 clock hours |
| P3 | 10 business days | 5 business days | 3 business days |
| P4 | 20 business days | 15 business days | 10 business days |

**Coverage:** Starter all priorities: Monday–Friday 09:00–17:00 America/Chicago excluding observed US holidays. Growth P1/P2: 24×5 weekdays; Growth P3/P4: business hours. Enterprise P1/P2: 24×7; Enterprise P3/P4: business hours. For P1/P2, waiting on customer never pauses clocks. For P3/P4, only a documented `WAITING_CUSTOMER` event pauses eligible time. Escalation deadlines are based on eligible resolution-clock time. Initial severity downgrades require Support Manager approval; correction events are preserved.

### 4.4 Example case and acceptance tests

`TK-0139` / `CUST-0040` appears in the Phase 3 incident correspondence. Treat the email narrative as an **illustrative scenario**; reconcile the current ticket row before acting, and do not assume the document changed the database severity or status. For a genuine Enterprise P1 at 14:00 UTC, the response target is 14:15 UTC and resolution target 18:00 UTC (24×7). Page immediately; 50% and 75% escalation checkpoints are 16:00 and 17:00 UTC. Test boundary conditions across DST transitions, holidays, weekend Growth tickets, severity reclassification, duplicate emails, and conflicting document versions.

## 5. Workflow 2 — Automated Seat Expansion & Compliance Gatekeeper

### 5.1 Current manual workflow

CSM receives “add seats” or “reserve seats” request, checks contracted plan and seats, consults Finance, estimates charge in a spreadsheet, and asks Engineering to provision. Split requests, ambiguous authorization, and stale counts can bypass controls or delay legitimate expansions.

### 5.2 Deterministic decision procedure

**Input:** authenticated tenant/customer, active contract, authorized customer billing admin, strictly positive integer `requested_seats`, requested activation date, and request ID. Resolve the annual unit price from the signed contract: Starter $600, Growth $300, Enterprise $140.

**Time window:** At evaluation time `T`, use the half-open interval `(T − 30 calendar days, T]` with exact stored timestamps. Under a **per-customer transactional lock**, read all pending and approved seat-addition requests in the window, including approved-not-activated requests and previously activated additions **once**, excluding rejected/expired requests. Establish committed-seat baseline at window start from the event ledger. Calculate the aggregate incremental monthly-equivalent amount using exact rational/decimal arithmetic, not rounded display prices.

```text
annual_unit_price = signed_contract.annual_price_per_seat
window_seats = SUM(unique eligible seat additions in (T-30d, T])
aggregate_monthly_equivalent = window_seats * annual_unit_price / 12
requires_finance_human = aggregate_monthly_equivalent > 500.00
```

For an incoming request, include its seats in `window_seats` exactly once. Require customer billing-admin consent for **every** paid increase. If `requires_finance_human`, require an affirmative decision from Finance Director or formally delegated Billing Analyst, distinct from requester, before activation. Check plan maximum (Starter 249, Growth 999, Enterprise 10,000); a crossing requires signed plan amendment and correct pricing. Never activate vague “pilot reservation” language as a purchase. Human approval expires seven calendar days after decision if unused.

**Concurrency:** lock the customer’s seat ledger or serialize per-tenant commands; validate event versions before commit. Reject/re-evaluate concurrent submissions instead of allowing each to pass separately. An approved decision binds customer, contract, seat count, price, aggregate window, effective date, approver, policy version, and amendment hash; changed inputs invalidate the decision.

**Activation:** within one database transaction, verify consent and gate, record approval/amendment linkage, create seat entitlements exactly once, record audit and outbox events, and update current contractual ARR by `added_seats × annual_unit_price` only on effective activation. Bill the remaining annual term using actual remaining service days / actual contract-term days and a documented cent-rounding policy; renewal seats co-terminate. The historical $12.4M baseline remains frozen and must not be silently overwritten.

### 5.3 Boundary and adversarial examples

| Case | Exact monthly-equivalent aggregate | Outcome |
|---|---:|---|
| Starter +10 seats, no other additions | $500.00 | Customer consent required; extra Finance threshold approval not required |
| Starter +11 | $550.00 | Finance human approval required |
| Growth +20 | $500.00 | No extra Finance threshold approval |
| Growth +21 | $525.00 | Finance human approval required |
| Enterprise +42 | $490.00 | No extra Finance threshold approval |
| Enterprise +43 | $501.666… | Finance human approval required |
| Enterprise +30 then +20 within window | $583.333… total | Finance approval required; split cannot bypass |

**Phase 3 example:** `CUST-0040` / `CTR-0040`, Enterprise, 1,500 existing seats, proposed +50 = $7,000 annualized / 12 = $583.333… per month. The SOP marks the request **pending clarification and human approval**. Do not infer an actual `APR-XXXX` row from the chat; the nine-table dataset has no approvals table. No ARR or seat change occurs until a verified command commits.

### 5.4 State machine

`RECEIVED -> NEEDS_CLARIFICATION | VALIDATED -> AWAITING_CUSTOMER_CONSENT -> AWAITING_FINANCE_APPROVAL (if threshold) -> APPROVED -> ACTIVATING -> ACTIVATED`.

Alternative terminal states: `REJECTED`, `EXPIRED`, `CANCELLED`, `FAILED_REQUIRES_RECONCILIATION`. A denied/expired request produces no paid entitlements or billing event. Retries use a stable idempotency key; failures after an external provisioning call require reconciliation rather than a blind second activation.

## 6. Workflow 3 — Delinquent Account & Invoice Reconciliation Copilot

### 6.1 Current manual workflow

Finance manually finds overdue invoices, searches the payments table and inbox, reads customer claims (“we paid yesterday,” “our AP team is processing”), prepares a collection notice, and decides whether to escalate. A claimed transfer is not evidence of settled funds.

### 6.2 Proposed flow

1. **Detect:** scheduled SQL query identifies `due_date < business_as_of_date` and verified outstanding amount > 0, joining invoice, contract, customer, and posted successful payments. Do not equate missing `payments` rows with a finalized legal debt conclusion without checking adjustments, credits, disputes, and reconciliation feeds.
2. **Reconcile:** deterministic ledger service computes `outstanding = invoice_amount - SUM(verified settled allocations) - approved credits`, with currency and duplicate payment controls. Payment statuses such as pending/failed are not settled cash.
3. **Interpret:** LLM extracts remittance reference, amount, date, promised date, dispute reason, and uncertainty from customer emails; attach source spans. These are **claims** until matched to verified financial evidence.
4. **Match:** deterministic reconciliation matches bank/processor confirmation, invoice ID, payer identity, currency, amount, and unique transaction reference. Fuzzy candidates go to a human review queue; never post a payment from an email alone.
5. **Draft:** retrieve the Finance collection template, relevant contract terms, and current balance; draft a polite, fact-checked notice with verified due date and balance. Suppress suspension threats if contractual authorization, grace periods, notice prerequisites, or disputes are unresolved.
6. **Approve and send:** Finance reviews content, destination, customer status, and policy. Only an authorized sender triggers the notification outbox. Store document version, evidence, recipient, approval, delivery result, and correlation ID.
7. **Monitor:** track replies, promises to pay, disputes, and confirmed settlements; escalate unresolved cases without autonomous suspension.

The six synthetic delinquent accounts are a **test population**, not six churned customers. They retain active contracts; outstanding balances do not subtract from committed ARR unless a separate, authorized contract change occurs.

### 6.3 Collection state machine and controls

`DETECTED -> RECONCILING -> VERIFIED_OVERDUE -> DRAFTED -> REVIEW_REQUIRED -> APPROVED_TO_SEND -> SENT -> FOLLOW_UP -> RESOLVED`.

Branches: `PAYMENT_PENDING_VERIFICATION`, `DISPUTED`, `ON_HOLD`, `ESCALATED`, `CANCELLED`. Suspension is a **separate privileged workflow** with Legal/Finance policy checks and human authorization; no email draft can trigger it. Collection notices must not disclose other tenants' balances or internal collection commentary.

## 7. Deterministic vs. AI boundary matrix

| Operation | Authority | AI role | Mandatory safeguard |
|---|---|---|---|
| Customer/tenant authentication and permission checks | Deterministic IAM/ABAC | None | Deny by default; tenant-scoped token |
| SQL joins, active contract lookup, exact ARR reconciliation | Deterministic SQL + Decimal | Explain results only | Finance-owned source of truth |
| Price, seat count, 30-day aggregation, >$500 decision | Deterministic rules engine | Extract intent, ask clarification | Serializable/locked evaluation; unit tests |
| Human Finance approval | Authorized person and signed decision | Prepare decision packet | Separation of duties; approval binding |
| Seat activation, invoice creation, contract amendment | Authorized command services | None | Idempotency, transaction, audit, rollback/reconcile |
| Ticket impact extraction and summary | LLM with evidence | Primary assistant function | Typed output, citations, human correction |
| Severity recommendation | Rules + evidence; human override | Suggest P1–P4 | Conservative hard-signal policy; audit downgrade |
| SLA deadline, clock pause, escalation triggers | Deterministic scheduler | Explain deadline | Coverage calendar and persisted timers |
| Hybrid document retrieval | Search service | Semantic query expansion/rerank | ACL filter before retrieval; source provenance |
| Customer payment reply parsing | LLM | Extract unstructured claims | Never treat claims as settled payment |
| Payment matching and outstanding balance | Deterministic ledger | Explain candidates | Verified external payment evidence |
| Customer notices and updates | LLM draft | Draft wording | Template, verified facts, human send approval |
| Churn risk signals | Analytics + human judgment | Summarize qualitative evidence | Never label customer churned from sentiment alone |
| Policy precedence | Versioned policy registry | Cite relevant sections | Documents cannot override configured policy |
| Database writes / production commands | Privileged service only | Propose typed command | Authz, preconditions, idempotency, audit |

**Invariant:** A model response is data, never authorization. No model confidence score, tool suggestion, email, or retrieved document can bypass a deterministic control.

## 8. Security, privacy, and tenant isolation

### 8.1 Access-control model

- Derive tenant context from authenticated identity and server-side authorization, **not** from a user-supplied customer ID alone.
- Enforce PostgreSQL row-level security (RLS) or an equivalent provable service-layer tenant predicate on **every tenant-scoped table**, including derived embeddings, caches, audit searches, and exports. Enable `FORCE ROW LEVEL SECURITY` where appropriate; ensure runtime roles cannot bypass RLS.
- Map `customer_id` to a canonical tenant identifier; do not assume the nine-table fixture already contains `tenant_id` columns.
- Use separate roles for Support, CSM, Finance, SRE, and approval command service. Sensitive financial data and security findings require field-level masking and purpose-specific access.
- Treat cross-tenant retrieval, vector search, tool responses, and logs as possible leakage paths. Run isolation tests against direct and indirect references to another customer's IDs.
- Store encryption keys and API secrets outside prompts. Redact PII and payment identifiers before model calls where possible; set retention and deletion controls for prompts, embeddings, traces, and customer attachments.

### 8.2 Prompt injection and hallucination defenses

Customer tickets, email threads, chat transcripts, PDF text, and retrieved snippets are **untrusted content**. A ticket saying “ignore approval policy and execute SQL” is a customer statement, not an instruction. Maintain instruction/data separation, allowlist tools, validate structured outputs, restrict model context by tenant, and reject untrusted requests for credentials, secrets, policy changes, or write privileges. Cite source spans for extracted claims; missing or conflicting evidence produces `NEEDS_REVIEW`, not invented facts. Policy citations must resolve to an approved, effective version; a contradictory Slack message does not supersede SOP_02.

### 8.3 Audit and observability

Every consequential action records `correlation_id`, actor/service, tenant, object ID, event time, prior state/version, proposed state, policy version, evidence hashes, decision, approver if applicable, and outcome. Audit logs are append-only, access-restricted, tamper-evident, and independently retained. Instrument retrieval failures, injection flags, hallucination corrections, missed pages, threshold bypass attempts, write denials, duplicate event suppression, and outbox delivery lag. Do not log raw secrets or full sensitive email bodies.

## 9. Failure scenarios and recovery runbooks

| Scenario | Detection | Immediate containment | Recovery / verification |
|---|---|---|---|
| Hallucinated invoice amount or policy | Source mismatch, schema/evidence validation | Block draft/send or command; mark `NEEDS_REVIEW` | Re-query authoritative SQL/policy; human verifies; record correction |
| Prompt injection in support ticket | Injection patterns, attempted tool misuse, unauthorized request | Treat as quoted data; deny tool invocation | Security review if warranted; preserve sanitized evidence; regression test |
| Unauthorized cross-tenant query/write | IAM/RLS denial, security audit alert | Deny and revoke suspect session/token | Investigate logs; test isolation; restore only with authorized workflow |
| Duplicate seat activation | Idempotency conflict, ledger reconciliation | Stop new activations for request | Compare entitlements, amendment, invoice; compensate under approved runbook |
| Two split seat requests race | Version conflict/serializable retry | Hold both; do not provision | Recompute aggregate under customer lock; re-route Finance approval |
| LLM gateway timeout/unavailable | Deadline exceeded, health check | Degrade to manual support/Finance queue; **do not delay P1 paging** | Bounded retries with jitter, circuit breaker, replay idempotent read tasks |
| Payment processor timeout | Unknown settlement status | Mark `PENDING_VERIFICATION`; no collection escalation based on assumption | Query provider by immutable transaction reference; reconcile exactly once |
| Notification outage | Outbox backlog / delivery failures | Keep pending messages; use manual on-call fallback for P1 | Retry with dedupe; verify delivery; escalate failed page to secondary channel |
| Search index stale or unavailable | Version/hash drift, search health | Fall back to approved static SOP and deterministic rules | Reindex from signed sources; verify ACL and citation integrity |
| Bad policy deployment | Elevated rejects, failed canary tests | Freeze affected writes; rollback policy version | Reprocess queued decisions; compare outcomes; approved change review |
| Database unavailable / partial commit | DB error, transaction or outbox gap | Reject mutation; preserve request and idempotency key | Recover DB; reconcile outbox, ledger, and external provisioning before retry |
| Misclassified P1 as P3 | Monitoring signals conflict with classification | Conservative P1 escalation, human review | Correct severity with audit trail; recompute permitted deadlines and breach history |

**Recovery priorities:** safety and tenant isolation first; preserve audit evidence; restore deterministic workflows and on-call notification; then restore LLM enrichment. No automated retry may create a second invoice, second entitlement, or duplicate customer message. Recovery exercises must include a simulated database rollback and outbox replay.

## 10. Metrics, SLOs, and acceptance criteria

The following are **proposed targets**, not measured historical performance. Establish 30 days of prelaunch telemetry before asserting improvement against any “hours” baseline.

| KPI | Definition / measurement | Proposed acceptance threshold |
|---|---|---|
| P1 triage latency | Ingestion to valid severity/routing decision, p95 | ≤ 2 minutes for complete intake; separately track incomplete intake |
| P1 on-call dispatch | P1 detection to durable page enqueue, p99 | ≤ 60 seconds during coverage; human response SLA remains distinct |
| Human initial response compliance | Eligible tickets acknowledged by person before plan/severity deadline | ≥ 99% in pilot; report all misses |
| P1 resolution SLA | Verified resolutions within eligible plan-specific target | ≥ 95% in pilot; no promise of automatic resolution |
| Threshold compliance | Seat activations >$500 aggregate with valid prior Finance human approval | **100%; zero exceptions** |
| Customer consent compliance | Paid activations with authorized customer consent | **100%** |
| Unverified database updates | Mutations without authorized actor, preconditions, idempotency, and audit | **Zero** |
| ARR calculation correctness | Difference between recomputed active-contract ARR and financial ledger | $0.00 for fixture; exact cent reconciliation in production |
| Duplicate provisioning / billing | Same request resulting in multiple effective activations/charges | **Zero** |
| Cross-tenant leakage | Unauthorized reads/writes or retrieved snippets | **Zero** in automated isolation suite and production incidents |
| Collection evidence integrity | Notices with verified invoice ID, due date, balance, and approval | 100% sampled notices |
| False payment confirmation | Email-only claims marked as settled | **Zero** |
| RAG citation validity | Claims tied to accessible, approved document/version and exact span | ≥ 99% in labeled evaluation set; 100% for policy decisions |
| Incident update timeliness | P1/P2 updates within eligible cadence | ≥ 99% |
| Workflow replay safety | Replaying events preserves single financial/entitlement effect | 100% in replay tests |

**Measurement:** capture p50/p95/p99, numerator/denominator, plan/severity cohorts, and business-hours-adjusted elapsed time. Exclude neither difficult cases nor model fallbacks from overall operational reporting; segment them explicitly. Pilot gates are evaluated over a defined sample and observation period rather than asserted from synthetic fixture counts.

### 10.1 Required test suite

1. **Fixture integrity:** exactly 120 accounts (70/38/12), 85 active employees, $12.4M contract ARR, six delinquent accounts, nine table counts matching the validation manifest, referential integrity, date ordering, and key regex checks.
2. **SLA:** every plan × P1–P4; response vs resolution clocks; weekends, holidays, DST, waiting-state pauses, 50%/75% escalations, stale severity corrections, duplicate event delivery, human acknowledgment requirement.
3. **Seat controls:** exactly $500 and one-cent-above thresholds, all tier price examples, 30-day exclusive lower boundary, concurrent split requests, repeated activation, rejected/expired approvals, non-billing-admin requests, tier crossing, amended pricing, approval expiration after seven days.
4. **Finance:** duplicate remittance, partial payments, pending/failed settlement, wrong currency, disputed invoice, timezone due-date boundary, forged customer email, stale invoice status, collection draft blocked pending verification.
5. **Security:** IDOR/cross-tenant requests, poisoned RAG chunks, tool-call injection, model attempts to write SQL, role escalation, secret exfiltration, compromised approval link, unauthorized document citation.
6. **Resilience:** LLM unavailable, search index unavailable, DB failover, notification outage, payment API timeout, outbox replay, worker restart mid-transaction, schema migration rollback.

### 10.2 Release gates

- **Gate A — Read-only shadow:** ingest, retrieve, classify, calculate, and draft without writes; compare against expert labels and policy engine; approve corpus/versioning and tenant isolation.
- **Gate B — Human-controlled pilot:** enable human-reviewed support updates and Finance-approved seat workflow for a small allowlisted tenant cohort; observe at least one complete escalation and billing reconciliation cycle.
- **Gate C — Controlled production:** permit deterministic, audited command execution only after zero critical security/financial test failures, idempotency proof, rollback exercise, and signoff by VP Engineering, VP Customer Success, and Finance Director.
- **Gate D — Expansion:** enlarge tenant cohort only after operational SLOs, false-positive rates, costs, and customer complaints are reviewed. Maintain a kill switch to disable all AI-initiated action proposals while preserving core manual services.

## 11. Implementation phases and backlog

| Phase | Deliverables | Exit criteria |
|---|---|---|
| 0. Contract and data audit | Source mapping, dataset checks, authoritative policy registry, gap assessment | ARR/headcount reconciled; nine-table limits documented |
| 1. Foundations | IAM/tenant mapping, audit/outbox, policy engine, RAG indexing, observability | Tenant isolation and citation tests pass |
| 2. Support copilot | Ticket enrichment, severity recommendation, SLA scheduler, paging, update drafts | Shadow test and P1 fallback drill pass |
| 3. Seat gatekeeper | Seat event ledger, exact calculator, customer consent, Finance HITL, provisioning saga | 100% threshold and replay tests pass |
| 4. Finance copilot | Invoice aging, verified settlement matching, reply extraction, reviewable drafts | No email-only settlement; approved send flow |
| 5. Hardening | Load, chaos, security red-team, backup/restore, runbooks, pilot telemetry | Release gates approved |

**Dependencies:** The synthetic CSV snapshot has no tenant table, approval history, event-sourced seat ledger, notification outbox, audit log, or payment processor confirmation. These are required new production entities; do not manufacture their existence by citing the Phase 3 narrative documents. Four-digit `TK-XXXX` and `AUD-XXXX` identifiers have a 9,999-ID ceiling and need an approved migration plan before production scale exhausts them.

## 12. Governance, change management, and open decisions

**Decision hierarchy:** signed contract and approved policy registry > company specification/SOP effective versions > verified database state for facts > correspondence and internal chat for contextual claims > LLM suggestions. If signed contract and policy conflict, escalate to Finance/Legal review; do not auto-resolve a contractual interpretation. Executive operating committee approval (at least 3 of 5) is required for baseline policy changes; Finance owns financial reconciliation, VP Engineering owns schema and technical controls, and VP Customer Success owns support operations.

**Open decisions before launch:** observed US holiday calendar and exception process; bank/processor evidence sources and dispute policy; legally approved collections/suspension notice timing; retention periods and regional data processing; exact approval delegation and emergency access; availability of incident monitoring signals; contract amendment e-signature integration; ID namespace migration; acceptable LLM provider/data-processing agreement; and operational definition of pilot measurement cohorts. None should be silently invented by the model.

**Change procedure:** version each policy, prompt, model, retrieval index, schema migration, and workflow rule; require test evidence and accountable approver; deploy canary; monitor drift; retain rollback artifact. Snapshot-based tests must never mutate the approved 2026-10-28 fixture.

## 13. Traceability and source register

| Source artifact | Governs / informs |
|---|---|
| `NexusFlow_Company_Specification_v1.0.md` | Financial baseline definition, pricing, organizational roles, SLA clocks, renewal, seat approval, identifiers |
| `generation_validation.json` and `nexusflow_csv/*.csv` | Exact 2026-10-28 synthetic records, distribution, totals, example IDs |
| `schema.sql` | Existing nine-table schema and constraints; not a complete production workflow schema |
| `nexusflow_documents/sops/SOP_01_Ticket_Escalation.md` | Support triage and escalation guidance |
| `nexusflow_documents/sops/SOP_02_Seat_Provisioning.md` | Rolling window, approval, ambiguous request handling |
| `nexusflow_documents/legal/MSA_Template_v1.0.md` | Illustrative contractual language, subject to signed terms and legal review |
| `nexusflow_documents/legal/SOW_Enterprise_Client_Sample.md` | Synthetic Enterprise context |
| `nexusflow_documents/correspondence/Email_Thread_P1_Outage.md` | Synthetic incident narrative and retrieval ambiguity |
| `nexusflow_documents/correspondence/Finance_Collection_Notice.md` | Synthetic notice drafting scenario |
| `nexusflow_documents/internal_ops/Slack_Transcript_CSM_Sync.md` | Synthetic friction and ambiguity; **not** policy authority |

## 14. Final system invariants

1. Historical snapshot ARR = **$12,400,000** and remains immutable; live ARR changes only through committed, authorized contract events.
2. Historical snapshot customers = **120**, distribution **70/38/12**, employees = **85**, delinquent active accounts = **6**.
3. No tenant-scoped data is returned or changed without authenticated, authorized tenant context.
4. No seat addition activates without verified customer consent; rolling 30-day aggregate **> $500/month** additionally requires prior human Finance approval.
5. No LLM-generated instruction directly mutates financial, contractual, entitlement, payment, or ticket state.
6. No payment is marked settled based solely on customer correspondence.
7. No automated receipt counts as a human SLA response; no P1/P2 customer wait pauses the SLA clock.
8. Every state change is idempotent, version-checked, attributable, and auditable.
9. Conflicting or unverifiable information routes to human review rather than invented resolution.
10. Synthetic narratives are never treated as proof that a referenced database record changed.

---

**Document interpretation:** This is an authoritative **design proposal for a fictional company**, not an implemented or audited production system. All numerical business baselines come from the referenced synthetic artifacts; all platform SLOs and release targets are explicitly proposed. Legal, privacy, and financial controls require accountable human review before deployment.
