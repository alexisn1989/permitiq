"""Verify (and top up) a Virginia Beach permit backfill against the source's
own count and OBJECTID set for the same window. See ingest/backfill_check.py.

Truly missing permits are fetched by OBJECTID and upserted, then the check
runs again. The result is stored in permit_backfill_checks for the backfill
run; the linker accepts that run as the coverage start only when verified.
Topped-up permits still need geocoding (scripts/geocode_backlog.py).

Exit codes: 0 verified; 3 not verified (gaps printed); 1 error.

Usage:
  python scripts/verify_permit_backfill.py --since 2021-01-01 [--run-id N] [--no-top-up]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import backfill_check as bc  # noqa: E402
from ingest import virginia_beach as vb  # noqa: E402
from ingest.common import upsert_permits  # noqa: E402


def show(r: dict) -> None:
    print(f"where {r['where']}: source count {r['source_count']}, source OBJECTIDs {r['source_ids']} "
          f"(listed {r['source_ids_listed']}), DB OBJECTIDs in window {r['db_ids']}")
    for k in ("truly_missing", "dup_permit_number", "no_permit_number", "extra_in_db", "dup_objectids_db"):
        v = r[k]
        print(f"  {k:<18} {len(v):>7}" + (f"   e.g. {v[:8]}" if v else ""))
    print(f"  {'null_date_source':<18} {r['null_date_source']:>7}   (no ApplicationDate: outside every window)")
    print("  year   source  db_permits")
    for y in r["per_year"]:
        flag = "  <- source > DB" if y["source"] > y["db_permits"] else ""
        print(f"  {y['year']}  {y['source']:>7}  {y['db_permits']:>10}{flag}")
    print(f"  verified: {bool(r['verified'])}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Verify a VB permit backfill against the source")
    p.add_argument("--since", required=True)
    p.add_argument("--run-id", type=int, help="backfill ingestion_runs.id (default: latest successful VB run)")
    p.add_argument("--no-top-up", action="store_true")
    args = p.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        p.error("--since must be YYYY-MM-DD")
    try:
        with db_module.db() as conn:
            run_id = args.run_id or (conn.execute(
                "SELECT id FROM ingestion_runs WHERE locality = 'virginia_beach' AND status = 'success' "
                "ORDER BY id DESC LIMIT 1").fetchone() or [None])[0]
            if run_id is None:
                print("[FATAL] no successful Virginia Beach ingestion run to verify", file=sys.stderr)
                return 1
            result = bc.check(conn, args.since)
            show(result)
            topped = 0
            if result["truly_missing"] and not args.no_top_up:
                permits = list(vb.fetch_by_ids(result["truly_missing"]))
                upsert_permits(conn, permits)
                topped = len(permits)
                print(f"top-up: fetched and upserted {topped} permit(s) by OBJECTID; checking again")
                result = bc.check(conn, args.since)
                show(result)
            cid = bc.record(conn, run_id, result, topped, db_module.now_iso())
            print(f"recorded permit_backfill_checks.{cid} for backfill run {run_id}")
    except (RuntimeError, vb.SourceSchemaError, KeyError, ValueError) as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1
    return 0 if result["verified"] else 3


if __name__ == "__main__":
    sys.exit(main())
