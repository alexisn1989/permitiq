#!/usr/bin/env python3
"""
ingest_permits.py

Pulls Virginia Beach "Building Permits Applications" from the city's ArcGIS
GeoServices REST FeatureServer and upserts them into PostGIS (permits_raw +
permits). Idempotent: keyed on permit number, safe to re-run daily.

Layer: PERMITS_LAYER_URL (default below), queried via <layer>/query with
f=json and outSR=4326.

Field names are NOT assumed: they are resolved (case-insensitively) against
the layer's live metadata at run time, and the run aborts listing the real
field names if a required one is missing. Use --inspect to print them, and
--map logical=ActualName to override (logical names: permit_number,
permit_type, application_date, issue_date, final_date, work_desc, address).

Dates: ESRI REST returns date fields as epoch MILLISECONDS (UTC). They are
converted by esri_to_date() and stored as the UTC calendar date, which is
correct whether the source encoded midnight UTC or local midnight.

Location: the API's own x/y (requested in WGS84) is used first. Records
whose geometry is missing, non-finite, or outside the Virginia Beach bounding
box fall back to geocoding the address (Census, then Nominatim) -- only if an
address field is configured (--map address=...). Otherwise geom stays NULL.
geocode_source records where each point came from.

Usage:
  python ingest_permits.py --inspect
  python ingest_permits.py --dry-run --limit 50
  python ingest_permits.py                 # full load
  python ingest_permits.py --days 14       # daily job: recent activity only
  python ingest_permits.py --reset         # truncate permits tables first
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import requests

try:  # optional: load .env for local runs
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from permitiq.geo_lite import geocode_lite, in_bounds

LAYER_URL = os.getenv(
    "PERMITS_LAYER_URL",
    "https://services2.arcgis.com/CyVvlIiUfRBmMQuu/arcgis/rest/services/"
    "Building_Permits_Applications/FeatureServer/0",
).rstrip("/")

# logical name -> expected source field name (verified against metadata at run
# time). PermitType / StreetAddress / the date fields are confirmed by a
# fixture recorded from the live VB layer (alexisn1989/permitiq,
# tests/fixtures/virginia_beach_page1.json).
DEFAULT_FIELDS = {
    "permit_number": "PermitNumber",
    "permit_type": "PermitType",
    "address": "StreetAddress",
    "application_date": "ApplicationDate",
    "issue_date": "IssueDate",
    "final_date": "FinalDate",
    "work_desc": "WorkDesc",
}
REQUIRED = ("permit_number",)  # everything else is stored as NULL if absent
DATE_FIELDS = ("application_date", "issue_date", "final_date")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_HEADERS = {"User-Agent": "permitiq/0.1"}


# ── ArcGIS helpers ───────────────────────────────────────────────────────────

def _get_json(url: str, params: dict, retries: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=60)
            r.raise_for_status()
            data = r.json()
            # ArcGIS reports many failures as HTTP 200 with an "error" body.
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"request failed after {retries} attempts: {last}")


def get_metadata() -> dict:
    return _get_json(LAYER_URL, {"f": "json"})


def resolve_fields(meta: dict, overrides: dict[str, str]) -> dict[str, str | None]:
    """Map logical names to the layer's actual field names (case-insensitive)."""
    actual = {f["name"].lower(): f["name"] for f in meta.get("fields", [])}
    wanted = {**DEFAULT_FIELDS, **overrides}
    wanted.setdefault("address", None)
    out: dict[str, str | None] = {}
    for logical, name in wanted.items():
        out[logical] = actual.get(name.lower()) if name else None
    return out


def fetch_pages(where: str, page_size: int, oid_field: str, limit: int | None):
    """Yield lists of features, paging with resultOffset until exhausted."""
    offset = 0
    while True:
        size = page_size if not limit else min(page_size, limit - offset)
        if size <= 0:
            return
        data = _get_json(
            f"{LAYER_URL}/query",
            {
                "where": where,
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": 4326,
                "orderByFields": oid_field,
                "resultOffset": offset,
                "resultRecordCount": size,
                "f": "json",
            },
        )
        feats = data.get("features") or []
        if not feats:
            return
        yield feats
        offset += len(feats)
        if len(feats) < size and not data.get("exceededTransferLimit"):
            return


