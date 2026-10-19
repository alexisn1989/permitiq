"""PermitIQ API.

Run locally:
    uvicorn permitiq.api:app --reload

Endpoints:
    GET  /health                       DB connectivity check
    GET  /api/permits/leads            JSON array from vw_permit_leads
    GET  /api/violations/leads         JSON array from vw_violation_leads
    GET  /api/planning/leads           JSON array from vw_planning_leads
    POST /api/admin/ingest-permits     runs ingest_permits.py (Bearer ADMIN_TOKEN)
    POST /api/admin/ingest-violations  runs ingest_violations.py (Bearer ADMIN_TOKEN)
    POST /api/admin/ingest-planning    runs ingest_planning.py (Bearer ADMIN_TOKEN)
"""
from __future__ import annotations

import hmac
import os
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

try:  # optional: load .env for local runs
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
PERMITS_SCRIPT = ROOT / "ingest_permits.py"
VIOLATIONS_SCRIPT = ROOT / "ingest_violations.py"
PLANNING_SCRIPT = ROOT / "ingest_planning.py"
INGEST_TIMEOUT = int(os.getenv("INGEST_TIMEOUT_SECONDS", "900"))

app = FastAPI(title="PermitIQ API", version="0.1.0")
# One lock for every ingest (permits, violations, planning), taken in
# _run_ingest: runs never overlap, which also keeps the shared Nominatim
# fallback within its 1 req/sec usage policy. Any new ingest endpoint must
# go through _run_ingest to stay under it.
_ingest_lock = threading.Lock()

FloodMatch = Literal["matched", "no_polygon", "not_geocoded"]


def _dsn() -> str:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")
    return dsn


