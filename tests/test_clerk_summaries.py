"""City Clerk's Summary of Council Actions: OCR-text parsing (synthetic fixture
in the real layout), storage, links to Planning Commission items, Council
stages for PC items and filings, privacy, archive coverage."""

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
from test_filings import FEATURES, LAYER, _stub, load_s  # noqa: E402,F401
from test_parcels import client, db_module, env, flooded, matched  # noqa: E402,F401
from ingest import clerk_summary as C, council_actions as CA, edocs, lead_facts, pc_hearings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "clerk" / "summary_03182025.txt"
PAGES = FIX.read_text().split("\f")


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _items():
    return {i["item_key"]: i for i in C.parse_summary("03182025", PAGES)["items"]}


# ---------- parsing ----------

def test_parses_land_use_items_motions_and_tallies():
    it = _items()
    k2 = it["vbcc:2025-03-18:K.2."]
    assert k2["is_land_use"] and k2["outcome"] == "approved_with_conditions" and (k2["aye"], k2["nay"]) == (11, 0)
    assert k2["motion_raw"] == "APPROVED/ CONDITIONED,BY CONSENT"
    assert [a["raw_text"] for a in k2["addresses"]] == ["4100 Quillfeather Road"] and k2["council_district"] == 4
    # "II-u" is OCR for 11-0; "fora" for "for a"
    k3 = it["vbcc:2025-03-18:K.3."]
    assert (k3["aye"], k3["nay"]) == (11, 0) and k3["is_land_use"]
    # "J.S." is J.5.; the deferral date wraps over three lines and a stray CONSENT / vote mark sits in the address
    j5 = it["vbcc:2025-03-18:J.5."]
    assert j5["outcome"] == "deferred" and j5["deferred_to"] == "2025-04-15"
    assert [a["raw_text"] for a in j5["addresses"]] == ["5200 Copperleaf Boulevard"]
    # dotless label; a "MOTION TO APPROVE FAILED / DENIED" motion wraps and does not start new items
    assert it["vbcc:2025-03-18:K.6."]["outcome"] == "denied" and it["vbcc:2025-03-18:K.6."]["aye"] == 6
    i7 = it["vbcc:2025-03-18:I.7."]
    assert i7["outcome"] == "denied" and "item label letter uncertain (I or L)" in i7["warnings"]
    assert [a["street_norm"] for a in i7["addresses"]] == ["22ND ST"]  # 22'd Street
    assert it["vbcc:2025-03-18:I.8."]["outcome"] == "withdrawn"
    assert it["vbcc:2025-03-18:I.8."]["request_types"] == ["street_closure"]
    # city business is not land use; the appointments heading ends the last item
    assert not it["vbcc:2025-03-18:K.1."]["is_land_use"]
    assert "WETLANDS" not in it["vbcc:2025-03-18:I.8."]["raw_block"]


@pytest.mark.parametrize("motion,meeting,expect", [
    ("APPROVED/ CONDITIONED,BY CONSENT", None, ("approved_with_conditions", None)),
    ("APPROVED/AS PROFFERED", None, ("approved_with_conditions", None)),
    ("APPROVED,BY CONSENT", None, ("approved", None)),
    ("DEFERRED TO JUNE 3,2025", None, ("deferred", "2025-06-03")),
    ("DEFERRED TO FEBRUARY 4,", "2024-12-10", ("deferred", "2025-02-04")),  # year not read: next such date
    ("DEFERRED INDEFINITELY", None, ("deferred_indefinitely", None)),
    ("DEFERED TO", None, ("deferred", None)),
    ("MOTION TO APPROVE FAILED", None, ("unmapped", None)),
    ("WITHDRAWN,BY CONSENT", None, ("withdrawn", None)),
    (None, None, ("unmapped", None)),
])
def test_outcome_map(motion, meeting, expect):
    assert C.normalize_outcome(motion, meeting) == expect


def test_motion_stops_before_garbled_votes():
    block = {"label": "L.1.", "page": 1, "lines": ["ACME YARDS, LLC for a Conditional Use APPROVED/ 10-I Y YYN Y Y",
                                                   "Permit re storage at 12 Example Road CONDITIONED"]}
    it = C.parse_item(block, "2025-01-07")
    assert it["motion_raw"] == "APPROVED/ CONDITIONED" and it["aye"] is None and "tally not readable" in it["warnings"]


