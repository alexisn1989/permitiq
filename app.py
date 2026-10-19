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
from ingest import flood_stats, land_use, land_use_stats
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

PERMIT_FIELDS = (
    "id", "locality", "source_permit_id", "source_name", "source_url",
    "source_record_url", "source_updated_at", "address", "lat", "lng",
    "geocoded_address", "coordinate_source", "geocode_status",
    "permit_type", "status", "status_bucket", "applied_date", "issued_date",
    "finaled_date", "expiration_date", "valuation", "description",
    "contractor", "applicant", "first_seen_at", "ingested_at",
) + FLOOD_FIELDS

EXPORT_FIELDS = (
    "id", "source_permit_id", "locality", "permit_type", "status", "status_bucket",
    "address", "applied_date", "issued_date", "finaled_date", "expiration_date",
    "valuation", "description", "contractor", "source_name", "source_url",
    "source_record_url", "lat", "lng",
) + FLOOD_FIELDS

# Every read joins the freshness view; permits columns are always p.-qualified.
FROM_SQL = "permits p JOIN permit_flood_effective e ON e.permit_id = p.id"
_FLOOD_SQL = {"flood_class": "e.effective_class", "fld_zone": "e.fld_zone",
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
                    "land_use_decisions", "land_use_decision_status"}
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
            return flood_stats.compute_flood_stats(conn, cohort_cfg, issued_from, issued_to, zip)
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
)
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
        clauses.append(f"EXISTS (SELECT 1 FROM decision_permit_links k WHERE k.decision_key = d.decision_key "
                       f"AND k.match_tier IN ({','.join('?' * len(tiers))}))")
        params.extend(tiers)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = [dict(r) for r in conn.execute(f"{DECISION_SQL} {where} ORDER BY d.meeting_date DESC, d.decision_key",
                                          params)]
    flood_by_key = land_use_stats.decision_flood(conn, [r["decision_key"] for r in rows])
    for r in rows:
        r.update(flood_by_key[r["decision_key"]])
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
        addresses = [dict(r) for r in conn.execute(
            "SELECT seq, source, raw_text, house_from, house_to, street_norm, gpin10, lat, lng, geocode_status "
            "FROM land_use_decision_addresses WHERE decision_key = ? ORDER BY seq", (decision_key,))]
        links = [dict(r) for r in conn.execute(
            f"""SELECT k.permit_id, k.match_tier, k.matched_on, k.link_state, k.is_new_construction,
                       p.source_permit_id, p.address, p.permit_type, p.applied_date, p.issued_date,
                       e.effective_class AS flood_class
                FROM decision_permit_links k JOIN permits p ON p.id = k.permit_id
                JOIN permit_flood_effective e ON e.permit_id = p.id
                WHERE k.decision_key = ? ORDER BY k.link_state DESC, p.applied_date""", (decision_key,))]
        conflicts = [dict(r) for r in conn.execute(
            "SELECT field, variants_json FROM land_use_decision_conflicts WHERE decision_key = ?", (decision_key,))]
        review = conn.execute("SELECT verified_outcome, evidence, reviewer, reviewed_at FROM decision_outcome_review "
                              "WHERE decision_key = ?", (decision_key,)).fetchone()
        cov = land_use_stats.coverage(conn)
    return {"decision": {k: row[k] for k in DECISION_FIELDS}, "addresses": addresses, "links": links,
            "conflicts": conflicts, "review": dict(review) if review else None, "coverage": cov}


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
