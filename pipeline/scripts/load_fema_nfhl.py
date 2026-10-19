#!/usr/bin/env python3
"""
load_fema_nfhl.py

Loads FEMA National Flood Hazard Layer (NFHL) flood hazard polygons
(S_FLD_HAZ_AR) for one DFIRM -- Virginia Beach is 51810C -- into
fema_flood_zones.

Source: NFHL_LAYER_URL (default: FEMA's public NFHL MapServer layer 28),
queried with where=DFIRM_ID='<id>', f=geojson, outSR=4326, paged by
resultOffset. Before fetching, the layer's metadata is checked: it must be a
polygon layer with a FLD_ZONE field, so a wrong layer number fails loudly
instead of loading junk.

Each polygon goes through ST_MakeValid, then ST_CollectionExtract(..., 3)
(MakeValid can return a GeometryCollection with stray lines/points) and
ST_Multi to fit the MultiPolygon column. Polygons that are empty after that
are dropped and counted.

The load replaces that DFIRM's rows in one transaction: the old rows are
deleted and the new ones inserted, then committed together, so
vw_permit_leads never sees a half-loaded map. If nothing is fetched, nothing
is deleted.

Usage:
  python scripts/load_fema_nfhl.py                    # live FEMA service
  python scripts/load_fema_nfhl.py --dry-run          # fetch + count only
  python scripts/load_fema_nfhl.py --file nfhl.geojson  # load a downloaded GeoJSON
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

import requests

try:  # optional: load .env for local runs
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

DEFAULT_URL = os.getenv(
    "NFHL_LAYER_URL",
    "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28",
).rstrip("/")
DEFAULT_DFIRM = "51810C"
_HEADERS = {"User-Agent": "permitiq/0.1"}

INSERT_ZONE = """
    INSERT INTO fema_flood_zones (dfirm_id, fld_zone, zone_subty, sfha_tf, source_vintage, geom)
    SELECT %(dfirm_id)s, %(fld_zone)s, %(zone_subty)s, %(sfha_tf)s, %(source_vintage)s, g
    FROM (
        SELECT ST_Multi(ST_CollectionExtract(
                   ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%(geom)s), 4326)), 3)) AS g
    ) s
    WHERE NOT ST_IsEmpty(g)
