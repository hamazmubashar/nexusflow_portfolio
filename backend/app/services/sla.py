"""SOP-01 SLA clocks and escalation thresholds.

Clocks (SOP-01 §2):
  * 24x7      - Enterprise P1/P2
  * coverage  - Growth P1/P2: Monday 00:00 through Friday 23:59:59 America/Chicago, holidays included
  * business  - everything else: Mon-Fri 09:00-17:00 America/Chicago, excluding observed US holidays;
                one business day = 8 business hours
Timestamps are stored in UTC; eligibility is evaluated in America/Chicago (DST-aware via zoneinfo).
P1/P2 clocks never pause; WAITING_CUSTOMER pauses for P3/P4 need an event ledger (not in the
snapshot), so they are not applied here.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
Clock = Literal["24x7", "coverage", "business"]

# Observed US federal holidays (Saturday -> Friday, Sunday -> Monday)
US_HOLIDAYS = {
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 5, 26), date(2025, 6, 19),
    date(2025, 7, 4), date(2025, 9, 1), date(2025, 10, 13), date(2025, 11, 11), date(2025, 11, 27),
    date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11), date(2026, 11, 26),
    date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 5, 31), date(2027, 6, 18),
    date(2027, 7, 5), date(2027, 9, 6), date(2027, 10, 11), date(2027, 11, 11), date(2027, 11, 25),
    date(2027, 12, 24),
}

H = 60          # minutes per clock/coverage/business hour
BD = 8 * H      # one business day

# (severity, plan) -> (clock-independent minutes) per SOP-01 §1 (resolution) and §2 (response)
RESOLUTION_MINUTES = {
    "P1": {"STARTER": 2 * BD, "GROWTH": 8 * H, "ENTERPRISE": 4 * H},
    "P2": {"STARTER": 4 * BD, "GROWTH": 48 * H, "ENTERPRISE": 12 * H},
    "P3": {"STARTER": 10 * BD, "GROWTH": 5 * BD, "ENTERPRISE": 3 * BD},
    "P4": {"STARTER": 20 * BD, "GROWTH": 15 * BD, "ENTERPRISE": 10 * BD},
}
RESPONSE_MINUTES = {
    "P1": {"STARTER": 4 * H, "GROWTH": 1 * H, "ENTERPRISE": 15},
    "P2": {"STARTER": 8 * H, "GROWTH": 4 * H, "ENTERPRISE": 1 * H},
    "P3": {"STARTER": 1 * BD, "GROWTH": 1 * BD, "ENTERPRISE": 8 * H},
    "P4": {"STARTER": 2 * BD, "GROWTH": 2 * BD, "ENTERPRISE": 1 * BD},
}
SEVERITY_ORDER = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


def clock_for(severity: str, plan: str) -> Clock:
    if severity in ("P1", "P2"):
        if plan == "ENTERPRISE":
            return "24x7"
        if plan == "GROWTH":
            return "coverage"
    return "business"


def _windows(local_day: date, clock: Clock) -> list[tuple[datetime, datetime]]:
    """Eligible [start, end) windows on one America/Chicago calendar day, returned in UTC."""
    if clock == "24x7":
        start, end = time(0), None
    elif clock == "coverage":
        if local_day.weekday() >= 5:
            return []
        start, end = time(0), None
    else:
        if local_day.weekday() >= 5 or local_day in US_HOLIDAYS:
            return []
        start, end = time(9), time(17)
    s = datetime.combine(local_day, start, CHICAGO)
    e = datetime.combine(local_day, end, CHICAGO) if end else datetime.combine(local_day + timedelta(days=1), time(0), CHICAGO)
    return [(s.astimezone(timezone.utc), e.astimezone(timezone.utc))]


def eligible_minutes(start: datetime, end: datetime, clock: Clock) -> int:
    if end <= start:
        return 0
    if clock == "24x7":
        return int((end - start).total_seconds() // 60)
    total = 0.0
    day = start.astimezone(CHICAGO).date()
    last = end.astimezone(CHICAGO).date()
    while day <= last:
        for ws, we in _windows(day, clock):
            lo, hi = max(ws, start), min(we, end)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return int(total // 60)


def add_eligible_minutes(start: datetime, minutes: int, clock: Clock) -> datetime:
    """Earliest instant at which `minutes` of eligible time have elapsed since `start`."""
    if clock == "24x7":
        return start + timedelta(minutes=minutes)
    remaining = timedelta(minutes=minutes)
    day = start.astimezone(CHICAGO).date()
    for _ in range(3660):  # 10 years of days: guards against an impossible schedule
        for ws, we in _windows(day, clock):
            lo = max(ws, start)
            if we <= lo:
                continue
            if we - lo >= remaining:
                return lo + remaining
            remaining -= we - lo
        day += timedelta(days=1)
    raise ValueError("SLA due date could not be computed")


def more_severe(a: str, b: str | None) -> str:
    """Support may raise severity immediately; a downgrade needs Support Manager approval (SOP-01 §3.2)."""
    return a if b is None or SEVERITY_ORDER[a] <= SEVERITY_ORDER[b] else b


def evaluate_sla(severity: str, plan: str, created_at: datetime, as_of: datetime) -> dict:
    clock = clock_for(severity, plan)
    resolution = RESOLUTION_MINUTES[severity][plan]
    response = RESPONSE_MINUTES[severity][plan]
    elapsed = eligible_minutes(created_at, as_of, clock)
    return {
        "clock": clock,
        "response_target_minutes": response,
        "resolution_target_minutes": resolution,
        "elapsed_eligible_minutes": elapsed,
        "resolution_budget_consumed_pct": round(100.0 * elapsed / resolution, 1),
        "response_due_at": add_eligible_minutes(created_at, response, clock),
        "resolution_due_at": add_eligible_minutes(created_at, resolution, clock),
        "breached": elapsed >= resolution,
    }


def escalation_level(severity: str, consumed_pct: float, is_open: bool) -> tuple[str, str, list[str]]:
    """(level, reason, roles to notify) per SOP-01 §3.3."""
    if not is_open:
        return "NONE", "Ticket is resolved/closed", []
    if severity == "P1":
        if consumed_pct >= 75:
            return ("EXECUTIVE_CHAIR", f"P1 at {consumed_pct}% of resolution budget (>=75%)",
                    ["VP Engineering", "Support Manager"])
        if consumed_pct >= 50:
            return "VP_ENGINEERING", f"P1 at {consumed_pct}% of resolution budget (>=50%)", ["VP Engineering", "Support Manager"]
        return "ON_CALL", "P1: page on-call Support and SRE immediately", ["Support Manager"]
    if severity == "P2":
        if consumed_pct >= 75:
            return ("VP_CUSTOMER_SUCCESS", f"P2 at {consumed_pct}% of resolution budget (>=75%)",
                    ["VP Customer Success", "Support Manager"])
        if consumed_pct >= 50:
            return "SUPPORT_MANAGER", f"P2 at {consumed_pct}% of resolution budget (>=50%)", ["Support Manager"]
        return "NONE", f"P2 at {consumed_pct}% of resolution budget; no escalation threshold reached", []
    return "NONE", f"{severity} follows the standard queue", []
