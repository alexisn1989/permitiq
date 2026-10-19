#!/usr/bin/env python3
"""
ingest_violations.py

Pulls Virginia Beach Code Enforcement records from the city's ArcGIS
GeoServices REST FeatureServer and upserts them into PostGIS
(code_violations_raw + code_violations). Idempotent: keyed on violation ID,
safe to re-run daily. Same architecture as ingest_permits.py, whose ArcGIS
request, date, geometry and text helpers it imports so both tracks convert
data identically.

Layer: VIOLATIONS_LAYER_URL (default below), queried via <layer>/query with
f=json and outSR=4326.

Field names: this layer's schema has not been verified, so there are NO
default field names. Run --inspect, then map them with
VIOLATIONS_FIELD_MAP in .env or --map flags (logical names: violation_id,
violation_type, description, complaint_date, compliance_date, address).
violation_id is required; unmapped optional fields are stored as NULL.

Dates: ESRI epoch milliseconds (UTC), or text dates (ISO / YYYY/MM/DD) ->
calendar date, via esri_to_date(). --days filters locally when the layer's
date fields are text (see ingest_permits.since_filter).

Location: the API's own x/y (requested in WGS84) is used first. Records
whose geometry is missing, non-finite, or outside the Virginia Beach bounding
box fall back to geocoding the address (Census, then Nominatim) -- only if an
address field is mapped. Otherwise geom stays NULL. geocode_source records
where each point came from.

Usage:
  python ingest_violations.py --inspect
  python ingest_violations.py --map violation_id=CaseNo --dry-run --limit 50
  python ingest_violations.py                 # full load
  python ingest_violations.py --days 14       # daily job: recent activity only
  python ingest_violations.py --reset         # truncate violation tables first
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

try:  # optional: load .env for local runs
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from ingest_permits import _get_json, clean_text, esri_to_date, extract_point, in_window, since_filter
from permitiq.geo_lite import geocode_lite

LAYER_URL = os.getenv(
    "VIOLATIONS_LAYER_URL",
    "https://services2.arcgis.com/CyVvlIiUfRBmMQuu/arcgis/rest/services/"
    "Code_Enforcement_Cases/FeatureServer/0",
).rstrip("/")

# logical name -> source field name. All None until mapped from --inspect.
DEFAULT_FIELDS: dict[str, str | None] = {
    "violation_id": None,
    "violation_type": None,
    "description": None,
    "complaint_date": None,
    "compliance_date": None,
    "address": None,
}
REQUIRED = ("violation_id",)  # everything else is stored as NULL if absent
DATE_FIELDS = ("complaint_date", "compliance_date")


# ── ArcGIS helpers ───────────────────────────────────────────────────────────

def get_metadata() -> dict:
    return _get_json(LAYER_URL, {"f": "json"})


def resolve_fields(meta: dict, overrides: dict[str, str]) -> dict[str, str | None]:
    """Map logical names to the layer's actual field names (case-insensitive)."""
    actual = {f["name"].lower(): f["name"] for f in meta.get("fields", [])}
    wanted = {**DEFAULT_FIELDS, **overrides}
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


# ── value conversion ─────────────────────────────────────────────────────────

