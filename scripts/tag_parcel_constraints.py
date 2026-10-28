"""Measure parcels against the active site-constraint layers (screening).

For each parcel of the active parcel load that a lead points at (filings,
Planning Commission items' GPINs, permits' parcels; --all: every parcel), and
each layer with an active load: acres and share inside polygon layers; length
inside and distance to line layers (ingest/constraints.py). Rows are keyed by
(parcel load, constraint load), so a new load of either reads as not measured
until this runs again. Only parcels that touch a layer get a row for it.

Usage:
  python scripts/tag_parcel_constraints.py [--all] [--layer KEY ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import constraints as C  # noqa: E402
from shapely.geometry import shape  # noqa: E402

LEAD_PARCELS_SQL = """
    SELECT display_gpin FROM permit_parcel_effective WHERE display_gpin IS NOT NULL
    UNION SELECT g.value FROM filings f JOIN filing_loads l ON l.load_id = f.load_id AND l.is_active = 1,
          json_each(f.gpins_json) g
    UNION SELECT p.gpin FROM pc_items i, json_each(i.gpins_json) g
          JOIN parcels p ON p.load_id = ? AND (p.gpin_digits = g.value OR (length(g.value) = 10 AND p.gpin10 = g.value))
    UNION SELECT gpin FROM board_action_parcels"""


class _DryRun(Exception):
    pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Measure parcels against site-constraint layers")
    p.add_argument("--all", action="store_true", help="every parcel of the active load, not just lead parcels")
    p.add_argument("--layer", action="append", choices=sorted(C.LAYERS), help="only these layers (default: all)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    ts = db_module.now_iso()
    try:
        with db_module.db() as conn:
            pl = conn.execute("SELECT load_id FROM parcel_loads WHERE is_active = 1").fetchone()
            if pl is None:
                print("[FATAL] needs an active parcel load (load_parcels.py)", file=sys.stderr)
                return 1
            pl = pl["load_id"]
            scope = "" if args.all else f" AND gpin IN ({LEAD_PARCELS_SQL})"
            parcels = [(r["gpin"], shape(json.loads(r["geom_geojson"]))) for r in conn.execute(
                f"SELECT gpin, geom_geojson FROM parcels WHERE load_id = ? {scope} ORDER BY gpin",
                (pl, pl) if scope else (pl,))]
            print(f"parcel load {pl}: {len(parcels)} parcel(s) in scope{' (all)' if args.all else ' (lead parcels)'}")
            touched: Counter = Counter()
            for key, layer in sorted(C.LAYERS.items()):
                if args.layer and key not in args.layer:
                    continue
                got = C.load_layer_index(conn, layer)
                if got is None:
                    print(f"  {key}: no active load (load_constraints.py) -- not measured")
                    continue
                cl, index = got
                conn.execute("DELETE FROM parcel_constraints WHERE parcel_load_id = ? AND constraint_load_id = ?",
                             (pl, cl))
                rows = []
                for gpin, geom in parcels:
                    m = index.measure(geom)
                    if m is None:
                        continue
                    rows.append((pl, cl, gpin, key, m["overlap_sqft"], m["share"], m["line_length_ft"],
                                 m["distance_ft"], json.dumps(m["detail"]), ts))
                conn.executemany(
                    "INSERT INTO parcel_constraints (parcel_load_id, constraint_load_id, gpin, layer_key, "
                    "overlap_sqft, share, line_length_ft, distance_ft, detail_json, computed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                touched[key] = len(rows)
                print(f"  {key}: {len(rows)} parcel(s) touch the layer")
            if args.dry_run:
                raise _DryRun
    except _DryRun:
        print("--dry-run: rolled back, nothing written")
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
