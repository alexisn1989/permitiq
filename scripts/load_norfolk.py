"""Load Norfolk's approved land-use items (ingest/norfolk_records.py): Conditional
Use Permits and Conditional Zoning, each granted by a City Council ordinance.

Each layer is read complete or not at all (count and OBJECTID set must match,
ingest/arcgis.py), activated only when complete, one active load per source.
Parcels are linked spatially against the active Norfolk parcel load: a CUP's point
inside a parcel, a conditional zoning's parcels inside its boundary.

Usage:
  python scripts/load_norfolk.py --all [--dry-run]
  python scripts/load_norfolk.py --source nf_cups
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import arcgis, norfolk_records as NR, parcels as P  # noqa: E402
from ingest.common import fetch_json_retry  # noqa: E402

fetch_json = fetch_json_retry  # indirection so tests can stub the network

_spec = importlib.util.spec_from_file_location("load_chesapeake", Path(__file__).resolve().parent / "load_chesapeake.py")
_lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lc)


def _link(row: dict, index, stats) -> list[str]:
    if index is None or row["geom"] is None:
        return []
    g = row["geom"]
    if g.geom_type in ("Point", "MultiPoint"):
        pts = [g] if g.geom_type == "Point" else list(g.geoms)
        found = sorted({x for pt in pts for x in index.at_point(pt.y, pt.x)})
    else:
        found = index.within(g)
    stats["parcels_spatial" if found else "parcels_none"] += 1
    return found


def load_source(conn, source: str, dry_run: bool) -> str:
    url = NR.SOURCES[source]
    meta = fetch_json(url, {"f": "json"})
    fields = {f.get("name", "").upper() for f in meta.get("fields", [])}
    need = NR.CUP_FIELDS if source == "nf_cups" else NR.CZ_FIELDS
    missing = [f for f in need if f.upper() not in fields]
    if missing:
        raise ValueError(f"{source}: fields {missing} are missing from the source")
    stats: Counter = Counter()
    raw = arcgis.fetch_all_by_ids(fetch_json, url, "1=1", label=source, oid_field=meta.get("objectIdField") or "OBJECTID",
                                  geometry=True)
    rows = (NR.normalize_cups(raw, stats) if source == "nf_cups" else NR.normalize_conditional_zoning(raw, stats))
    load = conn.execute("SELECT load_id FROM parcel_loads WHERE is_active = 1 AND locality = ?", (NR.LOCALITY,)).fetchone()
    plid = load["load_id"] if load else None
    index = P.load_index(conn, plid) if plid else None
    for r in rows:
        r["gpins"] = _link(r, index, stats)
    h = NR.content_hash(rows)
    print(f"{source}: {len(raw)} features -> {len(rows)} items; {dict(sorted(stats.items()))}"
          + ("" if plid else "  [no active Norfolk parcel load: parcels not linked]"))
    if dry_run:
        return "dry-run"
    active = conn.execute("SELECT load_id, content_hash, parcel_load_id FROM city_record_loads WHERE locality = ? "
                          "AND source = ? AND is_active = 1", (NR.LOCALITY, source)).fetchone()
    if active and active["content_hash"] == h and active["parcel_load_id"] == plid:
        print(f"  unchanged: identical to active load {active['load_id']}")
        return "unchanged"
    conn.execute("UPDATE city_record_loads SET is_active = 0 WHERE locality = ? AND source = ? AND is_active = 1",
                 (NR.LOCALITY, source))
    oid = meta.get("objectIdField") or "OBJECTID"
    load_id = conn.execute(
        "INSERT INTO city_record_loads (locality, source, source_url, expected_count, received_count, unique_ids, "
        "records, content_hash, domain_json, parcel_load_id, loaded_at, is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
        (NR.LOCALITY, source, url, len(raw), len(raw), len({arcgis.feature_id(f, oid) for f in raw}), len(rows), h,
         json.dumps({}), plid, db_module.now_iso())).lastrowid
    _lc.insert_cases(conn, load_id, NR.LOCALITY, rows)
    print(f"  activated load {load_id}")
    return "loaded"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load Norfolk's Conditional Use Permits and Conditional Zoning")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--source", action="append", choices=sorted(NR.SOURCES))
    g.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    failed = []
    for source in (sorted(NR.SOURCES) if args.all else args.source):
        try:
            with db_module.db() as conn:
                load_source(conn, source, args.dry_run)
        except (arcgis.IncompleteRead, RuntimeError, ValueError, KeyError) as e:
            failed.append(source)
            print(f"[error] {source}: {e}; nothing written for this source", file=sys.stderr)
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
