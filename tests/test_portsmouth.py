"""Portsmouth from VGIN statewide parcels: only FIPS 51740 is read, 8-digit tax-map ids
load, other cities keep the 10-digit minimum, and no owner field is stored. Synthetic data."""

import importlib.util
import json
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


def _feat(oid, fips, ptm, x):
    return {"type": "Feature", "id": oid,
            "geometry": {"type": "Polygon", "coordinates": [[[x, 36.85], [x + .001, 36.85], [x + .001, 36.851], [x, 36.851], [x, 36.85]]]},
            "properties": {"OBJECTID": oid, "FIPS": fips, "LOCALITY": "x", "PARCELID": str(oid), "PTM_ID": ptm,
                           "dataVersion": "2022 Q3", "Owner1": "Jane Q Public"}}


FEATS = [_feat(1, "51740", "0891-0090", -76.35), _feat(2, "51740", "0891-0100", -76.34), _feat(3, "51740", "-", -76.33),
         _feat(4, "51810", "1234-5678", -76.0)]  # another city in the statewide layer


def _service(seen):
    def fetch(url, params=None, **kw):
        if not url.endswith("/query"):
            return {"geometryType": "esriGeometryPolygon", "objectIdField": "OBJECTID", "maxRecordCount": 2000,
                    "fields": [{"name": n} for n in ("OBJECTID", "FIPS", "PTM_ID", "dataVersion")]}
        seen.append(params.get("where"))
        sel = [f for f in FEATS if params.get("where") == "1=1" or f["properties"]["FIPS"] == "51740"]
        if params.get("returnCountOnly"):
            return {"count": len(sel)}
        if params.get("returnIdsOnly"):
            return {"objectIds": [f["id"] for f in sel]}
        ids = [int(x) for x in params["objectIds"].split(",")]
        return {"type": "FeatureCollection", "features": [f for f in FEATS if f["id"] in ids]}
    return fetch


def test_statewide_layer_reads_only_portsmouth_and_short_ids_load(env, monkeypatch):
    seen = []
    monkeypatch.setattr(lp, "fetch_json", _service(seen))
    assert lp.main(["--locality", "portsmouth"]) == 0
    assert seen and all(w == "FIPS = '51740'" for w in seen if w)
    with db_module.db_readonly(env["db"]) as conn:
        load = dict(conn.execute("SELECT * FROM parcel_loads WHERE locality = 'portsmouth'").fetchone())
        rows = [dict(r) for r in conn.execute("SELECT * FROM parcels WHERE load_id = ?", (load["load_id"],))]
    assert sorted(r["gpin"] for r in rows) == ["0891-0090", "0891-0100"]  # "-" skipped, other FIPS never read
    assert load["source_vintage"] == "2022 Q3" and load["expected_count"] == 3
    assert "Jane Q Public" not in json.dumps(rows, default=str)


def test_min_digits_per_city():
    f = lambda g: {"type": "Feature", "geometry": FEATS[0]["geometry"], "properties": {"GPIN": g, "PTM_ID": g}}  # noqa: E731
    assert [p.gpin for p in P.normalize([f("0891-0090")], Counter(), "portsmouth")] == ["0891-0090"]
    assert P.normalize([f("0891-0090")], Counter(), "norfolk") == []  # Norfolk ids stay at 10 digits
    assert all(l.locality == "portsmouth" for k, l in C.LAYERS.items() if k.startswith("pt_"))
    assert "State = 'VIRGINIA'" in C.LAYERS["pt_nrhp_points"].where and C.LAYERS["pt_usda_soils"].url == "VA715"
