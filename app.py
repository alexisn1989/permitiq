"""PermitIQ read-only API.

Permits are synced exclusively by run_ingest.py (see that file's docstring
for why ingestion never runs from an HTTP path or app startup). This module
must never import run_ingest.py and exposes no POST/PUT/PATCH/DELETE routes
on /api/permits -- it is a read/filter/stats/export layer over data that is
always synced out-of-band.

Read-only is enforced, not just intended: every route opens the database
through db_module.db_readonly() (SQLite mode=ro, no journal_mode pragma), and
importing this module touches no file -- schema creation/migration happens
only in CLI writers (scripts/migrate_db.py, run_ingest.py). If the database
is missing or its schema is older than this code, routes answer 503.

Flood fields come from the permit_flood_effective view (db.py), the single
definition of whether a permit's flood tag is current.

All filtering goes through the parameterized build_filter() below. The only
SQL identifiers that are ever interpolated directly (never parameterized,
since SQLite doesn't allow binding column/direction names) are the sort
column and sort direction -- both validated against small fixed allowlists
before use, so no unvalidated string ever reaches the SQL string.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import db as db_module
from ingest import flood_stats, land_use, land_use_stats, lead_facts, pc_hearings, quality
from ingest.flood import CELL_SIZE, MAX_FLOOD_CELL_BYTES, cell_bbox, empty_cell_bytes, etag_of

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAND_USE_CONFIG_PATH = Path(os.getenv("LAND_USE_CONFIG", os.path.join(BASE_DIR, "config", "land_use.toml")))
FLOOD_CONFIG_PATH = Path(os.getenv("FLOOD_REPORT_CONFIG", os.path.join(BASE_DIR, "config", "flood_report.toml")))

app = FastAPI(title="PermitIQ", version="1.1.0")
# No init_db() here: the API never creates or migrates schema.


@contextmanager
def _ro():
    """Read-only connection; a missing/outdated database becomes a 503."""
    try:
        with db_module.db_readonly() as conn:
            yield conn
    except db_module.DatabaseNotReady as e:
        raise HTTPException(503, {"database": str(e), "action": "run: python scripts/migrate_db.py"}) from None

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
FLOOD_FIELDS = ("flood_class", "fld_zone", "zone_subty", "sfha")
# Stage 3 parcel enrichment (permit_parcel_effective + active parcel/flood
# loads). parcel_flood_class and its shares are a screening estimate.
PARCEL_FIELDS = ("parcel_gpin", "parcel_method", "parcel_disputed", "zoning", "land_use", "prop_class", "acreage",
                 "parcel_flood_class", "parcel_share_sfha_v", "parcel_share_sfha_a", "parcel_share_shaded_x",
                 "parcel_share_other_mapped", "parcel_share_unmapped")

PERMIT_FIELDS = (
    "id", "locality", "source_permit_id", "source_name", "source_url",
    "source_record_url", "source_updated_at", "address", "lat", "lng",
    "geocoded_address", "coordinate_source", "geocode_status",
    "permit_type", "status", "status_bucket", "applied_date", "issued_date",
    "finaled_date", "expiration_date", "valuation", "description",
    "contractor", "applicant", "first_seen_at", "ingested_at",
) + FLOOD_FIELDS + PARCEL_FIELDS

EXPORT_FIELDS = (
    "id", "source_permit_id", "locality", "permit_type", "status", "status_bucket",
    "address", "applied_date", "issued_date", "finaled_date", "expiration_date",
    "valuation", "description", "contractor", "source_name", "source_url",
    "source_record_url", "lat", "lng",
) + FLOOD_FIELDS + PARCEL_FIELDS

# Every read joins the freshness view; permits columns are always p.-qualified.
FROM_SQL = ("permits p JOIN permit_flood_effective e ON e.permit_id = p.id "
            "JOIN permit_parcel_effective pp ON pp.permit_id = p.id "
            "LEFT JOIN parcels pa ON pa.load_id = pp.load_id AND pa.gpin = pp.display_gpin "
            "LEFT JOIN parcel_flood_effective pf ON pf.gpin = pp.display_gpin")
PARCEL_SQL = {
    "parcel_gpin": "pp.display_gpin", "parcel_method": "pp.parcel_state",
    "parcel_disputed": "CASE WHEN pp.disputed IS NULL THEN NULL WHEN pp.disputed = 1 AND "
                       "(pp.review_verdict IS NULL OR pp.review_verdict = 'unresolvable') THEN 1 ELSE 0 END",
    "zoning": "pa.zoning", "land_use": "pa.land_use", "prop_class": "pa.prop_class",
    "acreage": "ROUND(pa.tax_area_sqft / 43560.0, 4)", "parcel_flood_class": "pf.parcel_flood_class",
    "parcel_share_sfha_v": "pf.share_sfha_v", "parcel_share_sfha_a": "pf.share_sfha_a",
    "parcel_share_shaded_x": "pf.share_shaded_x", "parcel_share_other_mapped": "pf.share_other_mapped",
    "parcel_share_unmapped": "pf.share_unmapped",
}
_FLOOD_SQL = {**PARCEL_SQL, "flood_class": "e.effective_class", "fld_zone": "e.fld_zone",
              "zone_subty": "e.zone_subty", "sfha": "e.sfha"}


def _select(fields: tuple[str, ...]) -> str:
    return ", ".join(f"{_FLOOD_SQL.get(f, 'p.' + f)} AS {f}" for f in fields)


MAX_EXPORT_FEATURES = 25000
MAX_EXPORT_CSV_ROWS = 50000
# Filters that would redefine the flood-stats cohort (see ingest/flood_stats.py).
FLOOD_STATS_POPULATION_PARAMS = ("locality", "permit_type", "status", "status_bucket", "q", "flood_class")
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
    flood_class: Optional[list[str]] = None,
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
        clauses.append("p.locality = ?")
        params.append(locality)
    if permit_type:
        clauses.append("p.permit_type = ?")
        params.append(permit_type)
    if status:
        clauses.append("p.status = ?")
        params.append(status)
    if status_bucket:
        if status_bucket not in db_module.STATUS_BUCKETS:
            raise HTTPException(422, f"unknown status_bucket '{status_bucket}'")
        clauses.append("p.status_bucket = ?")
        params.append(status_bucket)
    if q:
        needle = f"%{q.lower()}%"
        clauses.append(
            "(lower(p.address) LIKE ? OR lower(p.description) LIKE ? OR "
            "lower(p.contractor) LIKE ? OR lower(p.source_permit_id) LIKE ?)"
        )
        params.extend([needle, needle, needle, needle])
    if issued_from:
        _validate_date(issued_from, "issued_from")
        clauses.append("p.issued_date >= ?")
        params.append(issued_from)
    if issued_to:
        _validate_date(issued_to, "issued_to")
        clauses.append("p.issued_date <= ?")
        params.append(issued_to)
    classes = parse_flood_classes(flood_class)
    if classes:
        clauses.append(f"e.effective_class IN ({', '.join('?' for _ in classes)})")
        params.extend(classes)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def parse_flood_classes(values: Optional[list[str]]) -> list[str]:
    """Accepts repeated params and/or comma lists (?flood_class=sfha_v,sfha_a).
    Tokens are trimmed and de-duplicated; any unknown token is a 422."""
    out: list[str] = []
    for value in values or []:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            if token not in db_module.FLOOD_CLASSES:
                raise HTTPException(422, f"unknown flood_class {token!r} -- must be one of {db_module.FLOOD_CLASSES}")
            if token not in out:
                out.append(token)
    return out


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
        with db_module.db_readonly() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        required = {"permits", "ingestion_runs", "geocode_cache", "permit_flood", "permit_flood_effective",
                    "land_use_decisions", "land_use_decision_status", "permit_parcel_effective"}
        if not required.issubset(tables):
            return JSONResponse({"status": "error", "database": "schema_invalid"}, status_code=503)
        return {"status": "ok", "database": "ok", "schema_version": version}
    except db_module.DatabaseNotReady as e:
        return JSONResponse({"status": "error", "database": str(e),
                             "action": "run: python scripts/migrate_db.py"}, status_code=503)
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
    flood_class: Optional[list[str]] = Query(default=None),
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

    where, params = build_filter(locality, permit_type, status, status_bucket, q, issued_from, issued_to,
                                 flood_class)

    if sort:
        order_sql = f"ORDER BY p.{sort} {order.upper()}, p.id DESC"
    else:
        order_sql = "ORDER BY COALESCE(p.issued_date, p.applied_date) DESC, p.id DESC"

    with _ro() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM {FROM_SQL} {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_select(PERMIT_FIELDS)} FROM {FROM_SQL} {where} {order_sql} LIMIT ? OFFSET ?",
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
    with _ro() as conn:
        row = conn.execute(
            f"SELECT {_select(PERMIT_FIELDS)} FROM {FROM_SQL} WHERE p.id = ?", (permit_id,)
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
    with _ro() as conn:
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
    flood_class: Optional[list[str]] = Query(default=None),
):
    where, params = build_filter(locality, permit_type, status, status_bucket, q, issued_from, issued_to,
                                 flood_class)

    with _ro() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM {FROM_SQL} {where}", params).fetchone()[0]

        mapped_where = _append_condition(where, "p.lat IS NOT NULL")
        mapped = conn.execute(f"SELECT COUNT(*) FROM {FROM_SQL} {mapped_where}", params).fetchone()[0]

        pending_where = _append_condition(where, "p.geocode_status = 'pending'")
        pending_geocode = conn.execute(f"SELECT COUNT(*) FROM {FROM_SQL} {pending_where}", params).fetchone()[0]

        not_found_where = _append_condition(where, "p.geocode_status = 'not_found'")
        geocode_not_found = conn.execute(f"SELECT COUNT(*) FROM {FROM_SQL} {not_found_where}", params).fetchone()[0]

        no_address_where = _append_condition(where, "p.geocode_status = 'no_address'")
        no_address = conn.execute(f"SELECT COUNT(*) FROM {FROM_SQL} {no_address_where}", params).fetchone()[0]

        by_locality = dict(conn.execute(
            f"SELECT p.locality, COUNT(*) FROM {FROM_SQL} {where} GROUP BY p.locality", params
        ).fetchall())
        by_status_bucket = dict(conn.execute(
            f"SELECT p.status_bucket, COUNT(*) FROM {FROM_SQL} {where} GROUP BY p.status_bucket", params
        ).fetchall())
        by_flood_class = dict(conn.execute(
            f"SELECT e.effective_class, COUNT(*) FROM {FROM_SQL} {where} GROUP BY e.effective_class", params
        ).fetchall())

        latest_ingested_at = conn.execute(
            f"SELECT MAX(p.ingested_at) FROM {FROM_SQL} {where}", params
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
        "by_flood_class": by_flood_class,
        "latest_ingested_at": latest_ingested_at,
    }


@app.get("/api/data-status")
def data_status():
    """Safe operational transparency -- reads ingestion_runs for real
    success/failure history rather than inferring health from row counts
    alone (a locality with permits but a recently-failed refresh should show
    the failure, not look silently fine)."""
    result = []
    with _ro() as conn:
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
    flood_class: Optional[list[str]] = Query(default=None),
):
    where, params = build_filter(locality, permit_type, status, status_bucket, q, issued_from, issued_to,
                                 flood_class)
    geo_where = _append_condition(where, "p.lat IS NOT NULL AND p.lng IS NOT NULL")

    with _ro() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM {FROM_SQL} {geo_where}", params).fetchone()[0]
        if total > MAX_EXPORT_FEATURES:
            raise HTTPException(
                413,
                f"Too many matching records for GeoJSON export ({total} > {MAX_EXPORT_FEATURES}). "
                "Narrow your filters and try again.",
            )
        rows = conn.execute(
            f"SELECT {_select(EXPORT_FIELDS)} FROM {FROM_SQL} {geo_where} ORDER BY p.id", params
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


@app.get("/api/export.csv")
def export_csv(
    locality: Optional[str] = None,
    permit_type: Optional[str] = None,
    status: Optional[str] = None,
    status_bucket: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    issued_from: Optional[str] = None,
    issued_to: Optional[str] = None,
    flood_class: Optional[list[str]] = Query(default=None),
):
    """Same filters and fields as the GeoJSON export, as CSV for Power BI.
    Includes permits without coordinates (their flood_class says so)."""
    where, params = build_filter(locality, permit_type, status, status_bucket, q, issued_from, issued_to,
                                 flood_class)
    with _ro() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM {FROM_SQL} {where}", params).fetchone()[0]
        if total > MAX_EXPORT_CSV_ROWS:
            raise HTTPException(413, f"Too many matching records for CSV export ({total} > {MAX_EXPORT_CSV_ROWS}). "
                                     "Narrow your filters and try again.")
        rows = conn.execute(f"SELECT {_select(EXPORT_FIELDS)} FROM {FROM_SQL} {where} ORDER BY p.id",
                            params).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(EXPORT_FIELDS)
    for row in rows:
        writer.writerow([row[k] for k in EXPORT_FIELDS])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="permitiq_permits.csv"'})


@app.get("/api/flood-stats")
def flood_stats_route(
    request: Request,
    issued_from: Optional[str] = None,
    issued_to: Optional[str] = None,
    zip: Optional[str] = None,  # noqa: A002 - query parameter name
    basis: str = "point",
):
    """Flood exposure of issued Virginia Beach permits: baseline (all types)
    and the configured cohort, computed by ingest/flood_stats.py -- the same
    function scripts/flood_report.py uses. Only context filters are accepted;
    filters that would redefine the cohort are rejected."""
    rejected = [k for k in FLOOD_STATS_POPULATION_PARAMS if k in request.query_params]
    if rejected:
        raise HTTPException(422, f"flood-stats does not accept {rejected}: the cohort is defined in "
                                 "config/flood_report.toml; only issued_from, issued_to and zip apply")
    try:
        cohort_cfg = flood_stats.load_cohort_config(FLOOD_CONFIG_PATH)
    except flood_stats.ConfigError:
        raise HTTPException(503, "flood report cohort config is invalid") from None
    with _ro() as conn:
        try:
            return flood_stats.compute_flood_stats(conn, cohort_cfg, issued_from, issued_to, zip, basis)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None


# ---------------------------------------------------------------------------
# Stage 2: land-use decisions -> permits (read-only). Status comes only from
# the land_use_decision_status view; approval only from human review.
# ---------------------------------------------------------------------------
DECISION_FIELDS = (
    "decision_key", "meeting_date", "title", "title_truncated", "land_use_label", "result_raw",
    "yes_count", "no_count", "absent_count", "abstain_count", "outcome_evidence", "verified_outcome",
    "status", "n_candidate_links", "n_confirmed_links", "address_text", "lat", "lng",
    "flood_class", "fld_zone", "flood_source",
) + PARCEL_FIELDS
DECISION_SQL = """
    SELECT d.decision_key, d.meeting_date, d.title, d.title_truncated, d.land_use_label, d.result_raw,
           d.yes_count, d.no_count, d.absent_count, d.abstain_count, d.outcome_evidence,
           s.verified_outcome, s.status, s.n_candidate_links, s.n_confirmed_links,
           (SELECT a.raw_text FROM land_use_decision_addresses a WHERE a.decision_key = d.decision_key
            ORDER BY a.seq LIMIT 1) AS address_text,
           (SELECT a.lat FROM land_use_decision_addresses a WHERE a.decision_key = d.decision_key
            AND a.lat IS NOT NULL ORDER BY a.seq LIMIT 1) AS lat,
           (SELECT a.lng FROM land_use_decision_addresses a WHERE a.decision_key = d.decision_key
            AND a.lat IS NOT NULL ORDER BY a.seq LIMIT 1) AS lng
    FROM land_use_decisions d JOIN land_use_decision_status s ON s.decision_key = d.decision_key
