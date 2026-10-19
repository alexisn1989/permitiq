"""Real-data hardening: the two closed review gaps (cross-row parcel
reconciliation; stale-review status order for conflicting Council items),
plus the runbook, accuracy sampling/tally, quality freshness and the lead
screen's review CSV. Synthetic fixtures only; isolated DB; no network."""

import csv
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_land_use import (FIXTURE as VOTES_FIXTURE, _links, approve, client, db_module, env,  # noqa: E402,F401
                           ingest, ingest_s, link, review_outcomes, statuses)
from test_parcels import (IN_G1, IN_G2, _review, add, dmatch, load_parcels, match,  # noqa: E402
                          vp)


# ---------- 1a. cross-row reconciliation ----------

def _r1_gpin_row_points_at_g2(env):
    with db_module.db(env["db"]) as conn:  # R1: street row says 3853 Shore Dr (G1), printed GPIN row now says G2
        conn.execute("UPDATE land_use_decision_addresses SET gpin10 = '2400000000', raw_text = '2400-00-0000' "
                     "WHERE decision_key = 'vb:M1:R1' AND seq = 2")


@pytest.fixture
def conflicted(env):
    add(env, vp("Y", "7 GPIN ONLY ST", gpin="24000000000000"),   # links to R1 only via the printed GPIN (G2)
        vp("Z", "9 PARCEL ST", latlng=IN_G1))                     # links to R1 only via the street row's parcel (G1)
    assert ingest(env) == 0
    _r1_gpin_row_points_at_g2(env)
    assert load_parcels() == 0 and match(env) == 0 and link(env) == 0
    return env


def test_cross_row_conflict_blocks_parcel_and_gpin_links(conflicted):
    street, gpin_row = dmatch(conflicted, "vb:M1:R1", 1), dmatch(conflicted, "vb:M1:R1", 2)
    assert (street["display_gpin"], gpin_row["display_gpin"]) == ("14892864850000", "24000000000000")
    assert street["dispute_reason"] == gpin_row["dispute_reason"] == "cross_row"
    assert street["link_gpin"] is None and gpin_row["gpin_trusted"] == 0
    links = _links(conflicted)
    assert ("vb:M1:R1", "Y") not in links and ("vb:M1:R1", "Z") not in links
    review = list(csv.DictReader((conflicted["reports"] / "parcel_review.csv").open()))
    sets = [r for r in review if r["subject_kind"] == "decision_set"]
    assert [r["subject_key"] for r in sets] == ["vb:M1:R1"] and "3853 Shore Drive" in sets[0]["detail"]


@pytest.mark.parametrize("verdict, y_linked, z_linked", [
    ("rows_consistent", True, True),
    ("gpin_rows_correct", True, False),
    ("street_rows_correct", False, True),
    ("unresolvable", False, False),
])
def test_set_verdicts(conflicted, verdict, y_linked, z_linked):
    assert _review(conflicted, [{"subject_kind": "decision_set", "subject_key": "vb:M1:R1", "verdict": verdict}]) == 0
    assert link(conflicted) == 0
    links = _links(conflicted)
    assert (("vb:M1:R1", "Y") in links) is y_linked
    assert (("vb:M1:R1", "Z") in links) is z_linked


def test_set_verdict_goes_stale_when_a_row_changes(conflicted):
    assert _review(conflicted, [{"subject_kind": "decision_set", "subject_key": "vb:M1:R1",
                                 "verdict": "rows_consistent"}]) == 0
    assert link(conflicted) == 0 and ("vb:M1:R1", "Z") in _links(conflicted)
    with db_module.db(conflicted["db"]) as conn:  # the street row gets a geocode -> its match facts change
        conn.execute("UPDATE land_use_decision_addresses SET lat = ?, lng = ? "
                     "WHERE decision_key = 'vb:M1:R1' AND seq = 1", IN_G1)
    assert match(conflicted) == 0 and link(conflicted) == 0
    assert dmatch(conflicted, "vb:M1:R1", 1)["set_verdict"] is None
    assert ("vb:M1:R1", "Z") not in _links(conflicted)


