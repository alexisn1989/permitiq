"""Stage 3 parcel enrichment. Synthetic fixtures only
(tests/fixtures/vb_parcels_sample.geojson, labeled synthetic; NFHL and
Council-vote fixtures from Stages 1-2), isolated temp DB, zero network.

Fixture parcels (OBJECTID: GPIN, address -> intent):
  1  14892864850000  3853 SHORE DR    inside AE                  -> sfha_a
  2  24000000000000  100 STRADDLE ST  25% shaded X, 75% unmapped -> shaded_x (point there: no_polygon)
  3  24100000000000  200 SLIVER ST    <1% VE, rest unmapped      -> screening_sliver
  4,5 1500000000000x 300 CONDO CT     same box, shared 10-digit  -> ambiguous by GPIN10/point/address
  6  24200000000000  400 BOW ST       self-intersecting          -> repaired, no_polygon
  7,8 16000000000000 500 MULTI ST     two rows, one GPIN         -> unioned
  9  24300000000000  600 PARTIAL RD   50% minimal X, 50% none    -> screening_partial_coverage
  10 24400000000000  77 LAKE DR       (matches decision vb:M2:R6 by address)
  11 24500000000000  10 OLD RD        (matches decision vb:M0:R8 by address)
"""

import csv
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_land_use import (CONFIG, FIXTURE as VOTES_FIXTURE, _links, approve, client, db_module,  # noqa: E402,F401
                           env, ingest, link, pid, statuses)
import app as app_module  # noqa: E402
from ingest import parcels as P  # noqa: E402
from ingest.common import NormalizedPermit, upsert_permits  # noqa: E402
from shapely.geometry import box  # noqa: E402

PARCELS = ROOT / "tests" / "fixtures" / "vb_parcels_sample.geojson"
NFHL = ROOT / "tests" / "fixtures" / "nfhl_sample.geojson"
IN_G1, IN_G2, IN_CONDO, IN_G10 = (36.805, -76.003), (36.805, -76.0095), (36.805, -76.035), (36.7005, -76.1995)


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


load_s = _script("load_parcels")
match_s = _script("match_parcels")
review_s = _script("review_parcels")
tagpf_s = _script("tag_parcel_flood")
report_s = _script("parcel_report")
flood_load_s = _script("load_flood_zones")


def vp(pid_, street, applied="2025-03-01", gpin="", latlng=(None, None), locality="virginia_beach"):
    lat, lng = latlng
    return NormalizedPermit(
        locality=locality, source_permit_id=pid_, source_name="City of Virginia Beach", address=street,
        lat=lat, lng=lng, coordinate_source="native" if lat is not None else None,
        geocode_status="native" if lat is not None else "failed", permit_type="Building", status="Issued",
        status_bucket="issued", applied_date=applied, issued_date=applied,
        raw={"StreetAddress": street, "GPIN": gpin, "PermitType": "Building", "WorkType": "New", "Zip": "23451"})


def add(env, *permits):
    with db_module.db(env["db"]) as conn:
        upsert_permits(conn, list(permits))


def load_parcels(*extra, file=PARCELS, count=11):
    return load_s.main(["--file", str(file), "--expected-count", str(count), *extra])


def match(env, *extra):
    return match_s.main(["--reports-dir", str(env["reports"]), *extra])


def pmatch(env, source_id):
    with db_module.db_readonly(env["db"]) as conn:
        r = conn.execute("SELECT e.* FROM permit_parcel_effective e JOIN permits p ON p.id = e.permit_id "
                         "WHERE p.source_permit_id = ?", (source_id,)).fetchone()
    return dict(r)


def dmatch(env, key, seq=1):
    with db_module.db_readonly(env["db"]) as conn:
        return dict(conn.execute("SELECT * FROM decision_parcel_effective WHERE decision_key = ? AND seq = ?",
                                 (key, seq)).fetchone())


def variant(tmp, mutate) -> Path:
    data = json.loads(PARCELS.read_text())
    mutate(data)
    path = tmp / "parcels_variant.geojson"
    path.write_text(json.dumps(data))
    return path


def loads(env):
    with db_module.db_readonly(env["db"]) as conn:
        return [tuple(r) for r in conn.execute("SELECT load_id, is_active FROM parcel_loads ORDER BY load_id")]


# ---------- loader: --file ----------

