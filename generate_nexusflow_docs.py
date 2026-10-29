#!/usr/bin/env python3
"""Generate interconnected fictional NexusFlow Markdown business documents.

Python 3.10+; standard library only.
Usage: python generate_nexusflow_docs.py --data-dir ./nexusflow_csv --output-dir ./nexusflow_documents
Uses the approved CSV snapshot; fails closed if references or baseline drift.
All names, incidents, notices, and discussions are SYNTHETIC scenarios, not actual
operational events. Source rows are never modified.
"""
import argparse
import csv
import json
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

AS_OF = date(2026, 10, 28)
TABLES = ('departments','employees','products','customers','contracts','invoices','payments','support_tickets','assets')


def read_tables(folder):
    result = {}
    for table in TABLES:
        path = folder / (table + '.csv')
        with path.open(encoding='utf-8-sig', newline='') as f:
            result[table] = list(csv.DictReader(f))
    return result


def assert_baseline(t):
    assert len(t['customers']) == 120
    assert Counter(c['tier'] for c in t['customers']) == {'SMB':70,'MID_MARKET':38,'ENTERPRISE':12}
    assert len(t['employees']) == 85 and all(e['status']=='ACTIVE' for e in t['employees'])
    assert len(t['departments']) == 5 and sum(int(d['headcount']) for d in t['departments']) == 85
    assert sum(Decimal(c['annual_recurring_revenue']) for c in t['contracts'] if c['status']=='ACTIVE') == Decimal('12400000.00')
    assert len([c for c in t['customers'] if c['status']=='DELINQUENT']) == 6
    assert {p['name'] for p in t['products']} == {'AssetTracker','LicenseOptimizer','SecurityShield'}
    customer_ids={c['customer_id'] for c in t['customers']}
    employee_ids={e['employee_id'] for e in t['employees']}
    contract_ids={c['contract_id'] for c in t['contracts']}
    assert all(c['customer_id'] in customer_ids for c in t['contracts'])
    assert all(i['contract_id'] in contract_ids and i['customer_id'] in customer_ids for i in t['invoices'])
    assert all(x['assigned_employee_id'] in employee_ids for x in t['support_tickets'])