def require_admin_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer ADMIN_TOKEN from .env. Fails closed when the token is unset."""
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN not configured")
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.get("/health")
def health():
    try:
        with psycopg.connect(_dsn(), connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except HTTPException:
        raise
    except Exception:
        return JSONResponse({"status": "degraded", "db": "unreachable"}, status_code=503)
    return {"status": "ok", "db": "ok"}


def _query_leads(
    view: str,
    order_by: str,
    clauses: list[str],
    params: list,
    fld_zone: list[str] | None,
    flood_match: str | None,
    limit: int,
    offset: int,
) -> list[dict]:
    """Run a filtered, paged SELECT against a leads view. `view` and
    `order_by` are fixed identifiers from the caller, never user input."""
    clauses, params = list(clauses), list(params)
    if fld_zone:
        clauses.append("fld_zone = ANY(%s)")
        params.append([z.strip().upper() for z in fld_zone])
    if flood_match:
        clauses.append("flood_match = %s")
        params.append(flood_match)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM {view}{where} ORDER BY {order_by} LIMIT %s OFFSET %s"
    params += [limit, offset]
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/api/permits/leads")
def permit_leads(
    fld_zone: list[str] | None = Query(None, description="Repeatable, e.g. ?fld_zone=AE&fld_zone=VE"),
    flood_match: FloodMatch | None = None,
    since: date | None = Query(None, description="application_date or issue_date on/after this date"),
    limit: int = Query(10000, ge=1, le=100000),
    offset: int = Query(0, ge=0),
):
    clauses: list[str] = []
    params: list = []
    if since:
        clauses.append("(application_date >= %s OR issue_date >= %s)")
        params += [since, since]
    return _query_leads("vw_permit_leads", "permit_number", clauses, params,
                        fld_zone, flood_match, limit, offset)


@app.get("/api/violations/leads")
def violation_leads(
    fld_zone: list[str] | None = Query(None, description="Repeatable, e.g. ?fld_zone=AE&fld_zone=VE"),
    flood_match: FloodMatch | None = None,
    violation_type: list[str] | None = Query(None, description="Repeatable; exact match"),
    since: date | None = Query(None, description="complaint_date on/after this date"),
    limit: int = Query(10000, ge=1, le=100000),
    offset: int = Query(0, ge=0),
):
    clauses: list[str] = []
    params: list = []
    if violation_type:
        clauses.append("violation_type = ANY(%s)")
        params.append([t.strip() for t in violation_type])
    if since:
        clauses.append("complaint_date >= %s")
        params.append(since)
    return _query_leads("vw_violation_leads", "violation_id", clauses, params,
                        fld_zone, flood_match, limit, offset)


@app.get("/api/planning/leads")
def planning_leads(
    fld_zone: list[str] | None = Query(None, description="Repeatable, e.g. ?fld_zone=AE&fld_zone=VE"),
    flood_match: FloodMatch | None = None,
    application_type: list[str] | None = Query(None, description="Repeatable; exact match"),
    status: list[str] | None = Query(None, description="Repeatable; exact match"),
    since: date | None = Query(None, description="hearing_date on/after this date"),
    limit: int = Query(10000, ge=1, le=100000),
    offset: int = Query(0, ge=0),
):
    clauses: list[str] = []
    params: list = []
    if application_type:
        clauses.append("application_type = ANY(%s)")
        params.append([t.strip() for t in application_type])
    if status:
        clauses.append("status = ANY(%s)")
        params.append([s.strip() for s in status])
    if since:
        clauses.append("hearing_date >= %s")
        params.append(since)
    return _query_leads("vw_planning_leads", "application_id", clauses, params,
                        fld_zone, flood_match, limit, offset)


def _tail(text: str | bytes | None, n: int = 4000) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):  # TimeoutExpired carries bytes even with text=True
        text = text.decode("utf-8", errors="replace")
    return text[-n:]


@app.post("/api/admin/ingest-permits")
def ingest_permits(
    days: int = Query(14, ge=1, le=3650, description="Recent-activity window (ignored with full/reset)"),
    full: bool = Query(False, description="Fetch every record, not just the last N days"),
    reset: bool = Query(False, description="TRUNCATE permits tables, then full load"),
    _: None = Depends(require_admin_token),
):
    return _run_ingest(PERMITS_SCRIPT, days, full, reset)


@app.post("/api/admin/ingest-violations")
def ingest_violations(
    days: int = Query(14, ge=1, le=3650, description="Recent-activity window (ignored with full/reset)"),
    full: bool = Query(False, description="Fetch every record, not just the last N days"),
    reset: bool = Query(False, description="TRUNCATE violation tables, then full load"),
    _: None = Depends(require_admin_token),
):
    return _run_ingest(VIOLATIONS_SCRIPT, days, full, reset)


@app.post("/api/admin/ingest-planning")
def ingest_planning(
    days: int = Query(14, ge=1, le=3650, description="Hearing-date window (ignored with full/reset)"),
    full: bool = Query(False, description="Fetch every record, not just the last N days"),
    reset: bool = Query(False, description="TRUNCATE planning tables, then full load"),
    _: None = Depends(require_admin_token),
):
    return _run_ingest(PLANNING_SCRIPT, days, full, reset)


def _run_ingest(script: Path, days: int, full: bool, reset: bool) -> JSONResponse:
    """Run an ingest script as a subprocess under the shared ingest lock."""
    if not _ingest_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="An ingest is already running")
    try:
        cmd = [sys.executable, str(script)]
        if reset:
            cmd.append("--reset")
        elif not full:
            cmd += ["--days", str(days)]
        try:
            proc = subprocess.run(
                cmd, cwd=ROOT, capture_output=True, text=True, timeout=INGEST_TIMEOUT
            )
        except subprocess.TimeoutExpired as e:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"ingest timed out after {INGEST_TIMEOUT}s",
                    "stdout_tail": _tail(e.stdout),
                    "stderr_tail": _tail(e.stderr),
                },
                status_code=504,
            )
    finally:
        _ingest_lock.release()

    ok = proc.returncode == 0
    return JSONResponse(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "args": cmd[2:],
            "stdout_tail": _tail(proc.stdout),
            "stderr_tail": _tail(proc.stderr),
        },
        status_code=200 if ok else 502,
    )
