"""Load assessed values per parcel. Three kinds of source, all read complete or not at all,
all requested with value fields only (each also holds owner names; they never reach
this script). One active load per locality; an identical re-read changes nothing.

* Chesapeake and Portsmouth -- the HRGEO regional parcel layer (geo.hrsd.com), HRSD's republication of
  city GIS. No tax year is stated: rows are stored with tax_year NULL / 'unconfirmed'.
* Norfolk -- the city Real Estate Assessor's own roll on data.norfolk.gov ("Property
  Assessment and Sales - FY27", ProVal, updated daily). One row per building
  ("extension") repeating its account's value; condo units share a GPIN with their own
  account. Values are taken once per account (parcel_id; an account whose rows disagree
  stops the run) and summed per GPIN. The roll's effective date (July 1, 2026) fixes the
  tax year: FY2027, stored 'confirmed'.

* Newport News -- the city's own parcel layer (maps.nnva.gov), its "Current" land and
  improvement values (summed for the total). The layer states no year: 'unconfirmed'.

No tax bill is computed from any of them.

Usage (after scripts/migrate_db.py, schema v26):
  python scripts/load_parcel_values.py --locality chesapeake|newport_news|norfolk|portsmouth [--dry-run]
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
FIPS = {"chesapeake": "550", "portsmouth": "740"}  # HRGEO's 3-digit county code
# the HRGEO field that equals the city's parcel id in PermitIQ: Chesapeake's MAP_PARCEL is PARCELID;
# Portsmouth's parcels are keyed by their tax-map number (parcel load from this same layer)
KEY_FIELD = {"chesapeake": "PARCELID", "portsmouth": "TAXMAPNO"}
ROLLS = {"norfolk": "https://data.norfolk.gov/resource/qva7-tzrf.json"}  # Property Assessment and Sales - FY27
ROLL_FIELDS = ("parcel_id", "gpin", "extension", "current_land_value", "current_improvement_value",
               "current_total_value", "effective_year")
ROLL_PAGE = 50000
# a city's own parcel layer carrying its "current" land / improvement values (no total field and no
# year): Newport News publishes Previous and Current values; HRGEO equals Previous on 199 of 200
# sampled parcels, so Current is the newer roll. The layer also holds owner and mailing fields,
# never requested.
CITY_LAYERS = {"newport_news": {"url": "https://maps.nnva.gov/gis/rest/services/Operational/Parcel/MapServer/0",
                                "key": "PARCELID", "land": "CNTLNDVAL", "impr": "CNTIMPVAL"}}
LOCALITIES = sorted({*FIPS, *ROLLS, *CITY_LAYERS})
FIELDS = ("FIPS", "LNDVALUE", "IMPVALUE", "TOTVALUE", "LASTUPDATE")


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def normalize(features: list[dict], stats: Counter, key: str = "PARCELID") -> tuple[list[dict], str | None]:
    """One row per parcel id; conflicting duplicates keep no value; the newest LASTUPDATE."""
    by: dict[str, dict] = {}
    newest = None
    for f in features:
        p = f.get("properties") or {}
        gpin = str(p.get(key) or "").strip()
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


def _dec(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def read_roll(locality: str, stats: Counter) -> tuple[list[dict], dict]:
    """The Assessor's roll: every row (count checked), one value per account, summed per GPIN."""
    url = ROLLS[locality]
    expected = int(fetch_json(url, {"$select": "count(*)"})[0]["count"])
    raw: list[dict] = []
    while True:
        page = fetch_json(url, {"$select": ",".join(ROLL_FIELDS), "$order": "parcel_id,extension,:id",
                                "$limit": ROLL_PAGE, "$offset": len(raw)})
        raw.extend(page)
        if len(page) < ROLL_PAGE:
            break
    if len(raw) != expected:
        raise arcgis.IncompleteRead(f"{locality} roll: count {expected}, received {len(raw)}")
    if expected and int(fetch_json(url, {"$select": "count(*)"})[0]["count"]) != expected:
        raise arcgis.IncompleteRead(f"{locality} roll: row count changed during the read")
    effective = {r.get("effective_year") for r in raw}
    if len(effective) != 1 or not next(iter(effective)):
        raise ValueError(f"{locality} roll: expected one effective date, found {sorted(map(str, effective))}")
    eff = next(iter(effective))[:10]
    if eff[5:] != "07-01":
        raise ValueError(f"{locality} roll: effective date {eff} is not a July 1 fiscal-year start")
    accounts: dict[str, dict] = {}
    for r in raw:
        key = str(r.get("parcel_id") or "")
        vals = (str(r.get("gpin") or ""), _dec(r.get("current_land_value")),
                _dec(r.get("current_improvement_value")), _dec(r.get("current_total_value")))
        prev = accounts.get(key)
        if prev is None:
            accounts[key] = {"vals": vals}
        elif prev["vals"] != vals:
            raise ValueError(f"{locality} roll: account {key} has rows with different values")
        else:
            stats["building_rows_repeating_account"] += 1
    by: dict[str, dict] = {}
    for a in accounts.values():
        gpin, land, impr, total = a["vals"]
        if not gpin:
            stats["account_without_gpin"] += 1
            continue
        if total is not None and land is not None and abs(land + (impr or 0) - total) >= 1:
            stats["land_plus_improvement_not_total"] += 1
        row = by.setdefault(gpin, {"gpin": gpin, "land_value": 0.0, "improvement_value": 0.0, "total_value": 0.0,
                                   "accounts": 0})
        row["accounts"] += 1
        for k, v in (("land_value", land), ("improvement_value", impr), ("total_value", total)):
            row[k] = None if v is None or row[k] is None else row[k] + v
    stats["accounts"] = len(accounts)
    stats["gpins_with_several_accounts"] = sum(1 for r in by.values() if r["accounts"] > 1)
    rows = [{k: v for k, v in by[g].items() if k != "accounts"} for g in sorted(by)]
    meta = {"source_url": url, "expected": expected, "received": len(raw), "vintage": eff,
            "tax_year": int(eff[:4]) + 1, "status": "confirmed"}
    return rows, meta


