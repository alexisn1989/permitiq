"""Classify parcels by FEMA flood area shares (Stage 3; screening estimate).

For each parcel of the active parcel load referenced by a fresh permit or
decision parcel match (--all: every parcel), intersect it with the active
Virginia Beach flood load, resolve overlaps by Stage 1 precedence (VE/V > A >
shaded X > other mapped), and store the five area shares plus
parcel_flood_class (rules in ingest/parcels.py; min_share from
config/parcels.toml). Rows are keyed by (parcel load, flood load), so a new
load of either kind makes them stale automatically (parcel_flood_effective).

Usage:
  python scripts/tag_parcel_flood.py --dry-run
  python scripts/tag_parcel_flood.py
  python scripts/tag_parcel_flood.py --all
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import parcels as P  # noqa: E402
from shapely.geometry import shape  # noqa: E402

BASE = Path(__file__).resolve().parent.parent


class _DryRun(Exception):
    pass


def load_min_share(path: Path) -> float:
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    v = data.get("flood", {}).get("min_share", 0.01)
    if not isinstance(v, (int, float)) or not 0 < v < 0.5:
        raise ValueError(f"{path}: flood.min_share must be a number in (0, 0.5)")
    return float(v)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Parcel flood area shares (screening estimate)")
    p.add_argument("--config", type=Path, default=BASE / "config" / "parcels.toml")
    p.add_argument("--all", action="store_true", help="every parcel of the active load, not just referenced ones")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    try:
        min_share = load_min_share(args.config)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1
    ts = db_module.now_iso()
    try:
        with db_module.db() as conn:
            pl = conn.execute("SELECT load_id FROM parcel_loads WHERE is_active = 1").fetchone()
            fl = conn.execute("SELECT l.load_id FROM flood_zone_loads l JOIN flood_locality_dfirm m "
                              "ON m.dfirm_id = l.dfirm_id WHERE m.locality = 'virginia_beach' AND l.is_active = 1").fetchone()
            if pl is None or fl is None:
                print("[FATAL] needs an active parcel load (load_parcels.py) and an active flood load "
                      "(load_flood_zones.py)", file=sys.stderr)
                return 1
            pl, fl = pl["load_id"], fl["load_id"]
            zones = P.FloodShareIndex([(P.zone_class(r["fld_zone"], r["zone_subty"]), shape(json.loads(r["geom_geojson"])))
                                       for r in conn.execute("SELECT fld_zone, zone_subty, geom_geojson FROM flood_zones "
                                                             "WHERE load_id = ?", (fl,))])
            scope = "" if args.all else """ AND gpin IN (
                SELECT display_gpin FROM permit_parcel_effective WHERE display_gpin IS NOT NULL
                UNION SELECT alt_gpin FROM permit_parcel_effective WHERE alt_gpin IS NOT NULL
                UNION SELECT display_gpin FROM decision_parcel_effective WHERE display_gpin IS NOT NULL
                UNION SELECT alt_gpin FROM decision_parcel_effective WHERE alt_gpin IS NOT NULL)"""
            todo = conn.execute(
                f"""SELECT gpin, geom_geojson FROM parcels WHERE load_id = ? {scope}
                    AND gpin NOT IN (SELECT gpin FROM parcel_flood WHERE parcel_load_id = ? AND flood_load_id = ?
                                     AND min_share = ?)""", (pl, pl, fl, min_share)).fetchall()
            classes: Counter = Counter()
            rows = []
            for r in todo:
                s = zones.shares(shape(json.loads(r["geom_geojson"])))
                cls = P.parcel_flood_class(s, min_share)
                classes[cls] += 1
                rows.append((pl, fl, r["gpin"], s["sfha_v"], s["sfha_a"], s["shaded_x"], s["matched_non_sfha"],
                             s["unmapped"], min_share, cls, ts))
            print(f"parcel load {pl} x flood load {fl}: {len(rows)} parcel(s) classified (min_share {min_share})")
            for k, n in classes.most_common():
                print(f"  {n:>7}  {k}")
            if args.dry_run:
                raise _DryRun
            conn.executemany(
                """INSERT INTO parcel_flood (parcel_load_id, flood_load_id, gpin, share_sfha_v, share_sfha_a,
                       share_shaded_x, share_other_mapped, share_unmapped, min_share, parcel_flood_class, computed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(parcel_load_id, flood_load_id, gpin) DO UPDATE SET
                       share_sfha_v = excluded.share_sfha_v, share_sfha_a = excluded.share_sfha_a,
                       share_shaded_x = excluded.share_shaded_x, share_other_mapped = excluded.share_other_mapped,
                       share_unmapped = excluded.share_unmapped, min_share = excluded.min_share,
                       parcel_flood_class = excluded.parcel_flood_class, computed_at = excluded.computed_at""", rows)
    except _DryRun:
        print("--dry-run: rolled back, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
