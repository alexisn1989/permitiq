"""Phase 3 tests: Norfolk adapter -- fixture-based, no live API calls."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingest.norfolk as norfolk  # noqa: E402
from ingest.common import SourceSchemaError  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> list:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def page1():
    return load_fixture("norfolk_page1.json")


def test_fetch_maps_confirmed_fields(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["$offset"] == 0 else []

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)

    permits = list(norfolk.fetch())
    by_id = {p.source_permit_id: p for p in permits}

    p = by_id["F25-00156"]
    assert p.locality == "norfolk"
    assert p.source_name == "Norfolk Open Data Portal"
    assert p.address == "243 W BUTE STREET, Norfolk, VA"
    assert p.permit_type == "Fire Protection"
    assert p.status == "Cancelled"
    assert p.status_bucket == "expired_void"
    assert p.applied_date == "2025-06-06"
    assert p.issued_date == "2025-02-04"
    assert p.finaled_date == "2025-07-16"
    assert p.expiration_date == "2025-08-03"
    assert p.valuation is None
    assert p.contractor is None
    assert p.description == "New"
    assert p.raw["gpin"] == "1427878551"  # unmapped fields preserved in raw_json


def test_status_bucket_mapping(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["$offset"] == 0 else []

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)

    by_id = {p.source_permit_id: p for p in norfolk.fetch()}
    assert by_id["B25-01022"].status_bucket == "issued"
    assert by_id["B25-01099"].status_bucket == "finaled_closed"
    assert by_id["Z25-00042"].status_bucket == "active_pending"  # "Under Review"


def test_unmapped_status_falls_back_to_other(monkeypatch):
    def fake_fetch(url, params=None, **kwargs):
        if params["$offset"] != 0:
            return []
        return [{
            "permit_number": "X1", "address": "1 MAIN ST", "type": "Building",
            "status": "Some New Status Norfolk Never Used Before",
            "application_date": "2025-01-01",
        }]

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    permits = list(norfolk.fetch())
    assert permits[0].status_bucket == "other"


def test_record_missing_permit_number_is_skipped(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["$offset"] == 0 else []

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    permits = list(norfolk.fetch())
    # page1 has 5 records, one lacks permit_number -> 4 yielded
    assert len(permits) == 4
    assert "NO PERMIT NUMBER HERE" not in [p.address for p in permits]


def test_slash_date_format_normalized(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["$offset"] == 0 else []

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    by_id = {p.source_permit_id: p for p in norfolk.fetch()}
    assert by_id["Z25-00042"].applied_date == "2025-06-15"


def test_limit_is_respected(monkeypatch, page1):
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["$offset"] == 0 else []

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    permits = list(norfolk.fetch(limit=2))
    assert len(permits) == 2


def test_pagination_stops_on_short_page(monkeypatch, page1):
    calls = []

    def fake_fetch(url, params=None, **kwargs):
        calls.append(params["$offset"])
        # page1 has 5 records < PAGE_SIZE (1000), so fetch() should stop after page 1
        return page1 if params["$offset"] == 0 else []

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    list(norfolk.fetch())
    assert calls == [0]  # never asked for a second page


def test_pagination_continues_on_full_page(monkeypatch):
    full_page = [
        {"permit_number": f"P{i}", "address": f"{i} MAIN ST", "type": "Building",
         "status": "Issued", "application_date": "2025-01-01"}
        for i in range(norfolk.PAGE_SIZE)
    ]
    second_page = [
        {"permit_number": "P-LAST", "address": "999 MAIN ST", "type": "Building",
         "status": "Issued", "application_date": "2025-01-02"}
    ]
    calls = []

    def fake_fetch(url, params=None, **kwargs):
        calls.append(params["$offset"])
        return full_page if params["$offset"] == 0 else second_page

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    permits = list(norfolk.fetch())
    assert calls == [0, norfolk.PAGE_SIZE]
    assert len(permits) == norfolk.PAGE_SIZE + 1
    assert permits[-1].source_permit_id == "P-LAST"


def test_since_param_builds_where_clause(monkeypatch):
    captured = {}

    def fake_fetch(url, params=None, **kwargs):
        captured.update(params)
        return []

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    from datetime import date
    list(norfolk.fetch(since=date(2024, 1, 1)))
    assert captured["$where"] == "application_date >= '2024-01-01T00:00:00.000'"


def test_schema_drift_raises_when_field_entirely_absent(monkeypatch):
    def fake_fetch(url, params=None, **kwargs):
        # entire page has no "status" key anywhere -- simulates the source
        # renaming/dropping the field
        return [
            {"permit_number": "A1", "address": "1 MAIN ST", "type": "Building",
             "application_date": "2025-01-01"},
            {"permit_number": "A2", "address": "2 MAIN ST", "type": "Building",
             "application_date": "2025-01-02"},
        ]

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    with pytest.raises(SourceSchemaError):
        list(norfolk.fetch())


def test_schema_drift_not_raised_for_individual_nulls(monkeypatch):
    """Socrata omits null-valued keys per row -- one record legitimately
    missing 'status' must not be mistaken for schema drift."""
    def fake_fetch(url, params=None, **kwargs):
        if params["$offset"] != 0:
            return []
        return [
            {"permit_number": "A1", "address": "1 MAIN ST", "type": "Building",
             "status": "Issued", "application_date": "2025-01-01"},
            {"permit_number": "A2", "address": "2 MAIN ST", "type": "Building",
             "application_date": "2025-01-02"},  # this one just lacks status
        ]

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    permits = list(norfolk.fetch())  # should not raise
    assert len(permits) == 2
    assert permits[1].status is None
    assert permits[1].status_bucket == "other"


def test_non_list_response_raises_schema_error(monkeypatch):
    def fake_fetch(url, params=None, **kwargs):
        return {"error": "not found"}

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    with pytest.raises(SourceSchemaError):
        list(norfolk.fetch())


def test_no_fabricated_values(monkeypatch, page1):
    """Every yielded field must trace back to the raw record or be an
    explicit, documented None -- never a fabricated default."""
    def fake_fetch(url, params=None, **kwargs):
        return page1 if params["$offset"] == 0 else []

    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    permits = list(norfolk.fetch())
    for p in permits:
        assert p.valuation is None  # no verified cost field exists for Norfolk
        assert p.contractor is None  # no verified contractor field exists
        assert p.applicant is None  # no verified applicant field exists
        assert p.lat is None and p.lng is None  # no native geometry


# ---------- Phase 9: expanded status_bucket coverage (real production values) ----------

def test_expanded_status_bucket_map_covers_real_observed_values(monkeypatch):
    """Built from the real distinct `status` values that landed in
    status_bucket='other' against the live 10,850-row database (2026-07-24).
    A regression here means a real production status silently fell back to
    'other' again."""
    expected = {
        "Issued": "issued",
        "Authorized": "issued",
        "Finaled": "finaled_closed",
        "Closed": "finaled_closed",
        "C/O Issued": "finaled_closed",
        "Paid Certificate Issued": "finaled_closed",
        "Cancelled": "expired_void",
        "Abandoned": "expired_void",
        "Withdrawn": "expired_void",
        "Denied": "expired_void",
        "Revoked": "expired_void",
        "Expired": "expired_void",
        "Void": "expired_void",
        "Under Review": "active_pending",
        "Pending": "active_pending",
        "Pending Payment": "active_pending",
        "Additional Information Required": "active_pending",
        "Reviews in progress": "active_pending",
        "Reviews complete": "active_pending",
        "Documents Submitted": "active_pending",
        "Incomplete": "active_pending",
        "Portal Submitted": "active_pending",
        "New": "active_pending",
        "Final Review": "active_pending",
        "Submit for Approval": "active_pending",
        "Overdue": "active_pending",
        "Completed 3rd Party Review": "active_pending",
    }
    for status, expected_bucket in expected.items():
        raw = [{
            "permit_number": f"T-{status}", "address": "1 MAIN ST", "type": "Building",
            "status": status, "application_date": "2025-01-01",
        }]

        def fake_fetch(url, params=None, **kwargs):
            return raw if params["$offset"] == 0 else []

        monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
        permits = list(norfolk.fetch())
        assert permits[0].status_bucket == expected_bucket, f"{status!r} -> {permits[0].status_bucket!r}, expected {expected_bucket!r}"


def test_no_status_falls_into_other_bucket_from_real_backlog(monkeypatch):
    """The 21 previously-'other' statuses observed in production must now
    all resolve to a specific bucket, not 'other'."""
    real_backlog_statuses = [
        "Additional Information Required", "Pending", "Abandoned", "Authorized",
        "Withdrawn", "Reviews in progress", "C/O Issued", "Documents Submitted",
        "Incomplete", "Portal Submitted", "New", "Denied", "Paid Certificate Issued",
        "Final Review", "Submit for Approval", "Reviews complete", "Pending Payment",
        "Overdue", "Revoked", "Completed 3rd Party Review", "Closed",
    ]
    for status in real_backlog_statuses:
        assert norfolk._status_bucket(status) != "other", f"{status!r} still falls to 'other'"


def test_paging_order_is_unique_and_parcel_fields_are_used(monkeypatch):
    seen = []
    rows = [{"permit_number": "BLD26-00001", "address": "100 EXAMPLE ST", "type": "Building", "status": "Voided",
             "application_date": "2026-09-01T00:00:00.000", "gpin": "1234567890", "latitude": "36.88",
             "longitude": "-76.25"},
            {"permit_number": "BLD26-00002", "address": "200 EXAMPLE ST", "type": "Building", "status": "Issued",
             "application_date": "2026-09-01T00:00:00.000", "gpin": "1234567891", "latitude": "40.0",
             "longitude": "-76.25"}]

    def fake_fetch(url, params=None, **kwargs):
        seen.append(params)
        return rows if params["$offset"] == 0 else []
    monkeypatch.setattr(norfolk, "fetch_json_retry", fake_fetch)
    out = list(norfolk.fetch())
    assert ":id" in seen[0]["$order"]  # ties on application_date must not shift rows between pages
    a, b = out
    assert (a.lat, a.lng, a.coordinate_source, a.geocode_status) == (36.88, -76.25, "native", "native")
    assert (b.lat, b.coordinate_source, b.geocode_status) == (None, None, "pending")  # outside Hampton Roads
    assert a.raw["GPIN"] == "1234567890" and a.status_bucket == "expired_void"
