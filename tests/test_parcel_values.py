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
            return {"fields": [{"name": n} for n in ("OBJECTID", *lpv.FIELDS, "OWNERNME1", "MLADDRESS")]}
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
    assert "Assessed value (tax year unconfirmed)" in js and "no tax bill is shown" in js
