"""Database tests for migration 007 and ingest_planning.py identity/events.

Inputs are synthetic agenda text run through the scanner's real verification
and identity code (scan_planning_gemini.build_leads); Gemini is not called.
Skipped unless PERMITIQ_TEST_ADMIN_DSN is set (see conftest.py)."""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter

import psycopg
import pytest

import ingest_planning as ip
import scan_planning_gemini as scan
from conftest import (INPUT_ID, LEGACY_ADDRESS, LEGACY_APPLICANT, LEGACY_ID, MIGRATION_007, ROOT, dsn_for, q,
                      run_sql_file)
from permitiq import planning_identity as pid

VIEW_COLUMNS_006 = ["application_id", "project_name", "address", "application_type", "proposed_use",
                    "hearing_date", "status", "latitude", "longitude", "geocode_source", "fld_zone",
                    "zone_subty", "sfha_tf", "flood_match", "ingested_at", "source_excerpt"]


def agenda(meeting_date, items, source=None):
    """items: dicts with applicant, address, section, gpin?, case?. Returns
    scanner-built records exactly as scan_planning_gemini would write them."""
    lines, entries = [f"PLANNING COMMISSION AGENDA {meeting_date}"], []
    for it in items:
        ex = " ".join(filter(None, [it["applicant"], it["address"], it.get("gpin") and f"GPIN {it['gpin']}",
                                    it.get("case") and f"Case No. {it['case']}", "Council District 4"]))
        lines += [it["section"].upper(), ex]
        entries.append({f: None for f in scan.AgendaEntry.model_fields} | {
            "section": it["section"], "applicant": it["applicant"], "address": it["address"],
            "gpin": it.get("gpin"), "case_number": it.get("case"), "source_excerpt": ex})
    meeting = {"meeting_id": None, "meeting_date": meeting_date, "agenda_url": None,
               "source_document": source or f"doc:{meeting_date}"}
    leads, dropped = scan.build_leads(entries, "\n".join(lines), meeting, Counter())
    assert not dropped, dropped
    return leads


def counts(dsn):
    return q(dsn, "SELECT (SELECT count(*) FROM planning_applications WHERE application_id NOT IN (%s, %s)), "
                  "(SELECT count(*) FROM planning_occurrences), (SELECT count(*) FROM planning_status_events)",
             (LEGACY_ID, INPUT_ID))[0]


ACME = {"applicant": "Acme Builders LLC", "address": "100 Shore Drive", "section": "Consent Agenda"}


# ── migration ────────────────────────────────────────────────────────────────

def test_migration_007_on_existing_005_006_data(fresh_pre_007_db):
    name = fresh_pre_007_db
    dsn = dsn_for(name)
    before = q(dsn, "SELECT application_id, project_name, status, hearing_date FROM planning_applications ORDER BY 1")
    run_sql_file(name, MIGRATION_007)
    run_sql_file(name, MIGRATION_007)  # idempotent
    assert q(dsn, "SELECT application_id, project_name, status, hearing_date FROM planning_applications "
                  "ORDER BY 1") == before  # existing IDs and data preserved
    assert dict(q(dsn, "SELECT application_id, identity_basis FROM planning_applications")) == {
        LEGACY_ID: "legacy_id", INPUT_ID: "input_id"}
    cols = [r[0] for r in q(dsn, "SELECT column_name FROM information_schema.columns "
                                 "WHERE table_name = 'vw_planning_leads' ORDER BY ordinal_position")]
    assert cols == VIEW_COLUMNS_006 + ["gpin"]
    assert len(q(dsn, "SELECT * FROM vw_planning_leads")) == 2  # same rows as before
    fk_type = q(dsn, "SELECT data_type FROM information_schema.columns WHERE table_name = 'planning_status_events' "
                     "AND column_name = 'application_id'")[0][0]
    key_type = q(dsn, "SELECT data_type FROM information_schema.columns WHERE table_name = 'planning_applications' "
                      "AND column_name = 'application_id'")[0][0]
    assert fk_type == key_type == "text"
    gpin_types = q(dsn, "SELECT table_name, data_type FROM information_schema.columns WHERE column_name = 'gpin' "
                        "AND table_name IN ('planning_applications', 'planning_applications_raw') ORDER BY 1")
    assert gpin_types == [("planning_applications", "character varying"),
                          ("planning_applications_raw", "character varying")]


def _as_reader(dsn, sql):
    with psycopg.connect(dsn) as c:
        c.execute("SET ROLE powerbi_reader")
        return c.execute(sql).fetchall()


