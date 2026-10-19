"""Migration 008 + sync_fiscal_data.py, against a local stand-in for the
FiscalMapIQ (Neon) database. The stand-in's parcels/assessments DDL mirrors
FiscalMapIQ alembic revision dbf3adb70c64 (columns and keys; provenance FKs to
source_registry / methodology_versions omitted). All data is synthetic.
Skipped unless PERMITIQ_TEST_ADMIN_DSN is set (see conftest.py)."""
from __future__ import annotations

import uuid
from collections import Counter
from decimal import Decimal

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

import sync_fiscal_data as sfd
from conftest import ROOT, create_db, drop_db, dsn_for, q, run_sql_file

MIGRATION_008 = ROOT / "db" / "008_fiscal_enrichment.sql"
CITY = "TESTCITY"

FISCALMAP_DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE parcels (
    parcel_id BIGINT PRIMARY KEY, city_code TEXT NOT NULL, gpin TEXT NOT NULL,
    geom geometry(MultiPolygon, 2284) NOT NULL, acreage_computed NUMERIC(12,4), acreage_stated NUMERIC(12,4),
    zoning TEXT, land_use_code TEXT, property_class TEXT, tax_district TEXT, exempt_source TEXT,
    source_id INTEGER, data_year INTEGER, loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(), methodology_version_id INTEGER,
    CONSTRAINT uq_parcels_city_gpin_year UNIQUE (city_code, gpin, data_year));
CREATE TABLE assessments (
    assessment_id BIGINT PRIMARY KEY, parcel_id BIGINT NOT NULL REFERENCES parcels(parcel_id),
    city_code TEXT NOT NULL, tax_year INTEGER NOT NULL, land_value NUMERIC(14,2), improvement_value NUMERIC(14,2),
    total_value NUMERIC(14,2), source_id INTEGER, data_year INTEGER, loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    methodology_version_id INTEGER, CONSTRAINT uq_assessments_parcel_year UNIQUE (parcel_id, tax_year));
