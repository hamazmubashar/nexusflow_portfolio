"""Seed-data and ingestion tests.

Offline tests validate the CSVs against manifest.json and each other.
The integration test loads into a live PostgreSQL and is skipped when the
database is unreachable (start it with `docker compose up -d postgres`).
"""

import csv
import os
import re

import psycopg2
import pytest

from app.core.config import SEED_DATA_DIR, get_settings
from app.core.ingest_data import FOREIGN_KEYS, csv_header, load_manifest, run

MANIFEST = load_manifest(SEED_DATA_DIR)
SCHEMA = (SEED_DATA_DIR / "schema.sql").read_text()


def read_rows(table: str) -> list[dict]:
    with (SEED_DATA_DIR / f"{table}.csv").open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def schema_columns(table: str) -> set[str]:
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", SCHEMA, re.S).group(1)
    cols = set(re.findall(r"(?:^|,)\s*([a-z_]+) (?:varchar|text|integer|numeric|date|timestamptz|boolean)", body, re.M))
    return cols


@pytest.mark.parametrize("table", MANIFEST["import_order"])
def test_csv_exists_and_row_count_matches_manifest(table):
    assert len(read_rows(table)) == MANIFEST["table_row_counts"][table]


@pytest.mark.parametrize("table", MANIFEST["import_order"])
def test_csv_header_matches_schema_columns(table):
    assert set(csv_header(SEED_DATA_DIR / f"{table}.csv")) == schema_columns(table)


@pytest.mark.parametrize("child,ccols,parent,pcols", FOREIGN_KEYS)
def test_csv_foreign_keys_resolve(child, ccols, parent, pcols):
    parent_keys = {tuple(r[c] for c in pcols) for r in read_rows(parent)}
    orphans = [
        r for r in read_rows(child)
        if all(r[c] for c in ccols) and tuple(r[c] for c in ccols) not in parent_keys
    ]
    assert orphans == []


def _db_available() -> bool:
    try:
        psycopg2.connect(get_settings().psycopg2_dsn, connect_timeout=2).close()
        return True
    except psycopg2.OperationalError:
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _db_available(), reason="PostgreSQL not reachable at DATABASE_URL")
@pytest.mark.skipif(os.environ.get("NEXUSFLOW_TEST_ALLOW_DB_RESET") != "1",
                    reason="destructive: drops and reloads every table, including pending approvals and agent runs. "
                           "Run with NEXUSFLOW_TEST_ALLOW_DB_RESET=1 against a database you can wipe")
def test_full_ingestion_into_postgres():
    report = run(get_settings().psycopg2_dsn, reset=True)
    failed = [name for name, ok in report.checks.items() if not ok]
    assert failed == []
    assert report.row_counts == MANIFEST["table_row_counts"]
