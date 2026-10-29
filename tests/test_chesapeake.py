"""Chesapeake's published development records (synthetic fixtures in the real
field layout; invented names): complete loads, action codes from the layer's
own domain, final stages, legacy rows, names withheld, staff logins never
stored, records kept out of `permits`."""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import client, db_module, env  # noqa: E402,F401
from ingest import chesapeake_records as CH  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


load_s = _script("load_chesapeake")
DOMAIN = [("P", "Pending"), ("A", "Approved"), ("D", "Denied"), ("W", "Withdrawn"), ("NA", "Not_Applicable"),
          ("AS", "Approved with Stipulations"), ("AP", "Approved with Proffers")]


def _ms(d):
    return int(datetime.fromisoformat(d + "T00:00:00+00:00").timestamp() * 1000)


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def _case(oid, cid, final="Approved", cc="A", pc="AS", project="Example Commons, LLC", results="5 lot subdivision",
          applied="4/10/2025", geom=None):
    return {"type": "Feature", "id": oid, "geometry": geom or _box(-76.25, 36.70, -76.249, 36.701),
            "properties": {"OBJECTID": oid, "APPLICATIO": cid, "TYPE_": "Rezoning", "PROJECT_NA": project,
                           "ST_ADD": "100 Example Rd", "MAP_PARCEL": "0001000000010", "HOUSING_UN": 5,
                           "RESIDENTIAL": "YES", "ZONING_FRO": "A-1", "ZONING_TO": "R-15S", "DATEOFAPPL": applied,
                           "YR_APP": 2025, "STF_RCMNDTNS": "AS", "PC_ACTION": pc, "PCACTDATE": _ms("2025-06-11"),
                           "CC_ACTION": cc, "CCACTDATE": _ms("2025-07-15"), "ARC_ACTION": None, "ARCACTDATE": None,
                           "FINALAPPRO": final, "FINALACTIONDATE": _ms("2025-07-15"), "PLANNER": "Pat Planner",
                           "RESULTS": results, "EBUILD_LINK": "https://aca-prod.accela.com/CHESAPEAKE/x"}}


def _plan(oid, pid, contact="JDOE", applicant=None):
    return {"type": "Feature", "id": oid, "geometry": {"type": "Point", "coordinates": [-76.25, 36.70]},
            "properties": {"objectid": oid, "projectid": pid, "permittype": "DPLAN", "permitstat": "In Review",
                           "details": "Example site plan", "applicant": applicant, "contractor": None, "fulladdr": None,
                           "submitdt": _ms("2026-09-01"), "approvedt": None, "parcelid": "0001000000010",
                           "contact": contact, "created_user": "staff@example.invalid"}}


CASES = [_case(1, "PLN-REZ-2025-001"), _case(2, "PLN-REZ-2025-001", geom=_box(-76.249, 36.70, -76.248, 36.701)),
         _case(3, "PLN-USE-2025-002", final="Withdrawn", cc="W", project="Jane Q Public"),
         _case(4, "PLN-USE-2014-003", final="Approved D/P", results="Added from Dev & Permits Shapefile 05/29/2014",
               applied="sometime")]
PLANS = [_plan(10, "DEV-SITE-2026-001", applicant="Jane Q Public"), _plan(11, "DEV-SITE-2026-002")]


def _service(features_by_url, domain=True, count_delta=0):
    def fetch(url, params=None, **kw):
        base = url.removesuffix("/query")
        feats = features_by_url[base]
        if not url.endswith("/query"):
            case = base == CH.SOURCES["ches_cases"]
            names = CH.CASE_FIELDS if case else CH.RECORD_FIELDS
            fields = [{"name": "OBJECTID" if case else "objectid"}]
            for n in names:
                f = {"name": n}
                if case and n in CH.ACTION_FIELDS and domain:
                    f["domain"] = {"codedValues": [{"code": c, "name": l} for c, l in DOMAIN]}
                fields.append(f)
            return {"fields": fields, "objectIdField": "OBJECTID" if case else "objectid"}
        if params.get("returnCountOnly"):
            return {"count": len(feats) + count_delta}
        if params.get("returnIdsOnly"):
            return {"objectIds": [f["id"] for f in feats]}
        ids = [int(x) for x in params["objectIds"].split(",")]
        return {"type": "FeatureCollection", "features": [f for f in feats if f["id"] in ids]}
    return fetch


DATA = {CH.SOURCES["ches_cases"]: CASES, CH.SOURCES["ches_dev_plans"]: PLANS, CH.SOURCES["ches_row_permits"]: []}


