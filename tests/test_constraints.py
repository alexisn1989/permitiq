"""Site constraints: complete layer loads, per-parcel measurements (polygon share,
line crossing / distance -- never RPA acres), staleness, board-action history
with names withheld, API, CSV, quality (synthetic geometry over the fixture
parcels)."""

import csv
import importlib.util
import json
import math
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_filings import FEATURES, LAYER, _box, _feat, _stub, load_s  # noqa: E402,F401
from test_parcels import client, db_module, env, flooded, matched  # noqa: E402,F401
from ingest import constraints as C  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FT_LON = C.COS0 * C.FT_PER_DEG  # feet per degree of longitude at the city


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lc_s, tag_s, ba_s, fr_s = (_script(n) for n in ("load_constraints", "tag_parcel_constraints", "load_board_actions",
                                                  "filing_report"))


def _poly(oid, geom, **props):
    return {"type": "Feature", "id": oid, "geometry": geom, "properties": {"OBJECTID": oid, **props}}


def _line(oid, coords):
    return {"type": "Feature", "id": oid, "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"OBJECTID": oid}}


# parcel 14892864850000: lon -76.004..-76.002, lat 36.804..36.806 (a 0.002 x 0.002 degree box)
SWB = [_poly(1, _box(-76.005, 36.803, -76.003, 36.807))]                      # covers its west half
RPA = [_line(1, [[-76.0095, 36.803], [-76.0095, 36.807]]),                     # crosses parcel 2400... (0.002 deg)
       _line(2, [[-75.985 + 50 / FT_LON, 36.804], [-75.985 + 50 / FT_LON, 36.806]])]  # 50 ft east of 2410...
HIST = [_poly(1, _box(-76.0420, 36.8030, -76.0380, 36.8070), Name="<b>Test District</b>")]  # covers 2430...
# a pump-station project site 300 ft north of parcel 1489... (no overlap: reported by distance)
PS = [_poly(1, _box(-76.0035, 36.806 + 300 / C.FT_PER_DEG, -76.0030, 36.8065 + 300 / C.FT_PER_DEG),
            PU_NUM="X01-001", PROJECT_NAME="Example Pump Station PS-999")]
GEOMETRY = {"southern_watersheds_buffer": "esriGeometryPolygon", "rpa": "esriGeometryPolyline",
            "historic_overlay": "esriGeometryPolygon", "sewer_pump_station_projects": "esriGeometryPolygon",
            "vdot_adt": "esriGeometryPolyline", "nrhp_points": "esriGeometryPoint",
            "vdot_syip_lines": "esriGeometryPolyline", "nwi_wetlands": "esriGeometryPolygon"}


def _layers(data, count_delta=0):
    """A stub service per layer URL; any other layer answers an error."""
    stubs = {C.LAYERS[k].url: (_stub(v, count_delta=count_delta)[0], k) for k, v in data.items()}

    def fetch(url, params=None, **kw):
        base = url.removesuffix("/query")
        if base not in stubs:
            raise RuntimeError("503 Service Unavailable")
        if params.get("f") == "json" and not url.endswith("/query"):
            k = stubs[base][1]
            return {"geometryType": GEOMETRY[k], "objectIdField": "OBJECTID",
                    "fields": [{"name": "OBJECTID"}] + [{"name": a} for a in C.LAYERS[k].attrs]}
        return stubs[base][0](url, params)
    return fetch


DATA = {"southern_watersheds_buffer": SWB, "rpa": RPA, "historic_overlay": HIST, "sewer_pump_station_projects": PS}


@pytest.fixture
def measured(flooded, monkeypatch):
    monkeypatch.setattr(lc_s, "fetch_json", _layers(DATA))
    assert lc_s.main(["--layer", "southern_watersheds_buffer", "--layer", "rpa", "--layer", "historic_overlay",
                      "--layer", "sewer_pump_station_projects"]) == 0
    fetch, _ = _stub(FEATURES)
    monkeypatch.setattr(load_s, "fetch_json", fetch)
    assert load_s.main(["--url", LAYER]) == 0
    cbpa = [_feat(301, "2019-CBPA-00007", "2019-05-02", _box(-76.0045, 36.8035, -76.0015, 36.8065), "CBPA",
                  "Jane Q Public", "Jane Q Public")]
    monkeypatch.setattr(ba_s, "fetch_json", _layers_board(cbpa))
    assert ba_s.main(["--layer", "cbpa"]) == 0
    assert tag_s.main(["--all"]) == 0
    return flooded


def _layers_board(feats):
    url = C.BOARD_LAYERS["cbpa"][0]
    stub, _ = _stub(feats)

    def fetch(u, params=None, **kw):
        if not u.startswith(url):
            raise RuntimeError("503")
        return stub(u, params)
    return fetch


def _rows(env_):
    with db_module.db_readonly(env_["db"]) as conn:
        return {(r["gpin"], r["layer_key"]): dict(r) for r in conn.execute("SELECT * FROM parcel_constraints_effective")}


def test_polygon_share_and_acres(measured):
    r = _rows(measured)[("14892864850000", "southern_watersheds_buffer")]
    assert r["share"] == 0.5
    expected_sqft = (0.001 * FT_LON) * (0.002 * C.FT_PER_DEG)
    assert r["overlap_sqft"] == pytest.approx(expected_sqft, rel=1e-4)
    h = _rows(measured)[("24300000000000", "historic_overlay")]
    assert h["share"] == 1.0 and json.loads(h["detail_json"])["by"][0]["name"] == "<b>Test District</b>"


def test_pump_station_project_reported_by_distance(measured):
    r = _rows(measured)[("14892864850000", "sewer_pump_station_projects")]
    assert r["overlap_sqft"] is None and r["distance_ft"] == pytest.approx(300, abs=1)
    assert json.loads(r["detail_json"])["nearest"]["PROJECT_NAME"] == "Example Pump Station PS-999"
    assert C.describe("sewer_pump_station_projects", r) == "Sewer pump station project 300 ft from the parcel"
    assert ("24000000000000", "sewer_pump_station_projects") not in _rows(measured)  # beyond 1,000 ft


def test_line_crossing_and_distance_never_area(measured):
    rows = _rows(measured)
    cross = rows[("24000000000000", "rpa")]
    assert cross["line_length_ft"] == pytest.approx(0.002 * C.FT_PER_DEG, rel=1e-3)
    assert cross["overlap_sqft"] is None and cross["share"] is None and cross["distance_ft"] == 0
    near = rows[("24100000000000", "rpa")]
    assert near["line_length_ft"] == 0 and near["distance_ft"] == pytest.approx(50, abs=0.5)
    assert ("14892864850000", "rpa") not in rows  # far from any line: no row
    assert "crosses the parcel" in C.describe("rpa", cross) and "acres" not in C.describe("rpa", cross)
    assert C.describe("rpa", near).endswith("lies 50 ft from the parcel")


def test_incomplete_or_wrong_layer_writes_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(lc_s, "fetch_json", _layers(DATA, count_delta=1))
    assert lc_s.main(["--layer", "rpa"]) == 3
    monkeypatch.setattr(lc_s, "fetch_json", _layers({"rpa": RPA}))
    assert lc_s.main(["--layer", "rpa", "--layer", "dam_break"]) == 3  # dam_break unreachable: rpa still loads
    with db_module.db_readonly(env["db"]) as conn:
        assert [r[0] for r in conn.execute("SELECT layer_key FROM constraint_loads WHERE is_active = 1")] == ["rpa"]
    bad = dict(GEOMETRY, rpa="esriGeometryPolygon")
    monkeypatch.setattr(sys.modules[__name__], "GEOMETRY", bad)
    monkeypatch.setattr(lc_s, "fetch_json", _layers({"rpa": RPA}))
    assert lc_s.main(["--layer", "rpa"]) == 3 and "wrong layer number" in capsys.readouterr().err


def test_new_load_makes_measurements_stale_until_retagged(measured, monkeypatch):
    moved = [_poly(1, _box(-76.005, 36.803, -76.0035, 36.807))]  # now a quarter of the parcel
    monkeypatch.setattr(lc_s, "fetch_json", _layers({"southern_watersheds_buffer": moved}))
    assert lc_s.main(["--layer", "southern_watersheds_buffer"]) == 0
    assert ("14892864850000", "southern_watersheds_buffer") not in _rows(measured)
    assert tag_s.main(["--all"]) == 0
    assert _rows(measured)[("14892864850000", "southern_watersheds_buffer")]["share"] == 0.25
    monkeypatch.setattr(lc_s, "fetch_json", _layers({"southern_watersheds_buffer": moved}))
    with db_module.db_readonly(measured["db"]) as conn:
        n = conn.execute("SELECT COUNT(*) FROM constraint_loads").fetchone()[0]
    assert lc_s.main(["--layer", "southern_watersheds_buffer"]) == 0  # identical: nothing new
    with db_module.db_readonly(measured["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM constraint_loads").fetchone()[0] == n


def test_api_csv_and_names(measured, tmp_path, monkeypatch):
    body = client.get("/api/parcels/14892864850000/constraints").json()
    text = json.dumps(body)
    assert "Jane Q Public" not in text and "not a determination" in body["notice"]
    swb = next(c for c in body["constraints"] if c["layer"] == "southern_watersheds_buffer")
    assert swb["share"] == 0.5 and "(50%)" in swb["fact"]
    assert body["board_history"][0]["pln_id"] == "2019-CBPA-00007"
    assert body["board_history"][0]["applicant"] == {"name": None, "withheld": True}
    assert any("sewer" in x for x in body["not_covered"])
    assert client.get("/api/parcels/99999999999999/constraints").status_code == 404
    f = client.get("/api/land-use/filings/2025-PCCC-00001").json()
    assert {c["layer"] for c in f["site_constraints"]["constraints"]} == {"southern_watersheds_buffer",
                                                                         "sewer_pump_station_projects"}
    fc = client.get("/api/constraints/rpa.geojson?bbox=-76.02,36.80,-75.98,36.81").json()
    assert len(fc["features"]) == 2
    assert client.get("/api/constraints/nope.geojson?bbox=-76.02,36.80,-75.98,36.81").status_code == 404
    assert client.get("/api/constraints/rpa.geojson?bbox=-78,36,-75,37").status_code == 422
    import app as app_module
    monkeypatch.setattr(app_module, "CONSTRAINT_BBOX_MAX", 1)
    assert client.get("/api/constraints/rpa.geojson?bbox=-76.02,36.80,-75.98,36.81").status_code == 413
    assert fr_s.main(["--out", str(tmp_path)]) == 0
    rows = {r["pln_id"]: r for r in csv.DictReader((tmp_path / "filing_leads.csv").open())}
    assert rows["2025-PCCC-00001"]["southern_watersheds_buffer_share"] == "0.5"
    assert rows["2025-PCCC-00001"]["prior_cbpa_actions"] == "1"
    assert rows["2025-PCCC-00001"]["sewer_pump_station_project_ft"] == "300"
    assert rows["2026-PCCC-00002"]["rpa"] in ("crosses", "within 50 ft")
    assert "Jane Q Public" not in (tmp_path / "filing_leads.csv").read_text()


def test_quality_section_and_checklist(measured):
    from ingest import quality
    with db_module.db_readonly(measured["db"]) as conn:
        q = quality.quality(conn)
    sq = q["site_constraints"]
    assert sq["layers"]["rpa"]["features"] == 2 and "usda_soils" in sq["missing_layers"]
    line = next(x for x in quality.acceptance_checklist(q, None) if x["item"] == "Constraint layers complete")
    assert line["passed"] is False and "usda_soils" in line["detail"]


def test_v11_to_v12_migration_keeps_data(env):
    conn = sqlite3.connect(env["db"])
    n = conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0]
    for v in ("parcel_constraints_effective", "board_actions_effective"):
        conn.execute(f"DROP VIEW {v}")
    for t in ("board_action_parcels", "board_actions", "board_action_loads", "parcel_constraints",
              "constraint_features", "constraint_loads"):
        conn.execute(f"DROP TABLE {t}")
    conn.execute("PRAGMA user_version=11")
    conn.commit()
    conn.close()
    db_module.init_db(env["db"])
    with db_module.db_readonly(env["db"]) as c:
        assert c.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == n
        assert c.execute("SELECT COUNT(*) FROM constraint_loads").fetchone()[0] == 0


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node not installed")
def test_constraints_html_escapes_and_says_screening():
    sc = {"constraints": [{"gpin": "1", "label": "<i>RPA</i>", "fact": "<script>x</script>", "as_of": "2026-10-28T1",
                           "detail": {"by": [{"name": "<b>X</b>", "acres": 1.5}]}}],
          "board_history": [{"date_sub": "2019-05-02", "board": "cbpa", "pln_id": "<u>P</u>", "pln_type": "CBPA",
                             "applicant": {"name": "<i>Acme</i> LLC", "withheld": False}}],
          "layers_checked": [{"label": "RPA"}], "boards_checked": [{"board": "cbpa"}],
          "not_covered": ["sewer / water capacity (no public data)"], "notice": "Screening estimates"}
    js = "const u = require(process.argv[1]); process.stdout.write(u.buildConstraintsHtml(JSON.parse(process.argv[2])))"
    out = subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js"), json.dumps(sc)],
                         capture_output=True, text=True, check=True).stdout
    assert "<script>" not in out and "<b>X" not in out and "<u>P" not in out and "&lt;i&gt;Acme&lt;/i&gt; LLC" in out
    assert "Site constraints (screening)" in out and "Chesapeake Bay Preservation Area Board" in out
    assert "sewer / water capacity (no public data)" in out


