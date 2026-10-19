"""ArcGIS ingest regressions found against the real Virginia Beach permit
layer. The record below is copied from a fixture recorded from the live layer
(alexisn1989/permitiq: tests/fixtures/virginia_beach_page1.json): its date
fields are TEXT ("YYYY/MM/DD"), not esriFieldTypeDate epoch values."""
from __future__ import annotations

from datetime import date

import ingest_permits as ip
import ingest_violations as iv

VB_FEATURE = {"attributes": {
    "PermitNumber": "2023-ELER-00001", "PermitType": "Electrical", "ApplicationDate": "2023/01/01",
    "IssueDate": "2023/01/03", "FinalDate": "2023/04/27", "Status": "Closed",
    "WorkDesc": "We are applying to build our 2018 code approved Duplex 25 building plans",
    "StreetAddress": "110 58TH ST", "AddressUnit": "", "City": "Virginia Beach", "State": "VA", "Zip": "23451",
    "GPIN": "24198122130000", "ConstructionType": "Wood Frame", "WorkType": "New", "CreatedBy": "portalUser1",
    "OBJECTID": 1}}
STRING = "esriFieldTypeString"
META_TEXT_DATES = {"fields": [{"name": k, "type": STRING} for k in VB_FEATURE["attributes"]]}


def test_vb_text_dates_and_default_fields_resolve():
    fields = ip.resolve_fields(META_TEXT_DATES, {})
    assert fields["permit_type"] == "PermitType" and fields["address"] == "StreetAddress"
    row = ip.build_row(VB_FEATURE, fields, "OBJECTID")
    assert (row["application_date"], row["issue_date"], row["final_date"]) == (
        date(2023, 1, 1), date(2023, 1, 3), date(2023, 4, 27))
    assert row["bad_dates"] == 0 and row["permit_type"] == "Electrical" and row["address"] == "110 58TH ST"


def test_esri_to_date_formats():
    assert ip.esri_to_date("2023/01/01") == date(2023, 1, 1)
    assert ip.esri_to_date("2023/1/5 00:00:00") == date(2023, 1, 5)
    assert ip.esri_to_date(1672531200000) == date(2023, 1, 1)
    assert ip.esri_to_date("2023-01-01") == date(2023, 1, 1)
    assert ip.esri_to_date("2023/02/30") is None and ip.esri_to_date("01/05/2023") is None


def test_days_filter_never_compares_timestamps_against_text_fields():
    fields = ip.resolve_fields(META_TEXT_DATES, {})
    where, client_since = ip.since_filter(META_TEXT_DATES, fields, ip.DATE_FIELDS, date(2023, 1, 2))
    assert where == "1=1" and client_since == date(2023, 1, 2)  # filtered locally instead
    row = ip.build_row(VB_FEATURE, fields, "OBJECTID")
    assert ip.in_window(row, ip.DATE_FIELDS, date(2023, 4, 1))      # final date inside window
    assert not ip.in_window(row, ip.DATE_FIELDS, date(2023, 5, 1))  # every date before window


def test_days_filter_stays_server_side_for_date_typed_fields():
    meta = {"fields": [{"name": "PermitNumber", "type": STRING}, {"name": "ApplicationDate", "type": "esriFieldTypeDate"},
                       {"name": "IssueDate", "type": "esriFieldTypeDate"}, {"name": "FinalDate", "type": "esriFieldTypeDate"}]}
    where, client_since = ip.since_filter(meta, ip.resolve_fields(meta, {}), ip.DATE_FIELDS, date(2026, 10, 1))
    assert client_since is None and "ApplicationDate >= TIMESTAMP '2026-10-01 00:00:00'" in where


def test_violations_share_the_same_window_logic():
    assert iv.since_filter is ip.since_filter and iv.in_window is ip.in_window
