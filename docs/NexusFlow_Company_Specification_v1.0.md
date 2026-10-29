# NexusFlow Systems — Company Specification

**Document ID:** NFS-SPEC-001  
**Version:** 1.0  
**Status:** Authoritative fictional reference baseline  
**Effective date:** 2026-10-28  
**Owner:** Principal Systems Architect  
**Currency:** USD  
**Classification:** Internal — fictional simulation data

## 1. Executive Overview

NexusFlow Systems is a **fictional** B2B enterprise SaaS company founded in 2021 by former IT operations and software asset management practitioners. Its headquarters are fictionalized as Austin, Texas, with a distributed workforce. The company was conceived after its founders observed that growing organizations tracked devices, SaaS entitlements, and security posture in disconnected spreadsheets and vendor portals.

**Mission:** Give IT, procurement, finance, and security teams a reliable system of record for assets, software licenses, and associated security risks.

**Target market:** US-focused organizations with 50–10,000 employees, segmented by contracted managed-seat volume: SMB (50–249 seats), Mid-Market (250–999), and Enterprise (1,000–10,000). The segmentation is based on **billable managed seats**, not the customer's employee count. Customers outside those bounds require an exception and are not included in the baseline.

**Value proposition:** One integrated platform to discover assets, reconcile license consumption, identify underutilized subscriptions, and prioritize security exposure. Benefits are product objectives, not claims of measured customer savings.

**Business model:** Annual, prepaid, recurring subscriptions priced per committed managed seat per month, with a 12-month contract term. The three modules are bundled into every plan; tiers vary by functionality, SLA, support, and governance. No setup fees, usage overages, taxes, or professional services are included in baseline ARR.

## 2. Financial Baseline and Definitions

### 2.1 Authoritative snapshot

The following is a **synthetic, internally reconciled snapshot as of 2026-10-28**, not externally reported company performance.

| Metric | Value |
|---|---:|
| Annual recurring revenue (ARR) | **$12,400,000** |
| Monthly recurring revenue (MRR = ARR / 12) | $1,033,333.333… |
| Active paying customer accounts | **120** |
| Employees (full-time-equivalent headcount) | **85** |
| Core departments | **5** |
| Average ARR per account | $103,333.333… |

**Accounting convention:** ARR is 12 × the sum of unrounded contractual monthly recurring charges. Displayed MRR and averages are rounded only for presentation; never sum rounded display figures. No discounts, credits, taxes, one-time fees, or foreign-exchange adjustments apply to this baseline. One customer has exactly one active subscription and one active contract in the snapshot.

### 2.2 Customers, contracted seats, and ARR reconciliation

| Segment / plan | Accounts | Committed seats per account | Total seats | Price per seat / month | Monthly recurring charge | ARR |
|---|---:|---:|---:|---:|---:|---:|
| SMB / Starter | 60 | 100 | 6,000 | $50 | $300,000 | $3,600,000 |
| Mid-Market / Growth | 40 | 500 | 20,000 | $25 | $500,000 | $6,000,000 |
| Enterprise / Enterprise | 20 | 1,000 | 20,000 | $11.666666…* | $233,333.333…* | $2,800,000 |
| **Total** | **120** | — | **46,000** | — | **$1,033,333.333…** | **$12,400,000** |

\* Enterprise contracts use an **annual price of exactly $140 per committed seat**, equivalent to $140 / 12 per seat per month. The repeating decimal is illustrative only. The contract billing engine stores the Enterprise annual unit price as $140.00 and bills $140,000 annually per 1,000-seat account. Starter annual unit price = $600; Growth annual unit price = $300. This annual-unit-price representation prevents rounding drift.

**Proof:** (60 × 100 × $600) + (40 × 500 × $300) + (20 × 1,000 × $140) = $3,600,000 + $6,000,000 + $2,800,000 = **$12,400,000 ARR**. Accounts = 60 + 40 + 20 = **120**. Seats = 6,000 + 20,000 + 20,000 = **46,000**.

**Pricing interpretation:** Enterprise has a lower effective unit price due to high volume, despite stronger service commitments. Published prices are fictional reference prices; all baseline customers pay them without discount. Any later price or volume change must update the reconciliation ledger and constitutes a new dated snapshot.

### 2.3 Metric definitions

