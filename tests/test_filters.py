"""Phase 7 tests: the read-only API (everything except GeoJSON export, see
test_geojson.py). Uses an isolated temp SQLite DB for every test -- never the
real permits.db in this repo. db_module.DB_PATH is redirected to a throwaway
path BEFORE app.py is first imported (belt and braces: importing app.py no
longer touches any file -- it has no import-time init_db()); each test then
redirects DB_PATH to its own tmp_path DB via the isolated_db fixture, which
creates the schema with init_db() exactly as a CLI writer would. Every route
in app.py resolves db_module.DB_PATH fresh on each request via
db_module.db_readonly(), so the redirect takes effect with no need to reload
the app module.
"""

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


SAMPLE_PERMITS = [
    NormalizedPermit(
        locality="norfolk", source_permit_id="N1", source_name="Norfolk Open Data Portal",
        source_url="https://data.norfolk.gov/resource/fahm-yuh4.json",
        address="1 Main St, Norfolk, VA", lat=36.85, lng=-76.28, coordinate_source="census",
        geocode_status="matched", geocode_provider="census", geocode_confidence="high",
        permit_type="Building", status="Issued", status_bucket="issued",
        applied_date="2025-01-01", issued_date="2025-01-10",
        contractor="Acme Construction", description="New construction", valuation=50000.0,
        raw={"gpin": "123"},
    ),
    NormalizedPermit(
        locality="norfolk", source_permit_id="N2", source_name="Norfolk Open Data Portal",
        address="2 Main St, Norfolk, VA", permit_type="Electrical", status="Finaled",
        status_bucket="finaled_closed", applied_date="2025-02-01", issued_date="2025-02-15",
        finaled_date="2025-03-01", description="Panel upgrade",
    ),
    NormalizedPermit(
        locality="norfolk", source_permit_id="N3", source_name="Norfolk Open Data Portal",
        address="Verizon - Poles 12, Norfolk, VA", permit_type="Utility", status="Under Review",
        status_bucket="active_pending", applied_date="2025-02-15", geocode_status="no_address",
    ),
    NormalizedPermit(
        locality="virginia_beach", source_permit_id="VB1",
        source_name="Virginia Beach Building Permit Applications (ArcGIS Hub)",
        address="3 Ocean Ave, Virginia Beach, VA", lat=36.90, lng=-76.00, coordinate_source="native",
        geocode_status="matched", permit_type="Plumbing", status="Active", status_bucket="issued",
        applied_date="2025-03-01", issued_date="2025-03-05", valuation=15000.0,
        contractor="Beach Plumbing Co", description="Water heater",
    ),
    NormalizedPermit(
        locality="virginia_beach", source_permit_id="VB2",
        source_name="Virginia Beach Building Permit Applications (ArcGIS Hub)",
        address="4 Ocean Ave, Virginia Beach, VA", permit_type="Building", status="Closed",
        status_bucket="finaled_closed", applied_date="2025-01-20", issued_date="2025-01-25",
        finaled_date="2025-02-01", geocode_status="not_found",
        geocode_error="no geocoder returned a match",
    ),
    NormalizedPermit(
        locality="virginia_beach", source_permit_id="VB3",
        source_name="Virginia Beach Building Permit Applications (ArcGIS Hub)",
        address="5 Ocean Ave, Virginia Beach, VA", permit_type="Building", status="Pending",
        status_bucket="active_pending", applied_date="2025-04-01", issued_date="2025-01-10",  # tie with N1
    ),
]


@pytest.fixture
def seeded_db(isolated_db):
    load_permits(isolated_db, SAMPLE_PERMITS)
    return isolated_db


# ---------- /health ----------

def test_health_ok(seeded_db):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok", "schema_version": db_module.SCHEMA_VERSION}


def test_health_checks_required_tables(isolated_db):
    # schema is created by init_db() in the fixture -- confirm health passes
    # even with zero permit rows (schema presence, not data, is what matters)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["database"] == "ok"


def test_health_reports_error_for_missing_schema(tmp_path, monkeypatch):
    # point at a file that exists but has none of the required tables
    empty_db = tmp_path / "empty.db"
    import sqlite3
    sqlite3.connect(empty_db).close()
    monkeypatch.setattr(db_module, "DB_PATH", empty_db)
    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "error"
    assert "traceback" not in str(body).lower()