def since_filter(meta: dict, fields: dict[str, str | None], date_logicals, since: date) -> tuple[str, date | None]:
    """--days window. Returns (where, client_since). A TIMESTAMP literal is
    only correct against esriFieldTypeDate fields; against a text date field
    (VB permits: "YYYY/MM/DD" strings) it silently mismatches. So the filter
    runs server-side only when every mapped date field is Date-typed;
    otherwise everything is fetched and rows are filtered here after parsing
    (client_since is set). Returns ("1=1", None) when no date field is mapped."""
    types = {f["name"]: f.get("type") for f in meta.get("fields", [])}
    mapped = [fields[k] for k in date_logicals if fields.get(k)]
    if not mapped:
        return "1=1", None
    if all(types.get(n) == "esriFieldTypeDate" for n in mapped):
        return " OR ".join(f"{n} >= TIMESTAMP '{since:%Y-%m-%d} 00:00:00'" for n in mapped), None
    return "1=1", since


def in_window(row: dict, date_logicals, since: date) -> bool:
    return any(row.get(k) and row[k] >= since for k in date_logicals)


# ── value conversion ─────────────────────────────────────────────────────────

_SLASH_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})(?:[ T].*)?$")


def esri_to_date(v) -> date | None:
    """ESRI epoch-millisecond (UTC) -> date. Also tolerates numeric strings,
    ISO strings and "YYYY/MM/DD" strings (some layers store dates as text). Returns None for null,
    unparseable, or implausible (outside 1900-2100) values."""
    if v is None or isinstance(v, bool) or v == "":
        return None
    try:
        if isinstance(v, str):
            s = v.strip()
            if s.lstrip("-").isdigit():
                v = int(s)
            elif m := _SLASH_DATE.match(s):
                # VB's permit layer stores dates as text "YYYY/MM/DD"
                d = date(int(m[1]), int(m[2]), int(m[3]))
                return d if 1900 <= d.year <= 2100 else None
            else:
                d = datetime.fromisoformat(s.replace("Z", "+00:00")).date()
                return d if 1900 <= d.year <= 2100 else None
        if isinstance(v, (int, float)):
            if not math.isfinite(v):
                return None
            # timedelta from the epoch (not fromtimestamp) so pre-1970 values work.
            d = (_EPOCH + timedelta(milliseconds=v)).date()
            return d if 1900 <= d.year <= 2100 else None
    except (ValueError, OverflowError):
        return None
    return None


def extract_point(geom) -> tuple[tuple[float, float] | None, str]:
    """Return ((lon, lat) | None, reason) where reason is ok|missing|out_of_bounds."""
    if not geom:
        return None, "missing"
    try:
        lon, lat = float(geom.get("x")), float(geom.get("y"))
    except (TypeError, ValueError):
        return None, "missing"
    if not (math.isfinite(lon) and math.isfinite(lat)):  # ESRI sometimes sends "NaN"
        return None, "missing"
    if not in_bounds(lat, lon):
        return None, "out_of_bounds"
    return (lon, lat), "ok"


