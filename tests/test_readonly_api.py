"""Stage 1 prerequisite: the API is read-only. Importing app.py touches no
file, routes open the database with mode=ro, a missing/unmigrated database is
a 503 (never silently created), and serving requests leaves the file's bytes
and mtime unchanged."""

import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db as db_module  # noqa: E402

db_module.DB_PATH = Path(tempfile.mkdtemp()) / "_collection_time_placeholder.db"

import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from ingest.common import NormalizedPermit, upsert_permits  # noqa: E402

client = TestClient(app_module.app)

READ_ROUTES = ["/health", "/api/permits", "/api/stats", "/api/facets", "/api/data-status",
               "/api/export.geojson", "/api/export.csv", "/api/flood-stats", "/api/flood-zones/index.json",
               "/api/land-use/decisions", "/api/land-use/export.csv", "/api/land-use/stats",
               "/api/parcels/14892864850000.geojson", "/api/flood-stats?basis=parcel",
               "/api/quality", "/api/data-as-of", "/api/audit/decision_outcome_review?decision_key=vb:M1:R1",
               "/api/land-use/pc-items", "/api/land-use/pc-items/vbpc:2026-08-12:2",
               "/api/land-use/filings", "/api/land-use/filings/2026-PCCC-00001",
               "/api/land-use/council-actions", "/api/land-use/council-actions/vbcc:2025-03-18:K.2."]


def test_importing_app_creates_no_database_file(tmp_path):
    env = {**os.environ, "DATA_DIR": str(tmp_path)}
    subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT, env=env, check=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("route", READ_ROUTES)
def test_missing_database_is_503_and_not_created(tmp_path, monkeypatch, route):
    db_path = tmp_path / "absent.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    r = client.get(route)
    assert r.status_code == 503
    assert not db_path.exists()


def test_unmigrated_v1_database_is_503_with_hint(tmp_path, monkeypatch):
    db_path = tmp_path / "v1.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version=1")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    r = client.get("/api/permits")
    assert r.status_code == 503
    assert "migrate_db.py" in r.json()["detail"]["action"]
    health = client.get("/health")
    assert health.status_code == 503


def _digest(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_requests_do_not_modify_database(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    db_module.init_db(db_path)
    with db_module.db(db_path) as conn:
        upsert_permits(conn, [NormalizedPermit(
            locality="virginia_beach", source_permit_id="V1", source_name="VB", lat=36.8, lng=-76.0,
            coordinate_source="native", geocode_status="native", issued_date="2025-01-01",
            status_bucket="issued", raw={"PermitType": "Building"})])
    # Checkpoint the WAL so the main file holds everything and is stable.
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    before = _digest(db_path)
    for route in READ_ROUTES + ["/api/permits?flood_class=untagged", "/api/flood-zones/0/0.geojson"]:
        client.get(route)
    assert _digest(db_path) == before


def test_readonly_connection_rejects_writes(tmp_path):
    db_path = tmp_path / "test.db"
    db_module.init_db(db_path)
    with db_module.db_readonly(db_path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM permits")


def test_migrate_v1_database_keeps_rows(tmp_path):
    """A v1 file (no flood tables) gains the v2 schema additively."""
    db_path = tmp_path / "v1.db"
    db_module.init_db(db_path)
    with db_module.db(db_path) as conn:
        upsert_permits(conn, [NormalizedPermit(locality="virginia_beach", source_permit_id="V1", source_name="VB")])
        for t in ("permit_flood_effective",):
            conn.execute(f"DROP VIEW {t}")
        for t in ("permit_flood", "flood_display_cells", "flood_zones", "flood_zone_loads", "flood_locality_dfirm"):
            conn.execute(f"DROP TABLE {t}")
        conn.execute("PRAGMA user_version=1")
    db_module.init_db(db_path)
    assert db_module.schema_version(db_path) == db_module.SCHEMA_VERSION
    with db_module.db_readonly(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 1
        assert conn.execute("SELECT effective_class FROM permit_flood_effective").fetchone()[0] == "untagged"
        assert conn.execute("SELECT dfirm_id FROM flood_locality_dfirm WHERE locality='virginia_beach'").fetchone()[0] == "515531"
