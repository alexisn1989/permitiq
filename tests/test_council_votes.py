"""A city's Council votes from a VoteIQ export (Chesapeake). Synthetic rows in the
confirmed layouts; invented names."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import client, db_module, env  # noqa: E402,F401
from ingest import council_votes as CV  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lcv", ROOT / "scripts" / "load_council_votes.py")
lcv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lcv)


def _vote(i, title, motion="Approve", result="passed", votes=None, date="Jan 20, 2026", printed=None):
    votes = votes or {"Alpha": "yes", "Bravo": "yes", "Charlie": "no"}
    y, n = sum(v == "yes" for v in votes.values()), sum(v == "no" for v in votes.values())
    return {"id": str(i), "clip_id": "1", "meeting_date": date, "title": title, "agenda_item": f"7.{i}", "motion": motion,
            "moved_by": "Alpha", "seconded_by": "Bravo", "result": result, "vote_count": printed or f"{y}-{n}",
            "votes_json": json.dumps(votes), "category": "substantive", "scraped_at": "2026-07-19"}


def _write(tmp, votes, member_rows=None, agenda=None, members_header=None):
    import csv
    def w(name, cols, rows):
        with (tmp / name).open("w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols)
            wr.writeheader()
            wr.writerows(rows)
    w("chesapeake_council_votes.csv", CV.VOTE_COLUMNS, votes)
    if member_rows is None:
        member_rows = [{"id": f"{v['id']}-{k}", "vote_id": v["id"], "clip_id": "1", "meeting_date": v["meeting_date"],
                        "member_name": k, "vote": x, "agenda_item": v["agenda_item"], "title": v["title"],
                        "result": v["result"], "category": v["category"]}
                       for v in votes for k, x in json.loads(v["votes_json"]).items()]
    w("chesapeake_council_member_votes.csv", CV.MEMBER_VOTE_COLUMNS, member_rows)
    w("chesapeake_council_members.csv", members_header or CV.MEMBER_COLUMNS,
      [{"id": "1", "name": "Pat Alpha", "district": "Mayor", "district_num": "0", "email": "alpha@example.invalid",
        "jurisdiction": "Chesapeake", "scraped_date": "2026-07-05"}] if not members_header else [])
    w("chesapeake_upcoming_agenda.csv", CV.AGENDA_COLUMNS, agenda or [])
    return tmp


def _load(env, tmp):
    return lcv.main(["--locality", "chesapeake", "--dir", str(tmp), "--reports-dir", str(env["reports"])])


def test_motion_kinds_and_outcomes():
    assert [CV.motion_kind(m) for m in ("Approve", "approval of the Resolution", "Deny", "Continued", "Remand",
                                       "Approve Withdrawal", "Teleport")] == \
        ["approve", "approve", "deny", "continue", "other", "other", "other"]
    v = lambda k, r: {"motion_kind": k, "result": r, "meeting_date": "2026-01-01", "vote_id": "1"}  # noqa: E731
    assert CV.vote_outcome(v("approve", "passed")) == "approved"
    assert CV.vote_outcome(v("approve", "failed")) == "not_approved"
    assert CV.vote_outcome(v("deny", "passed")) == "not_approved"
    assert CV.vote_outcome(v("deny", "failed")) is None  # not denied is not approved
    assert CV.case_check([v("approve", "passed")], "AS", True) == "agrees"
    assert CV.case_check([v("approve", "passed")], "D", True) == "disagrees"
    assert CV.case_check([v("approve", "passed")], None, True) == "city_not_recorded"
    assert CV.case_check([v("continue", "passed")], None, True) == "continued"
    assert CV.case_check([v("approve", "passed")], None, False) == "vote_only"
    assert CV.case_ids("PLN-REZ-2025-019 and UP-10-01 and S-12-34", {"UP-10-01"}) == ["PLN-REZ-2025-019", "UP-10-01"]


def test_load_flags_tally_never_stores_email_and_never_changes_cases(env, tmp_path):
    with db_module.db_readonly(env["db"]) as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM land_use_cases")]
    votes = [_vote(1, "Approval of PLN-REZ-2025-901 Example Rezoning"),
             _vote(2, "Continue PLN-USE-2025-902", motion="Continue", printed="9-0",
                   votes={"Alpha": "yes", "Bravo": "absent", "deTriquet": "yes"})]
    assert _load(env, _write(tmp_path, votes)) == 0
    with db_module.db_readonly(env["db"]) as conn:
        rows = {r["vote_id"]: dict(r) for r in conn.execute("SELECT * FROM council_votes_effective")}
        members = {r["member"] for r in conn.execute("SELECT member FROM council_member_votes")}
        dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM council_members")])
        assert [tuple(r) for r in conn.execute("SELECT * FROM land_use_cases")] == before
    assert rows["1"]["case_ids_json"] == '["PLN-REZ-2025-901"]' and rows["1"]["meeting_date"] == "2026-01-20"
    assert (rows["2"]["yes"], rows["2"]["absent"], rows["2"]["tally_mismatch"]) == (2, 1, 1)
    assert "de Triquet" in members and "deTriquet" not in members
    assert "example.invalid" not in dump
    assert _load(env, tmp_path) == 0  # identical reload
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM council_vote_loads").fetchone()[0] == 1
    r = client.get("/api/land-use/council-votes?locality=chesapeake&has_case=true").json()
    assert r["count"] == 2 and r["coverage"]["votes"] == 2
    m = {x["member"]: x for x in client.get("/api/council/members?locality=chesapeake").json()["members"]}
    assert (m["Alpha"]["yes"], m["Bravo"]["absent"], m["Bravo"]["present_share"]) == (2, 1, 0.5)


@pytest.mark.parametrize("bad", ["orphan", "mismatch", "header", "result"])
def test_inconsistent_export_writes_nothing(env, tmp_path, bad):
    votes = [_vote(1, "Approval of PLN-REZ-2025-901")]
    member_rows = None
    if bad == "orphan":
        member_rows = [{"id": "x", "vote_id": "99", "clip_id": "1", "meeting_date": "", "member_name": "Alpha",
                        "vote": "yes", "agenda_item": "", "title": "", "result": "passed", "category": ""}]
    elif bad == "mismatch":
        member_rows = [{"id": "x", "vote_id": "1", "clip_id": "1", "meeting_date": "", "member_name": "Alpha",
                        "vote": "no", "agenda_item": "", "title": "", "result": "passed", "category": ""}]
    elif bad == "result":
        votes = [_vote(1, "x", result="exploded")]
    _write(tmp_path, votes, member_rows, members_header=("id", "name", "phone") if bad == "header" else None)
    assert _load(env, tmp_path) == 1
    with db_module.db_readonly(env["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM council_vote_loads").fetchone()[0] == 0


def test_case_page_shows_votes_check_and_scheduled_hearing(env, tmp_path, monkeypatch):
    import test_chesapeake as TC
    monkeypatch.setattr(TC.load_s, "fetch_json", TC._service(TC.DATA))
    assert TC.load_s.main(["--source", "ches_cases"]) == 0          # PLN-REZ-2025-001: city CC "A"
    agenda = [{"id": "1", "event_id": "9", "meeting_date": "2999-01-05", "item_ref": "7.A", "section": "PUBLIC HEARING",
               "title": "PLN-REZ-2025-001 Example: rezoning", "category": "public-hearing", "agenda_url": "https://example.invalid/a",
               "how_to_participate": "", "scraped_at": "2026-07-19", "lat": "", "lng": "", "geocoded_address": ""},
              {"id": "2", "event_id": "8", "meeting_date": "2020-01-05", "item_ref": "7.B", "section": "PUBLIC HEARING",
               "title": "PLN-REZ-2025-001 old hearing", "category": "public-hearing", "agenda_url": "",
               "how_to_participate": "", "scraped_at": "2020-01-01", "lat": "", "lng": "", "geocoded_address": ""}]
    assert _load(env, _write(tmp_path, [_vote(1, "Approval of PLN-REZ-2025-001 Example Commons")], agenda=agenda)) == 0
    d = client.get("/api/land-use/cases/PLN-REZ-2025-001").json()
    assert d["council_check"] == "agrees" and d["council_votes"][0]["members"][2] == {"member": "Charlie", "vote": "no"}
    assert [a["meeting_date"] for a in d["council_scheduled"]] == ["2999-01-05"]  # past agenda dates are history only
    assert d["final"]["stage"] == "approved"  # the city's record, unchanged by any vote


def test_council_card_escapes():
    import shutil, subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    c = {"council_votes_coverage": {"date_min": "2023-06-13", "date_max": "2026-07-14"}, "council_check": "disagrees",
         "council_scheduled": [{"meeting_date": "2999-01-05", "item_ref": "<i>", "agenda_url": "javascript:alert(1)"}],
         "council_votes": [{"meeting_date": "2026-01-20", "motion": "<b>Approve</b>", "result": "passed", "yes": 8, "no": 1,
                            "abstain": 0, "absent": 0, "tally_mismatch": 1, "printed_tally": "9-0",
                            "members": [{"member": "<script>", "vote": "yes"}]}]}
    js = "const u = require(process.argv[1]); process.stdout.write(u.buildCouncilVotesHtml(JSON.parse(process.argv[2])))"
    out = subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js"), json.dumps(c)],
                         capture_output=True, text=True, check=True).stdout
    assert "<b>Approve" not in out and "<script>" not in out and 'href="javascript' not in out
    assert "printed tally 9-0 differs" in out and "does not match the city" in out and "Scheduled for City Council 2999-01-05" in out


# ---------- Norfolk layout: testimony as counts only, exact case links ----------

def _nf_write(tmp, votes, testimony):
    import csv
    def w(name, cols, rows):
        with (tmp / name).open("w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols)
            wr.writeheader()
            wr.writerows(rows)
    vrows, mrows = [], []
    for i, title, motion, votes_json in votes:
        y = sum(v == "yes" for v in votes_json.values()); n = sum(v == "no" for v in votes_json.values())
        vrows.append({"id": str(i), "meeting_id": "77", "meeting_date": "MARCH 25, 2025", "agenda_item": f"R-{i}",
                      "title": title, "motion": motion, "moved_by": "Alpha", "seconded_by": "Bravo", "result": "passed",
                      "vote_count": f"{y}-{n}", "votes_json": json.dumps(votes_json), "scraped_at": "x",
                      "category": "substantive"})
        mrows += [{"id": f"{i}-{k}", "vote_id": str(i), "meeting_id": "77", "meeting_date": "MARCH 25, 2025",
                   "member_name": k, "vote": x, "agenda_item": f"R-{i}", "title": title, "result": "passed",
                   "category": "substantive"} for k, x in votes_json.items()]
    w("norfolk_council_votes.csv", CV.NF_VOTE_COLUMNS, vrows)
    w("norfolk_council_member_votes.csv", CV.NF_MEMBER_VOTE_COLUMNS, mrows)
    w("norfolk_upcoming_agenda.csv", CV.NF_AGENDA_COLUMNS, [])
    w("norfolk_council_testimony.csv", CV.NF_TESTIMONY_COLUMNS, testimony)
    return tmp


def test_norfolk_votes_testimony_counts_only_and_exact_links(env, tmp_path, monkeypatch):
    import test_norfolk_records as TN
    monkeypatch.setattr(TN.ln, "fetch_json", TN._service({TN.NR.SOURCES["nf_cups"]: [TN._cup(1, 50492.0, "1229 EXAMPLE AVENUE", effective="2025-03-25")],
                                                          TN.NR.SOURCES["nf_conditional_zoning"]: []}))
    assert TN.ln.main(["--all"]) == 0                        # NF-CUP-50492 at 1229 EXAMPLE AVENUE, approved
    who = {"Alpha": "yes", "Smigiel": "yes", "Thomas Jr.": "no"}
    votes = [(1, "An Ordinance Granting a Conditional Use Permit at 1229 Example Avenue", "Adopt", who),
             (2, "An Ordinance Granting a Conditional Use Permit at 1229 Example", "Adopted", who),  # truncated: no link
             (3, "Ordinance 50492 amending ...", "Adopt", who)]
    speaker = lambda i, stance: {"id": str(i), "meeting_date": "MARCH 25, 2025", "agenda_item": "R-1",  # noqa: E731
                                 "speaker_name": "Jane Q Public", "speaker_addr": "1 Private Ln", "stance": stance,
                                 "note": "lives next door", "scraped_at": "x", "ward": "1", "superward": "6",
                                 "lat": "36.9", "lon": "-76.2", "geocode_source": "census"}
    _nf_write(tmp_path, votes, [speaker(1, "support"), speaker(2, "oppose"), speaker(3, "support")])
    assert lcv.main(["--locality", "norfolk", "--dir", str(tmp_path), "--reports-dir", str(env["reports"])]) == 0
    with db_module.db_readonly(env["db"]) as conn:
        links = {r["vote_id"]: json.loads(r["case_ids_json"]) for r in conn.execute("SELECT * FROM council_votes_effective")}
        members = {r[0] for r in conn.execute("SELECT member FROM council_member_votes")}
        t = dict(conn.execute("SELECT * FROM council_testimony_counts").fetchone())
        dump = json.dumps([[dict(r) for r in conn.execute(f"SELECT * FROM {tb}")] for tb in
                           ("council_votes", "council_member_votes", "council_testimony_counts", "council_agenda_items")])
    assert links == {"1": ["NF-CUP-50492"], "2": [], "3": ["NF-CUP-50492"]}
    assert members == {"Alpha", "Smigiel Jr.", "Thomas Jr."}
    assert (t["support"], t["oppose"], t["comment"]) == (2, 1, 0) and t["meeting_date"] == "2025-03-25"
    assert "Jane Q Public" not in dump and "Private Ln" not in dump and "next door" not in dump
    d = client.get("/api/land-use/cases/NF-CUP-50492").json()
    assert d["council_check"] == "agrees" and d["council_votes"][0]["testimony"]["support"] == 2


def test_object_id_keys_are_not_ordinance_numbers():
    known = {"NF-CZ-OID-10": (None, None), "NF-CUP-49563": ("9633 19TH BAY STREET", "2024-04-23")}
    assert CV.case_ids("Ordinance amending City Code section 10", known, "norfolk") == []
    assert CV.case_ids("Ordinance No. 49563 granting ...", known, "norfolk") == ["NF-CUP-49563"]

    # an address links only on the day the case took effect: another CUP at the same address is another case
    assert CV.case_ids("CUP at 9633 19th Bay Street", known, "norfolk", "2024-04-23") == ["NF-CUP-49563"]
    assert CV.case_ids("CUP at 9633 19th Bay Street", known, "norfolk", "2025-01-14") == []


# ---------- Portsmouth layout; v21 widens the result CHECK ----------

def test_portsmouth_layout_deferred_denied_and_dates(env, tmp_path):
    import csv
    def w(name, cols, rows):
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols)
            wr.writeheader()
            wr.writerows(rows)
    who = {"Alpha": "yes", "Bravo": "yes", "Charlie": "no"}
    vrows = [{"id": str(i), "doc_id": "D1", "meeting_date": date, "title": f"{m} Z-24-03 rezoning", "agenda_item": str(i),
              "motion": m, "moved_by": "Alpha", "seconded_by": "Bravo", "result": res, "vote_count": "2-1",
              "votes_json": json.dumps(who), "category": "substantive", "scraped_at": "x"}
             for i, (m, res, date) in enumerate((("Deny", "denied", "02/24/2026"), ("Defer", "deferred", "02/27-28/2026")), 1)]
    mrows = [{"id": f"{v['id']}-{k}", "vote_id": v["id"], "doc_id": "D1", "meeting_date": v["meeting_date"],
              "member_name": k, "vote": x, "agenda_item": v["agenda_item"], "title": v["title"], "result": v["result"],
              "category": "substantive"} for v in vrows for k, x in who.items()]
    w("portsmouth_council_votes.csv", CV.PT_VOTE_COLUMNS, vrows)
    w("portsmouth_council_member_votes.csv", CV.PT_MEMBER_VOTE_COLUMNS, mrows)
    w("portsmouth_council_members.csv", CV.MEMBER_COLUMNS, [{"id": "1", "name": "Pat Alpha", "district": "Mayor",
      "district_num": "0", "email": "", "jurisdiction": "Portsmouth", "scraped_date": "2026-07-06"}])
    w("portsmouth_upcoming_agenda.csv", CV.PT_AGENDA_COLUMNS, [])
    assert lcv.main(["--locality", "portsmouth", "--dir", str(tmp_path), "--reports-dir", str(env["reports"])]) == 0
    with db_module.db_readonly(env["db"]) as conn:
        rows = {r["vote_id"]: dict(r) for r in conn.execute("SELECT * FROM council_votes_effective")}
    assert (rows["1"]["result"], rows["1"]["motion_kind"], rows["1"]["meeting_date"]) == ("denied", "deny", "2026-02-24")
    assert (rows["2"]["result"], rows["2"]["motion_kind"], rows["2"]["meeting_date"]) == ("deferred", "continue", None)
    assert CV.vote_outcome(rows["1"]) == "not_approved" and rows["1"]["case_ids_json"] == "[]"  # no Portsmouth cases


def test_v20_to_v21_keeps_council_votes(env, tmp_path):
    import sqlite3
    assert _load(env, _write(tmp_path, [_vote(1, "Approval of PLN-REZ-2025-901")])) == 0
    with db_module.db_readonly(env["db"]) as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM council_votes ORDER BY vote_id")]
    conn = sqlite3.connect(env["db"])
    conn.execute("PRAGMA legacy_alter_table=ON")
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'council_votes'").fetchone()[0]
    view = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'council_votes_effective'").fetchone()[0]
    conn.execute("DROP VIEW council_votes_effective")
    conn.execute("ALTER TABLE council_votes RENAME TO cv_old")
    conn.execute(sql.replace(",'deferred','denied'", ""))  # the v16 CHECK
    conn.execute("INSERT INTO council_votes SELECT * FROM cv_old")
    conn.execute("DROP TABLE cv_old")
    conn.execute(view)
    conn.execute("PRAGMA user_version=20")
    conn.commit()
    conn.close()
    db_module.init_db(env["db"])
    with db_module.db_readonly(env["db"]) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM council_votes ORDER BY vote_id")] == before
        assert "'deferred'" in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'council_votes'").fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM council_votes_effective").fetchone()[0] == 1


# ---------- Suffolk: members listed are exceptions only ----------

def test_suffolk_exceptions_only_tally_alias_and_app_links(env, tmp_path, monkeypatch):
    import csv
    import test_suffolk as TS
    feats = [{"type": "Feature", "id": 1, "geometry": TS.BOX(-76.6), "properties": {
        "OBJECTID": 1, "APPLICATION": "CUP2024-012", "APPROVAL_DATE": 1700000000000, "AMEND_DATE": None,
        "RESTRICTION_TYPE": "CUP", "Ordinance": None, "Document_Link": None}}]
    def fetch(url, params=None, **kw):
        if not url.endswith("/query"):
            return {"objectIdField": "OBJECTID", "fields": [{"name": "OBJECTID"}] + [{"name": n} for n in TS.SR.FIELDS]}
        if params.get("returnCountOnly"):
            return {"count": 1}
        if params.get("returnIdsOnly"):
            return {"objectIds": [1]}
        return {"type": "FeatureCollection", "features": feats}
    monkeypatch.setattr(TS.ls, "fetch_json", fetch)
    assert TS.ls.main([]) == 0                                    # SF-CUP2024-012, approved
    def w(name, cols, rows):
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols)
            wr.writeheader()
            wr.writerows(rows)
    votes = [("1", "Approve CUP2024-012 Example", "Approved", "7-0", {"Barlow": "absent"}),
             ("2", "Approve budget", "Approved", "8-0", {})]
    w("suffolk_council_votes.csv", CV.SF_VOTE_COLUMNS, [
        {"id": i, "meeting_id": "M1", "meeting_date": "2024-11-20", "title": t, "agenda_item": i, "motion": m,
         "result": "passed", "vote_count": vc, "votes_json": json.dumps(j), "category": "substantive", "scraped_at": "x"}
        for i, t, m, vc, j in votes])
    w("suffolk_council_member_votes.csv", CV.SF_MEMBER_VOTE_COLUMNS, [
        {"id": "m1", "vote_id": "1", "meeting_id": "M1", "meeting_date": "2024-11-20", "member_name": "Butler Barlow",
         "vote": "absent", "agenda_item": "1", "title": "x", "result": "passed", "category": "substantive"}])
    w("suffolk_upcoming_agenda.csv", CV.AGENDA_COLUMNS, [])
    assert lcv.main(["--locality", "suffolk", "--dir", str(tmp_path), "--reports-dir", str(env["reports"])]) == 0
    with db_module.db_readonly(env["db"]) as conn:
        rows = {r["vote_id"]: dict(r) for r in conn.execute("SELECT * FROM council_votes_effective")}
    assert (rows["1"]["yes"], rows["1"]["no"], rows["1"]["absent"], rows["1"]["tally_mismatch"]) == (7, 0, 1, 0)
    assert rows["1"]["case_ids_json"] == '["SF-CUP2024-012"]' and rows["2"]["case_ids_json"] == "[]"
    d = client.get("/api/land-use/cases/SF-CUP2024-012").json()
    assert d["council_check"] == "agrees" and d["council_votes"][0]["members_exceptions_only"] is True
    m = client.get("/api/council/members?locality=suffolk").json()
    assert m["exceptions_only"] and m["members"][0]["yes"] is None and m["members"][0]["absent"] == 1


def test_exceptions_only_contradiction_is_flagged_not_fatal():
    import io, csv
    def text(cols, rows):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()
    votes = text(CV.SF_VOTE_COLUMNS, [{"id": "1061", "meeting_id": "M", "meeting_date": "2026-05-06", "title": "x",
                                       "agenda_item": "1", "motion": "Approved", "result": "passed", "vote_count": "8-0",
                                       "votes_json": "{}", "category": "substantive", "scraped_at": "x"}])
    members = text(CV.SF_MEMBER_VOTE_COLUMNS, [{"id": "m", "vote_id": "1061", "meeting_id": "M", "meeting_date": "2026-05-06",
                                                "member_name": "Williams", "vote": "absent", "agenda_item": "1",
                                                "title": "x", "result": "passed", "category": "substantive"}])
    data = CV.build({"votes": votes, "member_votes": members, "agenda": text(CV.AGENDA_COLUMNS, [])}, {}, "suffolk")
    v = data["votes"][0]
    assert v["source_conflict"] == 1 and (v["yes"], v["no"], v["absent"]) == (8, 0, 1)  # kept as published
    assert data["member_votes"] == [{"vote_id": "1061", "member": "Williams", "member_raw": "Williams", "vote": "absent"}]


def test_hampton_layout():
    assert CV.meeting_date("3/12/2025") == "2025-03-12" and CV.meeting_date("12/1/2025") == "2025-12-01"
    assert CV.motion_kind("Approve with eleven conditions", "hampton") == "approve"
    assert CV.motion_kind("Defer Use Permits", "hampton") == "continue"
    assert CV.motion_kind("Defer Appointment", "hampton") == "other"  # not a land-use continuance
    assert CV.LAYOUTS["hampton"]["files"]["members"][1] == CV.MEMBER_COLUMNS  # emails read, never stored


def test_newport_news_roll_from_member_rows_unknown_kept(env, tmp_path):
    import csv
    def w(name, cols, rows):
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols)
            wr.writeheader()
            wr.writerows(rows)
    votes = [("1", "adoption of the ordinance", "Carried", "2-0"), ("2", "to close the public hearing", "DEFEATED.", "0-2")]
    w("newport_news_council_votes.csv", CV.NN_VOTE_COLUMNS, [
        {"id": i, "meeting_date": "Mar 12 2025", "title": "x", "agenda_item": i, "motion": m, "moved_by": "", "seconded_by": "",
         "result": res, "vote_count": vc, "votes_json": "{}", "category": "substantive", "scraped_at": "x"} for i, m, res, vc in votes])
    who = {"1": {"Alpha": "yes", "Bravo": "yes", "Charlie": "unknown"}, "2": {"Alpha": "no", "Bravo": "no", "Charlie": "absent"}}
    w("newport_news_council_member_votes.csv", CV.NN_MEMBER_VOTE_COLUMNS, [
        {"id": f"{v}-{k}", "vote_id": v, "meeting_date": "Mar 12 2025", "member_name": k, "vote": x, "agenda_item": v,
         "title": "x", "result": "", "category": ""} for v, d in who.items() for k, x in d.items()])
    w("newport_news_council_members.csv", CV.MEMBER_COLUMNS, [])
    w("newport_news_upcoming_agenda.csv", CV.PT_AGENDA_COLUMNS, [])
    assert lcv.main(["--locality", "newport_news", "--dir", str(tmp_path), "--reports-dir", str(env["reports"])]) == 0
    with db_module.db_readonly(env["db"]) as conn:
        rows = {r["vote_id"]: dict(r) for r in conn.execute("SELECT * FROM council_votes_effective")}
        unknown = conn.execute("SELECT COUNT(*) FROM council_member_votes WHERE vote = 'unknown'").fetchone()[0]
    assert (rows["1"]["result"], rows["1"]["motion_kind"], rows["1"]["yes"], rows["1"]["meeting_date"]) == \
        ("passed", "approve", 2, "2025-03-12")
    assert (rows["2"]["result"], rows["2"]["motion_kind"], rows["2"]["no"], rows["2"]["tally_mismatch"]) == ("failed", "other", 2, 0)
    assert unknown == 1  # kept as published, never read as absent or yes
