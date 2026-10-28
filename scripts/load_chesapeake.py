"""Load Chesapeake's published development records: land-use cases
(Development Tracking), development plans, right-of-way permits
(ingest/chesapeake_records.py). Chesapeake publishes no building permits.

Each source is read complete or not at all (count and OBJECTID set must match,
ingest/arcgis.py), activated only when complete, one active load per source.
Action codes come from the cases layer's own coded domain (an unknown code
stops the load). Parcels are linked by the record's parcel number when it is
in the active Chesapeake parcel load, else spatially.

Usage:
  python scripts/load_chesapeake.py --all [--dry-run]
  python scripts/load_chesapeake.py --source ches_cases
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import arcgis, chesapeake_records as CH, parcels as P  # noqa: E402
from ingest.common import fetch_json_retry  # noqa: E402
from shapely.geometry import Point  # noqa: E402

fetch_json = fetch_json_retry  # indirection so tests can stub the network


def _parcels(conn):
    load = conn.execute("SELECT load_id FROM parcel_loads WHERE is_active = 1 AND locality = ?",
                        (CH.LOCALITY,)).fetchone()
    if load is None:
        return None, None, set()
    ids = {r[0] for r in conn.execute("SELECT gpin FROM parcels WHERE load_id = ?", (load["load_id"],))}
    return load["load_id"], P.load_index(conn, load["load_id"]), ids


def _link(row: dict, index, ids: set, stats) -> list[str]:
    exact = [g for g in row["parcel_ids"] if g in ids]
    if exact:
        stats["parcels_by_number"] += 1
        return exact
    if index is None:
        return []
    if row.get("geom") is not None:
        found = index.within(row["geom"])
    elif row.get("lat") is not None:
        found = index.at_point(row["lat"], row["lng"])
    else:
        found = []
    stats["parcels_spatial" if found else "parcels_none"] += 1
    return found


def load_source(conn, source: str, dry_run: bool) -> str:
    url = CH.SOURCES[source]
    meta = fetch_json(url, {"f": "json"})
    fields = {f.get("name", "").upper() for f in meta.get("fields", [])}
    need = CH.CASE_FIELDS if source == "ches_cases" else CH.RECORD_FIELDS
    missing = [f for f in need if f.upper() not in fields]
    if missing:
        raise CH.DomainError(f"{source}: fields {missing} are missing from the source")
    domain = CH.action_domain(meta) if source == "ches_cases" else {}
    stats: Counter = Counter()
    raw = arcgis.fetch_all_by_ids(fetch_json, url, "1=1", label=source, oid_field=meta.get("objectIdField") or "OBJECTID",
                                  geometry=True)
    rows = (CH.merge_cases(raw, domain, stats) if source == "ches_cases"
            else CH.normalize_records(raw, source, stats))
    plid, index, ids = _parcels(conn)
    for r in rows:
        r["gpins"] = _link(r, index, ids, stats)
    h = CH.content_hash(rows)
    print(f"{source}: {len(raw)} features -> {len(rows)} rows; {dict(sorted(stats.items()))}"
          + ("" if plid else "  [no active Chesapeake parcel load: parcels not linked]"))
    if dry_run:
        return "dry-run"
    active = conn.execute("SELECT load_id, content_hash, parcel_load_id FROM city_record_loads WHERE locality = ? "
                          "AND source = ? AND is_active = 1", (CH.LOCALITY, source)).fetchone()
    if active and active["content_hash"] == h and active["parcel_load_id"] == plid:
        print(f"  unchanged: identical to active load {active['load_id']}")
        return "unchanged"
    conn.execute("UPDATE city_record_loads SET is_active = 0 WHERE locality = ? AND source = ? AND is_active = 1",
                 (CH.LOCALITY, source))
    load_id = conn.execute(
        "INSERT INTO city_record_loads (locality, source, source_url, expected_count, received_count, unique_ids, "
        "records, content_hash, domain_json, parcel_load_id, loaded_at, is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
        (CH.LOCALITY, source, url, len(raw), len(raw), len({arcgis.feature_id(f, meta.get('objectIdField') or 'OBJECTID')
                                                            for f in raw}),
         len(rows), h, json.dumps(domain), plid, db_module.now_iso())).lastrowid
    if source == "ches_cases":
        conn.executemany(
            "INSERT INTO land_use_cases (load_id, locality, case_id, object_ids_json, case_type, project, address, "
            "parcel_ids_json, housing_units, residential, zoning_from, zoning_to, applied_date, year_applied, "
            "staff_rec, pc_action, pc_date, cc_action, cc_date, arc_action, arc_date, final_raw, stage, final_date, "
            "planner, notes, legacy, source_link, geom_geojson, minx, miny, maxx, maxy, lat, lng, gpins_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(load_id, CH.LOCALITY, r["case_id"], json.dumps(r["object_ids"]), r["case_type"], r["project"],
              r["address"], json.dumps(r["parcel_ids"]), r["housing_units"], r["residential"], r["zoning_from"],
              r["zoning_to"], r["applied_date"], r["year_applied"], r["staff_rec"], r["pc_action"], r["pc_date"],
              r["cc_action"], r["cc_date"], r["arc_action"], r["arc_date"], r["final_raw"], r["stage"],
              r["final_date"], r["planner"], r["notes"], r["legacy"], r["source_link"], CH.geojson(r["geom"]),
              *(r["geom"].bounds if r["geom"] is not None else (None,) * 4), r["lat"], r["lng"],
              json.dumps(r["gpins"])) for r in rows])
    else:
        conn.executemany(
            "INSERT INTO development_records (load_id, locality, source, record_id, object_ids_json, record_type, "
            "project_type, status, details, organization, address, submitted_date, approved_date, expires_date, "
            "parcel_ids_json, lat, lng, gpins_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(load_id, CH.LOCALITY, source, r["record_id"], json.dumps(r["object_ids"]), r["record_type"],
              r["project_type"], r["status"], r["details"], r["organization"], r["address"], r["submitted_date"],
              r["approved_date"], r["expires_date"], json.dumps(r["parcel_ids"]), r["lat"], r["lng"],
              json.dumps(r["gpins"])) for r in rows])
    print(f"  activated load {load_id}")
    return "loaded"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load Chesapeake's land-use cases, development plans, ROW permits")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--source", action="append", choices=sorted(CH.SOURCES))
    g.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    failed = []
    for source in (sorted(CH.SOURCES) if args.all else args.source):
        try:
            with db_module.db() as conn:
                load_source(conn, source, args.dry_run)
        except (arcgis.IncompleteRead, CH.DomainError, RuntimeError, ValueError, KeyError) as e:
            failed.append(source)
            print(f"[error] {source}: {e}; nothing written for this source", file=sys.stderr)
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