def test_file_load_unions_multipart_and_repairs_bowtie(env):
    assert load_parcels() == 0
    with db_module.db_readonly(env["db"]) as conn:
        rows = {r["gpin"]: dict(r) for r in conn.execute("SELECT * FROM parcels")}
        l = dict(conn.execute("SELECT * FROM parcel_loads").fetchone())
    assert len(rows) == 10 and rows["16000000000000"]["source_rows"] == 2
    assert (l["expected_count"], l["received_count"], l["unique_source_ids"], l["is_active"]) == (11, 11, 11, 1)
    assert rows["14892864850000"]["gpin10"] == "1489286485" and rows["14892864850000"]["street_norm"] == "SHORE DR"
    assert rows["24200000000000"]["geom_geojson"]  # bowtie kept after make_valid


def test_file_requires_expected_count_and_checks_it(env):
    assert load_s.main(["--file", str(PARCELS)]) == 1
    assert load_parcels(count=12) == 2
    dup = variant(env["tmp"], lambda d: d["features"].append(d["features"][0]))
    assert load_parcels(file=dup, count=12) == 2   # 12 features but OBJECTID 1 twice
    assert loads(env) == []


def test_missing_field_refused(env):
    path = variant(env["tmp"], lambda d: [f["properties"].pop("FEMA_ZONE_BLDG") for f in d["features"]])
    assert load_parcels(file=path) == 1
    assert loads(env) == []


def test_identical_reload_and_new_revision(env):
    assert load_parcels() == 0 and load_parcels() == 0
    assert loads(env) == [(1, 1)]
    changed = variant(env["tmp"], lambda d: d["features"][0]["properties"].update(ZONING="B-4"))
    assert load_parcels(file=changed) == 0
    assert loads(env) == [(1, 0), (2, 1)]


def test_dry_run_writes_nothing(env):
    assert load_parcels("--dry-run") == 0
    assert loads(env) == []


def test_partial_unique_index_one_active_load(env):
    assert load_parcels() == 0
    with db_module.db(env["db"]) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO parcel_loads (source_url, content_hash, feature_count, expected_count, "
                     "received_count, unique_source_ids, loaded_at, is_active) VALUES ('x','y',1,1,1,1,'now',1)")


# ---------- loader: service completeness ----------

N_SERVICE = 5000


def _service(mode: str):
    feats = {i: {"type": "Feature", "id": i, "geometry": box(-76 + i * 1e-5, 36.9, -76 + i * 1e-5 + 5e-6, 36.90001).__geo_interface__,
                 "properties": {"OBJECTID": i, "PAR_GPIN": f"{30000000000000 + i}", "PROP_ADDRESS": f"{i} SVC ST",
                                "ZONING": "R", "LAND_USE": "R", "PROP_CLASS": "R", "TAX_AREA": 100,
                                "FEMA_ZONE_BLDG": "X"}} for i in range(1, N_SERVICE + 1)}
    state = {"counts": 0, "batches": []}

    def fetch(url, params=None, **kw):
        params = params or {}
        if not url.endswith("/query"):
            return {"geometryType": "esriGeometryPolygon", "objectIdField": "OBJECTID", "maxRecordCount": 2000,
                    "fields": [{"name": n} for n in P.PARCEL_FIELD_MAP.values()] + [{"name": "OBJECTID"}],
                    "editingInfo": {"lastEditDate": 1700000000000}}
        if params.get("returnCountOnly"):
            state["counts"] += 1
            return {"count": N_SERVICE + (1 if mode == "count_changes" and state["counts"] > 1 else 0)}
        if params.get("returnIdsOnly"):
            return {"objectIdFieldName": "OBJECTID", "objectIds": list(feats)}
        ids = [int(x) for x in params["objectIds"].split(",")]
        n = len(state["batches"])
        state["batches"].append(ids)
        if mode == "fail_batch" and n == 1:
            raise RuntimeError("HTTP 500 after 3 tries")
        if mode == "missing_page" and n == 1:
            return {"features": []}
        if mode == "duplicate_page" and n == 1:
            ids = state["batches"][0]
        out = [feats[i] for i in ids]
        if mode == "extra_id" and n == 0:
            out = out + [{**feats[ids[0]], "id": 999999}]
        return {"type": "FeatureCollection", "features": out}

    return fetch, state


