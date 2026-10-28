"""Chesapeake pipeline reports:
  reports/chesapeake_cases.csv     land-use cases (Development Tracking), newest first, legacy rows marked
  reports/development_plans.csv    development plans (not building permits)
Each row carries the parcels' site-constraint columns. Stages and actions are the
city's published record, labelled from the layer's own coded domain. Names of
private individuals are withheld.

Usage:
  python scripts/chesapeake_report.py --out reports/
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import chesapeake_records as CH, constraints as C  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
CASE_COLUMNS = ["case_id", "case_type", "stage", "final_as_printed", "final_date", "applied_date", "legacy", "project",
                "project_withheld", "address", "zoning_from", "zoning_to", "housing_units", "staff_recommendation",
                "pc_action", "pc_date", "cc_action", "cc_date", "parcels", "city_record_link", *C.CSV_COLUMNS]
PLAN_COLUMNS = ["record_id", "record_type", "status", "submitted_date", "approved_date", "organization",
                "organization_withheld", "address", "parcels", *C.CSV_COLUMNS]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Chesapeake land-use cases and development plans")
    p.add_argument("--out", type=Path, default=BASE / "reports")
    args = p.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        with db_module.db_readonly() as conn:
            load = conn.execute("SELECT domain_json FROM city_record_loads WHERE source = 'ches_cases' "
                                "AND is_active = 1").fetchone()
            domain = json.loads(load[0]) if load else {}
            with (args.out / "chesapeake_cases.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(CASE_COLUMNS)
                n_cases = 0
                for r in conn.execute("SELECT * FROM land_use_cases_effective "
                                      "ORDER BY applied_date IS NULL, applied_date DESC, case_id"):
                    c = CH.public_case(dict(r), domain)
                    w.writerow([c["case_id"], c["case_type"] or "", c["final"]["stage"], c["final"]["as_printed"] or "",
                                c["final"]["date"] or "", c["applied_date"] or "", int(c["legacy"]),
                                c["project"]["name"] or "", int(c["project"]["withheld"]), c["address"] or "",
                                c["zoning_from"] or "", c["zoning_to"] or "",
                                "" if c["housing_units"] is None else c["housing_units"],
                                c["staff_recommendation"] or "", c["planning_commission"]["action"] or "",
                                c["planning_commission"]["date"] or "", c["city_council"]["action"] or "",
                                c["city_council"]["date"] or "", " ".join(c["gpins"]), c["source_link"] or "",
                                *C.csv_summary(conn, c["gpins"]).values()])
                    n_cases += 1
            with (args.out / "development_plans.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(PLAN_COLUMNS)
                n_plans = 0
                for r in conn.execute("SELECT * FROM development_records_effective WHERE source = 'ches_dev_plans' "
                                      "ORDER BY submitted_date IS NULL, submitted_date DESC, record_id"):
                    d = CH.public_record(dict(r))
                    w.writerow([d["record_id"], d["record_type"] or "", d["status"] or "", d["submitted_date"] or "",
                                d["approved_date"] or "", d["organization"]["name"] or "",
                                int(d["organization"]["withheld"]), d["address"] or "", " ".join(d["gpins"]),
                                *C.csv_summary(conn, d["gpins"]).values()])
                    n_plans += 1
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    print(f"Wrote {args.out / 'chesapeake_cases.csv'} ({n_cases} cases) and development_plans.csv ({n_plans} plans)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
