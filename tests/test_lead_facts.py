"""Lead facts: applicant / request / size from the title (unverified), and timing."""

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import (approve, client, db_module, env, ingest, link, pid, review_links,  # noqa: F401
                           land_use)
from ingest import lead_facts as L  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("title,kind,name", [
    ("Application of ALPHA LLC for a Change of Zoning at 1 A Street", "organization", "ALPHA LLC"),
    ("Applications of Sea Breeze Properties, L.L.C. for a Conditional Change of Zoning", "organization",
     "Sea Breeze Properties, L.L.C."),
    ("Application of City of Virginia Beach for a Street Closure", "organization", "City of Virginia Beach"),
    ("Application of John Smith for a Conditional Use Permit at 12 Elm Street", "withheld", None),
    ("Application of John Smith and Acme Builders, Inc. for a Variance", "withheld", None),  # any person: withhold all
    ("Application of Mary Jones Trust for a Variance at 9 Bay Lane", "withheld", None),       # trusts name people
    ("Ordinance to APPROPRIATE $10,000 to Parks and Recreation", None, None),
])
def test_applicant_shows_organizations_only(title, kind, name):
    a = L.applicant(title)
    assert a["kind"] == kind and a["name"] == name
    if kind != "organization":
        assert a["text"] is None  # a withheld name is not leaked through its span either


def test_request_and_scale_keep_their_source_text():
    t = ("Application of Harbor Point Development Group for a Conditional Use Permit (Multi-Family) for 240 dwelling "
         "units and 12,500 square feet of retail on approximately 31.5 acres at 500 Atlantic Avenue")
    assert L.request(t)["text"] == "Conditional Use Permit (Multi-Family)"
    got = {(s["kind"], s["value"]) for s in L.scale(t)}
    assert got == {("units", 240.0), ("square_feet", 12500.0), ("acres", 31.5)}
    assert all(s["text"] in t for s in L.scale(t))
    assert L.scale("Change of Zoning from R-7.5 to B-2 ($10,000 fee) DISTRICT 4") == []


