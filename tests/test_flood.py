"""Stage 1 flood exposure: classification, the freshness view, the NFHL
loader and display cells, the tagger, the shared stats function, the report
and the flood API. Synthetic NFHL fixture (tests/fixtures/nfhl_sample.geojson),
zero network calls, isolated temp DB per test."""

import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db as db_module  # noqa: E402

db_module.DB_PATH = Path(tempfile.mkdtemp()) / "_collection_time_placeholder.db"

import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from ingest import flood, flood_stats  # noqa: E402
from ingest.common import NormalizedPermit, upsert_permits  # noqa: E402

client = TestClient(app_module.app)
FIXTURE = ROOT / "tests" / "fixtures" / "nfhl_sample.geojson"


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


loader = _script("load_flood_zones")
tagger = _script("tag_flood_zones")
report = _script("flood_report")

# (id, lat, lng, expected class) against the fixture polygons
POINTS = {
    "V_OVERLAP": (36.805, -75.997, "sfha_v"),     # inside VE and AE -> VE wins
    "A_ONLY": (36.805, -76.003, "sfha_a"),
    "A_EDGE": (36.805, -76.005, "sfha_a"),        # exactly on the AE boundary
    "SHADED": (36.805, -76.015, "shaded_x"),
    "MINIMAL": (36.805, -76.035, "matched_non_sfha"),
    "BOWTIE": (36.805, -76.058, "sfha_a"),        # inside the repaired bowtie
    "NOWHERE": (36.900, -76.100, "no_polygon"),
    "NOGEO": (None, None, "not_geocoded"),
}


def vb_permit(pid, lat, lng, issued="2024-05-01", bucket="issued", raw=None):
    return NormalizedPermit(
        locality="virginia_beach", source_permit_id=pid, source_name="City of Virginia Beach",
        address=f"{pid} Test Ave", lat=lat, lng=lng, coordinate_source="native" if lat is not None else None,
        geocode_status="native" if lat is not None else "failed", permit_type="Building", status="Issued",
        status_bucket=bucket, issued_date=issued,
        raw=raw if raw is not None else {"PermitType": "Building", "WorkType": "New", "ConstructionType": "Residential",
                                         "Zip": "23451"})


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db(db_path)
    return db_path


@pytest.fixture
def vb_db(isolated_db):
    permits = [vb_permit(k, lat, lng) for k, (lat, lng, _) in POINTS.items()]
    permits.append(NormalizedPermit(locality="norfolk", source_permit_id="N1", source_name="Norfolk",
                                    lat=36.85, lng=-76.28, coordinate_source="census", geocode_status="matched"))
    with db_module.db(isolated_db) as conn:
        upsert_permits(conn, permits)
    return isolated_db


def classes(db_path):
    with db_module.db_readonly(db_path) as conn:
        return {r["source_permit_id"]: dict(r) for r in conn.execute(
            "SELECT p.source_permit_id, e.effective_class, e.fresh, e.fld_zone, e.sfha, e.load_id "
            "FROM permits p JOIN permit_flood_effective e ON e.permit_id = p.id")}


def load(file=FIXTURE, *extra):
    return loader.main(["--file", str(file), *extra])


def write_fixture(tmp_path, mutate):
    data = json.loads(FIXTURE.read_text())
    mutate(data)
    path = tmp_path / "nfhl_variant.geojson"
    path.write_text(json.dumps(data))
    return path


# ---------- classification ----------

def test_classify_zone_values():
    assert flood.classify_zone("VE", None) == "sfha_v"
    assert flood.classify_zone("AO", None) == "sfha_a"
    assert flood.classify_zone("X", "0.2 PCT ANNUAL CHANCE FLOOD HAZARD") == "shaded_x"
    assert flood.classify_zone("X", "AREA OF MINIMAL FLOOD HAZARD") == "matched_non_sfha"
    assert flood.classify_zone("AREA NOT INCLUDED", None) == "matched_non_sfha"
    assert flood.classify_zone("D", None) == "matched_non_sfha"


# ---------- freshness view ----------

def test_untagged_before_any_load_with_null_zone_fields(vb_db):
    c = classes(vb_db)
    assert c["N1"]["effective_class"] == "out_of_scope"
    for k in POINTS:
        assert c[k]["effective_class"] == "untagged"
        assert c[k]["fresh"] == 0 and c[k]["fld_zone"] is None and c[k]["sfha"] is None