def test_service_complete_load_pages_by_object_ids(env, monkeypatch):
    fetch, state = _service("ok")
    monkeypatch.setattr(load_s, "fetch_json", fetch)
    assert load_s.main([]) == 0
    # 200 IDs per request: 2,000 made the GET URL too long on the live city layers (400)
    assert [len(b) for b in state["batches"]] == [200] * 25 and state["counts"] == 2
    with db_module.db_readonly(env["db"]) as conn:
        l = conn.execute("SELECT * FROM parcel_loads").fetchone()
    assert (l["expected_count"], l["received_count"], l["unique_source_ids"]) == (N_SERVICE,) * 3
    assert l["source_vintage"] == "1700000000000"


@pytest.mark.parametrize("mode, code", [("missing_page", 2), ("duplicate_page", 2), ("extra_id", 2),
                                        ("count_changes", 2), ("fail_batch", 1)])
def test_service_incomplete_never_activates(env, monkeypatch, mode, code):
    assert load_parcels() == 0  # a previous active load
    fetch, _ = _service(mode)
    monkeypatch.setattr(load_s, "fetch_json", fetch)
    assert load_s.main([]) == code
    assert loads(env) == [(1, 1)]  # nothing new; the old load keeps serving


def test_inspect_writes_nothing(env, monkeypatch, capsys):
    fetch, _ = _service("ok")
    monkeypatch.setattr(load_s, "fetch_json", lambda url, params=None, **kw:
                        {"features": [{"attributes": {"PAR_GPIN": "1"}}]} if params and params.get("resultRecordCount")
                        else fetch(url, params))
    assert load_s.main(["--inspect"]) == 0
    assert "returnCountOnly: 5000" in capsys.readouterr().out
    assert loads(env) == []


# ---------- matching ----------

@pytest.fixture
def matched(env):
    add(env,
        vp("A", "3853 SHORE DR", gpin="14892864850000", latlng=IN_G1),       # gpin14, agrees
        vp("B", "3853 SHORE DR", gpin="1489-28-6485"),                       # gpin10_unique, no point
        vp("C", "300 CONDO CT", gpin="1500000000", latlng=IN_CONDO),          # condo: ambiguous
        vp("D", "100 STRADDLE ST", latlng=IN_G2),                             # point_in_parcel
        vp("E", "9 ELSEWHERE ST", gpin="24000000000000", latlng=IN_G1),       # disputed: GPIN G2, point G1
        vp("F", "NO COORDS ST"),                                              # not_geocoded_no_gpin
        vp("G", "1 NOWHERE ST", latlng=(36.95, -76.5)),                       # no_parcel
        vp("N", "NORFOLK", locality="norfolk", latlng=IN_G1))                 # out_of_scope
    assert load_parcels() == 0 and match(env) == 0
    return env


def test_permit_match_methods(matched):
    m = {k: pmatch(matched, k) for k in "ABCDEFGN"}
    assert (m["A"]["method"], m["A"]["display_gpin"], m["A"]["disputed"]) == ("gpin14", "14892864850000", 0)
    assert (m["B"]["method"], m["B"]["display_gpin"]) == ("gpin10_unique", "14892864850000")
    assert m["C"]["method"] == "ambiguous" and m["C"]["link_gpin"] is None
    assert (m["D"]["method"], m["D"]["display_gpin"]) == ("point_in_parcel", "24000000000000")
    assert (m["E"]["disputed"], m["E"]["display_gpin"], m["E"]["alt_gpin"], m["E"]["link_gpin"]) == \
        (1, "24000000000000", "14892864850000", None)
    assert m["F"]["method"] == "not_geocoded_no_gpin" and m["G"]["method"] == "no_parcel"
    assert m["N"]["parcel_state"] == "out_of_scope"
    review = list(csv.DictReader((matched["reports"] / "parcel_review.csv").open()))
    assert [r["subject_key"] for r in review if r["subject_kind"] == "permit"] == [str(pid_of(matched, "E"))]


def pid_of(env, source_id):
    with db_module.db_readonly(env["db"]) as conn:
        return pid(conn, source_id)