def test_tally_ocr_and_sanity():
    assert C.parse_tally("I I-0 Y Y")[:2] == (11, 0)
    assert C.parse_tally("1 0-0 Y")[:2] == (10, 0)
    assert C.parse_tally("11-I) Y")[:2] == (None, None)  # not a readable count: never guessed
    assert C.parse_tally("9-9")[:2] == (None, None)  # more votes than members


def test_meeting_date_from_name_and_failures():
    assert C.meeting_date("03122026 SPECIAL", [""])[0] == "2026-03-12"
    d, w = C.meeting_date("03182025", ["DATE:03/19/2025 PAGE: 1"])
    assert d == "2025-03-18" and w and "differs" in w[0]
    assert C.parse_summary("03182025", ["", ""])["parse_status"] == "failed"
    assert C.parse_summary("notadate", ["SUMMARY OF COUNCIL ACTIONS"])["parse_status"] == "failed"


def test_slash_and_unspaced_ampersand_names_are_checked_part_by_part():
    assert not lead_facts.is_organization("JANE Q SAMPLE / ORBITAL MARINA, LLC")
    assert not lead_facts.is_organization("ORBITAL MARINA, LLC&JANE Q SAMPLE")
    assert lead_facts.is_organization("ORBITAL MARINA, LLC / NORTHWIND HOLDINGS, INC.")
    assert CA.public_names(["JANE Q SAMPLE", "ORBITAL MARINA, LLC"]) == {"names": ["ORBITAL MARINA, LLC"],
                                                                       "withheld": 1}


# ---------- storage, links, stages ----------

def _agenda(date_text, items):
    out = [f"A Public Hearing of the Virginia Beach Planning Commission will be held on Wednesday, {date_text}",
           "12:00 Noon: Public Hearing"]
    for n, (appl, req, addr, accela) in enumerate(items, 1):
        out += [f"{n}. {appl}", f"Property Owner: {appl}", req, f"Address: {addr}", "City Council: District 4",
                f"Accela Record: {accela}", "Staff Planner: Pat Planner", "Request."]
    return "\n".join(out) + "\n"


PC_FEB = _agenda("February 12, 2025", [
    ("Zephyr Testworks, LLC", "Conditional Use Permit (Bulk Storage Yard)", "4100 Quillfeather Road", "2025-PCCC-00001"),
    ("Orbital Marina, LLC", "Conditional Use Permit (Short Term Rental)", "210 Lumen Avenue, Unit 9", "2025-PCCC-00010"),
    ("Northwind Holdings, Inc.", "Conditional Rezoning (B-2 to A-24)", "5200 Copperleaf Boulevard", "2025-PCCC-00011"),
    ("Brightwater Studio, LLC", "Street Closure", "77 Saltmarsh Lane", "2025-PCCC-00012"),
])
APRIL = [""" CITY OF VIRGINIA BEACH
 SUMMARY OF COUNCIL ACTIONS
 DATE:04/15/2025 PAGE: 1
 ITEM# SUBJECT MOTION VOTE
 K.1. NORTHWIND HOLDINGS, INC. for a APPROVED/ 10-1 Y Y Y Y Y N Y Y Y Y Y
 Change of Zoning re apartments at 5200 PROFFERED
 Copperleaf Boulevard DISTRICT 9
 K.2. ZEPHYR TESTWORKS, LLC for a Change of APPROVED/ 11-0 Y Y Y Y Y Y Y Y Y Y Y
 Zoning re storage at 4100 Quillfeather Road PROFFERED
 DISTRICT 4
"""]


def _entry(eid, name, pages):
    return {"entry_id": eid, "name": name, "page_count": len(pages), "modified": "1/1/2025 1:00:00 PM"}


@pytest.fixture
def linked(env):
    with db_module.db(env["db"]) as conn:
        pc_hearings.store_document(conn, "agenda", "https://example.invalid/agendas/feb.pdf", "1" * 64, 1, PC_FEB)
        CA.store_document(conn, _entry(101, "03182025", PAGES), 9, 2025, PAGES)
        CA.store_document(conn, _entry(102, "04152025", APRIL), 9, 2025, APRIL)
        r = CA.link_pc(conn)
    return env, r


