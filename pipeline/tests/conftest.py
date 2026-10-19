"""Shared fixtures.

Unit tests need nothing. Database tests need a PostgreSQL server with
PostGIS and the `psql` client:

    PERMITIQ_TEST_ADMIN_DSN="host=127.0.0.1 port=5432 user=permitiq password=... dbname=postgres" pytest

The DSN must be able to CREATE DATABASE and SET ROLE powerbi_reader (the
docker-compose superuser can). Each test gets a throwaway database cloned
from a template built once per session: migrations 001-006, then rows in the
pre-007 shape (legacy hash IDs), then 007. Without the variable, database
tests are skipped.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from permitiq import planning_identity as pid  # noqa: E402

ADMIN_DSN = os.getenv("PERMITIQ_TEST_ADMIN_DSN")
MIGRATIONS_BEFORE_007 = [ROOT / "db" / f for f in (
    "001_schema.sql", "002_views.sql", "003_roles.sql", "004_violations.sql",
    "005_planning.sql", "006_planning_excerpt.sql")]
MIGRATION_007 = ROOT / "db" / "007_identity_and_events.sql"

# Pre-007 rows, written the way the old scanner + ingester wrote them.
LEGACY_APPLICANT, LEGACY_ADDRESS = "Oceanfront Holdings, LLC", "2401 Atlantic Avenue"
LEGACY_ID = pid.legacy_hash_id(LEGACY_APPLICANT, LEGACY_ADDRESS)
INPUT_ID = "EXT-2026-001"


def dsn_for(dbname: str) -> str:
    from psycopg.conninfo import make_conninfo
    return make_conninfo(ADMIN_DSN, dbname=dbname)


def run_sql_file(dbname: str, path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "POWERBI_READER_PASSWORD"}  # never touch the role's password
    subprocess.run(["psql", dsn_for(dbname), "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)],
                   check=True, capture_output=True, text=True, env=env)


def seed_pre_007(dbname: str) -> None:
    import psycopg
    with psycopg.connect(dsn_for(dbname)) as c:
        c.execute("""
            INSERT INTO planning_applications (application_id, project_name, address, application_type,
                proposed_use, hearing_date, status, source_excerpt, geocode_source)
            VALUES (%s, %s, %s, NULL, NULL, '2026-09-09', 'Consent Agenda', %s, 'none'),
                   (%s, 'External Co', '1 External Way', 'Rezoning', NULL, '2026-08-01', 'Pending', NULL, 'none')
        """, (LEGACY_ID, LEGACY_APPLICANT, LEGACY_ADDRESS, f"{LEGACY_APPLICANT} {LEGACY_ADDRESS} Council District 6",
              INPUT_ID))
        c.execute("INSERT INTO planning_applications_raw (application_id, payload) VALUES (%s, '{}'), (%s, '{}')",
                  (LEGACY_ID, INPUT_ID))


def create_db(name: str, template: str | None = None) -> None:
    import psycopg
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        tmpl = f" TEMPLATE {template}" if template else " TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
        c.execute(f'CREATE DATABASE "{name}"{tmpl}')


def drop_db(name: str) -> None:
    import psycopg
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _require_db():
    if not ADMIN_DSN:
        pytest.skip("PERMITIQ_TEST_ADMIN_DSN not set")
    if not shutil.which("psql"):
        pytest.skip("psql client not found")


@pytest.fixture(scope="session")
def template_db():
    _require_db()
    name = f"permitiq_tmpl_{uuid.uuid4().hex[:8]}"
    create_db(name)
    try:
        for m in MIGRATIONS_BEFORE_007:
            run_sql_file(name, m)
        seed_pre_007(name)
        run_sql_file(name, MIGRATION_007)
        yield name
    finally:
        drop_db(name)


@pytest.fixture
def db(template_db):
    """A fresh database (001-006, legacy rows, 007). Yields its DSN."""
    name = f"permitiq_t_{uuid.uuid4().hex[:8]}"
    create_db(name, template=template_db)
    try:
        yield dsn_for(name)
    finally:
        drop_db(name)


@pytest.fixture
def fresh_pre_007_db():
    """001-006 plus legacy rows, WITHOUT 007 -- for migration tests. Yields the db name."""
    _require_db()
    name = f"permitiq_m_{uuid.uuid4().hex[:8]}"
    create_db(name)
    try:
        for m in MIGRATIONS_BEFORE_007:
            run_sql_file(name, m)
        seed_pre_007(name)
        yield name
    finally:
        drop_db(name)


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    """ingest(dsn, records, *extra_args) -> exit code. Runs ingest_planning.main()
    in-process with geocoding stubbed out (counted in ingest.geocode_calls)."""
    import ingest_planning

    calls = []
    monkeypatch.setattr(ingest_planning, "geocode_lite", lambda q: calls.append(q) or None)

    def run(dsn, records, *extra):
        f = tmp_path / f"leads_{uuid.uuid4().hex[:6]}.json"
        f.write_text(json.dumps(records))
        monkeypatch.setattr(sys, "argv", ["ingest_planning.py", "--file", str(f), "--dsn", dsn, *extra])
        return ingest_planning.main()

    run.geocode_calls = calls
    return run


def q(dsn: str, sql: str, params=()):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql, params).fetchall()
