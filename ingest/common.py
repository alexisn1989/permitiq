"""Shared ingestion infrastructure: the normalized permit model, retry/backoff
fetch helper, date normalization, coordinate-precedence upsert, and ingestion
run tracking. Locality adapters (ingest/norfolk.py etc.) depend on this module
but never on each other.

Coordinate precedence (highest wins, never downgraded on re-ingest):
    manual > native > census > nominatim > (missing)
A locality adapter that has no native geometry leaves lat/lng/coordinate_source
unset (None) — the geocode pass fills them in later. A re-ingest must never
erase a coordinate the DB already holds; it may only replace it with an
equal-or-higher-precedence one.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable

import requests

from db import STATUS_BUCKETS, now_iso

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_COORDINATE_RANK = {"manual": 4, "native": 3, "census": 2, "nominatim": 1}


class SourceSchemaError(Exception):
    """Raised when a locality source's live response no longer matches the
    fields the adapter was built against. Never silently ingest degraded
    records — surface this and fail the ingestion run instead."""


class SourceUnavailableError(Exception):
    """Raised by an adapter for a locality with no verified point-level
    permit-records source yet (e.g. Chesapeake, Portsmouth in v1). --locality
    all must catch this and skip the locality with a warning, not crash."""


def validate_required_fields(record: dict, required: Iterable[str], source_name: str) -> None:
    """Raise SourceSchemaError if any required key is absent from a raw
    (pre-normalization) source record. Presence, not truthiness -- a field
    that is legitimately empty for this record is fine; a field that
    disappeared from the schema entirely is not."""
    missing = [f for f in required if f not in record]
    if missing:
        raise SourceSchemaError(
            f"{source_name}: response is missing required field(s) {missing} -- "
            "source schema may have changed, refusing to ingest"
        )


def detect_duplicate_source_ids(raw_records: list[dict], id_field: str, source_name: str) -> list:
    """Return every raw record that shares an id_field value with another
    record in the same fetch (including the first occurrence). Callers
    report/skip these rather than silently upserting one and dropping data
    from the other."""
    first_index: dict = {}
    dupes: list = []
    already_flagged: set = set()
    for i, rec in enumerate(raw_records):
        key = rec.get(id_field)
        if key in first_index:
            if key not in already_flagged:
                dupes.append(raw_records[first_index[key]])
                already_flagged.add(key)
            dupes.append(rec)
        else:
            first_index[key] = i
    return dupes


def normalize_date(value) -> str | None:
    """Accepts ISO datetime strings (optionally with fractional seconds),
    'YYYY/MM/DD' (Virginia Beach's ApplicationDate format), plain
    'YYYY-MM-DD', or an ArcGIS-style epoch-millisecond integer. Returns
    'YYYY-MM-DD' or None if unparseable -- never raises on bad input, since a
    single malformed date shouldn't sink an entire ingest."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None
    return None


def fetch_json_retry(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    tries: int = 3,
    timeout: int = 60,
) -> dict:
    """Retry/backoff GET, ported from Hampton_Roads_Flood_Map/prepare_data.py's
    fetch_json() -- run manually/on a schedule by run_ingest.py, never at
    server startup or from an HTTP request path."""
    last_err = None
    for attempt in range(tries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as err:  # noqa: BLE001 - retry any transient failure
            last_err = err
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


@dataclass
class NormalizedPermit:
    locality: str
    source_permit_id: str
    source_name: str
    source_url: str | None = None
    source_record_url: str | None = None
    source_updated_at: str | None = None

    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    geocoded_address: str | None = None
    coordinate_source: str | None = None  # manual | native | census | nominatim | None

    geocode_status: str = "pending"
    geocode_provider: str | None = None
    geocode_confidence: str | None = None
    geocode_attempted_at: str | None = None
    geocode_error: str | None = None

    permit_type: str | None = None
    status: str | None = None
    status_bucket: str = "other"

    applied_date: str | None = None
    issued_date: str | None = None
    finaled_date: str | None = None
    expiration_date: str | None = None

    valuation: float | None = None
    description: str | None = None
    contractor: str | None = None
    applicant: str | None = None

    raw: dict = field(default_factory=dict)

    def validation_errors(self) -> list[str]:
        errors = []
        if not self.locality or not self.locality.strip():
            errors.append("locality is required")
        if not self.source_permit_id or not str(self.source_permit_id).strip():
            errors.append("source_permit_id is required")
        if not self.source_name or not self.source_name.strip():
            errors.append("source_name is required")
        if self.status_bucket not in STATUS_BUCKETS:
            errors.append(f"status_bucket '{self.status_bucket}' not in {STATUS_BUCKETS}")

        has_lat, has_lng = self.lat is not None, self.lng is not None
        if has_lat != has_lng:
            errors.append("lat and lng must both be present or both be absent")
        elif has_lat:
            if not isinstance(self.lat, (int, float)) or not isinstance(self.lng, (int, float)):
                errors.append("lat/lng must be numeric")
            elif not (-90.0 <= self.lat <= 90.0) or not (-180.0 <= self.lng <= 180.0):
                errors.append(f"lat/lng out of range: ({self.lat}, {self.lng})")

        for field_name in ("applied_date", "issued_date", "finaled_date", "expiration_date"):
            val = getattr(self, field_name)
            if val is not None and not _ISO_DATE_RE.match(str(val)):
                errors.append(f"{field_name} '{val}' is not in YYYY-MM-DD format")

        try:
            json.dumps(self.raw)
        except (TypeError, ValueError):
            errors.append("raw is not JSON-serializable")

        return errors


_UPSERT_SQL = """
INSERT INTO permits (
    locality, source_permit_id, source_name, source_url, source_record_url,
    source_updated_at, address, lat, lng, geocoded_address, coordinate_source,
    geocode_status, geocode_provider, geocode_confidence, geocode_attempted_at,
    geocode_error, permit_type, status, status_bucket, applied_date,
    issued_date, finaled_date, expiration_date, valuation, description,
    contractor, applicant, raw_json, first_seen_at, ingested_at
) VALUES (
    :locality, :source_permit_id, :source_name, :source_url, :source_record_url,
    :source_updated_at, :address, :lat, :lng, :geocoded_address, :coordinate_source,
    :geocode_status, :geocode_provider, :geocode_confidence, :geocode_attempted_at,
    :geocode_error, :permit_type, :status, :status_bucket, :applied_date,
    :issued_date, :finaled_date, :expiration_date, :valuation, :description,
    :contractor, :applicant, :raw_json, :ts, :ts
)
ON CONFLICT(locality, source_permit_id) DO UPDATE SET
    source_name = excluded.source_name,
    source_url = excluded.source_url,
    source_record_url = excluded.source_record_url,
    source_updated_at = excluded.source_updated_at,
    address = excluded.address,
    permit_type = excluded.permit_type,
    status = excluded.status,
    status_bucket = excluded.status_bucket,
    applied_date = excluded.applied_date,
    issued_date = excluded.issued_date,
    finaled_date = excluded.finaled_date,
    expiration_date = excluded.expiration_date,
    valuation = excluded.valuation,
    description = excluded.description,
    contractor = excluded.contractor,
    applicant = excluded.applicant,
    raw_json = excluded.raw_json,
    ingested_at = :ts,
    lat = CASE
        WHEN permits.coordinate_source = 'manual' THEN permits.lat
        WHEN excluded.coordinate_source IS NULL THEN permits.lat
        WHEN permits.coordinate_source IS NULL THEN excluded.lat
        WHEN coord_rank(excluded.coordinate_source) >= coord_rank(permits.coordinate_source) THEN excluded.lat
        ELSE permits.lat
    END,
    lng = CASE
        WHEN permits.coordinate_source = 'manual' THEN permits.lng
        WHEN excluded.coordinate_source IS NULL THEN permits.lng
        WHEN permits.coordinate_source IS NULL THEN excluded.lng
        WHEN coord_rank(excluded.coordinate_source) >= coord_rank(permits.coordinate_source) THEN excluded.lng
        ELSE permits.lng
    END,
    geocoded_address = CASE
        WHEN permits.coordinate_source = 'manual' THEN permits.geocoded_address
        WHEN excluded.coordinate_source IS NULL THEN permits.geocoded_address
        WHEN permits.coordinate_source IS NULL THEN excluded.geocoded_address
        WHEN coord_rank(excluded.coordinate_source) >= coord_rank(permits.coordinate_source) THEN excluded.geocoded_address
        ELSE permits.geocoded_address
    END,
    coordinate_source = CASE
        WHEN permits.coordinate_source = 'manual' THEN permits.coordinate_source
        WHEN excluded.coordinate_source IS NULL THEN permits.coordinate_source
        WHEN permits.coordinate_source IS NULL THEN excluded.coordinate_source
        WHEN coord_rank(excluded.coordinate_source) >= coord_rank(permits.coordinate_source) THEN excluded.coordinate_source
        ELSE permits.coordinate_source
    END,
    geocode_status = CASE
        WHEN permits.coordinate_source = 'manual' THEN permits.geocode_status
        WHEN excluded.coordinate_source IS NULL THEN permits.geocode_status
        WHEN permits.coordinate_source IS NULL THEN excluded.geocode_status
        WHEN coord_rank(excluded.coordinate_source) >= coord_rank(permits.coordinate_source) THEN excluded.geocode_status
        ELSE permits.geocode_status
    END,
    geocode_provider = CASE
        WHEN permits.coordinate_source = 'manual' THEN permits.geocode_provider
        WHEN excluded.coordinate_source IS NULL THEN permits.geocode_provider
        WHEN permits.coordinate_source IS NULL THEN excluded.geocode_provider
        WHEN coord_rank(excluded.coordinate_source) >= coord_rank(permits.coordinate_source) THEN excluded.geocode_provider
        ELSE permits.geocode_provider
    END,
    geocode_confidence = CASE
        WHEN permits.coordinate_source = 'manual' THEN permits.geocode_confidence
        WHEN excluded.coordinate_source IS NULL THEN permits.geocode_confidence
        WHEN permits.coordinate_source IS NULL THEN excluded.geocode_confidence
        WHEN coord_rank(excluded.coordinate_source) >= coord_rank(permits.coordinate_source) THEN excluded.geocode_confidence
        ELSE permits.geocode_confidence
    END,
    geocode_attempted_at = CASE
        WHEN permits.coordinate_source = 'manual' THEN permits.geocode_attempted_at
        WHEN excluded.coordinate_source IS NULL THEN permits.geocode_attempted_at
        WHEN permits.coordinate_source IS NULL THEN excluded.geocode_attempted_at
        WHEN coord_rank(excluded.coordinate_source) >= coord_rank(permits.coordinate_source) THEN excluded.geocode_attempted_at
        ELSE permits.geocode_attempted_at
    END,
    geocode_error = CASE
        WHEN permits.coordinate_source = 'manual' THEN permits.geocode_error
        WHEN excluded.coordinate_source IS NULL THEN permits.geocode_error
        WHEN permits.coordinate_source IS NULL THEN excluded.geocode_error
        WHEN coord_rank(excluded.coordinate_source) >= coord_rank(permits.coordinate_source) THEN excluded.geocode_error
        ELSE permits.geocode_error
    END
WHERE
    permits.source_name IS NOT excluded.source_name OR
    permits.source_url IS NOT excluded.source_url OR
    permits.source_record_url IS NOT excluded.source_record_url OR
    permits.source_updated_at IS NOT excluded.source_updated_at OR
    permits.address IS NOT excluded.address OR
    permits.permit_type IS NOT excluded.permit_type OR
    permits.status IS NOT excluded.status OR
    permits.status_bucket IS NOT excluded.status_bucket OR
    permits.applied_date IS NOT excluded.applied_date OR
    permits.issued_date IS NOT excluded.issued_date OR
    permits.finaled_date IS NOT excluded.finaled_date OR
    permits.expiration_date IS NOT excluded.expiration_date OR
    permits.valuation IS NOT excluded.valuation OR
    permits.description IS NOT excluded.description OR
    permits.contractor IS NOT excluded.contractor OR
    permits.applicant IS NOT excluded.applicant OR
    permits.raw_json IS NOT excluded.raw_json OR
    (
        permits.coordinate_source IS NOT 'manual' AND excluded.coordinate_source IS NOT NULL AND
        (permits.coordinate_source IS NULL OR coord_rank(excluded.coordinate_source) >= coord_rank(permits.coordinate_source)) AND
        (permits.lat IS NOT excluded.lat OR permits.lng IS NOT excluded.lng OR permits.coordinate_source IS NOT excluded.coordinate_source)
    )
"""


def _coord_rank(source: str | None) -> int:
    return _COORDINATE_RANK.get(source, 0)


def upsert_permits(conn: sqlite3.Connection, records: Iterable[NormalizedPermit]) -> dict:
    """Idempotent upsert with coordinate precedence (see module docstring).
    Returns {"inserted": n, "updated": n, "unchanged": n, "skipped": n} --
    'skipped' covers both validation failures and in-batch duplicate IDs.
    Never counts a no-op conflict as an update."""
    conn.create_function("coord_rank", 1, _coord_rank)

    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    seen_keys: set[tuple[str, str]] = set()

    for permit in records:
        errors = permit.validation_errors()
        if errors:
            counts["skipped"] += 1
            continue

        key = (permit.locality, permit.source_permit_id)
        if key in seen_keys:
            counts["skipped"] += 1
            continue
        seen_keys.add(key)

        existing = conn.execute(
            "SELECT 1 FROM permits WHERE locality = ? AND source_permit_id = ?", key
        ).fetchone()

        params = {
            "locality": permit.locality,
            "source_permit_id": permit.source_permit_id,
            "source_name": permit.source_name,
            "source_url": permit.source_url,
            "source_record_url": permit.source_record_url,
            "source_updated_at": permit.source_updated_at,
            "address": permit.address,
            "lat": permit.lat,
            "lng": permit.lng,
            "geocoded_address": permit.geocoded_address,
            "coordinate_source": permit.coordinate_source,
            "geocode_status": permit.geocode_status,
            "geocode_provider": permit.geocode_provider,
            "geocode_confidence": permit.geocode_confidence,
            "geocode_attempted_at": permit.geocode_attempted_at,
            "geocode_error": permit.geocode_error,
            "permit_type": permit.permit_type,
            "status": permit.status,
            "status_bucket": permit.status_bucket,
            "applied_date": permit.applied_date,
            "issued_date": permit.issued_date,
            "finaled_date": permit.finaled_date,
            "expiration_date": permit.expiration_date,
            "valuation": permit.valuation,
            "description": permit.description,
            "contractor": permit.contractor,
            "applicant": permit.applicant,
            "raw_json": json.dumps(permit.raw),
            "ts": now_iso(),
        }

        cur = conn.execute(_UPSERT_SQL, params)

        if existing is None:
            counts["inserted"] += 1
        elif cur.rowcount > 0:
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1

    return counts


def start_ingestion_run(conn: sqlite3.Connection, locality: str, since_date: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO ingestion_runs (locality, started_at, status, since_date) VALUES (?, ?, 'running', ?)",
        (locality, now_iso(), since_date),
    )
    conn.commit()
    return cur.lastrowid


def finish_ingestion_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    fetched_count: int = 0,
    counts: dict | None = None,
    geocoded_count: int = 0,
    error_count: int = 0,
    error_message: str | None = None,
) -> None:
    # Note: counts.get("unchanged") deliberately has no column of its own --
    # unchanged rows are neither "updated" (no values changed) nor "skipped"
    # (they were valid); they're implicitly fetched - inserted - updated - skipped.
    counts = counts or {}
    conn.execute(
        """UPDATE ingestion_runs SET
               completed_at = ?, status = ?, fetched_count = ?,
               inserted_count = ?, updated_count = ?, skipped_count = ?,
               geocoded_count = ?, error_count = ?, error_message = ?
           WHERE id = ?""",
        (
            now_iso(),
            status,
            fetched_count,
            counts.get("inserted", 0),
            counts.get("updated", 0),
            counts.get("skipped", 0),
            geocoded_count,
            error_count,
            error_message,
            run_id,
        ),
    )
    conn.commit()