def test_api_after_migrate_before_tagging_is_all_untagged(vb_db):
    rows = client.get("/api/permits?locality=virginia_beach&limit=100").json()["permits"]
    assert rows and all(r["flood_class"] == "untagged" for r in rows)


# ---------- loader + tagger end to end ----------

def test_load_and_tag_classifies_every_point(vb_db):
    assert load() == 0
    assert tagger.main([]) == 0
    c = classes(vb_db)
    for k, (_, _, expected) in POINTS.items():
        assert c[k]["effective_class"] == expected, k
        assert c[k]["fresh"] == 1
    assert c["V_OVERLAP"]["fld_zone"] == "VE" and c["V_OVERLAP"]["sfha"] == 1
    assert c["NOWHERE"]["fld_zone"] is None and c["NOWHERE"]["sfha"] is None
    assert c["N1"]["effective_class"] == "out_of_scope"


def test_loader_records_zones_skips_lines_and_logs_run(vb_db):
    assert load() == 0
    with db_module.db_readonly(vb_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM flood_zones").fetchone()[0] == 5  # line feature skipped
        l = conn.execute("SELECT * FROM flood_zone_loads").fetchone()
        assert l["is_active"] == 1 and l["display_ok"] == 1 and l["source_vintage"] == "1.1.1.0"
        run = conn.execute("SELECT * FROM ingestion_runs WHERE locality = 'fema_nfhl'").fetchone()
        assert run["status"] == "success" and run["inserted_count"] == 5


def test_dry_run_writes_nothing(vb_db):
    assert load(FIXTURE, "--dry-run") == 0
    assert tagger.main(["--dry-run"]) == 1  # no active load yet -> refuses
    with db_module.db_readonly(vb_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM flood_zone_loads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM permit_flood").fetchone()[0] == 0


def test_identical_content_is_not_a_new_revision(vb_db):
    assert load() == 0 and tagger.main([]) == 0
    assert load() == 0
    with db_module.db_readonly(vb_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM flood_zone_loads").fetchone()[0] == 1
    assert all(r["fresh"] == 1 for k, r in classes(vb_db).items() if k != "N1")


def test_same_vintage_different_hash_retags(vb_db, tmp_path):
    assert load() == 0 and tagger.main([]) == 0

    def widen_ve(data):  # same VERSION_ID, VE now also covers the A_ONLY point
        data["features"][0]["geometry"]["coordinates"][0] = [
            [-76.004, 36.80], [-75.99, 36.80], [-75.99, 36.81], [-76.004, 36.81], [-76.004, 36.80]]

    assert load(write_fixture(tmp_path, widen_ve)) == 0
    c = classes(vb_db)
    assert all(r["effective_class"] == "untagged" and r["fld_zone"] is None for k, r in c.items() if k != "N1")
    with db_module.db_readonly(vb_db) as conn:
        loads = conn.execute("SELECT source_vintage, is_active FROM flood_zone_loads ORDER BY load_id").fetchall()
    assert [tuple(r) for r in loads] == [("1.1.1.0", 0), ("1.1.1.0", 1)]
    assert tagger.main([]) == 0
    assert classes(vb_db)["A_ONLY"]["effective_class"] == "sfha_v"


def test_coordinate_change_makes_tag_stale_then_retags(vb_db):
    assert load() == 0 and tagger.main([]) == 0
    with db_module.db(vb_db) as conn:
        conn.execute("UPDATE permits SET lat = 36.805, lng = -76.015 WHERE source_permit_id = 'NOWHERE'")
    c = classes(vb_db)["NOWHERE"]
    assert c["effective_class"] == "untagged" and c["fresh"] == 0
    assert tagger.main([]) == 0
    assert classes(vb_db)["NOWHERE"]["effective_class"] == "shaded_x"


def test_no_active_load_hides_old_tags(vb_db):
    assert load() == 0 and tagger.main([]) == 0
    with db_module.db(vb_db) as conn:
        conn.execute("UPDATE flood_zone_loads SET is_active = 0")
    c = classes(vb_db)
    assert all(r["effective_class"] == "untagged" and r["fld_zone"] is None and r["load_id"] is None
               for k, r in c.items() if k != "N1")


def test_zero_features_aborts_and_keeps_previous_load(vb_db, tmp_path):
    assert load() == 0
    empty = write_fixture(tmp_path, lambda d: d.update(features=[]))
    assert load(empty) == 1
    with db_module.db_readonly(vb_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM flood_zone_loads WHERE is_active = 1").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM flood_zone_loads").fetchone()[0] == 1


def test_partial_unique_index_rejects_second_active_load(isolated_db):
    with db_module.db(isolated_db) as conn:
        ins = ("INSERT INTO flood_zone_loads (dfirm_id, source_url, content_hash, feature_count, display_bytes, "
               "max_cell_bytes, simplify_tolerance, display_ok, loaded_at, is_active) "
               "VALUES ('515531', 'x', ?, 1, 1, 1, 0.1, 1, 'now', ?)")
        conn.execute(ins, ("a", 1))
        conn.execute(ins, ("b", 0))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ins, ("c", 1))


def test_service_paging_with_stubbed_fetch(vb_db, monkeypatch):
    feats = json.loads(FIXTURE.read_text())["features"]
    for i, f in enumerate(feats):
        f["id"] = 100 + i
    calls = []

    def fake_fetch(url, params=None, **kw):
        calls.append(params)
        if url.endswith("/28"):
            return {"geometryType": "esriGeometryPolygon", "fields": [{"name": "FLD_ZONE"}],
                    "maxRecordCount": 4, "objectIdField": "OBJECTID"}
        if params.get("returnCountOnly"):
            return {"count": len(feats)}
        if params.get("returnIdsOnly"):
            return {"objectIds": [f["id"] for f in feats]}
        ids = [int(x) for x in params["objectIds"].split(",")]
        if len(ids) > 1 and 102 in ids:  # like FEMA: a batch holding a large polygon fails, the feature alone works
            raise RuntimeError("500 Server Error")
        return {"type": "FeatureCollection", "features": [f for f in feats if f["id"] in ids]}

    monkeypatch.setattr(loader, "fetch_json", fake_fetch)
    assert loader.main(["--page-size", "4"]) == 0
    batches = [c["objectIds"] for c in calls if c.get("objectIds")]
    assert batches == ["100,101,102,103", "100,101", "102,103", "102", "103", "104,105"]  # failed batch split
    assert all(c.get("where", "DFIRM_ID = '515531'") == "DFIRM_ID = '515531'" for c in calls[1:])
    with db_module.db_readonly(vb_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM flood_zones").fetchone()[0] == 5


def test_missing_feature_aborts_load(vb_db, monkeypatch):
    feats = json.loads(FIXTURE.read_text())["features"]
    for i, f in enumerate(feats):
        f["id"] = 100 + i

    def fake_fetch(url, params=None, **kw):
        if url.endswith("/28"):
            return {"geometryType": "esriGeometryPolygon", "fields": [{"name": "FLD_ZONE"}], "maxRecordCount": 2}
        if params.get("returnCountOnly"):
            return {"count": len(feats)}
        if params.get("returnIdsOnly"):
            return {"objectIds": [f["id"] for f in feats]}
        ids = [int(x) for x in params["objectIds"].split(",")]
        return {"features": [f for f in feats if f["id"] in ids and f["id"] != 103]}  # one never arrives

    monkeypatch.setattr(loader, "fetch_json", fake_fetch)
    assert loader.main([]) == 1
    with db_module.db_readonly(vb_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM flood_zones").fetchone()[0] == 0


def test_wrong_layer_fails_loudly(vb_db, monkeypatch):
    monkeypatch.setattr(loader, "fetch_json", lambda url, params=None, **kw: {"geometryType": "esriGeometryPoint",
                                                                             "fields": []})
    assert loader.main([]) == 1


# ---------- display cells ----------

def test_display_cells_respect_limit_and_exclude_unshaded_x(vb_db):
    assert load() == 0
    with db_module.db_readonly(vb_db) as conn:
        cells = conn.execute("SELECT geojson, bytes, etag FROM flood_display_cells").fetchall()
    assert cells
    for c in cells:
        assert c["bytes"] == len(c["geojson"]) <= flood.MAX_FLOOD_CELL_BYTES
        assert c["etag"] == flood.etag_of(bytes(c["geojson"]))
        assert {f["properties"]["flood_class"] for f in json.loads(c["geojson"])["features"]} <= set(flood.DISPLAY_CLASSES)


def test_oversized_cells_store_load_inactive_and_old_load_serves(vb_db, tmp_path):
    assert load() == 0
    changed = write_fixture(tmp_path, lambda d: d["features"][0]["properties"].update(ZONE_SUBTY="COASTAL"))
    assert load(changed, "--max-cell-bytes", "50") == 2
    with db_module.db_readonly(vb_db) as conn:
        rows = conn.execute("SELECT load_id, is_active, display_ok FROM flood_zone_loads ORDER BY load_id").fetchall()
    assert [tuple(r) for r in rows] == [(1, 1, 1), (2, 0, 0)]
    assert client.get("/api/flood-zones/index.json").json()["load_id"] == 1


def test_etag_changes_when_only_tolerance_changes():
    feats = json.loads(FIXTURE.read_text())["features"]
    stats = __import__("collections").Counter()
    zones = [flood.zone_from_feature(i, f) for i, f in enumerate(loader.normalize(feats, stats))]
    # a wiggly polygon so simplification actually changes the output
    wiggle = [[-76.02 + i * 0.0001, 36.82 + (0.00003 if i % 2 else 0)] for i in range(100)]
    ring = wiggle + [[-75.99, 36.83], [-76.02, 36.83], wiggle[0]]
    zones.append(flood.Zone(99, "AE", None, "T", flood.polygonal(flood.shape({"type": "Polygon", "coordinates": [ring]}))))
    a = flood.build_display_cells(zones, 0.00001)
    b = flood.build_display_cells(zones, 0.0008)
    key = flood.cell_of(-76.015, 36.825)  # the cell holding the wiggly edge
    assert flood.etag_of(a[key]) != flood.etag_of(b[key])


def test_cell_route_etag_304_empty_and_404(vb_db):
    assert client.get("/api/flood-zones/0/0.geojson").status_code == 404  # no active load
    assert load() == 0
    idx = client.get("/api/flood-zones/index.json").json()
    cell = idx["cells"][0]
    r = client.get(f"/api/flood-zones/{cell['x']}/{cell['y']}.geojson")
    assert r.status_code == 200 and r.headers["etag"] == f'"{cell["etag"]}"'
    assert r.json()["type"] == "FeatureCollection" and r.json()["features"]
    r304 = client.get(f"/api/flood-zones/{cell['x']}/{cell['y']}.geojson", headers={"If-None-Match": r.headers["etag"]})
    assert r304.status_code == 304 and r304.content == b""
    empty = client.get("/api/flood-zones/0/0.geojson")
    assert empty.status_code == 200 and empty.json() == {"features": [], "type": "FeatureCollection"}


def test_cell_route_413_when_stored_cell_over_limit(vb_db, monkeypatch):
    assert load() == 0
    cell = client.get("/api/flood-zones/index.json").json()["cells"][0]
    monkeypatch.setattr(app_module, "MAX_FLOOD_CELL_BYTES", 10)
    assert client.get(f"/api/flood-zones/{cell['x']}/{cell['y']}.geojson").status_code == 413


# ---------- flood_class filter ----------

@pytest.fixture
def tagged_db(vb_db):
    assert load() == 0 and tagger.main([]) == 0
    return vb_db


def _ids(query):
    r = client.get(f"/api/permits?limit=100&{query}")
    assert r.status_code == 200, r.text
    return sorted(p["source_permit_id"] for p in r.json()["permits"])


def test_flood_class_comma_and_repeated_params(tagged_db):
    assert _ids("flood_class=sfha_v,sfha_a") == ["A_EDGE", "A_ONLY", "BOWTIE", "V_OVERLAP"]
    assert _ids("flood_class=sfha_v&flood_class=sfha_a&flood_class=sfha_v") == ["A_EDGE", "A_ONLY", "BOWTIE", "V_OVERLAP"]
    assert _ids("flood_class= no_polygon ,") == ["NOWHERE"]
    assert _ids("flood_class=out_of_scope") == ["N1"]


def test_flood_class_invalid_token_is_422(tagged_db):
    r = client.get("/api/permits?flood_class=sfha_v,outside")
    assert r.status_code == 422 and "outside" in r.json()["detail"]


def test_untagged_filter_matches_never_tagged_and_stale(tagged_db):
    with db_module.db(tagged_db) as conn:
        upsert_permits(conn, [vb_permit("NEW", 36.805, -76.003)])
        conn.execute("UPDATE permits SET lat = 36.9001 WHERE source_permit_id = 'NOWHERE'")
    assert _ids("flood_class=untagged") == ["NEW", "NOWHERE"]


def test_export_csv_has_flood_fields_and_no_raw_json(tagged_db):
    r = client.get("/api/export.csv?flood_class=sfha_v")
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    header = lines[0].split(",")
    assert {"flood_class", "fld_zone", "zone_subty", "sfha"} <= set(header)
    assert "raw_json" not in header and "WorkType" not in r.text
    assert len(lines) == 2 and "sfha_v" in lines[1]


def test_permit_and_geojson_responses_never_include_raw_json(tagged_db):
    p = client.get("/api/permits?limit=1").json()["permits"][0]
    assert "raw_json" not in p and "flood_class" in p
    f = client.get("/api/export.geojson").json()["features"][0]["properties"]
    assert "raw_json" not in f and "flood_class" in f


def test_stats_by_flood_class(tagged_db):
    by = client.get("/api/stats?locality=virginia_beach").json()["by_flood_class"]
    assert by["sfha_a"] == 3 and by["no_polygon"] == 1 and by["not_geocoded"] == 1


# ---------- stats + report ----------

COHORT_TOML = """
[cohort]
name = "test new construction"
include = [{ permit_type = "Building", work_type = "New", construction_type = "*" }]
min_n = 2
"""


@pytest.fixture
def cohort_cfg(tmp_path, monkeypatch):
    path = tmp_path / "flood_report.toml"
    path.write_text(COHORT_TOML)
    monkeypatch.setattr(app_module, "FLOOD_CONFIG_PATH", path)
    return path


def _stats_db(tagged_db):
    with db_module.db(tagged_db) as conn:
        upsert_permits(conn, [
            vb_permit("UNISSUED", 36.805, -75.997, issued=None, bucket="active_pending"),
            vb_permit("VOID", 36.805, -75.997, bucket="expired_void"),
            vb_permit("REPAIR", 36.805, -76.035, raw={"PermitType": "Building", "WorkType": "Repair", "Zip": "23451"}),
            vb_permit("OLD", 36.805, -76.003, issued="2019-03-01"),
        ])
    assert tagger.main([]) == 0
    return tagged_db


def test_denominators_exclude_coverage_classes_and_unissued(tagged_db, cohort_cfg):
    _stats_db(tagged_db)
    cfg = flood_stats.load_cohort_config(cohort_cfg)
    with db_module.db_readonly(tagged_db) as conn:
        s = flood_stats.compute_flood_stats(conn, cfg)
    o = s["cohort"]["overall"]
    # cohort: 8 POINTS + OLD (REPAIR is not in the allowlist), issued & not void
    assert o["total"] == 9 and o["matched"] == 7
    assert o["counts"]["no_polygon"] == 1 and o["counts"]["not_geocoded"] == 1
    assert sum(o["shares_of_matched"][c] for c in flood_stats.MATCHED) == pytest.approx(100, abs=0.2)
    assert o["shares_of_matched"]["sfha_total"] == round(100 * 5 / 7, 1)
    assert s["cohort"]["excluded"] == {"unissued": 1, "void_or_expired": 1}
    assert s["baseline"]["overall"]["total"] == 10  # + REPAIR


def test_small_n_suppression(tagged_db, cohort_cfg):
    _stats_db(tagged_db)
    cfg = flood_stats.load_cohort_config(cohort_cfg)
    with db_module.db_readonly(tagged_db) as conn:
        s = flood_stats.compute_flood_stats(conn, cfg)
    years = {r["key"]: r for r in s["cohort"]["by_year"]}
    assert years["2019"]["matched"] == 1 and years["2019"]["suppressed"] and years["2019"]["shares_of_matched"] is None
    assert not years["2024"]["suppressed"]


def test_context_filters_apply_to_cohort_and_baseline(tagged_db, cohort_cfg):
    _stats_db(tagged_db)
    r = client.get("/api/flood-stats?issued_from=2020-01-01")
    assert r.status_code == 200
    s = r.json()
    assert s["cohort"]["overall"]["total"] == 8 and s["baseline"]["overall"]["total"] == 9
    assert s["context_filters"]["issued_from"] == "2020-01-01"


@pytest.mark.parametrize("param", ["permit_type=Building", "status=Issued", "status_bucket=issued", "q=x",
                                   "flood_class=sfha_v", "locality=norfolk"])
def test_flood_stats_rejects_population_filters(tagged_db, cohort_cfg, param):
    assert client.get(f"/api/flood-stats?{param}").status_code == 422


def test_flood_stats_bad_context_is_422(tagged_db, cohort_cfg):
    assert client.get("/api/flood-stats?zip=2345").status_code == 422
    assert client.get("/api/flood-stats?issued_from=2024/01/01").status_code == 422


def test_flood_stats_unconfigured_cohort(tagged_db, tmp_path, monkeypatch):
    path = tmp_path / "empty.toml"
    path.write_text("[cohort]\ninclude = []\n")
    monkeypatch.setattr(app_module, "FLOOD_CONFIG_PATH", path)
    s = client.get("/api/flood-stats").json()
    assert s["cohort_configured"] is False and s["cohort"] is None
    assert s["baseline"]["overall"]["total"] == 8


def test_shipped_config_is_unconfigured():
    cfg = flood_stats.load_cohort_config(ROOT / "config" / "flood_report.toml")
    assert cfg["include"] == []


def test_report_matches_api_numbers(tagged_db, cohort_cfg, tmp_path):
    _stats_db(tagged_db)
    out = tmp_path / "reports"
    assert report.main(["--config", str(cohort_cfg), "--out", str(out), "--issued-from", "2020-01-01"]) == 0
    api = client.get("/api/flood-stats?issued_from=2020-01-01").json()
    rows = (out / "flood_exposure_vb.csv").read_text().splitlines()
    overall = next(r.split(",") for r in rows if r.startswith("cohort,overall,"))
    assert int(overall[3]) == api["cohort"]["overall"]["total"] and int(overall[4]) == api["cohort"]["overall"]["matched"]
    md = (out / "flood_exposure_vb.md").read_text()
    assert "Caveats" in md and "no_polygon" in md and "issued_from=2020-01-01" in md


def test_report_refusals(vb_db, cohort_cfg, tmp_path):
    empty = tmp_path / "empty.toml"
    empty.write_text("[cohort]\ninclude = []\n")
    out = tmp_path / "r"
    assert report.main(["--config", str(empty), "--out", str(out)]) == 1      # unconfigured
    assert report.main(["--config", str(cohort_cfg), "--out", str(out)]) == 1  # no active load
    assert load() == 0
    assert report.main(["--config", str(cohort_cfg), "--out", str(out)]) == 1  # untagged permits
    assert report.main(["--config", str(cohort_cfg), "--out", str(out), "--allow-untagged"]) == 0
    assert not (tmp_path / "unused").exists()


def test_tagger_counts_sum_to_vb_total(tagged_db):
    with db_module.db_readonly(tagged_db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM permits WHERE locality='virginia_beach'").fetchone()[0]
        counts = tagger.class_counts(conn, "virginia_beach")
    assert sum(counts.values()) == total and "untagged" not in counts


def test_config_rejects_unknown_keys(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('[cohort]\ninclude = [{ permit_typ = "Building" }]\n')
    with pytest.raises(flood_stats.ConfigError):
        flood_stats.load_cohort_config(bad)


def test_no_polygon_is_never_counted_as_non_sfha(tagged_db, cohort_cfg):
    cfg = flood_stats.load_cohort_config(cohort_cfg)
    with db_module.db_readonly(tagged_db) as conn:
        o = flood_stats.compute_flood_stats(conn, cfg)["cohort"]["overall"]
    assert o["counts"]["matched_non_sfha"] == 1  # MINIMAL only, not NOWHERE
    assert o["counts"]["no_polygon"] == 1