def test_match_freshness(matched):
    with db_module.db(matched["db"]) as conn:
        conn.execute("UPDATE permits SET lat = 36.8051 WHERE source_permit_id = 'D'")
        conn.execute("UPDATE permits SET raw_json = json_set(raw_json, '$.GPIN', '24100000000000') "
                     "WHERE source_permit_id = 'A'")
    assert pmatch(matched, "D")["parcel_state"] == "untagged" and pmatch(matched, "D")["display_gpin"] is None
    assert pmatch(matched, "A")["parcel_state"] == "untagged"
    assert match(matched) == 0
    assert pmatch(matched, "A")["display_gpin"] == "24100000000000"
    changed = variant(matched["tmp"], lambda d: d["features"][0]["properties"].update(ZONING="B-4"))
    assert load_parcels(file=changed) == 0
    assert pmatch(matched, "B")["parcel_state"] == "untagged"  # new parcel load -> untagged until re-matched
    assert match(matched) == 0 and pmatch(matched, "B")["parcel_state"] == "gpin10_unique"


def test_match_dry_run_rolls_back(env):
    add(env, vp("D", "100 STRADDLE ST", latlng=IN_G2))
    assert load_parcels() == 0 and match(env, "--dry-run") == 0
    assert pmatch(env, "D")["parcel_state"] == "untagged"


def test_decision_match_methods(env):
    assert ingest(env) == 0 and load_parcels() == 0 and match(env) == 0
    r1 = dmatch(env, "vb:M1:R1")  # seq 1: the street address 3853 Shore Drive -> G1
    assert (r1["method"], r1["display_gpin"], r1["disputed"]) == ("address_exact", "14892864850000", 0)
    gp = dmatch(env, "vb:M1:R1", 2)  # seq 2: the printed GPIN 1489-28-6485 -> the same G1
    assert (gp["method"], gp["display_gpin"], gp["disputed"]) == ("gpin10_unique", "14892864850000", 0)
    r6 = dmatch(env, "vb:M2:R6")
    assert (r6["method"], r6["display_gpin"]) == ("address_exact", "24400000000000")


def test_decision_side_dispute_never_links(env):
    assert ingest(env) == 0
    with db_module.db(env["db"]) as conn:  # R6's geocoded point lands in G2, its address says G10
        conn.execute("UPDATE land_use_decision_addresses SET lat = ?, lng = ? WHERE decision_key = 'vb:M2:R6'", IN_G2)
    add(env, vp("M", "2 OTHER ST", latlng=IN_G10))
    assert load_parcels() == 0 and match(env) == 0
    r6 = dmatch(env, "vb:M2:R6")
    assert (r6["disputed"], r6["display_gpin"], r6["alt_gpin"], r6["link_gpin"]) == \
        (1, "24400000000000", "24000000000000", None)
    assert link(env) == 0
    assert ("vb:M2:R6", "M") not in _links(env)


# ---------- same_parcel linking + disputes ----------

def test_same_parcel_links_different_address_as_candidate(env):
    add(env, vp("M", "2 OTHER ST", latlng=IN_G10),                          # same parcel as R6, other address
        vp("M_LATE", "3 OTHER ST", applied="2031-01-01", latlng=IN_G10),    # outside the 60-month window
        vp("M_ADDR", "77 LAKE DR", latlng=IN_G10))                           # address_exact outranks same_parcel
    assert ingest(env) == 0 and load_parcels() == 0 and match(env) == 0 and link(env) == 0
    links = _links(env)
    assert links[("vb:M2:R6", "M")][:2] == ("same_parcel", "candidate")
    assert ("vb:M2:R6", "M_LATE") not in links
    assert links[("vb:M2:R6", "M_ADDR")][0] == "address_exact"
    approve(env, "vb:M2:R6")
    assert statuses(env)["vb:M2:R6"] == "candidate_permit"


def _review(env, rows):
    path = env["tmp"] / "parcel_review_filled.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=match_s.REVIEW_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({"reviewer": "tester", **r})
    return review_s.main([str(path)])


