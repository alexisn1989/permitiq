"""Suffolk: city parcels requested without owner fields, development restrictions as
approved items. Synthetic data."""

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import client, db_module, env  # noqa: E402,F401
from ingest import constraints as C, suffolk_records as SR  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
lp, ls = _script("load_parcels"), _script("load_suffolk")
BOX = lambda x: {"type": "Polygon", "coordinates": [[[x, 36.7], [x + .001, 36.7], [x + .001, 36.701], [x, 36.701], [x, 36.7]]]}  # noqa: E731


def _parcel_service(seen):
    feats = [{"type": "Feature", "id": i, "geometry": BOX(-76.6 + i / 1000),
              "properties": {"OBJECTID": i, "GPIN": f"0453-11-39{i:02d}", "ZONE_CLASS": "RL", "NTI_ACREAGE": 1.5,
                             "OWNER": "Jane Q Public", "MAILING_ADDRESS1": "1 Private Ln"}} for i in (1, 2)]
    def fetch(url, params=None, **kw):
        if not url.endswith("/query"):
            return {"geometryType": "esriGeometryPolygon", "objectIdField": "OBJECTID", "maxRecordCount": 2000,
                    "fields": [{"name": n} for n in ("OBJECTID", "GPIN", "ZONE_CLASS", "NTI_ACREAGE", "OWNER")]}
        if params.get("returnCountOnly"):
            return {"count": 2}
        if params.get("returnIdsOnly"):
            return {"objectIds": [1, 2]}
        seen.append(params["outFields"])
        keep = params["outFields"].split(",") if params["outFields"] != "*" else None
        return {"type": "FeatureCollection", "features": [
            {**f, "properties": {k: v for k, v in f["properties"].items() if keep is None or k in keep}} for f in feats]}
    return fetch


def test_parcels_never_request_owner_fields(env, monkeypatch):
    seen = []
    monkeypatch.setattr(lp, "fetch_json", _parcel_service(seen))
    assert lp.main(["--locality", "suffolk"]) == 0
    assert seen and all("OWNER" not in f and "MAILING" not in f and f != "*" for f in seen)
    with db_module.db_readonly(env["db"]) as conn:
        rows = [dict(r) for r in conn.execute("SELECT gpin, zoning, tax_area_sqft FROM parcels")]
    assert rows[0]["gpin"] == "0453-11-3901" and rows[0]["zoning"] == "RL" and rows[0]["tax_area_sqft"] == 65340.0


def test_restrictions_merge_per_application_and_drop_comments(env, monkeypatch):
    MS = 1700000000000
    feats = [{"type": "Feature", "id": i, "geometry": BOX(-76.6 + i / 100), "properties": {
        "OBJECTID": i, "APPLICATION": app, "APPROVAL_DATE": MS, "AMEND_DATE": None, "RESTRICTION_TYPE": t,
        "Ordinance": "23-O-101" if t == "CZ" else None, "Document_Link": "https://suffolkgis.suffolk-va.net/x.pdf"}}
        for i, app, t in ((1, "RZ23-01", "CZ"), (2, "RZ23-01", "CZ"), (3, "BZA23-07", "VAR"))]
    rows = {r["case_id"]: r for r in SR.normalize(feats, Counter())}
    assert set(rows) == {"SF-RZ23-01", "SF-BZA23-07"} and rows["SF-RZ23-01"]["object_ids"] == [1, 2]
    assert rows["SF-RZ23-01"]["final_raw"] == "CZ approved 2023-11-14; ordinance 23-O-101"
    assert rows["SF-BZA23-07"]["case_type"] == "VAR" and rows["SF-BZA23-07"]["stage"] == "approved"
    assert "Comment" not in SR.OUT_FIELDS and "EDIT_BY" not in SR.OUT_FIELDS


def test_suffolk_layers():
    assert all(l.locality == "suffolk" for k, l in C.LAYERS.items() if k.startswith("sf_"))
    assert C._base("sf_rpa") == "rpa" and C.LAYERS["sf_usda_soils"].url == "VA800"
    assert "State = 'VIRGINIA'" in C.LAYERS["sf_nrhp_points"].where  # Suffolk is also in NY and MA
