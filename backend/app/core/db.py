"""Thin psycopg2 helpers shared by the API and the ingestion scripts."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extras

from app.core.config import MIGRATIONS_DIR, get_settings


@contextmanager
def get_conn(dsn: str | None = None) -> Iterator[psycopg2.extensions.connection]:
    """Connection that commits on success and rolls back on error; rows come back as dicts."""
    conn = psycopg2.connect(dsn or get_settings().psycopg2_dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def apply_migrations(cur) -> None:
    """Run every database/migrations/*.sql in name order (each file is idempotent)."""
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        cur.execute(sql_file.read_text(encoding="utf-8"))