def test_disputed_permit_links_only_after_review(env):
    add(env, vp("K", "1 OTHER ST", gpin="24400000000000", latlng=IN_G2),   # GPIN G10, point G2
        vp("H", "4 OTHER ST", gpin="24000000000000", latlng=IN_G10))      # GPIN G2, point G10
    assert ingest(env) == 0 and load_parcels() == 0 and match(env) == 0 and link(env) == 0
    assert ("vb:M2:R6", "K") not in _links(env) and ("vb:M2:R6", "H") not in _links(env)
    k, h = str(pid_of(env, "K")), str(pid_of(env, "H"))
    assert _review(env, [{"subject_kind": "permit", "subject_key": k, "verdict": "gpin_correct"},
                         {"subject_kind": "permit", "subject_key": h, "verdict": "unresolvable"}]) == 0
    assert link(env) == 0
    assert _links(env)[("vb:M2:R6", "K")][0] == "same_parcel" and ("vb:M2:R6", "H") not in _links(env)
    assert _review(env, [{"subject_kind": "permit", "subject_key": h, "verdict": "point_correct"}]) == 0
    assert link(env) == 0
    assert _links(env)[("vb:M2:R6", "H")][0] == "same_parcel"
    assert pmatch(env, "H")["display_gpin"] == "24400000000000"  # the point's parcel
    # the review judged specific facts: move K's point and it no longer applies
    with db_module.db(env["db"]) as conn:
        conn.execute("UPDATE permits SET lng = -76.0094 WHERE source_permit_id = 'K'")
    assert match(env) == 0 and link(env) == 0
    assert pmatch(env, "K")["disputed"] == 1 and pmatch(env, "K")["review_verdict"] is None
    assert ("vb:M2:R6", "K") not in _links(env)


def test_parcel_review_validation(env):
    add(env, vp("D", "100 STRADDLE ST", latlng=IN_G2), vp("K", "1 OTHER ST", gpin="24400000000000", latlng=IN_G2))
    assert load_parcels() == 0 and match(env) == 0
    d = str(pid_of(env, "D"))
    assert _review(env, [{"subject_kind": "permit", "subject_key": d, "verdict": "gpin_correct"}]) == 1  # not disputed
    assert _review(env, [{"subject_kind": "permit", "subject_key": "999", "verdict": "gpin_correct"}]) == 1
    assert _review(env, [{"subject_kind": "permit", "subject_key": str(pid_of(env, "K")), "verdict": "maybe"}]) == 1
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM parcel_match_review").fetchone()[0] == 0


# ---------- parcel flood ----------

@pytest.fixture
def flooded(matched):
    assert flood_load_s.main(["--file", str(NFHL)]) == 0
    assert tagpf_s.main(["--all"]) == 0
    return matched


def _pf(env):
    with db_module.db_readonly(env["db"]) as conn:
        return {r["gpin"]: dict(r) for r in conn.execute("SELECT * FROM parcel_flood_effective")}


def test_parcel_flood_classes_and_shares(flooded):
    pf = _pf(flooded)
    assert pf["14892864850000"]["parcel_flood_class"] == "sfha_a"
    assert pf["24000000000000"]["parcel_flood_class"] == "shaded_x"
    assert pf["24000000000000"]["share_shaded_x"] == pytest.approx(0.25, abs=1e-3)
    sliver = pf["24100000000000"]
    assert sliver["parcel_flood_class"] == "screening_sliver" and 0 < sliver["share_sfha_v"] < 0.01
    assert pf["15000000000001"]["parcel_flood_class"] == "matched_non_sfha"
    assert pf["24300000000000"]["parcel_flood_class"] == "screening_partial_coverage"
    assert pf["24200000000000"]["parcel_flood_class"] == "no_polygon"
    for r in pf.values():
        total = r["share_sfha_v"] + r["share_sfha_a"] + r["share_shaded_x"] + r["share_other_mapped"] + r["share_unmapped"]
        assert total == pytest.approx(1.0, abs=1e-4)


@pytest.mark.parametrize("shares, cls", [
    ({"sfha_v": 0.005, "sfha_a": 0, "shaded_x": 0, "matched_non_sfha": 0.995, "unmapped": 0}, "screening_sliver"),
    ({"sfha_v": 0, "sfha_a": 0, "shaded_x": 0, "matched_non_sfha": 1.0, "unmapped": 0}, "matched_non_sfha"),
    ({"sfha_v": 0, "sfha_a": 0, "shaded_x": 0, "matched_non_sfha": 0.6, "unmapped": 0.4}, "screening_partial_coverage"),
    ({"sfha_v": 0, "sfha_a": 0.3, "shaded_x": 0, "matched_non_sfha": 0.7, "unmapped": 0}, "sfha_a"),
    ({"sfha_v": 0, "sfha_a": 0, "shaded_x": 0, "matched_non_sfha": 0.005, "unmapped": 0.995}, "no_polygon"),
])
def test_parcel_flood_class_rules(shares, cls):
    assert P.parcel_flood_class(shares, 0.01) == cls


