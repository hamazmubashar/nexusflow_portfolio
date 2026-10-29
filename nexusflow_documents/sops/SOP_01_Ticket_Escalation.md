# SOP-01 — Ticket Triage, Escalation, and SLA Clocks

**Document classification:** Fictional training artifact. **Dataset snapshot:** 2026-10-28. **ARR baseline:** $12,400,000.00; 120 accounts; 85 active employees.

**Owner:** Sam Wright (EMP-0059; Support Manager; sam.wright.0059@nexusflow.example). **Executive escalation:** Alex Stone (EMP-0001; VP Engineering; alex.stone.0001@nexusflow.example). **Effective:** 2026-10-28. **System of record:** `support_tickets`.

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
Ticket `TK-0139` belongs to `CUST-0040` (Enterprise) and is assigned to Alex Stone (EMP-0069; Support Engineer; alex.stone.0069@nexusflow.example). The source row records severity `P3` (the hypothetical P1 reclassification is not in the dataset), status `IN_PROGRESS`, created `2026-10-26T09:00:00Z`, resolved `NULL`. The email thread demonstrates a **hypothetical P1 severity escalation**; it must not be interpreted as a resolved database ticket. Customer says "the portal is slow" while monitoring suggests a broad API outage: retain P1 until the Support Manager authorizes a downgrade. Do not assume an email saying "looks better" proves verified restoration.

## 5. Audit and retrieval controls
Authoritative facts are CSV status and timestamps; correspondence is scenario narrative. Never infer `resolved_at` from email timestamps. Escalation, customer updates, incident milestones and SLA pauses require an event table not present in the nine-table baseline schema. See `correspondence/Email_Thread_P1_Outage.md` and `legal/SOW_Enterprise_Client_Sample.md`.
