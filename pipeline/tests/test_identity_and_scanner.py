"""Unit tests: identity rules, scanner evidence checks, ingest validation.
No database, no network, no Gemini -- all inputs are synthetic."""
from __future__ import annotations

import hashlib
import json
from collections import Counter

import pytest

import ingest_planning as ip
import scan_planning_gemini as scan
from permitiq import planning_identity as pid

MEETING = {"meeting_id": 1790, "meeting_date": "2026-11-12", "agenda_url": "https://example/agenda.pdf",
           "source_document": "https://example/agenda.pdf"}


def entry(**k):
    base = {f: None for f in scan.AgendaEntry.model_fields}
    base.update(k)
    return base


def build(entries, pdf_text, meeting=MEETING):
    stats = Counter()
    leads, dropped = scan.build_leads(entries, pdf_text, meeting, stats)
    return leads, dropped, stats


# ── identity primitives ──────────────────────────────────────────────────────

def test_serialization_is_unambiguous():
    # naive concatenation collides; the JSON array encoding does not
    assert "ab" + "c" == "a" + "bc"
    assert pid.digest(["v", "ab", "c"]) != pid.digest(["v", "a", "bc"])
    assert pid.digest(["v", None]) != pid.digest(["v", "None"]) != pid.digest(["v", ""])


def test_keys_are_deterministic_and_time_free():
    k1 = pid.occurrence_key("2026-11-12", "Acme LLC", "1 Main St")
    assert k1 == pid.occurrence_key("2026-11-12", " ACME, llc ", "1  main st.")  # formatting-insensitive
    assert k1 != pid.occurrence_key("2026-12-10", "Acme LLC", "1 Main St")   # another meeting
    e1 = pid.event_key(k1, "doc", "Consent Agenda")
    assert e1 == pid.event_key(k1, "doc", "consent  agenda")
    assert e1 != pid.event_key(k1, "doc", "Withdrawals & Deferrals")
    assert e1 != pid.event_key(k1, "doc-amended", "Consent Agenda")


def test_legacy_formula_reproduces_pre_007_ids():
    def old(applicant, address):  # verbatim from the pre-007 scanner
        import re
        n = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
        return "VBPC-" + hashlib.sha1(f"{n(applicant)}|{n(address or '')}".encode()).hexdigest()[:12]
    for a, b in [("Oceanfront Holdings, LLC", "2401 Atlantic Avenue"), ("X", None)]:
        assert pid.legacy_hash_id(a, b) == old(a, b)
        assert pid.LEGACY_ID_RE.match(pid.legacy_hash_id(a, b))


def test_case_ids_are_scoped_and_normalized():
    assert pid.case_application_id("2026-bza-0012") == pid.case_application_id(" 2026-BZA-0012 ")
    assert pid.case_application_id("2026-BZA-0012") != pid.case_application_id("2026-BZA-0013")
    assert pid.case_application_id("2026-BZA-0012").startswith("VBCASE-")


@pytest.mark.parametrize("raw,expected", [
    ("Consent Agenda", "scheduled"),                 # listed, NOT approved
    ("Planning Commission Items", "scheduled"),
    ("Withdrawals & Deferrals", None),               # names both -> unknown
    ("Deferred", "deferred"), ("Deferrals", "deferred"),
    ("Withdrawn", "withdrawn"), ("Withdrawals", "withdrawn"),
    ("Approved", "approved"), ("Denied", "denied"),
    ("Approved as amended", None),                   # not an exact known wording
    ("Pending", None), ("Proposed", None), ("", None), (None, None),
])
def test_status_normalization_is_conservative(raw, expected):
    assert pid.normalize_status(raw) == expected
    assert expected is None or expected in pid.STATUS_VALUES


# ── scanner: schema and evidence ─────────────────────────────────────────────

