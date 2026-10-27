"""Planning Commission hearings: agenda / minutes parsing (synthetic fixtures in
the real layouts), storage, status, Council candidates, privacy, coverage."""

import csv
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import approve, client, db_module, env, ingest, link  # noqa: F401
from ingest import pc_agenda as P, pc_hearings as H  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "pc"
URL = "https://s3.us-east-1.amazonaws.com/virginia-beach-departments-docs/planning/Boards-Commissions/Planning-Commission"


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _items(name):
    return {i["item_label"]: i for i in P.parse_agenda((FIX / name).read_text())["items"]}


# ---------- parsing ----------

def test_2024_layout_roles_wrapping_and_false_headers():
    r = P.parse_agenda((FIX / "agenda_2024_style.txt").read_text())
    assert r["meeting_date"] == "2024-03-13"
    it = {i["item_label"]: i for i in r["items"]}
    assert list(it) == ["1", "2", "3 & 4", "5-9", "10", "11", "12"]  # "4.4(b)" and the briefing list are not items
    assert it["5-9"]["item_numbers"] == [5, 6, 7, 8, 9]
    assert it["3 & 4"]["applicants"] == ["Tidewater Power Company"]
    assert it["3 & 4"]["owners"] == ["Shoreline Entertainment, LC"]
    assert it["3 & 4"]["accela_records"] == ["2023-PCCC-00212", "2023-PCCC-00214"]  # wrapped across lines
    assert it["5-9"]["applicants"] == ["Cedar Grove Church & Maple Builders, LLC"]
    assert it["5-9"]["addresses"] == "1445 North Great Neck Road & 2307 Millwood Drive"
    assert it["5-9"]["gpins"] == ["2408191170", "2408099309"]
    assert it["2"]["adjacent"] == ["address", "gpin"] and it["2"]["addresses"] == "2375 Virginia Beach Boulevard"
    assert it["2"]["council_district"] == 3 and it["2"]["staff_planner"] == "Alex Planner"
    assert it["11"]["section"] == "SHORT TERM RENTALS" and it["11"]["gpins"] == ["24273224031620"]
    assert it["1"]["city_initiated"] and it["1"]["request"].startswith("An ordinance to amend Section 111")
    assert "Planning Commission Agenda" not in it["2"]["raw_block"]  # page headers stripped


def test_unreadable_item_is_kept_with_warnings():
    it = _items("agenda_2024_style.txt")["12"]
    assert set(it["warnings"]) >= {"no request type recognized", "no address or GPIN", "no Accela record"}
    assert it["raw_block"] == "Unreadable item with no labels at all"


def test_2026_layout_inline_applicant_owner_label_and_wraps():
    r = P.parse_agenda((FIX / "agenda_2026_style.txt").read_text())
    it = {i["item_label"]: i for i in r["items"]}
    assert r["meeting_date"] == "2026-08-12" and list(it) == ["1", "2", "3", "4"]
    assert r["notes"] and "numbering restarted" in r["notes"][0]  # the annual report list ends the items
    assert it["1"]["applicants"] == ["CITY OF VIRGINIA BEACH"] and it["1"]["request"].startswith("AN ORDINANCE")
    assert it["2"]["applicants"] == ["Kroll Example Residential, Inc."] and it["2"]["owners"] == ["Mary R Sample"]
    assert it["2"]["council_district_text"] == "District 4 (Ross-Hammond)" and it["2"]["council_district"] == 4
    assert it["3"]["notes"] == ["30-DAY DEFERRAL REQUESTED"] and it["3"]["applicants"] == ["Sample Real Estate Pursuits LC"]
    assert it["4"]["owners"] == ["Robert Example & Kim Example"]
    assert it["4"]["gpins"] == ["241798732601"] and "12 digits" in it["4"]["warnings"][0]
    assert it["4"]["description"] == "Request to operate a four-bedroom short term rental."
    assert it["2"]["description"] == "Request for a variance for a deficiency to the lot width requirement."  # "Intake" page header stripped


