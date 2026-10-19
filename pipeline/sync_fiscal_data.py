#!/usr/bin/env python3
"""
sync_fiscal_data.py

Nightly ETL: copies parcel and assessment reference data from the FiscalMapIQ
database (Neon) into PermitIQ's reference_parcels / reference_assessments
(migration 008), which vw_planning_leads joins for zoning, land use and
assessed values. A copy, not a Foreign Data Wrapper, so PermitIQ keeps working
when Neon is unreachable.

Source schema (FiscalMapIQ alembic revision dbf3adb70c64):
  parcels      one row per (city_code, gpin, data_year)
  assessments  one row per (parcel_id, tax_year); keyed by parcel_id, not gpin
Mapping, one row per GPIN:
  reference_parcels      latest data_year; land_use <- land_use_code;
                         acreage <- acreage_stated, else acreage_computed
  reference_assessments  latest tax_year; market_value <- total_value (total
                         assessed value -- FiscalMapIQ has no separate market
                         value); improvement_value <- improvement_value (already
                         one combined figure; there is no dwelling/other split)

Atomicity: both tables are loaded in ONE local transaction. Rows stream from a
read-only server-side cursor on Neon into temporary staging tables (COPY),
then are upserted with ON CONFLICT (gpin) DO UPDATE. If the remote connection
drops, or anything else fails, the local transaction rolls back and both
tables stay exactly as they were. Rows that disappear upstream are not deleted
here (upsert only).

Configuration (environment or flags):
  FISCALMAP_DATABASE_URL  Neon DSN; the SQLAlchemy "postgresql+psycopg://" form
                          FiscalMapIQ uses is accepted
  DATABASE_URL            local PermitIQ DSN
  FISCALMAP_CITY_CODE     REQUIRED: the city_code value FiscalMapIQ uses for
                          Virginia Beach. Not guessed -- FiscalMapIQ never
                          hard-codes one.

Usage:
  python sync_fiscal_data.py --dry-run     # read Neon, report counts, write nothing
  python sync_fiscal_data.py
  python sync_fiscal_data.py --skip-assessments
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

try:  # optional: load .env for local runs
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BATCH_SIZE = 5000

# Latest data_year per GPIN. Geometry travels as hex EWKB so its SRID comes along.
REMOTE_PARCELS = """
    SELECT DISTINCT ON (gpin)
           gpin, encode(ST_AsEWKB(geom), 'hex'), zoning, land_use_code, property_class,
           COALESCE(acreage_stated, acreage_computed), data_year, parcel_id
    FROM parcels
    WHERE city_code = %s
    ORDER BY gpin, data_year DESC NULLS LAST, parcel_id DESC
"""

# Latest tax_year per GPIN (assessments reach a GPIN through parcel_id).
REMOTE_ASSESSMENTS = """
    SELECT DISTINCT ON (p.gpin)
           p.gpin, a.total_value, a.land_value, a.improvement_value, a.tax_year
    FROM assessments a
    JOIN parcels p ON p.parcel_id = a.parcel_id
    WHERE a.city_code = %s AND p.city_code = %s
    ORDER BY p.gpin, a.tax_year DESC, a.assessment_id DESC
"""

STAGE_PARCELS = """
    CREATE TEMP TABLE stage_parcels (
        gpin TEXT, geom_hex TEXT, zoning TEXT, land_use TEXT, property_class TEXT,
        acreage NUMERIC, data_year INTEGER, source_parcel_id BIGINT
    ) ON COMMIT DROP
"""
STAGE_ASSESSMENTS = """
    CREATE TEMP TABLE stage_assessments (
        gpin TEXT, market_value NUMERIC, land_value NUMERIC, improvement_value NUMERIC, tax_year INTEGER
    ) ON COMMIT DROP
"""

# geometry(MultiPolygon, 2284) rejects any other SRID, which aborts the whole
# sync -- a reprojection problem upstream must not load silently.
UPSERT_PARCELS = """
    INSERT INTO reference_parcels
        (gpin, geom, zoning, land_use, property_class, acreage, city_code, data_year, source_parcel_id, synced_at)
    SELECT gpin, ST_Multi(ST_GeomFromEWKB(decode(geom_hex, 'hex'))), zoning, land_use, property_class,
           acreage, %s, data_year, source_parcel_id, now()
    FROM stage_parcels
    ON CONFLICT (gpin) DO UPDATE SET
        geom = EXCLUDED.geom, zoning = EXCLUDED.zoning, land_use = EXCLUDED.land_use,
        property_class = EXCLUDED.property_class, acreage = EXCLUDED.acreage,
        city_code = EXCLUDED.city_code, data_year = EXCLUDED.data_year,
        source_parcel_id = EXCLUDED.source_parcel_id, synced_at = now()
"""
UPSERT_ASSESSMENTS = """
    INSERT INTO reference_assessments (gpin, market_value, land_value, improvement_value, tax_year, synced_at)
    SELECT gpin, market_value, land_value, improvement_value, tax_year, now()
    FROM stage_assessments
    ON CONFLICT (gpin) DO UPDATE SET
        market_value = EXCLUDED.market_value, land_value = EXCLUDED.land_value,
        improvement_value = EXCLUDED.improvement_value, tax_year = EXCLUDED.tax_year, synced_at = now()
