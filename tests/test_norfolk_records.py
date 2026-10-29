"""Norfolk's approved land-use items (CUPs, conditional zoning). Synthetic features in the
confirmed field layout; no real names."""

import importlib.util
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import client, db_module, env  # noqa: E402,F401
from ingest import norfolk_records as NR  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("load_norfolk", ROOT / "scripts" / "load_norfolk.py")
ln = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ln)
MS = lambda d: int(__import__("datetime").datetime.fromisoformat(d).replace(tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)  # noqa: E731


def _cup(oid, ordn, address, effective="2024-10-22", expires=None, x=-76.25, y=36.88):
    return {"type": "Feature", "id": oid, "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": {"OBJECTID": oid, "CPCITEM": "123456", "ORDINANCE": ordn, "APPLICANT": "Jane Q Public",
                           "ADDRESS": address, "LINK": "https://gisshare.norfolk.gov/x", "TYPE": "Off-Lot Parking",
                           "EFFECTIVE_DATE": MS(effective), "EXPIRATION_DATE": MS(expires) if expires else None,
                           "NOTES": "requested by Jane Q Public"}}


def test_cups_merge_by_ordinance_flag_expiry_and_store_no_names():
    feats = [_cup(1, 40860.0, "801 EXAMPLE RD"), _cup(2, 40860.0, "900 EXAMPLE RD", x=-76.24),
             _cup(3, 49770.0, "1 EXAMPLE ST", expires="2020-01-01"), _cup(4, None, "5 EXAMPLE ST")]
    stats = Counter()
    rows = {r["case_id"]: r for r in NR.normalize_cups(feats, stats, today=date(2026, 10, 29))}
    assert set(rows) == {"NF-CUP-40860", "NF-CUP-49770", "NF-CUP-OID-4"}
    a = rows["NF-CUP-40860"]
    assert a["object_ids"] == [1, 2] and a["geom"].geom_type == "MultiPoint" and a["address"] == "801 EXAMPLE RD; 900 EXAMPLE RD"
    assert a["stage"] == "approved" and a["final_date"] == "2024-10-22" and "ordinance 40860" in a["final_raw"]
    assert "expired 2020-01-01" in rows["NF-CUP-49770"]["final_raw"]
    assert rows["NF-CUP-OID-4"]["stage"] == "other"
    assert "Jane Q Public" not in json.dumps({k: {x: str(y) for x, y in r.items()} for k, r in rows.items()})
    assert stats == Counter({"cup_without_ordinance": 1, "multi_site_ordinances": 1})


def _service(feats_by_url):
    def fetch(url, params=None, **kw):
        base = url.removesuffix("/query")
        feats = feats_by_url[base]
        if not url.endswith("/query"):
            names = NR.CUP_FIELDS if base == NR.SOURCES["nf_cups"] else NR.CZ_FIELDS
            return {"objectIdField": "OBJECTID", "fields": [{"name": "OBJECTID"}] + [{"name": n} for n in names]}
        if params.get("returnCountOnly"):
            return {"count": len(feats)}
        if params.get("returnIdsOnly"):
            return {"objectIds": [f["id"] for f in feats]}
        ids = [int(x) for x in params["objectIds"].split(",")]
        return {"type": "FeatureCollection", "features": [f for f in feats if f["id"] in ids]}
    return fetch


def test_loader_writes_cases_served_by_the_case_api(env, monkeypatch):
    cz = [{"type": "Feature", "id": 7, "geometry": {"type": "Polygon", "coordinates": [[[-76.3, 36.9], [-76.29, 36.9],
           [-76.29, 36.91], [-76.3, 36.91], [-76.3, 36.9]]]}, "properties": {"OBJECTID": 7, "ITEMNO": "123456",
           "ORDINANCE": "48001", "URL": "https://example.invalid/o", "NOTES": "x"}}]
    monkeypatch.setattr(ln, "fetch_json", _service({NR.SOURCES["nf_cups"]: [_cup(1, 40860.0, "801 EXAMPLE RD")],
                                                    NR.SOURCES["nf_conditional_zoning"]: cz}))
    assert ln.main(["--all"]) == 0
    assert ln.main(["--all"]) == 0  # identical reload writes nothing
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM city_record_loads WHERE locality = 'norfolk'").fetchone()[0] == 2
    d = client.get("/api/land-use/cases/NF-CUP-40860").json()
    assert d["locality"] == "norfolk" and d["final"]["stage"] == "approved" and d["project"]["name"] is None
    assert client.get("/api/land-use/cases/NF-CZ-48001").json()["case_type"] == "Conditional Zoning"


# ---------- permits -> parcels (GPIN path), isolated from Virginia Beach ----------

from test_parcels import matched, match, vp, add  # noqa: E402,F401
from test_chesapeake_permits import _snapshot  # noqa: E402


def test_norfolk_gpin_matching_and_vb_isolation(matched):
    from ingest import pc_hearings
    before = _snapshot(matched["db"])
    with db_module.db_readonly(matched["db"]) as conn:
        vb_g10 = conn.execute("SELECT gpin10 FROM parcels WHERE gpin10 IS NOT NULL LIMIT 1").fetchone()[0]
        vb_answer = pc_hearings.item_parcels(conn, [vb_g10])
    with db_module.db(matched["db"]) as conn:
        load_id = conn.execute(
            "INSERT INTO parcel_loads (source_url, content_hash, feature_count, expected_count, received_count, "
            "unique_source_ids, loaded_at, is_active, locality) VALUES ('x', 'h', 2, 2, 2, 2, 'now', 1, 'norfolk')").lastrowid
        for g, (x0, y0) in ((vb_g10, (-76.30, 36.90)), ("5000000001", (-76.28, 36.90))):  # one id equals a VB GPIN10
            geom = {"type": "Polygon", "coordinates": [[[x0, y0], [x0 + .001, y0], [x0 + .001, y0 + .001], [x0, y0 + .001], [x0, y0]]]}
            conn.execute("INSERT INTO parcels (load_id, gpin, gpin_digits, gpin10, source_rows, geom_geojson, minx, miny, "
                         "maxx, maxy) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                         (load_id, g, g, g, json.dumps(geom), x0, y0, x0 + .001, y0 + .001))
    nf = vp("NF1", "1 EXAMPLE ST, Norfolk, VA", gpin="5000000001", latlng=(36.9005, -76.2795), locality="norfolk")
    add(matched, nf)
    assert match(matched, "--locality", "norfolk") == 0
    with db_module.db_readonly(matched["db"]) as conn:
        r = dict(conn.execute("SELECT e.* FROM permit_parcel_effective e JOIN permits p ON p.id = e.permit_id "
                              "WHERE p.source_permit_id = 'NF1'").fetchone())
        assert pc_hearings.item_parcels(conn, [vb_g10]) == vb_answer  # a Norfolk parcel never answers a VB GPIN10
    assert (r["method"], r["display_gpin"], r["disputed"]) == ("gpin10_unique", "5000000001", 0)
    assert _snapshot(matched["db"]) == before
