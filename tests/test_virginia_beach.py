"""Phase 4 tests: Virginia Beach adapter -- fixture-based, no live API calls."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingest.virginia_beach as vb  # noqa: E402
from ingest.common import SourceSchemaError  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def page1():
    return load_fixture("virginia_beach_page1.json")


def test_fetch_maps_confirmed_fields(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["resultOffset"] == 0 else {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)

    permits = list(vb.fetch())
    by_id = {p.source_permit_id: p for p in permits}

    p = by_id["2023-ELER-00001"]
    assert p.locality == "virginia_beach"
    assert p.address == "110 58TH ST, Virginia Beach, VA 23451"
    assert p.permit_type == "Electrical"
    assert p.status == "Closed"
    assert p.status_bucket == "finaled_closed"
    assert p.applied_date == "2023-01-01"
    assert p.issued_date == "2023-01-03"
    assert p.finaled_date == "2023-04-27"
    assert p.valuation is None
    assert p.contractor is None
    assert p.applicant is None
    assert p.lat is None and p.lng is None
    assert p.raw["GPIN"] == "24198122130000"  # unmapped field preserved in raw_json


def test_address_includes_unit_when_present(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["resultOffset"] == 0 else {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    by_id = {p.source_permit_id: p for p in vb.fetch()}
    assert by_id["2025-PLBR-00001"].address == "500 LASKIN RD APT 3, Virginia Beach, VA 23451"


def test_address_no_double_space_when_unit_empty(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["resultOffset"] == 0 else {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    by_id = {p.source_permit_id: p for p in vb.fetch()}
    addr = by_id["2023-ELER-00001"].address
    assert "  " not in addr
    assert "None" not in addr


def test_status_bucket_mapping_covers_observed_values(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["resultOffset"] == 0 else {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    by_id = {p.source_permit_id: p for p in vb.fetch()}
    assert by_id["2025-PLBR-00001"].status_bucket == "issued"  # Active
    assert by_id["2025-BDRA-00002"].status_bucket == "active_pending"  # Pending Revisions


def test_unmapped_status_falls_back_to_other(monkeypatch):
    def fake_fetch(url, params=None, **kwargs):
        if params["resultOffset"] != 0:
            return {"features": []}
        return {
            "features": [{"attributes": {
                "PermitNumber": "X1", "StreetAddress": "1 MAIN ST",
                "Status": "Some Brand New Status VB Never Used", "ApplicationDate": "2025/01/01",
            }}]
        }

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    permits = list(vb.fetch())
    assert permits[0].status_bucket == "other"


def test_record_missing_permit_number_is_skipped(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["resultOffset"] == 0 else {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    permits = list(vb.fetch())
    # page1 has 4 features, one has an empty PermitNumber -> 3 yielded
    assert len(permits) == 3
    assert "1 NO ID AVE" not in [p.address for p in permits if p.address]


def test_slash_date_format_normalized(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["resultOffset"] == 0 else {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    by_id = {p.source_permit_id: p for p in vb.fetch()}
    assert by_id["2025-BDRA-00002"].applied_date == "2025-03-12"


def test_empty_string_dates_become_none(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["resultOffset"] == 0 else {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    by_id = {p.source_permit_id: p for p in vb.fetch()}
    assert by_id["2025-PLBR-00001"].finaled_date is None


def test_limit_is_respected(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["resultOffset"] == 0 else {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    permits = list(vb.fetch(limit=2))
    assert len(permits) == 2


def test_pagination_stops_when_not_exceeded_transfer_limit(monkeypatch, page1):
    calls = []

    def fake_fetch(url, params=None, **kwargs):
        calls.append(params["resultOffset"])
        return page1  # exceededTransferLimit: false in the fixture

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    list(vb.fetch())
    assert calls == [0]


def test_pagination_continues_while_exceeded_transfer_limit(monkeypatch):
    page_a = {
        "exceededTransferLimit": True,
        "features": [
            {"attributes": {"PermitNumber": f"P{i}", "StreetAddress": f"{i} MAIN ST",
                             "Status": "Active", "ApplicationDate": "2025/01/01"}}
            for i in range(vb.PAGE_SIZE)
        ],
    }
    page_b = {
        "exceededTransferLimit": False,
        "features": [
            {"attributes": {"PermitNumber": "P-LAST", "StreetAddress": "999 MAIN ST",
                             "Status": "Active", "ApplicationDate": "2025/01/02"}}
        ],
    }
    calls = []

    def fake_fetch(url, params=None, **kwargs):
        calls.append(params["resultOffset"])
        return page_a if params["resultOffset"] == 0 else page_b

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    permits = list(vb.fetch())
    assert calls == [0, vb.PAGE_SIZE]
    assert len(permits) == vb.PAGE_SIZE + 1
    assert permits[-1].source_permit_id == "P-LAST"


def test_since_param_uses_slash_format_not_sql_date_literal(monkeypatch):
    captured = {}

    def fake_fetch(url, params=None, **kwargs):
        captured.update(params)
        return {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    from datetime import date
    list(vb.fetch(since=date(2024, 1, 1)))
    assert captured["where"] == "ApplicationDate >= '2024/01/01'"
    assert "DATE" not in captured["where"]


def test_no_since_uses_unfiltered_where(monkeypatch):
    captured = {}

    def fake_fetch(url, params=None, **kwargs):
        captured.update(params)
        return {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    list(vb.fetch())
    assert captured["where"] == "1=1"


def test_schema_drift_raises_when_field_entirely_absent(monkeypatch):
    def fake_fetch(url, params=None, **kwargs):
        return {
            "exceededTransferLimit": False,
            "features": [
                {"attributes": {"PermitNumber": "A1", "StreetAddress": "1 MAIN ST",
                                 "ApplicationDate": "2025/01/01"}},  # no Status anywhere
                {"attributes": {"PermitNumber": "A2", "StreetAddress": "2 MAIN ST",
                                 "ApplicationDate": "2025/01/02"}},
            ],
        }

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    with pytest.raises(SourceSchemaError):
        list(vb.fetch())


def test_arcgis_error_response_raises_schema_error(monkeypatch):
    def fake_fetch(url, params=None, **kwargs):
        return {"error": {"code": 400, "message": "Invalid field"}}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    with pytest.raises(SourceSchemaError):
        list(vb.fetch())


def test_missing_features_key_raises_schema_error(monkeypatch):
    def fake_fetch(url, params=None, **kwargs):
        return {"objectIdFieldName": "OBJECTID"}  # no "features" key at all

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    with pytest.raises(SourceSchemaError):
        list(vb.fetch())


def test_no_fabricated_values(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["resultOffset"] == 0 else {"features": []}

    monkeypatch.setattr(vb, "fetch_json_retry", fake_fetch)
    permits = list(vb.fetch())
    for p in permits:
        assert p.valuation is None  # no verified cost field exists for VB
        assert p.contractor is None  # CreatedBy is a portal username, not a contractor
        assert p.applicant is None
        assert p.expiration_date is None  # no confirmed expiration field
        assert p.lat is None and p.lng is None  # no native geometry