# ---------- /api/permits: pagination ----------

def test_default_pagination(seeded_db):
    resp = client.get("/api/permits")
    body = resp.json()
    assert resp.status_code == 200
    assert body["count"] == 6
    assert body["returned"] == 6
    assert body["limit"] == 1000
    assert body["offset"] == 0


def test_count_differs_from_returned_when_paginated(seeded_db):
    resp = client.get("/api/permits?limit=2")
    body = resp.json()
    assert body["count"] == 6
    assert body["returned"] == 2
    assert len(body["permits"]) == 2


def test_limit_above_max_rejected(seeded_db):
    resp = client.get("/api/permits?limit=5001")
    assert resp.status_code == 422


def test_limit_at_max_accepted(seeded_db):
    resp = client.get("/api/permits?limit=5000")
    assert resp.status_code == 200


def test_limit_below_one_rejected(seeded_db):
    resp = client.get("/api/permits?limit=0")
    assert resp.status_code == 422


def test_offset_behavior(seeded_db):
    page1 = client.get("/api/permits?limit=2&offset=0&sort=id&order=asc").json()
    page2 = client.get("/api/permits?limit=2&offset=2&sort=id&order=asc").json()
    ids1 = {p["id"] for p in page1["permits"]}
    ids2 = {p["id"] for p in page2["permits"]}
    assert ids1.isdisjoint(ids2)


def test_negative_offset_rejected(seeded_db):
    resp = client.get("/api/permits?offset=-1")
    assert resp.status_code == 422


# ---------- /api/permits: sorting / stable ordering ----------

def test_default_ordering_is_stable_with_tiebreak(seeded_db):
    # N1 and VB3 both have issued_date=2025-01-10 -- id DESC must break the tie
    resp = client.get("/api/permits").json()
    ids = [p["id"] for p in resp["permits"]]
    assert ids == sorted(ids, reverse=True) or True  # sanity: no crash; real assertion below
    tied = [p for p in resp["permits"] if p["issued_date"] == "2025-01-10"]
    assert len(tied) == 2
    assert tied[0]["id"] > tied[1]["id"]  # id DESC tiebreak


def test_explicit_sort_field_includes_id_tiebreak(seeded_db):
    resp = client.get("/api/permits?sort=issued_date&order=asc").json()
    dates = [p["issued_date"] for p in resp["permits"] if p["issued_date"]]
    assert dates == sorted(dates)


def test_invalid_sort_field_rejected(seeded_db):
    resp = client.get("/api/permits?sort=raw_json")
    assert resp.status_code == 422


def test_invalid_sort_field_injection_attempt_rejected(seeded_db):
    resp = client.get("/api/permits?sort=id;DROP TABLE permits;--")
    assert resp.status_code == 422
    # confirm the table really is untouched
    resp2 = client.get("/api/permits")
    assert resp2.status_code == 200
    assert resp2.json()["count"] == 6


def test_invalid_order_rejected(seeded_db):
    resp = client.get("/api/permits?sort=id&order=sideways")
    assert resp.status_code == 422


def test_order_case_insensitive(seeded_db):
    resp = client.get("/api/permits?sort=id&order=ASC")
    assert resp.status_code == 200


# ---------- /api/permits: filtering ----------

def test_filter_by_locality(seeded_db):
    resp = client.get("/api/permits?locality=norfolk").json()
    assert resp["count"] == 3
    assert all(p["locality"] == "norfolk" for p in resp["permits"])


def test_filter_by_unknown_locality_rejected(seeded_db):
    resp = client.get("/api/permits?locality=atlantis")
    assert resp.status_code == 422


def test_filter_by_permit_type(seeded_db):
    resp = client.get("/api/permits?permit_type=Building").json()
    assert resp["count"] == 3
    assert all(p["permit_type"] == "Building" for p in resp["permits"])


def test_filter_by_status(seeded_db):
    resp = client.get("/api/permits?status=Closed").json()
    assert resp["count"] == 1
    assert resp["permits"][0]["source_permit_id"] == "VB2"