def test_parcel_flood_overlap_precedence():
    idx = P.FloodShareIndex([("sfha_a", box(0, 0, 0.6, 1)), ("sfha_v", box(0.4, 0, 1, 1))])
    s = idx.shares(box(0, 0, 1, 1))
    assert (s["sfha_v"], s["sfha_a"], s["unmapped"]) == (pytest.approx(0.6), pytest.approx(0.4), pytest.approx(0))


def test_parcel_flood_freshness(flooded):
    assert _pf(flooded)
    changed = variant(flooded["tmp"], lambda d: d["features"][0]["properties"].update(ZONING="B-4"))
    assert load_parcels(file=changed) == 0
    assert _pf(flooded) == {}  # new parcel load -> no current parcel flood rows


def test_point_vs_parcel_straddle(flooded):
    r = client.get("/api/permits?limit=100").json()["permits"]
    d = next(p for p in r if p["source_permit_id"] == "D")
    assert (d["flood_class"], d["parcel_flood_class"]) == ("untagged", "shaded_x")  # point untagged here
    assert d["parcel_share_shaded_x"] == pytest.approx(0.25, abs=1e-3)
    assert d["zoning"] == "R-7.5" and d["acreage"] == 0.5 and d["parcel_disputed"] == 0
    e = next(p for p in r if p["source_permit_id"] == "E")
    assert e["parcel_disputed"] == 1
    assert "raw_json" not in d


def test_flood_stats_parcel_basis(flooded):
    _script("tag_flood_zones").main([])
    point = client.get("/api/flood-stats").json()
    assert point["basis"] == "point"
    parcel = client.get("/api/flood-stats?basis=parcel").json()
    o = parcel["baseline"]["overall"]
    assert o["counts"]["parcel_disputed"] == 1 and o["counts"]["ambiguous"] == 1
    # A, B and the shared fixture's P1 (G1's GPIN, no parcel under its point) -> G1 sfha_a; D -> G2 shaded_x
    assert o["counts"]["sfha_a"] == 3 and o["counts"]["shaded_x"] == 1
    assert o["matched"] == 4  # screening/no_parcel/ambiguous/disputed never in the denominator
    assert o["counts"]["no_parcel"] >= 1
    assert client.get("/api/flood-stats?basis=footprint").status_code == 422


# ---------- API ----------

