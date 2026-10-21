"""Schema v8: append-only review history with per-import provenance."""

import csv
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_use import (approve, client, db_module, env, ingest, link, pid, review_links,  # noqa: F401
                           review_outcomes, review_s)


def _audit(env_, table=None):
    with db_module.db_readonly(env_["db"]) as conn:
        sql = "SELECT a.*, i.kind, i.source_file, i.source_sha256, i.acting_user FROM review_audit a " \
              "JOIN review_imports i ON i.import_id = a.import_id"
        rows = conn.execute(sql + (" WHERE a.review_table = ?" if table else "") + " ORDER BY a.audit_id",
                            (table,) if table else ()).fetchall()
    return [dict(r) for r in rows]


@pytest.fixture
def reviewed(env):
    assert ingest(env) == 0 and link(env) == 0
    approve(env, "vb:M1:R1")
    return env


def _raw(env_):
    conn = sqlite3.connect(env_["db"])
    conn.isolation_level = None  # autocommit: every statement stands alone
    return conn


def test_history_insert_update_update_with_provenance(reviewed):
    assert review_outcomes(reviewed, [{"decision_key": "vb:M1:R1", "verified_outcome": "denied"}]) == 0
    assert review_outcomes(reviewed, [{"decision_key": "vb:M1:R1", "verified_outcome": "approved",
                                       "reviewer": "second"}]) == 0
    h = [a for a in _audit(reviewed, "decision_outcome_review")
         if json.loads(a["subject_json"]) == {"decision_key": "vb:M1:R1"}]
    assert [a["action"] for a in h] == ["insert", "update", "update"]
    assert json.loads(h[1]["old_json"])["verified_outcome"] == "approved"
    assert json.loads(h[1]["new_json"])["verified_outcome"] == "denied"
    assert json.loads(h[2]["new_json"])["reviewer"] == "second" and h[2]["review_author"] == "second"
    assert len({a["import_id"] for a in h}) == 3  # each import its own id
    path = reviewed["tmp"] / "outcomes.csv"
    assert h[2]["source_file"] == str(path) and h[2]["source_sha256"] == db_module.file_sha256(path)
    assert h[0]["source_sha256"] != h[2]["source_sha256"]  # each import records its own file's hash
    with db_module.db_readonly(reviewed["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_write_context").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM review_audit_pending").fetchone()[0] == 0


def test_write_outside_an_import_aborts(reviewed):
    conn = _raw(reviewed)
    for sql in ("UPDATE decision_outcome_review SET evidence = 'x'",
                "DELETE FROM decision_outcome_review",
                "INSERT INTO decision_link_review VALUES ('vb:M1:R1', 1, 'rejected', NULL, 'x', 'now')"):
        with pytest.raises(sqlite3.IntegrityError, match="outside an import"):
            conn.execute(sql)
    assert conn.execute("SELECT evidence FROM decision_outcome_review").fetchone()[0].startswith("https://")


def test_failed_import_leaves_no_context_and_no_rows(reviewed):
    before = len(_audit(reviewed))
    with db_module.db(reviewed["db"]) as conn:
        with pytest.raises(RuntimeError):
            with db_module.review_import(conn, "outcomes", None, actor="t"):
                conn.execute("UPDATE decision_outcome_review SET evidence = 'changed'")
                raise RuntimeError("boom")
        assert conn.execute("SELECT COUNT(*) FROM review_write_context").fetchone()[0] == 0
        assert conn.execute("SELECT evidence FROM decision_outcome_review").fetchone()[0] != "changed"
    assert len(_audit(reviewed)) == before
    # an import refused by validation writes nothing either
    assert review_outcomes(reviewed, [{"decision_key": "vb:nope", "verified_outcome": "approved"}]) == 1
    assert len(_audit(reviewed)) == before


def test_import_cannot_start_inside_an_open_transaction(reviewed):
    with db_module.db(reviewed["db"]) as conn:
        conn.execute("UPDATE permits SET status = status")
        with pytest.raises(RuntimeError, match="its own transaction"):
            with db_module.review_import(conn, "outcomes", None):
                pass


@pytest.mark.parametrize("recursive", [False, True])
def test_audit_and_imports_are_append_only(reviewed, recursive):
    conn = _raw(reviewed)
    conn.execute(f"PRAGMA recursive_triggers = {'ON' if recursive else 'OFF'}")
    row = conn.execute("SELECT * FROM review_audit ORDER BY audit_id LIMIT 1").fetchone()
    aid, iid = row[0], row[1]
    for sql, args in (("UPDATE review_audit SET action = 'delete' WHERE audit_id = ?", (aid,)),
                      ("DELETE FROM review_audit WHERE audit_id = ?", (aid,)),
                      ("INSERT OR REPLACE INTO review_audit (audit_id, import_id, review_table, subject_json, action, "
                       "recorded_at) VALUES (?, ?, 'decision_outcome_review', '{}', 'insert', 'x')", (aid, iid)),
                      ("REPLACE INTO review_audit (audit_id, import_id, review_table, subject_json, action, "
                       "recorded_at) VALUES (?, ?, 'decision_outcome_review', '{}', 'insert', 'x')", (aid, iid)),
                      ("UPDATE review_imports SET source_file = 'forged' WHERE import_id = ?", (iid,)),
                      ("DELETE FROM review_imports WHERE import_id = ?", (iid,)),
                      ("INSERT OR REPLACE INTO review_imports (import_id, kind, started_at) VALUES (?, 'links', 'x')",
                       (iid,))):
        with pytest.raises(sqlite3.IntegrityError, match="append-only|current import"):
            conn.execute(sql, args)
    # inside a real import (so the current-import guard passes) REPLACE onto an existing row still fails
    with db_module.db(reviewed["db"]) as c2:
        c2.execute(f"PRAGMA recursive_triggers = {'ON' if recursive else 'OFF'}")
        with pytest.raises(KeyboardInterrupt):  # sentinel: roll the import back afterwards
            with db_module.review_import(c2, "outcomes", None, actor="t") as cur:
                for verb in ("INSERT OR REPLACE INTO", "REPLACE INTO"):
                    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                        c2.execute(f"{verb} review_audit (audit_id, import_id, review_table, subject_json, action, "
                                   "recorded_at) VALUES (?, ?, 'decision_outcome_review', '{}', 'insert', 'x')",
                                   (aid, cur))
                raise KeyboardInterrupt
    assert conn.execute("SELECT * FROM review_audit WHERE audit_id = ?", (aid,)).fetchone() == row
    # a new audit row outside an import is refused too (it would carry no provenance)
    with pytest.raises(sqlite3.IntegrityError, match="current import"):
        conn.execute("INSERT INTO review_audit (import_id, review_table, subject_json, action, recorded_at) "
                     "VALUES (?, 'decision_outcome_review', '{}', 'insert', 'x')", (iid,))


@pytest.mark.parametrize("recursive", [False, True])
def test_insert_or_replace_on_a_review_table_records_the_old_row(reviewed, recursive):
    with db_module.db(reviewed["db"]) as conn:
        conn.execute(f"PRAGMA recursive_triggers = {'ON' if recursive else 'OFF'}")
        with db_module.review_import(conn, "outcomes", None, actor="t"):
            conn.execute("INSERT OR REPLACE INTO decision_outcome_review (decision_key, verified_outcome, evidence, "
                         "reviewer, reviewed_at) VALUES ('vb:M1:R1', 'denied', 'e', 'replacer', 'now')")
            # a plain INSERT on an existing key still fails, and leaves nothing stashed
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                conn.execute("INSERT INTO decision_outcome_review (decision_key, verified_outcome, evidence, "
                             "reviewer, reviewed_at) VALUES ('vb:M1:R1', 'denied', 'e', 'x', 'now')")
    h = [a for a in _audit(reviewed, "decision_outcome_review") if a["kind"] == "outcomes" and a["acting_user"] == "t"]
    replaced = [a for a in h if a["action"] == "replace"]
    assert len(replaced) == 1 and json.loads(replaced[0]["old_json"])["verified_outcome"] == "approved"
    assert json.loads(replaced[0]["old_json"])["reviewer"] == "tester"
    assert replaced[0]["review_author"] == "replacer"
    if recursive:  # REPLACE's implicit delete now fires the delete trigger as well: also recorded, never lost
        assert [a["action"] for a in h] == ["delete", "replace"]


def test_upsert_is_an_update_not_a_replace(reviewed):
    assert review_outcomes(reviewed, [{"decision_key": "vb:M1:R1", "verified_outcome": "denied"}]) == 0
    assert "replace" not in {a["action"] for a in _audit(reviewed)}


def test_delete_names_author_and_actor_separately(reviewed):
    with db_module.db(reviewed["db"]) as conn:
        with db_module.review_import(conn, "outcomes", None, actor="admin-cleanup"):
            conn.execute("DELETE FROM decision_outcome_review WHERE decision_key = 'vb:M1:R1'")
    d = [a for a in _audit(reviewed) if a["action"] == "delete"]
    assert len(d) == 1 and d[0]["review_author"] == "tester" and d[0]["acting_user"] == "admin-cleanup"
    assert d[0]["new_json"] is None and json.loads(d[0]["old_json"])["decision_key"] == "vb:M1:R1"


def test_link_subject_round_trips_and_api(reviewed):
    with db_module.db_readonly(reviewed["db"]) as conn:
        p1 = pid(conn, "P1")
    assert review_links(reviewed, [{"decision_key": "vb:M1:R1", "permit_id": p1, "verdict": "rejected"}]) == 0
    a = _audit(reviewed, "decision_link_review")[-1]
    assert a["subject_json"] == db_module.review_subject("decision_link_review", decision_key="vb:M1:R1", permit_id=p1)
    assert json.loads(a["subject_json"]) == {"decision_key": "vb:M1:R1", "permit_id": p1}
    r = client.get(f"/api/audit/decision_link_review?decision_key=vb:M1:R1&permit_id={p1}")
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == {"source_permit_id": "P1"} and body["subject"]["permit_id"] == p1
    h = body["history"][-1]
    assert h["action"] == "insert" and h["review_author"] == "tester"
    assert h["import"]["acting_user_verified"] is False and h["import"]["kind"] == "links"
    assert "not authenticated" in body["notice"] and body["baseline_note"] is None


@pytest.mark.parametrize("url", [
    "/api/audit/nope?decision_key=x",
    "/api/audit/decision_link_review?decision_key=x",                       # missing permit_id
    "/api/audit/decision_link_review?decision_key=x&permit_id=1&extra=1",    # extra key
    "/api/audit/decision_link_review?decision_key=x&permit_id=abc",          # wrong type
    "/api/audit/parcel_match_review?subject_kind=other&subject_key=1",
    "/api/audit/decision_outcome_review",
])
def test_audit_api_rejects_ambiguous_subjects(reviewed, url):
    assert client.get(url).status_code == 400


def test_parcel_subject_is_compound():
    s = db_module.review_subject("parcel_match_review", subject_kind="decision", subject_key="vb:M1:R1#2")
    assert json.loads(s) == {"subject_key": "vb:M1:R1#2", "subject_kind": "decision"}
    with pytest.raises(ValueError):
        db_module.review_subject("parcel_match_review", subject_key="vb:M1:R1#2")


def test_v7_to_v8_baseline_snapshot_with_null_provenance(reviewed):
    with db_module.db_readonly(reviewed["db"]) as conn:
        n_reviews = conn.execute("SELECT COUNT(*) FROM decision_outcome_review").fetchone()[0]
        decisions = conn.execute("SELECT COUNT(*) FROM land_use_decisions").fetchone()[0]
    conn = _raw(reviewed)  # back to a v7 file: drop the v8 objects, keep the reviews
    for (name, kind) in conn.execute("SELECT name, type FROM sqlite_master WHERE type = 'trigger' AND "
                                     "(name LIKE '%audit%' OR name LIKE 'review_%')").fetchall():
        conn.execute(f'DROP TRIGGER "{name}"')
    for t in ("review_audit", "review_write_context", "review_audit_pending", "review_imports"):
        conn.execute(f"DROP TABLE {t}")
    conn.execute("PRAGMA user_version = 7")
    conn.close()
    db_module.init_db(reviewed["db"])
    rows = _audit(reviewed)
    assert [a["action"] for a in rows] == ["baseline_snapshot"] * n_reviews
    assert all(a["kind"] == "migration_baseline" and a["source_file"] is None and a["source_sha256"] is None
               and a["acting_user"] is None and a["old_json"] is None for a in rows)
    assert json.loads(rows[0]["new_json"])["verified_outcome"] == "approved" and rows[0]["review_author"] == "tester"
    with db_module.db_readonly(reviewed["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM land_use_decisions").fetchone()[0] == decisions
        assert conn.execute("SELECT COUNT(*) FROM review_write_context").fetchone()[0] == 0
    body = client.get("/api/audit/decision_outcome_review?decision_key=vb:M1:R1").json()
    assert body["baseline_note"].startswith("Captured when history tracking began")
    assert review_outcomes(reviewed, [{"decision_key": "vb:M1:R1", "verified_outcome": "denied"}]) == 0  # triggers back


def test_review_parcels_and_provenance_file(reviewed, tmp_path):
    sheet = tmp_path / "checked_sheet.csv"
    sheet.write_text("checked by a person\n")
    with db_module.db_readonly(reviewed["db"]) as conn:
        p1 = pid(conn, "P1")
    derived = tmp_path / "derived.csv"
    with derived.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["decision_key", "permit_id", "verdict", "reviewer", "note"])
        w.writerow(["vb:M1:R1", p1, "rejected", "checker", ""])
    assert review_s.main(["--links", str(derived), "--config", str(reviewed["cfg"]),
                          "--provenance-file", str(sheet), "--actor", "ops"]) == 0
    a = _audit(reviewed, "decision_link_review")[-1]
    assert a["kind"] == "accuracy_apply" and a["source_sha256"] == db_module.file_sha256(sheet)
    assert a["acting_user"] == "ops" and a["review_author"] == "checker"


ROOT = Path(__file__).resolve().parent.parent
import shutil  # noqa: E402
import subprocess  # noqa: E402

node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node not installed")
def test_history_html_escapes_and_separates_author_from_actor():
    body = {"history": [
        {"action": "baseline_snapshot", "recorded_at": "t0", "review_author": "a", "new": {"verdict": "confirmed"},
         "import": {"import_id": 1, "kind": "migration_baseline", "acting_user": None, "source_file": None}},
        {"action": "delete", "recorded_at": "t1", "review_author": "<b>orig</b>", "old": {"verdict": "rejected"},
         "import": {"import_id": 2, "kind": "links", "acting_user": "ops<script>", "source_file": "f.csv",
                    "source_sha256": "abcdef0123456789"}}],
        "baseline_note": "Captured when history tracking began (t0).", "notice": "not authenticated"}
    js = ("const u = require(process.argv[1]); process.stdout.write(u.buildHistoryHtml(JSON.parse(process.argv[2])))")
    out = subprocess.run([node, "-e", js, str(ROOT / "static" / "lead_util.js"), json.dumps(body)],
                         capture_output=True, text=True, check=True).stdout
    assert "<script>" not in out and "<b>orig" not in out and "&lt;b&gt;orig" in out
    assert "author of removed review: &lt;b&gt;orig&lt;/b&gt;" in out and "run by ops&lt;script&gt; (self-reported)" in out
    assert "source file not recorded" in out and "Captured when history tracking began" in out
