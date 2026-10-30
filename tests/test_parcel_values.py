"""Assessed values per parcel (HRGEO): only Chesapeake's county code is read, only the
value fields are requested (the layer holds owner names and mailing addresses), the
tax year stays unconfirmed, values attach to the same city's parcels only, and an
identical re-read changes nothing. Synthetic data."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import db_module, env  # noqa: E402,F401

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lpv", ROOT / "scripts" / "load_parcel_values.py")
lpv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lpv)


def _f(oid, fips, pid, land, impr, total):
    return {"type": "Feature", "id": oid, "geometry": None,
            "properties": {"OBJECTID": oid, "FIPS": fips, "PARCELID": pid, "LNDVALUE": land, "IMPVALUE": impr,
                           "TOTVALUE": total, "LASTUPDATE": 1772150400000, "OWNERNME1": "Jane Q Public",
                           "MLADDRESS": "1 Private Ln"}}


FEATS = [_f(1, "550", "0940000000293", 252000, 320500, 572500),
         _f(2, "550", "0940000000290", 403000, 0, 403000),
         _f(3, "550", "0940000000290", 403000, 0, 403000),  # identical duplicate row
         _f(4, "550", "0618003002660", None, None, None),  # no value published
         _f(5, "810", "0940000000293", 1, 1, 2)]  # Virginia Beach, same digits


def _service(seen):
    def fetch(url, params=None, **kw):
        if not url.endswith("/query"):
            return {"fields": [{"name": n} for n in ("OBJECTID", "PARCELID", "TAXMAPNO", *lpv.FIELDS, "OWNERNME1",
                                                     "MLADDRESS")]}
        seen.append(params)
        fips = params["where"].split("'")[1] if "where" in params else None
        sel = [f for f in FEATS if fips is None or f["properties"]["FIPS"] == fips]
        if params.get("returnCountOnly"):
            return {"count": len(sel)}
        if params.get("returnIdsOnly"):
            return {"objectIds": [f["id"] for f in sel]}
        keep = params["outFields"].split(",")
        ids = {int(x) for x in params["objectIds"].split(",")}
        return {"features": [{**f, "properties": {k: v for k, v in f["properties"].items() if k in keep}}
                             for f in FEATS if f["id"] in ids]}
    return fetch


def _parcels(conn, locality, gpins):
    load_id = conn.execute(
        "INSERT INTO parcel_loads (locality, source_url, content_hash, feature_count, expected_count, received_count, "
        "unique_source_ids, loaded_at, is_active) VALUES (?, 'x', ?, 1, 1, 1, 1, 'now', 1)",
        (locality, locality)).lastrowid
    for g in gpins:
        conn.execute("INSERT INTO parcels (load_id, gpin, gpin_digits, source_rows, geom_geojson, minx, miny, maxx, "
                     "maxy) VALUES (?, ?, ?, 1, '{}', 0, 0, 1, 1)", (load_id, g, g))


def test_loads_chesapeake_values_only_unconfirmed(env, monkeypatch):
    seen = []
    monkeypatch.setattr(lpv, "fetch_json", _service(seen))
    with db_module.db(env["db"]) as conn:
        _parcels(conn, "chesapeake", ["0940000000293", "0940000000290", "0618003002660", "0000000000001"])
    assert lpv.main(["--locality", "chesapeake"]) == 0
    assert {p["where"] for p in seen if "where" in p} == {"FIPS = '550'"}
    assert all("OWNER" not in p["outFields"] and "ML" not in p["outFields"] for p in seen if "outFields" in p)
    with db_module.db_readonly(env["db"]) as conn:
        rows = {r["gpin"]: dict(r) for r in conn.execute("SELECT * FROM parcel_values_effective")}
        load = dict(conn.execute("SELECT * FROM parcel_value_loads WHERE is_active = 1").fetchone())
    assert set(rows) == {"0940000000293", "0940000000290", "0618003002660"}
    assert rows["0940000000293"]["total_value"] == 572500 and rows["0618003002660"]["total_value"] is None
    assert all(r["tax_year"] is None and r["tax_year_status"] == "unconfirmed" and r["locality"] == "chesapeake"
               for r in rows.values())
    assert load["matched_parcels"] == 3 and load["source_vintage"] == "2026-02-27"
    assert json.loads(load["stats_json"]) == {"duplicate_identical": 1, "no_total_value": 1}
    assert lpv.main(["--locality", "chesapeake"]) == 0  # identical re-read: no new load
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parcel_value_loads").fetchone()[0] == 1
    with db_module.db(env["db"]) as conn:
        with pytest.raises(sqlite3.IntegrityError):  # a tax year only with 'confirmed'
            conn.execute("INSERT INTO parcel_values (load_id, locality, gpin, tax_year) VALUES (1, 'x', 'y', 2026)")


def test_case_parcels_show_values_of_the_same_city_only(env, monkeypatch):
    monkeypatch.setattr(lpv, "fetch_json", _service([]))
    with db_module.db(env["db"]) as conn:
        _parcels(conn, "chesapeake", ["0940000000293"])
        _parcels(conn, "norfolk", ["0940000000290"])  # same id under another city: never valued from Chesapeake
    assert lpv.main(["--locality", "chesapeake"]) == 0
    import app as app_module
    with db_module.db_readonly(env["db"]) as conn:
        got = {p["gpin"]: p for p in app_module._exact_parcels(conn, ["0940000000293", "0940000000290"])}
    assert got["0940000000293"]["total_value"] == 572500 and got["0940000000293"]["tax_year_status"] == "unconfirmed"
    assert got["0940000000290"]["total_value"] is None


def test_card_labels_values_and_shows_no_tax_bill():
    js = (ROOT / "static" / "lead_util.js").read_text()
    assert "Assessed value (tax year unconfirmed)" in js and "No tax bill is shown" in js and "no tax bill is shown" in js


ROLL = [  # synthetic Norfolk roll rows: a 3-building account repeats its value; two condo units share a GPIN
    {"parcel_id": "11605", "gpin": "1448671584", "extension": f"C0{i}", "current_land_value": "897400",
     "current_improvement_value": "6118200", "current_total_value": "7015600",
     "effective_year": "2026-07-01T00:00:00.000"} for i in (1, 2, 3)
] + [
    {"parcel_id": "312500", "gpin": "1429682282", "extension": "R02", "current_land_value": "50000",
     "current_improvement_value": "201600", "current_total_value": "251600", "effective_year": "2026-07-01T00:00:00.000"},
    {"parcel_id": "1776000", "gpin": "1429682282", "extension": "R02", "current_land_value": "40000",
     "current_improvement_value": "149900", "current_total_value": "189900", "effective_year": "2026-07-01T00:00:00.000"},
    {"parcel_id": "99", "gpin": None, "extension": "R01", "current_land_value": "1", "current_improvement_value": "1",
     "current_total_value": "2", "effective_year": "2026-07-01T00:00:00.000"},
]


def _roll(rows, seen):
    def fetch(url, params=None, **kw):
        seen.append(params)
        if params.get("$select") == "count(*)":
            return [{"count": str(len(rows))}]
        off, lim = int(params["$offset"]), int(params["$limit"])
        keep = params["$select"].split(",")
        return [{k: v for k, v in r.items() if k in keep} for r in rows[off:off + lim]]
    return fetch


def test_norfolk_roll_one_value_per_account_summed_per_gpin_confirmed_fy2027(env, monkeypatch):
    seen = []
    monkeypatch.setattr(lpv, "fetch_json", _roll(ROLL, seen))
    monkeypatch.setattr(lpv, "ROLL_PAGE", 2)  # paging is exercised
    with db_module.db(env["db"]) as conn:
        _parcels(conn, "norfolk", ["1448671584", "1429682282"])
    assert lpv.main(["--locality", "norfolk"]) == 0
    sel = {p["$select"] for p in seen}
    assert all("owner" not in s_ and "grant" not in s_ for s_ in sel)
    with db_module.db_readonly(env["db"]) as conn:
        rows = {r["gpin"]: dict(r) for r in conn.execute("SELECT * FROM parcel_values_effective")}
        stats = json.loads(conn.execute("SELECT stats_json FROM parcel_value_loads").fetchone()[0])
    assert rows["1448671584"]["total_value"] == 7015600  # not 3x
    assert rows["1429682282"]["total_value"] == 251600 + 189900  # two condo accounts
    assert all(r["tax_year"] == 2027 and r["tax_year_status"] == "confirmed" for r in rows.values())
    assert rows["1448671584"]["source_vintage"] == "2026-07-01"
    assert stats["account_without_gpin"] == 1 and stats["building_rows_repeating_account"] == 2


def test_norfolk_roll_refuses_disagreeing_account_or_mixed_years(env, monkeypatch):
    bad = [dict(ROLL[0]), {**ROLL[1], "current_total_value": "1"}]
    monkeypatch.setattr(lpv, "fetch_json", _roll(bad, []))
    assert lpv.main(["--locality", "norfolk"]) == 3
    mixed = [dict(ROLL[0]), {**ROLL[3], "effective_year": "2025-07-01T00:00:00.000"}]
    monkeypatch.setattr(lpv, "fetch_json", _roll(mixed, []))
    assert lpv.main(["--locality", "norfolk"]) == 3
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parcel_value_loads").fetchone()[0] == 0


def test_portsmouth_values_keyed_by_tax_map_number(env, monkeypatch):
    seen = []
    pt = [{"type": "Feature", "id": 50 + i, "geometry": None,
           "properties": {"OBJECTID": 50 + i, "FIPS": "740", "PARCELID": f"0846{i:04d}", "TAXMAPNO": tm,
                          "LNDVALUE": 20000, "IMPVALUE": v - 20000, "TOTVALUE": v, "LASTUPDATE": 1772150400000,
                          "OWNERNME1": "Jane Q Public"}}
          for i, (tm, v) in enumerate((("846-70", 133390), ("846-80", 134950)))]
    monkeypatch.setattr(sys.modules[__name__], "FEATS", FEATS + pt)
    monkeypatch.setattr(lpv, "fetch_json", _service(seen))
    with db_module.db(env["db"]) as conn:
        _parcels(conn, "portsmouth", ["846-70", "846-80"])
    assert lpv.main(["--locality", "portsmouth"]) == 0
    assert {p["where"] for p in seen if "where" in p} == {"FIPS = '740'"}
    assert all(p["outFields"].split(",")[:2] == ["OBJECTID", "TAXMAPNO"] and "OWNER" not in p["outFields"]
               for p in seen if "outFields" in p)
    with db_module.db_readonly(env["db"]) as conn:
        rows = {r["gpin"]: r["total_value"] for r in conn.execute(
            "SELECT gpin, total_value FROM parcel_values_effective WHERE locality = 'portsmouth'")}
    assert rows == {"846-70": 133390, "846-80": 134950}
