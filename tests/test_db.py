"""Phase 1 tests: schema creation, PRAGMAs, indexes, schema versioning."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402


@pytest.fixture
def tmp_db_path(tmp_path):
    return tmp_path / "test_permits.db"


def test_init_db_creates_all_tables(tmp_db_path):
    db_module.init_db(tmp_db_path)
    with db_module.db(tmp_db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"permits", "ingestion_runs", "geocode_cache"} <= tables


def test_permits_table_has_unique_constraint(tmp_db_path):
    db_module.init_db(tmp_db_path)
    ts = db_module.now_iso()
    with db_module.db(tmp_db_path) as conn:
        conn.execute(
            """INSERT INTO permits
               (locality, source_permit_id, source_name, first_seen_at, ingested_at)
               VALUES ('norfolk', 'ABC123', 'Norfolk Open Data', ?, ?)""",
            (ts, ts),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with db_module.db(tmp_db_path) as conn:
            conn.execute(
                """INSERT INTO permits
                   (locality, source_permit_id, source_name, first_seen_at, ingested_at)
                   VALUES ('norfolk', 'ABC123', 'Norfolk Open Data', ?, ?)""",
                (ts, ts),
            )


def test_permits_defaults(tmp_db_path):
    db_module.init_db(tmp_db_path)
    ts = db_module.now_iso()
    with db_module.db(tmp_db_path) as conn:
        conn.execute(
            """INSERT INTO permits
               (locality, source_permit_id, source_name, first_seen_at, ingested_at)
               VALUES ('norfolk', 'ABC123', 'Norfolk Open Data', ?, ?)""",
            (ts, ts),
        )
        row = conn.execute("SELECT * FROM permits WHERE source_permit_id='ABC123'").fetchone()
    assert row["status_bucket"] == "other"
    assert row["geocode_status"] == "pending"
    assert row["raw_json"] == "{}"
    assert row["lat"] is None
    assert row["lng"] is None


def test_indexes_created(tmp_db_path):
    db_module.init_db(tmp_db_path)
    with db_module.db(tmp_db_path) as conn:
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    expected = {
        "idx_permits_locality",
        "idx_permits_status_bucket",
        "idx_permits_issued_date",
        "idx_permits_latlng",
        "idx_permits_locality_issued",
        "idx_permits_locality_status",
        "idx_permits_locality_type",
        "idx_ingestion_runs_locality",
    }
    assert expected <= indexes


def test_pragmas_set(tmp_db_path):
    db_module.init_db(tmp_db_path)
    with db_module.db(tmp_db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_schema_version_stamped(tmp_db_path):
    db_module.init_db(tmp_db_path)
    assert db_module.schema_version(tmp_db_path) == db_module.SCHEMA_VERSION


def test_init_db_idempotent(tmp_db_path):
    db_module.init_db(tmp_db_path)
    db_module.init_db(tmp_db_path)  # must not raise or duplicate schema
    assert db_module.schema_version(tmp_db_path) == db_module.SCHEMA_VERSION


def test_init_db_rejects_newer_schema_version(tmp_db_path):
    db_module.init_db(tmp_db_path)
    with db_module.db(tmp_db_path) as conn:
        conn.execute(f"PRAGMA user_version={db_module.SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError):
        db_module.init_db(tmp_db_path)


def test_no_seed_data(tmp_db_path):
    """PermitIQ must never fabricate permit rows on init."""
    db_module.init_db(tmp_db_path)
    with db_module.db(tmp_db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0]
    assert count == 0


def test_ingestion_runs_defaults(tmp_db_path):
    db_module.init_db(tmp_db_path)
    ts = db_module.now_iso()
    with db_module.db(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO ingestion_runs (locality, started_at, status) VALUES ('norfolk', ?, 'running')",
            (ts,),
        )
        row = conn.execute("SELECT * FROM ingestion_runs WHERE locality='norfolk'").fetchone()
    assert row["fetched_count"] == 0
    assert row["error_count"] == 0
    assert row["completed_at"] is None


def test_geocode_cache_roundtrip(tmp_db_path):
    db_module.init_db(tmp_db_path)
    ts = db_module.now_iso()
    with db_module.db(tmp_db_path) as conn:
        conn.execute(
            """INSERT INTO geocode_cache
               (normalized_address, lat, lng, matched_address, provider, confidence, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("123 MAIN ST, NORFOLK, VA", 36.85, -76.28, "123 Main St, Norfolk, VA", "census", "high", "matched", ts),
        )
        row = conn.execute(
            "SELECT * FROM geocode_cache WHERE normalized_address=?",
            ("123 MAIN ST, NORFOLK, VA",),
        ).fetchone()
    assert row["lat"] == 36.85
    assert row["provider"] == "census"