def read_hrgeo(locality: str, stats: Counter) -> tuple[list[dict], dict]:
    meta = fetch_json(LAYER, {"f": "json"})
    names = {f.get("name", "").upper() for f in meta.get("fields", [])}
    key = KEY_FIELD[locality]
    missing = [f for f in (*FIELDS, key) if f not in names]
    if missing:
        raise ValueError(f"{locality}: fields {missing} are missing from the source")
    raw = arcgis.fetch_all_by_ids(fetch_json, LAYER, f"FIPS = '{FIPS[locality]}'", out_fields=",".join(("OBJECTID", key, *FIELDS)), label=f"{locality} values",
                                  geometry=False, batch=1000)
    rows, vintage = normalize(raw, stats, key)
    return rows, {"source_url": LAYER, "expected": len(raw), "received": len(raw), "vintage": vintage,
                  "tax_year": None, "status": "unconfirmed"}


def read_city_layer(locality: str, stats: Counter) -> tuple[list[dict], dict]:
    c = CITY_LAYERS[locality]
    meta = fetch_json(c["url"], {"f": "json"})
    names = {f.get("name", "") for f in meta.get("fields", [])}
    missing = [f for f in (c["key"], c["land"], c["impr"], "LASTUPDATE") if f not in names]
    if missing:
        raise ValueError(f"{locality}: fields {missing} are missing from the source")
    raw = arcgis.fetch_all_by_ids(fetch_json, c["url"], "1=1",
                                  out_fields=",".join(("OBJECTID", c["key"], c["land"], c["impr"], "LASTUPDATE")),
                                  label=f"{locality} values", geometry=False, batch=1000)
    feats = []
    for f in raw:  # the city publishes land and improvements; their sum is the total
        p = dict(f.get("properties") or {})
        land, impr = _num(p.get(c["land"])), _num(p.get(c["impr"]))
        p.update(LNDVALUE=land, IMPVALUE=impr, TOTVALUE=None if land is None and impr is None
                 else (land or 0) + (impr or 0))
        feats.append({"properties": p})
    rows, vintage = normalize(feats, stats, c["key"])
    return rows, {"source_url": c["url"], "expected": len(raw), "received": len(raw), "vintage": vintage,
                  "tax_year": None, "status": "unconfirmed"}


def content_hash(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load(conn, locality: str, dry_run: bool) -> str:
    stats: Counter = Counter()
    reader = read_roll if locality in ROLLS else read_city_layer if locality in CITY_LAYERS else read_hrgeo
    rows, src = reader(locality, stats)
    vintage = src["vintage"]
    pl = conn.execute("SELECT load_id FROM parcel_loads WHERE is_active = 1 AND locality = ?", (locality,)).fetchone()
    plid = pl["load_id"] if pl else None
    ours = {r["gpin"] for r in conn.execute("SELECT gpin FROM parcels WHERE load_id = ?", (plid,))} if plid else set()
    matched = sum(1 for r in rows if r["gpin"] in ours)
    total = sum(r["total_value"] or 0 for r in rows)
    h = content_hash([{"tax_year": src["tax_year"]}, *rows])
    year = f"tax year {src['tax_year']} ({src['status']})" if src["tax_year"] else "tax year unconfirmed"
    print(f"{locality}: {src['received']} rows -> {len(rows)} parcels; as of {vintage}; {year}; "
          f"{dict(sorted(stats.items()))}")
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
        (locality, src["source_url"], src["expected"], src["received"], len(rows), matched, plid, h, vintage,
         json.dumps(dict(stats)), db_module.now_iso())).lastrowid
    conn.executemany(
        "INSERT INTO parcel_values (load_id, locality, gpin, land_value, improvement_value, total_value, tax_year, "
        "tax_year_status) VALUES (?,?,?,?,?,?,?,?)",
        [(load_id, locality, r["gpin"], r["land_value"], r["improvement_value"], r["total_value"], src["tax_year"],
          src["status"]) for r in rows])
    print(f"  activated load {load_id}")
    return "loaded"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load assessed values per parcel (HRGEO, tax year unconfirmed)")
    p.add_argument("--locality", required=True, choices=LOCALITIES)
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
