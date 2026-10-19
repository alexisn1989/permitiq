"""Link Virginia Beach land-use decisions to later building permits and
record the permit observation window (Stage 2).

* Refuses unless config/land_use.toml [coverage] names a verified backfill
  run (backfill_from + backfill_run_id, checked against ingestion_runs) --
  the earliest permit on file never stands in for coverage.
* Rebuilds decision_permit_links in one transaction. Every automatic link is
  a CANDIDATE; decision_link_review verdicts (scripts/review_land_use.py
  --links) promote, reject or add links and survive every rebuild.
* Writes reports/link_review.csv: candidate links plus "nearby but not
  linked" permits (same street, house number within 10) -- for review only;
  nearby permits are never linked automatically.

Usage:
  python scripts/link_decisions_permits.py --dry-run
  python scripts/link_decisions_permits.py
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import land_use, land_use_link  # noqa: E402
from ingest.flood_stats import ConfigError  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
REVIEW_LIMIT = 200
LINK_REVIEW_COLUMNS = ("kind", "decision_key", "meeting_date", "decision_title", "permit_id", "source_permit_id",
                       "permit_address", "applied_date", "issued_date", "permit_type", "description", "match_tier",
                       "matched_on", "link_state",
                       # filled in by the reviewer, then imported with review_land_use.py --links
                       "verdict", "reviewer", "note")


class _DryRun(Exception):
    pass


def write_link_review(conn, result: dict, path: Path) -> int:
    def detail(dk, pid):
        return conn.execute(
            "SELECT d.meeting_date, d.title, p.source_permit_id, p.address, p.applied_date, p.issued_date, "
            "p.permit_type, p.description FROM land_use_decisions d, permits p "
            "WHERE d.decision_key = ? AND p.id = ?", (dk, pid)).fetchone()

    out = [("candidate_link", l) for l in result["links"] if l["link_state"] == "candidate"][:REVIEW_LIMIT]
    out += [("nearby_not_linked", {**n, "match_tier": "", "link_state": ""}) for n in result["nearby"]][:REVIEW_LIMIT]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(LINK_REVIEW_COLUMNS)
        for kind, l in out:
            d = detail(l["decision_key"], l["permit_id"])
            w.writerow([kind, l["decision_key"], d["meeting_date"], (d["title"] or "")[:200], l["permit_id"],
                        d["source_permit_id"], d["address"], d["applied_date"], d["issued_date"], d["permit_type"],
                        (d["description"] or "")[:200], l["match_tier"], l["matched_on"], l["link_state"], "", "", ""])
    return len(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Link land-use decisions to later VB permits")
    p.add_argument("--config", type=Path, default=BASE / "config" / "land_use.toml")
    p.add_argument("--reports-dir", type=Path, default=BASE / "reports")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    try:
        cfg = land_use.load_config(args.config)
    except ConfigError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1
    ts = db_module.now_iso()
    try:
        with db_module.db() as conn:
            cov = land_use_link.verify_coverage(conn, cfg)
            before = land_use_link.status_counts(conn)
            land_use_link.write_coverage(conn, cov, ts)
            result = land_use_link.rebuild_links(conn, cfg, ts)
            after = land_use_link.status_counts(conn)
            tiers = Counter((l["match_tier"], l["link_state"]) for l in result["links"])
            print(f"coverage: backfill_from {cov['backfill_from']} ({cov['backfill_source']}); "
                  f"last refresh {cov['last_refresh_at']} (run {cov['last_refresh_run_id']})")
            print(f"links: {len(result['links'])} -> " + ", ".join(f"{t}/{s}: {n}" for (t, s), n in sorted(tiers.items())))
            print(f"nearby but not linked (review only): {len(result['nearby'])}")
            print(f"parcel gating: {land_use_link.gpin_gate_counts(conn)}")
            for o in result["review_out_of_window"]:
                print(f"  confirmed review outside window -- ignored: {o['decision_key']} permit {o['permit_id']} "
                      f"(meeting {o['meeting_date']}, applied {o['applied_date']})")
            print(f"status before: {before}\nstatus after:  {after}")
            if args.dry_run:
                raise _DryRun
            n = write_link_review(conn, result, args.reports_dir / "link_review.csv")
            print(f"{n} row(s) for review -> {args.reports_dir / 'link_review.csv'}")
    except land_use_link.CoverageError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1
    except _DryRun:
        print("--dry-run: rolled back, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