def _outcomes(env_):
    with db_module.db_readonly(env_["db"]) as conn:
        return {r["pc_item_key"]: dict(r) for r in conn.execute("SELECT * FROM pc_council_outcomes")}


def test_links_and_council_stage(linked):
    env_, r = linked
    o = _outcomes(env_)
    # deferred on 3/18, approved with proffers on 4/15: the latest action is the stage
    assert o["vbpc:2025-02-12:3"]["stage"] == "council_approved_with_conditions"
    assert o["vbpc:2025-02-12:3"]["council_date"] == "2025-04-15" and o["vbpc:2025-02-12:3"]["n_actions"] == 2
    # 3/18 CUP approved and 4/15 rezoning at the same site: the 4/15 action is a different request type
    assert o["vbpc:2025-02-12:1"]["council_date"] == "2025-03-18"
    with db_module.db_readonly(env_["db"]) as conn:
        amb = {(x["clerk_item_key"], x["pc_item_key"]): x["reason"] for x in conn.execute(
            "SELECT * FROM clerk_pc_links WHERE ambiguous = 1")}
    assert "request types differ" in amb[("vbcc:2025-04-15:K.2.", "vbpc:2025-02-12:1")]
    # unit 7 on the Council item, unit 9 on the PC item: possible only, no stage
    assert "different unit" in amb[("vbcc:2025-03-18:K.3.", "vbpc:2025-02-12:2")]
    assert "vbpc:2025-02-12:2" not in o
    # a CUP denial vs a street-closure PC item at the same address: no stage
    assert "vbpc:2025-02-12:4" not in o


def test_same_date_disagreement_is_conflicting(env):
    may = [""" SUMMARY OF COUNCIL ACTIONS
 DATE:05/06/2025 PAGE: 1
 ITEM# SUBJECT MOTION VOTE
 K.1. ZEPHYR TESTWORKS, LLC for a Conditional Use APPROVED/ 11-0 Y Y Y Y Y Y Y Y Y Y Y
 Permit re storage yard at 4100 Quillfeather Road CONDITIONED
 K.2. ZEPHYR TESTWORKS, LLC for a Conditional Use DENIED 3-8 Y Y Y N N N N N N N N
 Permit re outdoor sales at 4100 Quillfeather Road
"""]
    with db_module.db(env["db"]) as conn:
        pc_hearings.store_document(conn, "agenda", "https://example.invalid/agendas/feb.pdf", "1" * 64, 1, PC_FEB)
        CA.store_document(conn, _entry(103, "05062025", may), 9, 2025, may)
        CA.link_pc(conn)
    assert _outcomes(env)["vbpc:2025-02-12:1"]["stage"] == "council_conflicting"


def test_council_before_hearing_or_after_window_is_not_linked(env):
    early = PAGES  # 3/18/2025 Council meeting
    with db_module.db(env["db"]) as conn:
        pc_hearings.store_document(conn, "agenda", "https://example.invalid/agendas/late.pdf", "2" * 64, 1,
                                   _agenda("April 9, 2025", [("Zephyr Testworks, LLC", "Conditional Use Permit (X)",
                                                              "4100 Quillfeather Road", "2025-PCCC-00001")]))
        CA.store_document(conn, _entry(101, "03182025", early), 9, 2025, early)
        late = [p.replace("03/18/2025", "05/20/2026") for p in PAGES]
        CA.store_document(conn, _entry(104, "05202026", late), 9, 2026, late)
        r = CA.link_pc(conn)
    assert r["links"] == 0 and _outcomes(env) == {}


def test_unchanged_document_is_not_reparsed(env):
    with db_module.db(env["db"]) as conn:
        CA.store_document(conn, _entry(101, "03182025", PAGES), 9, 2025, PAGES)
        assert CA.store_document(conn, _entry(101, "03182025", PAGES), 9, 2025, PAGES)["unchanged"]
        n = conn.execute("SELECT COUNT(*) FROM clerk_items WHERE entry_id = 101").fetchone()[0]
        CA.store_document(conn, _entry(101, "03182025", PAGES[:1]), 9, 2025, PAGES[:1])
        assert conn.execute("SELECT COUNT(*) FROM clerk_items WHERE entry_id = 101").fetchone()[0] < n


