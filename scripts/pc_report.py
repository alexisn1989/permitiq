"""Planning Commission leads: reports/pc_leads.csv.

One row per non-city agenda item that is scheduled or was recommended for
approval (or deferred) and has no Council decision candidate yet -- the
earliest signal of a land-use project. Names of private individuals are
withheld (organizations only); applicant, request and size come from the
agenda text and are not verified. A recommendation is never an approval.

Usage:
  python scripts/pc_report.py --out reports/
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import pc_hearings  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
LEAD_STATUSES = ("scheduled", "recommended_approval", "deferred")
COLUMNS = ["stage", "item_key", "hearing_date", "status", "status_source", "applicants", "applicants_withheld",
           "owners", "owners_withheld", "request", "addresses", "gpins", "council_district", "accela_records",
           "staff_planner", "description", "size_from_description", "agenda_url"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Planning Commission lead list")
    p.add_argument("--out", type=Path, default=BASE / "reports")
    p.add_argument("--provisional", action="store_true")
    args = p.parse_args(argv)
    try:
        with db_module.db_readonly() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT i.*, s.status, s.status_source, d.url AS agenda_url FROM pc_items i "
                "JOIN pc_item_status s ON s.item_key = i.item_key JOIN pc_documents d ON d.doc_id = i.doc_id "
                f"WHERE i.city_initiated = 0 AND s.n_council_candidates = 0 AND s.status IN "
                f"({','.join('?' * len(LEAD_STATUSES))}) ORDER BY i.meeting_date DESC, i.item_key", LEAD_STATUSES)]
            cov = pc_hearings.coverage(conn)
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "pc_leads.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            it = pc_hearings.public_item(r)
            w.writerow(["provisional" if args.provisional else "final", it["item_key"], it["meeting_date"],
                        r["status"], r["status_source"] or "", "; ".join(it["applicants"]["names"]),
                        it["applicants"]["withheld"], "; ".join(it["owners"]["names"]), it["owners"]["withheld"],
                        it["request"] or "", it["addresses"] or "", " ".join(it["gpins"]),
                        it["council_district_text"] or "", ", ".join(it["accela_records"]), it["staff_planner"] or "",
                        it["description"] or "", "; ".join(x["text"] for x in it["size_from_description"]),
                        r["agenda_url"]])
    (args.out / "pc_coverage.json").write_text(json.dumps(cov, indent=1), encoding="utf-8")
    print(f"Wrote {args.out / 'pc_leads.csv'} ({len(rows)} leads); agendas for {cov['agenda_found']} of "
          f"{cov['months']} months ({'verified' if cov['verified'] else 'coverage unverified'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