"""
SQ = "ST_Multi(ST_GeomFromText('POLYGON((12000000 3500000, 12000100 3500000, 12000100 3500100, 12000000 3500100, 12000000 3500000))', 2284))"


@pytest.fixture
def remote(template_db):
    """Stand-in FiscalMapIQ database with synthetic parcels/assessments."""
    name = f"permitiq_fm_{uuid.uuid4().hex[:8]}"
    create_db(name)
    dsn = dsn_for(name)
    with psycopg.connect(dsn) as c:
        c.execute(FISCALMAP_DDL)
        c.execute(f"""
            INSERT INTO parcels (parcel_id, city_code, gpin, geom, acreage_stated, acreage_computed, zoning,
                                 land_use_code, property_class, data_year) VALUES
              (1, %(c)s, '14943449190000', {SQ}, 2.5, 2.4, 'B-2', 'COMM', 'C', 2025),
              (2, %(c)s, '14943449190000', {SQ}, 2.5, 2.4, 'B-4', 'MIXED', 'C', 2026),     -- newer year wins
              (3, %(c)s, '14895000010001', {SQ}, NULL, 0.1, 'A-12', 'CONDO', 'R', 2026),  -- two units share
              (4, %(c)s, '14895000010002', {SQ}, NULL, 0.1, 'A-12', 'CONDO', 'R', 2026),  --   one 10-digit base
              (5, 'OTHERCITY', '99990000000000', {SQ}, 1, 1, 'X', 'X', 'X', 2026)        -- other city: skipped
        """, {"c": CITY})
        c.execute("""
            INSERT INTO assessments (assessment_id, parcel_id, city_code, tax_year, land_value, improvement_value,
                                     total_value) VALUES
              (10, 1, %(c)s, 2025, 400000, 600000, 1000000),
              (11, 2, %(c)s, 2026, 450000, 700000, 1150000)                               -- newer tax year wins
        """, {"c": CITY})
    try:
        yield dsn
    finally:
        drop_db(name)


@pytest.fixture
def local(db):
    run_sql_file(conninfo_to_dict(db)["dbname"], MIGRATION_008)
    return db


def run_sync(remote_dsn, local_dsn, monkeypatch, *extra):
    monkeypatch.setattr("sys.argv", ["sync_fiscal_data.py", "--remote-dsn", remote_dsn, "--dsn", local_dsn,
                                     "--city-code", CITY, *extra])
    return sfd.main()


def test_migration_008_keeps_007_columns_then_appends(local):
    cols = [r[0] for r in q(local, "SELECT column_name FROM information_schema.columns "
                                   "WHERE table_name = 'vw_planning_leads' ORDER BY ordinal_position")]
    assert cols[-6:] == ["gpin", "zoning", "land_use", "market_value", "land_value", "reference_parcel_gpin"]
    assert cols.index("gpin") == 16  # 007's 17 columns are still first, in order
    assert len(q(local, "SELECT * FROM vw_planning_leads")) == 2  # pre-existing rows unchanged, no dupes
    name = conninfo_to_dict(local)["dbname"]
    run_sql_file(name, MIGRATION_008)  # idempotent


def test_sync_latest_rows_and_mapping(remote, local, monkeypatch):
    assert run_sync(remote, local, monkeypatch) == 0
    rows = dict((r[0], r[1:]) for r in q(local, "SELECT gpin, zoning, land_use, acreage, data_year, ST_SRID(geom) "
                                                "FROM reference_parcels"))
    assert set(rows) == {"14943449190000", "14895000010001", "14895000010002"}  # other city excluded
    assert rows["14943449190000"] == ("B-4", "MIXED", Decimal("2.5000"), 2026, 2284)  # latest data_year
    assert rows["14895000010001"][2] == Decimal("0.1000")  # acreage falls back to computed
    assert q(local, "SELECT market_value, land_value, improvement_value, tax_year FROM reference_assessments") == [
        (Decimal("1150000.00"), Decimal("450000.00"), Decimal("700000.00"), 2026)]  # latest tax_year


def test_sync_is_idempotent_and_updates(remote, local, monkeypatch):
    assert run_sync(remote, local, monkeypatch) == 0
    assert run_sync(remote, local, monkeypatch) == 0
    assert q(local, "SELECT count(*) FROM reference_parcels") == [(3,)]
    with psycopg.connect(remote) as c:
        c.execute("UPDATE parcels SET zoning = 'B-4 (SD)' WHERE parcel_id = 2")
    assert run_sync(remote, local, monkeypatch) == 0
    assert q(local, "SELECT zoning FROM reference_parcels WHERE gpin = '14943449190000'") == [("B-4 (SD)",)]


def test_remote_drop_rolls_back_everything(remote, local, monkeypatch):
    assert run_sync(remote, local, monkeypatch) == 0
    before = q(local, "SELECT gpin, zoning, synced_at FROM reference_parcels ORDER BY 1")
    with psycopg.connect(remote) as c:
        c.execute("UPDATE parcels SET zoning = 'CHANGED'")
    real_stream = sfd.stream

    def dropping_stream(remote_conn, name, *a, **k):
        if name == "fiscal_assessments":  # parcels already staged + upserted in this transaction
            remote_conn.close()           # the remote connection goes away mid-sync
        return real_stream(remote_conn, name, *a, **k)

    monkeypatch.setattr(sfd, "stream", dropping_stream)
    assert run_sync(remote, local, monkeypatch) == 1
    assert q(local, "SELECT gpin, zoning, synced_at FROM reference_parcels ORDER BY 1") == before


def test_srid_mismatch_aborts(remote, local, monkeypatch):
    with psycopg.connect(remote) as c:
        c.execute("ALTER TABLE parcels ALTER COLUMN geom TYPE geometry(MultiPolygon, 4326) USING ST_Transform(geom, 4326)")
    assert run_sync(remote, local, monkeypatch) == 1
    assert q(local, "SELECT count(*) FROM reference_parcels") == [(0,)]


def test_view_enrichment_matches_only_unambiguous_parcels(remote, local, monkeypatch, ingest):
    assert run_sync(remote, local, monkeypatch) == 0
    base = {"project_name": None, "address": None, "application_type": None, "proposed_use": None,
            "hearing_date": "2026-07-14", "status": "I. PLANNING"}
    recs = [
        {**base, "application_id": "T-DASHED", "project_name": "Dashed 10-digit", "gpin": "1494-34-4919"},
        {**base, "application_id": "T-FULL", "project_name": "Exact 14-digit", "gpin": "14943449190000"},
        {**base, "application_id": "T-CONDO", "project_name": "Ambiguous base", "gpin": "1489500001"},
        {**base, "application_id": "T-MULTI", "project_name": "Multi-GPIN", "gpin": "1494344919, 1489500001"},
        {**base, "application_id": "T-NONE", "project_name": "No GPIN", "gpin": None},
    ]
    assert ingest(local, recs, "--max-geocode", "0") == 0
    got = {r[0]: r[1:] for r in q(local, "SELECT application_id, zoning, land_use, market_value, land_value, "
                                         "reference_parcel_gpin FROM vw_planning_leads WHERE application_id LIKE 'T-%%'")}
    hit = ("B-4", "MIXED", Decimal("1150000.00"), Decimal("450000.00"), "14943449190000")
    assert got["T-DASHED"] == hit and got["T-FULL"] == hit
    for miss in ("T-CONDO", "T-MULTI", "T-NONE"):
        assert got[miss] == (None, None, None, None, None)
    assert q(local, "SELECT count(*) FROM vw_planning_leads WHERE application_id LIKE 'T-%%'") == [(5,)]  # no dupes


def test_powerbi_reader_can_read_reference_tables(remote, local, monkeypatch):
    assert run_sync(remote, local, monkeypatch) == 0
    with psycopg.connect(local) as c:
        c.execute("SET ROLE powerbi_reader")
        assert c.execute("SELECT count(*) FROM reference_parcels").fetchone() == (3,)
        assert c.execute("SELECT count(*) FROM reference_assessments").fetchone() == (1,)


def test_requires_city_code_and_accepts_sqlalchemy_urls(monkeypatch):
    monkeypatch.setattr("sys.argv", ["sync_fiscal_data.py", "--remote-dsn", "postgresql://x/y", "--dsn", "postgresql://x/z"])
    monkeypatch.delenv("FISCALMAP_CITY_CODE", raising=False)
    assert sfd.main() == 1
    assert sfd.libpq_dsn("postgresql+psycopg://u:p@h/db?sslmode=require") == "postgresql://u:p@h/db?sslmode=require"
    assert sfd.libpq_dsn("postgresql://u@h/db") == "postgresql://u@h/db"