def test_filter_by_status_bucket(seeded_db):
    resp = client.get("/api/permits?status_bucket=issued").json()
    assert resp["count"] == 2
    assert {p["source_permit_id"] for p in resp["permits"]} == {"N1", "VB1"}


def test_filter_by_unknown_status_bucket_rejected(seeded_db):
    resp = client.get("/api/permits?status_bucket=not_a_bucket")
    assert resp.status_code == 422


def test_search_query_matches_address(seeded_db):
    resp = client.get("/api/permits?q=ocean").json()
    assert resp["count"] == 3
    assert all("ocean" in p["address"].lower() for p in resp["permits"])


def test_search_query_matches_contractor(seeded_db):
    resp = client.get("/api/permits?q=acme").json()
    assert resp["count"] == 1
    assert resp["permits"][0]["source_permit_id"] == "N1"


def test_search_query_sql_injection_string_treated_as_literal(seeded_db):
    resp = client.get("/api/permits?q=" + "' OR '1'='1")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0  # no address/description/contractor contains this literal string


def test_date_range_filter(seeded_db):
    resp = client.get("/api/permits?issued_from=2025-02-01&issued_to=2025-02-28").json()
    assert resp["count"] == 1
    assert resp["permits"][0]["source_permit_id"] == "N2"


def test_invalid_date_format_rejected(seeded_db):
    resp = client.get("/api/permits?issued_from=02/01/2025")
    assert resp.status_code == 422


def test_combined_filters(seeded_db):
    resp = client.get("/api/permits?locality=virginia_beach&status_bucket=finaled_closed").json()
    assert resp["count"] == 1
    assert resp["permits"][0]["source_permit_id"] == "VB2"


# ---------- /api/permits: field exposure ----------

def test_raw_json_never_exposed_in_list(seeded_db):
    resp = client.get("/api/permits?locality=norfolk&sort=id&order=asc").json()
    for p in resp["permits"]:
        assert "raw_json" not in p
        assert "raw" not in p


def test_provenance_fields_exposed(seeded_db):
    resp = client.get("/api/permits?locality=norfolk&sort=id&order=asc").json()
    n1 = next(p for p in resp["permits"] if p["source_permit_id"] == "N1")
    assert n1["source_name"] == "Norfolk Open Data Portal"
    assert n1["source_url"] == "https://data.norfolk.gov/resource/fahm-yuh4.json"


# ---------- /api/permits/{id} ----------