def test_filing_reaches_council_stage(flooded, monkeypatch):
    fetch, _ = _stub(FEATURES)
    monkeypatch.setattr(load_s, "fetch_json", fetch)
    assert load_s.main(["--url", LAYER]) == 0
    agenda = _agenda("March 12, 2025", [("Acme Builders, LLC", "Conditional Use Permit (Example)", "3853 Shore Drive",
                                         "2025-PCCC-00001")])
    summary = [""" SUMMARY OF COUNCIL ACTIONS
 DATE:04/15/2025 PAGE: 1
 ITEM# SUBJECT MOTION VOTE
 K.4. ACME BUILDERS, LLC for a Conditional Use APPROVED/ 11-0 Y Y Y Y Y Y Y Y Y Y Y
 Permit re example at 3853 Shore Drive CONDITIONED,BY
 DISTRICT 4 CONSENT
"""]
    with db_module.db(flooded["db"]) as conn:
        pc_hearings.store_document(conn, "agenda", "https://example.invalid/agendas/a.pdf", "1" * 64, 1, agenda)
        CA.store_document(conn, _entry(105, "04152025", summary), 9, 2025, summary)
        CA.link_pc(conn)
    body = client.get("/api/land-use/filings").json()
    stages = {f["pln_id"]: f["stage"] for f in body["filings"]}
    assert stages["2025-PCCC-00001"] == "council_approved_with_conditions"
    assert client.get("/api/land-use/filings?stage=council_approved_with_conditions").json()["count"] == 1
    d = client.get("/api/land-use/filings/2025-PCCC-00001").json()
    assert d["council_date"] == "2025-04-15" and d["council_actions"][0]["source_url"].startswith(edocs.BASE)


# ---------- API, privacy ----------

def test_api_shows_outcome_and_source_but_no_names_or_text(linked):
    env_, _ = linked
    lst = client.get("/api/land-use/council-actions").json()
    assert lst["count"] == 8 and "OCR" in lst["notice"]
    text = json.dumps(lst)
    assert "JANE Q SAMPLE" not in text and "JOHN EXAMPLE" not in text and "ORBITAL MARINA, LLC" in text
    for banned in ("subject", "raw_block", "applicant_text"):
        assert f'"{banned}"' not in text
    one = client.get("/api/land-use/council-actions/vbcc:2025-03-18:K.3.").json()
    assert one["applicant"] == {"names": ["ORBITAL MARINA, LLC"], "withheld": 1}
    assert one["source_url"] == edocs.doc_url(101) and one["page"] == 1
    assert one["pc_items"][0]["ambiguous"] == 1
    assert client.get("/api/land-use/council-actions?outcome=denied").json()["count"] == 2
    assert client.get("/api/land-use/council-actions?outcome=bogus").status_code == 422
    assert client.get("/api/land-use/council-actions/vbcc:2025-03-18:K.1.").status_code == 404  # not land use
    pc = client.get("/api/land-use/pc-items/vbpc:2025-02-12:3").json()
    assert pc["council"]["stage"] == "council_approved_with_conditions"
    assert [a["outcome"] for a in pc["council_actions"]] == ["deferred", "approved_with_conditions"]
    assert pc["status"] == "scheduled"  # the PC status itself is unchanged: it never reads approved
    lst_pc = {i["item_key"]: i for i in client.get("/api/land-use/pc-items").json()["items"]}
    assert lst_pc["vbpc:2025-02-12:2"]["council"] is None


# ---------- ingest script, coverage, reports ----------

class StubArchive:
    def __init__(self, docs, drop=None):
        self.docs, self.drop, self.page_calls = docs, drop, 0

    def folder_listing(self, folder_id):
        if folder_id == edocs.SUMMARY_ACTIONS_FOLDER:
            return [{"entry_id": 9, "name": "SUMMARIES 2025", "type": 0, "page_count": None, "modified": None}]
        return [_entry(eid, name, pages) for eid, (name, pages) in self.docs.items()]

    def page_text(self, doc_id, page):
        self.page_calls += 1
        if doc_id == self.drop:
            raise edocs.EdocsError("timeout")
        return self.docs[doc_id][1][page - 1]


