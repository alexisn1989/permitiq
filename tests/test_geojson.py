"""Phase 7 tests: /api/export.geojson. Same isolated-DB pattern as
test_filters.py -- see that file's module docstring for why this never
touches the real permits.db."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402

db_module.DB_PATH = Path(tempfile.mkdtemp()) / "_collection_time_placeholder.db"

import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from ingest.common import NormalizedPermit, upsert_permits  # noqa: E402

client = TestClient(app_module.app)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db(db_path)
    return db_path


def load_permits(db_path, permits):
    with db_module.db(db_path) as conn:
        upsert_permits(conn, permits)


GEO_SAMPLE_PERMITS = [
    NormalizedPermit(
        locality="norfolk", source_permit_id="N1", source_name="Norfolk Open Data Portal",
        source_url="https://data.norfolk.gov/resource/fahm-yuh4.json",
        address="1 Main St, Norfolk, VA", lat=36.85, lng=-76.28, coordinate_source="census",
        geocode_status="matched", permit_type="Building", status="Issued", status_bucket="issued",
        applied_date="2025-01-01", issued_date="2025-01-10",
        contractor="Acme Construction", description="New construction", valuation=50000.0,
        raw={"gpin": "999999", "internal_note": "should never appear in export"},
    ),
    NormalizedPermit(
        # no coordinates -- must be excluded from GeoJSON entirely
        locality="norfolk", source_permit_id="N2", source_name="Norfolk Open Data Portal",
        address="2 Main St, Norfolk, VA", permit_type="Electrical", status="Finaled",
        status_bucket="finaled_closed", applied_date="2025-02-01",
    ),
    NormalizedPermit(
        locality="virginia_beach", source_permit_id="VB1",
        source_name="Virginia Beach Building Permit Applications (ArcGIS Hub)",
        address="3 Ocean Ave, Virginia Beach, VA", lat=36.90, lng=-76.00, coordinate_source="native",
        geocode_status="matched", permit_type="Plumbing", status="Active", status_bucket="issued",
        applied_date="2025-03-01", issued_date="2025-03-05", valuation=15000.0,
    ),
]


@pytest.fixture
def seeded_geo_db(isolated_db):
    load_permits(isolated_db, GEO_SAMPLE_PERMITS)
    return isolated_db


def test_valid_feature_collection(seeded_geo_db):
    resp = client.get("/api/export.geojson")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert isinstance(body["features"], list)


def test_only_mapped_rows_included(seeded_geo_db):
    body = client.get("/api/export.geojson").json()
    # 3 permits total, only 2 have lat/lng (N2 has none)
    assert len(body["features"]) == 2
    ids = {f["properties"]["source_permit_id"] for f in body["features"]}
    assert ids == {"N1", "VB1"}
    assert "N2" not in ids


def test_coordinates_ordered_lng_lat(seeded_geo_db):
    body = client.get("/api/export.geojson?locality=norfolk").json()
    feature = body["features"][0]
    assert feature["geometry"]["type"] == "Point"
    lng, lat = feature["geometry"]["coordinates"]
    assert lng == -76.28
    assert lat == 36.85


def test_same_filters_as_permit_list(seeded_geo_db):
    body = client.get("/api/export.geojson?locality=virginia_beach").json()
    assert len(body["features"]) == 1
    assert body["features"][0]["properties"]["source_permit_id"] == "VB1"

    body2 = client.get("/api/export.geojson?status_bucket=finaled_closed").json()
    # VB2/N2 not mapped or don't match -- confirm bucket filter applies before the coord filter
    assert len(body2["features"]) == 0  # N2 is finaled_closed but has no coordinates


def test_raw_json_excluded_from_properties(seeded_geo_db):
    body = client.get("/api/export.geojson").json()
    for feature in body["features"]:
        assert "raw_json" not in feature["properties"]
        assert "internal_note" not in str(feature["properties"])


def test_geocode_error_excluded_from_properties(seeded_geo_db):
    body = client.get("/api/export.geojson").json()
    for feature in body["features"]:
        assert "geocode_error" not in feature["properties"]


def test_useful_properties_present(seeded_geo_db):
    body = client.get("/api/export.geojson?locality=norfolk").json()
    props = body["features"][0]["properties"]
    for field in (
        "id", "source_permit_id", "locality", "permit_type", "status", "status_bucket",
        "address", "applied_date", "issued_date", "description", "contractor",
        "source_name", "source_url",
    ):
        assert field in props


def test_export_cap_enforced(seeded_geo_db, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_EXPORT_FEATURES", 1)
    resp = client.get("/api/export.geojson")
    assert resp.status_code == 413
    assert "Narrow your filters" in resp.json()["detail"]


def test_export_cap_not_triggered_when_under_limit(seeded_geo_db, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_EXPORT_FEATURES", 100)
    resp = client.get("/api/export.geojson")
    assert resp.status_code == 200


def test_export_does_not_silently_truncate(seeded_geo_db, monkeypatch):
    # with the cap below the real matching count, it must error, not truncate
    monkeypatch.setattr(app_module, "MAX_EXPORT_FEATURES", 1)
    resp = client.get("/api/export.geojson")
    assert resp.status_code in (413, 422)
    body = resp.json()
    assert "features" not in body  # confirms no partial/truncated payload was returned