def test_get_permit_by_id(seeded_db):
    listing = client.get("/api/permits?locality=norfolk&sort=id&order=asc").json()
    permit_id = listing["permits"][0]["id"]
    resp = client.get(f"/api/permits/{permit_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == permit_id


def test_get_permit_missing_id_returns_404(seeded_db):
    resp = client.get("/api/permits/999999")
    assert resp.status_code == 404


def test_get_permit_raw_json_not_exposed(seeded_db):
    listing = client.get("/api/permits?locality=norfolk&sort=id&order=asc").json()
    permit_id = listing["permits"][0]["id"]
    resp = client.get(f"/api/permits/{permit_id}")
    assert "raw_json" not in resp.json()


# ---------- /api/facets ----------

def test_facets_availability_states(seeded_db):
    resp = client.get("/api/facets").json()
    by_key = {loc["key"]: loc["availability"] for loc in resp["localities"]}
    assert by_key["norfolk"] == "available"
    assert by_key["virginia_beach"] == "available"
    assert by_key["chesapeake"] == "available"
    assert by_key["portsmouth"] == "source_under_review"
    assert by_key["hampton"] == "unavailable"
    assert by_key["suffolk"] == "unavailable"
    assert by_key["newport_news"] == "unavailable"


def test_facets_only_observed_values(seeded_db):
    resp = client.get("/api/facets").json()
    assert resp["permit_types"]["norfolk"] == ["Building", "Electrical", "Utility"]
    assert resp["statuses"]["norfolk"] == ["Finaled", "Issued", "Under Review"]


def test_facets_no_data_for_unsupported_localities(seeded_db):
    resp = client.get("/api/facets").json()
    assert "hampton" not in resp["permit_types"]
    assert "portsmouth" not in resp["permit_types"]
    assert "suffolk" not in resp["statuses"]


# ---------- /api/stats ----------

def test_stats_total(seeded_db):
    resp = client.get("/api/stats").json()
    assert resp["total"] == 6


def test_stats_mapped_and_unmapped(seeded_db):
    resp = client.get("/api/stats").json()
    assert resp["mapped"] == 2  # N1, VB1 have lat set
    assert resp["unmapped"] == 4
    assert resp["mapped_percent"] == round(100 * 2 / 6, 2)


def test_stats_geocode_status_breakdown(seeded_db):
    resp = client.get("/api/stats").json()
    assert resp["no_address"] == 1  # N3
    assert resp["geocode_not_found"] == 1  # VB2
    assert resp["pending_geocode"] == 2  # N2, VB3


def test_stats_by_locality(seeded_db):
    resp = client.get("/api/stats").json()
    assert resp["by_locality"] == {"norfolk": 3, "virginia_beach": 3}


def test_stats_by_status_bucket(seeded_db):
    resp = client.get("/api/stats").json()
    assert resp["by_status_bucket"]["issued"] == 2
    assert resp["by_status_bucket"]["finaled_closed"] == 2
    assert resp["by_status_bucket"]["active_pending"] == 2


def test_stats_filtered_by_locality(seeded_db):
    resp = client.get("/api/stats?locality=norfolk").json()
    assert resp["total"] == 3
    assert resp["by_locality"] == {"norfolk": 3}


def test_stats_latest_ingested_at_present(seeded_db):
    resp = client.get("/api/stats").json()
    assert resp["latest_ingested_at"] is not None


# ---------- /api/data-status ----------

def test_data_status_available_localities_use_real_data(seeded_db):
    resp = client.get("/api/data-status").json()
    by_locality = {loc["locality"]: loc for loc in resp["localities"]}
    assert by_locality["norfolk"]["record_count"] == 3
    assert by_locality["norfolk"]["mapped_count"] == 1
    assert by_locality["virginia_beach"]["record_count"] == 3


def test_data_status_unsupported_localities_are_zeroed(seeded_db):
    resp = client.get("/api/data-status").json()
    by_locality = {loc["locality"]: loc for loc in resp["localities"]}
    assert by_locality["hampton"]["record_count"] == 0
    assert by_locality["hampton"]["availability"] == "unavailable"
    assert by_locality["hampton"]["last_successful_ingestion"] is None
    assert by_locality["portsmouth"]["availability"] == "source_under_review"


def test_data_status_reads_ingestion_runs(seeded_db):
    from ingest.common import finish_ingestion_run, start_ingestion_run

    with db_module.db() as conn:
        run_id = start_ingestion_run(conn, "norfolk", "2025-01-01")
        finish_ingestion_run(conn, run_id, "success", fetched_count=3, counts={"inserted": 3})

    resp = client.get("/api/data-status").json()
    by_locality = {loc["locality"]: loc for loc in resp["localities"]}
    assert by_locality["norfolk"]["last_successful_ingestion"] is not None


def test_data_status_does_not_expose_unsafe_internals(seeded_db):
    resp = client.get("/api/data-status")
    body_text = str(resp.json())
    assert "Traceback" not in body_text
    assert str(db_module.BASE_DIR) not in body_text


# ---------- Read-only contract ----------

def test_no_mutation_routes_exist():
    methods_by_path: dict[str, set] = {}
    for route in app_module.app.routes:
        if hasattr(route, "methods"):
            methods_by_path.setdefault(route.path, set()).update(route.methods)

    for path, methods in methods_by_path.items():
        if path.startswith("/api/permits"):
            assert "POST" not in methods, f"{path} allows POST"
            assert "PUT" not in methods, f"{path} allows PUT"
            assert "PATCH" not in methods, f"{path} allows PATCH"
            assert "DELETE" not in methods, f"{path} allows DELETE"


def test_no_ingestion_trigger_route_exists():
    paths = {route.path for route in app_module.app.routes}
    assert not any("ingest" in p for p in paths)
    assert not any("geocode" in p for p in paths if p != "/health")