def test_ingest_script_coverage_cache_and_reports(env, monkeypatch, tmp_path):
    script = _script("ingest_clerk_summaries")
    with db_module.db(env["db"]) as conn:
        pc_hearings.store_document(conn, "agenda", "https://example.invalid/agendas/feb.pdf", "1" * 64, 1, PC_FEB)
    stub = StubArchive({101: ("03182025", PAGES), 102: ("04152025", APRIL)})
    monkeypatch.setattr(script, "make_client", lambda: stub)
    assert script.main(["--years", "2025", "--reports-dir", str(tmp_path)]) == 0
    calls = stub.page_calls
    assert script.main(["--years", "2025", "--reports-dir", str(tmp_path)]) == 0
    assert stub.page_calls == calls  # cached text reused while the archive's modified time is unchanged
    with db_module.db_readonly(env["db"]) as conn:
        cov = CA.coverage(conn)
    assert cov["verified"] and cov["years"][0]["listed"] == cov["years"][0]["stored"] == 2
    review = list(csv.DictReader((tmp_path / "clerk_parse_review.csv").open()))
    assert review and all(r["url"].startswith(edocs.BASE) for r in review)
    assert "SAMPLE" not in (tmp_path / "clerk_parse_review.csv").read_text()
    # a summary that cannot be read makes the year incomplete (exit 3)
    shutil.rmtree(Path(env["db"]).parent / "clerk_docs")
    monkeypatch.setattr(script, "make_client", lambda: StubArchive({101: ("03182025", PAGES),
                                                                    102: ("04152025", APRIL)}, drop=102))
    assert script.main(["--years", "2025", "--reports-dir", str(tmp_path)]) == 3
    with db_module.db_readonly(env["db"]) as conn:
        assert not CA.coverage(conn)["verified"]
    assert _script("clerk_report").main(["--out", str(tmp_path)]) == 0
    rows = list(csv.DictReader((tmp_path / "council_actions.csv").open()))
    assert len(rows) == 8 and "JANE Q SAMPLE" not in (tmp_path / "council_actions.csv").read_text()
    k3 = next(r for r in rows if r["item_label"] == "K.3.")
    assert k3["applicant"] == "ORBITAL MARINA, LLC" and k3["applicant_withheld"] == "1"
    assert k3["possible_pc_items"] == "vbpc:2025-02-12:2" and k3["pc_items"] == ""


def test_quality_section_and_checklist(linked):
    from ingest import quality
    env_, _ = linked
    with db_module.db_readonly(env_["db"]) as conn:
        q = quality.quality(conn)
    cq = q["council_actions"]
    assert cq["land_use_items"] == 8 and cq["pc_items_with_council_stage"] >= 1
    line = next(x for x in quality.acceptance_checklist(q, None) if x["item"] == "Council outcomes (Clerk) coverage verified")
    assert line["passed"] is False  # no archive check recorded in this fixture: never a pass


def test_v10_to_v11_migration_keeps_data(env):
    with db_module.db(env["db"]) as conn:
        pc_hearings.store_document(conn, "agenda", "https://example.invalid/agendas/feb.pdf", "1" * 64, 1, PC_FEB)
    conn = sqlite3.connect(env["db"])
    for t in ("clerk_index_checks", "clerk_decision_links", "pc_council_outcomes", "clerk_pc_links",
              "clerk_item_addresses", "clerk_items", "clerk_documents"):
        conn.execute(f"DROP TABLE {t}")
    conn.execute("DROP VIEW filing_status")
    conn.executescript(db_module._SCHEMA_V10_STATEMENTS[-1] + ";")  # the v10 view
    conn.execute("PRAGMA user_version=10")
    conn.commit()
    conn.close()
    db_module.init_db(env["db"])
    with db_module.db_readonly(env["db"]) as c:
        assert c.execute("SELECT COUNT(*) FROM pc_items").fetchone()[0] == 4
        assert "stage" in [r[1] for r in c.execute("PRAGMA table_info(filing_status)")]
        assert c.execute("SELECT COUNT(*) FROM clerk_items").fetchone()[0] == 0


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node not installed")
def test_council_html_escapes_and_links_the_clerk_page():
    actions = [{"meeting_date": "2025-04-15", "item_label": "<b>K.1.</b>", "outcome": "approved_with_conditions",
                "aye": 10, "nay": 1, "motion_raw": "<script>x</script>", "page": 2, "deferred_to": None,
                "source_url": "https://edocs.vbgov.com/WebLink/DocView.aspx?id=1&dbid=0&repo=CityClerk",
                "applicant": {"names": ["<i>Acme</i> LLC"], "withheld": 1}, "ambiguous": True, "reason": "different unit"}]
    council = {"stage": "council_approved_with_conditions", "council_date": "2025-04-15"}
    js = ("const u = require(process.argv[1]); const a = JSON.parse(process.argv[2]);"
          "process.stdout.write(u.buildCouncilHtml(a.c, a.x, 'n'))")
    out = subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js"),
                          json.dumps({"c": council, "x": actions})], capture_output=True, text=True, check=True).stdout
    assert "<script>" not in out and "<b>K.1." not in out and "&lt;i&gt;Acme&lt;/i&gt; LLC" in out
    assert "Approved by City Council, with conditions" in out and "10–1" in out
    assert "Possible match only: different unit" in out and "individuals not shown" in out
    assert 'href="https://edocs.vbgov.com/WebLink/DocView.aspx?id=1&amp;dbid=0&amp;repo=CityClerk"' in out