"""


def _get_json(url: str, params: dict, retries: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=120)
            r.raise_for_status()
            data = r.json()
            # ArcGIS reports many failures as HTTP 200 with an "error" body.
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"request failed after {retries} attempts: {last}")


def check_layer(url: str) -> dict:
    meta = _get_json(url, {"f": "json"})
    fields = {f["name"].upper() for f in meta.get("fields", [])}
    if meta.get("geometryType") != "esriGeometryPolygon" or "FLD_ZONE" not in fields:
        raise RuntimeError(
            f"{url} does not look like S_FLD_HAZ_AR (name={meta.get('name')!r}, "
            f"geometryType={meta.get('geometryType')!r}, has FLD_ZONE={'FLD_ZONE' in fields})"
        )
    return meta


def fetch_pages(url: str, dfirm: str, page_size: int, oid_field: str):
    offset = 0
    while True:
        data = _get_json(
            f"{url}/query",
            {
                "where": f"DFIRM_ID = '{dfirm}'",
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": 4326,
                "orderByFields": oid_field,
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "f": "geojson",
            },
        )
        feats = data.get("features") or []
        if not feats:
            return
        yield feats
        offset += len(feats)
        # ArcGIS GeoJSON puts the flag under "properties"; JSON puts it top-level.
        exceeded = data.get("exceededTransferLimit") or (data.get("properties") or {}).get(
            "exceededTransferLimit"
        )
        if len(feats) < page_size and not exceeded:
            return


def read_file(path: Path, page_size: int):
    feats = json.loads(path.read_text()).get("features") or []
    for i in range(0, len(feats), page_size):
        yield feats[i : i + page_size]


def _prop(props: dict, name: str):
    """Case-insensitive property lookup, blank -> None."""
    for k, v in props.items():
        if k.upper() == name:
            if v is None:
                return None
            s = str(v).strip()
            return s or None
    return None


def to_row(feature: dict, dfirm: str, vintage: str | None) -> dict | None:
    geom = feature.get("geometry")
    if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    props = feature.get("properties") or {}
    return {
        "dfirm_id": dfirm,
        "fld_zone": (_prop(props, "FLD_ZONE") or "").upper() or None,
        "zone_subty": _prop(props, "ZONE_SUBTY"),
        "sfha_tf": _prop(props, "SFHA_TF"),
        "source_vintage": vintage or _prop(props, "VERSION_ID") or f"loaded {date.today()}",
        "geom": json.dumps(geom),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Load FEMA NFHL flood zones into PostGIS")
    p.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="Postgres DSN (default: $DATABASE_URL)")
    p.add_argument("--url", default=DEFAULT_URL, help="NFHL S_FLD_HAZ_AR layer URL")
    p.add_argument("--dfirm", default=DEFAULT_DFIRM, help="DFIRM_ID to load (default 51810C, Virginia Beach)")
    p.add_argument("--file", type=Path, help="Load a local GeoJSON FeatureCollection instead of the service")
    p.add_argument("--vintage", help="source_vintage label (default: VERSION_ID, else load date)")
    p.add_argument("--page-size", type=int, default=200, help="Features per request (polygons are large)")
    p.add_argument("--dry-run", action="store_true", help="Fetch and count only; no DB")
    args = p.parse_args()

    if not re.fullmatch(r"[0-9A-Za-z]{4,12}", args.dfirm):
        sys.exit(f"[FATAL] invalid --dfirm {args.dfirm!r}")
    dfirm = args.dfirm.upper()

    if args.file:
        pages = read_file(args.file, args.page_size)
        print(f"Reading {args.file}")
    else:
        try:
            meta = check_layer(args.url)
        except RuntimeError as e:
            print(f"[FATAL] {e}", file=sys.stderr)
            return 1
        page_size = min(args.page_size, int(meta.get("maxRecordCount") or 1000))
        pages = fetch_pages(args.url, dfirm, page_size, meta.get("objectIdField") or "OBJECTID")
        print(f"Fetching {args.url}  DFIRM_ID={dfirm}  page_size={page_size}")

    stats: Counter = Counter()
    conn = cur = None
    if not args.dry_run:
        if not args.dsn:
            sys.exit("[FATAL] no DSN: set DATABASE_URL or pass --dsn (or use --dry-run)")
        import psycopg

        conn = psycopg.connect(args.dsn)
        cur = conn.cursor()

    try:
        deleted = False
        for feats in pages:
            rows = []
            for f in feats:
                stats["fetched"] += 1
                row = to_row(f, dfirm, args.vintage)
                if row is None:
                    stats["skipped_no_polygon_geometry"] += 1
                    continue
                stats[f"zone:{row['fld_zone']}"] += 1
                rows.append(row)
            if args.dry_run or not rows:
                continue
            if not deleted:
                cur.execute("DELETE FROM fema_flood_zones WHERE dfirm_id = %s", (dfirm,))
                stats["replaced_old_rows"] = cur.rowcount
                deleted = True
            cur.executemany(INSERT_ZONE, rows)
            stats["candidates"] += len(rows)
            print(f"  fetched {stats['fetched']}")

        if conn:
            if stats["candidates"] == 0:
                conn.rollback()
                print("[FATAL] no polygons fetched; existing rows left untouched", file=sys.stderr)
                return 1
            cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE NOT ST_IsValid(geom)) "
                "FROM fema_flood_zones WHERE dfirm_id = %s",
                (dfirm,),
            )
            stored, invalid = cur.fetchone()
            stats["stored"] = stored
            stats["dropped_empty_after_makevalid"] = stats["candidates"] - stored
            if invalid:
                conn.rollback()
                print(f"[FATAL] {invalid} geometries still invalid after ST_MakeValid; rolled back",
                      file=sys.stderr)
                return 1
            conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[FATAL] load failed, rolled back: {e}", file=sys.stderr)
        return 1
    finally:
        if conn:
            conn.close()

    print("Done.")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
