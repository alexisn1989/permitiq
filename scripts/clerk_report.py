"""Council actions report: reports/council_actions.csv -- every land-use item
read from the City Clerk's Summary of Council Actions, newest first, with the
Planning Commission item it decides (when matched) and a link to the Clerk's
page. No subject text or raw blocks; names of private individuals withheld.

Usage:
  python scripts/clerk_report.py --out reports/
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import council_actions  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
COLUMNS = ["council_date", "item_label", "outcome", "deferred_to", "aye", "nay", "request_types", "addresses",
           "council_district", "applicant", "applicant_withheld", "pc_items", "possible_pc_items", "clerk_link",
           "page", "warnings"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Land-use items from the Clerk's Summary of Council Actions")
    p.add_argument("--out", type=Path, default=BASE / "reports")
    args = p.parse_args(argv)
    try:
        with db_module.db_readonly() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM clerk_items WHERE is_land_use = 1 ORDER BY meeting_date DESC, item_key")]
            addrs, links = {}, {}
            for a in conn.execute("SELECT item_key, raw_text FROM clerk_item_addresses ORDER BY item_key, seq"):
                addrs.setdefault(a["item_key"], []).append(a["raw_text"])
            for l in conn.execute("SELECT clerk_item_key, pc_item_key, ambiguous FROM clerk_pc_links"):
                links.setdefault(l["clerk_item_key"], ([], []))[l["ambiguous"]].append(l["pc_item_key"])
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "council_actions.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            a = council_actions.public_item(r)
            sure, maybe = links.get(r["item_key"], ([], []))
            w.writerow([a["meeting_date"], a["item_label"] or "", a["outcome"], a["deferred_to"] or "",
                        "" if a["aye"] is None else a["aye"], "" if a["nay"] is None else a["nay"],
                        " ".join(a["request_types"]), "; ".join(addrs.get(r["item_key"], [])),
                        a["council_district"] or "", "; ".join(a["applicant"]["names"]), a["applicant"]["withheld"],
                        " ".join(sure), " ".join(maybe), a["source_url"], a["page"], "; ".join(a["warnings"])])
    print(f"Wrote {args.out / 'council_actions.csv'} ({len(rows)} land-use Council actions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
