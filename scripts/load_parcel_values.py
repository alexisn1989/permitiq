"""Load assessed values per parcel from the HRGEO regional parcel layer (geo.hrsd.com),
which HRSD builds from each locality's published GIS. Read complete or not at all
(count and OBJECTID set must match, ingest/arcgis.py); only the value fields are
requested -- the layer also holds owner names and mailing addresses, which never
reach this script. One active load per locality; an identical re-read changes nothing.

The source does not state a tax year: every row is stored with tax_year NULL and
tax_year_status 'unconfirmed', and no tax bill is computed from it.

Usage (after scripts/migrate_db.py, schema v26):
  python scripts/load_parcel_values.py --locality chesapeake [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import arcgis  # noqa: E402
from ingest.common import fetch_json_retry  # noqa: E402

fetch_json = fetch_json_retry  # indirection so tests can stub the network

LAYER = "https://geo.hrsd.com/hrgeo/rest/services/regionalgis/HRGeo_Parcels_Public/MapServer/0"
FIPS = {"chesapeake": "550"}  # HRGEO's 3-digit county code; its PARCELID is Chesapeake's MAP_PARCEL
FIELDS = ("FIPS", "PARCELID", "LNDVALUE", "IMPVALUE", "TOTVALUE", "LASTUPDATE")
OUT_FIELDS = "OBJECTID," + ",".join(FIELDS)


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def normalize(features: list[dict], stats: Counter) -> tuple[list[dict], str | None]:
    """One row per parcel id; conflicting duplicates keep no value; the newest LASTUPDATE."""
    by: dict[str, dict] = {}
    newest = None
    for f in features:
        p = f.get("properties") or {}
        gpin = str(p.get("PARCELID") or "").strip()
        if not gpin or gpin == "None":
            stats["no_parcel_id"] += 1
            continue
        row = {"gpin": gpin, "land_value": _num(p.get("LNDVALUE")),
               "improvement_value": _num(p.get("IMPVALUE")), "total_value": _num(p.get("TOTVALUE"))}
        vals = (row["land_value"], row["improvement_value"], row["total_value"])
        if any(v is not None and v < 0 for v in vals):
            stats["negative_value"] += 1
            continue
        if row["total_value"] is None:
            stats["no_total_value"] += 1
        elif row["land_value"] is not None and abs((row["land_value"] or 0) + (row["improvement_value"] or 0)
                                                   - row["total_value"]) >= 1:
            stats["land_plus_improvement_not_total"] += 1
        if isinstance(p.get("LASTUPDATE"), (int, float)):
            d = datetime.fromtimestamp(p["LASTUPDATE"] / 1000, UTC).date().isoformat()
            newest = d if newest is None or d > newest else newest
        prev = by.get(gpin)
        if prev is None:
            by[gpin] = row
        elif prev == row:
            stats["duplicate_identical"] += 1
        else:
            stats["duplicate_conflicting"] += 1
            by[gpin] = {"gpin": gpin, "land_value": None, "improvement_value": None, "total_value": None}
    return [by[k] for k in sorted(by)], newest


def content_hash(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load(conn, locality: str, dry_run: bool) -> str:
    meta = fetch_json(LAYER, {"f": "json"})
    names = {f.get("name", "").upper() for f in meta.get("fields", [])}
    missing = [f for f in FIELDS if f not in names]
    if missing:
        raise ValueError(f"{locality}: fields {missing} are missing from the source")
    raw = arcgis.fetch_all_by_ids(fetch_json, LAYER, f"FIPS = '{FIPS[locality]}'", out_fields=OUT_FIELDS,
                                  label=f"{locality} values", geometry=False, batch=1000)
    stats: Counter = Counter()
    rows, vintage = normalize(raw, stats)
    pl = conn.execute("SELECT load_id FROM parcel_loads WHERE is_active = 1 AND locality = ?", (locality,)).fetchone()
    plid = pl["load_id"] if pl else None
    ours = {r["gpin"] for r in conn.execute("SELECT gpin FROM parcels WHERE load_id = ?", (plid,))} if plid else set()
    matched = sum(1 for r in rows if r["gpin"] in ours)
    total = sum(r["total_value"] or 0 for r in rows)
    h = content_hash(rows)
    print(f"{locality}: {len(raw)} features -> {len(rows)} parcels; refreshed {vintage}; {dict(sorted(stats.items()))}")
    print(f"  total ${total / 1e9:,.2f}B; {matched} of {len(ours)} parcels in the active parcel load have a value"
          + ("" if plid else "  [no active parcel load]"))
    if dry_run:
        return "dry-run"
    active = conn.execute("SELECT load_id, content_hash, parcel_load_id FROM parcel_value_loads "
                          "WHERE locality = ? AND is_active = 1", (locality,)).fetchone()
    if active and active["content_hash"] == h and active["parcel_load_id"] == plid:
        print(f"  unchanged: identical to active load {active['load_id']}")
        return "unchanged"
    conn.execute("UPDATE parcel_value_loads SET is_active = 0 WHERE locality = ? AND is_active = 1", (locality,))
    load_id = conn.execute(
        "INSERT INTO parcel_value_loads (locality, source_url, expected_count, received_count, parcels, "
        "matched_parcels, parcel_load_id, content_hash, source_vintage, stats_json, loaded_at, is_active) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
        (locality, LAYER, len(raw), len(raw), len(rows), matched, plid, h, vintage, json.dumps(dict(stats)),
         db_module.now_iso())).lastrowid
    conn.executemany(
        "INSERT INTO parcel_values (load_id, locality, gpin, land_value, improvement_value, total_value, tax_year, "
        "tax_year_status) VALUES (?,?,?,?,?,?,NULL,'unconfirmed')",
        [(load_id, locality, r["gpin"], r["land_value"], r["improvement_value"], r["total_value"]) for r in rows])
    print(f"  activated load {load_id}")
    return "loaded"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load assessed values per parcel (HRGEO, tax year unconfirmed)")
    p.add_argument("--locality", required=True, choices=sorted(FIPS))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    try:
        with db_module.db() as conn:
            load(conn, args.locality, args.dry_run)
    except (arcgis.IncompleteRead, RuntimeError, ValueError, KeyError) as e:
        print(f"[error] {e}; nothing written", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
