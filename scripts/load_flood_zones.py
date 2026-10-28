"""Load FEMA National Flood Hazard Layer polygons (S_FLD_HAZ_AR) for one
DFIRM -- Virginia Beach is 515531 -- into flood_zones, as a new revision in
flood_zone_loads, and pre-build the map's display grid cells.

Source: FEMA NFHL MapServer layer 28, where=DFIRM_ID='<id>': count and
OBJECTID list, then features by ID batch (f=geojson, outSR=4326), every ID
verified. The layer is checked first (polygon geometry + FLD_ZONE
field) so a wrong layer number fails loudly. Ported from the PostGIS
pipeline's scripts/load_fema_nfhl.py (branch postgis-pipeline).

Revisions:
  * content_hash (SHA-256 of the canonical feature list) identifies the
    dataset independently of FEMA's vintage labels. Same hash as the active
    load => nothing to do.
  * Activation is one transaction, in an order the partial unique index
    (one active load per DFIRM) never trips on:
      UPDATE ... SET is_active = 0 WHERE dfirm_id = ? AND is_active = 1
      INSERT new load with is_active = 1, then its zones and cells.
    Older loads' rows are kept, so a bad load can be undone by re-activating
    the previous one.
  * Display guard: cells are built BEFORE activation. If any cell is still
    over MAX_FLOOD_CELL_BYTES at the maximum simplification, the load is
    stored INACTIVE, the previous load keeps serving, and this exits 2.
  * Zero features => abort, nothing written.

After a new load is activated every VB permit reads as 'untagged' until
scripts/tag_flood_zones.py runs.

Usage:
  python scripts/load_flood_zones.py --dry-run
  python scripts/load_flood_zones.py
  python scripts/load_flood_zones.py --file nfhl_515531.geojson   # a downloaded FeatureCollection
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import arcgis, flood  # noqa: E402
from ingest.common import fetch_json_retry  # noqa: E402
from shapely.geometry import mapping  # noqa: E402

# checked live 2026-10-28: the older /gis/nfhl/rest/... path now answers 404
DEFAULT_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
DEFAULT_DFIRM = "515531"  # live NFHL id for Virginia Beach (not the "51810C" panel-style id)
RUN_LOCALITY = "fema_nfhl"  # ingestion_runs.locality for flood loads

fetch_json = fetch_json_retry  # indirection so tests can stub the network


class LoadError(Exception):
    pass


def check_layer(url: str) -> dict:
    meta = fetch_json(url, {"f": "json"})
    fields = {f.get("name", "").upper() for f in meta.get("fields", [])}
    if meta.get("geometryType") != "esriGeometryPolygon" or "FLD_ZONE" not in fields:
        raise LoadError(f"{url} does not look like NFHL S_FLD_HAZ_AR (geometryType="
                        f"{meta.get('geometryType')!r}, has FLD_ZONE={'FLD_ZONE' in fields})")
    return meta


def fetch_features(url: str, dfirm: str, page_size: int, oid_field: str) -> list[dict]:
    """Every polygon of one DFIRM, complete or not at all (ingest/arcgis.py)."""
    try:
        return arcgis.fetch_all_by_ids(fetch_json, url, f"DFIRM_ID = '{dfirm}'", batch=min(page_size, 100),
                                       oid_field=oid_field, label=f"DFIRM {dfirm}")
    except arcgis.IncompleteRead as e:
        raise LoadError(str(e)) from None


def _prop(props: dict, name: str):
    for k, v in props.items():
        if k.upper() == name:
            s = None if v is None else str(v).strip()
            return s or None
    return None


def normalize(raw: list[dict], stats: Counter) -> list[dict]:
    out = []
    for f in raw:
        geom = f.get("geometry")
        if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
            stats["skipped_no_polygon_geometry"] += 1
            continue
        props = f.get("properties") or {}
        out.append({"fld_zone": (_prop(props, "FLD_ZONE") or "").upper() or None,
                    "zone_subty": _prop(props, "ZONE_SUBTY"), "sfha_tf": _prop(props, "SFHA_TF"),
                    "version_id": _prop(props, "VERSION_ID"), "geometry": geom})
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load FEMA NFHL flood zones into permits.db")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--dfirm", default=DEFAULT_DFIRM)
    p.add_argument("--file", type=Path, help="Load a local GeoJSON FeatureCollection instead of the service")
    p.add_argument("--page-size", type=int, default=200)
    p.add_argument("--max-cell-bytes", type=int, default=flood.MAX_FLOOD_CELL_BYTES)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if not re.fullmatch(r"[0-9A-Za-z]{4,12}", args.dfirm):
        print(f"[FATAL] invalid --dfirm {args.dfirm!r}", file=sys.stderr)
        return 1
    dfirm = args.dfirm.upper()
    stats: Counter = Counter()
    source = str(args.file) if args.file else args.url

    try:
        if args.file:
            raw = json.loads(args.file.read_text(encoding="utf-8")).get("features") or []
        else:
            meta = check_layer(args.url)
            page = min(args.page_size, int(meta.get("maxRecordCount") or 1000))
            raw = fetch_features(args.url, dfirm, page, meta.get("objectIdField") or "OBJECTID")
        features = normalize(raw, stats)
        if not features:
            raise LoadError("no polygon features fetched; nothing written")
        zones = []
        for i, f in enumerate(features):
            z = flood.zone_from_feature(i, f)
            if z is None:
                stats["dropped_empty_after_make_valid"] += 1
                continue
            zones.append((z, f))
        h = flood.content_hash(features)
        vintages = Counter(f["version_id"] for f in features if f["version_id"])
        vintage = vintages.most_common(1)[0][0] if vintages else None
        cells, tol, display_ok = flood.fit_display_cells([z for z, _ in zones], args.max_cell_bytes)
    except (LoadError, RuntimeError, ValueError, OSError) as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        if not args.dry_run:
            _record_run(dfirm, "failed", 0, 0, str(e))
        return 1

    cell_sizes = sorted((len(b) for b in cells.values()), reverse=True)
    zone_mix = Counter(z.flood_class for z, _ in zones)
    print(f"{source}: DFIRM {dfirm}, {len(raw)} features fetched, {len(zones)} polygons kept")
    print(f"  content_hash={h[:16]}...  vintage={vintage}  zone mix: {dict(sorted(zone_mix.items()))}")
    print(f"  display: {len(cells)} cells, largest {cell_sizes[0] if cell_sizes else 0} bytes, "
          f"total {sum(cell_sizes)} bytes, tolerance {tol}, limit {args.max_cell_bytes} -> "
          f"{'OK' if display_ok else 'OVER LIMIT'}")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    if args.dry_run:
        print("--dry-run: nothing written")
        return 0 if display_ok else 2

    try:
        with db_module.db() as conn:
            active = conn.execute("SELECT load_id, content_hash FROM flood_zone_loads "
                                  "WHERE dfirm_id = ? AND is_active = 1", (dfirm,)).fetchone()
            if active and active["content_hash"] == h:
                print(f"Unchanged: identical to active load {active['load_id']}; nothing written")
                _record_run_conn(conn, dfirm, "success", len(raw), 0, "unchanged")
                return 0
            if display_ok:
                conn.execute("UPDATE flood_zone_loads SET is_active = 0 WHERE dfirm_id = ? AND is_active = 1", (dfirm,))
            cur = conn.execute(
                """INSERT INTO flood_zone_loads (dfirm_id, source_url, source_vintage, content_hash, feature_count,
                       display_bytes, max_cell_bytes, simplify_tolerance, display_ok, loaded_at, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (dfirm, source, vintage, h, len(zones), sum(cell_sizes), cell_sizes[0] if cell_sizes else 0,
                 tol, int(display_ok), db_module.now_iso(), int(display_ok)))
            load_id = cur.lastrowid
            conn.executemany(
                """INSERT INTO flood_zones (load_id, fld_zone, zone_subty, sfha_tf, flood_class, geom_geojson,
                       minx, miny, maxx, maxy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(load_id, z.fld_zone, z.zone_subty, z.sfha_tf, z.flood_class,
                  json.dumps(mapping(z.geom), separators=(",", ":")), *z.geom.bounds) for z, _ in zones])
            conn.executemany(
                "INSERT INTO flood_display_cells (load_id, cell_x, cell_y, geojson, bytes, etag) VALUES (?, ?, ?, ?, ?, ?)",
                [(load_id, cx, cy, payload, len(payload), flood.etag_of(payload)) for (cx, cy), payload in cells.items()])
            _record_run_conn(conn, dfirm, "success" if display_ok else "failed", len(raw), len(zones),
                             None if display_ok else "display cells over limit; load stored inactive")
    except Exception as e:  # noqa: BLE001 - transaction rolled back by db()
        print(f"[FATAL] write failed, nothing changed: {e}", file=sys.stderr)
        _record_run(dfirm, "failed", len(raw), 0, str(e))
        return 1

    if not display_ok:
        over = [k for k, b in cells.items() if len(b) > args.max_cell_bytes]
        print(f"[FATAL] load {load_id} stored INACTIVE: {len(over)} display cell(s) exceed "
              f"{args.max_cell_bytes} bytes even at tolerance {tol}: {sorted(over)[:10]}. "
              "The previous active load (if any) keeps serving.", file=sys.stderr)
        return 2
    print(f"Activated load {load_id}. Run scripts/tag_flood_zones.py next (permits read as 'untagged' until then).")
    return 0


def _record_run_conn(conn, dfirm, status, fetched, inserted, error):
    ts = db_module.now_iso()
    conn.execute(
        """INSERT INTO ingestion_runs (locality, started_at, completed_at, status, fetched_count, inserted_count,
               since_date, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (RUN_LOCALITY, ts, ts, status, fetched, inserted, dfirm, error))


def _record_run(dfirm, status, fetched, inserted, error):
    try:
        with db_module.db() as conn:
            _record_run_conn(conn, dfirm, status, fetched, inserted, error)
    except Exception:  # noqa: BLE001 - logging must never mask the real failure
        pass


if __name__ == "__main__":
    sys.exit(main())
