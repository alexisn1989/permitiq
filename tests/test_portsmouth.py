"""Portsmouth and Hampton from the HRGEO regional parcel layer: only their county code is
read, only the listed fields are requested (the layer holds owner names and mailing
addresses), short ids load, other cities keep the 10-digit minimum. Synthetic data."""

import importlib.util
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import db_module, env  # noqa: E402,F401
from ingest import constraints as C, parcels as P  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lp", ROOT / "scripts" / "load_parcels.py")
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)


def _feat(oid, fips, taxmap, parcelid, x):
    return {"type": "Feature", "id": oid,
            "geometry": {"type": "Polygon", "coordinates": [[[x, 36.85], [x + .001, 36.85], [x + .001, 36.851], [x, 36.851], [x, 36.85]]]},
            "properties": {"OBJECTID": oid, "FIPS": fips, "TAXMAPNO": taxmap, "PARCELID": parcelid,
                           "PSTLADDRESS": "100 EXAMPLE ST", "ZONING": "R-75", "CALCAREA": 0.25,
                           "LASTUPDATE": 1772150400000, "OWNERNME1": "Jane Q Public", "MLADDRESS": "1 Private Ln"}}


FEATS = [_feat(1, "740", "123-456", "12004866", -76.35), _feat(2, "740", "123-457", "12004867", -76.34),
         _feat(3, "740", None, "12004868", -76.33), _feat(4, "650", None, "12004866", -76.40)]  # same PARCELID, Hampton


def _service(seen_where, seen_fields):
    def fetch(url, params=None, **kw):
        if not url.endswith("/query"):
            return {"geometryType": "esriGeometryPolygon", "objectIdField": "OBJECTID", "maxRecordCount": 2000,
                    "fields": [{"name": n} for n in ("OBJECTID", "FIPS", "TAXMAPNO", "PARCELID", "PSTLADDRESS", "ZONING",
                                                     "CALCAREA", "LASTUPDATE", "OWNERNME1", "MLADDRESS")]}
        seen_where.append(params.get("where"))
        fips = params.get("where", "").split("'")[1] if "'" in params.get("where", "") else None
        sel = [f for f in FEATS if fips is None or f["properties"]["FIPS"] == fips]
        if params.get("returnCountOnly"):
            return {"count": len(sel)}
        if params.get("returnIdsOnly"):
            return {"objectIds": [f["id"] for f in sel]}
        seen_fields.append(params["outFields"])
        keep = params["outFields"].split(",")
        ids = [int(x) for x in params["objectIds"].split(",")]
        return {"type": "FeatureCollection", "features": [
            {**f, "properties": {k: v for k, v in f["properties"].items() if k in keep}} for f in FEATS if f["id"] in ids]}
    return fetch


def test_regional_layer_reads_one_city_with_listed_fields_only(env, monkeypatch):
    where, fields = [], []
    monkeypatch.setattr(lp, "fetch_json", _service(where, fields))
    assert lp.main(["--locality", "portsmouth"]) == 0
    assert lp.main(["--locality", "hampton"]) == 0
    assert set(w for w in where if w) == {"FIPS = '740'", "FIPS = '650'"}
    assert fields and all("OWNER" not in f and "MLADDRESS" not in f and f != "*" for f in fields)
    with db_module.db_readonly(env["db"]) as conn:
        rows = {(r["locality"], r["gpin"]): dict(r) for r in conn.execute(
            "SELECT l.locality, l.source_vintage, p.* FROM parcels p JOIN parcel_loads l ON l.load_id = p.load_id")}
    assert set(rows) == {("portsmouth", "123-456"), ("portsmouth", "123-457"), ("hampton", "12004866")}
    pt = rows[("portsmouth", "123-456")]
    assert pt["zoning"] == "R-75" and pt["prop_address"] == "100 EXAMPLE ST" and pt["tax_area_sqft"] == 10890.0
    assert pt["source_vintage"] == "2026-02-27"
    # ids stay distinct across cities: Portsmouth's dashed tax-map number never equals a Hampton PARCELID
    assert len({g for _, g in rows}) == len(rows)


def test_min_digits_per_city_and_layers():
    f = lambda g: {"type": "Feature", "geometry": FEATS[0]["geometry"], "properties": {"GPIN": g, "TAXMAPNO": g, "PARCELID": g}}  # noqa: E731
    assert [p.gpin for p in P.normalize([f("123-456")], Counter(), "portsmouth")] == ["123-456"]
    assert [p.gpin for p in P.normalize([f("4001836")], Counter(), "hampton")] == ["4001836"]
    assert P.normalize([f("0891-0090")], Counter(), "norfolk") == []  # Norfolk ids stay at 10 digits
    for prefix, city in (("pt_", "portsmouth"), ("ht_", "hampton")):
        assert all(l.locality == city for k, l in C.LAYERS.items() if k.startswith(prefix))
    assert "State = 'VIRGINIA'" in C.LAYERS["ht_nrhp_points"].where  # Hampton is also in South Carolina
    assert C.LAYERS["ht_vdot_adt"].where.count("'City of Hampton'") == 2  # not Northampton / Southampton


def test_newport_news_parcels_and_layers():
    f = {"type": "Feature", "geometry": FEATS[0]["geometry"], "properties": {"PARCELID": "123456789", "ZONE": "R4",
         "SITEADDRESS": "1 EXAMPLE AVE", "USEDSCRP": "Single Family", "CLASSDSCRP": "Residential"}}
    assert [(p.gpin, p.attrs["zoning"], p.attrs["land_use"]) for p in P.normalize([f], Counter(), "newport_news")] == \
        [("123456789", "R4", "Single Family")]
    out = P.PARCEL_OUT_FIELDS["newport_news"]
    assert "OWNER" not in out and "PSTL" not in out and "SALE" not in out and "newport_news" not in P.PARCEL_WHERE
    assert all(l.locality == "newport_news" for k, l in C.LAYERS.items() if k.startswith("nn_"))
    assert "State = 'VIRGINIA'" in C.LAYERS["nn_nrhp_points"].where  # "Newport" is Rhode Island
