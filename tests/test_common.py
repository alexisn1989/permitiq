"""Phase 2 tests: NormalizedPermit validation, date normalization, upsert
coordinate precedence, duplicate detection, required-field validation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest.common import (  # noqa: E402
    NormalizedPermit,
    SourceSchemaError,
    detect_duplicate_source_ids,
    finish_ingestion_run,
    normalize_date,
    start_ingestion_run,
    upsert_permits,
    validate_required_fields,
)


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    db_module.init_db(db_path)
    with db_module.db(db_path) as c:
        yield c


def make_permit(**overrides) -> NormalizedPermit:
    defaults = dict(
        locality="norfolk",
        source_permit_id="F25-00156",
        source_name="Norfolk Open Data",
        address="243 W Bute St, Norfolk, VA",
        permit_type="Fire Protection",
        status="Issued",
        status_bucket="issued",
        applied_date="2025-06-06",
    )
    defaults.update(overrides)
    return NormalizedPermit(**defaults)


# ---------- normalize_date ----------

def test_normalize_date_iso_with_fraction():
    assert normalize_date("2025-06-06T00:00:00.000") == "2025-06-06"


def test_normalize_date_iso_plain():
    assert normalize_date("2025-06-06T00:00:00") == "2025-06-06"


def test_normalize_date_slash_format():
    assert normalize_date("2023/01/15") == "2023-01-15"


def test_normalize_date_already_iso():
    assert normalize_date("2023-01-15") == "2023-01-15"


def test_normalize_date_epoch_millis():
    # 2023-01-15T00:00:00Z in epoch milliseconds
    assert normalize_date(1673740800000) == "2023-01-15"


def test_normalize_date_none_and_empty():
    assert normalize_date(None) is None
    assert normalize_date("") is None


def test_normalize_date_garbage():
    assert normalize_date("not a date") is None


def test_normalize_date_bool_rejected():
    # bool is an int subclass in Python -- must not be treated as epoch millis
    assert normalize_date(True) is None


# ---------- NormalizedPermit validation ----------

def test_valid_permit_has_no_errors():
    assert make_permit().validation_errors() == []


def test_missing_locality_is_error():
    errors = make_permit(locality="").validation_errors()
    assert any("locality" in e for e in errors)


def test_missing_source_permit_id_is_error():
    errors = make_permit(source_permit_id="").validation_errors()
    assert any("source_permit_id" in e for e in errors)


def test_invalid_status_bucket_is_error():
    errors = make_permit(status_bucket="not_a_real_bucket").validation_errors()
    assert any("status_bucket" in e for e in errors)


def test_lat_without_lng_is_error():
    errors = make_permit(lat=36.85).validation_errors()
    assert any("lat and lng" in e for e in errors)


def test_lat_lng_out_of_range_is_error():
    errors = make_permit(lat=200.0, lng=-76.0, coordinate_source="native").validation_errors()
    assert any("out of range" in e for e in errors)


def test_valid_lat_lng_no_error():
    errors = make_permit(lat=36.85, lng=-76.28, coordinate_source="native").validation_errors()
    assert errors == []


def test_bad_date_format_is_error():
    errors = make_permit(issued_date="06/06/2025").validation_errors()
    assert any("issued_date" in e for e in errors)


def test_non_serializable_raw_is_error():
    errors = make_permit(raw={"bad": object()}).validation_errors()
    assert any("raw" in e for e in errors)


# ---------- validate_required_fields / SourceSchemaError ----------

def test_validate_required_fields_passes_when_present():
    validate_required_fields({"a": 1, "b": None}, {"a", "b"}, "test source")


def test_validate_required_fields_raises_when_missing():
    with pytest.raises(SourceSchemaError):
        validate_required_fields({"a": 1}, {"a", "b"}, "test source")


# ---------- detect_duplicate_source_ids ----------

def test_no_duplicates_returns_empty():
    records = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    assert detect_duplicate_source_ids(records, "id", "test") == []


def test_duplicate_detected_both_copies_returned():
    records = [{"id": "1", "n": "first"}, {"id": "2"}, {"id": "1", "n": "second"}]
    dupes = detect_duplicate_source_ids(records, "id", "test")
    assert len(dupes) == 2
    assert {d["n"] for d in dupes} == {"first", "second"}


def test_triple_duplicate_not_double_counted_for_first():
    records = [{"id": "1"}, {"id": "1"}, {"id": "1"}]
    dupes = detect_duplicate_source_ids(records, "id", "test")
    assert len(dupes) == 3


# ---------- upsert_permits: insert / update / unchanged ----------

def test_upsert_inserts_new_permit(conn):
    counts = upsert_permits(conn, [make_permit()])
    assert counts == {"inserted": 1, "updated": 0, "unchanged": 0, "skipped": 0}
    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='F25-00156'").fetchone()
    assert row["locality"] == "norfolk"
    assert row["first_seen_at"] == row["ingested_at"]


def test_upsert_second_identical_call_is_unchanged(conn):
    upsert_permits(conn, [make_permit()])
    counts = upsert_permits(conn, [make_permit()])
    assert counts == {"inserted": 0, "updated": 0, "unchanged": 1, "skipped": 0}


def test_upsert_changed_field_counts_as_updated(conn):
    upsert_permits(conn, [make_permit(status="Issued")])
    counts = upsert_permits(conn, [make_permit(status="Finaled", status_bucket="finaled_closed")])
    assert counts == {"inserted": 0, "updated": 1, "unchanged": 0, "skipped": 0}
    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='F25-00156'").fetchone()
    assert row["status"] == "Finaled"


def test_upsert_preserves_first_seen_at_across_updates(conn):
    upsert_permits(conn, [make_permit(status="Issued")])
    first_row = conn.execute("SELECT first_seen_at FROM permits").fetchone()
    upsert_permits(conn, [make_permit(status="Finaled", status_bucket="finaled_closed")])
    second_row = conn.execute("SELECT first_seen_at FROM permits").fetchone()
    assert first_row["first_seen_at"] == second_row["first_seen_at"]


def test_upsert_invalid_permit_is_skipped(conn):
    counts = upsert_permits(conn, [make_permit(locality="")])
    assert counts["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 0


def test_upsert_in_batch_duplicate_is_skipped(conn):
    counts = upsert_permits(conn, [make_permit(), make_permit()])
    assert counts == {"inserted": 1, "updated": 0, "unchanged": 0, "skipped": 1}


def test_upsert_different_localities_same_source_id_both_insert(conn):
    counts = upsert_permits(
        conn,
        [make_permit(locality="norfolk"), make_permit(locality="virginia_beach")],
    )
    assert counts["inserted"] == 2


# ---------- upsert_permits: coordinate precedence ----------

def test_native_coordinates_accepted_on_insert(conn):
    upsert_permits(conn, [make_permit(lat=36.85, lng=-76.28, coordinate_source="native")])
    row = conn.execute("SELECT * FROM permits").fetchone()
    assert row["lat"] == 36.85
    assert row["coordinate_source"] == "native"


def test_geocoded_coordinates_not_erased_by_reingest_without_coords(conn):
    upsert_permits(conn, [make_permit(lat=36.85, lng=-76.28, coordinate_source="census")])
    # re-ingest with no coordinates at all (adapter has no native geometry)
    counts = upsert_permits(conn, [make_permit(status="Finaled", status_bucket="finaled_closed")])
    row = conn.execute("SELECT * FROM permits").fetchone()
    assert row["lat"] == 36.85
    assert row["coordinate_source"] == "census"
    assert row["status"] == "Finaled"
    assert counts["updated"] == 1  # status changed, coords preserved


def test_native_overwrites_lower_precedence_nominatim(conn):
    upsert_permits(conn, [make_permit(lat=1.0, lng=2.0, coordinate_source="nominatim")])
    upsert_permits(conn, [make_permit(lat=36.85, lng=-76.28, coordinate_source="native")])
    row = conn.execute("SELECT * FROM permits").fetchone()
    assert row["lat"] == 36.85
    assert row["coordinate_source"] == "native"


def test_nominatim_does_not_overwrite_existing_native(conn):
    upsert_permits(conn, [make_permit(lat=36.85, lng=-76.28, coordinate_source="native")])
    upsert_permits(conn, [make_permit(lat=1.0, lng=2.0, coordinate_source="nominatim")])
    row = conn.execute("SELECT * FROM permits").fetchone()
    assert row["lat"] == 36.85
    assert row["coordinate_source"] == "native"


def test_manual_coordinate_never_overwritten(conn):
    upsert_permits(conn, [make_permit(lat=36.85, lng=-76.28, coordinate_source="manual")])
    # simulate a re-ingest that would otherwise bring "better" native coordinates
    upsert_permits(conn, [make_permit(lat=36.90, lng=-76.30, coordinate_source="native", status="Finaled", status_bucket="finaled_closed")])
    row = conn.execute("SELECT * FROM permits").fetchone()
    assert row["lat"] == 36.85
    assert row["coordinate_source"] == "manual"
    assert row["status"] == "Finaled"  # non-coordinate fields still update normally


def test_census_overwrites_nominatim_same_or_higher_precedence(conn):
    upsert_permits(conn, [make_permit(lat=1.0, lng=2.0, coordinate_source="nominatim")])
    upsert_permits(conn, [make_permit(lat=36.85, lng=-76.28, coordinate_source="census")])
    row = conn.execute("SELECT * FROM permits").fetchone()
    assert row["lat"] == 36.85
    assert row["coordinate_source"] == "census"


# ---------- ingestion_runs tracking ----------

def test_start_and_finish_ingestion_run(conn):
    run_id = start_ingestion_run(conn, "norfolk", "2024-01-01")
    row = conn.execute("SELECT * FROM ingestion_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "running"
    assert row["since_date"] == "2024-01-01"

    finish_ingestion_run(
        conn, run_id, "success",
        fetched_count=10,
        counts={"inserted": 8, "updated": 1, "unchanged": 1, "skipped": 0},
        geocoded_count=8,
    )
    row = conn.execute("SELECT * FROM ingestion_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "success"
    assert row["fetched_count"] == 10
    assert row["inserted_count"] == 8
    assert row["completed_at"] is not None


def test_finish_ingestion_run_records_failure(conn):
    run_id = start_ingestion_run(conn, "chesapeake", None)
    finish_ingestion_run(conn, run_id, "failed", error_count=1, error_message="SourceUnavailableError: ...")
    row = conn.execute("SELECT * FROM ingestion_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert "SourceUnavailableError" in row["error_message"]
