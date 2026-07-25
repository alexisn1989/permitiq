"""PermitIQ read-only API.

Permits are synced exclusively by run_ingest.py (see that file's docstring
for why ingestion never runs from an HTTP path or app startup). This module
must never import run_ingest.py and exposes no POST/PUT/PATCH/DELETE routes
on /api/permits -- it is a read/filter/stats/export layer over data that is
always synced out-of-band.

All filtering goes through the parameterized build_filter() below. The only
SQL identifiers that are ever interpolated directly (never parameterized,
since SQLite doesn't allow binding column/direction names) are the sort
column and sort direction -- both validated against small fixed allowlists
before use, so no unvalidated string ever reaches the SQL string.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import db as db_module

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="PermitIQ", version="1.0.0")
db_module.init_db()  # schema-only DDL, safe at import time -- see db.py docstring

# ---------------------------------------------------------------------------
# Locality coverage metadata -- product/coverage decisions, not derived from
# data. This is the single source of truth for availability states shown in
# /api/facets and /api/data-status.
# ---------------------------------------------------------------------------
LOCALITY_INFO = {
    "norfolk": {
        "label": "Norfolk", "availability": "available",
        "source_name": "Norfolk Open Data Portal",
    },
    "virginia_beach": {
        "label": "Virginia Beach", "availability": "available",
        "source_name": "Virginia Beach Building Permit Applications (ArcGIS Hub)",
    },
    "chesapeake": {"label": "Chesapeake", "availability": "source_under_review", "source_name": None},
    "portsmouth": {"label": "Portsmouth", "availability": "source_under_review", "source_name": None},
    "hampton": {"label": "Hampton", "availability": "unavailable", "source_name": None},
    "suffolk": {"label": "Suffolk", "availability": "unavailable", "source_name": None},
    "newport_news": {"label": "Newport News", "availability": "unavailable", "source_name": None},
}
SUPPORTED_LOCALITIES = tuple(k for k, v in LOCALITY_INFO.items() if v["availability"] == "available")
ALL_LOCALITY_KEYS = tuple(LOCALITY_INFO.keys())

SORTABLE_COLUMNS = (
    "issued_date", "applied_date", "finaled_date", "expiration_date",
    "permit_type", "status", "locality", "source_permit_id", "id",
)
SORT_ORDERS = ("asc", "desc")

# Explicit allowlist of columns ever returned by /api/permits* -- raw_json is
# deliberately excluded here, not filtered out after the fact, so there is no
# path (typo, refactor, etc.) by which it could leak.
PERMIT_FIELDS = (
    "id", "locality", "source_permit_id", "source_name", "source_url",
    "source_record_url", "source_updated_at", "address", "lat", "lng",
    "geocoded_address", "coordinate_source", "geocode_status",
    "permit_type", "status", "status_bucket", "applied_date", "issued_date",
    "finaled_date", "expiration_date", "valuation", "description",
    "contractor", "applicant", "first_seen_at", "ingested_at",
)

EXPORT_FIELDS = (
    "id", "source_permit_id", "locality", "permit_type", "status", "status_bucket",
    "address", "applied_date", "issued_date", "finaled_date", "expiration_date",
    "valuation", "description", "contractor", "source_name", "source_url",
    "source_record_url", "lat", "lng",
)

MAX_EXPORT_FEATURES = 25000
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(value: Optional[str], field_name: str) -> None:
    if value is not None and not _ISO_DATE_RE.match(value):
        raise HTTPException(422, f"{field_name} must be in YYYY-MM-DD format (got {value!r})")


def _append_condition(where: str, condition: str) -> str:
    """Appends an additional AND-ed condition to a WHERE clause that may
    already be empty (build_filter returns "" when no filters apply)."""
    return f"{where} AND {condition}" if where else f"WHERE {condition}"


def build_filter(
    locality: Optional[str] = None,
    permit_type: Optional[str] = None,
    status: Optional[str] = None,
    status_bucket: Optional[str] = None,
    q: Optional[str] = None,
    issued_from: Optional[str] = None,
    issued_to: Optional[str] = None,
) -> tuple[str, list]:
    """Shared, fully-parameterized WHERE builder used by /api/permits,
    /api/stats, and /api/export.geojson so filter semantics can never drift
    between endpoints. Adapted from AssetIQ's build_filter() (assetiq/app.py).

    permit_type and status are intentionally NOT validated against a fixed
    enum -- unlike locality and status_bucket, their vocabulary is
    heterogeneous per source (see ingest/norfolk.py and
    ingest/virginia_beach.py's STATUS_BUCKET_MAP comments) and passing an
    unknown value here should simply match zero rows, not error.
    """
    clauses: list[str] = []
    params: list = []

    if locality:
        if locality not in ALL_LOCALITY_KEYS:
            raise HTTPException(422, f"unknown locality '{locality}'")
        clauses.append("locality = ?")
        params.append(locality)
    if permit_type:
        clauses.append("permit_type = ?")
        params.append(permit_type)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if status_bucket:
        if status_bucket not in db_module.STATUS_BUCKETS:
            raise HTTPException(422, f"unknown status_bucket '{status_bucket}'")
        clauses.append("status_bucket = ?")
        params.append(status_bucket)
    if q:
        needle = f"%{q.lower()}%"
        clauses.append(
            "(lower(address) LIKE ? OR lower(description) LIKE ? OR "
            "lower(contractor) LIKE ? OR lower(source_permit_id) LIKE ?)"
        )
        params.extend([needle, needle, needle, needle])
    if issued_from:
        _validate_date(issued_from, "issued_from")
        clauses.append("issued_date >= ?")
        params.append(issued_from)
    if issued_to:
        _validate_date(issued_to, "issued_to")
        clauses.append("issued_date <= ?")
        params.append(issued_to)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def row_to_permit_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in PERMIT_FIELDS}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness + schema check only -- never touches external APIs, never
    triggers ingestion or geocoding, never returns a filesystem path or
    traceback."""
    try:
        with db_module.db() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"permits", "ingestion_runs", "geocode_cache"}
        if not required.issubset(tables):
            return JSONResponse({"status": "error", "database": "schema_invalid"}, status_code=503)
        return {"status": "ok", "database": "ok"}
    except Exception:  # noqa: BLE001 - never leak the underlying exception
        return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)


