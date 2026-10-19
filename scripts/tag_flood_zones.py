"""Tag permits with their FEMA flood class (Stage 1 flood exposure).

Tags every in-scope permit whose permit_flood_effective.fresh is false -- no
tag yet, a tag from another flood load (a dataset revision, detected by
load_id, independent of vintage labels), or coordinates that moved since
tagging. Freshness is the same view the API reads, so "needs tagging" and
"shows as untagged" can never disagree. Values-only writes to permit_flood;
dry-run prints the before -> after transitions without writing.

Usage:
  python scripts/tag_flood_zones.py --dry-run
  python scripts/tag_flood_zones.py
  python scripts/tag_flood_zones.py --all     # recompute every in-scope permit
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import flood  # noqa: E402
from shapely.geometry import shape  # noqa: E402

UPSERT = """
    INSERT INTO permit_flood (permit_id, load_id, tagged_lat, tagged_lng, fld_zone, zone_subty, sfha, flood_class, tagged_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(permit_id) DO UPDATE SET
        load_id = excluded.load_id, tagged_lat = excluded.tagged_lat, tagged_lng = excluded.tagged_lng,
        fld_zone = excluded.fld_zone, zone_subty = excluded.zone_subty, sfha = excluded.sfha,
        flood_class = excluded.flood_class, tagged_at = excluded.tagged_at
"""


def class_counts(conn, locality: str) -> Counter:
    return Counter(dict(conn.execute(
        "SELECT e.effective_class, COUNT(*) FROM permits p JOIN permit_flood_effective e ON e.permit_id = p.id "
        "WHERE p.locality = ? GROUP BY e.effective_class", (locality,)).fetchall()))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Tag permits with FEMA flood classes")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--all", action="store_true", help="Recompute every in-scope permit, not just stale ones")
    args = p.parse_args(argv)

    with db_module.db() as conn:
        scopes = conn.execute(
            "SELECT m.locality, m.dfirm_id, l.load_id FROM flood_locality_dfirm m "
            "LEFT JOIN flood_zone_loads l ON l.dfirm_id = m.dfirm_id AND l.is_active = 1 ORDER BY m.locality"
        ).fetchall()
        for scope in scopes:
            locality, load_id = scope["locality"], scope["load_id"]
            if load_id is None:
                print(f"[FATAL] {locality}: no active flood load for DFIRM {scope['dfirm_id']}; "
                      "run scripts/load_flood_zones.py first", file=sys.stderr)
                return 1
            zones = [flood.Zone(r["id"], r["fld_zone"], r["zone_subty"], r["sfha_tf"], shape(json.loads(r["geom_geojson"])))
                     for r in conn.execute("SELECT id, fld_zone, zone_subty, sfha_tf, geom_geojson FROM flood_zones "
                                           "WHERE load_id = ?", (load_id,))]
            index = flood.FloodIndex(zones)
            before = class_counts(conn, locality)
            todo = conn.execute(
                "SELECT p.id, p.lat, p.lng, e.effective_class FROM permits p "
                "JOIN permit_flood_effective e ON e.permit_id = p.id "
                "WHERE p.locality = ? AND (e.fresh = 0 OR ?)", (locality, int(args.all))).fetchall()
            ts = db_module.now_iso()
            transitions: Counter = Counter()
            rows = []
            for r in todo:
                cls, zone = index.classify(r["lat"], r["lng"])
                transitions[f"{r['effective_class']} -> {cls}"] += 1
                rows.append((r["id"], load_id, r["lat"], r["lng"], zone.fld_zone if zone else None,
                             zone.zone_subty if zone else None, zone.sfha if zone else None, cls, ts))
            print(f"{locality}: load {load_id} ({len(zones)} polygons); {len(todo)} permit(s) to tag")
            for k, n in sorted(transitions.items(), key=lambda kv: -kv[1]):
                print(f"  {n:>6}  {k}")
            if args.dry_run:
                print("--dry-run: nothing written")
                continue
            conn.executemany(UPSERT, rows)
            after = class_counts(conn, locality)
            total = conn.execute("SELECT COUNT(*) FROM permits WHERE locality = ?", (locality,)).fetchone()[0]
            print(f"  before: {dict(sorted(before.items()))}")
            print(f"  after:  {dict(sorted(after.items()))}")
            if sum(after.values()) != total or after.get("untagged", 0):
                raise SystemExit(f"[FATAL] {locality}: classes sum to {sum(after.values())} of {total} permits, "
                                 f"{after.get('untagged', 0)} still untagged -- rolled back")
            print(f"  classes sum to {total} permits; 0 untagged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
