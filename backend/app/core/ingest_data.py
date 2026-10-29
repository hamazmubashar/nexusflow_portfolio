"""Relational seed ingestion for NexusFlow Autopilot.

Applies database/seed_data/schema.sql and bulk-loads every seed CSV into
PostgreSQL in a single transaction, then verifies the load against
manifest.json and the business invariants documented in schema.sql.

departments.manager_id -> employees and employees.department_id -> departments
form a cycle; the manager FK is DEFERRABLE, so all COPYs run inside one
transaction with constraints deferred and are checked at COMMIT.

Usage (from backend/):
    python -m app.core.ingest_data            # fails if tables already exist
    python -m app.core.ingest_data --reset    # drop + recreate, then load
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2 import sql

from app.core.config import SEED_DATA_DIR, get_settings
from app.core.db import apply_migrations

log = logging.getLogger("nexusflow.ingest")

# Migration-owned tables with FKs into the seed schema, dropped in this order on --reset
DEPENDENT_TABLES = ["seat_requests", "governance_runs", "approval_requests"]

EXPECTED_CUSTOMERS = 120
EXPECTED_ACTIVE_EMPLOYEES = 85
EXPECTED_ACTIVE_ARR = Decimal("12400000.00")

# Every FK in schema.sql, as (child_table, child_cols, parent_table, parent_cols).
# Used for an explicit orphan check after COMMIT, on top of the DB's own enforcement.
FOREIGN_KEYS = [
    ("departments", ("manager_id",), "employees", ("employee_id",)),
    ("employees", ("department_id",), "departments", ("department_id",)),
    ("customers", ("csm_employee_id",), "employees", ("employee_id",)),
    ("contracts", ("customer_id",), "customers", ("customer_id",)),
    ("invoices", ("contract_id", "customer_id"), "contracts", ("contract_id", "customer_id")),
    ("payments", ("invoice_id",), "invoices", ("invoice_id",)),
    ("support_tickets", ("customer_id",), "customers", ("customer_id",)),
    ("support_tickets", ("assigned_employee_id",), "employees", ("employee_id",)),
    ("assets", ("customer_id",), "customers", ("customer_id",)),
]


@dataclass
class IngestReport:
    row_counts: dict[str, int] = field(default_factory=dict)
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(self.checks.values())


def load_manifest(seed_dir: Path) -> dict:
    return json.loads((seed_dir / "manifest.json").read_text())


def csv_header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh))


def existing_tables(cur, tables: list[str]) -> list[str]:
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (tables,),
    )
    return [r[0] for r in cur.fetchall()]


def drop_tables(cur, tables: list[str]) -> None:
    for table in reversed(tables):
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(table)))


def apply_schema(cur, schema_path: Path) -> None:
    # schema.sql carries its own BEGIN/COMMIT, so it is atomic on its own.
    cur.execute(schema_path.read_text(encoding="utf-8"))


def copy_tables(conn, seed_dir: Path, import_order: list[str]) -> dict[str, int]:
    """COPY all CSVs in one transaction; deferred FKs are checked at COMMIT."""
    loaded: dict[str, int] = {}
    with conn:  # commits on success, rolls back everything on any error
        with conn.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL DEFERRED")
            for table in import_order:
                path = seed_dir / f"{table}.csv"
                columns = csv_header(path)
                copy_stmt = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                )
                with path.open(encoding="utf-8") as fh:
                    cur.copy_expert(copy_stmt.as_string(cur), fh)
                loaded[table] = cur.rowcount
                log.info("  %-16s %5d rows", table, cur.rowcount)
    return loaded


def count_orphans(cur, child: str, child_cols, parent: str, parent_cols) -> int:
    join_on = sql.SQL(" AND ").join(
        sql.SQL("p.{} = c.{}").format(sql.Identifier(pc), sql.Identifier(cc))
        for cc, pc in zip(child_cols, parent_cols)
    )
    not_null = sql.SQL(" AND ").join(
        sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(cc)) for cc in child_cols
    )
    query = sql.SQL(
        "SELECT COUNT(*) FROM {child} c WHERE {not_null} "
        "AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {join_on})"
    ).format(child=sql.Identifier(child), parent=sql.Identifier(parent),
             not_null=not_null, join_on=join_on)
    cur.execute(query)
    return cur.fetchone()[0]


def verify(cur, manifest: dict) -> IngestReport:
    report = IngestReport()
    for table, expected in manifest["table_row_counts"].items():
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
        actual = cur.fetchone()[0]
        report.row_counts[table] = actual
        report.checks[f"row_count:{table}={expected}"] = actual == expected

    for child, ccols, parent, pcols in FOREIGN_KEYS:
        orphans = count_orphans(cur, child, ccols, parent, pcols)
        report.checks[f"fk:{child}({','.join(ccols)})->{parent}"] = orphans == 0

    cur.execute("SELECT COUNT(*) FROM customers")
    report.checks[f"customers={EXPECTED_CUSTOMERS}"] = cur.fetchone()[0] == EXPECTED_CUSTOMERS
    cur.execute("SELECT COUNT(*) FROM employees WHERE status = 'ACTIVE'")
    report.checks[f"active_employees={EXPECTED_ACTIVE_EMPLOYEES}"] = (
        cur.fetchone()[0] == EXPECTED_ACTIVE_EMPLOYEES
    )
    cur.execute("SELECT COALESCE(SUM(annual_recurring_revenue), 0) FROM contracts WHERE status = 'ACTIVE'")
    report.checks[f"active_arr={EXPECTED_ACTIVE_ARR}"] = cur.fetchone()[0] == EXPECTED_ACTIVE_ARR
    cur.execute("SELECT COUNT(*) FROM departments")
    report.checks["departments=5"] = cur.fetchone()[0] == 5
    cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    report.checks["pgvector_enabled"] = cur.fetchone() is not None
    return report


def run(dsn: str, seed_dir: Path = SEED_DATA_DIR, reset: bool = False) -> IngestReport:
    manifest = load_manifest(seed_dir)
    import_order: list[str] = manifest["import_order"]

    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            present = existing_tables(cur, import_order)
            if present:
                if not reset:
                    raise SystemExit(
                        f"Tables already exist ({', '.join(sorted(present))}). "
                        "Re-run with --reset to drop and reload."
                    )
                # Ledger tables from database/migrations reference customers/contracts; DROP ... CASCADE
                # on the seed tables would silently strip their FKs, so they are dropped and rebuilt
                # too. Their rows describe the old seed data and are meaningless after a reload.
                # automation_events and document_chunks have no FKs and are kept.
                log.info("Dropping existing tables: %s (+ %s)", ", ".join(present), ", ".join(DEPENDENT_TABLES))
                drop_tables(cur, DEPENDENT_TABLES)
                drop_tables(cur, import_order)
            log.info("Applying %s", seed_dir / "schema.sql")
            apply_schema(cur, seed_dir / "schema.sql")

        conn.autocommit = False
        log.info("Loading CSVs (single transaction, constraints deferred):")
        copy_tables(conn, seed_dir, import_order)

        conn.autocommit = True
        with conn.cursor() as cur:
            apply_migrations(cur)  # recreate dependent tables with their FKs
            cur.execute(sql.SQL("ANALYZE"))
            return verify(cur, manifest)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load NexusFlow seed data into PostgreSQL.")
    parser.add_argument("--reset", action="store_true", help="drop and recreate tables before loading")
    parser.add_argument("--seed-dir", type=Path, default=SEED_DATA_DIR)
    parser.add_argument("--dsn", default=None, help="override DATABASE_URL")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dsn = args.dsn or get_settings().psycopg2_dsn
    report = run(dsn, seed_dir=args.seed_dir, reset=args.reset)

    log.info("\nVerification:")
    for name, passed in report.checks.items():
        log.info("  [%s] %s", "PASS" if passed else "FAIL", name)
    log.info("\n%s", "Ingestion OK" if report.ok else "Ingestion completed with FAILED checks")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