"""
DECISION_OUTCOME_FILTERS = db_module.VERIFIED_OUTCOMES + ("unverified",)
MAX_DECISION_PAGE = 500


def parse_tokens(values: Optional[list[str]], allowed: Optional[tuple[str, ...]], name: str) -> list[str]:
    """Same parsing as flood_class: repeated params and/or comma lists,
    trimmed, de-duplicated; an unknown token is a 422."""
    out: list[str] = []
    for value in values or []:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            if allowed is not None and token not in allowed:
                raise HTTPException(422, f"unknown {name} {token!r} -- must be one of {allowed}")
            if token not in out:
                out.append(token)
    return out


def _land_use_cfg() -> dict:
    try:
        return land_use.load_config(LAND_USE_CONFIG_PATH)
    except flood_stats.ConfigError:
        raise HTTPException(503, "land-use config is missing or invalid") from None


def _decision_parcels(conn, keys: list[str]) -> dict[str, dict]:
    """Parcel fields per decision: the first address row (by seq) with a
    fresh parcel match; all NULL when none."""
    out = {k: {f: None for f in PARCEL_FIELDS} for k in keys}
    if not keys:
        return out
    cols = ", ".join(f"{expr} AS {name}" for name, expr in PARCEL_SQL.items())
    rows = conn.execute(
        f"""SELECT pp.decision_key, pp.seq, {cols}
            FROM decision_parcel_effective pp
            LEFT JOIN parcels pa ON pa.load_id = pp.load_id AND pa.gpin = pp.display_gpin
            LEFT JOIN parcel_flood_effective pf ON pf.gpin = pp.display_gpin
            WHERE pp.fresh = 1 AND pp.decision_key IN ({','.join('?' * len(keys))})
            ORDER BY pp.decision_key, pp.seq""", keys).fetchall()
    seen = set()
    for r in rows:
        if r["decision_key"] in seen:
            continue
        seen.add(r["decision_key"])
        out[r["decision_key"]] = {f: r[f] for f in PARCEL_FIELDS}
    return out


def _decisions(conn, status, outcome, land_use_label, flood_class, decided_from, decided_to, match_tier):
    _validate_date(decided_from, "decided_from")
    _validate_date(decided_to, "decided_to")
    statuses = parse_tokens(status, db_module.LAND_USE_STATUSES, "status")
    outcomes = parse_tokens(outcome, DECISION_OUTCOME_FILTERS, "outcome")
    labels = parse_tokens(land_use_label, None, "land_use_label")
    floods = parse_tokens(flood_class, db_module.FLOOD_CLASSES, "flood_class")
    tiers = parse_tokens(match_tier, db_module.MATCH_TIERS, "match_tier")
    clauses, params = [], []

    def any_of(expr, values):
        clauses.append(f"{expr} IN ({','.join('?' * len(values))})")
        params.extend(values)

    if statuses:
        any_of("s.status", statuses)
    if outcomes:
        any_of("COALESCE(s.verified_outcome, 'unverified')", outcomes)
    if labels:
        any_of("d.land_use_label", labels)
    if decided_from:
        clauses.append("d.meeting_date >= ?")
        params.append(decided_from)
    if decided_to:
        clauses.append("d.meeting_date <= ?")
        params.append(decided_to)
    if tiers:
        clauses.append(f"EXISTS (SELECT 1 FROM decision_permit_links_effective k WHERE k.decision_key = d.decision_key "
                       f"AND k.match_tier IN ({','.join('?' * len(tiers))}))")
        params.extend(tiers)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = [dict(r) for r in conn.execute(f"{DECISION_SQL} {where} ORDER BY d.meeting_date DESC, d.decision_key",
                                          params)]
    flood_by_key = land_use_stats.decision_flood(conn, [r["decision_key"] for r in rows])
    parcel_by_key = _decision_parcels(conn, [r["decision_key"] for r in rows])
    for r in rows:
        r.update(flood_by_key[r["decision_key"]])
        r.update(parcel_by_key[r["decision_key"]])
    # The flood class is derived per decision (own point, else linked permits),
    # so it is filtered after the query; decision counts are small (hundreds).
    if floods:
        rows = [r for r in rows if r["flood_class"] in floods]
    return [{k: r[k] for k in DECISION_FIELDS} for r in rows]


@app.get("/api/land-use/decisions")
def land_use_decisions(
    status: Optional[list[str]] = Query(default=None),
    outcome: Optional[list[str]] = Query(default=None),
    land_use_label: Optional[list[str]] = Query(default=None),
    flood_class: Optional[list[str]] = Query(default=None),
    decided_from: Optional[str] = None,
    decided_to: Optional[str] = None,
    match_tier: Optional[list[str]] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=MAX_DECISION_PAGE),
    offset: int = Query(default=0, ge=0),
):
    with _ro() as conn:
        rows = _decisions(conn, status, outcome, land_use_label, flood_class, decided_from, decided_to, match_tier)
        cov = land_use_stats.coverage(conn)
    return {"count": len(rows), "limit": limit, "offset": offset, "coverage": cov,
            "decisions": rows[offset:offset + limit]}


@app.get("/api/land-use/decisions/{decision_key}")
def land_use_decision_detail(decision_key: str):
    with _ro() as conn:
        rows = [dict(r) for r in conn.execute(f"{DECISION_SQL} WHERE d.decision_key = ?", (decision_key,))]
        if not rows:
            raise HTTPException(404, "decision not found")
        row = rows[0]
        row.update(land_use_stats.decision_flood(conn, [decision_key])[decision_key])
        row.update(_decision_parcels(conn, [decision_key])[decision_key])
        addresses = [dict(r) for r in conn.execute(
            "SELECT seq, source, raw_text, house_from, house_to, street_norm, gpin10, lat, lng, geocode_status "
            "FROM land_use_decision_addresses WHERE decision_key = ? ORDER BY seq", (decision_key,))]
        links = [dict(r) for r in conn.execute(
            f"""SELECT k.permit_id, k.match_tier, k.matched_on, k.link_state, k.is_new_construction,
                       p.source_permit_id, p.address, p.permit_type, p.applied_date, p.issued_date,
                       e.effective_class AS flood_class, pp.display_gpin AS parcel_gpin,
                       {flood_stats.PARCEL_CLASS_SQL} AS parcel_flood_class,
                       lr.verdict AS review_verdict, lr.reviewer AS review_reviewer, lr.reviewed_at AS review_at
                FROM decision_permit_links_effective k JOIN permits p ON p.id = k.permit_id
                JOIN permit_flood_effective e ON e.permit_id = p.id {flood_stats.PARCEL_JOIN_SQL}
                LEFT JOIN decision_link_review lr ON lr.decision_key = k.decision_key AND lr.permit_id = k.permit_id
                WHERE k.decision_key = ? ORDER BY k.link_state DESC, p.applied_date""", (decision_key,))]
        parcel_rows = [dict(r) for r in conn.execute(
            """SELECT e.seq, a.raw_text, e.parcel_state, e.display_gpin, e.alt_gpin, e.dispute_reason,
                      e.review_verdict, e.set_verdict, e.link_gpin, e.gpin_trusted, pa.zoning, pa.land_use,
                      pa.prop_class, ROUND(pa.tax_area_sqft / 43560.0, 4) AS acreage, pa.prop_address,
                      pf.parcel_flood_class, pf.share_sfha_v, pf.share_sfha_a, pf.share_shaded_x,
                      pf.share_other_mapped, pf.share_unmapped
               FROM decision_parcel_effective e
               JOIN land_use_decision_addresses a ON a.decision_key = e.decision_key AND a.seq = e.seq
               LEFT JOIN parcels pa ON pa.load_id = e.load_id AND pa.gpin = e.display_gpin
               LEFT JOIN parcel_flood_effective pf ON pf.gpin = e.display_gpin
               WHERE e.decision_key = ? ORDER BY e.seq""", (decision_key,))]
        conflicts = [dict(r) for r in conn.execute(
            "SELECT field, variants_json FROM land_use_decision_conflicts WHERE decision_key = ?", (decision_key,))]
        review = conn.execute(
            "SELECT r.verified_outcome, r.evidence, r.reviewer, r.reviewed_at, r.conflict_resolution, "
            "COALESCE(r.reviewed_fingerprint = d.facts_fingerprint, 0) AS valid FROM decision_outcome_review r "
            "JOIN land_use_decisions d ON d.decision_key = r.decision_key WHERE r.decision_key = ?",
            (decision_key,)).fetchone()
        meeting_id = conn.execute("SELECT meeting_id FROM land_use_decisions WHERE decision_key = ?",
                                  (decision_key,)).fetchone()[0]
        cov = land_use_stats.coverage(conn)
        as_of = quality.data_as_of(conn)
        try:  # the timing benchmark needs min_n; without a config the rest is still shown
            cfg = land_use.load_config(LAND_USE_CONFIG_PATH)
        except flood_stats.ConfigError:
            cfg = None
        facts = lead_facts.lead_facts(conn, row, parcel_rows, cfg)
    return {"decision": {k: row[k] for k in DECISION_FIELDS}, "meeting_id": meeting_id, "addresses": addresses,
            "parcel_rows": parcel_rows, "links": links, "conflicts": conflicts,
            "review": dict(review) if review else None, "coverage": cov, "data_as_of": as_of,
            "lead_facts": facts}


PC_ITEM_SQL = """SELECT i.*, s.status, s.status_source, s.minutes_recommendation, s.recommendation_raw, s.aye, s.nay,
                         s.abstain, s.absent, s.n_council_candidates
                  FROM pc_items i JOIN pc_item_status s ON s.item_key = i.item_key"""
PC_NOTICE = ("A Planning Commission recommendation is advice to City Council, not an approval. Applicant, request "
             "and size are read from the agenda text and not verified; names of private individuals are not shown.")


@app.get("/api/land-use/pc-items")
def pc_items_route(
    status: Optional[list[str]] = Query(None), district: Optional[int] = Query(None, ge=1, le=99),
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    include_city: bool = False, limit: int = Query(200, ge=1, le=2000),
):
    """Planning Commission agenda items (earliest signal of a land-use application)."""
    _validate_date(date_from, "date_from")
    _validate_date(date_to, "date_to")
    statuses = parse_tokens(status, db_module.PC_STATUSES, "status")
    where, args = [], []
    if statuses:
        where.append(f"s.status IN ({','.join('?' * len(statuses))})")
        args += statuses
    if district is not None:
        where.append("i.council_district = ?")
        args.append(district)
    if date_from:
        where.append("i.meeting_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("i.meeting_date <= ?")
        args.append(date_to)
    if not include_city:
        where.append("i.city_initiated = 0")
    sql = PC_ITEM_SQL + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY i.meeting_date DESC, i.item_key"
    with _ro() as conn:
        rows = [dict(r) for r in conn.execute(sql + " LIMIT ?", (*args, limit))]
        items = []
        for r in rows:
            item = pc_hearings.public_item(r)
            item.update({k: r[k] for k in ("status", "status_source", "n_council_candidates")})
            parcels = [p for p in pc_hearings.item_parcels(conn, item["gpins"][:1]) if p.get("gpin")]
            first = parcels[0] if parcels and not parcels[0]["ambiguous"] else None
            item["lat"], item["lng"] = (first["lat"], first["lng"]) if first else (None, None)
            items.append(item)
        cov = pc_hearings.coverage(conn)
    return {"items": items, "count": len(items), "coverage": cov, "notice": PC_NOTICE}


@app.get("/api/land-use/pc-items/{item_key}")
def pc_item_detail(item_key: str):
    with _ro() as conn:
        row = conn.execute(PC_ITEM_SQL + " WHERE i.item_key = ?", (item_key,)).fetchone()
        if row is None:
            raise HTTPException(404, "planning commission item not found")
        row = dict(row)
        item = pc_hearings.public_item(row)
        doc = conn.execute("SELECT url, sha256, parse_status FROM pc_documents WHERE doc_id = ?",
                           (row["doc_id"],)).fetchone()
        minutes = conn.execute("SELECT url FROM pc_documents WHERE kind = 'minutes' AND meeting_date = ? "
                               "AND parse_status != 'failed'", (row["meeting_date"],)).fetchone()
        candidates = [dict(r) for r in conn.execute(
            "SELECT c.decision_key, c.matched_on, c.council_after_days, d.meeting_date, d.title, s.status, "
            "s.verified_outcome FROM pc_council_candidates c JOIN land_use_decisions d "
            "ON d.decision_key = c.decision_key JOIN land_use_decision_status s ON s.decision_key = c.decision_key "
            "WHERE c.item_key = ? ORDER BY d.meeting_date", (item_key,))]
        parcels = pc_hearings.item_parcels(conn, item["gpins"])
        as_of = quality.data_as_of(conn)
    item.update({
        "status": row["status"], "status_source": row["status_source"],
        "recommendation": {"raw": row["recommendation_raw"], "normalized": row["minutes_recommendation"],
                           "aye": row["aye"], "nay": row["nay"], "abstain": row["abstain"], "absent": row["absent"],
                           "minutes_url": minutes["url"] if minutes else None},
        "agenda": {"url": doc["url"], "sha256": doc["sha256"], "parse_status": doc["parse_status"]},
        "parcels": parcels, "council_candidates": candidates, "data_as_of": as_of, "notice": PC_NOTICE,
    })
    return item


@app.get("/api/land-use/export.csv")
def land_use_export_csv(
    status: Optional[list[str]] = Query(default=None),
    outcome: Optional[list[str]] = Query(default=None),
    land_use_label: Optional[list[str]] = Query(default=None),
    flood_class: Optional[list[str]] = Query(default=None),
    decided_from: Optional[str] = None,
    decided_to: Optional[str] = None,
    match_tier: Optional[list[str]] = Query(default=None),
):
    """Decisions (default: all; the lead list is ?status=no_candidate_permit)
    for Power BI. Every row and the response headers carry the permit
    observation window, because "no candidate permit" only means none
    appeared in the permits observed between those dates."""
    with _ro() as conn:
        rows = _decisions(conn, status, outcome, land_use_label, flood_class, decided_from, decided_to, match_tier)
        cov = land_use_stats.coverage(conn) or {}
    cov_from, cov_to = cov.get("backfill_from"), cov.get("last_refresh_at")
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(DECISION_FIELDS + ("permits_observed_from", "permits_observed_to"))
    for r in rows:
        writer.writerow([r[k] for k in DECISION_FIELDS] + [cov_from, cov_to])
    return Response(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="permitiq_land_use.csv"',
        "X-Permits-Observed-From": str(cov_from), "X-Permits-Observed-To": str(cov_to)})


@app.get("/api/land-use/stats")
def land_use_stats_route():
    cfg = _land_use_cfg()
    with _ro() as conn:
        return land_use_stats.compute_land_use_stats(conn, cfg)


@app.get("/api/data-as-of")
def data_as_of_route():
    """Source dates recorded by the CLI writers, plus the permit observation window."""
    with _ro() as conn:
        return quality.data_as_of(conn)


@app.get("/api/quality")
def quality_route():
    """Live coverage, unresolved cases and refresh age. Accuracy comes from
    the latest imported tally and is withheld ("freshness": "stale") when the
    data it was computed on has since changed."""
    with _ro() as conn:
        return quality.quality(conn)


AUDIT_NOTICE = ("Review history is recorded by database triggers per import. It is not authenticated and not "
                "tamper-proof: acting_user is self-reported, and anyone with write access to the database file can "
                "alter it.")
AUDIT_KEY_TYPES = {"permit_id": int}


@app.get("/api/audit/{review_table}")
def audit_route(review_table: str, request: Request):
    """Change history of one human review. Query parameters are exactly the
    review table's own key, e.g. /api/audit/decision_link_review?decision_key=..&permit_id=.."""
    if review_table not in db_module.REVIEW_TABLES:
        raise HTTPException(400, f"unknown review table {review_table!r}; one of {list(db_module.REVIEW_TABLES)}")
    keys, _ = db_module.REVIEW_TABLES[review_table]
    given = dict(request.query_params)
    if set(given) != set(keys):
        raise HTTPException(400, f"{review_table} is keyed by exactly {list(keys)}; got {sorted(given)}")
    subject = {}
    for k, v in given.items():
        try:
            subject[k] = AUDIT_KEY_TYPES.get(k, str)(v)
        except ValueError:
            raise HTTPException(400, f"{k} must be an integer") from None
    if review_table == "parcel_match_review" and subject["subject_kind"] not in ("permit", "decision"):
        raise HTTPException(400, "subject_kind must be permit or decision")
    with _ro() as conn:
        rows = conn.execute(
            "SELECT a.audit_id, a.action, a.recorded_at, a.review_author, a.old_json, a.new_json, i.import_id, i.kind, "
            "i.source_file, i.source_sha256, i.derived_file, i.derived_sha256, i.acting_user, i.started_at "
            "FROM review_audit a JOIN review_imports i ON i.import_id = a.import_id "
            "WHERE a.review_table = ? AND a.subject_json = ? ORDER BY a.audit_id",
            (review_table, db_module.review_subject(review_table, **subject))).fetchall()
        label = None
        if review_table == "decision_link_review":
            r = conn.execute("SELECT source_permit_id FROM permits WHERE id = ?", (subject["permit_id"],)).fetchone()
            label = {"source_permit_id": r[0] if r else None}  # readable label only, never the key
    history = [{"audit_id": r["audit_id"], "action": r["action"], "recorded_at": r["recorded_at"],
                "review_author": r["review_author"],
                "old": json.loads(r["old_json"]) if r["old_json"] else None,
                "new": json.loads(r["new_json"]) if r["new_json"] else None,
                "import": {"import_id": r["import_id"], "kind": r["kind"], "source_file": r["source_file"],
                           "source_sha256": r["source_sha256"], "derived_file": r["derived_file"],
                           "derived_sha256": r["derived_sha256"], "acting_user": r["acting_user"],
                           "acting_user_verified": False, "started_at": r["started_at"]}} for r in rows]
    baseline = next((h for h in history if h["action"] == "baseline_snapshot"), None)
    return {"review_table": review_table, "subject": subject, "label": label, "history": history,
            "baseline_note": None if baseline is None else
            f"Captured when history tracking began ({baseline['recorded_at']}). Earlier changes and the "
            "original file were not recorded.",
            "notice": AUDIT_NOTICE}


