"""Load site-constraint layers from the city's GIS (ingest/constraints.py).

Each layer is read complete or not at all (count and OBJECTID set must match,
ingest/arcgis.py) and stored as a new load, activated only when complete.
Only each layer's allowlisted attributes are kept. An unchanged layer (same
content hash as the active load) writes nothing.

After a load, run scripts/tag_parcel_constraints.py: parcels measured against
an older load read as stale until then.

Usage:
  python scripts/load_constraints.py --inspect
  python scripts/load_constraints.py --all [--dry-run]
  python scripts/load_constraints.py --layer rpa --layer historic_overlay
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import arcgis, constraints as C  # noqa: E402
from ingest.common import fetch_json_retry  # noqa: E402

fetch_json = fetch_json_retry  # indirection so tests can stub the network
EXPECTED_GEOMETRY = {"polygon_share": "esriGeometryPolygon", "line_crossing": "esriGeometryPolyline"}


def check_layer(layer: C.Layer) -> dict:
    meta = fetch_json(layer.url, {"f": "json"})
    if meta.get("geometryType") != EXPECTED_GEOMETRY[layer.kind]:
        raise C_Error(f"{layer.key}: {layer.url} has geometryType {meta.get('geometryType')!r}, expected "
                      f"{EXPECTED_GEOMETRY[layer.kind]} -- wrong layer number?")
    fields = {f.get("name", "").upper() for f in meta.get("fields", [])}
    missing = [a for a in layer.attrs if a.upper() not in fields]
    if missing:
        raise C_Error(f"{layer.key}: fields {missing} are missing from the source")
    return meta


class C_Error(Exception):
    pass


def load_one(conn, layer: C.Layer, dry_run: bool) -> str:
    meta = check_layer(layer)
    stats: Counter = Counter()
    raw = arcgis.fetch_all_by_ids(fetch_json, layer.url, "1=1", label=layer.key,
                                  oid_field=meta.get("objectIdField") or "OBJECTID")
    rows = C.normalize(layer, raw, stats)
    if not rows:
        raise C_Error(f"{layer.key}: no usable features; nothing written")
    h = C.content_hash(rows)
    print(f"{layer.key}: {len(raw)} features read, {len(rows)} usable {dict(stats) or ''}")
    if dry_run:
        return "dry-run"
    active = conn.execute("SELECT load_id, content_hash FROM constraint_loads WHERE layer_key = ? AND is_active = 1",
                          (layer.key,)).fetchone()
    if active and active["content_hash"] == h:
        print(f"  unchanged: identical to active load {active['load_id']}")
        return "unchanged"
    conn.execute("UPDATE constraint_loads SET is_active = 0 WHERE layer_key = ? AND is_active = 1", (layer.key,))
    load_id = conn.execute(
        "INSERT INTO constraint_loads (layer_key, source_url, where_clause, expected_count, received_count, "
        "unique_ids, features, content_hash, loaded_at, is_active) VALUES (?, ?, '1=1', ?, ?, ?, ?, ?, ?, 1)",
        (layer.key, layer.url, len(raw), len(raw), len({arcgis.feature_id(f) for f in raw}), len(rows), h,
         db_module.now_iso())).lastrowid
    conn.executemany(
        "INSERT INTO constraint_features (load_id, feature_id, geom_geojson, minx, miny, maxx, maxy, attrs_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(load_id, r["feature_id"], C.geojson(r["geom"]), *r["geom"].bounds, json.dumps(r["attrs"])) for r in rows])
    print(f"  activated load {load_id}")
    return "loaded"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load site-constraint layers")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--layer", action="append", choices=sorted(C.LAYERS))
    g.add_argument("--all", action="store_true")
    g.add_argument("--inspect", action="store_true", help="layer metadata and counts; write nothing")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    keys = sorted(C.LAYERS) if (args.all or args.inspect) else args.layer
    if args.inspect:
        for k in keys:
            layer = C.LAYERS[k]
            try:
                meta = fetch_json(layer.url, {"f": "json"})
                n = fetch_json(f"{layer.url}/query", {"where": "1=1", "returnCountOnly": "true", "f": "json"})["count"]
            except (RuntimeError, KeyError) as e:
                print(f"{k}: [error] {e}")
                continue
            print(f"{k}: {meta.get('name')!r} {meta.get('geometryType')} {n} features; fields "
                  f"{[f.get('name') for f in meta.get('fields', [])]}")
        return 0
    failed = []
    for k in keys:
        try:
            with db_module.db() as conn:  # one transaction per layer: a failed layer leaves the others loaded
                load_one(conn, C.LAYERS[k], args.dry_run)
        except (arcgis.IncompleteRead, C_Error, RuntimeError, ValueError) as e:
            failed.append(k)
            print(f"[error] {k}: {e}; nothing written for this layer", file=sys.stderr)
    if failed:
        print(f"failed layers: {failed}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