def test_parcel_outline_route(matched):
    r = client.get("/api/parcels/14892864850000.geojson")
    assert r.status_code == 200 and r.json()["properties"]["zoning"] == "B-2"
    assert client.get("/api/parcels/14892864850000.geojson",
                      headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    assert client.get("/api/parcels/0.geojson").status_code == 404


def test_decision_parcel_fields(env):
    assert ingest(env) == 0 and load_parcels() == 0 and match(env) == 0
    d = client.get("/api/land-use/decisions/vb:M2:R6").json()["decision"]
    assert (d["parcel_gpin"], d["parcel_method"], d["parcel_disputed"]) == ("24400000000000", "address_exact", 0)


def test_api_503_on_v4_database(env):
    with sqlite3.connect(env["db"]) as conn:
        conn.execute("PRAGMA user_version=4")
    assert client.get("/api/parcels/1.geojson").status_code == 503
    assert client.get("/api/permits").status_code == 503


# ---------- report ----------

def test_report(flooded):
    out = flooded["tmp"] / "out"
    assert report_s.main(["--out", str(out)]) == 0
    md = (out / "parcel_vb.md").read_text()
    assert "screening estimate" in md and "FEMA_ZONE_BLDG" in md
    rows = list(csv.DictReader((out / "parcel_vb.csv").open()))
    city = {(r["a"], r["b"]): int(r["count"]) for r in rows if r["section"] == "city_vs_ours"}
    assert city[("X", "screening_partial_coverage")] == 1 and city[("AE", "sfha_a")] == 1


def test_report_refuses_without_parcel_load(env):
    assert report_s.main(["--out", str(env["tmp"] / "o")]) == 1


# ---------- migration ----------

def test_v4_to_v5_copies_links_with_statuses_unchanged(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M1:R1", "vb:M2:R6")
    before_links, before_status = _links(env), statuses(env)
    assert before_links
    with db_module.db(env["db"]) as conn:  # rebuild the v4 table shape (no same_parcel in the CHECK)
        conn.execute("DROP VIEW land_use_decision_status")
        conn.execute("DROP VIEW decision_permit_links_effective")
        conn.execute(db_module._LINKS_V5_TABLE.replace("decision_permit_links_v5", "dpl_v4")
                     .replace("'same_parcel',", ""))
        conn.execute(f"INSERT INTO dpl_v4 SELECT {db_module._LINK_COLUMNS} FROM decision_permit_links")
        conn.execute("DROP TABLE decision_permit_links")
        conn.execute("ALTER TABLE dpl_v4 RENAME TO decision_permit_links")
        for statement in db_module._SCHEMA_V4_STATEMENTS[1:]:
            conn.execute(statement)
        conn.execute("PRAGMA user_version=4")
    db_module.init_db(env["db"])
    assert _links(env) == before_links and statuses(env) == before_status
    with db_module.db_readonly(env["db"]) as conn:
        assert "same_parcel" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'decision_permit_links'").fetchone()[0]


# ---------- review revisions: GPIN-tier gating, decision review freshness ----------

def test_dispute_gates_gpin_tier(env):
    add(env, vp("Q", "5 ELSE ST", gpin="14892864850000", latlng=IN_G2),   # GPIN says G1, point says G2: disputed
        vp("U", "6 ELSE ST", gpin="14892864850000"))                       # same GPIN, no point: undisputed
    assert ingest(env) == 0 and load_parcels() == 0 and match(env) == 0 and link(env) == 0
    links = _links(env)
    assert ("vb:M1:R1", "Q") not in links                       # its GPIN contradicts its location
    assert links[("vb:M1:R1", "U")][:2] == ("gpin", "candidate")  # undisputed GPIN still links
    q = str(pid_of(env, "Q"))
    assert _review(env, [{"subject_kind": "permit", "subject_key": q, "verdict": "point_correct"}]) == 0
    assert link(env) == 0 and ("vb:M1:R1", "Q") not in _links(env)  # GPIN judged wrong: still blocked
    assert _review(env, [{"subject_kind": "permit", "subject_key": q, "verdict": "gpin_correct"}]) == 0
    assert link(env) == 0 and _links(env)[("vb:M1:R1", "Q")][:2] == ("gpin", "candidate")


def _r6_disputed_and_reviewed(env):
    assert ingest(env) == 0
    with db_module.db(env["db"]) as conn:  # R6: address says G10, geocoded point says G2
        conn.execute("UPDATE land_use_decision_addresses SET lat = ?, lng = ? WHERE decision_key = 'vb:M2:R6'", IN_G2)
    assert load_parcels() == 0 and match(env) == 0
    assert _review(env, [{"subject_kind": "decision", "subject_key": "vb:M2:R6#1", "verdict": "gpin_correct"}]) == 0
    assert dmatch(env, "vb:M2:R6")["review_verdict"] == "gpin_correct"


@pytest.mark.parametrize("change", [
    "UPDATE land_use_decision_addresses SET house_to = 79 WHERE decision_key = 'vb:M2:R6'",
    "UPDATE land_use_decision_addresses SET street_norm = 'LAKE AVE' WHERE decision_key = 'vb:M2:R6'",
    "UPDATE land_use_decision_addresses SET gpin10 = '2440000000' WHERE decision_key = 'vb:M2:R6'",
    "UPDATE land_use_decision_addresses SET lat = lat + 0.00001 WHERE decision_key = 'vb:M2:R6'",
    "new parcel load",
])
def test_decision_dispute_review_goes_stale_on_any_fact_change(env, change):
    _r6_disputed_and_reviewed(env)
    if change == "new parcel load":
        assert load_parcels(file=variant(env["tmp"], lambda d: d["features"][0]["properties"].update(ZONING="X"))) == 0
    else:
        with db_module.db(env["db"]) as conn:
            conn.execute(change)
    r6 = dmatch(env, "vb:M2:R6")
    assert r6["parcel_state"] == "untagged" and r6["review_verdict"] is None and r6["link_gpin"] is None
    assert match(env) == 0
    assert dmatch(env, "vb:M2:R6")["review_verdict"] is None  # re-matched: the old review no longer applies
