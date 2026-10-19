"""Match Virginia Beach permits and land-use decision addresses to parcels of
the active parcel load (Stage 3). Rules: ingest/parcels.py.

Only rows whose match is not fresh are (re)matched (--all: every row);
freshness is the permit_parcel_effective / decision_parcel_effective views,
so "needs matching" and "reads as untagged" can never disagree. When two
pieces of evidence point at different parcels the match is DISPUTED: kept
and shown, excluded from same_parcel linking, and listed in
reports/parcel_review.csv for scripts/review_parcels.py.

Usage:
  python scripts/match_parcels.py --dry-run
  python scripts/match_parcels.py
  python scripts/match_parcels.py --all
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import parcels as P  # noqa: E402
from shapely.geometry import shape  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
REVIEW_COLUMNS = ("subject_kind", "subject_key", "method", "gpin", "alt_gpin", "detail",
                  # filled in by the reviewer, then imported with scripts/review_parcels.py
                  "verdict", "reviewer", "note")


class _DryRun(Exception):
    pass


def load_index(conn, load_id: int) -> P.ParcelIndex:
    rows = [{"gpin": r["gpin"], "gpin_digits": r["gpin_digits"], "gpin10": r["gpin10"], "house": r["house"],
             "street_norm": r["street_norm"], "geom": shape(json.loads(r["geom_geojson"]))}
            for r in conn.execute("SELECT gpin, gpin_digits, gpin10, house, street_norm, geom_geojson FROM parcels "
                                  "WHERE load_id = ? ORDER BY gpin", (load_id,))]
    return P.ParcelIndex(rows)


def match_permits(conn, index, load_id, everything, ts) -> Counter:
    todo = conn.execute(
        "SELECT p.id, p.lat, p.lng, json_extract(p.raw_json, '$.GPIN') AS gpin_raw FROM permits p "
        "JOIN permit_parcel_effective e ON e.permit_id = p.id WHERE p.locality = 'virginia_beach' "
        "AND (e.fresh = 0 OR ?)", (int(everything),)).fetchall()
    out: Counter = Counter()
    rows = []
    for r in todo:
        m = P.match_permit(index, r["gpin_raw"], r["lat"], r["lng"])
        out[m["method"]] += 1
        out["disputed"] += m["disputed"]
        fp = P.fingerprint("permit", load_id, r["gpin_raw"], r["lat"], r["lng"], m["method"], m["gpin"], m["alt_gpin"])
        rows.append((r["id"], load_id, m["method"], m["gpin"], m["alt_gpin"], m["disputed"], m["gpin_point_agree"],
                     r["lat"], r["lng"], r["gpin_raw"], fp, ts))
    conn.executemany(
        """INSERT INTO permit_parcel (permit_id, load_id, method, gpin, alt_gpin, disputed, gpin_point_agree,
               tagged_lat, tagged_lng, tagged_gpin_raw, match_fp, matched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(permit_id) DO UPDATE SET load_id = excluded.load_id, method = excluded.method,
               gpin = excluded.gpin, alt_gpin = excluded.alt_gpin, disputed = excluded.disputed,
               gpin_point_agree = excluded.gpin_point_agree, tagged_lat = excluded.tagged_lat,
               tagged_lng = excluded.tagged_lng, tagged_gpin_raw = excluded.tagged_gpin_raw,
               match_fp = excluded.match_fp, matched_at = excluded.matched_at""", rows)
    out["matched_rows"] = len(rows)
    return out


def match_decisions(conn, index, load_id, everything, ts) -> Counter:
    todo = conn.execute(
        "SELECT a.* FROM land_use_decision_addresses a JOIN decision_parcel_effective e "
        "ON e.decision_key = a.decision_key AND e.seq = a.seq WHERE e.fresh = 0 OR ?", (int(everything),)).fetchall()
    out: Counter = Counter()
    rows = []
    for a in todo:
        m = P.match_decision_address(index, a["gpin10"], a["house_from"], a["house_to"], a["street_norm"],
                                     a["lat"], a["lng"])
        out[m["method"]] += 1
        out["disputed"] += m["disputed"]
        # every fact the match used: printed GPIN, parsed address (both house numbers), point, load
        fp = P.fingerprint("decision", load_id, a["gpin10"], a["house_from"], a["house_to"], a["street_norm"],
                           a["lat"], a["lng"], m["method"], m["gpin"], m["alt_gpin"])
        rows.append((a["decision_key"], a["seq"], load_id, m["method"], m["gpin"], m["alt_gpin"], m["disputed"],
                     a["gpin10"], a["house_from"], a["house_to"], a["street_norm"], a["lat"], a["lng"], fp, ts))
    conn.executemany(
        """INSERT INTO decision_parcel (decision_key, seq, load_id, method, gpin, alt_gpin, disputed, tagged_gpin10,
               tagged_house, tagged_house_to, tagged_street, tagged_lat, tagged_lng, match_fp, matched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(decision_key, seq) DO UPDATE SET load_id = excluded.load_id, method = excluded.method,
               gpin = excluded.gpin, alt_gpin = excluded.alt_gpin, disputed = excluded.disputed,
               tagged_gpin10 = excluded.tagged_gpin10, tagged_house = excluded.tagged_house,
               tagged_house_to = excluded.tagged_house_to,
               tagged_street = excluded.tagged_street, tagged_lat = excluded.tagged_lat,
               tagged_lng = excluded.tagged_lng, match_fp = excluded.match_fp, matched_at = excluded.matched_at""",
        rows)
    out["matched_rows"] = len(rows)
    return out


def write_review(conn, path: Path) -> int:
    rows = [("permit", str(r["id"]), r["method"], r["gpin"], r["alt_gpin"],
             f"{r['source_permit_id']} {r['address']} raw GPIN {r['gpin_raw']!r} at ({r['lat']}, {r['lng']})")
            for r in conn.execute(
                "SELECT p.id, p.source_permit_id, p.address, p.lat, p.lng, json_extract(p.raw_json, '$.GPIN') AS gpin_raw, "
                "x.method, x.gpin, x.alt_gpin FROM permit_parcel_effective e JOIN permit_parcel x ON x.permit_id = e.permit_id "
                "JOIN permits p ON p.id = e.permit_id WHERE e.fresh = 1 AND e.disputed = 1 AND e.review_verdict IS NULL")]
    rows += [("decision", f"{r['decision_key']}#{r['seq']}", r["method"], r["gpin"], r["alt_gpin"],
              f"{r['raw_text']} gpin10={r['gpin10']} at ({r['lat']}, {r['lng']})")
             for r in conn.execute(
                 "SELECT a.decision_key, a.seq, a.raw_text, a.gpin10, a.lat, a.lng, x.method, x.gpin, x.alt_gpin "
                 "FROM decision_parcel_effective e JOIN decision_parcel x ON x.decision_key = e.decision_key "
                 "AND x.seq = e.seq JOIN land_use_decision_addresses a ON a.decision_key = e.decision_key "
                 "AND a.seq = e.seq WHERE e.fresh = 1 AND e.disputed = 1 AND e.review_verdict IS NULL")]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(REVIEW_COLUMNS)
        w.writerows([list(r) + ["", "", ""] for r in rows])
    return len(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Match VB permits and decision addresses to parcels")
    p.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--reports-dir", type=Path, default=BASE / "reports")
    args = p.parse_args(argv)
    ts = db_module.now_iso()
    try:
        with db_module.db() as conn:
            load = conn.execute("SELECT load_id FROM parcel_loads WHERE is_active = 1").fetchone()
            if load is None:
                print("[FATAL] no active parcel load: run scripts/load_parcels.py", file=sys.stderr)
                return 1
            index = load_index(conn, load["load_id"])
            print(f"parcel load {load['load_id']}: {len(index.rows)} parcels")
            pm = match_permits(conn, index, load["load_id"], args.all, ts)
            dm = match_decisions(conn, index, load["load_id"], args.all, ts)
            print(f"permits:   {dict(sorted(pm.items()))}")
            print(f"decisions: {dict(sorted(dm.items()))}")
            untagged = conn.execute("SELECT COUNT(*) FROM permit_parcel_effective e JOIN permits p ON p.id = e.permit_id "
                                    "WHERE p.locality = 'virginia_beach' AND e.fresh = 0").fetchone()[0]
            untagged += conn.execute("SELECT COUNT(*) FROM decision_parcel_effective WHERE fresh = 0").fetchone()[0]
            if untagged:
                raise SystemExit(f"[FATAL] {untagged} row(s) still unmatched -- rolled back")
            if args.dry_run:
                raise _DryRun
            n = write_review(conn, args.reports_dir / "parcel_review.csv")
            print(f"0 rows unmatched; {n} disputed match(es) await review -> {args.reports_dir / 'parcel_review.csv'}")
    except _DryRun:
        print("--dry-run: rolled back, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
