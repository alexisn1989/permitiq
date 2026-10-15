"""Tests for scripts/check_sources.py -- fake fetch, no network."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("check_sources", ROOT / "scripts" / "check_sources.py")
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


@pytest.mark.parametrize("name,expected", [
    ("contractor", "name"), ("applicant_name", "name"), ("Contractor", "name"),
    ("permit_project_cost", "cost"), ("valuation", "cost"), ("EstimatedValue", "cost"),
    ("permit_total_fee", "fee"), ("total_balance", "fee"),
    ("permit_number", None), ("address", None),
])
def test_classify_field(name, expected):
    assert cs.classify_field(name) == expected


def test_fill_rate_treats_zero_and_blank_as_empty():
    rows = [{"a": "x"}, {"a": ""}, {"a": "0.0"}, {}]
    assert cs.fill_rate(rows, "a") == 0.25
    assert cs.fill_rate([], "a") is None


def _socrata_fetch(calls):
    meta = {
        "name": "Permits - Right of Way",
        "description": "ROW permits",
        "rowsUpdatedAt": 1760000000,
        "license": {"name": "Public Domain", "termsLink": "https://example.org/terms"},
        "attribution": "City of Norfolk",
        "columns": [
            {"fieldName": "permit_number", "dataTypeName": "text", "name": "Permit Number"},
            {"fieldName": "permit_kind", "dataTypeName": "text", "name": "Permit Kind"},
            {"fieldName": "contractor", "dataTypeName": "text", "name": "Contractor"},
            {"fieldName": "fee", "dataTypeName": "number", "name": "Fee"},
            {"fieldName": ":id", "dataTypeName": "meta_data", "name": "id"},
        ],
    }
    rows = [{"permit_number": "1", "permit_kind": "Dumpster", "contractor": "ABC LLC"},
            {"permit_number": "2", "permit_kind": "Crane"}]

    def fetch(url, params=None, **kw):
        calls.append((url, params))
        if url.endswith(".json") and "/api/views/" in url:
            return meta
        if params and "$group" in params:
            return [{"permit_kind": "Dumpster", "n": "12"}, {"permit_kind": "Crane", "n": "3"}]
        return rows
    return fetch


def test_check_socrata_reports_fields_flags_and_distinct():
    calls = []
    res = cs.check_source("norfolk_row", fetch=_socrata_fetch(calls))
    assert res["ok"] is True
    assert res["license"] == "Public Domain"
    assert res["rows_updated_at"].startswith("2025-10-09")
    by_name = {f["name"]: f for f in res["fields"]}
    assert ":id" not in by_name  # system columns hidden
    assert by_name["contractor"]["flag"] == "name"
    assert by_name["contractor"]["sample_fill"] == 0.5
    assert by_name["fee"]["flag"] == "fee"
    # category column auto-detected from live schema, grouped server-side
    assert res["distinct"]["permit_kind"] == [("Dumpster", 12), ("Crane", 3)]
    assert any("mi4h-8pn3" in url for url, _ in calls)


def test_check_arcgis_reports_missing_distinct_field():
    def fetch(url, params=None, **kw):
        if params == {"f": "json"}:
            return {"name": "Building_Permits_Applications_view", "maxRecordCount": 2000,
                    "editingInfo": {"lastEditDate": 1760000000000},
                    "fields": [{"name": "PermitNumber", "type": "esriFieldTypeString"},
                               {"name": "PermitType", "type": "esriFieldTypeString"},
                               {"name": "ContractorName", "type": "esriFieldTypeString"}]}
        if params.get("returnDistinctValues"):
            return {"features": [{"attributes": {"PermitType": "Building"}}]}
        return {"features": [{"attributes": {"PermitNumber": "1", "ContractorName": "X Roofing"}}]}

    res = cs.check_source("virginia_beach_permits", fetch=fetch)
    assert res["ok"] is True
    assert res["max_record_count"] == 2000
    assert {f["name"]: f["flag"] for f in res["fields"]}["ContractorName"] == "name"
    assert res["distinct"]["PermitType"] == [("Building", None)]
    assert res["distinct"]["WorkType"] == "FIELD NOT IN LIVE SCHEMA"


def test_failure_is_reported_not_raised():
    def boom(url, params=None, **kw):
        raise RuntimeError("connect refused")
    res = cs.check_source("norfolk_permits", fetch=boom)
    assert res["ok"] is False and "connect refused" in res["error"]
    assert "FAILED" in cs.render(res)


def test_main_writes_json(tmp_path, capsys):
    out = tmp_path / "check.json"
    cs.main(["--source", "norfolk_row", "--json", str(out)], fetch=_socrata_fetch([]))
    assert '"norfolk_row"' in out.read_text()
    assert "<-- NAME" in capsys.readouterr().out