def clean_text(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def build_row(feature: dict, fields: dict[str, str | None], oid_field: str) -> dict | None:
    attrs = feature.get("attributes") or {}
    permit_number = clean_text(attrs.get(fields["permit_number"]))
    if not permit_number:
        return None
    point, reason = extract_point(feature.get("geometry"))
    row = {
        "permit_number": permit_number,
        "objectid": attrs.get(oid_field),
        "permit_type": clean_text(attrs.get(fields["permit_type"])) if fields["permit_type"] else None,
        "work_desc": clean_text(attrs.get(fields["work_desc"])) if fields["work_desc"] else None,
        "address": clean_text(attrs.get(fields["address"])) if fields["address"] else None,
        "lon": point[0] if point else None,
        "lat": point[1] if point else None,
        "geocode_source": "arcgis" if point else "none",
        "geom_reason": reason,
        "bad_dates": 0,
        "payload": attrs,
    }
    for logical in DATE_FIELDS:
        src = fields[logical]
        raw = attrs.get(src) if src else None
        val = esri_to_date(raw)
        if raw not in (None, "") and val is None:
            row["bad_dates"] += 1
        row[logical] = val
    return row


# ── database ─────────────────────────────────────────────────────────────────

UPSERT_RAW = """
    INSERT INTO permits_raw (permit_number, payload) VALUES (%(permit_number)s, %(payload)s)
    ON CONFLICT (permit_number) DO UPDATE SET
        payload = EXCLUDED.payload, ingested_at = now()
"""

# A re-ingest that has no location must not erase a point (or its source)
# from an earlier run.
UPSERT_PERMIT = """
    INSERT INTO permits
        (permit_number, objectid, permit_type, application_date, issue_date,
         final_date, work_desc, address, geom, geocode_source)
    VALUES
        (%(permit_number)s, %(objectid)s, %(permit_type)s, %(application_date)s,
         %(issue_date)s, %(final_date)s, %(work_desc)s, %(address)s,
         ST_SetSRID(ST_MakePoint(%(lon)s::double precision, %(lat)s::double precision), 4326),
         %(geocode_source)s)
    ON CONFLICT (permit_number) DO UPDATE SET
        objectid         = EXCLUDED.objectid,
        permit_type      = EXCLUDED.permit_type,
        application_date = EXCLUDED.application_date,
        issue_date       = EXCLUDED.issue_date,
        final_date       = EXCLUDED.final_date,
        work_desc        = EXCLUDED.work_desc,
        address          = EXCLUDED.address,
        geom             = COALESCE(EXCLUDED.geom, permits.geom),
        geocode_source   = CASE WHEN EXCLUDED.geom IS NOT NULL
                                THEN EXCLUDED.geocode_source
                                ELSE permits.geocode_source END,
        updated_at       = now()
"""


def geocode_fallback(cur, rows: list[dict], budget: int, stats: Counter) -> int:
    """Geocode rows lacking a point. Skips permits that already have a stored
    point, and stops after `budget` attempts (Nominatim is ~1 req/sec)."""
    need = [r for r in rows if r["lat"] is None and r["address"]]
    if not need:
        return budget
    cur.execute(
        "SELECT permit_number FROM permits WHERE permit_number = ANY(%s) AND geom IS NOT NULL",
        ([r["permit_number"] for r in need],),
    )
    have = {x[0] for x in cur.fetchall()}
    for r in need:
        if r["permit_number"] in have:
            continue
        if budget <= 0:
            stats["geocode_budget_exhausted"] += 1
            continue
        budget -= 1
        hit = geocode_lite(f"{r['address']}, Virginia Beach, VA")
        if hit:
            r["lat"], r["lon"], r["geocode_source"] = hit["lat"], hit["lng"], hit["source"]
        else:
            stats["geocode_failed"] += 1
    return budget


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest VB building permits from ArcGIS into PostGIS")
    p.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="Postgres DSN (default: $DATABASE_URL)")
    p.add_argument("--inspect", action="store_true", help="Print layer metadata/fields and exit")
    p.add_argument("--dry-run", action="store_true", help="Fetch and parse only; no DB, no geocoding")
    p.add_argument("--reset", action="store_true", help="TRUNCATE permits and permits_raw first")
    p.add_argument("--days", type=int, help="Only records with an application/issue/final date in the last N days")
    p.add_argument("--limit", type=int, help="Stop after N records (testing)")
    p.add_argument("--max-geocode", type=int, default=200, help="Max fallback geocode attempts per run")
    p.add_argument("--map", action="append", default=[], metavar="LOGICAL=FIELD",
                   help="Override a field mapping, e.g. --map address=SiteAddress")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    # PERMITS_FIELD_MAP="address=SiteAddress,permit_type=PermitType" sets the
    # mapping from .env; --map flags override it.
    env_map = [m for m in os.getenv("PERMITS_FIELD_MAP", "").split(",") if m.strip()]
    overrides = {}
    for m in env_map + args.map:
        if "=" not in m:
            sys.exit(f"--map / PERMITS_FIELD_MAP expects LOGICAL=FIELD, got {m!r}")
        k, v = m.split("=", 1)
        if k.strip() not in {*DEFAULT_FIELDS, "address"}:
            sys.exit(f"unknown logical field {k.strip()!r}; expected one of "
                     f"{sorted({*DEFAULT_FIELDS, 'address'})}")
        overrides[k.strip()] = v.strip()

    try:
        meta = get_metadata()
    except RuntimeError as e:
        print(f"[FATAL] could not read layer metadata from {LAYER_URL}: {e}", file=sys.stderr)
        return 1
    oid_field = meta.get("objectIdField") or "OBJECTID"
    geom_type = meta.get("geometryType")
    max_rc = int(meta.get("maxRecordCount") or 1000)
    paging = (meta.get("advancedQueryCapabilities") or {}).get("supportsPagination")

    if args.inspect:
        print(f"{meta.get('name')}  geometryType={geom_type}  maxRecordCount={max_rc}  "
              f"supportsPagination={paging}  objectIdField={oid_field}")
        for f in meta.get("fields", []):
            print(f"  {f['name']:<32} {f['type']}")
        return 0

    if geom_type != "esriGeometryPoint":
        print(f"[FATAL] expected esriGeometryPoint, layer reports {geom_type!r}; "
              "x/y extraction would be wrong. Aborting.", file=sys.stderr)
        return 1
    if paging is False:
        print("[FATAL] layer does not support resultOffset paging.", file=sys.stderr)
        return 1

    fields = resolve_fields(meta, overrides)
    missing = [k for k in REQUIRED if not fields.get(k)]
    if missing:
        names = ", ".join(f["name"] for f in meta.get("fields", []))
        print(f"[FATAL] could not resolve required field(s) {missing}. "
              f"Layer fields: {names}. Use --map LOGICAL=FIELD.", file=sys.stderr)
        return 1
    unresolved = [k for k, v in fields.items() if v is None and k != "address"]
    for k in unresolved:
        print(f"[warn] field for {k!r} not found in layer; it will be stored as NULL")
    if not fields["address"]:
        print("[info] no address field mapped (--map address=...): fallback geocoding is disabled")

    where, client_since = "1=1", None
    if args.days:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).date()
        where, client_since = since_filter(meta, fields, DATE_FIELDS, since)
        if where == "1=1" and client_since is None:
            print("[warn] --days given but no date field is mapped; fetching everything")
        elif client_since:
            print("[info] date fields are not Date-typed; fetching all records and filtering by date locally")

    page_size = min(max_rc, 2000)
    stats: Counter = Counter()
    budget = args.max_geocode

    conn = cur = None
    if not args.dry_run:
        if not args.dsn:
            sys.exit("[FATAL] no DSN: set DATABASE_URL or pass --dsn (or use --dry-run)")
        import psycopg
        from psycopg.types.json import Jsonb

        conn = psycopg.connect(args.dsn)
        cur = conn.cursor()
        if args.reset:
            cur.execute("TRUNCATE permits_raw, permits")
            conn.commit()
            print("Truncated permits_raw, permits")

    print(f"Fetching from {LAYER_URL}  where={where!r}  page_size={page_size}")
    seen: set[str] = set()
    try:
        for feats in fetch_pages(where, page_size, oid_field, args.limit):
            rows = []
            for f in feats:
                stats["fetched"] += 1
                row = build_row(f, fields, oid_field)
                if row is None:
                    stats["skipped_no_permit_number"] += 1
                    continue
                if client_since and not in_window(row, DATE_FIELDS, client_since):
                    stats["skipped_outside_days_window"] += 1
                    continue
                if row["permit_number"] in seen:
                    stats["duplicate_permit_number"] += 1  # last one wins
                seen.add(row["permit_number"])
                stats["bad_dates"] += row["bad_dates"]
                if row["geom_reason"] == "out_of_bounds":
                    stats["arcgis_out_of_bounds"] += 1
                rows.append(row)

            if args.dry_run:
                for r in rows:
                    stats[f"geocode_source:{r['geocode_source']}"] += 1
                if stats["fetched"] <= len(feats):
                    for r in rows[:3]:
                        print({k: r[k] for k in ("permit_number", "permit_type", "application_date",
                                                 "issue_date", "final_date", "lon", "lat")})
                continue

            budget = geocode_fallback(cur, rows, budget, stats)
            for r in rows:
                r["payload"] = Jsonb(r["payload"])
                stats[f"geocode_source:{r['geocode_source']}"] += 1
            cur.executemany(UPSERT_RAW, rows)
            cur.executemany(UPSERT_PERMIT, rows)
            conn.commit()
            stats["upserted"] += len(rows)
            print(f"  fetched {stats['fetched']}, upserted {stats['upserted']}")
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[FATAL] ingest failed: {e}", file=sys.stderr)
        return 1
    finally:
        if conn:
            conn.close()

    print("Done.")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
