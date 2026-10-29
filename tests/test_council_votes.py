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