@pytest.fixture
def loaded(env, monkeypatch):
    monkeypatch.setattr(load_s, "fetch_json", _service(DATA))
    assert load_s.main(["--source", "ches_cases", "--source", "ches_dev_plans"]) == 0
    return env


def test_cases_merge_stage_codes_and_legacy(loaded):
    with db_module.db_readonly(loaded["db"]) as conn:
        rows = {r["case_id"]: dict(r) for r in conn.execute("SELECT * FROM land_use_cases_effective")}
        domain = json.loads(conn.execute("SELECT domain_json FROM city_record_loads WHERE source = 'ches_cases'"
                                         ).fetchone()[0])
    assert set(rows) == {"PLN-REZ-2025-001", "PLN-USE-2025-002", "PLN-USE-2014-003"}  # two polygons, one case
    a = rows["PLN-REZ-2025-001"]
    assert a["stage"] == "approved" and a["cc_action"] == "A" and a["pc_action"] == "AS"
    assert a["applied_date"] == "2025-04-10" and a["cc_date"] == "2025-07-15"
    assert json.loads(a["geom_geojson"])["type"] in ("Polygon", "MultiPolygon")
    assert rows["PLN-USE-2025-002"]["stage"] == "withdrawn"
    legacy = rows["PLN-USE-2014-003"]
    assert legacy["legacy"] == 1 and legacy["stage"] == "other" and legacy["final_raw"] == "Approved D/P"
    assert legacy["applied_date"] is None
    pub = CH.public_case(a, domain)
    assert pub["city_council"]["action"] == "Approved" and pub["planning_commission"]["action"] == "Approved with Stipulations"
    assert CH.public_case(rows["PLN-USE-2025-002"], domain)["project"] == {"name": None, "withheld": True}


def test_unknown_code_or_missing_domain_or_incomplete_writes_nothing(env, monkeypatch, capsys):
    bad = [_case(1, "PLN-X-1", cc="ZZ")]
    monkeypatch.setattr(load_s, "fetch_json", _service({**DATA, CH.SOURCES["ches_cases"]: bad}))
    assert load_s.main(["--source", "ches_cases"]) == 3 and "not in the layer's domain" in capsys.readouterr().err
    monkeypatch.setattr(load_s, "fetch_json", _service(DATA, domain=False))
    assert load_s.main(["--source", "ches_cases"]) == 3 and "refusing to guess" in capsys.readouterr().err
    monkeypatch.setattr(load_s, "fetch_json", _service(DATA, count_delta=1))
    assert load_s.main(["--source", "ches_cases"]) == 3
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM city_record_loads").fetchone()[0] == 0


def test_records_never_store_staff_logins_and_stay_out_of_permits(loaded):
    with db_module.db_readonly(loaded["db"]) as conn:
        dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM development_records")])
        n_permits = conn.execute("SELECT COUNT(*) FROM permits WHERE locality = 'chesapeake'").fetchone()[0]
        r = dict(conn.execute("SELECT * FROM development_records WHERE record_id = 'DEV-SITE-2026-001'").fetchone())
    assert "JDOE" not in dump and "staff@example.invalid" not in dump
    assert n_permits == 0
    pub = CH.public_record(r)
    assert pub["organization"] == {"name": None, "withheld": True} and pub["submitted_date"] == "2026-09-01"
    assert pub["details"] == {"name": None, "withheld": True}  # "Example site plan" is not clearly an organization