# ---------- people-checked samples of the Council chain ----------

def test_council_strata_sample_and_tally(linked, tmp_path):
    from ingest import accuracy, quality
    env_, _ = linked
    with db_module.db_readonly(env_["db"]) as conn:
        pops = accuracy.council_populations(conn)
    assert len(pops["council_outcome"]) == 8  # every mapped land-use item
    assert set(pops["council_link"]) == {"vbcc:2025-03-18:J.5.|vbpc:2025-02-12:3", "vbcc:2025-04-15:K.1.|vbpc:2025-02-12:3",
                                         "vbcc:2025-03-18:K.2.|vbpc:2025-02-12:1"}  # ambiguous links excluded
    out = tmp_path / "s"
    assert _script("accuracy_sample").main(["--seed", "7", "--out", str(out), "--synthetic"]) == 0
    sheet = out / "accuracy_sample.csv"
    text = sheet.read_text()
    assert "JANE Q SAMPLE" not in text and "applicant" not in text.splitlines()[0]
    assert "raw_block" not in text and "subject" not in text.splitlines()[0]
    rows = list(csv.DictReader(sheet.open()))
    council = [r for r in rows if r["stratum"].startswith("council_")]
    assert {r["stratum"] for r in council} == {"council_outcome", "council_link"}
    assert all(r["clerk_link"].startswith(edocs.BASE) for r in council)
    for r in rows:
        if r["stratum"] == "council_outcome":
            r["outcome_check"] = "wrong" if r["row_id"] == "vbcc:2025-03-18:K.6." else "right"
            r["checker"] = "T1"
        elif r["stratum"] == "council_link":
            r["same_application"], r["checker"] = "yes", "T1"

    def write(rs, path):
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=accuracy.COLUMNS)
            w.writeheader()
            w.writerows(rs)

    write(rows, sheet)
    tally_s = _script("accuracy_tally")
    bad = [dict(r) for r in rows]
    next(r for r in bad if r["stratum"] == "council_link")["outcome_check"] = "right"  # wrong column for the stratum
    write(bad, tmp_path / "bad.csv")
    assert tally_s.main([str(tmp_path / "bad.csv"), "--meta", str(out / "accuracy_sample.meta.json")]) == 1
    assert tally_s.main([str(sheet)]) == 0
    res = json.loads((out / "accuracy_tally.json").read_text())["results"]
    assert res["council_outcomes"]["adjudicated"] == 8 and res["council_outcomes"]["precision"] == 0.875
    assert res["council_links"]["precision"] == 1.0 and res["council_links"]["population"] == 3
    with db_module.db_readonly(env_["db"]) as conn:
        q = quality.quality(conn)
    lines = {x["item"]: x for x in quality.acceptance_checklist(q, None)}
    assert lines["Council outcomes checked against the Clerk's page"]["passed"] is True
    assert lines["Council-to-hearing links checked"]["passed"] is True
    # a re-parse of a summary changes the facts: the tally reads stale and its figures are withheld
    with db_module.db(env_["db"]) as conn:
        CA.store_document(conn, _entry(102, "04152025", APRIL), 9, 2025, [APRIL[0].replace("10-1", "9-2")])
    with db_module.db_readonly(env_["db"]) as conn:
        assert quality.accuracy_block(conn)["freshness"] == "stale"
