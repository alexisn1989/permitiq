"""Neon parcels/assessments loader (ingest/neon_source.py +
scripts/load_neon_assessments.py) and the /api/parcels/{gpin} route.
Neon's HTTPS SQL endpoint is mocked -- zero live network calls. Every test
uses its own temp SQLite DB (same DB_PATH redirect as test_filters.py)."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402

db_module.DB_PATH = Path(tempfile.mkdtemp()) / "_collection_time_placeholder.db"

import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from ingest import neon_source  # noqa: E402
from ingest.common import SourceSchemaError  # noqa: E402
from scripts import load_neon_assessments as loader  # noqa: E402

client = TestClient(app_module.app)
CONN_STR = "postgresql://user:secret@ep-test-pooler.us-west-2.aws.neon.tech/fiscalmapiq?sslmode=require"


def raw_parcel(parcel_id, gpin, **overrides):
    # Neon returns bigint/numeric as strings -- mirror that.
    row = {src: None for src in neon_source.PARCEL_COLUMNS}
    row.update({
        "parcel_id": str(parcel_id), "city_code": "VB", "gpin": gpin,
        "prop_address": f"{parcel_id} SHOREHAVEN DR", "latitude": "36.881387",
        "longitude": "-76.057511", "source_id": 79, "data_year": 2026,
        "loaded_at": "2026-07-26 14:16:26.892545+00",
    })
    row.update(overrides)
    return row


def raw_assessment(assessment_id, parcel_id, **overrides):
    row = {src: None for src in neon_source.ASSESSMENT_COLUMNS}
    row.update({
        "assessment_id": str(assessment_id), "parcel_id": str(parcel_id), "city_code": "VB",
        "tax_year": 2027, "land_value": "472700.00", "improvement_value": "70100.00",
        "total_value": "542800.00", "assessment_effective_date": "2026-07-01",
        "source_id": 734, "data_year": 2026, "loaded_at": "2026-07-29 18:47:15.689995+00",
    })
    row.update(overrides)
    return row


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.ok = status_code < 400
        self.content = b"x"

    def json(self):
        return self._body


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db(db_path)
    return db_path


@pytest.fixture
def fake_neon(monkeypatch):
    """Serve `tables[name]` rows with keyset pagination like the real query."""
    tables = {"parcels": [], "assessments": []}
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "query": json["query"], "params": json["params"], "headers": headers})
        table = "parcels" if "FROM parcels" in json["query"] else "assessments"
        key = neon_source.TABLES[table][1]
        city, last_key, batch = json["params"]
        rows = [r for r in tables[table] if r["city_code"] == city and int(r[key]) > last_key]
        rows.sort(key=lambda r: int(r[key]))
        return FakeResponse({"rows": rows[:batch]})

    monkeypatch.setattr(neon_source.requests, "post", fake_post)
    monkeypatch.setattr(neon_source.time, "sleep", lambda s: None)
    return tables, calls


def test_fetch_converts_types_and_preserves_nulls(fake_neon):
    tables, _ = fake_neon
    tables["parcels"] = [raw_parcel(1, "100", prop_address=None)]
    (row,) = neon_source.fetch_table("parcels", conn_str=CONN_STR)
    assert row["parcel_id"] == 1
    assert row["latitude"] == pytest.approx(36.881387)
    assert row["prop_address"] is None
    assert row["zoning"] is None
    assert row["source_loaded_at"] == "2026-07-26 14:16:26.892545+00"


def test_fetch_paginates_by_key_and_respects_limit(fake_neon):
    tables, calls = fake_neon
    tables["parcels"] = [raw_parcel(i, str(i)) for i in range(1, 8)]
    rows = list(neon_source.fetch_table("parcels", conn_str=CONN_STR, page_size=3))
    assert [r["parcel_id"] for r in rows] == list(range(1, 8))
    assert [c["params"][1] for c in calls] == [-1, 3, 6]

    calls.clear()
    rows = list(neon_source.fetch_table("parcels", conn_str=CONN_STR, page_size=3, limit=4))
    assert len(rows) == 4


def test_query_is_select_only_and_targets_https_sql_endpoint(fake_neon):
    tables, calls = fake_neon
    tables["assessments"] = [raw_assessment(10, 1)]
    list(neon_source.fetch_table("assessments", conn_str=CONN_STR))
    assert calls[0]["url"] == "https://ep-test-pooler.us-west-2.aws.neon.tech/sql"
    assert calls[0]["headers"]["Neon-Connection-String"] == CONN_STR
    assert all(c["query"].startswith("SELECT ") for c in calls)


def test_missing_source_column_raises_schema_error(fake_neon):
    tables, _ = fake_neon
    row = raw_parcel(1, "100")
    del row["gpin"]
    tables["parcels"] = [row]
    with pytest.raises(SourceSchemaError):
        list(neon_source.fetch_table("parcels", conn_str=CONN_STR))


def test_sql_error_is_not_retried(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        return FakeResponse({"message": 'column "gpin" does not exist'}, status_code=400)

    monkeypatch.setattr(neon_source.requests, "post", fake_post)
    with pytest.raises(SourceSchemaError):
        neon_source.run_query(CONN_STR, "SELECT 1", [])
    assert len(calls) == 1


def test_missing_connection_string_fails_clearly(monkeypatch):
    monkeypatch.delenv(neon_source.ENV_VAR, raising=False)
    with pytest.raises(RuntimeError, match=neon_source.ENV_VAR):
        neon_source.connection_string()


def test_loader_is_idempotent_and_detects_changes(isolated_db, fake_neon, monkeypatch):
    tables, _ = fake_neon
    tables["parcels"] = [raw_parcel(1, "100"), raw_parcel(2, "200")]
    tables["assessments"] = [raw_assessment(10, 1), raw_assessment(11, 2)]
    monkeypatch.setenv(neon_source.ENV_VAR, CONN_STR)

    loader.main([])
    with db_module.db(isolated_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 2

    with db_module.db(isolated_db) as conn:
        rows = list(neon_source.fetch_table("parcels", conn_str=CONN_STR))
        assert loader.upsert_rows(conn, "parcels", rows) == {
            "inserted": 0, "updated": 0, "unchanged": 2, "skipped": 0,
        }

    tables["parcels"][0]["zoning"] = "R-10"
    with db_module.db(isolated_db) as conn:
        rows = list(neon_source.fetch_table("parcels", conn_str=CONN_STR))
        assert loader.upsert_rows(conn, "parcels", rows)["updated"] == 1
        assert conn.execute("SELECT zoning FROM parcels WHERE parcel_id=1").fetchone()[0] == "R-10"


def test_loader_records_runs_outside_permit_localities(isolated_db, fake_neon, monkeypatch):
    tables, _ = fake_neon
    tables["parcels"] = [raw_parcel(1, "100")]
    monkeypatch.setenv(neon_source.ENV_VAR, CONN_STR)
    loader.main([])
    with db_module.db(isolated_db) as conn:
        runs = conn.execute("SELECT locality, status FROM ingestion_runs").fetchall()
    assert {(r["locality"], r["status"]) for r in runs} == {(loader.RUN_LOCALITY, "success")}
    assert loader.RUN_LOCALITY not in app_module.LOCALITY_INFO


def test_dry_run_writes_nothing(isolated_db, fake_neon, monkeypatch):
    tables, _ = fake_neon
    tables["parcels"] = [raw_parcel(1, "100")]
    monkeypatch.setenv(neon_source.ENV_VAR, CONN_STR)
    loader.main(["--dry-run"])
    with db_module.db(isolated_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0


def test_parcel_endpoint_returns_parcel_with_assessments(isolated_db, fake_neon, monkeypatch):
    tables, _ = fake_neon
    tables["parcels"] = [raw_parcel(1, "14987925480000")]
    tables["assessments"] = [raw_assessment(10, 1)]
    monkeypatch.setenv(neon_source.ENV_VAR, CONN_STR)
    loader.main([])

    resp = client.get("/api/parcels/14987925480000")
    assert resp.status_code == 200
    body = resp.json()
    assert body["prop_address"] == "1 SHOREHAVEN DR"
    assert body["assessments"][0]["total_value"] == 542800.0
    assert body["assessments"][0]["taxable_total_value"] is None


def test_parcel_endpoint_404_when_absent(isolated_db):
    assert client.get("/api/parcels/does-not-exist").status_code == 404