@app.get("/api/parcels/{gpin}.geojson")
def parcel_outline(gpin: str, request: Request):
    """One parcel of the active parcel load as a GeoJSON Feature, for the map
    popup. Never a city-wide layer. ETag = SHA-256 of the exact bytes."""
    with _ro() as conn:
        row = conn.execute(
            "SELECT pa.gpin, pa.zoning, pa.land_use, pa.prop_class, pa.prop_address, pa.geom_geojson, "
            "pf.parcel_flood_class FROM parcels pa JOIN parcel_loads l ON l.load_id = pa.load_id AND l.is_active = 1 "
            "LEFT JOIN parcel_flood_effective pf ON pf.gpin = pa.gpin WHERE pa.gpin = ?", (gpin,)).fetchone()
    if row is None:
        raise HTTPException(404, "parcel not in the active parcel load")
    props = {k: row[k] for k in ("gpin", "zoning", "land_use", "prop_class", "prop_address", "parcel_flood_class")}
    payload = json.dumps({"type": "Feature", "properties": props, "geometry": json.loads(row["geom_geojson"])},
                         sort_keys=True, separators=(",", ":")).encode()
    etag = etag_of(payload)
    headers = {"ETag": f'"{etag}"', "Cache-Control": "no-cache"}
    inm = request.headers.get("if-none-match", "")
    if etag in {t.strip().removeprefix("W/").strip('"') for t in inm.split(",")}:
        return Response(status_code=304, headers=headers)
    return Response(payload, media_type="application/geo+json", headers=headers)


