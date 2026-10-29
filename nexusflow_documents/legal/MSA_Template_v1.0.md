# NexusFlow Systems — Master Services Agreement (MSA) v1.0

**Document classification:** Fictional training artifact. **Dataset snapshot:** 2026-10-28. **ARR baseline:** $12,400,000.00; 120 accounts; 85 active employees.

**Status:** Model contract template, not executed legal advice. **Owner:** Finance Director (EMP-0082) with external counsel review. **Effective date for template:** 2026-10-28. **Governing-law proposal:** Delaware, USA; parties may negotiate by signed amendment.

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