def test_2021_annotated_agenda_results():
    it = _items("agenda_2021_annotated.txt")
    assert it["1"]["agenda_result"] == {"result_raw": "APPROVAL", "council_on": "April 21"}
    assert it["1"]["applicants"] == ["City of Virginia Beach"]
    assert it["2"]["agenda_result"]["council_on"] == "June 1" and it["2"]["council_district_text"] == "Beach"
    assert it["3"]["agenda_result"]["result_raw"] == "WITHDRAWAL"
    assert it["4 & 5"]["item_numbers"] == [4, 5] and it["4 & 5"]["agenda_result"]["result_raw"] == "DENIAL"
    assert all("Deferral" not in i["raw_block"] for i in it.values())  # legend lines stripped


def test_minutes_recommendations_ranges_and_unmapped():
    m = P.parse_minutes((FIX / "minutes_2026_style.txt").read_text())
    assert m["meeting_date"] == "2026-08-12"
    got = [(s["numbers"], s["recommendation"], s["tally"]["aye"]) for s in m["items"]]
    assert got == [([1], "recommended_approval", 11), ([2], "recommended_denial", 3), ([3, 4], "unmapped", 10)]


@pytest.mark.parametrize("raw,norm", [("RECOMMENDED FOR APPROVAL", "recommended_approval"),
                                      ("RECOMMENDED FOR 30-DAY DEFERRAL", "deferred"),
                                      ("RECOMMENDED FOR DENIAL", "recommended_denial"), ("WITHDRAWN", "withdrawn"),
                                      ("APPROVAL", "recommended_approval"), ("APPROVED", "unmapped"), (None, "unmapped")])
def test_recommendation_map_never_yields_approval(raw, norm):
    assert P.normalize_recommendation(raw) == norm


def test_document_without_date_fails_and_stores_no_items(env):
    with db_module.db(env["db"]) as conn:
        r = H.store_document(conn, "agenda", URL + "/agendas/x.pdf", "a" * 64, 10, "12:00 Noon: Public Hearing\n1. X")
        assert r["parse_status"] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM pc_items").fetchone()[0] == 0
        assert conn.execute("SELECT parse_status FROM pc_documents").fetchone()[0] == "failed"


# ---------- storage, status, privacy ----------

def _store(conn, name, kind, url, sha="1"):
    return H.store_document(conn, kind, url, sha * 64, 100, (FIX / name).read_text())


@pytest.fixture
def stored(env):
    with db_module.db(env["db"]) as conn:
        _store(conn, "agenda_2024_style.txt", "agenda", URL + "/agendas/2024/a.pdf", "1")
        _store(conn, "agenda_2026_style.txt", "agenda", URL + "/agendas/2026/b.pdf", "2")
        _store(conn, "agenda_2021_annotated.txt", "agenda", URL + "/agendas/2021/c.pdf", "3")
    return env


def _status(env):
    with db_module.db_readonly(env["db"]) as conn:
        return dict(conn.execute("SELECT item_key, status FROM pc_item_status").fetchall())


def test_status_scheduled_then_minutes_then_never_approved(stored):
    st = _status(stored)
    assert st["vbpc:2026-08-12:2"] == "scheduled" and st["vbpc:2021-04-14:3"] == "withdrawn"
    assert st["vbpc:2021-04-14:4 & 5"] == "recommended_denial"
    with db_module.db(stored["db"]) as conn:
        _store(conn, "minutes_2026_style.txt", "minutes", URL + "/Minutes/2026/m.pdf", "4")
    st = _status(stored)
    assert st["vbpc:2026-08-12:1"] == "recommended_approval" and st["vbpc:2026-08-12:2"] == "recommended_denial"
    assert st["vbpc:2026-08-12:3"] == st["vbpc:2026-08-12:4"] == "recommendation_unmapped"
    assert set(st.values()) <= set(db_module.PC_STATUSES) and "approved" not in st.values()


def test_unchanged_document_is_not_reparsed(stored):
    with db_module.db(stored["db"]) as conn:
        assert _store(conn, "agenda_2024_style.txt", "agenda", URL + "/agendas/2024/a.pdf", "1") == {
            "url": URL + "/agendas/2024/a.pdf", "unchanged": True}


