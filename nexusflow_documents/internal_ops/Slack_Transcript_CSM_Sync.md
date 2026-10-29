# Internal Chat — CSM / Finance Risk Sync (Synthetic Transcript)

**Document classification:** Fictional training artifact. **Dataset snapshot:** 2026-10-28. **ARR baseline:** $12,400,000.00; 120 accounts; 85 active employees.

**Channel:** `#csm-finance-risk` (fictional). **Date:** 2026-10-28. **Purpose:** Training-only contradictory evidence and approval routing. No `APR-XXXX` row exists in the approved CSV dataset.

**10:30 UTC — Avery Diaz (`EMP-0058`, CSM):** Enterprise customer `CUST-0040` / `CTR-0040` asked for "about 50" extra seats during their P1 issue `TK-0139`. I interpreted it as a reservation, not an order. Current commitment is 1500 seats and ARR is $210,000.00. They also mentioned a temporary pilot.

**10:33 UTC — Rowan Patel (`EMP-0084`, Billing Analyst):** If +50 is a paid addition, that's 50 × $140 ÷ 12 = $583.333… monthly equivalent. It exceeds $500, so a human Finance approval is mandatory. A "reservation" alone is not a provisioning authorization. Do we have an exact count, signed amendment or effective date?

**10:36 UTC — Avery Diaz:** Not yet. Customer suggested "30 now and 20 next week" to avoid paperwork. Can we treat those as independent requests?

**10:39 UTC — Rowan Patel:** No. Rolling 30-calendar-day aggregation counts pending and approved additions; +30 and +20 still equal +50 and require approval. Nothing has been activated. Please refer to `sops/SOP_02_Seat_Provisioning.md`.

**10:43 UTC — Drew Chen (`EMP-0062`, CSM):** Separate risk: `CUST-0011` has invoice `INV-2026-0011` for $150,000.00 overdue since 2026-09-16, but their contract `CTR-0011` is still ACTIVE. Customer says their AP team "already sent a transfer." I cannot verify settlement in the snapshot. Renewal discussion is at risk; should I mark churn now?

**10:46 UTC — Rowan Patel:** Do **not** convert DELINQUENT to CHURNED on an unverified claim. We have six delinquent accounts (5% of 120), not six confirmed cancellations. The collection letter is a simulation; a real notice needs recorded delivery and cure timing. Reconcile bank receipt before changing invoice status.

**10:51 UTC — Casey Rivera (`EMP-0057`, VP Customer Success):** Record both as risks. The 30-day nonrenewal deadline is a contract rule, not a grace period for overdue invoices. Do not promise suspension or a credit. The P1 ticket remains open until verified restoration or accepted workaround; a customer's "looks better" email is not a closure event.

**10:55 UTC — Parker Kim (`EMP-0082`, Finance Director):** Decision: HOLD the Enterprise +50 request pending exact written authorization, 30-day aggregate review and Finance approval. HOLD any account-status change for `CUST-0011` pending payment reconciliation. Neither discussion changes the $12.4M ARR snapshot. Any future approval identifier (`APR-XXXX`) belongs in a new approval-event ledger, not an invented current database row.

**Retrieval warning:** This transcript contains *requests, interpretations and proposed actions*, not signed amendments, payment confirmation, actual approval, or verified incident resolution. Source-of-truth records remain the approved CSV tables and the governing SOPs.
