"""Tests for scripts/audit_permits.py -- fixture data in a temp DB, no network."""

import importlib.util
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db as db_module  # noqa: E402
from ingest.common import NormalizedPermit, upsert_permits  # noqa: E402

_spec = importlib.util.spec_from_file_location("audit_permits", ROOT / "scripts" / "audit_permits.py")
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

COMMERCIAL = audit.DEFAULT_COMMERCIAL_TERMS
STRUCTURAL = audit.DEFAULT_STRUCTURAL_TERMS


# ------------------------------------------------------------ classify_name


@pytest.mark.parametrize("name", [
    "ABC ROOFING", "Tidewater Builders", "Smith Construction LLC", "ACME, INC.",
    "Coastal Fence Co", "Hampton Roads Demolition Corp", "XYZ L.L.C.",
])
def test_classify_name_business(name):
    assert audit.classify_name(name) == "business"


@pytest.mark.parametrize("name", ["John Smith", "MARY COLLINS", "Jane Q. Public", "Inca Jones"])
def test_classify_name_individual(name):
    # "CO" inside COLLINS and "INC" inside "Inca" must not count as business tokens
    assert audit.classify_name(name) == "individual"


@pytest.mark.parametrize("name", [None, "", "   "])
def test_classify_name_blank(name):
    assert audit.classify_name(name) is None


# ------------------------------------------------------------ qualify


def test_qualify_commercial_from_norfolk_raw_use_class():
    row = {"permit_type": "Building", "description": "New", "valuation": None,
           "raw": {"use_class": "Commercial"}}
    assert audit.qualify(row, COMMERCIAL, STRUCTURAL, 50000) == ["commercial"]


def test_qualify_demolition_from_description():
    row = {"permit_type": "Building", "description": "Demolish single family dwelling",
           "valuation": None, "raw": {}}
    assert audit.qualify(row, COMMERCIAL, STRUCTURAL, 50000) == ["demo_structural"]


def test_qualify_valuation_threshold_is_inclusive_and_configurable():
    row = {"permit_type": "Electrical", "description": "panel", "valuation": 50000.0, "raw": {}}
    assert audit.qualify(row, COMMERCIAL, STRUCTURAL, 50000) == ["valuation"]
    assert audit.qualify(row, COMMERCIAL, STRUCTURAL, 50001) == []


def test_qualify_residential_minor_work_does_not_qualify():
    row = {"permit_type": "Electrical", "description": "replace outlet", "valuation": None,
           "raw": {"use_class": "Residential", "work_type": "Alteration"}}
    assert audit.qualify(row, COMMERCIAL, STRUCTURAL, 50000) == []


def test_qualify_term_needs_left_word_boundary():
    row = {"permit_type": "Sign", "description": "noncommercial banner", "valuation": None, "raw": {}}
    assert audit.qualify(row, COMMERCIAL, STRUCTURAL, 50000) == []


# ------------------------------------------------------------ helpers


def test_week_start_is_monday():
    assert audit.week_start(date(2026, 10, 15)) == date(2026, 10, 12)  # Thu -> Mon
    assert audit.week_start(date(2026, 10, 12)) == date(2026, 10, 12)


def test_summarize_lags():
    s = audit.summarize_lags([0, 1, 2, 3, 10])
    assert s["n"] == 5 and s["median"] == 2 and s["min"] == 0 and s["max"] == 10
    assert audit.summarize_lags([])["median"] is None


@pytest.mark.parametrize("daily,expected", [
    ({"weekdays": 10, "days_with_match": 9, "median_per_weekday": 4, "mean_per_weekday": 4.2,
      "max_per_weekday": 9}, "DAILY"),
    ({"weekdays": 10, "days_with_match": 4, "median_per_weekday": 0, "mean_per_weekday": 1.5,
      "max_per_weekday": 5}, "WEEKLY"),
    ({"weekdays": 10, "days_with_match": 1, "median_per_weekday": 0, "mean_per_weekday": 0.1,
      "max_per_weekday": 1}, "BROADEN"),
])
def test_viability_hint(daily, expected):
    assert audit.viability_hint(daily).startswith(expected)


# ------------------------------------------------------------ end to end


def _permit(locality, pid, **kw):
    return NormalizedPermit(locality=locality, source_permit_id=pid, source_name="test", **kw)