def test_new_schema_fields_are_required_but_nullable():
    schema = scan.AgendaEntry.model_json_schema()
    for f in ("gpin", "case_number", "agenda_item"):
        assert f in schema["required"]
        assert {"type": "null"} in schema["properties"][f]["anyOf"]
        assert "default" not in schema["properties"][f]  # SDK-compatible: no defaults


PDF = """PLANNING COMMISSION  November 12, 2026
III. CONSENT AGENDA
1. Lighthouse Development, LLC  125 Ash Avenue  GPIN 0123-45-6789  Council District 4
2. Bayside Church of God  4500 Shore Drive  Council District 4  Conditional Use Permit
3. Shore Partners  2401 Atlantic Avenue  GPIN: 2417-95-5832  Case No. 2026-PC-0042
4. Shore Partners  2401 Atlantic Avenue  GPIN: 2417-95-5832  Case No. 2026-PC-0043
"""
E1 = "1. Lighthouse Development, LLC 125 Ash Avenue GPIN 0123-45-6789 Council District 4"
E2 = "Bayside Church of God 4500 Shore Drive Council District 4 Conditional Use Permit"
E3 = "3. Shore Partners 2401 Atlantic Avenue GPIN: 2417-95-5832 Case No. 2026-PC-0042"
E4 = "4. Shore Partners 2401 Atlantic Avenue GPIN: 2417-95-5832 Case No. 2026-PC-0043"


def test_gpin_kept_as_printed_text_with_leading_zeros():
    leads, _, _ = build([entry(applicant="Lighthouse Development, LLC", address="125 Ash Avenue",
                               gpin="0123-45-6789", agenda_item="1", source_excerpt=E1)], PDF)
    assert leads[0]["gpin"] == "0123-45-6789"
    assert leads[0]["agenda_item"] == "1"


def test_missing_gpin_stays_null():
    leads, _, stats = build([entry(applicant="Bayside Church of God", address="4500 Shore Drive",
                                   source_excerpt=E2)], PDF)
    assert leads[0]["gpin"] is None and not any(k.startswith("gpin_") for k in stats)


@pytest.mark.parametrize("applicant,address,excerpt,gpin,reason", [
    # labelled entry, but this value is not what it prints
    ("Lighthouse Development, LLC", "125 Ash Avenue", E1, "9999-99-9999", "gpin_not_in_excerpt_set_null"),
    # entry prints no GPIN label at all; "4500" is the street number
    ("Bayside Church of God", "4500 Shore Drive", E2, "4500", "gpin_no_label_in_excerpt_set_null"),
    # labelled, printed, but too short to be a parcel number
    ("Lighthouse Development, LLC", "125 Ash Avenue", E1, "0123", "gpin_too_few_digits_set_null"),
])
def test_unsupported_gpin_is_nulled(applicant, address, excerpt, gpin, reason):
    leads, _, stats = build([entry(applicant=applicant, address=address, gpin=gpin, source_excerpt=excerpt)], PDF)
    assert leads[0]["gpin"] is None and stats[reason] == 1


def test_gpin_inferred_from_address_is_nulled():
    pdf = "CONSENT AGENDA\n1. Acme LLC 1234567 Shore Drive GPIN pending Council District 4\n"
    ex = "Acme LLC 1234567 Shore Drive GPIN pending Council District 4"
    leads, _, stats = build([entry(applicant="Acme LLC", address="1234567 Shore Drive", gpin="1234567",
                                   source_excerpt=ex)], pdf)
    assert leads[0]["gpin"] is None and stats["gpin_from_address_set_null"] == 1


def test_existing_checks_still_apply():
    leads, dropped, stats = build([
        entry(applicant="Phantom LLC", address="1 Nowhere", source_excerpt="Phantom LLC 1 Nowhere Lane District 9"),
        entry(applicant="Bayside Church of God", address="4500 Shore Drive", application_type="Rezoning",
              proposed_use="Townhomes", source_excerpt=E2),
        entry(applicant="Wrong Name", address="4500 Shore Drive", source_excerpt=E2),
    ], PDF)
    assert stats["dropped_excerpt_not_in_pdf"] == 1 and stats["dropped_applicant_not_in_excerpt"] == 1
    assert len(leads) == 1 and leads[0]["application_type"] is None and leads[0]["proposed_use"] is None


