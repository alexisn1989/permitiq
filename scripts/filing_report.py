"""Filing leads: reports/filing_leads.csv -- applications filed and not yet
past a hearing (stage "filed" or "scheduled"), newest first. Names of private
individuals are withheld. A filing is not an approval.

Usage:
  python scripts/filing_report.py --out reports/
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import constraints as C, filings as F  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
COLUMNS = ["stage", "pln_id", "filed", "plan_type", "applicant", "applicant_withheld", "project", "parcels",
           "hearing_date", "permits_on_site_since_filing", "accela_link", *C.CSV_COLUMNS]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Applications filed, not yet heard")
    p.add_argument("--out", type=Path, default=BASE / "reports")
    args = p.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        with db_module.db_readonly() as conn, (args.out / "filing_leads.csv").open("w", newline="",
                                                                                   encoding="utf-8") as fh:
            rows = [dict(r) for r in conn.execute(
                "SELECT f.*, s.stage, s.hearing_date, s.permits_after_filing FROM filings f "
                "JOIN filing_loads l ON l.load_id = f.load_id AND l.is_active = 1 "
                "JOIN filing_status s ON s.pln_id = f.pln_id "
                "WHERE s.stage IN ('filed', 'scheduled') ORDER BY f.date_sub DESC, f.pln_id")]
            w = csv.writer(fh)
            w.writerow(COLUMNS)
            for r in rows:
                f = F.public_filing(r)
                w.writerow([r["stage"], f["pln_id"], f["date_sub"], f["plan_type_label"],
                            f["applicant"]["name"] or "", int(f["applicant"]["withheld"]), f["project"]["name"] or "",
                            " ".join(f["gpins"]), r["hearing_date"] or "", r["permits_after_filing"], f["deeplink"] or "",
                            *C.csv_summary(conn, f["gpins"]).values()])
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    print(f"Wrote {args.out / 'filing_leads.csv'} ({len(rows)} applications filed, not yet heard)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