- **ARR:** Sum of annualized committed recurring subscription consideration for active contracts; excludes services, taxes, and nonrecurring charges.
- **MRR:** ARR / 12; internally maintained at full precision for reporting, not used to create invoices.
- **Active customer:** Distinct customer with one active, billable subscription on the snapshot date; trials and terminated contracts excluded.
- **Gross revenue churn:** Lost beginning-period ARR from cancellations and contractions / beginning-period ARR; exclude expansions.
- **Net revenue retention (NRR):** (Beginning ARR − churn − contraction + expansion) / beginning ARR, for a fixed starting cohort.
- **Logo churn:** Customers terminated in a period / active customers at the start of that period.
- **Seat:** One managed user or device identity designated as billable in a customer's contract. Each billable identity is unique within a tenant; the same identity cannot be charged twice within a billing period.
- **Headcount:** Employees only, one person allocated to one primary department; excludes contractors and vacancies.

Churn and NRR are defined but **not assigned invented historical values**.

## 3. Product Portfolio and Subscription Plans

### 3.1 Modules

| Module | Primary capabilities | System-of-record objects |
|---|---|---|
| **AssetTracker** | Asset inventory, lifecycle status, ownership, discovery connectors, audit exports | Asset, owner, location, discovery event |
| **LicenseOptimizer** | Entitlement registry, usage reconciliation, renewal calendar, optimization recommendations | License, entitlement, allocation, usage record |
| **SecurityShield** | Asset risk scoring, policy findings, vulnerability correlation, remediation queues | Finding, policy, exposure, remediation task |

All modules share a tenant-scoped identity layer, RBAC, immutable audit events, and common reporting. SecurityShield highlights risks but does not claim to replace endpoint protection or a SIEM.

### 3.2 Plan entitlements and commercial terms

| Attribute | Starter (SMB) | Growth (Mid-Market) | Enterprise |
|---|---|---|---|
| Eligible committed seats | 50–249 | 250–999 | 1,000–10,000 |
| Price / seat / month (equivalent) | $50.00 | $25.00 | $11.666666… |
| **Invoice unit price / seat / year** | **$600.00** | **$300.00** | **$140.00** |
| Billing | Annual prepaid | Annual prepaid | Annual prepaid |
| AssetTracker | Standard discovery and inventory | Advanced integrations | Advanced + custom connector configuration |
| LicenseOptimizer | Usage and entitlement reports | Automated reconciliation | Automated reconciliation + policy workflows |
| SecurityShield | Standard findings | Prioritized risk dashboards | Custom policies and governance exports |
| SSO/SAML | No | Yes | Yes |
| Audit retention | 90 days | 365 days | 730 days |
| Monthly uptime SLA | 99.5% | 99.9% | 99.95% |
| Support coverage | Business hours | 24×5 for P1/P2; business hours P3/P4 | 24×7 for P1/P2; business hours P3/P4 |
| Initial response: P1 | 4 business hours | 1 clock hour | 15 clock minutes |
| Initial response: P2 | 8 business hours | 4 clock hours | 1 clock hour |
| Initial response: P3 | 1 business day | 1 business day | 8 business hours |
| Initial response: P4 | 2 business days | 2 business days | 1 business day |
| Customer success | Shared pool | Named CSM | Named CSM + quarterly business review |

**Business hours:** Monday–Friday, 09:00–17:00 America/Chicago, excluding company-observed US holidays. One business day = 8 business hours. Clock-hour support clocks run only during the stated coverage schedule; for Growth P1/P2, 24×5 means Monday 00:00 through Friday 23:59:59 America/Chicago, excluding weekends but not holidays. Enterprise P1/P2 clocks run continuously. Initial response means human acknowledgment with triage; automated receipts do not satisfy it.

### 3.3 Availability SLA rules

