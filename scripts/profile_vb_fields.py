"""Read-only profile of Virginia Beach permit fields, run BEFORE defining the
flood report's cohort (config/flood_report.toml). The cohort must be built
from values that actually occur, never guessed.

Prints: distinct PermitType / WorkType / ConstructionType with counts (from
raw_json, which the API never exposes), their cross-tab, issued vs unissued
and void counts, the issue-date range, and how many records lack a ZIP.

Usage:
  python scripts/profile_vb_fields.py
  python scripts/profile_vb_fields.py --top 50
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest.flood_stats import ZIP_SQL  # noqa: E402

FIELDS = (("PermitType", "$.PermitType"), ("WorkType", "$.WorkType"), ("ConstructionType", "$.ConstructionType"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Profile VB permit fields (read-only)")
    p.add_argument("--top", type=int, default=40, help="rows to show per listing")
    args = p.parse_args(argv)
    try:
        with db_module.db_readonly() as conn:
            total = conn.execute("SELECT COUNT(*) FROM permits WHERE locality = 'virginia_beach'").fetchone()[0]
            print(f"virginia_beach permits: {total}\n")
            for label, path in FIELDS:
                rows = conn.execute(
                    f"SELECT COALESCE(json_extract(raw_json, '{path}'), '<null>') AS v, COUNT(*) AS n FROM permits "
                    "WHERE locality = 'virginia_beach' GROUP BY v ORDER BY n DESC LIMIT ?", (args.top,)).fetchall()
                print(f"{label}:")
                for r in rows:
                    print(f"  {r['n']:>7}  {r['v']}")
                print()
            print("PermitType x WorkType x ConstructionType:")
            for r in conn.execute(
                    "SELECT COALESCE(json_extract(raw_json,'$.PermitType'),'<null>') AS pt, "
                    "COALESCE(json_extract(raw_json,'$.WorkType'),'<null>') AS wt, "
                    "COALESCE(json_extract(raw_json,'$.ConstructionType'),'<null>') AS ct, COUNT(*) AS n "
                    "FROM permits WHERE locality = 'virginia_beach' GROUP BY pt, wt, ct ORDER BY n DESC LIMIT ?",
                    (args.top,)):
                print(f"  {r['n']:>7}  {r['pt']!r} / {r['wt']!r} / {r['ct']!r}")
            s = conn.execute(
                "SELECT SUM(issued_date IS NOT NULL) AS issued, SUM(issued_date IS NULL) AS unissued, "
                "SUM(status_bucket = 'expired_void') AS void, MIN(issued_date) AS first, MAX(issued_date) AS last, "
                f"SUM({ZIP_SQL} = 'unknown') AS no_zip FROM permits p WHERE locality = 'virginia_beach'").fetchone()
            print(f"\nissued: {s['issued']}  unissued: {s['unissued']}  void/expired/withdrawn: {s['void']}")
            print(f"issue dates: {s['first']} .. {s['last']}   missing/invalid ZIP: {s['no_zip']}")
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