def build_row(feature: dict, fields: dict[str, str | None], oid_field: str) -> dict | None:
    attrs = feature.get("attributes") or {}
    violation_id = clean_text(attrs.get(fields["violation_id"]))
    if not violation_id:
        return None
    point, reason = extract_point(feature.get("geometry"))

    def text(logical: str) -> str | None:
        return clean_text(attrs.get(fields[logical])) if fields[logical] else None

    row = {
        "violation_id": violation_id,
        "objectid": attrs.get(oid_field),
        "violation_type": text("violation_type"),
        "description": text("description"),
        "address": text("address"),
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
    INSERT INTO code_violations_raw (violation_id, payload) VALUES (%(violation_id)s, %(payload)s)
    ON CONFLICT (violation_id) DO UPDATE SET
        payload = EXCLUDED.payload, ingested_at = now()
"""

# A re-ingest that has no location must not erase a point (or its source)
# from an earlier run.
UPSERT_VIOLATION = """
    INSERT INTO code_violations
        (violation_id, objectid, violation_type, description, complaint_date,
         compliance_date, address, geom, geocode_source)
    VALUES
        (%(violation_id)s, %(objectid)s, %(violation_type)s, %(description)s,
         %(complaint_date)s, %(compliance_date)s, %(address)s,
         ST_SetSRID(ST_MakePoint(%(lon)s::double precision, %(lat)s::double precision), 4326),
         %(geocode_source)s)
    ON CONFLICT (violation_id) DO UPDATE SET
        objectid         = EXCLUDED.objectid,
        violation_type   = EXCLUDED.violation_type,
        description      = EXCLUDED.description,
        complaint_date   = EXCLUDED.complaint_date,
        compliance_date  = EXCLUDED.compliance_date,
        address          = EXCLUDED.address,
        geom             = COALESCE(EXCLUDED.geom, code_violations.geom),
        geocode_source   = CASE WHEN EXCLUDED.geom IS NOT NULL
                                THEN EXCLUDED.geocode_source
                                ELSE code_violations.geocode_source END,
        updated_at       = now()
"""


def geocode_fallback(cur, rows: list[dict], budget: int, stats: Counter) -> int:
    """Geocode rows lacking a point. Skips violations that already have a
    stored point, and stops after `budget` attempts (Nominatim is ~1 req/sec)."""
    need = [r for r in rows if r["lat"] is None and r["address"]]
    if not need:
        return budget
    cur.execute(
        "SELECT violation_id FROM code_violations WHERE violation_id = ANY(%s) AND geom IS NOT NULL",
        ([r["violation_id"] for r in need],),
    )
    have = {x[0] for x in cur.fetchall()}
    for r in need:
        if r["violation_id"] in have:
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
    p = argparse.ArgumentParser(description="Ingest VB code enforcement violations from ArcGIS into PostGIS")
    p.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="Postgres DSN (default: $DATABASE_URL)")
    p.add_argument("--inspect", action="store_true", help="Print layer metadata/fields and exit")
    p.add_argument("--dry-run", action="store_true", help="Fetch and parse only; no DB, no geocoding")
    p.add_argument("--reset", action="store_true", help="TRUNCATE code_violations and code_violations_raw first")
    p.add_argument("--days", type=int, help="Only records with a complaint/compliance date in the last N days")
    p.add_argument("--limit", type=int, help="Stop after N records (testing)")
    p.add_argument("--max-geocode", type=int, default=200, help="Max fallback geocode attempts per run")
    p.add_argument("--map", action="append", default=[], metavar="LOGICAL=FIELD",
                   help="Set a field mapping, e.g. --map violation_id=CaseNumber")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    # VIOLATIONS_FIELD_MAP="violation_id=CaseNumber,address=Address" sets the
    # mapping from .env; --map flags override it.
    env_map = [m for m in os.getenv("VIOLATIONS_FIELD_MAP", "").split(",") if m.strip()]
    overrides = {}
    for m in env_map + args.map:
        if "=" not in m:
            sys.exit(f"--map / VIOLATIONS_FIELD_MAP expects LOGICAL=FIELD, got {m!r}")
        k, v = m.split("=", 1)
        if k.strip() not in DEFAULT_FIELDS:
            sys.exit(f"unknown logical field {k.strip()!r}; expected one of {sorted(DEFAULT_FIELDS)}")
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
              f"Layer fields: {names}. Use --map LOGICAL=FIELD or VIOLATIONS_FIELD_MAP.", file=sys.stderr)
        return 1
    for k, v in overrides.items():
        if fields.get(k) is None:
            print(f"[warn] mapped field {v!r} for {k!r} not found in layer; it will be stored as NULL")
    unmapped = [k for k, v in fields.items() if v is None and k not in overrides and k != "address"]
    if unmapped:
        print(f"[warn] not mapped, stored as NULL: {', '.join(unmapped)}")
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
            cur.execute("TRUNCATE code_violations_raw, code_violations")
            conn.commit()
            print("Truncated code_violations_raw, code_violations")

    print(f"Fetching from {LAYER_URL}  where={where!r}  page_size={page_size}")
    seen: set[str] = set()
    try:
        for feats in fetch_pages(where, page_size, oid_field, args.limit):
            rows = []
            for f in feats:
                stats["fetched"] += 1
                row = build_row(f, fields, oid_field)
                if row is None:
                    stats["skipped_no_violation_id"] += 1
                    continue
                if client_since and not in_window(row, DATE_FIELDS, client_since):
                    stats["skipped_outside_days_window"] += 1
                    continue
                if row["violation_id"] in seen:
                    stats["duplicate_violation_id"] += 1  # last one wins
                seen.add(row["violation_id"])
                stats["bad_dates"] += row["bad_dates"]
                if row["geom_reason"] == "out_of_bounds":
                    stats["arcgis_out_of_bounds"] += 1
                rows.append(row)

            if args.dry_run:
                for r in rows:
                    stats[f"geocode_source:{r['geocode_source']}"] += 1
                if stats["fetched"] <= len(feats):
                    for r in rows[:3]:
                        print({k: r[k] for k in ("violation_id", "violation_type", "complaint_date",
                                                 "compliance_date", "lon", "lat")})
                continue

            budget = geocode_fallback(cur, rows, budget, stats)
            for r in rows:
                r["payload"] = Jsonb(r["payload"])
                stats[f"geocode_source:{r['geocode_source']}"] += 1
            cur.executemany(UPSERT_RAW, rows)
            cur.executemany(UPSERT_VIOLATION, rows)
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