def write(out, relative, content):
    target=out / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.strip()+'\n', encoding='utf-8')
    return {'file':relative,'bytes':target.stat().st_size}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parent/'nexusflow_csv')
    parser.add_argument('--output-dir',type=Path,default=Path(__file__).resolve().parent/'nexusflow_documents')
    args=parser.parse_args()
    t=read_tables(args.data_dir)
    assert_baseline(t)
    employees={e['employee_id']:e for e in t['employees']}
    customers={c['customer_id']:c for c in t['customers']}
    contracts={c['customer_id']:c for c in t['contracts']}
    invoices={i['customer_id']:i for i in t['invoices']}
    def person(eid):
        e=employees[eid]
        return f"{e['first_name']} {e['last_name']} ({eid}; {e['role']}; {e['email']})"
    def money(v): return f"${Decimal(v):,.2f}"
    enterprise=next(c for c in t['customers'] if c['tier']=='ENTERPRISE' and any(x['customer_id']==c['customer_id'] and x['status'] in ('IN_PROGRESS','TRIAGED') for x in t['support_tickets']))
    ecid=enterprise['customer_id']; econ=contracts[ecid]
    eticket=next(x for x in t['support_tickets'] if x['customer_id']==ecid and x['status'] in ('IN_PROGRESS','TRIAGED') )
    delinquent=next(c for c in t['customers'] if c['status']=='DELINQUENT' and invoices[c['customer_id']]['status']=='OVERDUE')
    dcid=delinquent['customer_id']; dinv=invoices[dcid]; dcon=contracts[dcid]
    finance=next(e for e in t['employees'] if e['role']=='Billing Analyst')
    director=next(e for e in t['employees'] if e['role']=='Finance Director')
    support_manager=next(e for e in t['employees'] if e['role']=='Support Manager')
    vp_engineering=next(e for e in t['employees'] if e['role']=='VP Engineering')
    vp_cs=next(e for e in t['employees'] if e['role']=='VP Customer Success')
    csm=employees[enterprise['csm_employee_id']]
    dcsm=employees[delinquent['csm_employee_id']]
    assignee=employees[eticket['assigned_employee_id']]
    issue=date.fromisoformat(dinv['issue_date']); due=date.fromisoformat(dinv['due_date'])
    assert issue <= due < AS_OF
    # Synthetic communications do not alter the underlying open-ticket or invoice rows.
    common=f"**Document classification:** Fictional training artifact. **Dataset snapshot:** {AS_OF.isoformat()}. **ARR baseline:** $12,400,000.00; 120 accounts; 85 active employees."
    documents={}
    documents['legal/MSA_Template_v1.0.md']=f"""
# NexusFlow Systems — Master Services Agreement (MSA) v1.0

{common}

**Status:** Model contract template, not executed legal advice. **Owner:** Finance Director ({director['employee_id']}) with external counsel review. **Effective date for template:** 2026-10-28. **Governing-law proposal:** Delaware, USA; parties may negotiate by signed amendment.

## 1. Parties, order of precedence, and scope
NexusFlow Systems (Provider) and the customer identified on a signed order form (Customer) agree to this MSA. A signed order form identifies `CUST-XXXX`, `CTR-XXXX`, service tier, committed seats, annual fee, dates and permitted users. The order of precedence is signed amendment, signed order form/SOW, data processing addendum, this MSA, then published operational SOPs. SOPs cannot silently change negotiated commercial terms.

## 2. Services and subscription
Provider supplies hosted AssetTracker, LicenseOptimizer, and SecurityShield, subject to the selected Starter, Growth, or Enterprise entitlements. Fees are annual prepaid and calculated from committed seats using annual unit prices of $600, $300, and $140 respectively. The annual-unit-price convention controls over rounded monthly equivalents. Provider may not charge an unapproved expansion. Usage, seat counts and entitlements must be auditable by tenant.

## 3. Availability and service levels
Monthly production uptime targets: Starter **99.5%**, Growth **99.9%**, Enterprise **99.95%**. Availability = 100 × (eligible minutes − unavailable eligible minutes) / eligible minutes. Maintenance announced at least 72 hours ahead, limited to four hours per calendar month, is excluded from eligible minutes. Also excluded are force majeure, customer networks and independent third-party outages. A minute is unavailable when more than half of synthetic authenticated core API checks fail. Incident response and resolution targets follow `SOP_01_Ticket_Escalation.md` and the plan-specific matrix in the company specification. A ticket-resolution miss does not automatically create an uptime credit. Any negotiated credit must appear on the signed order form; this template creates no automatic credit percentage.

## 4. Customer and provider obligations
Customer must provide lawful input data, designate administrators, maintain endpoint security and authorize personnel. Provider must use least-privilege tenant access, segregate tenant records, log privileged actions, encrypt data in transit and at rest, maintain tested backups, review vulnerabilities and restrict subcontractor access. SecurityShield findings are decision support, not a guarantee against breaches. Provider shall notify Customer without undue delay after confirming a security incident affecting Customer data and cooperate on legally required notices; a fixed notification deadline requires an executed data processing addendum.

## 5. Data ownership and retention
Customer owns Customer Data. Provider receives a limited right to process it to deliver and secure services. At termination, Customer may request a machine-readable export within 30 calendar days. Provider targets deletion within 90 days after termination, except legal holds and backups retained under documented schedules. Processing locations, subprocessors and any cross-border transfer mechanism are governed by the signed data processing addendum.

## 6. Fees, late payment, and disputed charges
Invoices are issued on the contract start date and due 30 calendar days later unless the signed order form varies this. Provider shall flag overdue balances, provide a written collection notice, and allow a 15-calendar-day cure period following that notice before discretionary service suspension, subject to legal restrictions and documented security exceptions. A good-faith written invoice dispute raised before suspension triggers Finance review; undisputed balances remain payable. Suspension does not extinguish payment obligations. Finance Director must approve credits and nonstandard payment terms. See `correspondence/Finance_Collection_Notice.md` for a **simulation**, not an issued notice.

## 7. Term, renewal, and termination
Initial term is 12 months; automatic renewals are successive 12-month terms at the signed annual unit price and committed seats. Provider sends reminders 60 and 30 days before expiry. Customer's written nonrenewal notice must arrive **at least 30 calendar days** before term end, by 23:59:59 America/Chicago on the deadline. Midterm cancellation carries no automatic refund. Material breach may be terminated after written notice and a reasonable cure period specified in the order form; immediate suspension for verified security threats is reserved with documented approval.

## 8. Confidentiality and intellectual property
Each party protects nonpublic business and technical information using reasonable care and may disclose only to personnel and advisers with a need to know. Provider retains platform IP; Customer retains its data and trademarks. No reverse engineering except where mandatory law permits.

## 9. Liability and exclusions
Except for excluded claims, each party's aggregate liability is capped at fees paid or payable under the affected contract during the preceding 12 months. Neither party is liable for indirect or consequential losses, lost profits or business interruption, except where law prohibits exclusion. Caps and exclusions do not apply to fraud, willful misconduct, payment obligations, or liabilities that cannot lawfully be limited. Any special data-breach cap requires a signed amendment. These are **fictional proposed terms**, not a statement of existing customer agreements.

## 10. Disputes and execution
Parties first escalate a dispute to their designated business owners for 15 days before commencing proceedings, except urgent injunctive relief. Amendments require authorized signatures. Electronic signatures are acceptable. No signature is represented in this training template.

## Cross-references
`SOW_Enterprise_Client_Sample.md`; `SOP_01_Ticket_Escalation.md`; `SOP_02_Seat_Provisioning.md`; `Finance_Collection_Notice.md`.
"""
    documents['legal/SOW_Enterprise_Client_Sample.md']=f"""
# Statement of Work — {enterprise['company_name']}

{common}

**Status:** Completed fictional SOW example; NOT an executed agreement. **Customer:** {enterprise['company_name']} (`{ecid}`), `{enterprise['domain']}`. **Contract:** `{econ['contract_id']}`. **Plan:** {econ['plan_tier']}. **Term:** {econ['start_date']} through {econ['end_date']} (end exclusive). **Customer owner:** IT Operations Lead (role only; synthetic external correspondent). **NexusFlow CSM:** {person(enterprise['csm_employee_id'])}.

## 1. Commercial schedule
- Committed managed seats: **{int(enterprise['total_seats']):,}**.
- Enterprise annual unit price: **$140.00 per seat per year**, equivalent to $11.666666… per seat per month (display only).
- Annual recurring subscription amount: **{money(econ['annual_recurring_revenue'])}** = {enterprise['total_seats']} × $140.00.
- Billing: annual prepaid; initial invoice on term start; payment due 30 days after issue. No professional services or implementation fee included in ARR.
- Auto-renewal: `{econ['auto_renew']}`; nonrenewal notice required 30 days before term end, per MSA.

## 2. Included scope and acceptance
AssetTracker tenant setup, discovery connectors and asset inventory; LicenseOptimizer entitlement reconciliation and license utilization reports; SecurityShield security posture findings and policy views. NexusFlow will configure SSO, role-based admin access and audit logging. Acceptance requires successful administrator login, sample asset sync, sample entitlement reconciliation and verified role restrictions; CSM records acceptance evidence. Customer supplies credentials and lawful data. Unavailable customer integrations are tracked as dependencies, not silently treated as delivered.

## 3. Service commitments
Enterprise uptime **99.95% monthly**; P1 initial human response **15 clock minutes**, resolution **4 clock hours**, both 24×7. P2 initial response **1 clock hour**, resolution **12 clock hours**. P3 initial response **8 business hours**, resolution **3 business days**; P4 initial response **1 business day**, resolution **10 business days**. Business hours are Mon–Fri 09:00–17:00 America/Chicago, excluding observed US holidays. Resolution requires verified restoration or an accepted workaround; RCA may follow. See `sops/SOP_01_Ticket_Escalation.md`.

## 4. Change control and business friction example
The customer asked in a synthetic follow-up for "about 50 additional seats, perhaps temporarily." At $140 per seat per year, **50 seats = $583.333… monthly equivalent**, above the $500 approval threshold; **no seats are provisioned** until a human Finance approval and customer authorization are recorded. An exact seat count, effective date, and whether a signed amendment is required remain unresolved. Approval expires seven days after grant if not activated. Splitting into smaller requests within a rolling 30 days does not bypass approval. See `sops/SOP_02_Seat_Provisioning.md`.

## 5. Roles and communications
CSM coordinates onboarding and renewals. Support owns ticket triage; on-call SRE owns mitigation; Finance approves nonstandard billing and high-value seat expansions. All P1 incident communications must cite the canonical ticket, e.g. `{eticket['ticket_id']}`. The example thread in `correspondence/Email_Thread_P1_Outage.md` is a synthetic scenario linked to this contract, not proof of actual downtime.

## 6. Exclusions and assumptions
No bespoke development, migration of historical licenses, customer-managed network repair, tax calculation, or guaranteed security outcome. Any material change to scope or price requires a signed amendment. This completed example contains no actual signatures.
"""
    documents['sops/SOP_01_Ticket_Escalation.md']=f"""
# SOP-01 — Ticket Triage, Escalation, and SLA Clocks

{common}

**Owner:** {person(support_manager['employee_id'])}. **Executive escalation:** {person(vp_engineering['employee_id'])}. **Effective:** 2026-10-28. **System of record:** `support_tickets`.

## 1. Severity rubric
| Priority | Qualifying condition | Starter resolution | Growth resolution | Enterprise resolution |
|---|---|---|---|---|
| P1 | Production-wide outage, severe security incident, essential service unusable | 2 business days | 8 coverage hours | 4 clock hours |
| P2 | Major feature unavailable, no viable workaround, material impact | 4 business days | 48 coverage hours | 12 clock hours |
| P3 | Partial degradation with workaround | 10 business days | 5 business days | 3 business days |
| P4 | How-to, cosmetic issue, minor enhancement | 20 business days | 15 business days | 10 business days |

## 2. Human initial response matrix
| Priority | Starter | Growth | Enterprise |
|---|---|---|---|
| P1 | 4 business hours | 1 coverage hour | 15 clock minutes |
| P2 | 8 business hours | 4 coverage hours | 1 clock hour |
| P3 | 1 business day | 1 business day | 8 business hours |
| P4 | 2 business days | 2 business days | 1 business day |

Business hours = Mon–Fri 09:00–17:00 America/Chicago, excluding observed US holidays; one business day = eight business hours. Growth P1/P2 coverage = Monday 00:00 through Friday 23:59:59 America/Chicago, including weekday holidays; Enterprise P1/P2 = 24×7. All other clocks run in business hours. Store event timestamps in UTC and calculate eligibility in America/Chicago with DST-aware time-zone logic. Auto-replies do not count as human responses.

## 3. Procedure
1. Log ticket ID, customer ID, plan snapshot, severity, creation timestamp, reporter, owner, business impact and evidence. Confirm whether incident is security-related.
2. Start response and resolution clocks at ticket creation. Human triage acknowledges the customer and confirms severity. Support may raise severity immediately; downgrade requires Support Manager's documented approval.
3. Page on-call Support and SRE immediately for P1 within covered hours. At **50%** of P1 resolution budget, escalate to VP Engineering; at **75%**, escalate to the executive committee chair (same VP Engineering role, separate executive escalation record). P2 goes to Support Manager at 50% and VP Customer Success at 75%.
4. Send P1 updates at least every 60 eligible coverage minutes and P2 updates every four eligible coverage hours. Log acknowledgments, updates, mitigation, breach and verification events.
5. P1/P2 clocks **never pause** for customer input. P3/P4 resolution clocks may pause only in documented `WAITING_CUSTOMER` state with explicit begin/end timestamps; the flat CSV status vocabulary may not encode this state, so store it in the future event ledger rather than overwrite the snapshot.
6. Stop the resolution clock only after verified restoration or accepted workaround, record resolver and verification. Open a P1 post-incident review within five business days. An overdue SLA is still a breach even if later resolved.

## 4. Synthetic example and ambiguity
Ticket `{eticket['ticket_id']}` belongs to `{ecid}` (Enterprise) and is assigned to {person(assignee['employee_id'])}. The source row records severity `{eticket['severity']}` (the hypothetical P1 reclassification is not in the dataset), status `{eticket['status']}`, created `{eticket['created_at']}`, resolved `{eticket['resolved_at'] or 'NULL'}`. The email thread demonstrates a **hypothetical P1 severity escalation**; it must not be interpreted as a resolved database ticket. Customer says "the portal is slow" while monitoring suggests a broad API outage: retain P1 until the Support Manager authorizes a downgrade. Do not assume an email saying "looks better" proves verified restoration.

## 5. Audit and retrieval controls
Authoritative facts are CSV status and timestamps; correspondence is scenario narrative. Never infer `resolved_at` from email timestamps. Escalation, customer updates, incident milestones and SLA pauses require an event table not present in the nine-table baseline schema. See `correspondence/Email_Thread_P1_Outage.md` and `legal/SOW_Enterprise_Client_Sample.md`.
"""
    documents['sops/SOP_02_Seat_Provisioning.md']=f"""
# SOP-02 — Managed Seat Expansion and Human Approval

{common}

**Owner:** {person(director['employee_id'])}. **Commercial counterpart:** Customer Success and Sales. **Effective:** 2026-10-28.

## 1. Rule and arithmetic
For each request, calculate `incremental_monthly_equivalent = added_seats × annual_unit_price / 12` using exact decimal arithmetic. Starter $600/year ($50/month); Growth $300/year ($25/month); Enterprise $140/year ($11.666666…/month). **Human Finance approval is mandatory when the aggregated incremental monthly equivalent is strictly greater than $500.00.** Exactly $500.00 does not trigger this extra approval, but customer authorization remains mandatory.

## 2. Rolling 30-calendar-day window
At evaluation time T, find all pending and approved seat additions for the customer submitted in `(T − 30 days, T]`; compute aggregate requested incremental seats against the committed-seat baseline at the start of that window, counting previously activated additions only once. Exclude rejected/expired requests, but do not exclude approved-not-yet-activated additions. Maintain a stable event ledger and evaluate under a tenant-scoped transaction lock to prevent concurrent requests from evading the threshold. The existing nine-table CSV snapshot has **no approval event table**: all `APR-XXXX` references in training discussions are scenario-only and must not be treated as database rows.

## 3. Workflow
1. Capture `CUST-XXXX`, `CTR-XXXX`, requested count, request timestamp, desired activation date, business justification, and authorized customer requester.
2. Validate customer/contract active, signed seat count and tier limits (Starter 50–249, Growth 250–999, Enterprise 1,000–10,000). A tier-crossing request requires a signed plan amendment before provisioning.
3. Lock customer seat ledger; compute 30-day aggregate and monthly equivalent using `Decimal`; record full calculation and prior requests.
4. If aggregate exceeds $500/month, route to Finance human approver. No provisioning while approval is pending. Approval expires seven calendar days after decision if unused; a rejection/expiry creates no charge.
5. Once customer authorization and required Finance approval exist, activate exactly the approved seat count. Prorate first-year incremental invoice by remaining service days divided by actual contract-term days; additions co-terminate with the existing contract. Update ARR ledger only on effective activation, with immutable audit events.
6. Notify CSM and Finance, and reconcile entitlements, invoice, and amended committed seats. Avoid retroactive changes to the frozen 2026-10-28 baseline.

## 4. Boundary examples
- Starter +10 seats: $500/month; no threshold approval. Starter +11: $550/month; approval required.
- Growth +20 seats: $500/month; no threshold approval. Growth +21: $525/month; approval required.
- Enterprise +42 seats: $490/month; no threshold approval. Enterprise +43 seats: $501.666…/month; approval required.
- Enterprise `{ecid}` (`{econ['contract_id']}`), {enterprise['total_seats']} current seats: proposed +50 = $583.333…/month. Request is **pending clarification and human approval**, not activated. A split +30 then +20 within 30 days still totals +50 and requires approval. Neither request changes the dataset's {money(econ['annual_recurring_revenue'])} ARR.

## 5. Evidence and disputes
A vague request such as "reserve 50 seats for a pilot" is not an activation instruction. Clarify whether reservation, purchase, or temporary access is intended, obtain explicit written authorization, and log an approval decision with approver employee ID, timestamp, amount and amendment reference. The synthetic chat `internal_ops/Slack_Transcript_CSM_Sync.md` intentionally contains conflicting interpretations; this SOP governs.
"""
    # The ticket is open in source CSV; the thread therefore ends without claiming database resolution.
    documents['correspondence/Email_Thread_P1_Outage.md']=f"""
# Email Thread — P1 Incident Follow-up (Synthetic)

{common}

**Linked customer:** `{ecid}` ({enterprise['company_name']}); **contract:** `{econ['contract_id']}`; **ticket:** `{eticket['ticket_id']}`; **scenario proposed severity:** P1 (unrecorded escalation); **dataset severity:** `{eticket['severity']}`; **dataset status:** `{eticket['status']}`; **created:** `{eticket['created_at']}`; **resolved_at:** NULL. This training thread simulates a proposed P1 escalation of a source ticket whose recorded severity is `{eticket['severity']}`; it is not an incident-event ledger or an actual severity change.

---
**From:** it-operations@{enterprise['domain']}  
**To:** {assignee['email']}  
**Date:** 2026-10-28 09:02 UTC  
**Subject:** [P1] API inventory refresh failing — {eticket['ticket_id']}

Our inventory sync is failing for multiple offices. Some users can still open the portal, so I cannot tell whether this is a complete outage. We have a compliance export due today. Please confirm whether SecurityShield is affected too.

---
**From:** {assignee['email']}  
**To:** it-operations@{enterprise['domain']}  
**Cc:** {csm['email']}  
**Date:** 2026-10-28 09:10 UTC  
**Subject:** Re: [P1] API inventory refresh failing — {eticket['ticket_id']}

Human acknowledgment: we are treating this as P1 pending impact verification. Engineering/on-call SRE has been paged. The Enterprise P1 human response objective is 15 clock minutes and resolution objective is four clock hours from ticket creation, with continuous 24×7 coverage. Please confirm affected regions and whether the compliance export depends on the same endpoint. This message is an illustrative acknowledgment; it is **not present as an event in the source CSV**.

---
**From:** {csm['email']}  
**To:** it-operations@{enterprise['domain']}  
**Date:** 2026-10-28 09:48 UTC  
**Subject:** Re: [P1] API inventory refresh failing — {eticket['ticket_id']}

We are tracking mitigation and will update at least every 60 minutes while P1 coverage is active. Please do not treat a successful manual export as confirmation of full service restoration. A proposed +50-seat pilot is being handled separately under `SOP_02_Seat_Provisioning.md`; it cannot be silently activated during incident response.

---
**From:** it-operations@{enterprise['domain']}  
**To:** {assignee['email']}  
**Date:** 2026-10-28 10:16 UTC  
**Subject:** Re: [P1] API inventory refresh failing — {eticket['ticket_id']}

A manual export worked for one team, but the scheduled sync still appears unreliable. Is that enough to close the ticket? We also need an explanation for the failed jobs.

---
**From:** {assignee['email']}  
**To:** it-operations@{enterprise['domain']}  
**Date:** 2026-10-28 10:22 UTC  
**Subject:** Re: [P1] API inventory refresh failing — {eticket['ticket_id']}

Not yet. We need verified restoration or an explicitly accepted workaround, recorded against the ticket. The incident remains open in the dataset. Root-cause analysis follows verification; no resolution timestamp has been asserted. Next customer update is due no later than the applicable 60-eligible-minute interval. Escalations follow `SOP_01_Ticket_Escalation.md`.
"""
    cure=AS_OF+timedelta(days=15)
    documents['correspondence/Finance_Collection_Notice.md']=f"""
# Finance — Collection Notice and Conditional Suspension Warning (Synthetic)

{common}

**Status:** Unsent example; no real notice or suspension has occurred.  
**To:** accounts-payable@{delinquent['domain']}  
**From:** {finance['email']}  
**Date:** {AS_OF.isoformat()}  
**Subject:** Payment overdue — {dinv['invoice_id']} / {dcon['contract_id']}

Dear Accounts Payable,

Our billing snapshot shows an outstanding invoice for **{delinquent['company_name']}** (`{dcid}`), contract `{dcon['contract_id']}`:

| Field | Ledger value |
|---|---|
| Invoice | `{dinv['invoice_id']}` |
| Issued | {dinv['issue_date']} |
| Due | {dinv['due_date']} |
| Outstanding amount | **{money(dinv['amount'])}** |
| Invoice status | `{dinv['status']}` |
| Account status | `{delinquent['status']}` |
| Contract status | `{dcon['status']}` (still active) |

Please remit payment or provide written evidence of payment and remittance details. If you dispute this invoice in good faith, notify Finance with the disputed line items; undisputed charges remain due. Under the model MSA, the cure period is **15 calendar days after receipt of a valid notice**. If receipt occurs on {AS_OF.isoformat()}, the illustrative cure deadline is **{cure.isoformat()}**; actual deadline must be computed from documented receipt. Continued nonpayment may lead to discretionary suspension **only after** review, notice, and any applicable legal restrictions. No suspension is automatic or recorded here.

A colleague has asked whether the customer's pending seat expansion can proceed before the debt is resolved. That request remains separate; Finance must evaluate credit risk and the seat-approval threshold before any provisioning. We have not applied an unapproved credit or modified the source invoice.

Regards,  
{finance['first_name']} {finance['last_name']}  
Billing Analyst, NexusFlow Systems (`{finance['employee_id']}`)

**Internal routing:** CSM {person(dcsm['employee_id'])}; escalation to {person(director['employee_id'])}. **Cross-reference:** `legal/MSA_Template_v1.0.md`, `internal_ops/Slack_Transcript_CSM_Sync.md`.
"""
    documents['internal_ops/Slack_Transcript_CSM_Sync.md']=f"""
# Internal Chat — CSM / Finance Risk Sync (Synthetic Transcript)

{common}

**Channel:** `#csm-finance-risk` (fictional). **Date:** 2026-10-28. **Purpose:** Training-only contradictory evidence and approval routing. No `APR-XXXX` row exists in the approved CSV dataset.

**10:30 UTC — {csm['first_name']} {csm['last_name']} (`{csm['employee_id']}`, CSM):** Enterprise customer `{ecid}` / `{econ['contract_id']}` asked for "about 50" extra seats during their P1 issue `{eticket['ticket_id']}`. I interpreted it as a reservation, not an order. Current commitment is {enterprise['total_seats']} seats and ARR is {money(econ['annual_recurring_revenue'])}. They also mentioned a temporary pilot.

**10:33 UTC — {finance['first_name']} {finance['last_name']} (`{finance['employee_id']}`, Billing Analyst):** If +50 is a paid addition, that's 50 × $140 ÷ 12 = $583.333… monthly equivalent. It exceeds $500, so a human Finance approval is mandatory. A "reservation" alone is not a provisioning authorization. Do we have an exact count, signed amendment or effective date?

**10:36 UTC — {csm['first_name']} {csm['last_name']}:** Not yet. Customer suggested "30 now and 20 next week" to avoid paperwork. Can we treat those as independent requests?

**10:39 UTC — {finance['first_name']} {finance['last_name']}:** No. Rolling 30-calendar-day aggregation counts pending and approved additions; +30 and +20 still equal +50 and require approval. Nothing has been activated. Please refer to `sops/SOP_02_Seat_Provisioning.md`.

**10:43 UTC — {dcsm['first_name']} {dcsm['last_name']} (`{dcsm['employee_id']}`, CSM):** Separate risk: `{dcid}` has invoice `{dinv['invoice_id']}` for {money(dinv['amount'])} overdue since {dinv['due_date']}, but their contract `{dcon['contract_id']}` is still ACTIVE. Customer says their AP team "already sent a transfer." I cannot verify settlement in the snapshot. Renewal discussion is at risk; should I mark churn now?

**10:46 UTC — {finance['first_name']} {finance['last_name']}:** Do **not** convert DELINQUENT to CHURNED on an unverified claim. We have six delinquent accounts (5% of 120), not six confirmed cancellations. The collection letter is a simulation; a real notice needs recorded delivery and cure timing. Reconcile bank receipt before changing invoice status.

**10:51 UTC — {vp_cs['first_name']} {vp_cs['last_name']} (`{vp_cs['employee_id']}`, VP Customer Success):** Record both as risks. The 30-day nonrenewal deadline is a contract rule, not a grace period for overdue invoices. Do not promise suspension or a credit. The P1 ticket remains open until verified restoration or accepted workaround; a customer's "looks better" email is not a closure event.

**10:55 UTC — {director['first_name']} {director['last_name']} (`{director['employee_id']}`, Finance Director):** Decision: HOLD the Enterprise +50 request pending exact written authorization, 30-day aggregate review and Finance approval. HOLD any account-status change for `{dcid}` pending payment reconciliation. Neither discussion changes the $12.4M ARR snapshot. Any future approval identifier (`APR-XXXX`) belongs in a new approval-event ledger, not an invented current database row.

**Retrieval warning:** This transcript contains *requests, interpretations and proposed actions*, not signed amendments, payment confirmation, actual approval, or verified incident resolution. Source-of-truth records remain the approved CSV tables and the governing SOPs.
"""
    results=[write(args.output_dir, name, body) for name,body in sorted(documents.items())]
    manifest={'generator':'generate_nexusflow_docs.py','snapshot':AS_OF.isoformat(),'source_directory':str(args.data_dir),'enterprise_customer_id':ecid,'enterprise_contract_id':econ['contract_id'],'incident_ticket_id':eticket['ticket_id'],'delinquent_customer_id':dcid,'delinquent_invoice_id':dinv['invoice_id'],'fictional_scenarios':True,'files':results}
    (args.output_dir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(args.output_dir),'documents':len(results),'references':{k:manifest[k] for k in ('enterprise_customer_id','incident_ticket_id','delinquent_customer_id')}},indent=2))


if __name__=='__main__':
    main()
