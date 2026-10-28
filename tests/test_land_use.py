"""Stage 2: land-use decisions -> permits. Synthetic export fixture
(tests/fixtures/vb_council_votes_sample.jsonl, labeled synthetic), isolated
temp DB per test, zero network calls.

Fixture items (decision_key -> intent):
  vb:M1:R1  rezoning at 3853 Shore Drive + printed GPIN       approved by review
  vb:M1:R2  CUP at 1200-1212 Laskin Road (range)              approved by review
  vb:M1:R3  appropriation                                     excluded (not land use)
  vb:M1:R4  member rows disagree on the address in the title  quarantined
  vb:M2:R5  'Defeated'                                        never approved
  vb:M2:R6  'Motion to approve ...'                           proposed_approved, still unverified
  vb:M2:R7  no house number                                   address_unparsed once approved
  vb:M0:R8  2023, before the verified backfill start          observation_uncertain
  vb:M2:R9  400-char title                                    title_truncated
"""

import csv
import re
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
from ingest import land_use, land_use_link, land_use_stats  # noqa: E402
from ingest.address_norm import gpin10, parse_permit_street, parse_title_addresses  # noqa: E402
from ingest.common import NormalizedPermit, upsert_permits  # noqa: E402

client = TestClient(app_module.app)
FIXTURE = ROOT / "tests" / "fixtures" / "vb_council_votes_sample.jsonl"


