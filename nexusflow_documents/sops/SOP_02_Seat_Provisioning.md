# SOP-02 — Managed Seat Expansion and Human Approval

**Document classification:** Fictional training artifact. **Dataset snapshot:** 2026-10-28. **ARR baseline:** $12,400,000.00; 120 accounts; 85 active employees.

**Owner:** Parker Kim (EMP-0082; Finance Director; parker.kim.0082@nexusflow.example). **Commercial counterpart:** Customer Success and Sales. **Effective:** 2026-10-28.

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
- Enterprise `CUST-0040` (`CTR-0040`), 1500 current seats: proposed +50 = $583.333…/month. Request is **pending clarification and human approval**, not activated. A split +30 then +20 within 30 days still totals +50 and requires approval. Neither request changes the dataset's $210,000.00 ARR.

## 5. Evidence and disputes
A vague request such as "reserve 50 seats for a pilot" is not an activation instruction. Clarify whether reservation, purchase, or temporary access is intended, obtain explicit written authorization, and log an approval decision with approver employee ID, timestamp, amount and amendment reference. The synthetic chat `internal_ops/Slack_Transcript_CSM_Sync.md` intentionally contains conflicting interpretations; this SOP governs.
