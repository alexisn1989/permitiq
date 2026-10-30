"""Load Suffolk's approved land-use items (ingest/suffolk_records.py): the city's
Development Restrictions layer. Read complete or not at all (count and OBJECTID set
must match, ingest/arcgis.py), one active load. Only the listed fields are requested
(the layer's free-text Comment and editor fields never are). Parcels are linked
spatially against the active Suffolk parcel load (parcels inside the boundary).

Usage:
  python scripts/load_suffolk.py [--dry-run]
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
from ingest import arcgis, parcels as P, suffolk_records as SR  # noqa: E402
from ingest.common import fetch_json_retry  # noqa: E402

fetch_json = fetch_json_retry  # indirection so tests can stub the network

_spec = importlib.util.spec_from_file_location("load_norfolk", Path(__file__).resolve().parent / "load_norfolk.py")
_ln = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ln)


def load_source(conn, source: str, dry_run: bool) -> str:
    url = SR.SOURCES[source]
    meta = fetch_json(url, {"f": "json"})
    fields = {f.get("name", "").upper() for f in meta.get("fields", [])}
    missing = [f for f in SR.FIELDS if f.upper() not in fields]
    if missing:
        raise ValueError(f"{source}: fields {missing} are missing from the source")
    stats: Counter = Counter()
    oid = meta.get("objectIdField") or "OBJECTID"
    raw = arcgis.fetch_all_by_ids(fetch_json, url, "1=1", out_fields=SR.OUT_FIELDS, label=source, oid_field=oid,
                                  geometry=True)
    rows = SR.normalize(raw, stats)
    load = conn.execute("SELECT load_id FROM parcel_loads WHERE is_active = 1 AND locality = ?", (SR.LOCALITY,)).fetchone()
    plid = load["load_id"] if load else None
    index = P.load_index(conn, plid) if plid else None
    for r in rows:
        r["gpins"] = _ln._link(r, index, stats)
    h = SR.content_hash(rows)
    print(f"{source}: {len(raw)} features -> {len(rows)} items; {dict(sorted(stats.items()))}"
          + ("" if plid else "  [no active Suffolk parcel load: parcels not linked]"))
    if dry_run:
        return "dry-run"
    active = conn.execute("SELECT load_id, content_hash, parcel_load_id FROM city_record_loads WHERE locality = ? "
                          "AND source = ? AND is_active = 1", (SR.LOCALITY, source)).fetchone()
    if active and active["content_hash"] == h and active["parcel_load_id"] == plid:
        print(f"  unchanged: identical to active load {active['load_id']}")
        return "unchanged"
    conn.execute("UPDATE city_record_loads SET is_active = 0 WHERE locality = ? AND source = ? AND is_active = 1",
                 (SR.LOCALITY, source))
    load_id = conn.execute(
        "INSERT INTO city_record_loads (locality, source, source_url, expected_count, received_count, unique_ids, "
        "records, content_hash, domain_json, parcel_load_id, loaded_at, is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
        (SR.LOCALITY, source, url, len(raw), len(raw), len({arcgis.feature_id(f, oid) for f in raw}), len(rows), h,
         json.dumps({}), plid, db_module.now_iso())).lastrowid
    _ln._lc.insert_cases(conn, load_id, SR.LOCALITY, rows)
    print(f"  activated load {load_id}")
    return "loaded"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load Suffolk's Development Restrictions (approved land-use items)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    failed = []
    for source in sorted(SR.SOURCES):
        try:
            with db_module.db() as conn:
                load_source(conn, source, args.dry_run)
        except (arcgis.IncompleteRead, RuntimeError, ValueError, KeyError) as e:
            failed.append(source)
            print(f"[error] {source}: {e}; nothing written for this source", file=sys.stderr)
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