def _script(name, folder="scripts"):
    spec = importlib.util.spec_from_file_location(name, ROOT / folder / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ingest_s = _script("ingest_land_use_decisions")
link_s = _script("link_decisions_permits")
review_s = _script("review_land_use")
report_s = _script("land_use_report")
profile_s = _script("profile_land_use_export")
exporter = _script("export_vb_council_votes", "tools")

CONFIG = """
[votes]
vote_map = {{ "Aye" = "yes", "Nay" = "no", "Absent" = "absent" }}
result_map = {{ "Vote Complete" = "complete", "Defeated" = "defeated", "Deferred" = "deferred" }}
[classify]
land_use_patterns = [{{ label = "rezoning", pattern = "(?i)change of zoning" }},
                     {{ label = "conditional_use", pattern = "(?i)conditional use" }}]
exclude_patterns = [{{ pattern = "(?i)appropriat" }}]
motion_patterns = [{{ pattern = "(?i)^motion to approve", proposes = "approved" }}]
[link]
link_window_months = 60
[coverage]
{coverage}
max_staleness_days = 14
[new_construction]
include = [{{ permit_type = "Building", work_type = "New" }}]
min_n = 1
"""


def vb(pid, street, applied, issued=None, gpin="", ptype="Building", wtype="New"):
    return NormalizedPermit(
        locality="virginia_beach", source_permit_id=pid, source_name="City of Virginia Beach",
        address=f"{street}, Virginia Beach, VA 23451", lat=36.85, lng=-76.0, coordinate_source="native",
        geocode_status="native", permit_type=ptype, status="Issued", status_bucket="issued",
        applied_date=applied, issued_date=issued,
        raw={"StreetAddress": street, "GPIN": gpin, "PermitType": ptype, "WorkType": wtype})


PERMITS = [
    vb("P1", "3853 SHORE DR", "2025-03-01", "2025-04-01", gpin="14892864850000"),   # gpin tier for R1
    vb("P2", "3853 SHORE DR", "2025-06-01", ptype="Electrical", wtype="Repair"),      # address_exact for R1
    vb("P3", "1206 LASKIN RD", "2025-02-01"),                                         # address_range for R2
    vb("P4", "1220 LASKIN RD", "2025-02-01"),                                         # nearby only
    vb("P5", "3853 SHORE DR", "2024-12-01"),                                          # before the approval
    vb("P6", "3853 SHORE DR", "2031-01-01"),                                          # outside the 60-month window
    vb("P7", "10 OLD RD", "2024-08-01"),                                              # for the pre-backfill R8
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db(db_path)
    with db_module.db(db_path) as conn:
        upsert_permits(conn, PERMITS)
        run_id = conn.execute(
            "INSERT INTO ingestion_runs (locality, started_at, completed_at, status, fetched_count, since_date) "
            "VALUES ('virginia_beach', ?, ?, 'success', 7, '2024-06-01')",
            (db_module.now_iso(), db_module.now_iso())).lastrowid
        conn.execute("INSERT INTO permit_backfill_checks (backfill_run_id, where_clause, source_count, source_ids, "
                         "db_ids, truly_missing, dup_permit_number, no_permit_number, extra_in_db, dup_objectids_db, "
                         "null_date_source, per_year_json, verified, checked_at) "
                         "VALUES (?, 'test', 0, 0, 0, 0, 0, 0, 0, 0, 0, '[]', 1, 'now')", (run_id,))
    cfg = tmp_path / "land_use.toml"
    cfg.write_text(CONFIG.format(coverage=f'backfill_from = "2024-06-01"\nbackfill_run_id = {run_id}'))
    monkeypatch.setattr(app_module, "LAND_USE_CONFIG_PATH", cfg)
    return {"db": db_path, "cfg": cfg, "reports": tmp_path / "reports", "run_id": run_id, "tmp": tmp_path}


def ingest(env, *extra):
    return ingest_s.main(["--file", str(FIXTURE), "--config", str(env["cfg"]),
                          "--reports-dir", str(env["reports"]), *extra])


def link(env, *extra):
    return link_s.main(["--config", str(env["cfg"]), "--reports-dir", str(env["reports"]), *extra])


def review_outcomes(env, rows):
    path = env["tmp"] / "outcomes.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ingest_s.REVIEW_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({"reviewer": "tester", "evidence": "https://example.invalid/minutes#item", **r})
    return review_s.main(["--outcomes", str(path), "--config", str(env["cfg"])])


def review_links(env, rows):
    path = env["tmp"] / "links.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=link_s.LINK_REVIEW_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({"reviewer": "tester", **r})
    return review_s.main(["--links", str(path), "--config", str(env["cfg"])])


def pid(conn, source_id):
    return conn.execute("SELECT id FROM permits WHERE source_permit_id = ?", (source_id,)).fetchone()[0]


def statuses(env):
    with db_module.db_readonly(env["db"]) as conn:
        return dict(conn.execute("SELECT decision_key, status FROM land_use_decision_status").fetchall())


def approve(env, *keys):
    assert review_outcomes(env, [{"decision_key": k, "verified_outcome": "approved"} for k in keys]) == 0


# ---------- address_norm ----------

def test_parse_title_addresses():
    a = parse_title_addresses("at 1205, 1209 and 1213 Laskin Road and 200-212 North 58th Street (GPINs 1489286485, "
                              "1494-34-4919-0000)")
    streets = [(x.house_from, x.house_to, x.street_norm) for x in a if x.street_norm]
    assert streets == [(1205, 1205, "LASKIN RD"), (1209, 1209, "LASKIN RD"), (1213, 1213, "LASKIN RD"),
                       (200, 212, "N 58TH ST")]
    assert [x.gpin10 for x in a if x.gpin10] == ["1489286485", "1494344919"]


@pytest.mark.parametrize("title", ["Mail to P.O. Box 55 Main Street", "the property on Holland Road",
                                   "Ordinance 2024 Fiscal Year", "Section 21 Road Map", "call 757-385-1234"])
def test_parse_title_rejects_non_addresses(title):
    assert parse_title_addresses(title) == []


def test_permit_street_and_gpin():
    assert parse_permit_street("3853 SHORE DR") == (3853, "SHORE DR")
    assert parse_permit_street("200 N 58TH ST") == (200, "N 58TH ST")
    assert parse_permit_street("") is None
    assert gpin10("14892864850000") == "1489286485" and gpin10("1489-28-6485") == "1489286485"
    assert gpin10("123") is None


# ---------- exporter (runs on the VoteIQ host) ----------

def _voteiq_db(path, columns):
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE vb_council_member_votes ({', '.join(columns)})")
        conn.execute("CREATE TABLE vb_split_votes (id, title, topic)")
        conn.execute(f"INSERT INTO vb_council_member_votes ({', '.join(columns)}) VALUES "
                     f"({', '.join('?' * len(columns))})", [f"v{i}" for i in range(len(columns))])


def test_exporter_reads_only_and_omits_user_id(tmp_path, capsys):
    path = tmp_path / "polls.db"
    _voteiq_db(path, ["id", "user_id", "member_name", "district", "meeting_date", "meeting_id", "resolution_id",
                      "title", "vote", "result"])
    before = path.read_bytes()
    assert exporter.main([str(path)]) == 0
    out, err = capsys.readouterr()
    lines = [json.loads(x) for x in out.splitlines()]
    assert lines[0]["_meta"]["rows"] == 1 and "user_id" not in lines[1]
    assert set(lines[1]) == set(exporter.COLUMNS)
    assert "vb_split_votes: id, title, topic" in err
    assert path.read_bytes() == before


def test_exporter_column_mismatch_exits_2(tmp_path, capsys):
    path = tmp_path / "polls.db"
    _voteiq_db(path, ["id", "member_name", "meeting_date"])
    assert exporter.main([str(path)]) == 2
    assert "actual columns" in capsys.readouterr().err


# ---------- profile / config ----------

def test_profile_runs_read_only(env, capsys):
    assert profile_s.main(["--file", str(FIXTURE), "--config", str(env["cfg"])]) == 0
    out = capsys.readouterr().out
    assert "'Aye'" in out and "'Vote Complete'" in out and "disagree" in out


def test_shipped_config_is_empty_and_ingest_refuses(env):
    shipped = land_use.load_config(ROOT / "config" / "land_use.toml")
    assert not shipped["vote_map"] and not shipped["land_use_patterns"] and shipped["backfill_from"] is None
    assert ingest_s.main(["--file", str(FIXTURE), "--config", str(ROOT / "config" / "land_use.toml")]) == 1


def test_unmapped_vote_value_refuses(env):
    env["cfg"].write_text(env["cfg"].read_text().replace('"Absent" = "absent"', ""))
    assert ingest(env) == 1
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM land_use_decisions").fetchone()[0] == 0


# ---------- ingest (R1, R5) ----------

def test_ingest_aggregates_and_never_approves(env):
    assert ingest(env) == 0
    with db_module.db_readonly(env["db"]) as conn:
        d = {r["decision_key"]: dict(r) for r in conn.execute("SELECT * FROM land_use_decisions")}
    assert "vb:M1:R3" not in d and len(d) == 8
    assert d["vb:M1:R2"]["yes_count"] == 2 and d["vb:M1:R2"]["no_count"] == 1
    assert d["vb:M2:R5"]["meeting_date"] == "2025-02-11"  # MM/DD/YYYY parsed
    assert d["vb:M1:R1"]["outcome_evidence"] == "unverified"  # Vote Complete + all yes is NOT approval
    assert d["vb:M2:R6"]["outcome_evidence"] == "proposed_approved"
    assert d["vb:M2:R9"]["title_truncated"] == 1
    s = statuses(env)
    assert s["vb:M1:R1"] == "outcome_unverified" and s["vb:M2:R6"] == "outcome_unverified"
    assert "no_candidate_permit" not in s.values()


def test_conflicting_member_rows_are_quarantined(env):
    assert ingest(env) == 0
    with db_module.db_readonly(env["db"]) as conn:
        d = conn.execute("SELECT title, outcome_evidence FROM land_use_decisions WHERE decision_key = 'vb:M1:R4'").fetchone()
        c = conn.execute("SELECT field, variants_json FROM land_use_decision_conflicts").fetchall()
        n_addr = conn.execute("SELECT COUNT(*) FROM land_use_decision_addresses WHERE decision_key='vb:M1:R4'").fetchone()[0]
    assert d["title"] is None and d["outcome_evidence"] == "unknown" and n_addr == 0
    assert [(r["field"], json.loads(r["variants_json"])) for r in c] == [("title", {
        "Application of GAMMA LLC for a Change of Zoning at 500 Pacific Avenue": 2,
        "Application of GAMMA LLC for a Change of Zoning at 502 Pacific Avenue": 1})]
    assert statuses(env)["vb:M1:R4"] == "quarantined"
    review_csv = (env["reports"] / "land_use_review.csv").read_text()
    assert "502 Pacific Avenue" in review_csv


def test_ingest_is_idempotent_and_keeps_geocodes(env):
    assert ingest(env) == 0
    with db_module.db(env["db"]) as conn:
        conn.execute("UPDATE land_use_decision_addresses SET lat = 36.9, lng = -76.0, geocode_status = 'matched'")
    assert ingest(env) == 0
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM land_use_decisions").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM land_use_decision_addresses WHERE lat IS NULL").fetchone()[0] == 0


def test_dry_run_writes_nothing(env):
    assert ingest(env, "--dry-run") == 0
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM land_use_decisions").fetchone()[0] == 0


def test_geocode_uses_cache_and_stub(env, monkeypatch):
    assert ingest(env) == 0
    calls = []

    def fake(address, locality=None):
        calls.append(address)
        from ingest.geocode import GeocodeResult
        return GeocodeResult(36.8, -76.0, address, "census", "high", "matched", True, None)

    monkeypatch.setattr(ingest_s, "geocoder", fake)
    assert ingest_s.main(["--geocode", "--config", str(env["cfg"])]) == 0
    assert "3853 SHORE DR, Virginia Beach, VA" in calls
    n = len(calls)
    with db_module.db(env["db"]) as conn:
        conn.execute("UPDATE land_use_decision_addresses SET geocode_status = NULL")
    assert ingest_s.main(["--geocode", "--config", str(env["cfg"])]) == 0
    assert len(calls) == n  # second pass served from geocode_cache


# ---------- review (R1) ----------

def test_review_requires_evidence_and_is_atomic(env):
    assert ingest(env) == 0
    assert review_outcomes(env, [{"decision_key": "vb:M1:R1", "verified_outcome": "approved"},
                                 {"decision_key": "vb:M1:R2", "verified_outcome": "approved", "evidence": ""}]) == 1
    assert statuses(env)["vb:M1:R1"] == "outcome_unverified"  # nothing written


def test_quarantine_needs_resolution_and_resolved_title(env):
    assert ingest(env) == 0
    assert review_outcomes(env, [{"decision_key": "vb:M1:R4", "verified_outcome": "approved"}]) == 1
    assert review_outcomes(env, [{"decision_key": "vb:M1:R4", "verified_outcome": "approved",
                                  "conflict_resolution": "minutes show 500"}]) == 1
    assert review_outcomes(env, [{"decision_key": "vb:M1:R4", "verified_outcome": "approved",
                                  "conflict_resolution": "minutes show 500",
                                  "resolved_title": "Application of GAMMA LLC at 500 Pacific Avenue"}]) == 0
    with db_module.db_readonly(env["db"]) as conn:
        a = conn.execute("SELECT source, house_from, street_norm FROM land_use_decision_addresses "
                         "WHERE decision_key = 'vb:M1:R4'").fetchall()
    assert [tuple(r) for r in a] == [("review", 500, "PACIFIC AVE")]
    assert ingest(env) == 0  # re-ingest keeps the review addresses
    assert statuses(env)["vb:M1:R4"] != "quarantined"


def test_verified_denial_is_not_approved(env):
    assert ingest(env) == 0
    assert review_outcomes(env, [{"decision_key": "vb:M2:R5", "verified_outcome": "denied"}]) == 0
    assert statuses(env)["vb:M2:R5"] == "not_approved"


# ---------- coverage (R2) ----------

def test_linker_refuses_without_named_backfill(env):
    assert ingest(env) == 0
    env["cfg"].write_text(CONFIG.format(coverage=""))
    assert link(env) == 1


@pytest.mark.parametrize("problem", ["missing", "wrong_status", "late_since"])
def test_linker_refuses_unbacked_backfill(env, problem):
    assert ingest(env) == 0
    with db_module.db(env["db"]) as conn:
        if problem == "missing":
            env["cfg"].write_text(CONFIG.format(coverage='backfill_from = "2024-06-01"\nbackfill_run_id = 999'))
        elif problem == "wrong_status":
            conn.execute("UPDATE ingestion_runs SET status = 'failed' WHERE id = ?", (env["run_id"],))
        else:
            conn.execute("UPDATE ingestion_runs SET since_date = '2024-09-01' WHERE id = ?", (env["run_id"],))
    assert link(env) == 1
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM permit_coverage").fetchone()[0] == 0


def test_old_permit_record_does_not_move_coverage(env):
    with db_module.db(env["db"]) as conn:
        upsert_permits(conn, [vb("OLD", "1 ANY ST", "2015-01-01")])
    assert ingest(env) == 0 and link(env) == 0
    with db_module.db_readonly(env["db"]) as conn:
        assert land_use_stats.coverage(conn)["backfill_from"] == "2024-06-01"


def test_pre_backfill_and_stale_refresh_are_uncertain(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M0:R8", "vb:M2:R6")
    s = statuses(env)
    assert s["vb:M0:R8"] == "observation_uncertain"  # its candidate permit P7 doesn't change that
    assert s["vb:M2:R6"] == "no_candidate_permit"
    with db_module.db(env["db"]) as conn:
        conn.execute("UPDATE permit_coverage SET last_refresh_at = '2020-01-01T00:00:00Z'")
    assert statuses(env)["vb:M2:R6"] == "observation_uncertain"
    with db_module.db(env["db"]) as conn:
        conn.execute("UPDATE permit_coverage SET last_refresh_at = NULL")
    assert statuses(env)["vb:M2:R6"] == "observation_uncertain"


def test_unparsed_address_is_never_a_lead(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M2:R7")
    assert statuses(env)["vb:M2:R7"] == "address_unparsed"


# ---------- links (R3) ----------

def _links(env):
    with db_module.db_readonly(env["db"]) as conn:
        return {(r["decision_key"], r["source_permit_id"]): (r["match_tier"], r["link_state"], r["is_new_construction"])
                for r in conn.execute("SELECT k.*, p.source_permit_id FROM decision_permit_links k "
                                      "JOIN permits p ON p.id = k.permit_id")}


def test_link_tiers_window_and_candidates(env):
    assert ingest(env) == 0 and link(env) == 0
    links = _links(env)
    assert links[("vb:M1:R1", "P1")] == ("gpin", "candidate", 1)          # gpin beats address_exact
    assert links[("vb:M1:R1", "P2")] == ("address_exact", "candidate", 0)
    assert links[("vb:M1:R2", "P3")] == ("address_range", "candidate", 1)
    assert ("vb:M1:R1", "P5") not in links and ("vb:M1:R1", "P6") not in links  # before / after window
    assert ("vb:M1:R2", "P4") not in links                                     # nearby: review only
    review = (env["reports"] / "link_review.csv").read_text()
    assert "nearby_not_linked" in review and "1220 LASKIN RD" in review
    approve(env, "vb:M1:R1")
    assert statuses(env)["vb:M1:R1"] == "candidate_permit"  # not a lead, not confirmed


def test_link_reviews_survive_rebuild(env):
    assert ingest(env) == 0 and link(env) == 0
    with db_module.db_readonly(env["db"]) as conn:
        p1, p2, p4 = pid(conn, "P1"), pid(conn, "P2"), pid(conn, "P4")
    assert review_links(env, [{"decision_key": "vb:M1:R1", "permit_id": p1, "verdict": "rejected"},
                              {"decision_key": "vb:M1:R1", "permit_id": p2, "verdict": "confirmed"},
                              {"decision_key": "vb:M1:R2", "permit_id": p4, "verdict": "confirmed"}]) == 0
    for _ in range(2):
        links = _links(env)
        assert ("vb:M1:R1", "P1") not in links
        assert links[("vb:M1:R1", "P2")][:2] == ("address_exact", "confirmed")
        assert links[("vb:M1:R2", "P4")][:2] == ("manual", "confirmed")
        assert link(env) == 0
    approve(env, "vb:M1:R1")
    assert statuses(env)["vb:M1:R1"] == "confirmed_permit"


def test_link_review_rejects_non_vb_permit(env):
    assert ingest(env) == 0
    with db_module.db(env["db"]) as conn:
        upsert_permits(conn, [NormalizedPermit(locality="norfolk", source_permit_id="N1", source_name="Norfolk")])
        n1 = pid(conn, "N1")
    assert review_links(env, [{"decision_key": "vb:M1:R1", "permit_id": n1, "verdict": "confirmed"}]) == 1


def test_link_dry_run_rolls_back(env):
    assert ingest(env) == 0
    assert link(env, "--dry-run") == 0
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_permit_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM permit_coverage").fetchone()[0] == 0


# ---------- time to permit (R4) ----------

def test_timing_one_observation_per_decision_confirmed_vs_candidates(env):
    assert ingest(env) == 0 and link(env) == 0
    with db_module.db_readonly(env["db"]) as conn:
        p2 = pid(conn, "P2")
    assert review_links(env, [{"decision_key": "vb:M1:R1", "permit_id": p2, "verdict": "confirmed"}]) == 0
    approve(env, "vb:M1:R1", "vb:M1:R2", "vb:M2:R6")
    cfg = land_use.load_config(env["cfg"])
    with db_module.db_readonly(env["db"]) as conn:
        t = land_use_stats.compute_land_use_stats(conn, cfg)["timing"]
    conf, cand = t["confirmed_only"], t["including_candidates"]
    # confirmed: only R1 via P2 (2025-01-14 -> 2025-06-01 = 138 days); R2 (candidate only) and R6 are open
    assert conf["t_first_application"]["n"] == 1 and conf["t_first_application"]["median_days"] == 138
    assert conf["t_first_application"]["n_open"] == 2
    assert conf["t_first_issued"]["n"] == 0 and conf["t_first_new_construction_application"]["n"] == 0
    # with candidates: R1 -> P1 (46 days, one value despite two permits), R2 -> P3 (18 days)
    assert cand["t_first_application"]["n"] == 2
    assert (cand["t_first_application"]["p25_days"], cand["t_first_application"]["median_days"]) == (18, 18)
    assert cand["t_first_application"]["p75_days"] == 46
    assert cand["t_first_issued"]["n"] == 1 and cand["t_first_issued"]["median_days"] == 77
    assert cand["t_first_new_construction_application"]["n"] == 2
    assert cand["t_first_application"]["n_open"] == 1  # R6: no permit, counted not imputed


def test_timing_suppression(env):
    env["cfg"].write_text(env["cfg"].read_text().replace("min_n = 1", "min_n = 5"))
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M1:R1")
    cfg = land_use.load_config(env["cfg"])
    with db_module.db_readonly(env["db"]) as conn:
        b = land_use_stats.compute_land_use_stats(conn, cfg)["timing"]["including_candidates"]["t_first_application"]
    assert b["n"] == 1 and b["suppressed"] and b["median_days"] is None


def test_nearest_rank():
    assert land_use_stats.nearest_rank([10, 20, 30, 40], 0.5) == 20
    assert land_use_stats.nearest_rank([10, 20, 30, 40], 0.75) == 30
    assert land_use_stats.nearest_rank([5], 0.25) == 5


# ---------- API + report ----------

@pytest.fixture
def linked(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M1:R1", "vb:M1:R2", "vb:M2:R6")
    return env


def test_api_decisions_filters_and_allowlist(linked):
    r = client.get("/api/land-use/decisions?status=no_candidate_permit")
    assert r.status_code == 200
    body = r.json()
    assert [d["decision_key"] for d in body["decisions"]] == ["vb:M2:R6"]
    assert body["coverage"]["backfill_from"] == "2024-06-01"
    d = body["decisions"][0]
    assert set(d) == set(app_module.DECISION_FIELDS) and "raw_json" not in d
    keys = {x["decision_key"] for x in client.get("/api/land-use/decisions?outcome=unverified").json()["decisions"]}
    assert "vb:M1:R1" not in keys and "vb:M2:R5" in keys
    tier = client.get("/api/land-use/decisions?match_tier=gpin").json()["decisions"]
    assert [x["decision_key"] for x in tier] == ["vb:M1:R1"]


@pytest.mark.parametrize("q", ["status=approved", "outcome=yes", "match_tier=fuzzy", "flood_class=outside",
                               "decided_from=2025/01/01"])
def test_api_invalid_tokens_422(linked, q):
    assert client.get(f"/api/land-use/decisions?{q}").status_code == 422


def test_api_detail_and_404(linked):
    r = client.get("/api/land-use/decisions/vb:M1:R1").json()
    assert {l["source_permit_id"] for l in r["links"]} == {"P1", "P2"}
    assert all(l["link_state"] == "candidate" for l in r["links"])
    assert r["review"]["verified_outcome"] == "approved"
    assert client.get("/api/land-use/decisions/vb:none").status_code == 404


def test_lead_csv_carries_observation_window(linked):
    r = client.get("/api/land-use/export.csv?status=no_candidate_permit")
    assert r.status_code == 200 and r.headers["x-permits-observed-from"] == "2024-06-01"
    rows = list(csv.DictReader(r.text.splitlines()))
    assert [x["decision_key"] for x in rows] == ["vb:M2:R6"]
    assert rows[0]["permits_observed_from"] == "2024-06-01" and rows[0]["permits_observed_to"]


def test_decision_flood_class_from_linked_permits(linked):
    from ingest.flood import FloodIndex  # noqa: F401 - flood tagging covered in test_flood
    with db_module.db(linked["db"]) as conn:
        conn.execute("INSERT INTO flood_zone_loads (dfirm_id, content_hash, feature_count, loaded_at, is_active) "
                     "VALUES ('515531', 'h', 1, 'now', 1)")
        p1 = pid(conn, "P1")
        conn.execute("INSERT INTO permit_flood (permit_id, load_id, tagged_lat, tagged_lng, flood_class, sfha, "
                     "fld_zone, tagged_at) VALUES (?, 1, 36.85, -76.0, 'sfha_a', 1, 'AE', 'now')", (p1,))
    d = client.get("/api/land-use/decisions/vb:M1:R1").json()["decision"]
    assert (d["flood_class"], d["flood_source"]) == ("sfha_a", "candidate_permit")
    keys = [x["decision_key"] for x in client.get("/api/land-use/decisions?flood_class=sfha_a").json()["decisions"]]
    assert keys == ["vb:M1:R1"]


def test_tagger_tags_decision_points(linked):
    fixture = ROOT / "tests" / "fixtures" / "nfhl_sample.geojson"
    assert _script("load_flood_zones").main(["--file", str(fixture)]) == 0
    with db_module.db(linked["db"]) as conn:
        conn.execute("UPDATE land_use_decision_addresses SET lat = 36.805, lng = -75.997 WHERE decision_key = 'vb:M2:R6'")
    assert _script("tag_flood_zones").main(["--decisions"]) == 0
    d = client.get("/api/land-use/decisions/vb:M2:R6").json()["decision"]
    assert (d["flood_class"], d["fld_zone"], d["flood_source"]) == ("sfha_v", "VE", "decision_point")


def test_stats_api_matches_report(linked):
    api = client.get("/api/land-use/stats").json()
    out = linked["tmp"] / "out"
    assert report_s.main(["--config", str(linked["cfg"]), "--out", str(out)]) == 0
    rows = list(csv.DictReader((out / "land_use_vb.csv").open()))
    r = next(x for x in rows if x["links"] == "including_candidates" and x["measure"] == "t_first_application")
    assert int(r["n"]) == api["timing"]["including_candidates"]["t_first_application"]["n"]
    leads = list(csv.DictReader((out / "land_use_leads.csv").open()))
    assert [x["decision_key"] for x in leads] == ["vb:M2:R6"] and leads[0]["permits_observed_from"] == "2024-06-01"
    md = (out / "land_use_vb.md").read_text()
    assert "Caveats" in md and "Confirmed links (headline)" in md


def test_report_refuses_without_coverage(env):
    assert ingest(env) == 0
    assert report_s.main(["--config", str(env["cfg"]), "--out", str(env["tmp"] / "o")]) == 1


def test_land_use_api_503_on_v2_database(env):
    with sqlite3.connect(env["db"]) as conn:
        conn.execute("PRAGMA user_version=2")
    assert client.get("/api/land-use/decisions").status_code == 503
    assert client.get("/api/land-use/stats").status_code == 503


def test_migrate_v2_to_v3_keeps_permits_and_flood_tags(tmp_path):
    path = tmp_path / "v2.db"
    db_module.init_db(path)
    with db_module.db(path) as conn:
        upsert_permits(conn, [vb("P1", "1 A ST", "2025-01-01")])
        conn.execute("INSERT INTO flood_zone_loads (dfirm_id, content_hash, feature_count, loaded_at, is_active) "
                     "VALUES ('515531', 'h', 1, 'now', 1)")
        conn.execute("INSERT INTO permit_flood (permit_id, load_id, tagged_lat, tagged_lng, flood_class, tagged_at) "
                     "VALUES (1, 1, 36.85, -76.0, 'sfha_a', 'now')")
        for v in ("land_use_decision_status", "decision_flood_effective"):
            conn.execute(f"DROP VIEW {v}")
        for t in ("decision_flood", "decision_link_review", "decision_permit_links", "permit_coverage",
                  "decision_outcome_review", "land_use_decision_addresses", "land_use_decision_conflicts",
                  "land_use_decisions"):
            conn.execute(f"DROP TABLE {t}")
        conn.execute("PRAGMA user_version=2")
    db_module.init_db(path)
    with db_module.db_readonly(path) as conn:
        assert conn.execute("SELECT effective_class FROM permit_flood_effective").fetchone()[0] == "sfha_a"
        assert conn.execute("SELECT COUNT(*) FROM land_use_decision_status").fetchone()[0] == 0


# ---------- review fixes: export integrity ----------

def _export_variant(env, mutate) -> Path:
    lines = FIXTURE.read_text().splitlines()
    path = env["tmp"] / "variant.jsonl"
    path.write_text("\n".join(mutate(lines)) + "\n")
    return path


@pytest.mark.parametrize("mutate, message", [
    (lambda ls: ls[:-1], "header says 27 rows, file has 26"),                      # truncated at a line boundary
    (lambda ls: ls + [ls[-1]], "header says 27 rows, file has 28"),                 # appended / altered
    (lambda ls: ls[:-1] + [ls[-1][:40]], "probably truncated"),                     # cut mid-line
    (lambda ls: ls[1:], "header is missing"),                                       # no header
    (lambda ls: [ls[0].replace('"rows": 27', '"rows": "27"')] + ls[1:], "valid integer 'rows'"),
    (lambda ls: ls + [ls[0]], "second _meta"),
])
def test_export_integrity_refused_before_ingest(env, capsys, mutate, message):
    path = _export_variant(env, mutate)
    with pytest.raises(ValueError, match=re.escape(message) if "'" not in message else "rows"):
        land_use.read_export(path)
    for extra in ((), ("--dry-run",)):
        assert ingest_s.main(["--file", str(path), "--config", str(env["cfg"]),
                              "--reports-dir", str(env["reports"]), *extra]) == 1
    assert profile_s.main(["--file", str(path), "--config", str(env["cfg"])]) == 1
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM land_use_decisions").fetchone()[0] == 0


def test_exporter_header_count_matches_rows_written(tmp_path, capsys):
    path = tmp_path / "polls.db"
    _voteiq_db(path, ["id", "user_id", "member_name", "district", "meeting_date", "meeting_id", "resolution_id",
                      "title", "vote", "result"])
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO vb_council_member_votes SELECT * FROM vb_council_member_votes")
    assert exporter.main([str(path)]) == 0
    out_path = tmp_path / "export.jsonl"
    out_path.write_text(capsys.readouterr().out)
    meta, rows = land_use.read_export(out_path)
    assert meta["rows"] == len(rows) == 2


# ---------- review fixes: stale outcome reviews ----------

def _reingest_with(env, key, field, new_value):
    mid, rid = key.split(":")[1:]
    data = [json.loads(x) for x in FIXTURE.read_text().splitlines()]
    for r in data[1:]:
        if r["meeting_id"] == mid and r["resolution_id"] == rid:
            r[field] = new_value
    path = env["tmp"] / "changed.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in data) + "\n")
    return ingest_s.main(["--file", str(path), "--config", str(env["cfg"]), "--reports-dir", str(env["reports"])])


@pytest.mark.parametrize("field, value", [
    ("title", "Application of ALPHA LLC for a Change of Zoning at 3855 Shore Drive"),
    ("result", "Deferred"),
    ("meeting_date", "2025-01-28"),
])
def test_changed_facts_make_review_stale_until_rereviewed(env, field, value):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M1:R1")
    assert statuses(env)["vb:M1:R1"] == "candidate_permit"
    assert _reingest_with(env, "vb:M1:R1", field, value) == 0
    assert statuses(env)["vb:M1:R1"] == "outcome_review_stale"
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT verified_outcome FROM decision_outcome_review "
                            "WHERE decision_key = 'vb:M1:R1'").fetchone()[0] == "approved"  # kept for audit
        assert conn.execute("SELECT verified_outcome FROM land_use_decision_status "
                            "WHERE decision_key = 'vb:M1:R1'").fetchone()[0] is None
    review = {r["decision_key"]: r for r in csv.DictReader((env["reports"] / "land_use_review.csv").open())}
    assert review["vb:M1:R1"]["previous_verified_outcome"] == "approved"
    assert "vb:M1:R1" not in [d["decision_key"] for d in client.get(
        "/api/land-use/decisions?outcome=approved").json()["decisions"]]
    approve(env, "vb:M1:R1")
    assert statuses(env)["vb:M1:R1"] != "outcome_review_stale"


def test_unchanged_reingest_keeps_review_valid(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M1:R1")
    assert ingest(env) == 0
    assert statuses(env)["vb:M1:R1"] == "candidate_permit"


def test_review_without_fingerprint_is_stale(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M2:R6")
    with db_module.db(env["db"]) as conn, db_module.review_import(conn, "outcomes", None, actor="test-fixture"):
        conn.execute("UPDATE decision_outcome_review SET reviewed_fingerprint = NULL")
    assert statuses(env)["vb:M2:R6"] == "outcome_review_stale"


# ---------- review fixes: one window rule ----------

def test_confirmed_link_outside_window_is_refused(env):
    assert ingest(env) == 0 and link(env) == 0
    with db_module.db_readonly(env["db"]) as conn:
        p5, p6, p4 = pid(conn, "P5"), pid(conn, "P6"), pid(conn, "P4")
    assert review_links(env, [{"decision_key": "vb:M1:R1", "permit_id": p5, "verdict": "confirmed"}]) == 1  # before
    assert review_links(env, [{"decision_key": "vb:M1:R1", "permit_id": p6, "verdict": "confirmed"}]) == 1  # after
    assert review_links(env, [{"decision_key": "vb:M1:R1", "permit_id": p5, "verdict": "rejected"}]) == 0   # fine
    assert review_links(env, [{"decision_key": "vb:M1:R2", "permit_id": p4, "verdict": "confirmed"}]) == 0
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_link_review WHERE verdict = 'confirmed'").fetchone()[0] == 1


def test_shrunk_window_drops_manual_link_on_rebuild(env, capsys):
    assert ingest(env) == 0 and link(env) == 0
    with db_module.db_readonly(env["db"]) as conn:
        p4 = pid(conn, "P4")
    assert review_links(env, [{"decision_key": "vb:M1:R2", "permit_id": p4, "verdict": "confirmed"}]) == 0
    approve(env, "vb:M1:R2")
    assert statuses(env)["vb:M1:R2"] == "confirmed_permit"
    # P4 applied 2025-02-01, 18 days after the 2025-01-14 approval; P3 too. A 0-month window excludes both.
    env["cfg"].write_text(env["cfg"].read_text().replace("link_window_months = 60", "link_window_months = 0"))
    capsys.readouterr()
    assert link(env) == 0
    assert "confirmed review outside window -- ignored: vb:M1:R2" in capsys.readouterr().out
    assert ("vb:M1:R2", "P4") not in _links(env)
    assert statuses(env)["vb:M1:R2"] != "confirmed_permit"


def test_out_of_window_link_is_invisible_everywhere(linked):
    with db_module.db(linked["db"]) as conn:
        p5 = pid(conn, "P5")  # applied 2024-12-01, before R6's 2025-02-11 meeting
        conn.execute("INSERT INTO decision_permit_links (decision_key, permit_id, match_tier, matched_on, link_state, "
                     "is_new_construction, link_run_at) VALUES ('vb:M2:R6', ?, 'manual', 'x', 'confirmed', 1, 'now')",
                     (p5,))
        conn.execute("INSERT INTO flood_zone_loads (dfirm_id, content_hash, feature_count, loaded_at, is_active) "
                     "VALUES ('515531', 'h', 1, 'now', 1)")
        conn.execute("INSERT INTO permit_flood (permit_id, load_id, tagged_lat, tagged_lng, flood_class, sfha, "
                     "fld_zone, tagged_at) VALUES (?, 1, 36.85, -76.0, 'sfha_a', 1, 'AE', 'now')", (p5,))
    assert statuses(linked)["vb:M2:R6"] == "no_candidate_permit"
    detail = client.get("/api/land-use/decisions/vb:M2:R6").json()
    assert detail["links"] == []
    d = detail["decision"]
    assert (d["n_confirmed_links"], d["n_candidate_links"]) == (0, 0)
    assert d["flood_class"] is None and d["flood_source"] is None
    tier = [x["decision_key"] for x in client.get("/api/land-use/decisions?match_tier=manual").json()["decisions"]]
    assert "vb:M2:R6" not in tier
    cfg = land_use.load_config(linked["cfg"])
    with db_module.db_readonly(linked["db"]) as conn:
        t = land_use_stats.compute_land_use_stats(conn, cfg)["timing"]["confirmed_only"]["t_first_application"]
    assert t["n"] == 0


def test_month_end_boundary_same_in_python_and_sql(env):
    with db_module.db(env["db"]) as conn:
        assert land_use_link.window_end(conn, "2025-01-31", 1) == "2025-03-03"   # SQLite date() arithmetic
        assert land_use_link.in_window(conn, "2025-01-31", "2025-03-03", 1)
        assert not land_use_link.in_window(conn, "2025-01-31", "2025-03-04", 1)
        assert not land_use_link.in_window(conn, "2025-01-31", None, 1)
        upsert_permits(conn, [vb("B1", "1 EDGE ST", "2025-03-03"), vb("B2", "1 EDGE ST", "2025-03-04")])
        conn.execute("INSERT INTO land_use_decisions (decision_key, meeting_id, resolution_id, meeting_date, "
                     "land_use_label, matched_pattern, source_file_sha256, ingested_at, updated_at) "
                     "VALUES ('vb:X:1', 'X', '1', '2025-01-31', 'rezoning', 'p', 's', 'now', 'now')")
        conn.execute("INSERT INTO permit_coverage (locality, backfill_from, backfill_run_id, backfill_source, "
                     "last_refresh_at, link_window_months, max_staleness_days, computed_at) "
                     "VALUES ('virginia_beach', '2024-06-01', 1, 't', ?, 1, 14, 'now')", (db_module.now_iso(),))
        for p in ("B1", "B2"):
            conn.execute("INSERT INTO decision_permit_links (decision_key, permit_id, match_tier, matched_on, "
                         "link_state, link_run_at) VALUES ('vb:X:1', ?, 'address_exact', 'x', 'candidate', 'now')",
                         (pid(conn, p),))
        eff = {r[0] for r in conn.execute("SELECT p.source_permit_id FROM decision_permit_links_effective k "
                                          "JOIN permits p ON p.id = k.permit_id")}
    assert eff == {"B1"}


# ---------- review fixes: v3 -> v4 migration ----------

def test_migrate_v3_to_v4_backfills_fingerprints_and_stales_old_reviews(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M1:R1")
    with db_module.db_readonly(env["db"]) as conn:
        expected = dict(conn.execute("SELECT decision_key, facts_fingerprint FROM land_use_decisions").fetchall())
    assert expected["vb:M1:R4"]  # quarantined decision has one too
    # simulate a v3 database: no fingerprints anywhere
    with db_module.db(env["db"]) as conn:
        with db_module.review_import(conn, "outcomes", None, actor="test-fixture"):
            conn.execute("UPDATE land_use_decisions SET facts_fingerprint = NULL")
            conn.execute("UPDATE decision_outcome_review SET reviewed_fingerprint = NULL")
        conn.execute("PRAGMA user_version=3")
    assert review_outcomes(env, [{"decision_key": "vb:M1:R2", "verified_outcome": "approved"}]) == 1  # NULL fp refused
    db_module.init_db(env["db"])
    with db_module.db_readonly(env["db"]) as conn:
        got = dict(conn.execute("SELECT decision_key, facts_fingerprint FROM land_use_decisions").fetchall())
    assert got == expected                                   # identical to what the ingest computes
    assert statuses(env)["vb:M1:R1"] == "outcome_review_stale"  # old review stays stale
    approve(env, "vb:M1:R1")                                  # re-review right after migration, no new ingest
    assert statuses(env)["vb:M1:R1"] == "candidate_permit"