def test_identical_reload_writes_nothing(loaded, monkeypatch):
    monkeypatch.setattr(load_s, "fetch_json", _service(DATA))
    assert load_s.main(["--source", "ches_cases"]) == 0
    with db_module.db_readonly(loaded["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM city_record_loads WHERE source = 'ches_cases'").fetchone()[0] == 1


# ---------- parcels and constraints per locality ----------

from collections import Counter  # noqa: E402

from ingest import constraints as C, parcels as P  # noqa: E402


def _parcel_feat(oid, map_parcel, box, address="100 EXAMPLE RD", acres=2.0):
    return {"type": "Feature", "id": oid, "geometry": _box(*box),
            "properties": {"OBJECTID": oid, "MAP_PARCEL": map_parcel, "PROPCLASS": "2010", "ADDRESS": address,
                           "CALCACREAGE": acres}}


def test_chesapeake_parcel_field_map():
    stats = Counter()
    rows = P.normalize([_parcel_feat(1, "0001000000010", (-76.25, 36.70, -76.249, 36.701)),
                        _parcel_feat(2, None, (-76.25, 36.70, -76.249, 36.701))], stats, "chesapeake")
    assert [r.gpin for r in rows] == ["0001000000010"] and stats["skipped_no_gpin"] == 1
    assert rows[0].attrs["tax_area_sqft"] == 87120.0 and rows[0].attrs["zoning"] is None
    assert P.missing_fields({"MAP_PARCEL", "PROPCLASS", "ADDRESS", "CALCACREAGE"}, "chesapeake") == []
    assert "PAR_GPIN" in P.missing_fields({"MAP_PARCEL"}, "virginia_beach")


def test_city_layers_share_national_behaviour_and_keys():
    assert C.LAYERS["ches_usda_soils"].source == "sda" and C.LAYERS["ches_usda_soils"].url == "VA550"
    assert C._base("ches_nrhp_polygons") == "nrhp_polygons" and C._base("rpa") == "rpa"
    assert all(l.locality == "chesapeake" for k, l in C.LAYERS.items() if k.startswith("ches_"))
    assert all(l.locality == "norfolk" for k, l in C.LAYERS.items() if k.startswith("nf_"))
    assert all(l.locality == "virginia_beach" for k, l in C.LAYERS.items() if not k.startswith(("ches_", "nf_")))
    assert C.ENVELOPES["chesapeake"] != C.ENVELOPES["virginia_beach"] != C.ENVELOPES["norfolk"]
    assert C._base("nf_usda_soils") == "usda_soils" and C.LAYERS["nf_usda_soils"].url == "VA715"
    assert "State = 'VIRGINIA'" in C.LAYERS["nf_nrhp_points"].where  # not Norfolk County, Massachusetts
    assert "ATTRIBUTE <> 'U'" in C.LAYERS["nf_wetlands"].where  # upland is not a wetland


def test_constraint_tagging_only_measures_its_own_locality(env, monkeypatch):
    lp = _script("load_parcels")
    tag = _script("tag_parcel_constraints")
    lc = _script("load_constraints")
    feats = [_parcel_feat(1, "0001000000010", (-76.25, 36.70, -76.249, 36.701))]
    stub = _service({P.PARCEL_SOURCES["chesapeake"]: feats})

    def fetch(url, params=None, **kw):
        if not url.endswith("/query") and url == P.PARCEL_SOURCES["chesapeake"]:
            return {"geometryType": "esriGeometryPolygon", "objectIdField": "OBJECTID", "maxRecordCount": 2000,
                    "fields": [{"name": n} for n in ("OBJECTID", "MAP_PARCEL", "PROPCLASS", "ADDRESS", "CALCACREAGE")]}
        return stub(url, params)
    monkeypatch.setattr(lp, "fetch_json", fetch)
    assert lp.main(["--locality", "chesapeake"]) == 0
    rpa = [{"type": "Feature", "id": 1, "geometry": _box(-76.2505, 36.6995, -76.2495, 36.7015),
            "properties": {"OBJECTID": 1, "Distance": 100}}]
    rstub = _service({C.LAYERS["ches_rpa_buffer"].url: rpa})

    def rfetch(url, params=None, **kw):
        if not url.endswith("/query"):
            return {"geometryType": "esriGeometryPolygon", "objectIdField": "OBJECTID",
                    "fields": [{"name": "OBJECTID"}, {"name": "Distance"}]}
        return rstub(url, params)
    monkeypatch.setattr(lc, "fetch_json", rfetch)
    assert lc.main(["--layer", "ches_rpa_buffer"]) == 0
    assert tag.main(["--all"]) == 1  # the Virginia Beach run refuses: no Virginia Beach parcel load
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parcel_constraints").fetchone()[0] == 0
    assert tag.main(["--all", "--locality", "chesapeake"]) == 0
    with db_module.db_readonly(env["db"]) as conn:
        r = conn.execute("SELECT * FROM parcel_constraints_effective WHERE layer_key = 'ches_rpa_buffer'").fetchone()
        assert r["gpin"] == "0001000000010" and r["share"] == 0.5
        assert C.describe("ches_rpa_buffer", dict(r)) == "1.22 ac (50%) of the parcel inside: Chesapeake Bay RPA buffer"
        block = C.public_constraints(conn, ["0001000000010"])
        assert [x["layer"] for x in block["layers_checked"]] == ["ches_rpa_buffer"]
        assert not any("pipe locations" in x for x in block["not_covered"])
        assert not C.public_constraints(conn, [])["layers_checked"]  # no gpins: Virginia Beach's layers only


# ---------- API ----------

def test_case_and_record_routes(loaded):
    r = client.get("/api/land-use/cases").json()
    assert {c["case_id"] for c in r["cases"]} == {"PLN-REZ-2025-001", "PLN-USE-2025-002"}  # legacy hidden by default
    assert r["load"]["source"] == "ches_cases" and "candidates" in r["notice"]
    assert len(client.get("/api/land-use/cases?include_legacy=true").json()["cases"]) == 3
    assert [c["case_id"] for c in client.get("/api/land-use/cases?stage=withdrawn").json()["cases"]] == \
        ["PLN-USE-2025-002"]
    assert client.get("/api/land-use/cases?stage=bogus").status_code == 422
    d = client.get("/api/land-use/cases/PLN-REZ-2025-001").json()
    assert d["city_council"]["action"] == "Approved" and d["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert d["parcels"] == [] and "site_constraints" in d
    assert client.get("/api/land-use/cases/none").status_code == 404
    body = client.get("/api/land-use/cases?include_legacy=true").text + client.get("/api/development-records").text
    assert "Jane Q Public" not in body and "JDOE" not in body
    assert "5 lot subdivision" not in body and "Pat Planner" not in body  # free-text notes and staff never served
    recs = client.get("/api/development-records?source=ches_dev_plans").json()
    assert {x["record_id"] for x in recs["records"]} == {"DEV-SITE-2026-001", "DEV-SITE-2026-002"}
    assert client.get("/api/development-records?source=ches_cases").status_code == 422


import shutil, subprocess  # noqa: E402,E401

node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node not installed")
def test_case_html_escapes_and_withholds(loaded):
    c = client.get("/api/land-use/cases/PLN-USE-2025-002").json()
    c["address"] = "<img src=x>"
    c["source_link"] = "javascript:alert(1)"
    js = "const u = require(process.argv[1]); process.stdout.write(u.buildCaseHtml(JSON.parse(process.argv[2])))"
    out = subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js"), json.dumps(c)],
                         capture_output=True, text=True, check=True).stdout
    assert "Withdrawn" in out and "The project name is an individual — name not shown" in out
    assert "<img" not in out and 'href="javascript' not in out and "Jane Q Public" not in out
    assert "No permit data loaded for this city." in out and "candidates" in out


def test_report_csvs_withhold_names(loaded, tmp_path):
    import csv as _csv
    rep = _script("chesapeake_report")
    assert rep.main(["--out", str(tmp_path)]) == 0
    cases = list(_csv.DictReader((tmp_path / "chesapeake_cases.csv").open()))
    plans = list(_csv.DictReader((tmp_path / "development_plans.csv").open()))
    assert len(cases) == 3 and len(plans) == 2
    assert {c["stage"] for c in cases} == {"approved", "withdrawn", "other"}
    text = (tmp_path / "chesapeake_cases.csv").read_text() + (tmp_path / "development_plans.csv").read_text()
    assert "Jane Q Public" not in text and "JDOE" not in text


def test_city_layers_expected_only_once_that_city_has_parcels(env):
    from ingest import quality as Q
    with db_module.db(env["db"]) as conn:
        conn.execute("INSERT INTO constraint_loads (layer_key, source_url, where_clause, expected_count, received_count, "
                     "unique_ids, features, content_hash, loaded_at, is_active) "
                     "VALUES ('cbpa_rpa', 'x', '1=1', 0, 0, 0, 0, 'h', 'now', 1)")
        missing = Q.constraint_quality(conn)["missing_layers"]
        assert missing and not [k for k in missing if k.startswith("ches_")]
        conn.execute("INSERT INTO parcel_loads (source_url, content_hash, feature_count, expected_count, received_count, "
                     "unique_source_ids, loaded_at, is_active, locality) VALUES ('x', 'h', 0, 0, 0, 0, 'now', 1, "
                     "'chesapeake')")
        assert "ches_rpa_buffer" in Q.constraint_quality(conn)["missing_layers"]


def test_pump_station_fact_names_station_and_hrsd_connection():
    row = {"detail_json": json.dumps({"by": [{"name": 28, "acres": 0.44, "share": 1.0,
                                              "attrs": {"PUMP_STATI": 28, "HRSDCONNCT": "Direct"}}]})}
    assert C.describe("ches_pump_station_areas", row) == \
        "Service area of pump station 28: 0.44 ac (100%), HRSD connection Direct (as published)"
