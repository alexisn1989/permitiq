"""Load land-use applications as filed (Virginia Beach "Planning Commission and
City Council Actions" layer) -- the earliest lead signal.

Reads every record filed since --since (default 2023-01-01, the start of the
city's permit data) by OBJECTID with a completeness check (ingest/arcgis.py),
merges polygons that share an Accela record, finds the parcels inside each
boundary (active parcel load), and activates the new load only when
complete. Records without a filing date are counted, never loaded. The
layer's "Approval Date" field is not used: it is empty (2 values in 6,463).

Usage:
  python scripts/load_filings.py --inspect
  python scripts/load_filings.py --dry-run
  python scripts/load_filings.py --since 2023-01-01
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import arcgis, filings as F  # noqa: E402
from ingest.common import fetch_json_retry  # noqa: E402

fetch_json = fetch_json_retry  # indirection so tests can stub the network


def inspect(url: str, since: str) -> int:
    meta = fetch_json(url, {"f": "json"})
    q = f"{url}/query"
    total = fetch_json(q, {"where": "1=1", "returnCountOnly": "true", "f": "json"})["count"]
    window = fetch_json(q, {"where": F.where_since(since), "returnCountOnly": "true", "f": "json"})["count"]
    no_date = fetch_json(q, {"where": "DATE_SUB IS NULL", "returnCountOnly": "true", "f": "json"})["count"]
    print(f"{url}\n  name={meta.get('name')!r} geometryType={meta.get('geometryType')} "
          f"maxRecordCount={meta.get('maxRecordCount')}")
    print(f"  fields: {[f.get('name') for f in meta.get('fields', [])]}")
    print(f"  records: {total} in all; {window} filed since {since}; {no_date} without a filing date")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load land-use applications as filed")
    p.add_argument("--url", default=F.LAYER_URL)
    p.add_argument("--since", default="2023-01-01")
    p.add_argument("--inspect", action="store_true", help="print layer metadata and counts; write nothing")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        p.error("--since must be YYYY-MM-DD")
    if args.inspect:
        try:
            return inspect(args.url, args.since)
        except (RuntimeError, KeyError) as e:
            print(f"[FATAL] {e}", file=sys.stderr)
            return 1
    where = F.where_since(args.since)
    with db_module.db() as conn:
        try:
            raw, merged, stats, plid = F.read_and_link(fetch_json, conn, args.url, where, "filings layer")
        except (arcgis.IncompleteRead, RuntimeError, ValueError) as e:
            print(f"[FATAL] incomplete or failed read, nothing written: {e}", file=sys.stderr)
            return 2 if isinstance(e, arcgis.IncompleteRead) else 1
        if not merged:
            print("[FATAL] no filings with a date in the window; nothing written", file=sys.stderr)
            return 1
        h = F.content_hash(merged)
        print(f"{args.url}: {len(raw)} records since {args.since} -> {len(merged)} filings; {dict(stats)}")
        if plid is None:
            print("  [warn] no active parcel load: filings are stored without parcels (run load_parcels.py first)")
        print(f"  parcels found for {sum(1 for f in merged if f['gpins'])} of {len(merged)} filings")
        if args.dry_run:
            print("--dry-run: nothing written")
            return 0
        active = conn.execute("SELECT load_id, content_hash, parcel_load_id FROM filing_loads WHERE is_active = 1"
                              ).fetchone()
        if active and active["content_hash"] == h and active["parcel_load_id"] == plid:
            print(f"Unchanged: identical to active filing load {active['load_id']}; nothing written")
            return 0
        conn.execute("UPDATE filing_loads SET is_active = 0 WHERE is_active = 1")
        load_id = conn.execute(
            "INSERT INTO filing_loads (source_url, where_clause, expected_count, received_count, unique_ids, no_date, "
            "filings, content_hash, max_date_sub, parcel_load_id, loaded_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (args.url, where, len(raw), len(raw), len(raw), stats["no_date"], len(merged), h,
             max(f["date_sub"] for f in merged), plid, db_module.now_iso())).lastrowid
        conn.executemany(
            "INSERT INTO filings (load_id, pln_id, objectids_json, date_sub, pln_type, pln_file_no, proj_name, "
            "appl_name, deeplink, geom_geojson, minx, miny, maxx, maxy, lat, lng, gpins_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(load_id, f["pln_id"], json.dumps(f["objectids"]), f["date_sub"], f["pln_type"], f["pln_file_no"],
              f["proj_name"], f["appl_name"], f["deeplink"], F.geojson(f["geom"]), *f["bounds"], f["lat"], f["lng"],
              json.dumps(f["gpins"])) for f in merged])
    print(f"Activated filing load {load_id}: {len(merged)} filings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
