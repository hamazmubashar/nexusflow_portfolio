"""automation_events: audit log + idempotency store for every webhook in and out."""

from __future__ import annotations

import json
from uuid import UUID

from app.core.db import get_conn


def _dumps(obj) -> str:
    return json.dumps(obj, default=str)


def find_inbound(cur, event_id: UUID) -> dict | None:
    cur.execute(
        "SELECT status, response FROM automation_events WHERE event_id = %s AND direction = 'inbound'",
        (str(event_id),),
    )
    return cur.fetchone()


def record(cur, *, event_id: UUID, event_type: str, direction: str, source: str, status: str,
           subject_id: str | None = None, payload=None, response=None) -> bool:
    """Insert one event row; returns False if (event_id, direction) already exists."""
    cur.execute(
        """INSERT INTO automation_events (event_id, event_type, direction, source, subject_id, status, payload, response)
           VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
           ON CONFLICT (event_id, direction) DO NOTHING RETURNING id""",
        (str(event_id), event_type, direction, source, subject_id, status,
         _dumps(payload or {}), _dumps(response) if response is not None else None),
    )
    return cur.fetchone() is not None


def record_standalone(**kwargs) -> bool:
    """record() in its own transaction, for background tasks that run after the request committed."""
    with get_conn() as conn, conn.cursor() as cur:
        return record(cur, **kwargs)