@pytest.fixture
def populated_db(tmp_path):
    path = tmp_path / "permits.db"
    db_module.init_db(path)
    permits = [
        # Norfolk: commercial, issued in window, geocoded
        _permit("norfolk", "N1", permit_type="Building", description="New", applied_date="2026-10-01",
                issued_date="2026-10-06", lat=36.85, lng=-76.29, raw={"use_class": "Commercial"}),
        # Norfolk: residential, not qualifying
        _permit("norfolk", "N2", permit_type="Electrical", description="Alteration",
                applied_date="2026-10-02", raw={"use_class": "Residential"}),
        # VB: demolition, issued, with a business contractor and a cost
        _permit("virginia_beach", "V1", permit_type="Building",
                description="Demolition of commercial structure", applied_date="2026-10-05",
                issued_date="2026-10-07", contractor="ABC Demolition LLC", valuation=120000.0),
        # VB: individual applicant, not qualifying
        _permit("virginia_beach", "V2", permit_type="Plumbing", description="water heater",
                applied_date="2026-10-06", issued_date="2026-10-06", applicant="John Smith"),
        # Outside the window entirely
        _permit("virginia_beach", "V3", permit_type="Building", description="demolition",
                applied_date="2025-01-01", issued_date="2025-01-02"),
    ]
    with db_module.db(path) as conn:
        upsert_permits(conn, permits)
        # V3 is the VB backfill (first ingest 2026-09-30); N1/V1 seen a day after issue
        first_seen = {"N1": "2026-10-07", "N2": "2026-10-03", "V1": "2026-10-08",
                      "V2": "2026-10-07", "V3": "2026-09-30"}
        for pid, day in first_seen.items():
            conn.execute("UPDATE permits SET first_seen_at = ? WHERE source_permit_id = ?",
                         (f"{day}T11:00:00Z", pid))
        for loc, day in [("norfolk", "2026-10-06"), ("norfolk", "2026-10-07"), ("virginia_beach", "2026-10-07")]:
            conn.execute("INSERT INTO ingestion_runs (locality, started_at, status) VALUES (?, ?, 'success')",
                         (loc, f"{day}T10:00:00Z"))
        conn.execute("INSERT INTO ingestion_runs (locality, started_at, status) VALUES "
                     "('virginia_beach', '2026-10-08T10:00:00Z', 'failed')")
    return path


def _run(populated_db, tmp_path, *extra):
    return audit.main(["--db", str(populated_db), "--as-of", "2026-10-15", "--days", "90",
                       "--out-dir", str(tmp_path / "reports"), *extra])


def test_end_to_end_counts(populated_db, tmp_path, capsys):
    report = _run(populated_db, tmp_path)

    assert report["localities"] == ["norfolk", "virginia_beach"]
    assert report["in_window_count"] == 4  # V3 is outside the 90-day window

    comp = report["completeness"]
    assert comp["norfolk"]["qualifying"] == 1
    assert comp["norfolk"]["geocoded"] == 1
    assert comp["norfolk"]["contractor_or_applicant"] == 0
    assert comp["virginia_beach"]["qualifying"] == 1
    assert comp["virginia_beach"]["valuation"] == 1
    assert comp["virginia_beach"]["contractor"] == 1

    assert report["reason_counts"]["virginia_beach"]["demo_structural"] == 1
    assert report["reason_counts"]["virginia_beach"]["commercial"] == 1
    assert report["reason_counts"]["virginia_beach"]["valuation"] == 1

    names = report["names"]["virginia_beach"]
    assert names[("contractor", "business")] == 1
    assert names[("applicant", "individual")] == 1

    # qualifying issued: N1 on Tue 10-06, V1 on Wed 10-07
    daily_all = report["daily"]["ALL"]
    assert daily_all["days_with_match"] == 2
    assert daily_all["max_per_weekday"] == 1

    f = report["freshness"]["norfolk"]
    assert f["issued_to_first_seen"]["median"] == 1
    assert f["applied_to_issued"]["median"] == 5
    # norfolk's first ingest is 2026-10-03: N2 (applied 10-02, never issued) is
    # backfill and excluded; N1 (issued 10-06) still counts toward issue lag
    assert f["excluded_backfill"] == 1
    assert f["applied_to_first_seen"]["n"] == 0
    assert report["freshness"]["virginia_beach"]["issued_to_first_seen"]["median"] == 1

    runs = report["runs"]["virginia_beach"]
    assert runs["success"] == 1 and runs["failed"] == 1
    assert report["runs"]["norfolk"]["median_gap_days"] == 1

    out = capsys.readouterr().out
    assert "Field completeness" in out and "Viability hint" in out


def test_min_valuation_arg_changes_qualification(populated_db, tmp_path):
    # With no commercial/structural terms, only the valuation rule remains.
    report = _run(populated_db, tmp_path, "--commercial-terms", "", "--structural-terms", "",
                  "--min-valuation", "200000")
    assert report["completeness"]["virginia_beach"]["qualifying"] == 0
    report = _run(populated_db, tmp_path, "--commercial-terms", "", "--structural-terms", "",
                  "--min-valuation", "100000")
    assert report["completeness"]["virginia_beach"]["qualifying"] == 1


def test_writes_csvs(populated_db, tmp_path):
    _run(populated_db, tmp_path)
    files = {p.name for p in (tmp_path / "reports").glob("*.csv")}
    for section in ("weekly_totals", "weekly_by_type", "daily_digest_sim", "completeness",
                    "names", "freshness", "ingestion_cadence", "sources", "raw_candidate_keys"):
        assert f"audit_2026-10-15_{section}.csv" in files
    text = (tmp_path / "reports" / "audit_2026-10-15_completeness.csv").read_text()
    assert text.startswith("locality,qualifying,valuation")


def test_opens_database_read_only(populated_db):
    conn = audit.connect_readonly(populated_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM permits")
    finally:
        conn.close()


def test_missing_database_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        audit.connect_readonly(tmp_path / "nope.db")
