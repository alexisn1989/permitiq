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


def test_identical_reload_writes_nothing(loaded, monkeypatch):
    monkeypatch.setattr(load_s, "fetch_json", _service(DATA))
    assert load_s.main(["--source", "ches_cases"]) == 0
    with db_module.db_readonly(loaded["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM city_record_loads WHERE source = 'ches_cases'").fetchone()[0] == 1