def test_case_number_and_agenda_item_need_evidence():
    leads, _, stats = build([entry(applicant="Bayside Church of God", address="4500 Shore Drive",
                                   case_number="2026-PC-9999", agenda_item="7", source_excerpt=E2)], PDF)
    assert leads[0]["case_number"] is None and leads[0]["agenda_item"] is None
    assert stats["case_number_not_in_excerpt_set_null"] == 1 and stats["agenda_item_not_in_excerpt_set_null"] == 1


def test_two_cases_on_one_parcel_at_one_meeting_stay_separate():
    leads, _, stats = build([
        entry(applicant="Shore Partners", address="2401 Atlantic Avenue", gpin="2417-95-5832",
              case_number="2026-PC-0042", source_excerpt=E3),
        entry(applicant="Shore Partners", address="2401 Atlantic Avenue", gpin="2417-95-5832",
              case_number="2026-PC-0043", source_excerpt=E4),
    ], PDF)
    assert len({l["occurrence_key"] for l in leads}) == 2
    assert {l["application_id"] for l in leads} == {pid.case_application_id("2026-PC-0042"),
                                                    pid.case_application_id("2026-PC-0043")}
    assert stats["occurrence_key_disambiguated"] == 2


def test_same_entry_rescanned_gets_same_keys_and_new_meeting_gets_new_ones():
    e = [entry(applicant="Bayside Church of God", address="4500 Shore Drive", source_excerpt=E2)]
    a, b = build(e, PDF)[0][0], build(e, PDF)[0][0]
    assert (a["occurrence_key"], a["application_id"]) == (b["occurrence_key"], b["application_id"])
    later = build(e, PDF, {**MEETING, "meeting_date": "2026-12-10"})[0][0]
    assert later["occurrence_key"] != a["occurrence_key"] and later["application_id"] != a["application_id"]


def test_file_merge_keeps_one_record_per_occurrence(tmp_path):
    e = [entry(applicant="Bayside Church of God", address="4500 Shore Drive", source_excerpt=E2)]
    out = tmp_path / "leads.json"
    scan.merge_into_file(out, build(e, PDF)[0], overwrite=False)
    scan.merge_into_file(out, build(e, PDF, {**MEETING, "meeting_date": "2026-12-10"})[0], overwrite=False)
    scan.merge_into_file(out, build(e, PDF)[0], overwrite=False)  # re-scan of the first meeting
    recs = json.loads(out.read_text())
    assert sorted(r["meeting_date"] for r in recs) == ["2026-11-12", "2026-12-10"]


# ── ingest validation ────────────────────────────────────────────────────────

BASE = {"application_id": "X-1", "project_name": "A", "address": None, "application_type": None,
        "proposed_use": None, "hearing_date": "2026-11-12", "status": "Consent Agenda"}


def test_ingest_rejects_non_text_gpin_and_bad_effective_date():
    _, errors, _ = ip.validate([{**BASE, "gpin": 123456789}, {**BASE, "effective_date": "11/12/2026"}])
    assert any("gpin must be a string" in e for e in errors)
    assert any("effective_date" in e for e in errors)


def test_ingest_accepts_pre_007_files_and_keeps_gpin_text():
    rows, errors, _ = ip.validate([BASE, {**BASE, "gpin": "0012-34-5678"}])
    assert not errors
    assert rows[0]["gpin"] is None and rows[0]["effective_date"] is None and rows[0]["occurrence_key"] is None
    assert rows[1]["gpin"] == "0012-34-5678"
    assert str(rows[0]["meeting_date"]) == "2026-11-12"  # defaults to hearing_date
