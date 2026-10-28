"""Write the Stage 3 parcel report: reports/parcel_vb.md + reports/parcel_vb.csv.

Sections: parcel match coverage by method (permits, decision addresses);
disputed matches (unresolved / resolved); a point-vs-parcel flood agreement
matrix for issued Virginia Beach permits; our parcel class vs the city's own
FEMA_ZONE_BLDG field (cross-check); and flood exposure on the parcel basis
next to the Stage 1 point basis, from ingest/flood_stats.py (same
denominators, same suppression) -- baseline always, cohort when
config/flood_report.toml defines one.

Refuses without an active parcel load.

Usage:
  python scripts/parcel_report.py
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import flood_stats  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
FLOOD_CONFIG = Path(os.getenv("FLOOD_REPORT_CONFIG", str(BASE / "config" / "flood_report.toml")))
POPULATION = "p.locality = 'virginia_beach' AND p.issued_date IS NOT NULL AND p.status_bucket != 'expired_void'"
CAVEATS = [
    "Parcel flood classes are a screening estimate from parcel-polygon area shares, not a flood-zone determination. "
    "The city's parcel terms: 'not valid for legal survey, engineering design, or flood-zone determination'.",
    "screening_sliver: a hazard zone touches the parcel but covers less than min_share of it (below the city's "
    "stated +/-2-5 ft accuracy on a typical lot). screening_partial_coverage: part of the parcel has no FEMA polygon. "
    "Both are coverage classes, never counted as outside the flood zone.",
    "Disputed parcel matches (GPIN and point on different parcels) are excluded from parcel-basis shares and from "
    "same_parcel linking until reviewed.",
    "Parcel boundaries are as of the parcel load's vintage; no assessed values are included (no assessor source yet).",
]


def matrix(conn, sql: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(sql).fetchall()]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Stage 3 parcel report")
    p.add_argument("--out", type=Path, default=BASE / "reports")
    p.add_argument("--config", type=Path, default=FLOOD_CONFIG)
    args = p.parse_args(argv)
    try:
        cohort_cfg = flood_stats.load_cohort_config(args.config)
        with db_module.db_readonly() as conn:
            load = conn.execute("SELECT * FROM parcel_loads WHERE is_active = 1 "
                                "AND locality = 'virginia_beach'").fetchone()
            if load is None:
                print("[FATAL] no active parcel load: run scripts/load_parcels.py", file=sys.stderr)
                return 1
            permit_methods = matrix(conn, "SELECT e.parcel_state, COUNT(*) FROM permits p JOIN permit_parcel_effective e "
                                          "ON e.permit_id = p.id WHERE p.locality = 'virginia_beach' "
                                          "GROUP BY e.parcel_state ORDER BY 2 DESC")
            decision_methods = matrix(conn, "SELECT parcel_state, COUNT(*) FROM decision_parcel_effective "
                                            "GROUP BY parcel_state ORDER BY 2 DESC")
            disputes = matrix(conn, "SELECT 'permit', COALESCE(review_verdict, 'unreviewed'), COUNT(*) "
                                    "FROM permit_parcel_effective WHERE disputed = 1 GROUP BY 2 "
                                    "UNION ALL SELECT 'decision', COALESCE(review_verdict, 'unreviewed'), COUNT(*) "
                                    "FROM decision_parcel_effective WHERE disputed = 1 GROUP BY 2")
            point_vs_parcel = matrix(conn, f"""
                SELECT e.effective_class, {flood_stats.PARCEL_CLASS_SQL} AS parcel_class, COUNT(*)
                FROM permits p JOIN permit_flood_effective e ON e.permit_id = p.id {flood_stats.PARCEL_JOIN_SQL}
                WHERE {POPULATION} GROUP BY 1, 2 ORDER BY 1, 2""")
            city_vs_ours = matrix(conn, """
                SELECT COALESCE(pa.fema_zone_bldg, '<null>'), pf.parcel_flood_class, COUNT(*)
                FROM parcel_flood_effective pf JOIN parcels pa ON pa.load_id = pf.parcel_load_id AND pa.gpin = pf.gpin
                GROUP BY 1, 2 ORDER BY 1, 2""")
            stats = {b: flood_stats.compute_flood_stats(conn, cohort_cfg, basis=b) for b in ("point", "parcel")}
    except flood_stats.ConfigError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1

    def table(header, rows):
        return [f"| {' | '.join(header)} |", "|" + "---|" * len(header)] + \
               [f"| {' | '.join(str(x) for x in r)} |" for r in rows]

    def shares_row(label, block):
        o = block["overall"]
        s = o["shares_of_matched"] or {}
        pct = lambda k: "–" if k not in s else f"{s[k]:.1f}%"  # noqa: E731
        return (label, o["total"], o["matched"], pct("sfha_total"), pct("sfha_v"), pct("sfha_a"), pct("shaded_x"))

    exposure = [shares_row(f"{pop} — {b} basis", stats[b][pop])
                for pop in ("baseline", "cohort") for b in ("point", "parcel") if stats[b][pop]]
    md = ["# Virginia Beach parcels — Stage 3", "",
          f"Parcel load {load['load_id']}: {load['feature_count']} parcels from {load['source_url']} "
          f"(vintage {load['source_vintage'] or 'unknown'}; {load['received_count']} of {load['expected_count']} "
          f"source features, {load['unique_source_ids']} unique IDs), loaded {load['loaded_at']}.", "",
          "## Parcel match coverage", "", "### Permits (Virginia Beach)", "", *table(("method", "permits"), permit_methods),
          "", "### Decision addresses", "", *table(("method", "addresses"), decision_methods),
          "", "## Disputed matches (GPIN and point on different parcels)", "",
          *table(("subject", "review", "count"), disputes),
          "", "## Flood exposure: point basis vs parcel basis", "",
          "Shares of matched permits; coverage classes (screening, no parcel, ambiguous, disputed, untagged) are "
          f"excluded from denominators. Suppressed below n={stats['point']['min_n']}.", "",
          *table(("population", "permits", "matched", "% SFHA", "% V", "% A", "% shaded X"), exposure),
          "", "## Point class vs parcel class (issued permits)", "",
          *table(("point class", "parcel class", "permits"), point_vs_parcel),
          "", "## City FEMA_ZONE_BLDG vs our parcel class (cross-check)", "",
          *table(("city FEMA_ZONE_BLDG", "our parcel class", "parcels"), city_vs_ours),
          "", "## Caveats", "", *[f"- {c}" for c in CAVEATS], ""]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "parcel_vb.md").write_text("\n".join(md), encoding="utf-8")
    with (args.out / "parcel_vb.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "a", "b", "count"])
        for name, rows in (("permit_method", permit_methods), ("decision_method", decision_methods)):
            w.writerows([(name, a, "", n) for a, n in rows])
        for name, rows in (("dispute", disputes), ("point_vs_parcel", point_vs_parcel), ("city_vs_ours", city_vs_ours)):
            w.writerows([(name, *r) for r in rows])
    print(f"Wrote {args.out / 'parcel_vb.md'} and parcel_vb.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