def test_detail_api_lead_facts_and_timing(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M2:R6")
    f = client.get("/api/land-use/decisions/vb:M2:R6").json()["lead_facts"]
    assert f["applicant"] == {"name": "EPSILON LLC", "kind": "organization",
                              "text": "Application of EPSILON LLC for"}
    assert f["request"]["text"] == "Conditional Use Permit"
    t = f["timing"]
    with db_module.db_readonly(env["db"]) as conn:
        cov = conn.execute("SELECT last_refresh_at, link_window_months FROM permit_coverage").fetchone()
        days = conn.execute("SELECT CAST(julianday(?) - julianday(meeting_date) AS INTEGER) FROM land_use_decisions "
                            "WHERE decision_key = 'vb:M2:R6'", (cov[0][:10],)).fetchone()[0]
        end = conn.execute("SELECT date(meeting_date, '+' || ? || ' months') FROM land_use_decisions "
                           "WHERE decision_key = 'vb:M2:R6'", (cov[1],)).fetchone()[0]
    assert t["shown"] and t["days_since_approval"] == days and t["window_end"] == end
    assert t["benchmark"]["n"] == 0 and t["benchmark"]["suppressed"] and t["typical_first_application"] is None
    unverified = client.get("/api/land-use/decisions/vb:M2:R5").json()["lead_facts"]["timing"]
    assert not unverified["shown"] and "not verified" in unverified["reason"]


def test_benchmark_from_confirmed_links_only(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M1:R1", "vb:M2:R6")
    cfg = land_use.load_config(env["cfg"])  # min_n = 1
    with db_module.db_readonly(env["db"]) as conn:
        p1 = pid(conn, "P1")
        decision = dict(conn.execute("SELECT meeting_date, 'approved' AS verified_outcome FROM land_use_decisions "
                                     "WHERE decision_key = 'vb:M2:R6'").fetchone())
        assert L.timing(conn, decision, cfg)["benchmark"]["n"] == 0  # candidates never count
    assert review_links(env, [{"decision_key": "vb:M1:R1", "permit_id": p1, "verdict": "confirmed"}]) == 0
    with db_module.db_readonly(env["db"]) as conn:
        t = L.timing(conn, decision, cfg)
        lag = conn.execute("SELECT CAST(julianday(p.applied_date) - julianday(d.meeting_date) AS INTEGER) "
                           "FROM permits p, land_use_decisions d WHERE p.id = ? AND d.decision_key = 'vb:M1:R1'",
                           (p1,)).fetchone()[0]
        expect = conn.execute("SELECT date(?, '+' || ? || ' days')", (decision["meeting_date"], lag)).fetchone()[0]
    b = t["benchmark"]
    assert b["n"] == 1 and b["median_days"] == lag and b["min_n"] == 1
    assert t["typical_first_application"] == {"from": expect, "median": expect, "to": expect}
    assert t["past_typical_range"] is False  # decision dict has no status: never flagged without "no permit"
    with db_module.db_readonly(env["db"]) as conn:
        t2 = L.timing(conn, {**decision, "status": "no_candidate_permit"}, cfg)
        assert t2["past_typical_range"] == (t2["days_since_approval"] > lag)


def test_detail_without_config_still_shows_facts(env, monkeypatch):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M2:R6")
    import app as app_module
    monkeypatch.setattr(app_module, "LAND_USE_CONFIG_PATH", env["tmp"] / "missing.toml")
    f = client.get("/api/land-use/decisions/vb:M2:R6").json()["lead_facts"]
    assert f["timing"]["shown"] and f["timing"]["benchmark"] == {"configured": False}


def test_leads_csv_has_lead_fact_columns(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M2:R6")
    import importlib.util
    spec = importlib.util.spec_from_file_location("land_use_report", ROOT / "scripts" / "land_use_report.py")
    rep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rep)
    out = env["tmp"] / "rep"
    assert rep.main(["--config", str(env["cfg"]), "--out", str(out)]) == 0
    rows = list(csv.DictReader((out / "land_use_leads.csv").open()))
    r = next(x for x in rows if x["decision_key"] == "vb:M2:R6")
    assert r["applicant_from_title"] == "EPSILON LLC" and r["request_from_title"] == "Conditional Use Permit"
    assert r["days_since_approval"].isdigit() and r["link_window_end"]
    assert r["typical_first_application_from"] == ""  # no confirmed approvals yet: no benchmark range


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node not installed")
def test_lead_facts_html_escapes_and_labels():
    f = {"source_note": "extracted <unverified>", "title_truncated": True,
         "applicant": {"kind": "organization", "name": "<b>ACME</b> LLC", "text": "Application of <b>ACME</b> LLC for"},
         "request": {"text": "Change of Zoning"},
         "scale_from_title": [{"kind": "acres", "value": 31.5, "text": "31.5 acres"}],
         "parcel_acres": 2.5, "parcels": 1,
         "timing": {"shown": True, "approved_on": "2025-01-14", "days_since_approval": 400, "observed_to": "2026-02-18",
                    "window_end": "2030-01-14", "benchmark": {"n": 0, "suppressed": True, "min_n": 20},
                    "typical_first_application": None}}
    js = "const u = require(process.argv[1]); process.stdout.write(u.buildLeadFactsHtml(JSON.parse(process.argv[2])))"
    out = subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js"), json.dumps(f)],
                         capture_output=True, text=True, check=True).stdout
    assert "<b>ACME" not in out and "&lt;b&gt;ACME&lt;/b&gt; LLC" in out
    assert "31.5 acres" in out and "2.5 acres" in out and "parcel layer, 1 parcel)" in out
    assert "Not enough confirmed approvals for a benchmark yet (n=0, needs 20)" in out
    assert "cut at 400 characters" in out and "extracted &lt;unverified&gt;" in out
    f["applicant"] = {"kind": "withheld", "name": None, "text": None}
    out = subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js"), json.dumps(f)],
                         capture_output=True, text=True, check=True).stdout
    assert "individual applicant — name not shown" in out