@app.get("/api/permits")
def list_permits(
    locality: Optional[str] = None,
    permit_type: Optional[str] = None,
    status: Optional[str] = None,
    status_bucket: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    issued_from: Optional[str] = None,
    issued_to: Optional[str] = None,
    sort: Optional[str] = None,
    order: str = "desc",
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
):
    order = (order or "desc").lower()
    if sort is not None and sort not in SORTABLE_COLUMNS:
        raise HTTPException(422, f"unknown sort column '{sort}' -- must be one of {SORTABLE_COLUMNS}")
    if order not in SORT_ORDERS:
        raise HTTPException(422, f"order must be one of {SORT_ORDERS}")

    where, params = build_filter(locality, permit_type, status, status_bucket, q, issued_from, issued_to)

    if sort:
        order_sql = f"ORDER BY {sort} {order.upper()}, id DESC"
    else:
        order_sql = "ORDER BY COALESCE(issued_date, applied_date) DESC, id DESC"

    with db_module.db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM permits {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {', '.join(PERMIT_FIELDS)} FROM permits {where} {order_sql} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

    return {
        "count": total,
        "returned": len(rows),
        "limit": limit,
        "offset": offset,
        "permits": [row_to_permit_dict(r) for r in rows],
    }


@app.get("/api/permits/{permit_id}")
def get_permit(permit_id: int):
    with db_module.db() as conn:
        row = conn.execute(
            f"SELECT {', '.join(PERMIT_FIELDS)} FROM permits WHERE id = ?", (permit_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"permit {permit_id} not found")
    return row_to_permit_dict(row)


@app.get("/api/facets")
def facets():
    localities_out = [
        {"key": k, "label": v["label"], "availability": v["availability"]}
        for k, v in LOCALITY_INFO.items()
    ]

    permit_types: dict[str, list] = {}
    statuses: dict[str, list] = {}
    with db_module.db() as conn:
        for loc in SUPPORTED_LOCALITIES:
            permit_types[loc] = sorted(
                r[0] for r in conn.execute(
                    "SELECT DISTINCT permit_type FROM permits WHERE locality=? AND permit_type IS NOT NULL",
                    (loc,),
                )
            )
            statuses[loc] = sorted(
                r[0] for r in conn.execute(
                    "SELECT DISTINCT status FROM permits WHERE locality=? AND status IS NOT NULL",
                    (loc,),
                )
            )

    return {"localities": localities_out, "permit_types": permit_types, "statuses": statuses}


@app.get("/api/stats")
def stats(
    locality: Optional[str] = None,
    permit_type: Optional[str] = None,
    status: Optional[str] = None,
    status_bucket: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    issued_from: Optional[str] = None,
    issued_to: Optional[str] = None,
):
    where, params = build_filter(locality, permit_type, status, status_bucket, q, issued_from, issued_to)

    with db_module.db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM permits {where}", params).fetchone()[0]

        mapped_where = _append_condition(where, "lat IS NOT NULL")
        mapped = conn.execute(f"SELECT COUNT(*) FROM permits {mapped_where}", params).fetchone()[0]

        pending_where = _append_condition(where, "geocode_status = 'pending'")
        pending_geocode = conn.execute(f"SELECT COUNT(*) FROM permits {pending_where}", params).fetchone()[0]

        not_found_where = _append_condition(where, "geocode_status = 'not_found'")
        geocode_not_found = conn.execute(f"SELECT COUNT(*) FROM permits {not_found_where}", params).fetchone()[0]

        no_address_where = _append_condition(where, "geocode_status = 'no_address'")
        no_address = conn.execute(f"SELECT COUNT(*) FROM permits {no_address_where}", params).fetchone()[0]

        by_locality = dict(conn.execute(
            f"SELECT locality, COUNT(*) FROM permits {where} GROUP BY locality", params
        ).fetchall())
        by_status_bucket = dict(conn.execute(
            f"SELECT status_bucket, COUNT(*) FROM permits {where} GROUP BY status_bucket", params
        ).fetchall())

        latest_ingested_at = conn.execute(
            f"SELECT MAX(ingested_at) FROM permits {where}", params
        ).fetchone()[0]

    unmapped = total - mapped
    mapped_percent = round(100 * mapped / total, 2) if total else 0.0

    return {
        "total": total,
        "mapped": mapped,
        "unmapped": unmapped,
        "mapped_percent": mapped_percent,
        "pending_geocode": pending_geocode,
        "geocode_not_found": geocode_not_found,
        "no_address": no_address,
        "by_locality": by_locality,
        "by_status_bucket": by_status_bucket,
        "latest_ingested_at": latest_ingested_at,
    }


@app.get("/api/data-status")
def data_status():
    """Safe operational transparency -- reads ingestion_runs for real
    success/failure history rather than inferring health from row counts
    alone (a locality with permits but a recently-failed refresh should show
    the failure, not look silently fine)."""
    result = []
    with db_module.db() as conn:
        for key, info in LOCALITY_INFO.items():
            if info["availability"] != "available":
                result.append({
                    "locality": key, "label": info["label"], "availability": info["availability"],
                    "source_name": info["source_name"],
                    "last_successful_ingestion": None, "last_failed_ingestion": None,
                    "latest_error_summary": None, "record_count": 0, "mapped_count": 0,
                    "mapped_percent": 0.0, "earliest_permit_date": None, "latest_permit_date": None,
                })
                continue

            record_count = conn.execute(
                "SELECT COUNT(*) FROM permits WHERE locality=?", (key,)
            ).fetchone()[0]
            mapped_count = conn.execute(
                "SELECT COUNT(*) FROM permits WHERE locality=? AND lat IS NOT NULL", (key,)
            ).fetchone()[0]
            mapped_percent = round(100 * mapped_count / record_count, 2) if record_count else 0.0

            date_row = conn.execute(
                """SELECT MIN(issued_date) AS earliest, MAX(issued_date) AS latest
                   FROM permits WHERE locality=? AND issued_date IS NOT NULL""",
                (key,),
            ).fetchone()

            last_success = conn.execute(
                """SELECT completed_at FROM ingestion_runs
                   WHERE locality=? AND status='success' ORDER BY completed_at DESC LIMIT 1""",
                (key,),
            ).fetchone()
            last_failed = conn.execute(
                """SELECT completed_at, error_message FROM ingestion_runs
                   WHERE locality=? AND status='failed' ORDER BY completed_at DESC LIMIT 1""",
                (key,),
            ).fetchone()

            result.append({
                "locality": key, "label": info["label"], "availability": info["availability"],
                "source_name": info["source_name"],
                "last_successful_ingestion": last_success["completed_at"] if last_success else None,
                "last_failed_ingestion": last_failed["completed_at"] if last_failed else None,
                "latest_error_summary": last_failed["error_message"] if last_failed else None,
                "record_count": record_count, "mapped_count": mapped_count,
                "mapped_percent": mapped_percent,
                "earliest_permit_date": date_row["earliest"] if date_row else None,
                "latest_permit_date": date_row["latest"] if date_row else None,
            })

    return {"localities": result}


@app.get("/api/export.geojson")
def export_geojson(
    locality: Optional[str] = None,
    permit_type: Optional[str] = None,
    status: Optional[str] = None,
    status_bucket: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    issued_from: Optional[str] = None,
    issued_to: Optional[str] = None,
):
    where, params = build_filter(locality, permit_type, status, status_bucket, q, issued_from, issued_to)
    geo_where = _append_condition(where, "lat IS NOT NULL AND lng IS NOT NULL")

    with db_module.db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM permits {geo_where}", params).fetchone()[0]
        if total > MAX_EXPORT_FEATURES:
            raise HTTPException(
                413,
                f"Too many matching records for GeoJSON export ({total} > {MAX_EXPORT_FEATURES}). "
                "Narrow your filters and try again.",
            )
        rows = conn.execute(
            f"SELECT {', '.join(EXPORT_FIELDS)} FROM permits {geo_where} ORDER BY id", params
        ).fetchall()

    features = []
    for row in rows:
        props = {k: row[k] for k in EXPORT_FIELDS if k not in ("lat", "lng")}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row["lng"], row["lat"]]},
            "properties": props,
        })

    return JSONResponse({"type": "FeatureCollection", "features": features})


@app.get("/")
def index():
    index_path = os.path.join(BASE_DIR, "static", "index.html")
    if not os.path.exists(index_path):
        # Phase 8 frontend not built yet -- still a valid, non-erroring response.
        return JSONResponse({"detail": "PermitIQ frontend not yet built (Phase 8)"})
    return FileResponse(index_path)


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
