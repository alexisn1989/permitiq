"""Record source dates ("data as of") from the database's active loads and
runs, plus the Council export header, into source_dates. Run by
scripts/run_vb_pipeline.py after the loads; served by /api/data-as-of.

Usage:
  python scripts/record_source_dates.py [--council-export vb_council_votes.jsonl]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import land_use, quality  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Record source dates")
    p.add_argument("--council-export", type=Path)
    args = p.parse_args(argv)
    meta = None
    if args.council_export:
        try:
            meta, _ = land_use.read_export(args.council_export)  # also re-verifies the row count
        except (OSError, ValueError) as e:
            print(f"[FATAL] {e}", file=sys.stderr)
            return 1
    with db_module.db() as conn:
        dates = quality.compute_source_dates(conn, meta)
        quality.record_source_dates(conn, dates, db_module.now_iso())
    print(json.dumps({k: v.get("as_of") for k, v in dates.items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
