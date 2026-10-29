"""Import Chesapeake building permits from Accela "Download results" CSVs
(ingest/chesapeake_permits.py). A person downloads the results of the site's own
General Search, one date window per file; this script never contacts Accela.

Complete or nothing: each file names its window and the result count the search
page showed. Every file must hold exactly that many rows, every row's date must
fall inside its window, and the windows must be contiguous with no overlap.
Any failure exits 2 and nothing is written. An unknown column or status stops
the import (exit 1).

counts.csv columns: file,start,end,page_count (dates YYYY-MM-DD, inclusive)

Usage:
  python scripts/import_chesapeake_permits.py --dir downloads/ --counts downloads/counts.csv [--dry-run]
  python scripts/import_chesapeake_permits.py --file RecordList.csv --start 2025-10-29 --end 2026-10-28 \
      --page-count 18565 [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import chesapeake_permits as CP  # noqa: E402
from ingest.common import finish_ingestion_run, start_ingestion_run, upsert_permits  # noqa: E402


class Incomplete(Exception):
    pass


def _windows(args) -> list[dict]:
    if args.file:
        return [{"file": args.file, "start": date.fromisoformat(args.start), "end": date.fromisoformat(args.end),
                 "page_count": args.page_count}]
    out = []
    with args.counts.open(encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            out.append({"file": args.dir / r["file"].strip(), "start": date.fromisoformat(r["start"].strip()),
                        "end": date.fromisoformat(r["end"].strip()), "page_count": int(r["page_count"])})
    return out


def check(windows: list[dict]) -> tuple[list[dict], dict]:
    """All rows of all windows, or Incomplete / ExportError. Nothing is written here."""
    if not windows:
        raise Incomplete("no files listed")
    windows = sorted(windows, key=lambda w: w["start"])
    problems, rows, stats = [], [], Counter()
    for prev, w in zip(windows, windows[1:]):
        if w["start"] != prev["end"] + timedelta(days=1):
            problems.append(f"windows not contiguous: {prev['end']} then {w['start']}")
    for w in windows:
        if w["end"] < w["start"]:
            problems.append(f"{w['file']}: end before start")
            continue
        got = CP.read_rows(Path(w["file"]).read_text(encoding="utf-8-sig"))
        if len(got) != w["page_count"]:
            problems.append(f"{Path(w['file']).name}: {len(got)} rows, the search page showed {w['page_count']}")
        outside = [r["Record Number"] for r in got if not w["start"] <= CP.row_date(r) <= w["end"]]
        if outside:
            problems.append(f"{Path(w['file']).name}: {len(outside)} row(s) dated outside {w['start']}..{w['end']}: "
                            f"{outside[:5]}")
        rows += got
    if problems:
        raise Incomplete("; ".join(problems))
    unknown = CP.unknown_statuses(rows)
    if unknown:
        raise CP.ExportError(f"statuses not seen before (meaning not checked): {unknown}")
    # Accela lists a few records twice in one result list; identical rows count once
    seen: dict[str, dict] = {}
    for r in rows:
        prev = seen.get(r["Record Number"])
        if prev is None:
            seen[r["Record Number"]] = r
        elif prev == r:
            stats["duplicate_rows_identical"] += 1
        else:
            raise CP.ExportError(f"{r['Record Number']} appears twice with different values")
    return list(seen.values()), {"span": [windows[0]["start"].isoformat(), windows[-1]["end"].isoformat()],
                                 "files": len(windows), "rows": len(rows), **stats}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Import Chesapeake permits from Accela downloaded results")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", type=Path)
    src.add_argument("--file", type=Path)
    p.add_argument("--counts", type=Path, help="with --dir: file,start,end,page_count")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--page-count", type=int)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if args.dir and not args.counts:
        p.error("--dir requires --counts")
    if args.file and None in (args.start, args.end, args.page_count):
        p.error("--file requires --start, --end and --page-count (the count the search page showed)")
    try:
        records, summary = check(_windows(args))
        permits = [CP.to_permit(r) for r in records]
    except Incomplete as e:
        print(f"[FATAL] incomplete download, nothing written: {e}", file=sys.stderr)
        return 2
    except (CP.ExportError, ValueError, KeyError, OSError) as e:
        print(f"[FATAL] {e}; nothing written", file=sys.stderr)
        return 1
    by_bucket = Counter(x.status_bucket for x in permits)
    withheld = sum(1 for x in permits if x.raw["project_withheld"])
    print(f"{summary['files']} file(s), {summary['span'][0]}..{summary['span'][1]}: {summary['rows']} rows -> "
          f"{len(permits)} permits; status buckets {dict(sorted(by_bucket.items()))}; project names withheld "
          f"{withheld}; {json.dumps({k: v for k, v in summary.items() if k not in ('span', 'files', 'rows')})}")
    if args.dry_run:
        print("--dry-run: nothing written")
        return 0
    with db_module.db() as conn:
        run_id = start_ingestion_run(conn, CP.LOCALITY, summary["span"][0])
        before = dict(conn.execute("SELECT source_permit_id, address FROM permits WHERE locality = ?", (CP.LOCALITY,)))
        counts = upsert_permits(conn, permits)
        # the address is the parcel match's evidence: a changed address clears the match (reads untagged)
        moved = [x.source_permit_id for x in permits
                 if x.source_permit_id in before and before[x.source_permit_id] != x.address]
        conn.executemany("DELETE FROM permit_parcel WHERE permit_id = (SELECT id FROM permits WHERE locality = ? "
                         "AND source_permit_id = ?)", [(CP.LOCALITY, k) for k in moved])
        counts["address_changed"] = len(moved)
        finish_ingestion_run(conn, run_id, "success", fetched_count=summary["rows"], counts=counts,
                             error_message=f"accela download {summary['span'][0]}..{summary['span'][1]}")
    print(f"ingestion run {run_id}: {counts}. Next: python run_ingest.py --geocode-only, then "
          "python scripts/match_parcels.py --locality chesapeake")
    return 0


if __name__ == "__main__":
    sys.exit(main())
