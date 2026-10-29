# Statement of Work — Nexus Client 0040 LLC

**Document classification:** Fictional training artifact. **Dataset snapshot:** 2026-10-28. **ARR baseline:** $12,400,000.00; 120 accounts; 85 active employees.

**Status:** Completed fictional SOW example; NOT an executed agreement. **Customer:** Nexus Client 0040 LLC (`CUST-0040`), `client0040.example`. **Contract:** `CTR-0040`. **Plan:** ENTERPRISE. **Term:** 2026-04-08 through 2027-04-08 (end exclusive). **Customer owner:** IT Operations Lead (role only; synthetic external correspondent). **NexusFlow CSM:** Avery Diaz (EMP-0058; Customer Success Manager - Lead; avery.diaz.0058@nexusflow.example).

## 1. Commercial schedule
- Committed managed seats: **1,500**.
- Enterprise annual unit price: **$140.00 per seat per year**, equivalent to $11.666666… per seat per month (display only).
- Annual recurring subscription amount: **$210,000.00** = 1500 × $140.00.
- Billing: annual prepaid; initial invoice on term start; payment due 30 days after issue. No professional services or implementation fee included in ARR.
- Auto-renewal: `true`; nonrenewal notice required 30 days before term end, per MSA.

## 2. Included scope and acceptance
AssetTracker tenant setup, discovery connectors and asset inventory; LicenseOptimizer entitlement reconciliation and license utilization reports; SecurityShield security posture findings and policy views. NexusFlow will configure SSO, role-based admin access and audit logging. Acceptance requires successful administrator login, sample asset sync, sample entitlement reconciliation and verified role restrictions; CSM records acceptance evidence. Customer supplies credentials and lawful data. Unavailable customer integrations are tracked as dependencies, not silently treated as delivered.

## 3. Service commitments
Enterprise uptime **99.95% monthly**; P1 initial human response **15 clock minutes**, resolution **4 clock hours**, both 24×7. P2 initial response **1 clock hour**, resolution **12 clock hours**. P3 initial response **8 business hours**, resolution **3 business days**; P4 initial response **1 business day**, resolution **10 business days**. Business hours are Mon–Fri 09:00–17:00 America/Chicago, excluding observed US holidays. Resolution requires verified restoration or an accepted workaround; RCA may follow. See `sops/SOP_01_Ticket_Escalation.md`.

## 4. Change control and business friction example
The customer asked in a synthetic follow-up for "about 50 additional seats, perhaps temporarily." At $140 per seat per year, **50 seats = $583.333… monthly equivalent**, above the $500 approval threshold; **no seats are provisioned** until a human Finance approval and customer authorization are recorded. An exact seat count, effective date, and whether a signed amendment is required remain unresolved. Approval expires seven days after grant if not activated. Splitting into smaller requests within a rolling 30 days does not bypass approval. See `sops/SOP_02_Seat_Provisioning.md`.

## 5. Roles and communications
CSM coordinates onboarding and renewals. Support owns ticket triage; on-call SRE owns mitigation; Finance approves nonstandard billing and high-value seat expansions. All P1 incident communications must cite the canonical ticket, e.g. `TK-0139`. The example thread in `correspondence/Email_Thread_P1_Outage.md` is a synthetic scenario linked to this contract, not proof of actual downtime.

## 6. Exclusions and assumptions
No bespoke development, migration of historical licenses, customer-managed network repair, tax calculation, or guaranteed security outcome. Any material change to scope or price requires a signed amendment. This completed example contains no actual signatures.
