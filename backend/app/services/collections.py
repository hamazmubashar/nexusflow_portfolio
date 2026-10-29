"""Delinquent-invoice account summary and collection-notice drafting (MSA §6).

Drafts are deterministic templates filled from ledger facts only, so every amount
and date in a notice traces to a database row. They are never sent: MSA §6 needs a
documented delivery before the 15-calendar-day cure period starts, and the internal
CSM/Finance sync forbids changing account status on unverified payment claims.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

CURE_PERIOD_DAYS = 15
SETTLED_STATUSES = ("SETTLED",)

GUARDRAILS = [
    "Draft only: a real notice requires recorded delivery; the 15-calendar-day cure period runs from documented receipt (MSA §6).",
    "Do not change the customer status (e.g. DELINQUENT -> CHURNED) or invoice status on an unverified payment claim; reconcile the bank receipt first.",
    "No suspension, credit or nonstandard payment term without Finance Director approval (MSA §6).",
    "Seat expansions for this customer stay on hold until Finance evaluates credit risk (SOP-02).",
]


class CollectionsError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


OVERDUE_SQL = """
SELECT i.invoice_id, i.customer_id, c.company_name, i.amount, i.due_date, i.status AS invoice_status,
       c.status AS customer_status,
       i.amount - coalesce((SELECT sum(p.amount) FROM payments p
                            WHERE p.invoice_id = i.invoice_id AND p.status = ANY(%(settled)s)), 0) AS outstanding
FROM invoices i JOIN customers c USING (customer_id)
WHERE i.status NOT IN ('PAID', 'VOID', 'DRAFT') AND i.due_date < %(as_of)s {extra}
"""


def _overdue_rows(cur, as_of: date, extra: str = "", params: dict | None = None) -> list[dict]:
    cur.execute(OVERDUE_SQL.format(extra=extra) + " ORDER BY i.due_date, i.invoice_id",
                {"as_of": as_of, "settled": list(SETTLED_STATUSES), **(params or {})})
    rows = []
    for r in cur.fetchall():
        if r["outstanding"] > 0:
            rows.append({**r, "days_overdue": (as_of - r["due_date"]).days})
    return rows


def list_overdue(cur, as_of: date) -> list[dict]:
    return _overdue_rows(cur, as_of)


def _contact(cur, employee_id: str | None = None, role: str | None = None) -> dict:
    cur.execute(
        "SELECT employee_id, first_name || ' ' || last_name AS name, role, email FROM employees "
        "WHERE (%s::text IS NULL OR employee_id = %s) AND (%s::text IS NULL OR role = %s) "
        "AND status = 'ACTIVE' ORDER BY employee_id LIMIT 1",
        (employee_id, employee_id, role, role),
    )
    return cur.fetchone()


def summarize_delinquency(cur, invoice_id: str, customer_id: str | None, as_of: date, draft: bool) -> dict:
    cur.execute("SELECT customer_id FROM invoices WHERE invoice_id = %s", (invoice_id,))
    inv = cur.fetchone()
    if inv is None:
        raise CollectionsError(404, "INVOICE_NOT_FOUND", f"{invoice_id} does not exist")
    if customer_id and inv["customer_id"] != customer_id:
        raise CollectionsError(422, "INVOICE_CUSTOMER_MISMATCH", f"{invoice_id} belongs to {inv['customer_id']}")

    overdue = _overdue_rows(cur, as_of, "AND i.customer_id = %(cid)s", {"cid": inv["customer_id"]})
    target = next((r for r in overdue if r["invoice_id"] == invoice_id), None)
    if target is None:
        raise CollectionsError(409, "INVOICE_NOT_DELINQUENT",
                               f"{invoice_id} is not past due with an outstanding balance as of {as_of}")

    cur.execute(
        """SELECT c.customer_id, c.company_name, c.domain, c.tier, c.status AS customer_status, c.csm_employee_id,
                  k.contract_id, k.plan_tier, k.status AS contract_status, k.annual_recurring_revenue,
                  k.end_date AS contract_end_date, k.auto_renew
           FROM customers c
           JOIN invoices i ON i.customer_id = c.customer_id AND i.invoice_id = %s
           JOIN contracts k ON k.contract_id = i.contract_id""",
        (invoice_id,),
    )
    acct = cur.fetchone()
    cur.execute(
        "SELECT severity, count(*) AS n FROM support_tickets WHERE customer_id = %s AND resolved_at IS NULL "
        "GROUP BY severity ORDER BY severity",
        (acct["customer_id"],),
    )
    open_tickets = {r["severity"]: r["n"] for r in cur.fetchall()}
    csm = _contact(cur, employee_id=acct.pop("csm_employee_id"))
    finance = _contact(cur, role="Finance Director")
    billing = _contact(cur, role="Billing Analyst")
    total_outstanding = sum((r["outstanding"] for r in overdue), Decimal("0"))

    account = {**acct, "csm": csm, "total_outstanding": total_outstanding,
               "overdue_invoices": overdue, "open_tickets_by_severity": open_tickets}

    actions = [
        f"Billing to reconcile bank receipts for {invoice_id} before any status change",
        f"CSM {csm['name']} ({csm['employee_id']}) to confirm accounts-payable contact and any dispute",
        f"Finance Director {finance['name']} ({finance['employee_id']}) to review and approve the notice before delivery",
    ]
    if open_tickets.get("P1") or open_tickets.get("P2"):
        actions.append("Open P1/P2 tickets exist: coordinate with Support so collections does not disrupt incident handling")
    if acct["contract_status"] == "ACTIVE":
        actions.append(f"{acct['contract_id']} remains ACTIVE: delinquency does not change ARR or contract status")

    notice = None
    if draft:
        cure = as_of + timedelta(days=CURE_PERIOD_DAYS)
        others = [r for r in overdue if r["invoice_id"] != invoice_id]
        other_line = (f"\nThe account also shows {len(others)} other overdue invoice(s) totalling "
                      f"${sum((r['outstanding'] for r in others), Decimal('0')):,.2f}.\n" if others else "")
        body = (
            f"Dear Accounts Payable,\n\n"
            f"Our billing records show an outstanding balance for {acct['company_name']} ({acct['customer_id']}), "
            f"contract {acct['contract_id']}:\n\n"
            f"  Invoice: {invoice_id}\n"
            f"  Due date: {target['due_date']:%Y-%m-%d} ({target['days_overdue']} days overdue as of {as_of:%Y-%m-%d})\n"
            f"  Outstanding: ${target['outstanding']:,.2f}\n{other_line}\n"
            f"Please remit payment, or send written evidence of payment with remittance details. If you dispute this "
            f"invoice in good faith, tell Finance which line items are disputed; undisputed charges remain due.\n\n"
            f"Under the Master Services Agreement, a {CURE_PERIOD_DAYS}-calendar-day cure period begins on receipt of "
            f"this notice. If received on {as_of:%Y-%m-%d}, the cure deadline would be {cure:%Y-%m-%d}. Any suspension "
            f"is discretionary and only follows review and notice; none is scheduled.\n\n"
            f"Regards,\n{billing['name']}\n{billing['role']}, NexusFlow Systems"
        )
        notice = {
            "to": f"accounts-payable@{acct['domain']}",
            "from_": billing["email"],
            "cc": [csm["email"], finance["email"]],
            "subject": f"Payment overdue — {invoice_id} / {acct['contract_id']}",
            "body": body,
            "illustrative_cure_deadline": cure,
            "requires_review_by": finance,
        }

    return {"invoice": target, "account": account, "notice": notice,
            "recommended_actions": actions, "guardrails": GUARDRAILS}
