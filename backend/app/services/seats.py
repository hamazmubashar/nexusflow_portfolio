"""SOP-02 seat-expansion evaluation: $500/month threshold over a rolling 30-day window.

Nothing here provisions seats. Every request is recorded in seat_requests in one of
the PENDING_* states; activation needs customer authorization and, above the
threshold, a human Finance approval logged elsewhere.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

ANNUAL_UNIT_PRICE = {"STARTER": Decimal("600"), "GROWTH": Decimal("300"), "ENTERPRISE": Decimal("140")}
SEAT_RANGE = {"STARTER": (50, 249), "GROWTH": (250, 999), "ENTERPRISE": (1000, 10000)}
THRESHOLD_MONTHLY = Decimal("500.00")
WINDOW = timedelta(days=30)
COUNTED_STATUSES = ("PENDING_PLAN_AMENDMENT", "PENDING_CLARIFICATION", "PENDING_FINANCE_APPROVAL",
                    "PENDING_CUSTOMER_AUTHORIZATION", "APPROVED", "ACTIVATED")


class SeatRequestError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def payload_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def monthly_equivalent(seats: int, annual_price: Decimal) -> Decimal:
    return Decimal(seats) * annual_price / Decimal(12)


def evaluate_seat_request(cur, data) -> dict:
    """Validate, aggregate and record one request. Caller owns the transaction."""
    cur.execute(
        """SELECT c.customer_id, c.company_name, c.status AS customer_status, c.total_seats,
                  k.contract_id, k.customer_id AS contract_customer, k.plan_tier, k.status AS contract_status
           FROM contracts k JOIN customers c ON c.customer_id = %s
           WHERE k.contract_id = %s""",
        (data.customer_id, data.contract_id),
    )
    row = cur.fetchone()
    if row is None:
        raise SeatRequestError(404, "NOT_FOUND", f"{data.contract_id} or {data.customer_id} does not exist")
    if row["contract_customer"] != data.customer_id:
        raise SeatRequestError(422, "CONTRACT_CUSTOMER_MISMATCH",
                               f"{data.contract_id} belongs to {row['contract_customer']}, not {data.customer_id}")
    if row["contract_status"] != "ACTIVE":
        raise SeatRequestError(409, "CONTRACT_NOT_ACTIVE", f"{data.contract_id} is {row['contract_status']}")
    if row["customer_status"] != "ACTIVE":
        raise SeatRequestError(409, "CUSTOMER_NOT_ACTIVE",
                               f"{data.customer_id} is {row['customer_status']}; Finance must clear the account first")

    # Serialize evaluations per customer so concurrent requests can't split around the threshold
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (data.customer_id,))

    phash = payload_hash(data.model_dump(mode="json"))
    cur.execute("SELECT * FROM seat_requests WHERE request_id = %s", (data.request_id,))
    existing = cur.fetchone()
    if existing:
        if existing["payload_hash"] != phash:
            raise SeatRequestError(409, "REQUEST_ID_REUSED",
                                   f"{data.request_id} was already submitted with different details")
        return {"duplicate": True, **existing["calculation"]["decision"]}

    plan = row["plan_tier"]
    price = ANNUAL_UNIT_PRICE[plan]
    window_start = data.requested_at - WINDOW
    cur.execute(
        """SELECT request_id, added_seats, status FROM seat_requests
           WHERE customer_id = %s AND requested_at > %s AND requested_at <= %s AND status = ANY(%s)
           ORDER BY requested_at""",
        (data.customer_id, window_start, data.requested_at, list(COUNTED_STATUSES)),
    )
    prior = cur.fetchall()
    aggregate_seats = data.added_seats + sum(p["added_seats"] for p in prior)
    aggregate_monthly = monthly_equivalent(aggregate_seats, price)
    lo, hi = SEAT_RANGE[plan]
    # ACTIVATED additions are already in customers.total_seats: count them once (SOP-02 §2)
    projected = (row["total_seats"] + data.added_seats
                 + sum(p["added_seats"] for p in prior if p["status"] != "ACTIVATED"))

    requires_finance = aggregate_monthly > THRESHOLD_MONTHLY       # strictly greater (SOP-02 §1)
    requires_amendment = projected > hi
    requires_clarification = data.request_type != "PURCHASE"
    if requires_amendment:
        status = "PENDING_PLAN_AMENDMENT"
    elif requires_clarification:
        status = "PENDING_CLARIFICATION"
    elif requires_finance:
        status = "PENDING_FINANCE_APPROVAL"
    else:
        status = "PENDING_CUSTOMER_AUTHORIZATION"

    notes = []
    if prior:
        notes.append(f"Aggregated with {len(prior)} prior request(s) in the rolling 30-day window: "
                     + ", ".join(p["request_id"] for p in prior))
    if requires_finance:
        notes.append(f"Aggregate ${aggregate_monthly.quantize(Decimal('0.01'))}/month exceeds the "
                     f"${THRESHOLD_MONTHLY}/month threshold: human Finance approval is mandatory")
    elif aggregate_monthly == THRESHOLD_MONTHLY:
        notes.append("Exactly $500.00/month does not trigger Finance approval; customer authorization is still required")
    if requires_amendment:
        notes.append(f"Projected {projected} seats exceeds the {plan} range {lo}-{hi}: signed plan amendment required")
    if requires_clarification:
        notes.append(f"Request type {data.request_type} is not an activation instruction: confirm purchase vs "
                     "reservation and obtain explicit written authorization (SOP-02 §5)")
    notes.append("No seats are provisioned until customer authorization and any required approval are recorded")

    calculation = {
        "annual_unit_price": price,
        "monthly_equivalent": monthly_equivalent(data.added_seats, price),
        "window_start": window_start,
        "window_end": data.requested_at,
        "prior_requests_in_window": [p["request_id"] for p in prior],
        "aggregate_seats_30d": aggregate_seats,
        "aggregate_monthly_30d": aggregate_monthly,
        "threshold_monthly": THRESHOLD_MONTHLY,
        "committed_seats": row["total_seats"],
        "projected_seats": projected,
        "tier_seat_range": (lo, hi),
    }
    decision = {
        "request_id": data.request_id, "customer_id": data.customer_id, "contract_id": data.contract_id,
        "company_name": row["company_name"], "plan_tier": plan, "added_seats": data.added_seats,
        "status": status, "requires_finance_approval": requires_finance,
        "requires_plan_amendment": requires_amendment, "requires_clarification": requires_clarification,
        "requires_customer_authorization": True, "calculation": calculation, "notes": notes,
    }

    cur.execute(
        """INSERT INTO seat_requests (request_id, customer_id, contract_id, added_seats, request_type, plan_tier,
               annual_unit_price, monthly_equivalent, aggregate_seats_30d, aggregate_monthly_30d,
               requires_finance_approval, requires_plan_amendment, status, requester_email, requested_at,
               payload_hash, calculation)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (data.request_id, data.customer_id, data.contract_id, data.added_seats, data.request_type, plan,
         price, calculation["monthly_equivalent"], aggregate_seats, aggregate_monthly,
         requires_finance, requires_amendment, status, data.requester_email, data.requested_at, phash,
         json.dumps({"decision": decision}, default=str)),
    )
    return {"duplicate": False, **decision}