Availability is calculated monthly per tenant as `100 × (eligible minutes − unavailable eligible minutes) / eligible minutes`. Eligible minutes exclude announced maintenance (at least 72 hours' notice, maximum 4 hours per calendar month), force majeure, customer-side network failures, and third-party systems outside NexusFlow control. A minute is unavailable when authenticated core API requests fail for more than 50% of synthetic checks in that minute, measured by NexusFlow monitoring and corroborated with incident logs. Scheduled maintenance is excluded from both numerator and denominator. SLA commitments apply to production, not sandbox environments.

For a verified monthly uptime miss, the customer may request a service credit within 30 calendar days of month-end: 5% of the affected month's attributable recurring subscription charge for a miss of up to 0.5 percentage points below the commitment; 10% for a miss greater than 0.5 and up to 2.0 points; 20% for a miss greater than 2.0 points. Credits are capped at 20% of the affected month's charge, applied to the next invoice, noncash, and are the sole contractual uptime remedy in this fictional policy. This credit schedule is independent of support ticket resolution targets.

## 4. Organizational Structure

### 4.1 Headcount reconciliation

| Department | Department head / manager title | Employees including head | Key roles and allocated headcounts |
|---|---|---:|---|
| Engineering | VP Engineering | **38** | VP Engineering 1; Engineering Managers 3; Software Engineers 21; SREs 4; QA Engineers 4; Product Managers 3; UX Designers 2 |
| Sales | VP Sales | **18** | VP Sales 1; Sales Managers 2; Account Executives 9; SDRs 4; Sales Operations Analysts 2 |
| Customer Success/Support | VP Customer Success | **17** | VP Customer Success 1; CS Manager 1; Support Manager 1; Customer Success Managers 7; Support Engineers 7 |
| Operations/HR | Director of Operations & People | **8** | Director 1; HR Business Partners 2; IT Administrators 2; Security & Compliance Analysts 2; Workplace Coordinator 1 |
| Finance | Finance Director | **4** | Finance Director 1; Controller 1; Billing Analyst 1; FP&A Analyst 1 |
| **Total** | **5 department heads** | **85** | **38 + 18 + 17 + 8 + 4 = 85** |

There is no additional CEO, executive assistant, legal, marketing, or separate product department counted outside these five allocations. For this fictional operating model, the five department heads form a **five-person executive operating committee**; the VP Engineering serves as committee chair without an additional position or headcount. External counsel and specialist marketing agencies are vendors, not employees.

### 4.2 Decision rights

- **VP Engineering:** Architecture, production change control, service reliability, technical incident command; Product Managers own feature prioritization under Engineering.
- **VP Sales:** Pipeline, new bookings, commercial negotiations within approved price policy.
- **VP Customer Success:** Onboarding, customer health, support queue ownership, renewal coordination.
- **Director of Operations & People:** Hiring administration, workforce access, vendor security reviews, internal IT.
- **Finance Director:** Invoicing, revenue reconciliation, credit approval, billing controls, financial close.
- **Executive operating committee:** Majority approval (at least 3 of 5) for changes to standard pricing, risk appetite, or this baseline specification. The relevant department head remains the accountable execution owner.

Segregation of duties: sales cannot approve its own nonstandard pricing; Support cannot issue billing credits; Engineering cannot unilaterally authorize a customer's paid seat increase above the approval threshold.

## 5. Operational Workflows and Business Logic

### 5.1 Ticket severity and resolution targets

**Resolution** means service restored or an accepted, documented workaround is delivered and verified by the customer or incident commander. Root-cause analysis can follow later. Severity is assigned by Support at intake, and the incident commander can raise it immediately; downgrades require documented approval by the Support Manager. The SLA resolution clock starts at ticket creation, follows the same plan-specific coverage schedule as the applicable response clock, and stops on verified resolution. Waiting for customer input does **not** pause P1/P2 clocks; for P3/P4, a documented `WAITING_CUSTOMER` state pauses the clock, with timestamps recorded.

| Priority | Definition | Starter resolution | Growth resolution | Enterprise resolution |
|---|---|---|---|---|
| **P1 Critical** | Production-wide outage, severe security incident, or essential service unusable | 2 business days | 8 coverage hours | 4 clock hours |
| **P2 High** | Major feature unavailable, no viable workaround, material business impact | 4 business days | 2 coverage days (48 coverage hours) | 12 clock hours |
| **P3 Medium** | Partial degradation with workaround or noncritical defect | 10 business days | 5 business days | 3 business days |
| **P4 Low** | How-to request, cosmetic defect, minor enhancement | 20 business days | 15 business days | 10 business days |

For Growth, **coverage hours** for P1/P2 accumulate only during the 24×5 support window; 48 coverage hours = two complete weekday coverage days. For Starter all priority clocks use business hours. For Enterprise P1/P2 clocks run 24×7, and P3/P4 use business hours. All targets are maximum elapsed eligible time, not guaranteed engineering fix dates. The ticket's plan and severity are snapshotted at creation; changes are audit-logged and prospective unless correction of an intake error is authorized.

**Escalation:** P1 pages on-call Support and SRE immediately when their plan coverage is active; unresolved P1 escalates to VP Engineering at 50% of the resolution target and to the executive operating committee chair at 75%. P2 escalates to Support Manager at 50% and VP Customer Success at 75%. Breach events are recorded even when a workaround subsequently resolves the ticket. Response and resolution targets are contractual service objectives; support resolution misses trigger service review and corrective action, not automatic uptime credits.

### 5.2 New customer onboarding

1. Sales creates an opportunity and proposed plan; Finance validates legal entity, tax treatment, and annual contract value.
2. Authorized customer signer and NexusFlow Sales sign a 12-month order form; Finance marks the contract `ACTIVE` only after signature and billing validation.
3. Provisioning service creates a unique tenant, default admin role, and module entitlements, and enforces committed-seat limits.
4. Customer Success performs kickoff, identity configuration, discovery connector setup, and acceptance checklist.
5. All provisioning, access grants, and billing events are written to the audit log with actor, timestamp, tenant, and correlation ID.

**Provisioning timing objective:** Within 1 business day of contract activation, excluding customer-dependent setup. Trial tenants are nonbillable and excluded from the baseline.

### 5.3 Seat additions and approval controls

- A requested seat addition is evaluated against the **incremental recurring monthly-equivalent amount**, calculated as `added seats × annual unit price / 12` at full precision. No discounts exist in the baseline.
- If incremental amount is **greater than $500.00/month**, **human approval is mandatory before any paid seats are provisioned**. Exactly $500.00 does not cross the threshold.
- The customer's authorized billing administrator must request/consent to all paid additions. For amounts above $500, a NexusFlow Finance Director or formally delegated Billing Analyst must additionally approve; the requester cannot approve their own request.
- The threshold applies to the **aggregate of all pending and approved seat additions for the same customer within a rolling 30-calendar-day window**, measured against the committed-seat count at the beginning of that window. Splitting requests does not bypass approval.
- Seat additions must not exceed the plan's maximum; crossing a tier boundary requires a signed plan amendment and new unit pricing before provisioning.
- Approved additions are effective on the approved activation date. Incremental recurring ARR = added seats × annual unit price; the first annual-term invoice is prorated by remaining service days using actual days in the term. Thereafter additions co-terminate with the original contract and are billed at the next renewal as part of the new annual seat commitment.
- Rejected or expired requests create no entitlements or charges. Approval records store approver ID, timestamp, amount, decision, and amendment reference. An approval expires after 7 calendar days if not activated.

**Examples:** Starter +10 seats = $500/month equivalent, customer authorization sufficient; Starter +11 = $550/month, human Finance approval required. Growth +20 = $500, no additional Finance approval; Growth +21 = $525, approval required. Enterprise +42 = $490/month equivalent, no additional Finance approval; Enterprise +43 = $501.666…/month, approval required. These examples assume no other additions in the rolling window and no plan-limit violation.

### 5.4 Contract renewal, cancellation, and churn

- Default initial term: 12 months; renewals are successive 12-month terms unless amended in writing.
- **30-day notice window:** NexusFlow sends an automated renewal reminder **60 and 30 calendar days before** the current term ends. The customer must deliver a cancellation/nonrenewal notice **at least 30 calendar days before** term end; receipt by 23:59:59 in the contract's America/Chicago time zone on the deadline is timely.
- If valid notice is not received by the deadline, the contract **automatically renews** for another 12 months at the then-current signed seat count and existing contractual annual unit price, unless a mutually signed amendment states otherwise. NexusFlow does not unilaterally change pricing at renewal under this baseline policy.
- Cancellations received after the deadline are treated as requests for the **following** term, subject to applicable law and negotiated terms; Customer Success confirms the effective date in writing.
- Midterm cancellation does not automatically generate a refund; any exception requires Finance Director approval and written contract amendment. The subscription remains active until the paid-through termination date unless the contract explicitly provides otherwise.
- Customer Success opens a renewal case 90 days before expiration, reviews adoption and risk, and records `RENEW`, `NONRENEW`, or `PENDING`. Sales owns negotiated expansions; Finance validates the final annual contract value.
- At effective termination, tenant access becomes read-only for 30 calendar days for export, then disabled. Customer data is scheduled for deletion 60 calendar days after termination, subject to legal holds and contractual exceptions; deletion completion is logged.
- **Logo churn recognition:** Customer is counted as churned on the effective termination date, not the notice date. **ARR churn recognition:** Lost recurring value is recognized in operational ARR metrics on the same effective termination date. A contraction without termination affects revenue churn but not logo churn.

### 5.5 Incident, change, and billing workflow

**Incident:** Monitoring alert/customer report → ticket and severity → on-call triage → mitigation → customer update → verified restoration → post-incident review for P1 within 5 business days. P1 customer updates at least every 60 eligible coverage minutes; P2 every 4 eligible coverage hours. A security incident also notifies the Security & Compliance Analysts.

**Production change:** Engineering PR and automated tests → independent reviewer → deployment approval by Engineering Manager (emergency changes by on-call SRE plus retrospective approval) → rollout monitoring → rollback if error budget or service health thresholds are breached. Every production deployment links to a change record.

**Billing:** Contract activation/amendment → validated seat ledger → annual prepaid invoice → payment reconciliation → ARR ledger update. Billing Analyst prepares, Controller reviews exceptions, Finance Director approves credits. Invoices use exact annual-unit-price arithmetic; only final payable invoice totals are rounded to cents using half-up rounding.

## 6. Naming, Keys, and Entity Conventions

### 6.1 Primary key formats

All IDs are immutable, uppercase ASCII, unique within their entity namespace, and allocated centrally. `XXXX` denotes a zero-padded, **four-digit decimal sequence** from `0001` through `9999`, never reused after deletion. A sequence exhaustion event blocks creation and requires an approved schema migration; IDs must never silently wrap or switch format.

| Entity | Key field | Required format | Example |
|---|---|---|---|
| Customer account | `customer_id` | `CUST-XXXX` | `CUST-0001` |
| Employee | `employee_id` | `EMP-XXXX` | `EMP-0001` |
| Contract | `contract_id` | `CTR-XXXX` | `CTR-0001` |
| Support ticket | `ticket_id` | `TK-XXXX` | `TK-0001` |
| Tenant | `tenant_id` | `TEN-XXXX` | `TEN-0001` |
| Subscription | `subscription_id` | `SUB-XXXX` | `SUB-0001` |
| Managed asset | `asset_id` | `AST-XXXX` | `AST-0001` |
| License entitlement | `license_id` | `LIC-XXXX` | `LIC-0001` |
| Security finding | `finding_id` | `FND-XXXX` | `FND-0001` |
| Approval request | `approval_id` | `APR-XXXX` | `APR-0001` |
| Audit event | `audit_id` | `AUD-XXXX` | `AUD-0001` |

**Validation regex:** `^(CUST|EMP|CTR|TK|TEN|SUB|AST|LIC|FND|APR|AUD)-[0-9]{4}$`; each entity must additionally enforce its specific prefix. The 9,999-ID namespace capacity is a **deliberate fictional v1.0 limitation** and is not suitable for high-volume event, asset, or ticket production without migration. For the modeled 46,000 billable seats, seats are **not** given four-digit global IDs: use composite key `(tenant_id, seat_external_key)` where `seat_external_key` is a normalized, tenant-scoped string of 1–128 characters. Audit events at production scale must migrate to a larger ID format before capacity is exhausted.

### 6.2 Canonical data model

| Entity | Required relationships | Cardinality / integrity |
|---|---|---|
| Customer | tenant, contracts, subscriptions | Exactly one active tenant and one active subscription in baseline |
| Tenant | customer, seats, assets, findings | Exactly one owning customer; strict tenant isolation |
| Contract | customer, subscription | Exactly one customer and one subscription; at most one active contract per customer |
| Subscription | customer, contract, plan | Exactly one plan; committed seat count within plan bounds |
| Ticket | customer, tenant, priority, assignee | Exactly one customer and tenant; assignee may be null before triage |
| Approval | customer, requestor, approver | Approver mandatory for above-threshold requests |
| Asset | tenant, owner (optional) | Exactly one tenant; may have zero or one assigned owner |
| License | tenant, vendor, allocations | Exactly one tenant; allocations cannot exceed purchased entitlements without a finding |
| Finding | tenant, asset (optional), policy | Exactly one tenant; optional related asset |
| Employee | department, manager (optional) | Exactly one primary department; department heads have no internal manager |

All tenant-owned data access must filter by `tenant_id`; foreign keys must prevent cross-tenant associations. User-facing display names are not keys. Records use UTC ISO 8601 timestamps (`YYYY-MM-DDTHH:mm:ss.sssZ`), USD amounts as integer cents or exact decimal values, and enumerated lifecycle states. No floating-point binary arithmetic is permitted for money.

### 6.3 Lifecycle enums

- `contract_status`: `DRAFT`, `PENDING_SIGNATURE`, `ACTIVE`, `NONRENEWING`, `EXPIRED`, `TERMINATED`.
- `subscription_status`: `PENDING`, `ACTIVE`, `SUSPENDED`, `TERMINATED`.
- `ticket_status`: `NEW`, `TRIAGED`, `IN_PROGRESS`, `WAITING_CUSTOMER`, `RESOLVED`, `CLOSED`.
- `ticket_priority`: `P1`, `P2`, `P3`, `P4`.
- `approval_status`: `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `ACTIVATED`.
- `plan_code`: `STARTER`, `GROWTH`, `ENTERPRISE`.
- `customer_segment`: `SMB`, `MID_MARKET`, `ENTERPRISE`.

## 7. Architecture, Security, and Governance Requirements

**Logical architecture:** Web UI and API gateway → authentication/RBAC → tenant-aware services for assets, licenses, security findings, subscriptions, tickets, and approvals → relational transactional store plus event/audit pipeline → analytics/reporting. Discovery connectors ingest source-system data asynchronously; imports must be idempotent using `(tenant_id, source_system, source_record_id)`.

**Security controls:** SSO/SAML according to plan; MFA mandatory for all NexusFlow employees and customer administrators; least-privilege RBAC; TLS in transit; encrypted storage at rest; centralized secrets management; tenant isolation enforced in authorization and database queries; audit events append-only and access-controlled. Vendor compliance certification is **not asserted** by this fictional specification.

**Reliability controls:** Monitored availability probes, alert routing, daily encrypted backups, quarterly restore tests, and documented incident runbooks. Internal recovery objectives: RPO ≤ 24 hours and RTO ≤ 8 hours for catastrophic restoration scenarios; these are engineering objectives, separate from customer uptime SLAs.

**Governance:** Specification changes require version bump, recorded change request, impact assessment for ARR/headcount/SLAs, and executive operating committee approval. Finance is the owner of baseline metric reconciliation; VP Engineering owns data schemas and implementation conformance.

## 8. Acceptance Criteria and Invariants

1. `SUM(customer_count_by_tier) = 60 + 40 + 20 = 120`.
2. `SUM(employee_count_by_department) = 38 + 18 + 17 + 8 + 4 = 85`.
3. `SUM(contracted_seats_by_tier) = 46,000`.
4. `SUM(active_contract_annual_value) = $12,400,000.00` exactly, with annual unit prices stored as decimal currency values.
5. Each active baseline account maps to exactly one active tenant, subscription, and contract; exactly 120 of each are active in this snapshot.
6. Plan seat bands and customer segments align; no baseline account falls outside its plan's allowed seat range.
7. A seat addition with incremental monthly-equivalent value **>$500**, including rolling-window aggregation, cannot activate without customer authorization and recorded human Finance approval.
8. A timely nonrenewal notice at least 30 calendar days before expiration prevents the next auto-renewal; notices after the cutoff do not silently alter the current renewal.
9. Every ticket has one priority, a plan-derived coverage calendar, and distinct initial-response and resolution targets; SLA breaches are auditable.
10. All IDs pass entity-specific prefix and four-digit validation; no deleted ID is reassigned.
11. No monetary calculation uses binary floating point; annual unit prices are authoritative for billing and ARR.
12. All tenant-scoped reads/writes enforce tenant authorization; all contract, approval, provisioning, and billing transitions produce audit records.

## 9. Explicit Assumptions and Boundaries

This is a **fictional specification**, not a statement about a real company, verified market performance, or legally reviewed service agreement. The customer distribution, uniform per-tier seat commitments, unit prices, and organizational structure are deliberately constructed to satisfy the fixed financial and staffing baselines. The model assumes no discounts, no trials counted as customers, no add-on revenue, no partial-term contract adjustments in the snapshot, and no employees allocated to more than one department. Enterprise pricing is intentionally volume-discounted and specified by exact annual unit price. Support targets, service credits, privacy practices, and renewal clauses are proposed internal policies requiring legal and operational review before any real-world use.

## 10. Change Log

| Version | Date | Change | Approver |
|---|---|---|---|
| 1.0 | 2026-10-28 | Initial authoritative fictional company specification; reconciled ARR, accounts, headcount, product, SLA, renewals, provisioning, and entity conventions | Fictional executive operating committee (baseline designation) |