def _active_flood_load(conn) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT l.load_id, l.content_hash, l.simplify_tolerance FROM flood_zone_loads l "
        "JOIN flood_locality_dfirm m ON m.dfirm_id = l.dfirm_id "
        "WHERE m.locality = 'virginia_beach' AND l.is_active = 1").fetchone()


@app.get("/api/flood-zones/index.json")
def flood_zones_index():
    """Grid cells of the active flood load that have drawable polygons."""
    with _ro() as conn:
        load = _active_flood_load(conn)
        if load is None:
            return {"load_id": None, "cell_size": CELL_SIZE, "max_cell_bytes": MAX_FLOOD_CELL_BYTES, "cells": []}
        cells = conn.execute(
            "SELECT cell_x, cell_y, bytes, etag FROM flood_display_cells WHERE load_id = ? ORDER BY cell_x, cell_y",
            (load["load_id"],)).fetchall()
    return {
        "load_id": load["load_id"], "content_hash": load["content_hash"],
        "simplify_tolerance": load["simplify_tolerance"], "cell_size": CELL_SIZE,
        "max_cell_bytes": MAX_FLOOD_CELL_BYTES,
        "cells": [{"x": c["cell_x"], "y": c["cell_y"], "bbox": cell_bbox(c["cell_x"], c["cell_y"]),
                   "bytes": c["bytes"], "etag": c["etag"]} for c in cells],
    }