def test_api_withholds_individuals_and_raw_blocks(stored):
    body = client.get("/api/land-use/pc-items?limit=100").json()
    text = json.dumps(body)
    for person in ("Jane Q Public", "John Doe", "Mary R Sample", "Robert Example", "Kim Example"):
        assert person not in text
    assert "raw_block" not in text and "Harborview Holdings, LLC" in text
    it = {i["item_key"]: i for i in body["items"]}
    assert it["vbpc:2024-03-13:10"]["applicants"] == {"names": [], "withheld": 1}
    assert it["vbpc:2024-03-13:11"]["owners"] == {"names": ["Acme Rentals, LLC"], "withheld": 0}
    assert "vbpc:2024-03-13:1" not in it  # city-initiated items only with include_city=true
    assert "vbpc:2024-03-13:1" in {i["item_key"] for i in client.get("/api/land-use/pc-items?include_city=true&limit=100").json()["items"]}
    d = client.get("/api/land-use/pc-items/vbpc:2024-03-13:10").json()
    assert "Jane Q Public" not in json.dumps(d) and "raw_block" not in json.dumps(d)
    assert d["agenda"]["url"].endswith("/agendas/2024/a.pdf") and "not an approval" in d["notice"]
    assert client.get("/api/land-use/pc-items?status=approved").status_code == 422
    assert client.get("/api/land-use/pc-items/nope").status_code == 404


def test_council_candidates_only_after_the_hearing(env):
    assert ingest(env) == 0 and link(env) == 0  # vb:M1:R1: Council 2025-01-14, GPIN 1489-28-6485
    block = ("A Public Hearing of the Virginia Beach Planning Commission will be held on Wednesday, {d}\n"
             "12:00 Noon: Public Hearing\n1. Example Builders, LLC\nProperty Owner: Example Builders, LLC\n"
             "Conditional Rezoning (R-7.5 to B-2)\nAddress: 3853 Shore Drive\nGPIN: 1489286485\n"
             "City Council: District 4\nAccela Record: 2024-PCCC-00999\nStaff Planner: Pat Planner\nRequest.\n")
    with db_module.db(env["db"]) as conn:
        H.store_document(conn, "agenda", URL + "/agendas/before.pdf", "5" * 64, 1, block.format(d="December 11, 2024"))
        H.store_document(conn, "agenda", URL + "/agendas/after.pdf", "6" * 64, 1, block.format(d="February 12, 2025"))
    assert _script("link_pc_council").main([]) == 0
    with db_module.db_readonly(env["db"]) as conn:
        rows = conn.execute("SELECT item_key, decision_key, matched_on, council_after_days FROM pc_council_candidates"
                            ).fetchall()
    assert [tuple(r) for r in rows] == [("vbpc:2024-12-11:1", "vb:M1:R1", "gpin", 34)]
    d = client.get("/api/land-use/pc-items/vbpc:2024-12-11:1").json()
    assert d["council_candidates"][0]["decision_key"] == "vb:M1:R1"


# ---------- scripts, coverage, report ----------

def test_ingest_script_with_stubbed_pdf_text(env, monkeypatch):
    ing = _script("ingest_pc_documents")
    monkeypatch.setattr(ing, "pdf_text", lambda path: (FIX / "agenda_2026_style.txt").read_text())
    pdf = env["tmp"] / "x.pdf"
    pdf.write_bytes(b"%PDF-1.7 synthetic")
    assert ing.main(["--file", str(pdf), "--url", URL + "/agendas/2026/PC-Agenda-August-12-2026.pdf",
                     "--reports-dir", str(env["tmp"] / "rep")]) == 0
    review = list(csv.DictReader((env["tmp"] / "rep" / "pc_parse_review.csv").open()))
    assert [r["key"] for r in review if r["what"] == "item"] == ["vbpc:2026-08-12:4"]  # the 12-digit GPIN
    assert "Robert" not in (env["tmp"] / "rep" / "pc_parse_review.csv").read_text()
    stored = list((Path(db_module.DB_PATH).parent / "pc_docs").glob("*.pdf"))
    assert len(stored) == 1 and stored[0].read_bytes() == b"%PDF-1.7 synthetic"


def test_expected_hearings_are_second_wednesdays():
    assert H.expected_hearings("2026-08-01", "2026-10-31") == ["2026-08-12", "2026-09-09", "2026-10-14"]
    assert H.expected_hearings("2024-03-01", "2024-03-31") == ["2024-03-13"]


