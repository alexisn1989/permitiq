"""Import human review for Stage 2 -- the ONLY way a decision becomes
approved and the only way a candidate link becomes confirmed.

--outcomes  reports/land_use_review.csv, filled in. Rows with a blank
            verified_outcome are skipped (not reviewed yet). Every other row
            needs verified_outcome, evidence (minutes / IQM2 item URL or the
            verbatim motion) and reviewer. A quarantined decision also needs
            conflict_resolution, and resolved_title when its titles disagreed
            (addresses are then re-extracted from resolved_title).
--links     reports/link_review.csv, filled in. Rows with a blank verdict are
            skipped; others need verdict (confirmed | rejected) and reviewer.
            A confirmed permit must have been applied for on/after the
            approval and within link_window_months (the same SQLite date rule
            as the linker and the decision_permit_links_effective view).
            An outcome review records the decision's facts fingerprint; if
            the title/result/date later change on re-ingest, the review goes
            stale and must be redone.

The whole file is validated first; any invalid row means nothing is written.
Link verdicts take effect immediately (the links are rebuilt) and survive
every later rebuild.

Usage:
  python scripts/review_land_use.py --outcomes reports/land_use_review.csv
  python scripts/review_land_use.py --links reports/link_review.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import land_use, land_use_link  # noqa: E402
from ingest.address_norm import parse_title_addresses  # noqa: E402
from ingest.flood_stats import ConfigError  # noqa: E402

BASE = Path(__file__).resolve().parent.parent


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]


def validate_outcomes(conn, rows: list[dict]) -> tuple[list[dict], list[str]]:
    ok, errors = [], []
    for i, r in enumerate(rows, 2):
        if not r.get("verified_outcome"):
            continue
        dk = r.get("decision_key", "")
        if not conn.execute("SELECT 1 FROM land_use_decisions WHERE decision_key = ?", (dk,)).fetchone():
            errors.append(f"line {i}: unknown decision_key {dk!r}")
            continue
        if conn.execute("SELECT facts_fingerprint IS NULL FROM land_use_decisions WHERE decision_key = ?",
                        (dk,)).fetchone()[0]:
            errors.append(f"line {i}: {dk} has no facts fingerprint -- run scripts/migrate_db.py")
        if r["verified_outcome"] not in db_module.VERIFIED_OUTCOMES:
            errors.append(f"line {i}: verified_outcome must be one of {db_module.VERIFIED_OUTCOMES}")
        if not r.get("evidence"):
            errors.append(f"line {i}: evidence is required (minutes / IQM2 URL or verbatim motion)")
        if not r.get("reviewer"):
            errors.append(f"line {i}: reviewer is required")
        conflicts = {x[0] for x in conn.execute(
            "SELECT field FROM land_use_decision_conflicts WHERE decision_key = ?", (dk,))}
        if conflicts and not r.get("conflict_resolution"):
            errors.append(f"line {i}: {dk} is quarantined ({sorted(conflicts)}); conflict_resolution is required")
        if "title" in conflicts and not r.get("resolved_title"):
            errors.append(f"line {i}: {dk} has conflicting titles; resolved_title is required")
        ok.append(r)
    return ok, errors


def validate_links(conn, rows: list[dict], window_months: int) -> tuple[list[dict], list[str]]:
    ok, errors = [], []
    for i, r in enumerate(rows, 2):
        if not r.get("verdict"):
            continue
        if r["verdict"] not in ("confirmed", "rejected"):
            errors.append(f"line {i}: verdict must be confirmed or rejected")
        if not r.get("reviewer"):
            errors.append(f"line {i}: reviewer is required")
        if not conn.execute("SELECT 1 FROM land_use_decisions WHERE decision_key = ?", (r.get("decision_key"),)).fetchone():
            errors.append(f"line {i}: unknown decision_key {r.get('decision_key')!r}")
        if not r.get("permit_id", "").isdigit() or not conn.execute(
                "SELECT 1 FROM permits WHERE id = ? AND locality = 'virginia_beach'", (r.get("permit_id"),)).fetchone():
            errors.append(f"line {i}: permit_id {r.get('permit_id')!r} is not a Virginia Beach permit")
        elif r["verdict"] == "confirmed":
            d = conn.execute("SELECT meeting_date FROM land_use_decisions WHERE decision_key = ?",
                             (r.get("decision_key"),)).fetchone()
            applied = conn.execute("SELECT applied_date FROM permits WHERE id = ?", (r["permit_id"],)).fetchone()[0]
            meeting = d["meeting_date"] if d else None
            if not land_use_link.in_window(conn, meeting, applied, window_months):
                end = land_use_link.window_end(conn, meeting, window_months) if meeting else None
                errors.append(f"line {i}: permit {r['permit_id']} applied {applied!r} is outside the approval window "
                              f"{meeting!r} .. {end!r} ({window_months} months); only permits applied for on/after "
                              "the approval and within the window can be confirmed")
        ok.append(r)
    return ok, errors


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Import Stage 2 human review")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--outcomes", type=Path)
    g.add_argument("--links", type=Path)
    p.add_argument("--config", type=Path, default=BASE / "config" / "land_use.toml")
    args = p.parse_args(argv)
    ts = db_module.now_iso()
    cfg = None
    if args.links:
        try:
            cfg = land_use.load_config(args.config)  # the link window is part of validation
        except ConfigError as e:
            print(f"[FATAL] {e}", file=sys.stderr)
            return 1
    with db_module.db() as conn:
        if args.outcomes:
            rows, errors = validate_outcomes(conn, _rows(args.outcomes))
        else:
            rows, errors = validate_links(conn, _rows(args.links), cfg["link_window_months"])
        if errors:
            print("[FATAL] nothing written:\n  " + "\n  ".join(errors), file=sys.stderr)
            return 1
        if args.outcomes:
            for r in rows:
                conn.execute(
                    """INSERT INTO decision_outcome_review (decision_key, verified_outcome, evidence, reviewer,
                           conflict_resolution, resolved_title, reviewed_at, reviewed_fingerprint)
                       VALUES (?, ?, ?, ?, ?, ?, ?,
                               (SELECT facts_fingerprint FROM land_use_decisions WHERE decision_key = ?))
                       ON CONFLICT(decision_key) DO UPDATE SET verified_outcome = excluded.verified_outcome,
                           reviewed_fingerprint = excluded.reviewed_fingerprint,
                           evidence = excluded.evidence, reviewer = excluded.reviewer,
                           conflict_resolution = excluded.conflict_resolution,
                           resolved_title = excluded.resolved_title, reviewed_at = excluded.reviewed_at""",
                    (r["decision_key"], r["verified_outcome"], r["evidence"], r["reviewer"],
                     r.get("conflict_resolution") or None, r.get("resolved_title") or None, ts, r["decision_key"]))
                if r.get("resolved_title"):
                    land_use.sync_addresses(conn, r["decision_key"], parse_title_addresses(r["resolved_title"]), "review")
            print(f"{len(rows)} outcome review(s) imported")
        else:
            conn.executemany(
                """INSERT INTO decision_link_review (decision_key, permit_id, verdict, note, reviewer, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(decision_key, permit_id) DO UPDATE SET
                       verdict = excluded.verdict, note = excluded.note, reviewer = excluded.reviewer,
                       reviewed_at = excluded.reviewed_at""",
                [(r["decision_key"], int(r["permit_id"]), r["verdict"], r.get("note") or None, r["reviewer"], ts)
                 for r in rows])
            print(f"{len(rows)} link verdict(s) imported")
            if conn.execute("SELECT 1 FROM permit_coverage").fetchone():
                land_use_link.rebuild_links(conn, cfg, ts)
                print("links rebuilt with the new verdicts")
            else:
                print("verdicts stored; they apply when scripts/link_decisions_permits.py runs")
        print(f"status: {land_use_link.status_counts(conn)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
