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


def _run(args, data_dir, cfg, reviews=None, stage="provisional", phase="full"):
    return runbook.main(["--phase", phase, "--stage", stage, "--council-export", str(VOTES_FIXTURE),
                         "--nfhl-file", str(NFHL),
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
        conn.execute("INSERT INTO permit_backfill_checks (backfill_run_id, where_clause, source_count, source_ids, "
                     "db_ids, truly_missing, dup_permit_number, no_permit_number, extra_in_db, dup_objectids_db, "
                     "null_date_source, per_year_json, verified, checked_at) "
                     "VALUES (?, 'test', 0, 0, 0, 0, 0, 0, 0, 0, 0, '[]', 1, 'now')", (rid,))
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
    run_dir = next((root / "reports").glob("run_*_full_provisional"))
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
    assert m["stage"] == "provisional" and st["accuracy_sample"]["status"] == "skipped"
    assert "provisional" in st["accuracy_sample"]["reason"]
    assert len(m["db"]["sha256"]) == 64 and m["db"]["bytes"] > 0
    with db_module.db_readonly(run_env["db"]) as conn:  # R6 was ingested in this run and still got a parcel + link
        r6 = conn.execute("SELECT display_gpin FROM decision_parcel_effective WHERE decision_key = 'vb:M2:R6'").fetchone()
        tier = conn.execute("SELECT k.match_tier FROM decision_permit_links k JOIN permits p ON p.id = k.permit_id "
                            "WHERE k.decision_key = 'vb:M2:R6' AND p.source_permit_id = 'M'").fetchone()
    assert r6[0] == "24400000000000" and tier[0] == "same_parcel"
    for f in ("unmatched_reasons.csv", "quality_vb.md", "land_use_vb.md", "land_use_leads.csv"):
        assert (run_env["run"] / f).is_file()
    assert not (run_env["run"] / "accuracy_sample.csv").exists()
    q = (run_env["run"] / "quality_vb.md").read_text()
    assert "SYNTHETIC" in q and "PROVISIONAL" in q
    assert "PROVISIONAL" in (run_env["run"] / "land_use_vb.md").read_text()
    assert next(csv.reader((run_env["run"] / "land_use_leads.csv").open()))[0] == "stage"


def test_final_stage_refused_without_reviews_then_draws_sample(run_env):
    assert _run(run_env["root"] / "rf", run_env["root"] / "data", run_env["cfg"], stage="final") == 1
    m = json.loads(next((run_env["root"] / "rf").glob("run_*/run_manifest.json")).read_text())
    assert m["steps"][-1]["name"] == "stage_check" and "no outcome reviews" in m["steps"][-1]["reason"]
    reviews = run_env["root"] / "reviews"
    reviews.mkdir()
    (reviews / "land_use_review.csv").write_text(
        "decision_key,verified_outcome,evidence,reviewer\nvb:M2:R9,approved,https://example.invalid/m,t\n")
    _approve_lead(run_env)  # a review must exist before a final run is allowed to start
    assert _run(run_env["root"] / "rf2", run_env["root"] / "data", run_env["cfg"], reviews, stage="final") == 0
    run = next((run_env["root"] / "rf2").glob("run_*_full_final"))
    meta = json.loads((run / "accuracy_sample.meta.json").read_text())
    assert meta["strata"]["lead"]["population"] == 1  # drawn from the reviewed snapshot
    assert "PROVISIONAL" not in (run / "quality_vb.md").read_text()


def test_report_phase_keeps_sampled_population(run_env):
    _approve_lead(run_env)
    out = run_env["root"] / "s"
    sheet = _draw(run_env, out)
    _fill(sheet, lambda r: r.update(same_project="yes", checker="t") if r["stratum"].startswith("link_") else None)
    meta = json.loads((out / "accuracy_sample.meta.json").read_text())
    assert tally_s.main([str(sheet), "--apply-links", "--config", str(run_env["cfg"])]) == 0
    assert _run(run_env["root"] / "rr", run_env["root"] / "data", run_env["cfg"], stage="final", phase="report") == 0
    run = next((run_env["root"] / "rr").glob("run_*_report_final"))
    m = json.loads((run / "run_manifest.json").read_text())
    assert "council_ingest" not in m["order_executed"]
    assert next(s for s in m["steps"] if s["name"] == "accuracy_sample")["status"] == "skipped"
    q = json.loads((run / "quality_vb.json").read_text())
    tier = q["accuracy"]["results"]["links_by_tier"]["same_parcel"]
    assert tier["population"] == meta["strata"]["link_same_parcel"]["population"]  # frozen N kept
    assert q["accuracy"]["current_population"]["link_same_parcel"] == 0  # confirmed links left the candidate pool
    assert "current candidates" in (run / "quality_vb.md").read_text()


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
    with db_module.db_readonly(run_env["db"]) as conn:  # provenance: the checked sheet, not the derived CSV
        imp = conn.execute("SELECT * FROM review_imports WHERE kind = 'accuracy_apply'").fetchone()
    assert imp["source_file"] == str(sheet) and imp["source_sha256"] == db_module.file_sha256(sheet)
    derived = sheet.with_name("accuracy_link_verdicts.csv")
    assert imp["derived_file"] == str(derived) and imp["derived_sha256"] == db_module.file_sha256(derived)


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


# ---------- first real run: backfill completeness ----------

from ingest import virginia_beach as vb_adapter  # noqa: E402

verify_s = _script("verify_permit_backfill")


def _source(rows):
    """Stub for ingest.virginia_beach.query_json over a list of attribute dicts."""
    import re as _re

    def matches(where, a):
        d = a.get("ApplicationDate")
        if "IS NULL" in where:
            return not d
        m = _re.search(r"ApplicationDate >= '([^']+)'", where)
        if not d or (m and d < m.group(1)):
            return False
        y = _re.search(r"LIKE '(\d{4})/%'", where)
        return not y or d.startswith(y.group(1) + "/")

    def query(params):
        if "objectIds" in params:
            ids = {int(x) for x in params["objectIds"].split(",")}
            return {"features": [{"attributes": a} for a in rows if a["OBJECTID"] in ids]}
        sel = [a for a in rows if matches(params["where"], a)]
        if params.get("returnCountOnly"):
            return {"count": len(sel)}
        if params.get("returnIdsOnly"):
            return {"objectIds": [a["OBJECTID"] for a in sel]}
        raise AssertionError(params)
    return query


def _attrs(oid, number, date_):
    return {"OBJECTID": oid, "PermitNumber": number, "ApplicationDate": date_, "StreetAddress": f"{oid} TEST ST",
            "Status": "Active", "PermitType": "Building", "GPIN": "", "IssueDate": None, "FinalDate": None}


SOURCE_ROWS = [_attrs(1, "A1", "2021/03/01"), _attrs(2, "A2", "2021/06/01"), _attrs(3, "A3", "2022/02/01"),
               _attrs(4, "A2", "2022/05/01"),          # same permit number as OBJECTID 2 (amendment row)
               _attrs(5, None, "2022/07/01"),          # no permit number: the adapter skips these
               _attrs(6, "A6", None),                  # no ApplicationDate: outside every window
               _attrs(7, "A7", "2020/12/31")]          # before the window


@pytest.fixture
def backfill_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bf.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db(db_path)
    monkeypatch.setattr(vb_adapter, "query_json", _source(SOURCE_ROWS))
    # the backfill "skipped a page": OBJECTID 3 never arrived
    with db_module.db(db_path) as conn:
        upsert_permits(conn, [vb_adapter._to_permit(a, a["PermitNumber"]) for a in SOURCE_ROWS if a["OBJECTID"] in (1, 2)])
        conn.execute("INSERT INTO ingestion_runs (locality, started_at, completed_at, status, since_date) "
                     "VALUES ('virginia_beach', 'now', 'now', 'success', '2021-01-01')")
    return db_path


def test_backfill_gap_detected_topped_up_and_verified(backfill_db, capsys):
    assert verify_s.main(["--since", "2021-01-01", "--no-top-up"]) == 3
    out = capsys.readouterr().out
    assert "truly_missing            1" in out and "dup_permit_number        1" in out
    assert "no_permit_number         1" in out and "null_date_source         1" in out
    assert "2022        3           0  <- source > DB" in out
    assert verify_s.main(["--since", "2021-01-01"]) == 0  # top-up fetches OBJECTID 3 by ID
    with db_module.db_readonly(backfill_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM permits WHERE source_permit_id = 'A3'").fetchone()[0] == 1
        rows = conn.execute("SELECT verified, truly_missing, topped_up, dup_permit_number, null_date_source "
                            "FROM permit_backfill_checks ORDER BY check_id").fetchall()
    assert [tuple(r) for r in rows] == [(0, 1, 0, 1, 1), (1, 0, 1, 1, 1)]


def test_linker_refuses_unverified_backfill(env):
    assert ingest(env) == 0
    with db_module.db(env["db"]) as conn:
        conn.execute("UPDATE permit_backfill_checks SET verified = 0")
    assert link(env) == 1
    with db_module.db(env["db"]) as conn:
        conn.execute("DELETE FROM permit_backfill_checks")
    assert link(env) == 1


def test_handoff_extract_has_no_raw_json_and_records_hash(run_env):
    out = run_env["root"] / "handoff"
    assert _script("handoff_extract").main(["--run", str(run_env["run"]), "--out", str(out)]) == 0
    inv = json.loads((out / "db_inventory.json").read_text())
    import hashlib
    assert inv["sha256"] == hashlib.sha256(run_env["db"].read_bytes()).hexdigest()
    assert inv["tables"]["permits"]["rows"] == 5 and "raw_json" in inv["tables"]["permits"]["columns"]
    for f in (out / "extract").glob("*.csv"):
        assert "raw_json" not in f.read_text().splitlines()[0]
    assert not list(out.rglob("*.db")) and (out / "run" / "run_manifest.json").is_file()


def test_profile_phase_order_with_batch_geocode(tmp_path, monkeypatch):
    calls = []

    def fake_step(self, name, script, args, skip_if=(), gap_codes=frozenset()):
        calls.append((name, script, [str(a) for a in args]))
        self.record(name, [script], 0, "t", "t", "ok", None, [], None)
        return "ok"

    monkeypatch.setattr(runbook.Runner, "step", fake_step)
    monkeypatch.setattr(runbook, "_db_state", lambda fn: 42)
    monkeypatch.setattr(runbook, "_db_hash", lambda: {"sha256": "x" * 64, "bytes": 1})
    assert runbook.main(["--phase", "profile", "--since", "2021-01-01", "--batch-geocode",
                         "--council-export", str(VOTES_FIXTURE), "--out-root", str(tmp_path)]) == 0
    names = [c[0] for c in calls]
    assert names == ["migrate", "permit_ingest", "verify_backfill", "permit_geocode_batch", "profile_permit_fields",
                     "profile_council_export", "parcel_layer_inspect"]
    ingest_args = dict(zip(names, [c[2] for c in calls]))
    assert "--no-geocode" in ingest_args["permit_ingest"] and "2021-01-01" in ingest_args["permit_ingest"]
    assert ingest_args["verify_backfill"] == ["--since", "2021-01-01", "--run-id", "42"]
    m = json.loads(next(tmp_path.glob("run_*/run_manifest.json")).read_text())
    assert m["backfill_run_id"] == 42 and m["db"]["sha256"] == "x" * 64


# ---------- run record, acceptance checklist, actions-layer profile ----------

def test_quality_report_has_run_record_and_checklist(run_env):
    md = (run_env["run"] / "quality_vb.md").read_text()
    assert "## Locality acceptance checklist" in md and "## Run record" in md and "## Source rows" in md
    q = json.loads((run_env["run"] / "quality_vb.json").read_text())
    skipped = {s["name"] for s in q["run_record"]["not_ok"] if s["status"] == "skipped"}
    assert {"council_geocode", "accuracy_sample"} <= skipped
    items = {c["item"]: c for c in q["checklist"]}
    assert items["Permit backfill verified"]["passed"] is True
    assert items["Official cross-check (Clerk / Planning actions)"]["passed"] is None
    assert items["Every link tier sampled"]["passed"] is None  # provisional: no tally, never a pass
    assert items["No failed or gap steps"]["passed"] is True and "accuracy_sample" in items["No failed or gap steps"]["detail"]
    assert q["source_rows"]["permits"] == 5 and q["source_rows"]["parcel_features_received"] == 11 \
        and q["source_rows"]["parcels_active"] == 10  # the fixture's feature without a GPIN is dropped


def _q(**over):
    tier = {"population": 50, "adjudicated": 30, "cant_tell": 0}
    q = {"backfill": {"verified": 1, "source_count": 9, "db_ids": 9, "truly_missing": 0, "null_date_source": 0},
         "refresh_age": {s: {"as_of": "2026-10-01"} for s in quality.CHECKLIST_SOURCES},
         "unresolved": {"outcome_unverified": 0, "outcome_review_stale": 0, "quarantined": 0},
         "official_crosscheck": {"passed": True, "detail": "x"},
         "planning_commission": {"coverage": {"verified": True, "months_without_agenda": [], "agenda_found": 2,
                                              "months": 2, "minutes_found": 2, "from": "2026-08", "to": "2026-09"},
                                 "items": 3, "items_with_warnings": 0},
         "accuracy": {"freshness": "fresh", "results": {
             "links_by_tier": {"gpin_exact": dict(tier), "address": {"population": 4, "adjudicated": 4}},
             "leads": {"population": 10, "adjudicated": 10, "cant_tell": 1},
             "flood_disagreements": {"population": 3, "sampled": 3, "unchecked": 0, "stale": 0}}}}
    q.update(over)
    return q


def test_checklist_passes_and_fails_from_numbers():
    rec = {"steps_total": 3, "not_ok": []}
    assert all(c["passed"] is True for c in quality.acceptance_checklist(_q(), rec))
    bad = _q(unresolved={"outcome_unverified": 2, "outcome_review_stale": 0, "quarantined": 0},
             refresh_age={"permits": {"as_of": "2026-10-01"}})
    bad["accuracy"]["results"]["links_by_tier"]["gpin_exact"]["adjudicated"] = 12
    rec_bad = {"steps_total": 3, "not_ok": [{"name": "verify_backfill", "status": "gap", "reason": "r"}]}
    items = {c["item"]: c for c in quality.acceptance_checklist(bad, rec_bad)}
    assert items["Outcome reviews complete and fresh"]["passed"] is False
    assert items["Source dates recorded"]["passed"] is False and "fema" in items["Source dates recorded"]["detail"]
    assert items["Every link tier sampled"]["passed"] is False and "gpin_exact 12/30" in items["Every link tier sampled"]["detail"]
    assert items["No failed or gap steps"]["passed"] is False
    assert quality.acceptance_checklist(_q(), None)[-1]["passed"] is None


inspect_pc = _script("inspect_pc_actions_layer")


def test_inspect_pc_actions_layer_profile_is_read_only(tmp_path, monkeypatch):
    ms = lambda d: int(__import__("datetime").datetime.fromisoformat(d + "T05:00:00+00:00").timestamp() * 1000)  # noqa: E731
    calls = []

    def fake(url, params):
        calls.append((url, params))
        if not url.endswith("/query"):
            return {"name": "PC and Council Actions", "description": "d", "geometryType": "esriGeometryPolygon",
                    "objectIdField": "OBJECTID", "maxRecordCount": 1000,
                    "fields": [{"name": "CASE_ID", "type": "esriFieldTypeString", "alias": "Case"},
                               {"name": "APPROVAL_DATE", "type": "esriFieldTypeDate", "alias": "Approval Date"}]}
        if params.get("returnCountOnly"):
            return {"count": 3}
        if "groupByFieldsForStatistics" in params:
            return {"features": [{"attributes": {"APPROVAL_DATE": ms("2024-03-05"), "n": 2}},
                                 {"attributes": {"APPROVAL_DATE": ms("2024-02-14"), "n": 1}}]}
        if "outStatistics" in params:
            return {"features": [{"attributes": {"MN": ms("2024-02-14"), "MX": ms("2024-03-05"), "N": 3}}]}
        return {"features": [{"attributes": {"CASE_ID": "C1", "APPROVAL_DATE": ms("2024-03-05")}}]}

    monkeypatch.setattr(inspect_pc, "fetch_json", fake)
    monkeypatch.setattr(inspect_pc, "council_meeting_dates", lambda: {"2024-03-05"})
    out = tmp_path / "p" / "pc_actions_profile.json"
    assert inspect_pc.main(["--out", str(out), "--compare-council"]) == 0
    prof = json.loads(out.read_text())
    assert prof["count"] == 3 and prof["date_fields"]["APPROVAL_DATE"] == {"min": "2024-02-14", "max": "2024-03-05",
                                                                           "non_null": 3}
    assert prof["sample"][0]["APPROVAL_DATE"] == "2024-03-05"
    ev = prof["council_date_evidence"]["by_field"]["APPROVAL_DATE"]
    assert ev["values"] == 3 and ev["on_council_meeting_date"] == 2 and ev["share"] == 0.6667
    assert prof["date_meaning"].startswith("undetermined")  # never asserted by the script
    assert all(p.get("f") == "json" for _, p in calls)
    assert sorted(x.name for x in tmp_path.rglob("*") if x.is_file()) == ["pc_actions_profile.json"]


# ---------- real-run fixes ----------

def test_old_dfirm_id_is_corrected_on_migration(tmp_path):
    p = tmp_path / "old.db"
    db_module.init_db(p)
    with db_module.db(p) as conn:
        conn.execute("UPDATE flood_locality_dfirm SET dfirm_id = '51810C' WHERE locality = 'virginia_beach'")
    db_module.init_db(p)
    with db_module.db_readonly(p) as conn:
        assert conn.execute("SELECT dfirm_id FROM flood_locality_dfirm WHERE locality = 'virginia_beach'"
                            ).fetchone()[0] == "515531"


def test_objectid_lookups_stay_under_url_limit(monkeypatch):
    from ingest import backfill_check
    calls = []

    def fake(params):
        ids = params["objectIds"].split(",")
        calls.append(len(ids))
        return {"features": [{"attributes": {"OBJECTID": int(i), "PermitNumber": f"P{i}"}} for i in ids]}
    monkeypatch.setattr(vb_adapter, "query_json", fake)
    got = backfill_check.source_permit_numbers(list(range(1, 451)))
    assert len(got) == 450 and calls and max(calls) <= vb_adapter.ID_BATCH == 200
    calls.clear()
    list(vb_adapter.fetch_by_ids(list(range(1, 451))))
    assert max(calls) <= 200 and sum(calls) == 450