"""


def libpq_dsn(url: str) -> str:
    """Accept SQLAlchemy URLs ('postgresql+psycopg://...') as well as plain libpq ones."""
    return re.sub(r"^postgres(?:ql)?\+[a-z0-9_]+://", "postgresql://", url)


def stream(remote, name: str, sql: str, params: tuple, local_cur, copy_sql: str, batch_size: int) -> int:
    """Server-side cursor on Neon -> COPY into a local staging table. Returns rows copied."""
    n = 0
    with remote.cursor(name=name) as rc:
        rc.itersize = batch_size
        rc.execute(sql, params)
        with local_cur.copy(copy_sql) as cp:
            for row in rc:
                cp.write_row(row)
                n += 1
    return n


def sync(remote, local, city_code: str, batch_size: int, skip_assessments: bool, stats: Counter) -> None:
    """Load both tables in one local transaction; any exception rolls it back."""
    with local.transaction():
        cur = local.cursor()
        cur.execute(STAGE_PARCELS)
        stats["parcels_read"] = stream(
            remote, "fiscal_parcels", REMOTE_PARCELS, (city_code,), cur,
            "COPY stage_parcels (gpin, geom_hex, zoning, land_use, property_class, acreage, data_year, "
            "source_parcel_id) FROM STDIN", batch_size)
        cur.execute(UPSERT_PARCELS, (city_code,))
        stats["parcels_upserted"] = cur.rowcount
        if not skip_assessments:
            cur.execute(STAGE_ASSESSMENTS)
            stats["assessments_read"] = stream(
                remote, "fiscal_assessments", REMOTE_ASSESSMENTS, (city_code, city_code), cur,
                "COPY stage_assessments (gpin, market_value, land_value, improvement_value, tax_year) FROM STDIN",
                batch_size)
            cur.execute(UPSERT_ASSESSMENTS)
            stats["assessments_upserted"] = cur.rowcount
        remote.rollback()  # end the read-only remote transaction; nothing was written there


def dry_run(remote, city_code: str, stats: Counter) -> None:
    with remote.cursor() as rc:
        rc.execute("SELECT count(DISTINCT gpin), count(*) FROM parcels WHERE city_code = %s", (city_code,))
        stats["remote_parcel_gpins"], stats["remote_parcel_rows"] = rc.fetchone()
        rc.execute("SELECT count(*) FROM assessments WHERE city_code = %s", (city_code,))
        stats["remote_assessment_rows"] = rc.fetchone()[0]
        rc.execute("SELECT DISTINCT ST_SRID(geom) FROM parcels WHERE city_code = %s LIMIT 5", (city_code,))
        stats["remote_srids"] = ",".join(str(r[0]) for r in rc.fetchall()) or "none"
        rc.execute("SELECT DISTINCT city_code FROM parcels ORDER BY 1 LIMIT 20")
        stats["remote_city_codes"] = ",".join(r[0] for r in rc.fetchall()) or "none"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync FiscalMapIQ parcels/assessments into PermitIQ reference tables")
    p.add_argument("--remote-dsn", default=os.getenv("FISCALMAP_DATABASE_URL"),
                   help="FiscalMapIQ (Neon) DSN (default: $FISCALMAP_DATABASE_URL)")
    p.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="PermitIQ DSN (default: $DATABASE_URL)")
    p.add_argument("--city-code", default=os.getenv("FISCALMAP_CITY_CODE"),
                   help="FiscalMapIQ city_code to sync (default: $FISCALMAP_CITY_CODE; required)")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Rows per server-side fetch")
    p.add_argument("--skip-assessments", action="store_true", help="Sync parcels only")
    p.add_argument("--dry-run", action="store_true", help="Read Neon and report counts; write nothing")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.remote_dsn:
        print("[FATAL] no FiscalMapIQ DSN: set FISCALMAP_DATABASE_URL or pass --remote-dsn", file=sys.stderr)
        return 1
    if not args.city_code:
        print("[FATAL] no city code: set FISCALMAP_CITY_CODE or pass --city-code (the city_code value "
              "FiscalMapIQ uses; --dry-run lists the ones present)", file=sys.stderr)
        if not args.dry_run:
            return 1
    if not args.dry_run and not args.dsn:
        print("[FATAL] no PermitIQ DSN: set DATABASE_URL or pass --dsn", file=sys.stderr)
        return 1
    import psycopg

    stats: Counter = Counter()
    try:
        remote = psycopg.connect(libpq_dsn(args.remote_dsn), connect_timeout=20)
    except psycopg.Error as e:
        print(f"[FATAL] cannot connect to FiscalMapIQ: {e}", file=sys.stderr)
        return 1
    remote.read_only = True  # this script never writes to Neon
    local = None
    try:
        if args.dry_run:
            dry_run(remote, args.city_code or "", stats)
        else:
            local = psycopg.connect(args.dsn)
            print(f"Syncing city_code={args.city_code!r} from FiscalMapIQ")
            sync(remote, local, args.city_code, args.batch_size, args.skip_assessments, stats)
            if stats["parcels_read"] == 0:
                print(f"[warn] FiscalMapIQ returned no parcels for city_code={args.city_code!r} "
                      "(wrong code, or parcels not loaded yet); nothing changed", file=sys.stderr)
    except Exception as e:  # remote drop, SRID mismatch, schema drift ...
        print(f"[FATAL] sync failed; local transaction rolled back, reference tables unchanged: {e}",
              file=sys.stderr)
        if "reference_parcels" in str(e) or "reference_assessments" in str(e):
            print("  hint: apply db/008_fiscal_enrichment.sql", file=sys.stderr)
        return 1
    finally:
        remote.close()
        if local is not None:
            local.close()

    print("Done." if not args.dry_run else "Dry run (nothing written).")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
