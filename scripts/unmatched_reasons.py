"""Write every unmatched reason: reports/unmatched_reasons.csv has one row per
entity per aspect that did not fully match (permit geocode / parcel / point
flood / parcel flood; decision status / title / parcel rows), with a reason
code taken from the existing state columns -- nothing new is inferred.
reports/unmatched_reasons_counts.csv has the per-reason counts. Counts that
exist only in a run's logs (titles that were not land use, parcels skipped
without a GPIN or polygon, multi-part attribute conflicts) are in the run
manifest's step output.

Usage:
  python scripts/unmatched_reasons.py --out reports/
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import quality  # noqa: E402

BASE = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Export every unmatched reason")
    p.add_argument("--out", type=Path, default=BASE / "reports")
    args = p.parse_args(argv)
    try:
        with db_module.db_readonly() as conn:
            rows = quality.unmatched_rows(conn)
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "unmatched_reasons.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["entity", "key", "aspect", "reason", "detail"])
        w.writerows(rows)
    counts = Counter((r[0], r[2], r[3]) for r in rows)
    with (args.out / "unmatched_reasons_counts.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["entity", "aspect", "reason", "count"])
        w.writerows([(*k, n) for k, n in sorted(counts.items())])
    print(f"{len(rows)} unmatched row(s), {len(counts)} distinct reason(s) -> {args.out / 'unmatched_reasons.csv'}")
    for (e, a, r), n in counts.most_common(15):
        print(f"  {n:>7}  {e}/{a}: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