def test_powerbi_reader_access(db):
    from psycopg.conninfo import conninfo_to_dict
    assert _as_reader(db, "SELECT gpin FROM vw_planning_leads") is not None
    assert _as_reader(db, "SELECT event_id, status_raw FROM planning_status_events") is not None
    for table in ("planning_applications", "planning_occurrences", "planning_identity_review",
                  "planning_identity_links"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _as_reader(db, f"SELECT 1 FROM {table}")
    # re-running 003 (which revokes everything first) keeps the event grant
    run_sql_file(conninfo_to_dict(db)["dbname"], ROOT / "db" / "003_roles.sql")
    assert _as_reader(db, "SELECT count(*) FROM planning_status_events") == [(0,)]


# ── idempotency and concurrency ──────────────────────────────────────────────

def test_reingesting_the_same_occurrence(db, ingest):
    recs = agenda("2026-11-12", [ACME])
    assert ingest(db, recs, "--max-geocode", "0") == 0
    first = q(db, "SELECT application_id, event_key FROM planning_status_events")
    assert ingest(db, recs, "--max-geocode", "0") == 0
    assert ingest(db, agenda("2026-11-12", [ACME]), "--max-geocode", "0") == 0  # re-scan of same agenda
    assert counts(db) == (1, 1, 1)
    assert q(db, "SELECT application_id, event_key FROM planning_status_events") == first


def test_concurrent_ingestion_does_not_duplicate(db, tmp_path):
    items = [{"applicant": f"Builder {i} LLC", "address": f"{100 + i} Shore Drive", "section": "Consent Agenda"}
             for i in range(40)]
    f = tmp_path / "leads.json"
    f.write_text(json.dumps(agenda("2026-11-12", items)))
    cmd = [sys.executable, str(ROOT / "ingest_planning.py"), "--file", str(f), "--dsn", db, "--max-geocode", "0"]
    procs = [subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(3)]
    results = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [r[1] for r in results]
    assert counts(db) == (40, 40, 40)


# ── identity ─────────────────────────────────────────────────────────────────

def test_separate_applications_on_one_parcel(db, ingest):
    recs = agenda("2026-11-12", [
        {"applicant": "Acme Builders LLC", "address": "100 Shore Drive", "gpin": "1477-12-3456", "section": "Consent Agenda"},
        {"applicant": "Bay Church", "address": "100 Shore Drive", "gpin": "1477-12-3456", "section": "Consent Agenda"},
        {"applicant": "Shore LP", "address": "200 Shore Drive", "gpin": "1477-99-0001", "case": "2026-PC-0042",
         "section": "Planning Commission Items"},
        {"applicant": "Shore LP", "address": "200 Shore Drive", "gpin": "1477-99-0001", "case": "2026-PC-0043",
         "section": "Planning Commission Items"},
    ])
    assert ingest(db, recs, "--max-geocode", "0") == 0
    assert counts(db) == (4, 4, 4)
    flags = q(db, "SELECT reason, count(*) FROM planning_identity_review GROUP BY 1 ORDER BY 1")
    assert ("same_gpin", 2) in flags  # flagged both pairs ...
    assert q(db, "SELECT count(*) FROM planning_identity_review WHERE resolution IS NOT NULL") == [(0,)]  # ... merged none


def test_official_case_number_joins_meetings(db, ingest):
    case = {**ACME, "case": "2026-PC-0042"}
    assert ingest(db, agenda("2026-11-12", [case]), "--max-geocode", "0") == 0
    assert ingest(db, agenda("2026-12-10", [{**case, "section": "Planning Commission Items"}]),
                  "--max-geocode", "0") == 0
    assert counts(db) == (1, 2, 2)
    app = pid.case_application_id("2026-PC-0042")
    assert q(db, "SELECT hearing_date::text, status, identity_basis FROM planning_applications "
                 "WHERE application_id = %s", (app,)) == [("2026-12-10", "Planning Commission Items",
                                                          "official_case_id")]


def test_confirmed_link_joins_meetings(db, ingest):
    nov, dec = agenda("2026-11-12", [ACME]), agenda("2026-12-10", [ACME])
    assert ingest(db, nov + dec, "--max-geocode", "0") == 0
    assert counts(db) == (2, 2, 2)  # no case number -> separate, flagged
    assert q(db, "SELECT reason FROM planning_identity_review") == [("same_applicant_address",)]
    nov_app, dec_occ = nov[0]["application_id"], dec[0]["occurrence_key"]

    with psycopg.connect(db) as c:
        ip.confirm_link(c, dec_occ, nov_app, "reviewer@example", "same deferred case")
    assert q(db, "SELECT application_id FROM vw_planning_leads WHERE project_name = %s",
             (ACME["applicant"],)) == [(nov_app,)]
    assert q(db, "SELECT count(*), count(DISTINCT meeting_date) FROM planning_status_events "
                 "WHERE application_id = %s", (nov_app,)) == [(2, 2)]
    assert q(db, "SELECT hearing_date::text FROM planning_applications WHERE application_id = %s",
             (nov_app,)) == [("2026-12-10",)]  # took the newer occurrence's state
    assert q(db, "SELECT superseded_by FROM planning_applications WHERE application_id = %s",
             (dec[0]["application_id"],)) == [(nov_app,)]  # kept, hidden

    assert ingest(db, dec, "--max-geocode", "0") == 0  # re-ingest after linking: stays linked, no dupes
    assert counts(db) == (2, 2, 2)

    jan = agenda("2027-01-13", [ACME])  # link confirmed BEFORE ingestion
    with psycopg.connect(db) as c:
        ip.confirm_link(c, jan[0]["occurrence_key"], nov_app, "reviewer@example", None)
    assert ingest(db, jan, "--max-geocode", "0") == 0
    assert q(db, "SELECT count(*) FROM planning_status_events WHERE application_id = %s", (nov_app,)) == [(3,)]
    assert counts(db) == (2, 3, 3)  # no new application


def test_ambiguous_cases_remain_separate(db, ingest):
    a = {**ACME, "gpin": "1477-12-3456"}
    assert ingest(db, agenda("2026-11-12", [a]) + agenda("2026-12-10", [a]), "--max-geocode", "0") == 0
    assert len(q(db, "SELECT 1 FROM vw_planning_leads WHERE project_name = %s", (ACME["applicant"],))) == 2
    assert sorted(r[0] for r in q(db, "SELECT reason FROM planning_identity_review")) == [
        "same_applicant_address", "same_gpin"]
    assert q(db, "SELECT count(*) FROM planning_identity_links") == [(0,)]


# ── GPIN ─────────────────────────────────────────────────────────────────────

def test_gpin_leading_zeros_and_no_erasure(db, ingest):
    case = {**ACME, "case": "2026-PC-0042"}
    app = pid.case_application_id("2026-PC-0042")
    get = lambda: q(db, "SELECT a.gpin, r.gpin FROM planning_applications a JOIN planning_applications_raw r "  # noqa: E731
                        "USING (application_id) WHERE application_id = %s", (app,))[0]
    assert ingest(db, agenda("2026-11-12", [case]), "--max-geocode", "0") == 0
    assert get() == (None, None)  # missing GPIN
    assert ingest(db, agenda("2026-12-10", [{**case, "gpin": "0012-34-5678"}]), "--max-geocode", "0") == 0
    assert get() == ("0012-34-5678", "0012-34-5678")  # leading zeros kept
    assert ingest(db, agenda("2027-01-13", [case]), "--max-geocode", "0") == 0
    assert get() == ("0012-34-5678", "0012-34-5678")  # later omission doesn't erase


# ── events ───────────────────────────────────────────────────────────────────

def test_events_effective_date_and_status_ledger(db, ingest):
    case = {**ACME, "case": "2026-PC-0042"}
    assert ingest(db, agenda("2026-11-12", [case]) + agenda("2026-12-10", [case]), "--max-geocode", "0") == 0
    rows = q(db, "SELECT meeting_date::text, status_raw, status_normalized, effective_date FROM "
                 "planning_status_events ORDER BY meeting_date")
    # unchanged status at two meetings = two observations; listed != approved; no effective date
    assert rows == [("2026-11-12", "Consent Agenda", "scheduled", None),
                    ("2026-12-10", "Consent Agenda", "scheduled", None)]
    wd = agenda("2027-01-13", [{**case, "section": "Withdrawals & Deferrals"}])
    explicit = [{**wd[0], "status": "Approved", "effective_date": "2027-01-20",
                 "occurrence_key": wd[0]["occurrence_key"] + ":minutes", "source_document": "minutes:2027-01-13"}]
    assert ingest(db, wd + explicit, "--max-geocode", "0") == 0
    assert q(db, "SELECT status_raw, status_normalized, effective_date::text FROM planning_status_events "
                 "WHERE meeting_date = '2027-01-13' ORDER BY event_id") == [
        ("Withdrawals & Deferrals", None, None), ("Approved", "approved", "2027-01-20")]


def test_older_agenda_after_newer(db, ingest):
    case = {**ACME, "case": "2026-PC-0042"}
    app = pid.case_application_id("2026-PC-0042")
    assert ingest(db, agenda("2026-12-10", [{**case, "section": "Planning Commission Items"}])) == 0
    calls_before = len(ingest.geocode_calls)
    assert ingest(db, agenda("2026-11-12", [{**case, "address": "999 Old Road", "gpin": "1477-12-3456"}])) == 0
    assert q(db, "SELECT hearing_date::text, status, address, gpin FROM planning_applications "
                 "WHERE application_id = %s", (app,)) == [
        ("2026-12-10", "Planning Commission Items", ACME["address"], "1477-12-3456")]  # current state kept; GPIN filled
    assert counts(db)[2] == 2  # the older observation is still in the ledger
    assert len(ingest.geocode_calls) == calls_before  # older record not geocoded


def test_failed_batch_writes_nothing(db, ingest, monkeypatch):
    recs = agenda("2026-11-12", [ACME, {**ACME, "applicant": "Second Co", "address": "2 Shore Drive"}])
    real = pid.normalize_status
    calls = iter([real, lambda s: "not-a-valid-status"])  # 2nd record violates the CHECK
    monkeypatch.setattr(ip.pid, "normalize_status", lambda s: next(calls)(s))
    assert ingest(db, recs, "--max-geocode", "0") == 1
    assert counts(db) == (0, 0, 0)  # first record's application/occurrence/event rolled back too
    assert q(db, "SELECT count(*) FROM planning_applications_raw WHERE application_id NOT IN (%s, %s)",
             (LEGACY_ID, INPUT_ID)) == [(0,)]


# ── existing IDs ─────────────────────────────────────────────────────────────

def test_legacy_ids_are_preserved_and_ambiguity_flagged(db, ingest):
    legacy_rec = {"application_id": LEGACY_ID, "project_name": LEGACY_APPLICANT, "address": LEGACY_ADDRESS,
                  "application_type": None, "proposed_use": None, "status": "Consent Agenda"}
    before = q(db, "SELECT * FROM planning_applications WHERE application_id = %s", (LEGACY_ID,))
    assert ingest(db, [{**legacy_rec, "hearing_date": "2026-09-09"}], "--max-geocode", "0") == 0
    assert q(db, "SELECT application_id FROM planning_occurrences") == [(LEGACY_ID,)]  # same meeting -> same record
    assert ingest(db, [{**legacy_rec, "hearing_date": "2026-10-14", "status": "Withdrawals & Deferrals"}],
                  "--max-geocode", "0") == 0
    split = pid.legacy_split_application_id(LEGACY_ID, "2026-10-14")
    after = q(db, "SELECT * FROM planning_applications WHERE application_id = %s", (LEGACY_ID,))
    assert [r[:8] for r in after] == [r[:8] for r in before]  # legacy row's data not overwritten
    assert q(db, "SELECT status FROM planning_applications WHERE application_id = %s", (split,)) == [
        ("Withdrawals & Deferrals",)]
    assert ("legacy_hash_match",) in q(db, "SELECT reason FROM planning_identity_review")
    # a new-format scanner record for the same applicant+address is flagged, not merged
    assert ingest(db, agenda("2026-11-12", [{"applicant": LEGACY_APPLICANT, "address": LEGACY_ADDRESS,
                                             "section": "Consent Agenda"}]), "--max-geocode", "0") == 0
    assert q(db, "SELECT count(*) FROM planning_identity_review WHERE reason = 'legacy_hash_match'") == [(2,)]
    assert q(db, "SELECT status, hearing_date::text FROM planning_applications WHERE application_id = %s",
             (LEGACY_ID,)) == [("Consent Agenda", "2026-09-09")]
    # a non-legacy producer ID keeps old behavior
    assert ingest(db, [{**legacy_rec, "application_id": INPUT_ID, "hearing_date": "2026-11-01"}],
                  "--max-geocode", "0") == 0
    assert q(db, "SELECT hearing_date::text FROM planning_applications WHERE application_id = %s",
             (INPUT_ID,)) == [("2026-11-01",)]


def test_api_returns_gpin_without_route_changes(db, ingest, monkeypatch):
    from fastapi.testclient import TestClient
    import permitiq.api as api

    assert ingest(db, agenda("2026-11-12", [{**ACME, "gpin": "0012-34-5678"}]), "--max-geocode", "0") == 0
    monkeypatch.setenv("DATABASE_URL", db)
    rows = TestClient(api.app).get("/api/planning/leads").json()
    assert {r["application_id"] for r in rows} >= {LEGACY_ID, INPUT_ID}
    assert [r["gpin"] for r in rows if r["project_name"] == ACME["applicant"]] == ["0012-34-5678"]
