"""Load Virginia Beach parcels (city Property_Information layer) into
parcels, as a new revision in parcel_loads (Stage 3).

Completeness is verified BEFORE anything is written. The city layer caps
responses (maxRecordCount, 2,000) and is paged, and a missing or duplicated
page would otherwise load silently:
  service  1. returnCountOnly -> expected count N; returnIdsOnly -> object-ID set S
           2. fetch by object-ID batches of <= min(maxRecordCount, 2000)
           3. received IDs must equal S exactly (none missing, none extra, none
              twice), received count = N = |S|, and a second returnCountOnly
              must still return N (the layer did not change mid-fetch)
  --file   requires --expected-count N (from --inspect); the file must hold
           exactly N features with N unique object IDs
Any failure exits 2 with the IDs listed; nothing is written and the previous
active load keeps serving. Distinct object IDs sharing one PAR_GPIN are
multi-part parcels: unioned, not an error.

Revisions: identical content hash to the active load => nothing written.
Activation deactivates the old load then inserts the new one in one
transaction (partial unique index: one active load). After a new load every
VB permit reads parcel_state 'untagged' until scripts/match_parcels.py runs.

The city's terms: data "as is", +/-2-5 ft, not valid for legal survey,
engineering design, or flood-zone determination.

Usage:
  python scripts/load_parcels.py --inspect
  python scripts/load_parcels.py --dry-run
  python scripts/load_parcels.py
  python scripts/load_parcels.py --file vb_parcels.geojson --expected-count 152345
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import parcels as P  # noqa: E402
from ingest.common import fetch_json_retry  # noqa: E402
from shapely.geometry import mapping  # noqa: E402

DEFAULT_URL = "https://geo.vbgov.com/mapservices/rest/services/Basemaps/Property_Information/MapServer/12"
PAGE_CAP = 2000
ID_BATCH = 200
RUN_LOCALITY = "vb_parcels"

fetch_json = fetch_json_retry  # indirection so tests can stub the network


class IncompleteLoad(Exception):
    pass


class LoadError(Exception):
    pass


def layer_meta(url: str) -> dict:
    meta = fetch_json(url, {"f": "json"})
    if meta.get("geometryType") != "esriGeometryPolygon":
        raise LoadError(f"{url}: geometryType {meta.get('geometryType')!r}, expected polygons")
    missing = P.missing_fields({f.get("name", "") for f in meta.get("fields", [])})
    if missing:
        raise LoadError(f"{url}: layer lacks {missing}; actual fields: "
                        f"{[f.get('name') for f in meta.get('fields', [])]}")
    return meta


def _count(url: str) -> int:
    return int(fetch_json(f"{url}/query", {"where": "1=1", "returnCountOnly": "true", "f": "json"})["count"])


def fetch_complete(url: str, meta: dict, page_size: int) -> tuple[list[dict], dict]:
    oid_field = meta.get("objectIdField") or "OBJECTID"
    expected = _count(url)
    ids_resp = fetch_json(f"{url}/query", {"where": "1=1", "returnIdsOnly": "true", "f": "json"})
    id_list = ids_resp.get("objectIds") or []
    id_set = set(id_list)
    if len(id_set) != len(id_list):
        raise IncompleteLoad(f"returnIdsOnly listed {len(id_list) - len(id_set)} duplicate object IDs")
    if len(id_set) != expected:
        raise IncompleteLoad(f"returnCountOnly says {expected}, returnIdsOnly lists {len(id_set)} IDs")
    # objectIds go in the GET URL: 2,000 IDs per request got 400 on the live permit layer, so batches stay at 200
    page = max(1, min(page_size, int(meta.get("maxRecordCount") or PAGE_CAP), PAGE_CAP, ID_BATCH))
    ordered = sorted(id_set)
    features, received = [], []
    for i in range(0, len(ordered), page):
        batch = ordered[i:i + page]
        data = fetch_json(f"{url}/query", {
            "objectIds": ",".join(str(x) for x in batch), "outFields": "*", "returnGeometry": "true",
            "outSR": 4326, "f": "geojson"})  # fetch_json_retry retries; a batch is never skipped
        for f in data.get("features") or []:
            features.append(f)
            received.append(_source_id(f, oid_field))
    recount = _count(url)
    check = verify(received, expected, id_set)
    if recount != expected:
        check["problems"].append(f"layer count changed during fetch ({expected} -> {recount})")
    if check["problems"]:
        raise IncompleteLoad("; ".join(check["problems"]))
    return features, check


def _source_id(feature: dict, oid_field: str):
    if feature.get("id") is not None:
        return feature["id"]
    props = feature.get("properties") or {}
    for k, v in props.items():
        if k.upper() == oid_field.upper():
            return v
    return None


def verify(received: list, expected: int, id_set: set | None) -> dict:
    counts = Counter(received)
    dups = sorted(k for k, n in counts.items() if n > 1 and k is not None)
    problems = []
    if None in counts:
        problems.append(f"{counts[None]} feature(s) without an object ID")
    if dups:
        problems.append(f"{len(dups)} object ID(s) received more than once (duplicate page?): {dups[:10]}")
    if id_set is not None:
        got = set(counts) - {None}
        missing, extra = sorted(id_set - got), sorted(got - id_set)
        if missing:
            problems.append(f"{len(missing)} object ID(s) missing (dropped page?): {missing[:10]}")
        if extra:
            problems.append(f"{len(extra)} unexpected object ID(s): {extra[:10]}")
    if len(received) != expected:
        problems.append(f"received {len(received)} features, expected {expected}")
    return {"expected": expected, "received": len(received), "unique": len(set(counts) - {None}),
            "problems": problems}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load Virginia Beach parcels")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--file", type=Path, help="downloaded GeoJSON FeatureCollection (EPSG:4326)")
    p.add_argument("--expected-count", type=int, help="required with --file (see --inspect)")
    p.add_argument("--oid-field", default="OBJECTID", help="object-ID property name in --file features")
    p.add_argument("--page-size", type=int, default=PAGE_CAP)
    p.add_argument("--inspect", action="store_true", help="print layer metadata and samples; write nothing")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    stats: Counter = Counter()

    if args.inspect:
        try:
            meta = fetch_json(args.url, {"f": "json"})
            print(f"{args.url}\n  geometryType={meta.get('geometryType')} objectIdField={meta.get('objectIdField')} "
                  f"maxRecordCount={meta.get('maxRecordCount')} "
                  f"lastEditDate={(meta.get('editingInfo') or {}).get('lastEditDate')}")
            print(f"  fields: {[f.get('name') for f in meta.get('fields', [])]}")
            print(f"  required fields missing: {P.missing_fields({f.get('name', '') for f in meta.get('fields', [])})}")
            print(f"  returnCountOnly: {_count(args.url)}  (use as --expected-count for --file)")
            sample = fetch_json(f"{args.url}/query", {"where": "1=1", "outFields": "*", "returnGeometry": "false",
                                                      "resultRecordCount": 5, "f": "json"})
            for f in sample.get("features") or []:
                print(f"  sample: {f.get('attributes')}")
        except (RuntimeError, ValueError, KeyError) as e:
            print(f"[FATAL] {e}", file=sys.stderr)
            return 1
        return 0

    vintage = None
    try:
        if args.file:
            if args.expected_count is None:
                print("[FATAL] --file requires --expected-count (the layer's returnCountOnly, see --inspect): "
                      "a downloaded file cannot otherwise be checked for completeness", file=sys.stderr)
                return 1
            raw = json.loads(args.file.read_text(encoding="utf-8")).get("features") or []
            props = set().union(*[set((f.get("properties") or {}).keys()) for f in raw[:50]]) if raw else set()
            missing = P.missing_fields(props)
            if missing:
                raise LoadError(f"{args.file}: features lack {missing}; properties seen: {sorted(props)}")
            check = verify([_source_id(f, args.oid_field) for f in raw], args.expected_count, None)
            if check["problems"]:
                raise IncompleteLoad("; ".join(check["problems"]))
            source = str(args.file)
        else:
            meta = layer_meta(args.url)
            vintage = (meta.get("editingInfo") or {}).get("lastEditDate")
            raw, check = fetch_complete(args.url, meta, args.page_size)
            source = args.url
        if not raw:
            raise LoadError("no features; nothing written")
        parcels = P.normalize(raw, stats)
        if not parcels:
            raise LoadError("no usable parcels (all lacked a GPIN or polygon); nothing written")
        h = P.content_hash(parcels)
    except IncompleteLoad as e:
        print(f"[FATAL] incomplete source, nothing written, previous load still active: {e}", file=sys.stderr)
        return 2
    except (LoadError, RuntimeError, ValueError, OSError) as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1

    print(f"{source}: {check['received']} features (expected {check['expected']}, {check['unique']} unique IDs) "
          f"-> {len(parcels)} parcels; content_hash={h[:16]}... vintage={vintage}")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    if args.dry_run:
        print("--dry-run: nothing written")
        return 0

    with db_module.db() as conn:
        active = conn.execute("SELECT load_id, content_hash FROM parcel_loads WHERE is_active = 1").fetchone()
        if active and active["content_hash"] == h:
            print(f"Unchanged: identical to active parcel load {active['load_id']}; nothing written")
            return 0
        conn.execute("UPDATE parcel_loads SET is_active = 0 WHERE is_active = 1")
        load_id = conn.execute(
            """INSERT INTO parcel_loads (source_url, source_vintage, content_hash, feature_count, expected_count,
                   received_count, unique_source_ids, loaded_at, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (source, None if vintage is None else str(vintage), h, len(parcels), check["expected"],
             check["received"], check["unique"], db_module.now_iso())).lastrowid
        rows = []
        for pc in parcels:
            house, street = P.address_parts(pc.attrs["prop_address"])
            rows.append((load_id, pc.gpin, pc.gpin_digits, pc.gpin10, pc.attrs["zoning"], pc.attrs["land_use"],
                         pc.attrs["prop_class"], pc.attrs["tax_area_sqft"], pc.attrs["prop_address"], house, street,
                         pc.attrs["fema_zone_bldg"], pc.source_rows,
                         json.dumps(mapping(pc.geom), separators=(",", ":")), *pc.geom.bounds))
        conn.executemany(
            """INSERT INTO parcels (load_id, gpin, gpin_digits, gpin10, zoning, land_use, prop_class, tax_area_sqft,
                   prop_address, house, street_norm, fema_zone_bldg, source_rows, geom_geojson, minx, miny, maxx, maxy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        ts = db_module.now_iso()
        conn.execute("""INSERT INTO ingestion_runs (locality, started_at, completed_at, status, fetched_count,
                            inserted_count, error_message) VALUES (?, ?, ?, 'success', ?, ?, ?)""",
                     (RUN_LOCALITY, ts, ts, check["received"], len(parcels), f"load_id={load_id}"))
    print(f"Activated parcel load {load_id}. Run scripts/match_parcels.py next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
