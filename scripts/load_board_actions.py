"""Load the city's board-action layers -- Chesapeake Bay Preservation Area Board
(cbpa), Wetlands Board (wetlands_board), Historical Review Board (hrb) -- as
each parcel's history with those boards.

Same schema and rules as the filings layer (ingest/filings.read_and_link):
complete reads only, one row per Accela record, parcels inside each boundary,
records without a date counted and not stored. All dates are kept (history).

Usage:
  python scripts/load_board_actions.py --all [--dry-run]
  python scripts/load_board_actions.py --layer cbpa
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import arcgis, constraints as C, filings as F  # noqa: E402
from ingest.common import fetch_json_retry  # noqa: E402

fetch_json = fetch_json_retry  # indirection so tests can stub the network


def load_one(conn, key: str, dry_run: bool) -> str:
    url, label = C.BOARD_LAYERS[key]
    raw, merged, stats, plid = F.read_and_link(fetch_json, conn, url, "1=1", key)
    h = F.content_hash(merged)
    print(f"{key}: {len(raw)} records -> {len(merged)} actions; parcels for "
          f"{sum(1 for m in merged if m['gpins'])}; {dict(stats)}")
    if dry_run or not merged:
        return "dry-run" if dry_run else "empty"
    active = conn.execute("SELECT load_id, content_hash, parcel_load_id FROM board_action_loads WHERE layer_key = ? "
                          "AND is_active = 1", (key,)).fetchone()
    if active and active["content_hash"] == h and active["parcel_load_id"] == plid:
        print(f"  unchanged: identical to active load {active['load_id']}")
        return "unchanged"
    conn.execute("UPDATE board_action_loads SET is_active = 0 WHERE layer_key = ? AND is_active = 1", (key,))
    load_id = conn.execute(
        "INSERT INTO board_action_loads (layer_key, source_url, where_clause, expected_count, received_count, "
        "unique_ids, no_date, actions, content_hash, max_date_sub, parcel_load_id, loaded_at, is_active) "
        "VALUES (?, ?, '1=1', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (key, url, len(raw), len(raw), len(raw), stats["no_date"], len(merged), h,
         max(m["date_sub"] for m in merged), plid, db_module.now_iso())).lastrowid
    conn.executemany(
        "INSERT INTO board_actions (load_id, layer_key, pln_id, objectids_json, date_sub, pln_type, pln_file_no, "
        "proj_name, appl_name, deeplink, geom_geojson, minx, miny, maxx, maxy, lat, lng, gpins_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(load_id, key, m["pln_id"], json.dumps(m["objectids"]), m["date_sub"], m["pln_type"], m["pln_file_no"],
          m["proj_name"], m["appl_name"], m["deeplink"], F.geojson(m["geom"]), *m["bounds"], m["lat"], m["lng"],
          json.dumps(m["gpins"])) for m in merged])
    conn.executemany("INSERT INTO board_action_parcels (load_id, pln_id, gpin) VALUES (?, ?, ?)",
                     [(load_id, m["pln_id"], g) for m in merged for g in m["gpins"]])
    print(f"  activated load {load_id}")
    return "loaded"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load board-action history layers")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--layer", action="append", choices=sorted(C.BOARD_LAYERS))
    g.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    failed = []
    for key in (sorted(C.BOARD_LAYERS) if args.all else args.layer):
        try:
            with db_module.db() as conn:
                load_one(conn, key, args.dry_run)
        except (arcgis.IncompleteRead, RuntimeError, ValueError) as e:
            failed.append(key)
            print(f"[error] {key}: {e}; nothing written for this layer", file=sys.stderr)
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