def test_set_review_validation(conflicted, env):
    assert _review(conflicted, [{"subject_kind": "decision_set", "subject_key": "vb:M1:R1", "verdict": "gpin_correct"}]) == 1
    assert _review(conflicted, [{"subject_kind": "decision_set", "subject_key": "vb:M2:R6",
                                 "verdict": "rows_consistent"}]) == 1  # no cross-row conflict there


def test_multi_parcel_application_is_not_a_conflict(env):
    assert ingest(env) == 0
    with db_module.db(env["db"]) as conn:  # R1 prints a second GPIN (G2); its street address is still G1
        conn.execute("INSERT INTO land_use_decision_addresses (decision_key, seq, source, raw_text, gpin10) "
                     "VALUES ('vb:M1:R1', 3, 'title', '2400-00-0000', '2400000000')")
    assert load_parcels() == 0 and match(env) == 0
    for seq in (1, 2, 3):
        r = dmatch(env, "vb:M1:R1", seq)
        assert r["cross_row_conflict"] == 0 and r["link_gpin"] is not None


# ---------- 1b. stale review before quarantine ----------

def _reingest_title_variant(env, new_title):
    data = [json.loads(x) for x in VOTES_FIXTURE.read_text().splitlines()]
    for r in data[1:]:
        if r["resolution_id"] == "R4" and r["member_name"] == "Member C":
            r["title"] = new_title
    path = env["tmp"] / "changed_variants.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in data) + "\n")
    return ingest_s.main(["--file", str(path), "--config", str(env["cfg"]), "--reports-dir", str(env["reports"])])


def test_conflicting_item_with_stale_review_reads_stale_not_quarantined(env):
    assert ingest(env) == 0
    assert review_outcomes(env, [{"decision_key": "vb:M1:R4", "verified_outcome": "approved",
                                  "conflict_resolution": "minutes show 500",
                                  "resolved_title": "Application of GAMMA LLC at 500 Pacific Avenue"}]) == 0
    assert statuses(env)["vb:M1:R4"] != "quarantined"
    assert _reingest_title_variant(env, "Application of GAMMA LLC for a Change of Zoning at 504 Pacific Avenue") == 0
    with db_module.db_readonly(env["db"]) as conn:
        s = conn.execute("SELECT status, has_conflicts FROM land_use_decision_status "
                         "WHERE decision_key = 'vb:M1:R4'").fetchone()
    assert (s["status"], s["has_conflicts"]) == ("outcome_review_stale", 1)
    review = {r["decision_key"]: r for r in csv.DictReader((env["reports"] / "land_use_review.csv").open())}
    assert review["vb:M1:R4"]["previous_verified_outcome"] == "approved"
    assert "504 Pacific Avenue" in review["vb:M1:R4"]["conflicts"]


def test_unreviewed_conflict_still_quarantined(env):
    assert ingest(env) == 0
    assert statuses(env)["vb:M1:R4"] == "quarantined"


# ---------- migration ----------

