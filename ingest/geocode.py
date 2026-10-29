"""Standalone geocoder for PermitIQ: Census Geocoder first (free, no key,
no documented per-request rate limit), Nominatim fallback (single-threaded,
paced to its ~1 req/sec usage policy), backed by a persistent geocode_cache
so the same address is never re-queried across permits, localities, or
re-runs.

Two entry points for two different scales of work:
  - geocode_missing(): row-by-row, used by run_ingest.py for small daily
    incremental syncs (dozens-hundreds of new permits).
  - geocode_backlog_batch(): dedupes addresses and sends them through
    Census's batch endpoint (thousands of addresses per HTTP call) before
    falling through to Nominatim only for the addresses Census couldn't
    resolve -- used by scripts/geocode_backlog.py for large catch-up runs.
    Census resolves ~97% of real matches in this dataset, so batching it
    turns a multi-hour row-by-row backlog into minutes.

Vendored from Vriginia_api_election's voteiq/services/geo_lite.py and
scripts/geocode_agenda_addresses.py pattern -- no cross-repo import,
PermitIQ stays self-contained.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from db import now_iso

# Distinguishes "a real street address that the geocoder couldn't resolve"
# (not_found) from "the source record has no meaningful street address at
# all" (no_address) -- e.g. Virginia Beach utility permits like
# "Verizon - Poles 104, Virginia Beach, VA" observed live in production.
# Loose on purpose: a leading house-number run followed by more text. A
# permit whose address genuinely starts with a number but still fails to
# geocode (e.g. "1345 TAYLOR FARM RD 111") correctly stays not_found.
_STREET_ADDRESS_RE = re.compile(r"^\s*\d{1,6}(-\d{1,6})?\s+\S")


def _looks_like_street_address(address: str | None) -> bool:
    return bool(_STREET_ADDRESS_RE.match(address or ""))


_CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
_CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
# Census documents a 10,000-row cap per batch file; stay well under it so a
# single chunk's request/response size and server-side processing time stay
# reasonable. Verified live against the real endpoint (2026-07) with a small
# sample before this was written -- response is a CSV of
# id,input_address,"Match"|"No_Match"[,match_type,matched_address,"lon,lat",tiger_id,side].
_CENSUS_BATCH_CHUNK_SIZE = 5000
_NOMINATIM_UA = "permitiq_civic_platform_v1"

# Hampton Roads bounding box with slack -- rejects obviously-wrong geocodes
# (wrong state/region entirely), same constant family as
# Hampton_Roads_Flood_Map/prepare_data.py's regional bbox.
HAMPTON_ROADS_BBOX = {"min_lat": 36.50, "max_lat": 37.70, "min_lng": -77.60, "max_lng": -75.55}

LOCALITY_DISPLAY = {
    "norfolk": "Norfolk",
    "virginia_beach": "Virginia Beach",
    "chesapeake": "Chesapeake",
    "portsmouth": "Portsmouth",
}

# Don't hammer a permanently-bad address every single run -- only retry a
# not_found/rejected/mismatch result after this many days, or with --force.
RETRY_INTERVAL_DAYS = 30


@dataclass
class GeocodeResult:
    """Explicit provenance for a geocoding outcome -- answers all of: was it
    matched, by which provider, at what confidence, or why not (no usable
    address / not found / outside the region / wrong locality)."""

    lat: float | None
    lng: float | None
    matched_address: str | None
    provider: str | None  # "census" | "nominatim" | None
    confidence: str | None  # "high" | "medium" | None
    status: str  # matched | not_found | no_address | rejected_outside_area | locality_mismatch
    matched: bool
    error: str | None
    from_cache: bool = False  # this invocation's provenance, not persisted -- the
    # ORIGINAL provider/confidence that produced the coordinate is what's
    # stored in geocode_cache/permits regardless of how many times it's
    # later served from cache.


class NominatimRateLimiter:
    """Enforces Nominatim's ~1 req/sec usage policy across calls process-wide
    -- tracks actual elapsed time since the last real Nominatim call (via an
    injectable clock) rather than a naive fixed sleep before every call, so
    calls already naturally spaced out by other work (cache lookups, Census
    attempts, DB writes) don't oversleep. Single instance, single-threaded --
    Nominatim's usage policy assumes one requester, not parallel fan-out, and
    this class does not implement or permit concurrent calls."""

    def __init__(self, min_interval: float = 1.0, sleep_fn=time.sleep, clock=time.monotonic):
        self.min_interval = min_interval
        self._sleep = sleep_fn
        self._clock = clock
        self._last_call: float | None = None

    def wait(self) -> None:
        """Call immediately before the real Nominatim network request."""
        now = self._clock()
        if self._last_call is not None:
            remaining = self.min_interval - (now - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
        self._last_call = self._clock()


_nominatim_limiter = NominatimRateLimiter()


def _normalize_address(address: str) -> str:
    return " ".join(address.strip().upper().split())


def _in_bbox(lat: float, lng: float) -> bool:
    return (
        HAMPTON_ROADS_BBOX["min_lat"] <= lat <= HAMPTON_ROADS_BBOX["max_lat"]
        and HAMPTON_ROADS_BBOX["min_lng"] <= lng <= HAMPTON_ROADS_BBOX["max_lng"]
    )


def _locality_matches(matched_address: str | None, locality: str) -> bool:
    display = LOCALITY_DISPLAY.get(locality, locality)
    return display.lower() in (matched_address or "").lower()


def _finalize_geocode(
    lat: float | None, lng: float | None, matched_address: str | None,
    provider: str | None, locality: str | None,
) -> GeocodeResult:
    """Applies bbox + locality validation uniformly regardless of which
    provider or code path (single-address vs. batch) produced the raw
    coordinate -- the one place this logic lives."""
    if lat is None or lng is None:
        return GeocodeResult(
            lat=None, lng=None, matched_address=None, provider=provider, confidence=None,
            status="not_found", matched=False, error=f"{provider or 'geocoder'}: no match",
        )
    if not _in_bbox(lat, lng):
        return GeocodeResult(
            lat=None, lng=None, matched_address=matched_address, provider=provider, confidence=None,
            status="rejected_outside_area", matched=False,
            error=f"geocoded outside Hampton Roads bbox: ({lat}, {lng})",
        )
    if locality and not _locality_matches(matched_address, locality):
        return GeocodeResult(
            lat=None, lng=None, matched_address=matched_address, provider=provider, confidence=None,
            status="locality_mismatch", matched=False,
            error=f"geocoded outside expected locality {locality!r}: {matched_address!r}",
        )
    confidence = "high" if provider == "census" else "medium" if provider == "nominatim" else None
    return GeocodeResult(
        lat=lat, lng=lng, matched_address=matched_address, provider=provider, confidence=confidence,
        status="matched", matched=True, error=None,
    )


def _geocode_via_census(address: str) -> dict | None:
    """Raw, single-address Census call -- no validation applied here."""
    try:
        r = requests.get(
            _CENSUS_URL,
            params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
            timeout=10,
        )
        matches = r.json().get("result", {}).get("addressMatches", [])
        if matches:
            coords = matches[0]["coordinates"]
            return {
                "lat": float(coords["y"]),
                "lng": float(coords["x"]),
                "matched_address": matches[0].get("matchedAddress", address),
            }
    except Exception:  # noqa: BLE001 - caller treats this the same as "no match"
        pass
    return None


def _geocode_via_nominatim(address: str) -> dict | None:
    """Raw, single-address Nominatim call -- no validation, no pacing here.
    Pacing is the caller's responsibility via NominatimRateLimiter.wait(),
    called immediately before this function so spacing is enforced between
    real Nominatim network requests regardless of caller."""
    try:
        from geopy.geocoders import Nominatim

        geolocator = Nominatim(user_agent=_NOMINATIM_UA, timeout=15)
        location = geolocator.geocode(address)
        if location:
            return {
                "lat": location.latitude,
                "lng": location.longitude,
                "matched_address": location.raw.get("display_name", address),
            }
    except Exception:  # noqa: BLE001 - caller treats this the same as "no match"
        pass
    return None


def geocode_address(
    address: str, locality: str | None = None, nominatim_limiter: NominatimRateLimiter | None = None,
) -> GeocodeResult:
    """Canonical single-address flow: Census first; if Census's raw result
    is missing OR fails validation (outside the region / wrong locality),
    fall through to a rate-limited Nominatim call, validated the same way.
    Nominatim's pacing is invoked immediately before the real Nominatim
    request, only when Census didn't already produce a valid match --
    Census gets no artificial pacing here (see module docstring)."""
    limiter = nominatim_limiter or _nominatim_limiter

    census_raw = _geocode_via_census(address)
    census_result = _finalize_geocode(
        census_raw["lat"] if census_raw else None,
        census_raw["lng"] if census_raw else None,
        census_raw["matched_address"] if census_raw else None,
        "census", locality,
    )
    if census_result.matched:
        return census_result

    limiter.wait()
    nominatim_raw = _geocode_via_nominatim(address)
    if nominatim_raw is not None:
        nominatim_result = _finalize_geocode(
            nominatim_raw["lat"], nominatim_raw["lng"], nominatim_raw["matched_address"],
            "nominatim", locality,
        )
        return nominatim_result

    # Neither provider produced a usable match -- Census's own failure
    # reason (e.g. "outside the region") is more informative than a
    # generic not_found when Census actually found *something*.
    return census_result


def _split_address_for_batch(address: str) -> tuple[str, str, str, str]:
    """Splits a fully-qualified 'STREET, City, ST ZIP' address (the shape
    every adapter in this project builds) into (street, city, state, zip)
    for the Census batch CSV format. Falls back to the whole string in
    'street' with empty city/state/zip if the shape doesn't match -- the
    batch geocoder can sometimes still resolve from that, just less
    reliably."""
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 3:
        street = parts[0]
        city = parts[1]
        state_zip = parts[2].split()
        state = state_zip[0] if state_zip else ""
        zip_code = state_zip[1] if len(state_zip) > 1 else ""
        return street, city, state, zip_code
    return address, "", "", ""


def _post_census_batch(rows: list[tuple[str, str, str, str, str]]) -> str:
    """rows: (unique_id, street, city, state, zip). Returns the raw CSV
    response text. Isolated so tests can monkeypatch just the network call."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    resp = requests.post(
        _CENSUS_BATCH_URL,
        files={"addressFile": ("batch.csv", buf.getvalue().encode("utf-8"), "text/csv")},
        data={"benchmark": "Public_AR_Current"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.text


_CENSUS_MIN_SPLIT = 250


def _census_with_split(addresses: dict[str, str]) -> tuple[dict[str, dict], set[str]]:
    """Census results for a chunk, and the ids whose request failed. A failed
    request (timeout, HTTP error) is retried as two halves down to
    _CENSUS_MIN_SPLIT addresses; ids still failing are returned, never
    treated as Census misses."""
    try:
        return geocode_census_batch(addresses), set()
    except Exception:  # noqa: BLE001 - any request failure: split, or report the ids
        if len(addresses) <= _CENSUS_MIN_SPLIT:
            return {}, set(addresses)
        ids = list(addresses)
        half = len(ids) // 2
        a, fa = _census_with_split({k: addresses[k] for k in ids[:half]})
        b, fb = _census_with_split({k: addresses[k] for k in ids[half:]})
        return {**a, **b}, fa | fb


def geocode_census_batch(addresses: dict[str, str]) -> dict[str, dict]:
    """addresses: {unique_id: full_address}, at most _CENSUS_BATCH_CHUNK_SIZE
    entries (callers chunk larger sets). Returns {unique_id: {"lat":..,
    "lng":.., "matched_address":..}} -- raw, unvalidated -- for every id
    Census matched. Ids that came back No_Match/Tie, or that Census's
    response omits entirely (rare malformed rows), are simply absent from
    the result; the caller treats a missing id the same as a single-address
    miss and falls through to Nominatim."""
    if not addresses:
        return {}
    if len(addresses) > _CENSUS_BATCH_CHUNK_SIZE:
        raise ValueError(
            f"geocode_census_batch() called with {len(addresses)} addresses, "
            f"over the {_CENSUS_BATCH_CHUNK_SIZE} chunk size -- caller must chunk"
        )

    rows = []
    for uid, addr in addresses.items():
        street, city, state, zip_code = _split_address_for_batch(addr)
        rows.append((uid, street, city, state, zip_code))

    csv_text = _post_census_batch(rows)
    results: dict[str, dict] = {}
    for row in csv.reader(io.StringIO(csv_text)):
        if len(row) < 3:
            continue
        uid, _input_addr, match_status = row[0], row[1], row[2]
        if match_status == "Match" and len(row) >= 6:
            matched_addr = row[4]
            lng_str, _, lat_str = row[5].partition(",")
            try:
                lng, lat = float(lng_str), float(lat_str)
            except ValueError:
                continue
            results[uid] = {"lat": lat, "lng": lng, "matched_address": matched_addr}
    return results


def _persist_geocode_result(conn: sqlite3.Connection, normalized_address: str, result: GeocodeResult) -> None:
    conn.execute(
        """INSERT INTO geocode_cache
               (normalized_address, lat, lng, matched_address, provider,
                confidence, status, error_message, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(normalized_address) DO UPDATE SET
               lat=excluded.lat, lng=excluded.lng,
               matched_address=excluded.matched_address,
               provider=excluded.provider, confidence=excluded.confidence,
               status=excluded.status, error_message=excluded.error_message,
               updated_at=excluded.updated_at""",
        (
            normalized_address, result.lat, result.lng, result.matched_address, result.provider,
            result.confidence, result.status, result.error, now_iso(),
        ),
    )


def _apply_result_to_permits(conn: sqlite3.Connection, result: GeocodeResult, permit_ids: list[int]) -> None:
    """Direct UPDATE, not the precedence-aware upsert -- safe because every
    caller here only ever selects rows with lat IS NULL to begin with (see
    each entry point's WHERE clause), so there is never an existing
    coordinate to protect against downgrading."""
    ts = now_iso()
    if result.matched:
        for pid in permit_ids:
            conn.execute(
                """UPDATE permits SET
                       lat=?, lng=?, geocoded_address=?, coordinate_source=?,
                       geocode_status='matched', geocode_provider=?,
                       geocode_confidence=?, geocode_attempted_at=?, geocode_error=NULL
                   WHERE id=?""",
                (result.lat, result.lng, result.matched_address, result.provider,
                 result.provider, result.confidence, ts, pid),
            )
    else:
        for pid in permit_ids:
            conn.execute(
                """UPDATE permits SET
                       geocode_status=?, geocode_provider=?, geocode_confidence=?,
                       geocode_attempted_at=?, geocode_error=?
                   WHERE id=?""",
                (result.status, result.provider, result.confidence, ts, result.error, pid),
            )


def _result_from_cache_row(cached: sqlite3.Row) -> GeocodeResult:
    return GeocodeResult(
        lat=cached["lat"], lng=cached["lng"], matched_address=cached["matched_address"],
        provider=cached["provider"], confidence=cached["confidence"], status=cached["status"],
        matched=cached["status"] == "matched", error=cached["error_message"], from_cache=True,
    )


def geocode_missing(
    conn: sqlite3.Connection,
    locality: str | None = None,
    limit: int | None = None,
    courtesy_pace_seconds: float = 0.2,
    force: bool = False,
    nominatim_limiter: NominatimRateLimiter | None = None,
) -> dict:
    """Row-by-row incremental geocoder for small daily syncs (run_ingest.py).
    Only ever selects rows with lat IS NULL, so a re-run naturally skips
    everything already resolved. Commits per row -- crash-safe. For large
    backlogs, use geocode_backlog_batch() instead (Census batch geocoding
    turns thousands of individual calls into a handful of requests)."""
    limiter = nominatim_limiter or _nominatim_limiter
    retry_cutoff = (date.today() - timedelta(days=RETRY_INTERVAL_DAYS)).isoformat()

    # Deliberately NOT filtering out NULL/empty addresses here -- doing so
    # would let those permits sit silently in geocode_status='pending'
    # forever, since nothing else would ever select them again either. The
    # _looks_like_street_address() check just below already returns False
    # for None/empty, correctly routing them to the honest 'no_address'
    # classification instead (confirmed live: 829 real Virginia Beach
    # permits, mostly HAUL-type, have no address field in the source data
    # at all -- this was previously invisible, silently stuck as "pending").
    where = ["lat IS NULL"]
    params: list = []
    if locality:
        where.append("locality = ?")
        params.append(locality)
    if not force:
        where.append("(geocode_status = 'pending' OR geocode_attempted_at IS NULL OR geocode_attempted_at < ?)")
        params.append(retry_cutoff)

    query = f"SELECT id, locality, address FROM permits WHERE {' AND '.join(where)} ORDER BY id"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    counts = {"attempted": 0, "matched": 0, "cache_hit": 0, "failed": 0, "no_address": 0}

    for row in rows:
        counts["attempted"] += 1

        if not _looks_like_street_address(row["address"]):
            conn.execute(
                """UPDATE permits SET
                       geocode_status='no_address', geocode_provider=NULL,
                       geocode_confidence=NULL, geocode_attempted_at=?, geocode_error=?
                   WHERE id=?""",
                (now_iso(), "source record has no usable street address", row["id"]),
            )
            counts["no_address"] += 1
            conn.commit()
            continue

        normalized = _normalize_address(row["address"])
        cached = conn.execute(
            "SELECT * FROM geocode_cache WHERE normalized_address = ?", (normalized,)
        ).fetchone()
        # force bypasses a stale cached *failure* (the whole point of --force
        # is to retry known failures) but still reuses a cached *success*.
        if cached is not None and force and cached["status"] != "matched":
            cached = None

        if cached is not None:
            counts["cache_hit"] += 1
            outcome = _result_from_cache_row(cached)
        else:
            outcome = geocode_address(row["address"], locality=row["locality"], nominatim_limiter=limiter)
            _persist_geocode_result(conn, normalized, outcome)
            # Nominatim's pacing already happened inside geocode_address()
            # via the rate limiter; this is just a light courtesy pause on
            # the (much more common) Census-only path, which has no
            # documented per-request rate limit but shouldn't be hammered
            # in a tight per-row loop either.
            time.sleep(courtesy_pace_seconds)

        _apply_result_to_permits(conn, outcome, [row["id"]])
        counts["matched" if outcome.matched else "failed"] += 1
        conn.commit()

    return counts


def geocode_backlog_batch(
    conn: sqlite3.Connection,
    locality: str | None = None,
    limit: int | None = None,
    census_chunk_size: int = _CENSUS_BATCH_CHUNK_SIZE,
    force: bool = False,
    nominatim_limiter: NominatimRateLimiter | None = None,
    census_only: bool = False,
) -> dict:
    """Batch-first backlog geocoder for large catch-up runs. Flow:
      pending permits -> dedupe by normalized address -> cache lookup
      -> unique addresses not yet cached go through Census's batch endpoint
      (chunked) -> only Census misses/rejections fall through to Nominatim,
      sequential and rate-limited -> persist + apply to every permit row
      sharing that address.

    Resumable: commits after (1) no_address classification, (2) cache-hit
    application, (3) each Census chunk, and (4) every individual Nominatim
    call -- an interrupted run never redoes already-committed work and
    never loses more than the current chunk/call in flight. Never creates
    permit rows (only UPDATEs by id), so no duplicate-row risk. Only ever
    selects rows with lat IS NULL, so existing coordinates (manual, native,
    or previously geocoded) are never touched, regardless of `force`.
    """
    limiter = nominatim_limiter or _nominatim_limiter
    started = time.time()

    mapped_before = _mapped_count(conn, locality)

    retry_cutoff = (date.today() - timedelta(days=RETRY_INTERVAL_DAYS)).isoformat()
    # Deliberately NOT filtering out NULL/empty addresses here -- doing so
    # would let those permits sit silently in geocode_status='pending'
    # forever, since nothing else would ever select them again either. The
    # _looks_like_street_address() check just below already returns False
    # for None/empty, correctly routing them to the honest 'no_address'
    # classification instead (confirmed live: 829 real Virginia Beach
    # permits, mostly HAUL-type, have no address field in the source data
    # at all -- this was previously invisible, silently stuck as "pending").
    where = ["lat IS NULL"]
    params: list = []
    if locality:
        where.append("locality = ?")
        params.append(locality)
    if not force:
        where.append("(geocode_status = 'pending' OR geocode_attempted_at IS NULL OR geocode_attempted_at < ?)")
        params.append(retry_cutoff)
    query = f"SELECT id, locality, address FROM permits WHERE {' AND '.join(where)} ORDER BY id"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()

    counts = {
        "total_pending_considered": len(rows),
        "no_address": 0,
        "cache_hits": 0,
        "unique_addresses_sent_to_census": 0,
        "census_matched": 0,
        "census_rejected": 0,
        "census_not_found": 0,
        "nominatim_attempted": 0,
        "nominatim_matched": 0,
        "nominatim_rejected": 0,
        "nominatim_not_found": 0,
        "errors": 0,
        "newly_mapped": 0,
    }
    per_locality: dict[str, dict] = {}

    def _bump(loc: str, key: str, n: int = 1) -> None:
        bucket = per_locality.setdefault(loc, dict.fromkeys(counts, 0))
        bucket[key] = bucket.get(key, 0) + n

    # Step 1: split off no-address rows immediately (no network, no cache),
    # dedupe the rest by normalized address.
    address_groups: dict[str, dict] = {}
    for row in rows:
        _bump(row["locality"], "total_pending_considered")
        if not _looks_like_street_address(row["address"]):
            conn.execute(
                """UPDATE permits SET
                       geocode_status='no_address', geocode_provider=NULL,
                       geocode_confidence=NULL, geocode_attempted_at=?, geocode_error=?
                   WHERE id=?""",
                (now_iso(), "source record has no usable street address", row["id"]),
            )
            counts["no_address"] += 1
            _bump(row["locality"], "no_address")
            continue

        normalized = _normalize_address(row["address"])
        group = address_groups.setdefault(
            normalized, {"address": row["address"], "locality": row["locality"], "permit_ids": []}
        )
        group["permit_ids"].append(row["id"])
    conn.commit()  # checkpoint: no_address classifications are durable

    # Step 2: resolve from cache where possible.
    to_geocode: dict[str, str] = {}  # normalized -> raw address
    for normalized, group in address_groups.items():
        cached = conn.execute(
            "SELECT * FROM geocode_cache WHERE normalized_address = ?", (normalized,)
        ).fetchone()
        if cached is not None and not (force and cached["status"] != "matched"):
            counts["cache_hits"] += 1
            _bump(group["locality"], "cache_hits")
            result = _result_from_cache_row(cached)
            _apply_result_to_permits(conn, result, group["permit_ids"])
            if result.matched:
                counts["newly_mapped"] += len(group["permit_ids"])
                _bump(group["locality"], "newly_mapped", len(group["permit_ids"]))
            continue
        to_geocode[normalized] = group["address"]
    conn.commit()  # checkpoint: all cache-hit applications are durable

    counts["unique_addresses_sent_to_census"] = len(to_geocode)

    # Step 3: Census batch, one chunk at a time, committed per chunk so an
    # interruption never loses more than one chunk's worth of work.
    to_nominatim: dict[str, str] = {}
    to_geocode_ids = list(to_geocode.keys())
    for i in range(0, len(to_geocode_ids), census_chunk_size):
        chunk_ids = to_geocode_ids[i:i + census_chunk_size]
        chunk_addresses = {nid: to_geocode[nid] for nid in chunk_ids}
        # a failed request is not "no match": split and retry; what still fails is not recorded as a miss
        census_raw, failed = _census_with_split(chunk_addresses)
        if failed:
            counts["errors"] += 1
            counts["census_failed"] = counts.get("census_failed", 0) + len(failed)

        for normalized in chunk_ids:
            if normalized in failed:  # never recorded as a Census miss; Nominatim may still resolve it
                to_nominatim[normalized] = chunk_addresses[normalized]
                continue
            group = address_groups[normalized]
            raw = census_raw.get(normalized)
            if raw is not None:
                result = _finalize_geocode(raw["lat"], raw["lng"], raw["matched_address"], "census", group["locality"])
            else:
                result = GeocodeResult(
                    lat=None, lng=None, matched_address=None, provider="census", confidence=None,
                    status="not_found", matched=False, error="Census batch: no match",
                )

            _persist_geocode_result(conn, normalized, result)
            _apply_result_to_permits(conn, result, group["permit_ids"])

            if result.matched:
                counts["census_matched"] += 1
                _bump(group["locality"], "census_matched")
                counts["newly_mapped"] += len(group["permit_ids"])
                _bump(group["locality"], "newly_mapped", len(group["permit_ids"]))
            elif result.status in ("rejected_outside_area", "locality_mismatch"):
                counts["census_rejected"] += 1
                _bump(group["locality"], "census_rejected")
                to_nominatim[normalized] = chunk_addresses[normalized]
            else:
                counts["census_not_found"] += 1
                _bump(group["locality"], "census_not_found")
                to_nominatim[normalized] = chunk_addresses[normalized]
        conn.commit()  # checkpoint: this chunk's results are durable

    if census_only:
        to_nominatim = {}

    # Step 4: sequential, rate-limited Nominatim fallback for Census
    # misses/rejections only -- never parallel, never a retry storm.
    for normalized, raw_address in to_nominatim.items():
        group = address_groups[normalized]
        limiter.wait()
        counts["nominatim_attempted"] += 1
        _bump(group["locality"], "nominatim_attempted")

        nominatim_raw = _geocode_via_nominatim(raw_address)
        if nominatim_raw is not None:
            result = _finalize_geocode(
                nominatim_raw["lat"], nominatim_raw["lng"], nominatim_raw["matched_address"],
                "nominatim", group["locality"],
            )
        else:
            result = GeocodeResult(
                lat=None, lng=None, matched_address=None, provider="nominatim", confidence=None,
                status="not_found", matched=False, error="no geocoder returned a match",
            )

        _persist_geocode_result(conn, normalized, result)
        _apply_result_to_permits(conn, result, group["permit_ids"])

        if result.matched:
            counts["nominatim_matched"] += 1
            _bump(group["locality"], "nominatim_matched")
            counts["newly_mapped"] += len(group["permit_ids"])
            _bump(group["locality"], "newly_mapped", len(group["permit_ids"]))
        elif result.status in ("rejected_outside_area", "locality_mismatch"):
            counts["nominatim_rejected"] += 1
            _bump(group["locality"], "nominatim_rejected")
        else:
            counts["nominatim_not_found"] += 1
            _bump(group["locality"], "nominatim_not_found")
        conn.commit()  # checkpoint: crash-safe, never redoes a paced call

    counts["elapsed_seconds"] = round(time.time() - started, 1)
    counts["mapped_before"] = mapped_before
    counts["mapped_after"] = _mapped_count(conn, locality)
    counts["remaining_pending"] = _pending_count(conn, locality)
    counts["per_locality"] = per_locality
    return counts


def _mapped_count(conn: sqlite3.Connection, locality: str | None = None) -> int:
    if locality:
        return conn.execute(
            "SELECT COUNT(*) FROM permits WHERE locality = ? AND lat IS NOT NULL", (locality,)
        ).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM permits WHERE lat IS NOT NULL").fetchone()[0]


def _pending_count(conn: sqlite3.Connection, locality: str | None = None) -> int:
    where = "lat IS NULL AND address IS NOT NULL AND address != ''"
    if locality:
        return conn.execute(
            f"SELECT COUNT(*) FROM permits WHERE {where} AND locality = ?", (locality,)
        ).fetchone()[0]
    return conn.execute(f"SELECT COUNT(*) FROM permits WHERE {where}").fetchone()[0]


def analyze_unmapped(conn: sqlite3.Connection, locality: str | None = None) -> dict:
    """Classifies remaining unmapped permits by why they're unmapped --
    trustworthy coverage over an arbitrary percentage target. Read-only,
    never fabricates or infers a coordinate."""
    where = "WHERE lat IS NULL"
    params: list = []
    if locality:
        where += " AND locality = ?"
        params.append(locality)

    by_status = dict(
        conn.execute(f"SELECT geocode_status, COUNT(*) FROM permits {where} GROUP BY geocode_status", params)
    )
    never_attempted = conn.execute(
        f"SELECT COUNT(*) FROM permits {where} AND geocode_attempted_at IS NULL", params
    ).fetchone()[0]
    empty_address_field = conn.execute(
        f"SELECT COUNT(*) FROM permits {where} AND (address IS NULL OR address = '')", params
    ).fetchone()[0]
    sample_not_found = [
        dict(r) for r in conn.execute(
            f"SELECT source_permit_id, address FROM permits {where} AND geocode_status='not_found' LIMIT 10",
            params,
        )
    ]

    return {
        "by_geocode_status": by_status,
        "never_attempted": never_attempted,
        "empty_address_field": empty_address_field,
        "sample_not_found_addresses": sample_not_found,
    }
