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
