"""Import human verdicts on disputed parcel matches (Stage 3) from a filled-in
reports/parcel_review.csv.

A dispute is a GPIN match (or printed GPIN / address) and a point that land on
different parcels. Until resolved it is excluded from same_parcel linking.
Verdicts: gpin_correct (keep the GPIN parcel), point_correct (use the point's
parcel -- requires alt_gpin), unresolvable (stays excluded). Rows with a blank
verdict are skipped; every other row needs a reviewer. The whole file is
validated first; any invalid row means nothing is written.

Each verdict records the fingerprint of the match it judged (load, raw GPIN,
coordinates, result). If any of those change, the verdict stops applying and
the match is disputed again until re-reviewed.

Usage:
  python scripts/review_parcels.py reports/parcel_review.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402


def current_match(conn, kind: str, key: str):
    if kind == "permit":
        if not key.isdigit():
            return None
        return conn.execute(
            "SELECT x.match_fp, x.alt_gpin, e.disputed, e.fresh FROM permit_parcel x "
            "JOIN permit_parcel_effective e ON e.permit_id = x.permit_id WHERE x.permit_id = ?", (int(key),)).fetchone()
    dk, _, seq = key.rpartition("#")
    if not seq.isdigit():
        return None
    return conn.execute(
        "SELECT x.match_fp, x.alt_gpin, e.disputed, e.fresh FROM decision_parcel x JOIN decision_parcel_effective e "
        "ON e.decision_key = x.decision_key AND e.seq = x.seq WHERE x.decision_key = ? AND x.seq = ?",
        (dk, int(seq))).fetchone()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Import parcel dispute verdicts")
    p.add_argument("file", type=Path)
    args = p.parse_args(argv)
    with args.file.open(newline="", encoding="utf-8") as fh:
        rows = [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]
    ts = db_module.now_iso()
    with db_module.db() as conn:
        ok, errors = [], []
        for i, r in enumerate(rows, 2):
            if not r.get("verdict"):
                continue
            kind, key = r.get("subject_kind", ""), r.get("subject_key", "")
            if kind not in ("permit", "decision"):
                errors.append(f"line {i}: subject_kind must be permit or decision")
                continue
            if r["verdict"] not in db_module.PARCEL_REVIEW_VERDICTS:
                errors.append(f"line {i}: verdict must be one of {db_module.PARCEL_REVIEW_VERDICTS}")
            if not r.get("reviewer"):
                errors.append(f"line {i}: reviewer is required")
            m = current_match(conn, kind, key)
            if m is None or not m["fresh"]:
                errors.append(f"line {i}: no current parcel match for {kind} {key!r} -- rerun scripts/match_parcels.py")
                continue
            if not m["disputed"]:
                errors.append(f"line {i}: {kind} {key} is not disputed")
            if r["verdict"] == "point_correct" and not m["alt_gpin"]:
                errors.append(f"line {i}: point_correct needs a point parcel, and {kind} {key} has none")
            ok.append((kind, key, r["verdict"], m["match_fp"], r["reviewer"], r.get("note") or None, ts))
        if errors:
            print("[FATAL] nothing written:\n  " + "\n  ".join(errors), file=sys.stderr)
            return 1
        conn.executemany(
            """INSERT INTO parcel_match_review (subject_kind, subject_key, verdict, reviewed_fp, reviewer, note,
                   reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(subject_kind, subject_key) DO UPDATE SET verdict = excluded.verdict,
                   reviewed_fp = excluded.reviewed_fp, reviewer = excluded.reviewer, note = excluded.note,
                   reviewed_at = excluded.reviewed_at""", ok)
    print(f"{len(ok)} parcel verdict(s) imported; rerun scripts/link_decisions_permits.py to apply them to links")
    return 0


if __name__ == "__main__":
    sys.exit(main())
