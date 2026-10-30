"""Copy Virginia Beach parcels + assessments from the FiscalMapIQ Neon
database into permits.db (schema v2 tables `parcels` and `assessments`).

Neon is read-only here: ingest/neon_source.py only sends SELECTs. SQLite is
upserted by primary key, so re-running is idempotent and reports unchanged
rows rather than rewriting them. Each table load is recorded in
ingestion_runs under its own locality key (RUN_LOCALITY) so it never shows
up as a permit sync in /api/data-status.

Requires NEON_DATABASE_URL (the Neon connection string).

Usage:
  python scripts/load_neon_assessments.py --dry-run --limit 100
  python scripts/load_neon_assessments.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import neon_source  # noqa: E402
from ingest.common import finish_ingestion_run, start_ingestion_run  # noqa: E402

RUN_LOCALITY = "neon_parcels_vb"

# Parcels first: assessments reference parcel_id.
LOAD_ORDER = ("parcels", "assessments")


def upsert_rows(conn: sqlite3.Connection, table: str, rows: Iterable[dict]) -> dict:
    """Insert new rows, update rows whose values changed, leave the rest.
    Returns {"inserted", "updated", "unchanged", "skipped"} like
    ingest.common.upsert_permits (skipped = in-batch duplicate keys)."""
    columns, key = neon_source.TABLES[table]
    dest_cols = [dest for dest, _ in columns.values()]
    non_key = [c for c in dest_cols if c != key]
    all_cols = [*dest_cols, "ingested_at"]

    sql = (
        f"INSERT INTO {table} ({', '.join(all_cols)}) "
        f"VALUES ({', '.join(':' + c for c in all_cols)}) "
        f"ON CONFLICT({key}) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in [*non_key, "ingested_at"])
        + " WHERE " + " OR ".join(f"{table}.{c} IS NOT excluded.{c}" for c in non_key)
    )

    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    seen: set = set()
    ts = db_module.now_iso()
    for row in rows:
        if row[key] in seen:
            counts["skipped"] += 1
            continue
        seen.add(row[key])

        existing = conn.execute(f"SELECT 1 FROM {table} WHERE {key} = ?", (row[key],)).fetchone()
        cur = conn.execute(sql, {**row, "ingested_at": ts})
        if existing is None:
            counts["inserted"] += 1
        elif cur.rowcount > 0:
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
    return counts


def load_table(table: str, limit: int | None, dry_run: bool, conn_str: str) -> None:
    print(f"\n=== {table} ===")
    if dry_run:
        rows = list(neon_source.fetch_table(table, conn_str=conn_str, limit=limit))
        print(f"  fetched {len(rows)} rows")
        for r in rows[:3]:
            print(f"    sample: {r}")
        print("  --dry-run: no changes written")
        return

    with db_module.db() as conn:
        run_id = start_ingestion_run(conn, RUN_LOCALITY, None)

    try:
        rows = list(neon_source.fetch_table(table, conn_str=conn_str, limit=limit))
        with db_module.db() as conn:
            counts = upsert_rows(conn, table, rows)
    except Exception as exc:  # noqa: BLE001 - record and re-raise so the CLI exits non-zero
        print(f"  FAILED: {exc}")
        with db_module.db() as conn:
            finish_ingestion_run(conn, run_id, "failed", error_message=f"{table}: {exc}")
        raise

    print(
        f"  fetched={len(rows)} inserted={counts['inserted']} updated={counts['updated']} "
        f"unchanged={counts['unchanged']} skipped={counts['skipped']}"
    )
    with db_module.db() as conn:
        finish_ingestion_run(conn, run_id, "success", fetched_count=len(rows), counts=counts)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Load VB parcels + assessments from Neon")
    parser.add_argument("--table", choices=list(LOAD_ORDER) + ["all"], default="all")
    parser.add_argument("--limit", type=int, default=None, help="cap on rows fetched per table")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    conn_str = neon_source.connection_string()
    db_module.init_db()

    tables = LOAD_ORDER if args.table == "all" else (args.table,)
    for table in tables:
        load_table(table, args.limit, args.dry_run, conn_str)


if __name__ == "__main__":
    main()
