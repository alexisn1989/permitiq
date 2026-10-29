"""Geocoding tests: bbox/locality validation, GeocodeResult provenance,
NominatimRateLimiter pacing, cache behavior, the row-by-row incremental path
(geocode_missing), and the batch-first backlog path (geocode_backlog_batch).
All network calls and sleeps are mocked/injected -- no real 1-second sleeps
anywhere in this suite."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
import ingest.geocode as geocode  # noqa: E402
from ingest.common import NormalizedPermit, upsert_permits  # noqa: E402
from ingest.geocode import GeocodeResult, NominatimRateLimiter  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    db_module.init_db(db_path)
    with db_module.db(db_path) as c:
        yield c


def insert_permit(conn, source_permit_id="P1", address="1 MAIN ST, Norfolk, VA", locality="norfolk"):
    upsert_permits(conn, [NormalizedPermit(
        locality=locality, source_permit_id=source_permit_id, source_name="test",
        address=address, status_bucket="other",
    )])


class FakeClock:
    """Deterministic, injectable clock+sleep for NominatimRateLimiter tests
    -- advances only when `sleep` is called, so tests run instantly."""

    def __init__(self):
        self.now = 0.0
        self.sleep_calls: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------- NominatimRateLimiter ----------

def test_rate_limiter_no_wait_on_first_call():
    clock = FakeClock()
    limiter = NominatimRateLimiter(min_interval=1.0, sleep_fn=clock.sleep, clock=clock.clock)
    limiter.wait()
    assert clock.sleep_calls == []


def test_rate_limiter_waits_remaining_time_on_rapid_second_call():
    clock = FakeClock()
    limiter = NominatimRateLimiter(min_interval=1.0, sleep_fn=clock.sleep, clock=clock.clock)
    limiter.wait()
    limiter.wait()  # called immediately after -- must wait ~1.0s
    assert clock.sleep_calls == [1.0]


def test_rate_limiter_does_not_oversleep_if_time_already_passed():
    clock = FakeClock()
    limiter = NominatimRateLimiter(min_interval=1.0, sleep_fn=clock.sleep, clock=clock.clock)
    limiter.wait()
    clock.advance(1.5)  # other work (Census, DB writes) already consumed 1.5s
    limiter.wait()
    assert clock.sleep_calls == []  # no sleep needed -- already well-spaced


def test_rate_limiter_multiple_calls_respect_spacing():
    clock = FakeClock()
    limiter = NominatimRateLimiter(min_interval=1.0, sleep_fn=clock.sleep, clock=clock.clock)
    for _ in range(5):
        limiter.wait()
    # first call free, next 4 each need a full 1.0s wait since no time passes otherwise
    assert clock.sleep_calls == [1.0, 1.0, 1.0, 1.0]


# ---------- geocode_address / _finalize_geocode: bbox / locality / provenance ----------

def test_matched_result_inside_bbox_and_locality(monkeypatch):
    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": "1 Main St, Norfolk, VA",
    })
    result = geocode.geocode_address("1 Main St, Norfolk, VA", locality="norfolk")
    assert isinstance(result, GeocodeResult)
    assert result.status == "matched"
    assert result.matched is True
    assert result.lat == 36.85
    assert result.provider == "census"
    assert result.confidence == "high"


def test_no_geocoder_match_returns_not_found(monkeypatch):
    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: None)
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: None)
    result = geocode.geocode_address("nonsense address")
    assert result.status == "not_found"
    assert result.matched is False
    assert result.lat is None


def test_outside_bbox_rejected(monkeypatch):
    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: {
        "lat": 40.71, "lng": -74.00, "matched_address": "New York, NY",
    })
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: None)
    result = geocode.geocode_address("1 Main St")
    assert result.status == "rejected_outside_area"
    assert result.lat is None


def test_locality_mismatch_rejected(monkeypatch):
    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": "1 Main St, Chesapeake, VA",
    })
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: None)
    result = geocode.geocode_address("1 Main St", locality="norfolk")
    assert result.status == "locality_mismatch"
    assert result.lat is None


def test_no_locality_hint_skips_locality_check(monkeypatch):
    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": "1 Main St, Chesapeake, VA",
    })
    result = geocode.geocode_address("1 Main St")  # no locality passed
    assert result.status == "matched"


# ---------- Census success never calls Nominatim / no Nominatim pacing ----------

def test_census_success_never_calls_nominatim(monkeypatch):
    called = {"n": 0}

    def fake_nominatim(addr):
        called["n"] += 1
        return {"lat": 1.0, "lng": 2.0, "matched_address": addr}

    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": "1 Main St, Norfolk, VA",
    })
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", fake_nominatim)
    result = geocode.geocode_address("1 Main St, Norfolk, VA", locality="norfolk")
    assert result.provider == "census"
    assert called["n"] == 0


def test_census_success_incurs_no_nominatim_pacing(monkeypatch):
    clock = FakeClock()
    limiter = NominatimRateLimiter(min_interval=1.0, sleep_fn=clock.sleep, clock=clock.clock)
    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": "1 Main St, Norfolk, VA",
    })
    geocode.geocode_address("1 Main St, Norfolk, VA", locality="norfolk", nominatim_limiter=limiter)
    assert clock.sleep_calls == []  # Nominatim's limiter was never invoked


def test_census_miss_falls_back_to_nominatim(monkeypatch):
    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: None)
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": "1 Main St, Norfolk, VA",
    })
    result = geocode.geocode_address("1 Main St", locality="norfolk")
    assert result.status == "matched"
    assert result.provider == "nominatim"
    assert result.confidence == "medium"


def test_census_rejection_also_falls_back_to_nominatim(monkeypatch):
    """Canonical flow: Census miss/invalid result -> Nominatim -- a Census
    match that fails bbox/locality validation must still try Nominatim,
    not just an outright Census miss."""
    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: {
        "lat": 40.71, "lng": -74.00, "matched_address": "New York, NY",  # outside bbox
    })
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": "1 Main St, Norfolk, VA",
    })
    result = geocode.geocode_address("1 Main St", locality="norfolk")
    assert result.status == "matched"
    assert result.provider == "nominatim"


def test_nominatim_rate_limiter_invoked_immediately_before_real_call(monkeypatch):
    clock = FakeClock()
    limiter = NominatimRateLimiter(min_interval=1.0, sleep_fn=clock.sleep, clock=clock.clock)
    call_order = []

    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: None)

    def fake_nominatim(addr):
        call_order.append(("nominatim_call", clock.now))
        return {"lat": 36.85, "lng": -76.28, "matched_address": "1 Main St, Norfolk, VA"}

    monkeypatch.setattr(geocode, "_geocode_via_nominatim", fake_nominatim)
    limiter.wait()  # simulate a prior nominatim call having just happened
    geocode.geocode_address("1 Main St", locality="norfolk", nominatim_limiter=limiter)
    # the second wait() must have slept the full interval right before the call
    assert clock.sleep_calls == [1.0]
    assert call_order == [("nominatim_call", 1.0)]


def test_multiple_geocode_address_nominatim_calls_respect_spacing(monkeypatch):
    clock = FakeClock()
    limiter = NominatimRateLimiter(min_interval=1.0, sleep_fn=clock.sleep, clock=clock.clock)
    monkeypatch.setattr(geocode, "_geocode_via_census", lambda addr: None)
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": addr,
    })
    for i in range(3):
        geocode.geocode_address(f"{i} Main St", locality="norfolk", nominatim_limiter=limiter)
    # first call is free (no prior call to space against); each subsequent
    # call happens with zero elapsed time in this fake clock, so each needs
    # the full 1.0s wait -- 2 sleeps recorded for 3 total calls.
    assert clock.sleep_calls == [1.0, 1.0]


# ---------- geocode_missing: DB integration (row-by-row incremental path) ----------

def test_geocode_missing_updates_permit_row(conn, monkeypatch):
    insert_permit(conn)
    monkeypatch.setattr(geocode, "geocode_address", lambda addr, locality=None, nominatim_limiter=None: GeocodeResult(
        lat=36.85, lng=-76.28, matched_address="1 Main St, Norfolk, VA", provider="census",
        confidence="high", status="matched", matched=True, error=None,
    ))
    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert counts == {"attempted": 1, "matched": 1, "cache_hit": 0, "failed": 0, "no_address": 0}

    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["lat"] == 36.85
    assert row["coordinate_source"] == "census"
    assert row["geocode_status"] == "matched"
    assert row["geocode_provider"] == "census"


def test_geocode_missing_records_failure_status(conn, monkeypatch):
    insert_permit(conn)
    monkeypatch.setattr(geocode, "geocode_address", lambda addr, locality=None, nominatim_limiter=None: GeocodeResult(
        lat=None, lng=None, matched_address=None, provider=None, confidence=None,
        status="not_found", matched=False, error="no geocoder returned a match",
    ))
    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert counts == {"attempted": 1, "matched": 0, "cache_hit": 0, "failed": 1, "no_address": 0}

    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["lat"] is None
    assert row["geocode_status"] == "not_found"
    assert row["geocode_error"] == "no geocoder returned a match"


def test_geocode_missing_skips_rows_already_geocoded(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1")
    insert_permit(conn, source_permit_id="P2", address="2 Main St, Norfolk, VA")
    calls = {"n": 0}

    def fake_geocode(addr, locality=None, nominatim_limiter=None):
        calls["n"] += 1
        return GeocodeResult(lat=36.85, lng=-76.28, matched_address=addr, provider="census",
                              confidence="high", status="matched", matched=True, error=None)

    monkeypatch.setattr(geocode, "geocode_address", fake_geocode)
    geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert calls["n"] == 2

    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert counts == {"attempted": 0, "matched": 0, "cache_hit": 0, "failed": 0, "no_address": 0}
    assert calls["n"] == 2  # no new network calls


def test_geocode_missing_uses_cache_for_repeated_address(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    insert_permit(conn, source_permit_id="P2", address="1 Main St, Norfolk, VA")
    calls = {"n": 0}

    def fake_geocode(addr, locality=None, nominatim_limiter=None):
        calls["n"] += 1
        return GeocodeResult(lat=36.85, lng=-76.28, matched_address=addr, provider="census",
                              confidence="high", status="matched", matched=True, error=None)

    monkeypatch.setattr(geocode, "geocode_address", fake_geocode)
    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert counts["matched"] == 2
    assert counts["cache_hit"] == 1
    assert calls["n"] == 1


def test_geocode_missing_caches_negative_results_too(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="999 Nowhere Ave, Norfolk, VA")
    insert_permit(conn, source_permit_id="P2", address="999 Nowhere Ave, Norfolk, VA")
    calls = {"n": 0}

    def fake_geocode(addr, locality=None, nominatim_limiter=None):
        calls["n"] += 1
        return GeocodeResult(lat=None, lng=None, matched_address=None, provider=None,
                              confidence=None, status="not_found", matched=False, error="no match")

    monkeypatch.setattr(geocode, "geocode_address", fake_geocode)
    geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert calls["n"] == 1
    cached = conn.execute(
        "SELECT * FROM geocode_cache WHERE normalized_address='999 NOWHERE AVE, NORFOLK, VA'"
    ).fetchone()
    assert cached["status"] == "not_found"


def test_geocode_missing_respects_limit(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    insert_permit(conn, source_permit_id="P2", address="2 Main St, Norfolk, VA")
    monkeypatch.setattr(geocode, "geocode_address", lambda addr, locality=None, nominatim_limiter=None: GeocodeResult(
        lat=36.85, lng=-76.28, matched_address=addr, provider="census", confidence="high",
        status="matched", matched=True, error=None,
    ))
    counts = geocode.geocode_missing(conn, limit=1, courtesy_pace_seconds=0)
    assert counts["attempted"] == 1


def test_geocode_missing_locality_filter(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", locality="norfolk", address="1 Main St, Norfolk, VA")
    insert_permit(conn, source_permit_id="P2", locality="virginia_beach", address="2 Main St, Virginia Beach, VA")
    monkeypatch.setattr(geocode, "geocode_address", lambda addr, locality=None, nominatim_limiter=None: GeocodeResult(
        lat=36.85, lng=-76.28, matched_address=addr, provider="census", confidence="high",
        status="matched", matched=True, error=None,
    ))
    counts = geocode.geocode_missing(conn, locality="norfolk", courtesy_pace_seconds=0)
    assert counts["attempted"] == 1


def test_geocode_missing_null_address_never_reaches_geocode_address(conn, monkeypatch):
    """A NULL-address permit IS now considered (so it gets an honest
    'no_address' classification instead of sitting silently in 'pending'
    forever -- see test_geocode_missing_classifies_null_address_as_no_address)
    but must still never reach an actual geocoder call, since there's
    nothing to geocode."""
    upsert_permits(conn, [NormalizedPermit(
        locality="norfolk", source_permit_id="P1", source_name="test",
        address=None, status_bucket="other",
    )])
    called = {"n": 0}
    monkeypatch.setattr(geocode, "geocode_address", lambda *a, **k: called.update(n=called["n"] + 1))
    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert counts["no_address"] == 1
    assert called["n"] == 0


def test_geocode_missing_does_not_retry_recent_failure_without_force(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1")
    monkeypatch.setattr(geocode, "geocode_address", lambda addr, locality=None, nominatim_limiter=None: GeocodeResult(
        lat=None, lng=None, matched_address=None, provider=None, confidence=None,
        status="not_found", matched=False, error="no match",
    ))
    geocode.geocode_missing(conn, courtesy_pace_seconds=0)

    calls = {"n": 0}

    def fake_geocode(addr, locality=None, nominatim_limiter=None):
        calls["n"] += 1
        return GeocodeResult(lat=None, lng=None, matched_address=None, provider=None,
                              confidence=None, status="not_found", matched=False, error="no match")

    monkeypatch.setattr(geocode, "geocode_address", fake_geocode)
    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert counts["attempted"] == 0
    assert calls["n"] == 0


def test_geocode_missing_force_retries_recent_failure(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1")
    monkeypatch.setattr(geocode, "geocode_address", lambda addr, locality=None, nominatim_limiter=None: GeocodeResult(
        lat=None, lng=None, matched_address=None, provider=None, confidence=None,
        status="not_found", matched=False, error="no match",
    ))
    geocode.geocode_missing(conn, courtesy_pace_seconds=0)

    calls = {"n": 0}

    def fake_geocode(addr, locality=None, nominatim_limiter=None):
        calls["n"] += 1
        return GeocodeResult(lat=36.85, lng=-76.28, matched_address=addr, provider="census",
                              confidence="high", status="matched", matched=True, error=None)

    monkeypatch.setattr(geocode, "geocode_address", fake_geocode)
    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0, force=True)
    assert counts["attempted"] == 1
    assert calls["n"] == 1


# ---------- no_address classification ----------

def test_looks_like_street_address_true_for_real_addresses():
    assert geocode._looks_like_street_address("1424 SEA BREEZE TRL, Virginia Beach, VA 23452")
    assert geocode._looks_like_street_address("243 W BUTE STREET, Norfolk, VA")
    assert geocode._looks_like_street_address("1345 TAYLOR FARM RD 111, Virginia Beach, VA 23453")


def test_looks_like_street_address_false_for_non_addresses():
    assert not geocode._looks_like_street_address("Verizon - Poles 104, Virginia Beach, VA")
    assert not geocode._looks_like_street_address(None)
    assert not geocode._looks_like_street_address("")


def test_geocode_missing_marks_no_address_without_network_call(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="Verizon - Poles 104, Virginia Beach, VA")
    called = {"n": 0}

    def fake_geocode(*a, **k):
        called["n"] += 1
        raise AssertionError("geocode_address should never be called for a no_address record")

    monkeypatch.setattr(geocode, "geocode_address", fake_geocode)
    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0)

    assert called["n"] == 0
    assert counts == {"attempted": 1, "matched": 0, "cache_hit": 0, "failed": 0, "no_address": 1}
    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["geocode_status"] == "no_address"
    assert row["lat"] is None


def test_real_street_address_that_fails_geocoding_stays_not_found_not_no_address(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1345 TAYLOR FARM RD 111, Virginia Beach, VA 23453")
    monkeypatch.setattr(geocode, "geocode_address", lambda addr, locality=None, nominatim_limiter=None: GeocodeResult(
        lat=None, lng=None, matched_address=None, provider=None, confidence=None,
        status="not_found", matched=False, error="no geocoder returned a match",
    ))
    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert counts["no_address"] == 0
    assert counts["failed"] == 1
    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["geocode_status"] == "not_found"


# ---------- geocode_census_batch ----------

def test_geocode_census_batch_maps_results_to_correct_ids(monkeypatch):
    csv_response = (
        '"1","1 Main St, Norfolk, VA, ","Match","Exact","1 MAIN ST, NORFOLK, VA, 23510",'
        '"-76.28,36.85","123","L"\n'
        '"2","2 Main St, Norfolk, VA, ","No_Match"\n'
        '"3","3 Main St, Norfolk, VA, ","Match","Exact","3 MAIN ST, NORFOLK, VA, 23510",'
        '"-76.30,36.86","124","L"\n'
    )
    monkeypatch.setattr(geocode, "_post_census_batch", lambda rows: csv_response)

    results = geocode.geocode_census_batch({
        "1": "1 Main St, Norfolk, VA", "2": "2 Main St, Norfolk, VA", "3": "3 Main St, Norfolk, VA",
    })
    assert set(results.keys()) == {"1", "3"}  # id 2 (No_Match) correctly absent
    assert results["1"]["lat"] == 36.85
    assert results["1"]["lng"] == -76.28
    assert results["3"]["lat"] == 36.86


def test_geocode_census_batch_empty_input_returns_empty():
    assert geocode.geocode_census_batch({}) == {}


def test_geocode_census_batch_rejects_oversized_chunk():
    addresses = {str(i): f"{i} Main St" for i in range(geocode._CENSUS_BATCH_CHUNK_SIZE + 1)}
    with pytest.raises(ValueError):
        geocode.geocode_census_batch(addresses)


# ---------- geocode_backlog_batch: DB integration ----------

def test_backlog_batch_no_address_skips_external_calls(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="Verizon - Poles 104, Norfolk, VA")
    census_called = {"n": 0}
    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: census_called.update(n=census_called["n"] + 1) or {})
    counts = geocode.geocode_backlog_batch(conn)
    assert census_called["n"] == 0
    assert counts["no_address"] == 1
    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["geocode_status"] == "no_address"


def test_backlog_batch_dedupes_repeated_addresses_before_census(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    insert_permit(conn, source_permit_id="P2", address="1 Main St, Norfolk, VA")  # identical
    insert_permit(conn, source_permit_id="P3", address="2 Main St, Norfolk, VA")

    seen_batches = []

    def fake_batch(addrs):
        seen_batches.append(dict(addrs))
        return {k: {"lat": 36.85, "lng": -76.28, "matched_address": v} for k, v in addrs.items()}

    monkeypatch.setattr(geocode, "geocode_census_batch", fake_batch)
    counts = geocode.geocode_backlog_batch(conn)

    assert counts["unique_addresses_sent_to_census"] == 2  # deduped: 2 unique, not 3
    assert len(seen_batches) == 1
    assert len(seen_batches[0]) == 2


def test_backlog_batch_applies_result_to_all_permits_sharing_address(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    insert_permit(conn, source_permit_id="P2", address="1 Main St, Norfolk, VA")

    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: {
        k: {"lat": 36.85, "lng": -76.28, "matched_address": v} for k, v in addrs.items()
    })
    counts = geocode.geocode_backlog_batch(conn)
    assert counts["newly_mapped"] == 2  # both permits updated from one census match

    rows = conn.execute("SELECT * FROM permits WHERE source_permit_id IN ('P1','P2')").fetchall()
    assert all(r["lat"] == 36.85 for r in rows)
    assert all(r["geocode_provider"] == "census" for r in rows)


def test_backlog_batch_census_miss_falls_back_to_nominatim(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: {})  # no match

    clock = FakeClock()
    limiter = NominatimRateLimiter(min_interval=1.0, sleep_fn=clock.sleep, clock=clock.clock)
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": addr,
    })
    counts = geocode.geocode_backlog_batch(conn, nominatim_limiter=limiter)
    assert counts["census_not_found"] == 1
    assert counts["nominatim_attempted"] == 1
    assert counts["nominatim_matched"] == 1
    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["geocode_provider"] == "nominatim"


def test_backlog_batch_census_rejection_falls_back_to_nominatim(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: {
        k: {"lat": 40.71, "lng": -74.00, "matched_address": "New York, NY"} for k in addrs
    })
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": addr,
    })
    counts = geocode.geocode_backlog_batch(conn, nominatim_limiter=NominatimRateLimiter(sleep_fn=lambda s: None))
    assert counts["census_rejected"] == 1
    assert counts["nominatim_attempted"] == 1
    assert counts["nominatim_matched"] == 1


def test_backlog_batch_nominatim_calls_are_sequential_and_paced(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    insert_permit(conn, source_permit_id="P2", address="2 Main St, Norfolk, VA")
    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: {})

    clock = FakeClock()
    limiter = NominatimRateLimiter(min_interval=1.0, sleep_fn=clock.sleep, clock=clock.clock)
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": addr,
    })
    counts = geocode.geocode_backlog_batch(conn, nominatim_limiter=limiter)
    assert counts["nominatim_attempted"] == 2
    assert clock.sleep_calls == [1.0]  # first call free, second waits


def test_backlog_batch_locality_mismatch_rejected(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", locality="norfolk", address="1 Main St, Norfolk, VA")
    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: {
        k: {"lat": 36.85, "lng": -76.28, "matched_address": "1 Main St, Chesapeake, VA"} for k in addrs
    })
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: None)  # nominatim also fails
    counts = geocode.geocode_backlog_batch(conn, nominatim_limiter=NominatimRateLimiter(sleep_fn=lambda s: None))
    assert counts["census_rejected"] == 1
    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["geocode_status"] == "not_found"  # nominatim's own failure is the final status
    assert row["lat"] is None


def test_backlog_batch_uses_cache_without_calling_census(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    conn.execute(
        """INSERT INTO geocode_cache (normalized_address, lat, lng, matched_address, provider,
               confidence, status, error_message, updated_at)
           VALUES ('1 MAIN ST, NORFOLK, VA', 36.85, -76.28, '1 Main St, Norfolk, VA', 'census',
                   'high', 'matched', NULL, '2026-01-01T00:00:00Z')"""
    )
    conn.commit()

    census_called = {"n": 0}
    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: census_called.update(n=census_called["n"] + 1) or {})
    counts = geocode.geocode_backlog_batch(conn)
    assert census_called["n"] == 0
    assert counts["cache_hits"] == 1
    assert counts["newly_mapped"] == 1
    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["lat"] == 36.85


def test_backlog_batch_existing_coordinates_never_touched(conn, monkeypatch):
    """Only rows with lat IS NULL are ever selected -- a permit that already
    has a coordinate (manual, native, or previously geocoded) must be
    completely untouched by a batch run, even with --force."""
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    conn.execute(
        "UPDATE permits SET lat=99.0, lng=99.0, coordinate_source='manual', geocode_status='manual_override' "
        "WHERE source_permit_id='P1'"
    )
    conn.commit()

    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: {
        k: {"lat": 36.85, "lng": -76.28, "matched_address": v} for k, v in addrs.items()
    })
    counts = geocode.geocode_backlog_batch(conn, force=True)
    assert counts["total_pending_considered"] == 0  # never selected at all

    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["lat"] == 99.0
    assert row["coordinate_source"] == "manual"


def test_backlog_batch_no_duplicate_permit_rows_created(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    before = conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0]

    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: {
        k: {"lat": 36.85, "lng": -76.28, "matched_address": v} for k, v in addrs.items()
    })
    geocode.geocode_backlog_batch(conn)
    after = conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0]
    assert before == after == 1


def test_backlog_batch_resumable_second_run_does_not_redo_work(conn, monkeypatch):
    """Simulates an interrupted-and-resumed run: after the first (complete)
    run, a second run must not re-call Census/Nominatim for already-resolved
    permits, and must not corrupt or duplicate anything."""
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    insert_permit(conn, source_permit_id="P2", address="999 Nowhere Ave, Norfolk, VA")

    call_log = []

    def fake_batch(addrs):
        call_log.append(dict(addrs))
        return {k: {"lat": 36.85, "lng": -76.28, "matched_address": v}
                for k, v in addrs.items() if "Main" in v}

    monkeypatch.setattr(geocode, "geocode_census_batch", fake_batch)
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: None)  # P2 stays not_found

    first = geocode.geocode_backlog_batch(conn, nominatim_limiter=NominatimRateLimiter(sleep_fn=lambda s: None))
    assert first["newly_mapped"] == 1
    assert first["nominatim_attempted"] == 1

    dupes = conn.execute(
        "SELECT locality, source_permit_id, COUNT(*) c FROM permits GROUP BY 1,2 HAVING c>1"
    ).fetchall()
    assert len(dupes) == 0

    # second run: P1 already mapped (lat IS NOT NULL, excluded); P2 failed
    # recently so is gated by the retry interval -- neither should trigger
    # new external calls.
    second = geocode.geocode_backlog_batch(conn, nominatim_limiter=NominatimRateLimiter(sleep_fn=lambda s: None))
    assert second["total_pending_considered"] == 0
    assert len(call_log) == 1  # no new census batch call on the second run

    row1 = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row1["lat"] == 36.85  # untouched/preserved by the second run


def test_backlog_batch_reports_mapped_before_after_and_remaining(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    insert_permit(conn, source_permit_id="P2", address="2 Main St, Norfolk, VA")
    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: {
        list(addrs.keys())[0]: {"lat": 36.85, "lng": -76.28, "matched_address": list(addrs.values())[0]},
    } if addrs else {})
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: None)

    counts = geocode.geocode_backlog_batch(conn, nominatim_limiter=NominatimRateLimiter(sleep_fn=lambda s: None))
    assert counts["mapped_before"] == 0
    assert counts["mapped_after"] == 1
    # remaining_pending means "still unmapped with a usable address" (the
    # actual backlog size), not "never attempted" -- the other permit
    # failed (not_found) but still has no coordinate, so it still counts.
    assert counts["remaining_pending"] == 1


def test_backlog_batch_per_locality_breakdown(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", locality="norfolk", address="1 Main St, Norfolk, VA")
    insert_permit(conn, source_permit_id="P2", locality="virginia_beach", address="2 Main St, Virginia Beach, VA")
    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: {
        k: {"lat": 36.85, "lng": -76.28, "matched_address": v} for k, v in addrs.items()
    })
    counts = geocode.geocode_backlog_batch(conn)
    assert counts["per_locality"]["norfolk"]["census_matched"] == 1
    assert counts["per_locality"]["virginia_beach"]["census_matched"] == 1


def test_backlog_batch_errors_counted_when_census_call_raises(conn, monkeypatch):
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")

    def raising_batch(addrs):
        raise RuntimeError("network error")

    monkeypatch.setattr(geocode, "geocode_census_batch", raising_batch)
    monkeypatch.setattr(geocode, "_geocode_via_nominatim", lambda addr: {
        "lat": 36.85, "lng": -76.28, "matched_address": addr,
    })
    counts = geocode.geocode_backlog_batch(conn, nominatim_limiter=NominatimRateLimiter(sleep_fn=lambda s: None))
    assert counts["errors"] == 1
    # the address still falls through to nominatim rather than being lost
    assert counts["nominatim_attempted"] == 1


# ---------- analyze_unmapped ----------

def test_analyze_unmapped_breakdown(conn):
    # P1 left fully untouched -- still 'pending', never attempted.
    insert_permit(conn, source_permit_id="P1", address="1 Main St, Norfolk, VA")
    # P2/P3 constructed directly to match what geocode_missing/backlog would
    # have produced -- avoids any real or mocked network call in this test.
    insert_permit(conn, source_permit_id="P2", address="Verizon - Poles 1, Norfolk, VA")
    conn.execute(
        "UPDATE permits SET geocode_status='no_address', geocode_attempted_at=? WHERE source_permit_id='P2'",
        (db_module.now_iso(),),
    )
    insert_permit(conn, source_permit_id="P3", address="999 Nowhere Ave, Norfolk, VA")
    conn.execute(
        "UPDATE permits SET geocode_status='not_found', geocode_attempted_at=? WHERE source_permit_id='P3'",
        (db_module.now_iso(),),
    )
    conn.commit()

    analysis = geocode.analyze_unmapped(conn)
    assert analysis["by_geocode_status"]["no_address"] == 1
    assert analysis["by_geocode_status"]["not_found"] == 1
    assert analysis["by_geocode_status"]["pending"] == 1
    assert analysis["never_attempted"] == 1  # only P1
    assert len(analysis["sample_not_found_addresses"]) == 1
    assert analysis["sample_not_found_addresses"][0]["source_permit_id"] == "P3"


# ---------- NULL-address permits are classified, not silently stuck pending ----------
# Regression coverage for a real gap found in production: 829 real Virginia
# Beach permits (mostly HAUL-type) have address=NULL in the source data.
# These were previously excluded by the WHERE clause before ever reaching
# the no_address check, so they sat in geocode_status='pending' forever
# with no honest classification.

def test_geocode_missing_classifies_null_address_as_no_address(conn):
    upsert_permits(conn, [NormalizedPermit(
        locality="virginia_beach", source_permit_id="P1", source_name="test",
        address=None, status_bucket="other",
    )])
    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert counts == {"attempted": 1, "matched": 0, "cache_hit": 0, "failed": 0, "no_address": 1}
    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["geocode_status"] == "no_address"


def test_geocode_missing_classifies_empty_string_address_as_no_address(conn):
    upsert_permits(conn, [NormalizedPermit(
        locality="virginia_beach", source_permit_id="P1", source_name="test",
        address="", status_bucket="other",
    )])
    counts = geocode.geocode_missing(conn, courtesy_pace_seconds=0)
    assert counts["no_address"] == 1


def test_backlog_batch_classifies_null_address_as_no_address(conn, monkeypatch):
    upsert_permits(conn, [NormalizedPermit(
        locality="virginia_beach", source_permit_id="P1", source_name="test",
        address=None, status_bucket="other",
    )])
    census_called = {"n": 0}
    monkeypatch.setattr(geocode, "geocode_census_batch", lambda addrs: census_called.update(n=census_called["n"] + 1) or {})
    counts = geocode.geocode_backlog_batch(conn)
    assert census_called["n"] == 0
    assert counts["no_address"] == 1
    row = conn.execute("SELECT * FROM permits WHERE source_permit_id='P1'").fetchone()
    assert row["geocode_status"] == "no_address"


def test_backlog_batch_failed_census_request_is_split_and_never_recorded_as_a_miss(conn, monkeypatch):
    for i in range(6):
        insert_permit(conn, source_permit_id=f"P{i}", address=f"{i} Main St, Norfolk, VA")
    calls = []

    def flaky(addrs):  # the full chunk times out; halves succeed, except one half that keeps failing
        calls.append(len(addrs))
        if len(addrs) > 3 or any(v.startswith("0 ") for v in addrs.values()):
            raise RuntimeError("timeout")
        return {k: {"lat": 36.85, "lng": -76.28, "matched_address": v} for k, v in addrs.items()}

    monkeypatch.setattr(geocode, "_CENSUS_MIN_SPLIT", 3)
    monkeypatch.setattr(geocode, "geocode_census_batch", flaky)
    counts = geocode.geocode_backlog_batch(conn, census_only=True)
    assert calls[0] == 6 and counts["census_matched"] == 3 and counts["census_failed"] == 3
    assert counts["census_not_found"] == 0 and counts["nominatim_attempted"] == 0
    statuses = dict(conn.execute("SELECT geocode_status, COUNT(*) FROM permits GROUP BY 1").fetchall())
    assert statuses == {"matched": 3, "pending": 3}  # failed ones stay pending, retried next run
    assert conn.execute("SELECT COUNT(*) FROM geocode_cache WHERE status != 'matched'").fetchone()[0] == 0