@app.get("/api/flood-zones/{cell_x}/{cell_y}.geojson")
def flood_zone_cell(cell_x: int, cell_y: int, request: Request):
    """One grid cell of the drawable flood layer: the exact bytes stored at
    load time (never re-serialized), under a hard size limit, with an ETag."""
    with _ro() as conn:
        load = _active_flood_load(conn)
        if load is None:
            raise HTTPException(404, "no active flood-zone load; run scripts/load_flood_zones.py")
        row = conn.execute(
            "SELECT geojson, bytes, etag FROM flood_display_cells WHERE load_id = ? AND cell_x = ? AND cell_y = ?",
            (load["load_id"], cell_x, cell_y)).fetchone()
    if row is None:
        payload = empty_cell_bytes()
        etag = etag_of(payload)
    else:
        payload, etag = bytes(row["geojson"]), row["etag"]
        if len(payload) > MAX_FLOOD_CELL_BYTES:
            raise HTTPException(413, f"flood cell exceeds {MAX_FLOOD_CELL_BYTES} bytes")
    headers = {"ETag": f'"{etag}"', "Cache-Control": "no-cache"}
    inm = request.headers.get("if-none-match", "")
    if etag in {t.strip().removeprefix("W/").strip('"') for t in inm.split(",")}:
        return Response(status_code=304, headers=headers)
    return Response(payload, media_type="application/geo+json", headers=headers)


@app.get("/")
def index():
    index_path = os.path.join(BASE_DIR, "static", "index.html")
    if not os.path.exists(index_path):
        # Phase 8 frontend not built yet -- still a valid, non-erroring response.
        return JSONResponse({"detail": "PermitIQ frontend not yet built (Phase 8)"})
    return FileResponse(index_path)


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