def test_coverage_unverified_until_index_read(stored, monkeypatch):
    with db_module.db_readonly(stored["db"]) as conn:
        c = H.coverage(conn, "2024-03-01", "2024-04-30")
    assert c["agenda_found"] == 1 and c["months_without_agenda"] == ["2024-04"] and not c["verified"]
    idx = _script("pc_index")
    monkeypatch.setattr(idx, "get_html", lambda url: (
        f'<a href="{URL}/agendas/2024/A.pdf">a</a><a href="{URL}/Minutes/2024/M.pdf">m</a>'
        '<a href="/boards-commissions/planning-commission/archive">older</a>' if url.endswith("planning-commission")
        else f'<a href="{URL}/agendas/2023/Old.pdf">old</a>'))
    out = stored["tmp"] / "idx.json"
    assert idx.main(["--out", str(out)]) == 0
    docs = json.loads(out.read_text())["documents"]
    assert {d["kind"] for d in docs} == {"agenda", "minutes"} and len(docs) == 3  # one archive page followed
    with db_module.db_readonly(stored["db"]) as conn:
        assert H.coverage(conn, "2024-03-01", "2024-04-30")["verified"]


def test_index_blocked_is_a_gap(env, monkeypatch):
    idx = _script("pc_index")

    def boom(url):
        raise idx.requests.ConnectionError("refused")
    monkeypatch.setattr(idx, "get_html", boom)
    assert idx.main(["--out", str(env["tmp"] / "i.json")]) == 3
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT status FROM pc_index_checks").fetchone()[0] == "blocked"


def test_pc_report_and_quality_checklist(stored):
    assert _script("pc_report").main(["--out", str(stored["tmp"] / "rep")]) == 0
    text = (stored["tmp"] / "rep" / "pc_leads.csv").read_text()
    rows = list(csv.DictReader((stored["tmp"] / "rep" / "pc_leads.csv").open()))
    assert "Jane Q Public" not in text and "John Doe" not in text
    assert {r["status"] for r in rows} <= {"scheduled", "recommended_approval", "deferred"}
    assert not any(r["item_key"].endswith(":1") and "CITY" in r["request"].upper() for r in rows)
    from ingest import quality
    with db_module.db_readonly(stored["db"]) as conn:
        q = quality.quality(conn)
    items = {c["item"]: c for c in quality.acceptance_checklist(q, None)}
    assert items["Planning Commission coverage verified"]["passed"] is None  # index never read
    assert q["planning_commission"]["items"] == 15


def test_v8_to_v9_migration_keeps_data(env):
    assert ingest(env) == 0
    conn = sqlite3.connect(env["db"])
    conn.execute("DROP VIEW pc_item_status")
    for t in ("pc_council_candidates", "pc_recommendations", "pc_items", "pc_index_checks", "pc_documents"):
        conn.execute(f"DROP TABLE {t}")
    conn.execute("PRAGMA user_version = 8")
    n = conn.execute("SELECT COUNT(*) FROM land_use_decisions").fetchone()[0]
    conn.commit()
    conn.close()
    db_module.init_db(env["db"])
    with db_module.db_readonly(env["db"]) as c:
        assert c.execute("SELECT COUNT(*) FROM land_use_decisions").fetchone()[0] == n
        assert c.execute("SELECT COUNT(*) FROM pc_item_status").fetchone()[0] == 0


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node not installed")
def test_pc_item_html_escapes_and_labels():
    it = {"item_key": "vbpc:2026-08-12:2", "meeting_date": "2026-08-12", "item_label": "2", "status": "recommended_approval",
          "status_source": "minutes", "addresses": "<b>1 Main</b>", "request": "Rezoning",
          "applicants": {"names": ["<i>Acme</i> LLC"], "withheld": 0}, "owners": {"names": [], "withheld": 1},
          "recommendation": {"aye": 9, "nay": 1, "abstain": 0, "absent": 1,
                             "minutes_url": "https://example.invalid/m.pdf"},
          "agenda": {"url": "javascript:alert(1)"}, "parcels": [], "council_candidates": [], "notice": "n",
          "size_from_description": [], "accela_records": ["2026-PCCC-1"], "warnings": []}
    js = "const u = require(process.argv[1]); process.stdout.write(u.buildPcItemHtml(JSON.parse(process.argv[2])))"
    out = subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js"), json.dumps(it)],
                         capture_output=True, text=True, check=True).stdout
    assert "<b>1 Main" not in out and "&lt;i&gt;Acme&lt;/i&gt; LLC" in out
    assert "1 individual owner — name not shown" in out and "City Council decides" in out
    assert 'href="javascript' not in out and 'href="https://example.invalid/m.pdf"' in out
    assert "vote 9–1" in out