def test_v5_to_v6_keeps_data_and_statuses(conflicted):
    before_links, before_status = _links(conflicted), statuses(conflicted)
    with db_module.db(conflicted["db"]) as conn:  # rebuild the v5 shape
        for v in ("land_use_decision_status", "decision_parcel_effective"):
            conn.execute(f"DROP VIEW {v}")
        for t in ("decision_parcel_set_review", "source_dates", "quality_tallies"):
            conn.execute(f"DROP TABLE {t}")
        conn.execute("ALTER TABLE decision_parcel DROP COLUMN cross_row")
        conn.execute("ALTER TABLE decision_parcel DROP COLUMN set_fp")
        v5_dpe = next(x for x in db_module._SCHEMA_V5_STATEMENTS if "VIEW IF NOT EXISTS decision_parcel_effective" in x)
        conn.execute(v5_dpe)
        conn.execute(db_module._SCHEMA_V4_STATEMENTS[2])  # v4/v5 status view
        conn.execute("PRAGMA user_version=5")
    db_module.init_db(conflicted["db"])
    assert db_module.schema_version(conflicted["db"]) == db_module.SCHEMA_VERSION
    assert _links(conflicted) == before_links and statuses(conflicted) == before_status
    # until match_parcels reconciles, migrated decision rows are not current, so nothing links through them
    assert dmatch(conflicted, "vb:M1:R1", 1)["parcel_state"] == "untagged"
    assert match(conflicted) == 0 and dmatch(conflicted, "vb:M1:R1", 1)["dispute_reason"] == "cross_row"
    with db_module.db_readonly(conflicted["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_parcel").fetchone()[0] > 0


# ---------- 2. runbook (synthetic rehearsal) ----------

import importlib.util  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

from ingest import accuracy, quality  # noqa: E402
from ingest.common import NormalizedPermit, upsert_permits  # noqa: E402
from test_land_use import CONFIG  # noqa: E402
import app as app_module  # noqa: E402

NFHL = ROOT / "tests" / "fixtures" / "nfhl_sample.geojson"
PARCELS = ROOT / "tests" / "fixtures" / "vb_parcels_sample.geojson"


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runbook = _script("run_vb_pipeline")
sample_s = _script("accuracy_sample")
tally_s = _script("accuracy_tally")
review_lu = _script("review_land_use")


def _rp(pid_, street, applied, gpin="", latlng=(None, None)):
    lat, lng = latlng
    return NormalizedPermit(locality="virginia_beach", source_permit_id=pid_, source_name="City of Virginia Beach",
                            address=street, lat=lat, lng=lng, coordinate_source="native" if lat else None,
                            geocode_status="native" if lat else "failed", permit_type="Building", status="Issued",
                            status_bucket="issued", applied_date=applied, issued_date=applied,
                            raw={"StreetAddress": street, "GPIN": gpin, "PermitType": "Building", "WorkType": "New",
                                 "Zip": "23451"})


def _run(args, data_dir, cfg, reviews=None):
    return runbook.main(["--phase", "full", "--council-export", str(VOTES_FIXTURE), "--nfhl-file", str(NFHL),
                         "--parcels-file", str(PARCELS), "--parcels-count", "11", "--land-use-config", str(cfg),
                         "--no-geocode", "--seed", "7", "--out-root", str(args), "--synthetic",
                         *(["--reviews-dir", str(reviews)] if reviews else [])])


@pytest.fixture(scope="module")
def base_run(tmp_path_factory):
    """One synthetic full run (subprocess steps), reused by copying its data dir."""
    root = tmp_path_factory.mktemp("base_run")
    data = root / "data"
    data.mkdir()
    db_path = data / "permits.db"
    db_module.init_db(db_path)
    with db_module.db(db_path) as conn:
        upsert_permits(conn, [_rp("P1", "3853 SHORE DR", "2025-03-01", gpin="14892864850000"),
                              _rp("P3", "1206 LASKIN RD", "2025-02-01"),
                              _rp("Z", "9 PARCEL ST", "2025-03-01", latlng=IN_G1),
                              _rp("M", "2 OTHER ST", "2025-03-01", latlng=(36.7005, -76.1995)),
                              _rp("D", "100 STRADDLE ST", "2025-03-01", latlng=IN_G2)])
        rid = conn.execute("INSERT INTO ingestion_runs (locality, started_at, completed_at, status, fetched_count, "
                           "since_date) VALUES ('virginia_beach', ?, ?, 'success', 5, '2024-06-01')",
                           (db_module.now_iso(), db_module.now_iso())).lastrowid
    cfg = root / "land_use.toml"
    cfg.write_text(CONFIG.format(coverage=f'backfill_from = "2024-06-01"\nbackfill_run_id = {rid}'))
    mp = pytest.MonkeyPatch()
    mp.setenv("DATA_DIR", str(data))
    mp.setattr(db_module, "DB_PATH", db_path)
    try:
        assert _run(root / "reports", data, cfg) == 0
    finally:
        mp.undo()
    return root


@pytest.fixture
def run_env(base_run, tmp_path, monkeypatch):
    shutil.copytree(base_run, tmp_path / "r")
    root = tmp_path / "r"
    monkeypatch.setenv("DATA_DIR", str(root / "data"))
    monkeypatch.setattr(db_module, "DB_PATH", root / "data" / "permits.db")
    monkeypatch.setattr(app_module, "LAND_USE_CONFIG_PATH", root / "land_use.toml")
    run_dir = next((root / "reports").glob("run_*_full"))
    return {"root": root, "db": root / "data" / "permits.db", "cfg": root / "land_use.toml", "run": run_dir}


def test_runbook_order_manifest_and_same_run_parcel_links(run_env):
    m = json.loads((run_env["run"] / "run_manifest.json").read_text())
    order = m["order_executed"]
    assert order.index("council_ingest") < order.index("council_geocode") < order.index("match_parcels") \
        < order.index("parcel_flood") < order.index("link") < order.index("land_use_report")
    st = {s["name"]: s for s in m["steps"]}
    assert st["council_geocode"]["status"] == "skipped" and st["flood_report"]["status"] == "skipped"
    assert "cohort not configured" in st["flood_report"]["reason"]
    assert all(s["status"] in ("ok", "skipped") for s in m["steps"])
    assert m["synthetic"] is True and m["source_dates"]["sources"]["parcels"]["as_of"]
    with db_module.db_readonly(run_env["db"]) as conn:  # R6 was ingested in this run and still got a parcel + link
        r6 = conn.execute("SELECT display_gpin FROM decision_parcel_effective WHERE decision_key = 'vb:M2:R6'").fetchone()
        tier = conn.execute("SELECT k.match_tier FROM decision_permit_links k JOIN permits p ON p.id = k.permit_id "
                            "WHERE k.decision_key = 'vb:M2:R6' AND p.source_permit_id = 'M'").fetchone()
    assert r6[0] == "24400000000000" and tier[0] == "same_parcel"
    for f in ("unmatched_reasons.csv", "accuracy_sample.csv", "accuracy_sample.meta.json", "quality_vb.md"):
        assert (run_env["run"] / f).is_file()
    assert "SYNTHETIC" in (run_env["run"] / "quality_vb.md").read_text()


def test_runbook_imports_reviews_then_relinks(run_env):
    reviews = run_env["root"] / "reviews"
    reviews.mkdir()
    rows = list(csv.DictReader((run_env["run"] / "link_review.csv").open()))
    target = next(r for r in rows if r["kind"] == "candidate_link")
    with (reviews / "link_review.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerow({**target, "verdict": "rejected", "reviewer": "tester"})
    assert _run(run_env["root"] / "reports2", run_env["root"] / "data", run_env["cfg"], reviews) == 0
    m = json.loads(next((run_env["root"] / "reports2").glob("run_*/run_manifest.json")).read_text())
    order = m["order_executed"]
    assert order.index("import_link_reviews") < order.index("relink_after_reviews") < order.index("land_use_report")
    with db_module.db_readonly(run_env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_permit_links WHERE decision_key = ? AND permit_id = ?",
                            (target["decision_key"], int(target["permit_id"]))).fetchone()[0] == 0


def test_unmatched_reasons_cover_every_unmatched_entity(run_env):
    rows = list(csv.DictReader((run_env["run"] / "unmatched_reasons.csv").open()))
    reasons = {(r["entity"], r["key"], r["reason"]) for r in rows}
    assert ("permit", "P1", "not_geocoded:failed") in reasons
    assert ("decision", "vb:M1:R4", "status_quarantined") in reasons
    assert ("decision", "vb:M2:R9", "title_truncated") in reasons
    counts = list(csv.DictReader((run_env["run"] / "unmatched_reasons_counts.csv").open()))
    assert sum(int(c["count"]) for c in counts) == len(rows)


# ---------- 3. frozen samples, tally, freshness ----------

def _approve_lead(env_):
    path = env_["root"] / "outcomes.csv"
    path.write_text("decision_key,verified_outcome,evidence,reviewer\nvb:M2:R9,approved,https://example.invalid/m,t\n")
    assert review_lu.main(["--outcomes", str(path), "--config", str(env_["cfg"])]) == 0


def _draw(env_, out, seed=11):
    assert sample_s.main(["--seed", str(seed), "--out", str(out), "--synthetic"]) == 0
    return out / "accuracy_sample.csv"


def _fill(sheet, fn):
    rows = list(csv.DictReader(sheet.open()))
    fields = list(rows[0].keys())
    for r in rows:
        fn(r)
    with sheet.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return rows


def test_sample_is_deterministic_and_counts_population(run_env):
    _approve_lead(run_env)
    a = _draw(run_env, run_env["root"] / "s1")
    b = _draw(run_env, run_env["root"] / "s2")
    assert a.read_text() == b.read_text()
    meta = json.loads((a.parent / "accuracy_sample.meta.json").read_text())
    assert meta["strata"]["lead"]["population"] == 1 and meta["strata"]["link_same_parcel"]["population"] >= 1
    assert meta["snapshot"]["parcel_load_id"] == 1 and meta["sample_hash"]


def test_edited_sheet_refused(run_env):
    sheet = _draw(run_env, run_env["root"] / "s")
    _fill(sheet, lambda r: r.update(permit_address=r["permit_address"] + " X") if r["stratum"] == "flood" else None)
    assert tally_s.main([str(sheet), "--no-import"]) == 1


def test_lead_without_independent_search_refused(run_env):
    _approve_lead(run_env)
    sheet = _draw(run_env, run_env["root"] / "s")
    _fill(sheet, lambda r: r.update(lead_check="no_permit_found", checker="t") if r["stratum"] == "lead" else None)
    assert tally_s.main([str(sheet), "--no-import"]) == 1


def test_wrong_judgment_column_refused(run_env):
    sheet = _draw(run_env, run_env["root"] / "s")
    _fill(sheet, lambda r: r.update(same_project="yes", checker="t") if r["stratum"] == "flood" else None)
    assert tally_s.main([str(sheet), "--no-import"]) == 1


def test_only_explicit_same_project_rows_become_link_reviews(run_env):
    _approve_lead(run_env)
    sheet = _draw(run_env, run_env["root"] / "s")

    def judge(r):
        if r["stratum"].startswith("link_"):
            r.update(same_project="cant_tell" if r["stratum"] == "link_address_range" else "yes", checker="t")
        elif r["stratum"] == "lead":
            r.update(lead_check="no_permit_found", search_done="city portal by address + GPIN; minutes", checker="t")
        else:
            r.update(flood_check="parcel_class_right", checker="t")
    rows = _fill(sheet, judge)
    stale_key = next(r for r in rows if r["stratum"] == "link_gpin")
    with db_module.db(run_env["db"]) as conn:  # one sampled link's facts change after sampling
        conn.execute("UPDATE decision_permit_links SET matched_on = 'changed' WHERE decision_key = ? AND permit_id = ?",
                     (stale_key["decision_key"], int(stale_key["permit_id"])))
    assert tally_s.main([str(sheet), "--apply-links", "--config", str(run_env["cfg"])]) == 0
    tally = json.loads((sheet.parent / "accuracy_tally.json").read_text())
    assert f"link_gpin:{stale_key['row_id']}" in tally["stale_rows"]
    assert tally["results"]["links_by_tier"]["gpin"]["stale"] == 1
    assert tally["results"]["links_pooled"]["estimate"] is None  # address_range only cant_tell: no pooled figure
    with db_module.db_readonly(run_env["db"]) as conn:
        reviewed = {(r[0], r[1]) for r in conn.execute("SELECT decision_key, permit_id FROM decision_link_review")}
    expected = {(r["decision_key"], int(r["permit_id"])) for r in rows
                if r["stratum"] == "link_same_parcel" and r["same_project"] == "yes"}
    assert reviewed == expected  # no stale, cant_tell, lead or flood row became a link review


def test_whole_sample_stale_after_new_parcel_load(run_env):
    sheet = _draw(run_env, run_env["root"] / "s")
    data = json.loads(PARCELS.read_text())
    data["features"][0]["properties"]["ZONING"] = "B-4"
    changed = run_env["root"] / "parcels2.geojson"
    changed.write_text(json.dumps(data))
    load = _script("load_parcels")
    assert load.main(["--file", str(changed), "--expected-count", "11"]) == 0
    assert tally_s.main([str(sheet), "--no-import"]) == 2


def test_wilson_reference_values():
    assert accuracy.wilson(8, 10) == (0.4902, 0.9433)
    assert accuracy.wilson(0, 1) == (0.0, 0.7935)
    assert accuracy.wilson(0, 0) is None


def test_quality_accuracy_freshness(run_env):
    assert client.get("/api/quality").json()["accuracy"]["freshness"] == "none"
    sheet = _draw(run_env, run_env["root"] / "s")
    _fill(sheet, lambda r: r.update(same_project="yes", checker="t") if r["stratum"].startswith("link_") else None)
    assert tally_s.main([str(sheet)]) == 0
    q = client.get("/api/quality").json()
    assert q["accuracy"]["freshness"] == "fresh" and q["accuracy"]["results"]["links_by_tier"]
    # an idempotent relink (link_run_at changes, content doesn't) keeps it fresh
    assert _script("link_decisions_permits").main(["--config", str(run_env["cfg"]),
                                                   "--reports-dir", str(run_env["root"] / "x")]) == 0
    assert client.get("/api/quality").json()["accuracy"]["freshness"] == "fresh"
    # a new review is a real change: figures withheld
    _approve_lead(run_env)
    a = client.get("/api/quality").json()["accuracy"]
    assert a["freshness"] == "stale" and "results" not in a and "state.outcome_reviews" in a["changed_since_tally"]


def test_data_as_of_route(run_env):
    d = client.get("/api/data-as-of").json()
    assert set(d["sources"]) >= {"permits", "council_export", "fema", "parcels"}
    assert d["permit_window"]["backfill_from"] == "2024-06-01"


# ---------- 4. lead screen ----------

def test_lead_detail_api(run_env):
    _approve_lead(run_env)
    d = client.get("/api/land-use/decisions/vb:M2:R6").json()
    assert d["meeting_id"] == "M2" and d["data_as_of"]["sources"]
    assert d["parcel_rows"][0]["display_gpin"] == "24400000000000"
    link = next(l for l in d["links"] if l["source_permit_id"] == "M")
    assert link["match_tier"] == "same_parcel" and link["matched_on"].startswith("parcel ")
    assert "parcel_flood_class" in link and "review_verdict" in link
    assert "raw_json" not in json.dumps(d)
    r9 = client.get("/api/land-use/decisions/vb:M2:R9").json()
    assert r9["review"]["valid"] == 1 and r9["review"]["evidence"].startswith("https://")


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node not installed")
def test_lead_escaping_under_node():
    js = ("const u=require(process.argv[1]);"
          "process.stdout.write(JSON.stringify([u.esc('<b>\"x\"</b>'),"
          "u.escLinkify('<img src=x onerror=alert(1)> https://e.com/a?b=1&c=2 javascript:alert(1)')]))")
    out = json.loads(subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js")],
                                    capture_output=True, text=True, check=True).stdout)
    assert out[0] == "&lt;b&gt;&quot;x&quot;&lt;/b&gt;"
    assert "<img" not in out[1] and "&lt;img" in out[1]
    assert '<a href="https://e.com/a?b=1&amp;c=2"' in out[1] and 'href="javascript' not in out[1]


@pytest.mark.skipif(node is None, reason="node not installed")
def test_lead_review_csv_is_accepted_by_importer(run_env):
    with db_module.db_readonly(run_env["db"]) as conn:
        pid_m = conn.execute("SELECT id FROM permits WHERE source_permit_id = 'M'").fetchone()[0]
    queue = [{"decision_key": "vb:M2:R6", "permit_id": pid_m, "verdict": "rejected", "reviewer": "Doe, J.",
              "note": 'different project: "pool"\nsee minutes'}]
    js = "const u=require(process.argv[1]);process.stdout.write(u.buildReviewCsv(JSON.parse(process.argv[2])))"
    out = subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js"), json.dumps(queue)],
                         capture_output=True, text=True, check=True).stdout
    path = run_env["root"] / "queue.csv"
    path.write_text(out, newline="")
    assert review_lu.main(["--links", str(path), "--config", str(run_env["cfg"])]) == 0
    with db_module.db_readonly(run_env["db"]) as conn:
        r = conn.execute("SELECT verdict, reviewer, note FROM decision_link_review").fetchone()
    assert tuple(r) == ("rejected", "Doe, J.", 'different project: "pool"\nsee minutes')