# ---------- phase 2: VDOT, National Register, NWI, USDA soils ----------

def _feat_line(oid, coords, **props):
    return {"type": "Feature", "id": oid, "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"OBJECTID": oid, **props}}


def _pt(oid, lng, lat, **props):
    return {"type": "Feature", "id": oid, "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {"OBJECTID": oid, **props}}


E = -76.002  # east edge of parcel 14892864850000
ADT = [_feat_line(1, [[E + 100 / FT_LON, 36.803], [E + 100 / FT_LON, 36.807]], ROUTE_COMMON_NAME="Example Rd",
                  ADT=12000, ADT_QUALITY="A", DATA_DATE=1735689600000),
       _feat_line(2, [[E + 250 / FT_LON, 36.803], [E + 250 / FT_LON, 36.807]], ROUTE_COMMON_NAME="Far Rd",
                  ADT=50000, ADT_QUALITY="G", DATA_DATE=1735689600000)]
NRHP_PTS = [_pt(1, E + 400 / FT_LON, 36.805, RESNAME="<b>Example House</b>", NRIS_Refnum="1", ResType="building")]
SYIP = [_feat_line(1, [[E + 900 / FT_LON, 36.803], [E + 900 / FT_LON, 36.807]], UPC=123, DESCRIPTION="EXAMPLE WIDENING",
                   SCOPE_OF_WORK_DESC="Widening", CN_START_DATE=1767225600000, TOTAL_ALLOCATIONS_CURRENT=1e6,
                   PROJECT_MANAGER="Pat Manager")]
NWI_F = [_poly(1, _box(-76.004, 36.804, -76.003, 36.805), ATTRIBUTE="PFO1A", WETLAND_TYPE="Freshwater Forested/Shrub Wetland")]


def test_nearest_attributes_points_and_no_person_fields(measured, monkeypatch):
    data = {"vdot_adt": ADT, "nrhp_points": NRHP_PTS, "vdot_syip_lines": SYIP, "nwi_wetlands": NWI_F}
    monkeypatch.setattr(lc_s, "fetch_json", _layers(data))
    assert lc_s.main([x for k in data for x in ("--layer", k)]) == 0
    assert tag_s.main(["--all"]) == 0
    r = _rows(measured)
    adt = r[("14892864850000", "vdot_adt")]
    nearest = json.loads(adt["detail_json"])["nearest"]
    assert adt["distance_ft"] == pytest.approx(100, abs=0.5) and nearest["ADT"] == 12000  # the nearest, not the busiest
    assert nearest["DATA_DATE"] == "2025-01-01"
    assert C.describe("vdot_adt", adt) == ("VDOT traffic count (ADT) lies 100 ft from the parcel: Example Rd: "
                                           "ADT 12,000 (2025-01-01, quality code A)")
    pt = r[("14892864850000", "nrhp_points")]
    assert pt["distance_ft"] == pytest.approx(400, abs=0.5) and "Example House" in C.describe("nrhp_points", pt)
    sy = r[("14892864850000", "vdot_syip_lines")]
    assert sy["distance_ft"] == pytest.approx(900, abs=0.5) and "construction start 2026-01-01" in C.describe(
        "vdot_syip_lines", sy)
    assert r[("14892864850000", "nwi_wetlands")]["share"] == 0.25
    body = json.dumps(client.get("/api/parcels/14892864850000/constraints").json())
    assert "Pat Manager" not in body and "PROJECT_MANAGER" not in body
    with db_module.db_readonly(measured["db"]) as conn:
        assert "Pat Manager" not in json.dumps([dict(x) for x in conn.execute("SELECT attrs_json FROM constraint_features")])
        summ = C.csv_summary(conn, ["14892864850000"])
    assert summ["nearest_adt"] == "12000 (Example Rd, 100 ft)" and summ["syip_within_ft"] == 900
    assert summ["nrhp"] == "listed site 400 ft" and summ["nwi_wetland_share"] == 0.25


def _sda_stub(polys, units, count=None, version="9/11/2026 3:42:55 PM"):
    """polys: [(mupolygonkey, mukey, wkt)]; units: {mukey: attrs}."""
    def query(sql):
        if sql.startswith("SELECT COUNT(*)"):
            return [{"n": len(polys) if count is None else count}]
        if sql.startswith("SELECT saverest"):
            return [{"saverest": version}]
        if sql.startswith("SELECT DISTINCT mukey"):
            return [{"mukey": m} for m in sorted({m for _, m, _ in polys})]
        if sql.startswith("SELECT mu.mukey"):
            return [{"mukey": k, **v} for k, v in units.items()]
        keys = sql.split("mukey IN (")[1].split(")")[0].split(",")
        return [{"mupolygonkey": k, "mukey": m, "wkt": w} for k, m, w in polys if str(m) in keys]
    return query


def _wkt(x0, y0, x1, y1):
    return f"POLYGON (({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"


SDA_UNITS = {"1": {"musym": "1", "muname": "Wet loam", "hydclprs": "96", "wtdepannmin": "15", "hydgrpdcd": "C/D",
                   "drclassdcd": "Poorly drained"},
             "2": {"musym": "2", "muname": "Dry sand", "hydclprs": "0", "wtdepannmin": "153", "hydgrpdcd": "A",
                   "drclassdcd": "Well drained"}}
SDA_POLYS = [(11, "1", _wkt(-76.005, 36.803, -76.0035, 36.807)),   # west quarter of parcel 1489...
             (12, "2", _wkt(-76.0035, 36.803, -76.0, 36.807))]


def test_usda_soils_complete_load_and_derived_shares(measured, monkeypatch):
    monkeypatch.setattr(lc_s, "sda_query", _sda_stub(SDA_POLYS, SDA_UNITS, count=3))
    assert lc_s.main(["--layer", "usda_soils"]) == 3  # 3 counted, 2 received: nothing written
    with db_module.db_readonly(measured["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM constraint_loads WHERE layer_key = 'usda_soils'").fetchone()[0] == 0
    monkeypatch.setattr(lc_s, "sda_query", _sda_stub(SDA_POLYS, SDA_UNITS))
    assert lc_s.main(["--layer", "usda_soils"]) == 0
    assert tag_s.main(["--all"]) == 0
    r = _rows(measured)[("14892864850000", "usda_soils")]
    d = json.loads(r["detail_json"])
    assert d["shallow_water_table"]["share"] == 0.25 and d["hydric"]["share"] == 0.25
    assert [b["name"] for b in d["by"]] == ["Dry sand", "Wet loam"]
    fact = C.describe("usda_soils", r)
    assert "Water table within 18 in" in fact and "(25%)" in fact
    with db_module.db_readonly(measured["db"]) as conn:
        assert "USDA survey version 9/11/2026" in conn.execute(
            "SELECT where_clause FROM constraint_loads WHERE layer_key = 'usda_soils'").fetchone()[0]
        assert C.csv_summary(conn, ["14892864850000"])["hydric_share"] == 0.25
