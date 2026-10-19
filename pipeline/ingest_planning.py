#!/usr/bin/env python3
"""
ingest_planning.py

Loads Virginia Beach planning applications (rezonings, conditional use
permits, variances) from a local JSON file -- produced by
scan_planning_gemini.py or another document scraper -- into PostGIS, and
records every sourced status observation in the planning_status_events
ledger (migration 007). Idempotent: re-running a file, or running it twice
at once, writes nothing new.

Input: a JSON array of objects.
  Required keys (values may be null unless stated):
    application_id    non-empty string (or number). For scanner records this
                      is provisional -- identity is resolved here, see below.
    project_name      string or null
    address           string or null   -- geocoded to get geom
    application_type  string or null
    proposed_use      string or null
    hearing_date      "YYYY-MM-DD" or null -- for agenda sources, the meeting
    status            string or null   -- as printed (e.g. agenda section)
  Optional keys (absent = null; files written before 007 stay valid):
    source_excerpt, gpin, case_number, agenda_item, occurrence_key,
    source_document   string or null (gpin must be text: leading zeros)
    meeting_date      "YYYY-MM-DD" or null (defaults to hearing_date)
    effective_date    "YYYY-MM-DD" or null -- ONLY if the source explicitly
                      establishes when the status took effect; the agenda
                      scanner never sets it
The whole file is validated before anything is written.

Identity (permitiq/planning_identity.py), resolved per record, first match wins:
  1. a human-confirmed link for this occurrence          -> linked application
  2. this occurrence was ingested before                 -> same application
  3. an official case number is printed                  -> case-scoped ID
  4. scanner record without a case number                -> its own unresolved
                                                            occurrence record
  5. pre-007 record, legacy hash ID ("VBPC-<12 hex>"):
       same meeting as the stored legacy row (or new)    -> legacy ID
       a different meeting                               -> separate record,
                                                            flagged for review
  6. anything else                                       -> the producer's ID
Then possible matches (same GPIN, same applicant+address, legacy-hash match)
are written to planning_identity_review. Nothing is merged automatically.

Current state vs history: every record adds (at most) one event. The
canonical planning_applications row only takes a record's status and details
when its hearing_date is the same or newer than the stored one, so an older
agenda ingested later never replaces a newer observation. A GPIN is never
erased by a later record that omits it.

Location: addresses go through geo_lite.geocode_lite() (Census, then
Nominatim). Skipped when the stored point came from the same address, or the
record is older than the stored one. A changed address on a current record
clears the old point rather than leaving it on the new address.

Usage:
  python ingest_planning.py --file extracted_leads.json --dry-run
  python ingest_planning.py --file extracted_leads.json
  python ingest_planning.py --file extracted_leads.json --days 14
  python ingest_planning.py --file extracted_leads.json --reset   # wipes planning tables AND the ledger
  python ingest_planning.py --confirm-link OCCURRENCE_KEY APPLICATION_ID --confirmed-by NAME [--note TEXT]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:  # optional: load .env for local runs
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from ingest_permits import clean_text
from permitiq import planning_identity as pid
from permitiq.geo_lite import geocode_lite

FIELDS = ("application_id", "project_name", "address", "application_type",
          "proposed_use", "hearing_date", "status")
TEXT_FIELDS = ("project_name", "address", "application_type", "proposed_use", "status")
# Optional: may be absent (files from other producers stay valid); string or null if present.
OPTIONAL_TEXT_FIELDS = ("source_excerpt", "gpin", "case_number", "agenda_item",
                        "occurrence_key", "source_document")
OPTIONAL_DATE_FIELDS = ("meeting_date", "effective_date")
MAX_ERRORS_SHOWN = 20
BATCH_SIZE = 100
PLANNING_TABLES = ("planning_status_events", "planning_identity_review", "planning_identity_links",
                   "planning_occurrences", "planning_applications_raw", "planning_applications")


# ── input validation ─────────────────────────────────────────────────────────

class SchemaError(Exception):
    pass


def load_file(path: Path) -> list:
    if not path.is_file():
        raise SchemaError(f"file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as e:
        raise SchemaError(f"{path} is not UTF-8 text: {e}")
    except json.JSONDecodeError as e:
        raise SchemaError(f"{path} is not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})")
    if not isinstance(data, list):
        raise SchemaError(f"{path}: top level must be a JSON array of objects, got {type(data).__name__}")
    return data


def _parse_date(v, key: str, where: str, errors: list[str], notes: Counter) -> date | None:
    if v is None:
        return None
    if not isinstance(v, str):
        errors.append(f"{where}: {key} must be a 'YYYY-MM-DD' string or null, got {v!r}")
        return None
    s = v.strip()
    if not s:
        return None
    try:
        d = date.fromisoformat(s[:10])
        if len(s) > 10:
            # e.g. "2026-11-05T19:00:00" -- keep the date, note the time was dropped
            datetime.fromisoformat(s)
            notes[f"{key}_time_dropped"] += 1
        return d
    except ValueError:
        errors.append(f"{where}: {key} {v!r} is not a valid YYYY-MM-DD date")
        return None


def validate(records: list) -> tuple[list[dict], list[str], Counter]:
    """Return (rows, errors, notes). rows is only meaningful when errors is empty."""
    rows: list[dict] = []
    errors: list[str] = []
    notes: Counter = Counter()
    known = set(FIELDS) | set(OPTIONAL_TEXT_FIELDS) | set(OPTIONAL_DATE_FIELDS)
    for i, rec in enumerate(records):
        where = f"record [{i}]"
        if not isinstance(rec, dict):
            errors.append(f"{where}: must be an object, got {type(rec).__name__}")
            continue
        missing = [k for k in FIELDS if k not in rec]
        if missing:
            errors.append(f"{where}: missing key(s) {missing}")
            continue
        if set(rec) - known:
            notes["records_with_extra_keys"] += 1

        app_id = rec["application_id"]
        if isinstance(app_id, bool) or not isinstance(app_id, (str, int)) or not clean_text(app_id):
            errors.append(f"{where}: application_id must be a non-empty string, got {app_id!r}")
            continue
        where = f"record [{i}] ({clean_text(app_id)})"

        row = {"file_application_id": clean_text(app_id), "payload": rec, "_index": i}
        for k in TEXT_FIELDS + OPTIONAL_TEXT_FIELDS:
            v = rec.get(k)
            if v is not None and not isinstance(v, str):
                errors.append(f"{where}: {k} must be a string or null, got {type(v).__name__}")
            row[k] = clean_text(v) if isinstance(v, str) else None
        row["hearing_date"] = _parse_date(rec["hearing_date"], "hearing_date", where, errors, notes)
        for k in OPTIONAL_DATE_FIELDS:
            row[k] = _parse_date(rec.get(k), k, where, errors, notes)
        row["meeting_date"] = row["meeting_date"] or row["hearing_date"]
        rows.append(row)
    return rows, errors, notes


# ── identity resolution ──────────────────────────────────────────────────────

def resolve_identity(cur, r: dict, stats: Counter) -> None:
    """Set r['application_id'], r['occurrence_key'], r['identity_basis'] and
    r['review'] (pairs to flag). Deterministic for a given database state."""
    md = r["meeting_date"].isoformat() if r["meeting_date"] else None
    from_scanner = r["occurrence_key"] is not None
    occ = r["occurrence_key"] or pid.input_occurrence_key(r["file_application_id"], md)
    r["occurrence_key"] = occ
    r["review"] = []

    cur.execute("SELECT application_id FROM planning_identity_links WHERE occurrence_key = %s", (occ,))
    hit = cur.fetchone()
    if hit:
        app, basis = hit[0], "confirmed_link"
    else:
        cur.execute("SELECT application_id, resolution_method FROM planning_occurrences WHERE occurrence_key = %s",
                    (occ,))
        hit = cur.fetchone()
        if hit:
            app, basis = hit
        elif r["case_number"]:
            app, basis = pid.case_application_id(r["case_number"]), "official_case_id"
        elif from_scanner:
            app, basis = pid.occurrence_application_id(occ), "occurrence_unresolved"
        elif pid.LEGACY_ID_RE.match(r["file_application_id"]):
            legacy = r["file_application_id"]
            cur.execute("SELECT hearing_date FROM planning_applications WHERE application_id = %s", (legacy,))
            stored = cur.fetchone()
            if stored and stored[0] is not None and r["meeting_date"] is not None and stored[0] != r["meeting_date"]:
                # The legacy hash merged appearances across meetings; don't
                # overwrite that record with a different meeting's entry.
                app, basis = pid.legacy_split_application_id(legacy, md), "legacy_split"
                r["review"].append((legacy, "legacy_hash_match"))
                stats["legacy_hash_split"] += 1
            else:
                app, basis = legacy, "legacy_id"
        else:
            app, basis = r["file_application_id"], "input_id"
    if from_scanner and app != r["file_application_id"]:
        stats["provisional_id_replaced"] += 1
    r["application_id"], r["identity_basis"] = app, basis
    stats[f"identity:{basis}"] += 1


def find_review_candidates(cur, r: dict) -> None:
    """Flag (never merge) other records that might be the same application."""
    app = r["application_id"]
    if r["gpin"]:
        cur.execute(
            "SELECT application_id FROM planning_applications WHERE application_id <> %s AND superseded_by IS NULL "
            "AND gpin IS NOT NULL AND regexp_replace(gpin, '\\D', '', 'g') = %s",
            (app, pid.gpin_digits(r["gpin"])))
        r["review"] += [(c, "same_gpin") for (c,) in cur.fetchall()]
    if r["project_name"]:
        cur.execute(
            "SELECT application_id FROM planning_applications WHERE application_id <> %s AND superseded_by IS NULL "
            "AND planning_norm_text(project_name) = %s AND coalesce(planning_norm_text(address), '') = %s",
            (app, pid.norm_text(r["project_name"]), pid.norm_text(r["address"])))
        r["review"] += [(c, "same_applicant_address") for (c,) in cur.fetchall()]
        legacy = pid.legacy_hash_id(r["project_name"], r["address"])
        if legacy != app:
            cur.execute("SELECT 1 FROM planning_applications WHERE application_id = %s", (legacy,))
            if cur.fetchone():
                r["review"].append((legacy, "legacy_hash_match"))


# ── database writes ──────────────────────────────────────────────────────────

# The incoming record is "current" when it is at least as new as what is stored.
_CURRENT = ("(planning_applications.hearing_date IS NULL OR (EXCLUDED.hearing_date IS NOT NULL "
            "AND EXCLUDED.hearing_date >= planning_applications.hearing_date))")


def _cur(col: str) -> str:
    return f"CASE WHEN {_CURRENT} THEN EXCLUDED.{col} ELSE planning_applications.{col} END"


UPSERT_RAW = """
    INSERT INTO planning_applications_raw (application_id, payload, gpin)
    VALUES (%(application_id)s, %(payload)s, %(gpin)s)
    ON CONFLICT (application_id) DO UPDATE SET
        payload     = EXCLUDED.payload,
        gpin        = COALESCE(EXCLUDED.gpin, planning_applications_raw.gpin),
        ingested_at = now()
"""

# Current-state fields change only for a current record. A GPIN is never
# erased by a record that omits it. A current record that could not be
# geocoded keeps the stored point only while the address is unchanged.
UPSERT_APPLICATION = f"""
    INSERT INTO planning_applications
        (application_id, project_name, address, application_type, proposed_use,
         hearing_date, status, source_excerpt, gpin, case_number, identity_basis,
         geom, geocode_source)
    VALUES
        (%(application_id)s, %(project_name)s, %(address)s, %(application_type)s, %(proposed_use)s,
         %(hearing_date)s, %(status)s, %(source_excerpt)s, %(gpin)s, %(case_number)s, %(identity_basis)s,
         ST_SetSRID(ST_MakePoint(%(lon)s::double precision, %(lat)s::double precision), 4326),
         %(geocode_source)s)
    ON CONFLICT (application_id) DO UPDATE SET
        project_name     = {_cur("project_name")},
        address          = {_cur("address")},
        application_type = {_cur("application_type")},
        proposed_use     = {_cur("proposed_use")},
        status           = {_cur("status")},
        source_excerpt   = {_cur("source_excerpt")},
        hearing_date     = {_cur("hearing_date")},
        gpin             = CASE WHEN {_CURRENT}
                                THEN COALESCE(EXCLUDED.gpin, planning_applications.gpin)
                                ELSE COALESCE(planning_applications.gpin, EXCLUDED.gpin) END,
        case_number      = COALESCE(planning_applications.case_number, EXCLUDED.case_number),
        identity_basis   = COALESCE(planning_applications.identity_basis, EXCLUDED.identity_basis),
        geom             = CASE
                               WHEN NOT {_CURRENT} THEN planning_applications.geom
                               WHEN EXCLUDED.geom IS NOT NULL THEN EXCLUDED.geom
                               WHEN planning_applications.address IS DISTINCT FROM EXCLUDED.address THEN NULL
                               ELSE planning_applications.geom
                           END,
        geocode_source   = CASE
                               WHEN NOT {_CURRENT} THEN planning_applications.geocode_source
                               WHEN EXCLUDED.geom IS NOT NULL THEN EXCLUDED.geocode_source
                               WHEN planning_applications.address IS DISTINCT FROM EXCLUDED.address THEN 'none'
                               ELSE planning_applications.geocode_source
                           END,
        updated_at       = now()
"""

INSERT_OCCURRENCE = """
    INSERT INTO planning_occurrences
        (occurrence_key, application_id, resolution_method, jurisdiction, meeting_date,
         case_number, agenda_item, source_document)
    VALUES (%(occurrence_key)s, %(application_id)s, %(identity_basis)s, %(jurisdiction)s, %(meeting_date)s,
            %(case_number)s, %(agenda_item)s, %(source_document)s)
    ON CONFLICT (occurrence_key) DO NOTHING
"""

INSERT_EVENT = """
    INSERT INTO planning_status_events
        (application_id, occurrence_key, status_raw, status_normalized, effective_date,
         source_excerpt, source_document, meeting_date, agenda_item, event_key)
    VALUES (%(application_id)s, %(occurrence_key)s, %(status)s, %(status_normalized)s, %(effective_date)s,
            %(source_excerpt)s, %(source_document)s, %(meeting_date)s, %(agenda_item)s, %(event_key)s)
    ON CONFLICT (event_key) DO NOTHING
"""

INSERT_REVIEW = """
    INSERT INTO planning_identity_review (application_id, candidate_application_id, reason)
    VALUES (%s, %s, %s)
    ON CONFLICT DO NOTHING
"""


def _geocode_query(address: str) -> str:
    return address if "virginia beach" in address.lower() else f"{address}, Virginia Beach, VA"


def geocode_row(cur, r: dict, budget: int, stats: Counter) -> int:
    """Geocode the row's address unless the stored point came from the same
    address, or the row is older than the stored record (its location would
    not be applied). Stops after `budget` attempts (Nominatim ~1 req/sec)."""
    r["lat"] = r["lon"] = None
    r["geocode_source"] = "none"
    if not r["address"]:
        stats["no_address"] += 1
        return budget
    cur.execute("SELECT address, geom IS NOT NULL, hearing_date FROM planning_applications "
                "WHERE application_id = %s", (r["application_id"],))
    stored = cur.fetchone()
    if stored:
        s_addr, s_has_geom, s_date = stored
        if s_date is not None and (r["hearing_date"] is None or r["hearing_date"] < s_date):
            stats["geocode_skipped_older_record"] += 1
            return budget
        if s_has_geom and s_addr == r["address"]:
            stats["geocode_skipped_already_stored"] += 1
            return budget
    if budget <= 0:
        stats["geocode_budget_exhausted"] += 1
        return budget
    hit = geocode_lite(_geocode_query(r["address"]))
    if hit:
        r["lat"], r["lon"], r["geocode_source"] = hit["lat"], hit["lng"], hit["source"]
    else:
        stats["geocode_failed"] += 1
    return budget - 1


def write_row(cur, r: dict, budget: int, stats: Counter, jsonb) -> int:
    """Resolve, upsert, record the occurrence and event, flag reviews. Runs
    inside the caller's transaction."""
    resolve_identity(cur, r, stats)
    budget = geocode_row(cur, r, budget, stats)
    r["payload"] = jsonb(r["payload"])
    r["jurisdiction"] = pid.JURISDICTION
    r["status_normalized"] = pid.normalize_status(r["status"])
    r["event_key"] = pid.event_key(r["occurrence_key"], r["source_document"], r["status"])
    cur.execute(UPSERT_RAW, r)
    cur.execute(UPSERT_APPLICATION, r)
    cur.execute(INSERT_OCCURRENCE, r)
    cur.execute(INSERT_EVENT, r)
    stats["events_added" if cur.rowcount == 1 else "events_already_recorded"] += 1
    find_review_candidates(cur, r)
    for other, reason in r["review"]:
        a, b = sorted((r["application_id"], other))
        cur.execute(INSERT_REVIEW, (a, b, reason))
        stats[f"review_flag:{reason}" if cur.rowcount == 1 else "review_flag_already_recorded"] += 1
    stats[f"geocode_source:{r['geocode_source']}"] += 1
    return budget


# ── confirmed links ──────────────────────────────────────────────────────────

def confirm_link(conn, occurrence_key: str, target: str, confirmed_by: str, note: str | None) -> str:
    """Record a reviewer's decision that `occurrence_key` belongs to
    application `target`. If that occurrence was already ingested as a
    separate record, its occurrence and events move to the target, the target
    takes the moved record's details when they are newer, and the old record
    is marked superseded (kept, hidden from vw_planning_leads). One
    transaction."""
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT superseded_by FROM planning_applications WHERE application_id = %s FOR UPDATE",
                    (target,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"application {target!r} does not exist")
        if row[0] is not None:
            raise ValueError(f"application {target!r} is itself superseded by {row[0]!r}; link to that instead")
        cur.execute("""
            INSERT INTO planning_identity_links (occurrence_key, application_id, confirmed_by, note)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (occurrence_key) DO UPDATE SET application_id = EXCLUDED.application_id,
                confirmed_by = EXCLUDED.confirmed_by, confirmed_at = now(), note = EXCLUDED.note
        """, (occurrence_key, target, confirmed_by, note))
        cur.execute("SELECT application_id FROM planning_occurrences WHERE occurrence_key = %s FOR UPDATE",
                    (occurrence_key,))
        row = cur.fetchone()
        if row is None:
            return "link recorded; the occurrence has not been ingested yet and will resolve to it"
        old = row[0]
        if old == target:
            return "link recorded; the occurrence already belongs to that application"
        cur.execute("UPDATE planning_occurrences SET application_id = %s, resolution_method = 'confirmed_link' "
                    "WHERE occurrence_key = %s", (target, occurrence_key))
        cur.execute("UPDATE planning_status_events SET application_id = %s WHERE occurrence_key = %s",
                    (target, occurrence_key))
        cur.execute("""
            UPDATE planning_applications t SET
                project_name = o.project_name, address = o.address, application_type = o.application_type,
                proposed_use = o.proposed_use, hearing_date = o.hearing_date, status = o.status,
                source_excerpt = o.source_excerpt, geom = o.geom, geocode_source = o.geocode_source,
                updated_at = now()
            FROM planning_applications o
            WHERE t.application_id = %s AND o.application_id = %s
              AND (t.hearing_date IS NULL OR (o.hearing_date IS NOT NULL AND o.hearing_date >= t.hearing_date))
        """, (target, old))
        cur.execute("""
            UPDATE planning_applications t SET gpin = COALESCE(t.gpin, o.gpin),
                case_number = COALESCE(t.case_number, o.case_number)
            FROM planning_applications o WHERE t.application_id = %s AND o.application_id = %s
        """, (target, old))
        cur.execute("SELECT 1 FROM planning_occurrences WHERE application_id = %s LIMIT 1", (old,))
        if cur.fetchone() is None:
            cur.execute("UPDATE planning_applications SET superseded_by = %s WHERE application_id = %s",
                        (target, old))
        a, b = sorted((old, target))
        cur.execute("UPDATE planning_identity_review SET resolution = 'confirmed_same' "
                    "WHERE application_id = %s AND candidate_application_id = %s", (a, b))
        return f"occurrence moved from {old} to {target}"


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load planning leads from scraper JSON into PostGIS")
    p.add_argument("--file", type=Path, default=os.getenv("PLANNING_LEADS_FILE") or None,
                   help="JSON array of planning leads (default: $PLANNING_LEADS_FILE)")
    p.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="Postgres DSN (default: $DATABASE_URL)")
    p.add_argument("--dry-run", action="store_true", help="Validate the file only; no DB, no geocoding")
    p.add_argument("--reset", action="store_true",
                   help="TRUNCATE all planning tables, including the status ledger and identity links, first")
    p.add_argument("--days", type=int,
                   help="Only load records with a hearing date in the last N days or later "
                        "(records with no hearing date are skipped)")
    p.add_argument("--limit", type=int, help="Stop after N records (testing)")
    p.add_argument("--max-geocode", type=int, default=200, help="Max geocode attempts per run")
    p.add_argument("--confirm-link", nargs=2, metavar=("OCCURRENCE_KEY", "APPLICATION_ID"),
                   help="Record a reviewer-confirmed link instead of ingesting")
    p.add_argument("--confirmed-by", help="Reviewer name (required with --confirm-link)")
    p.add_argument("--note", help="Optional note for --confirm-link")
    return p.parse_args()


def _hint(e: Exception) -> None:
    msg = str(e)
    if any(t in msg for t in ("planning_status_events", "planning_occurrences", "planning_identity",
                              "planning_norm_text", "identity_basis", "gpin")):
        print("  hint: apply db/007_identity_and_events.sql", file=sys.stderr)
    elif "source_excerpt" in msg:
        print("  hint: apply db/006_planning_excerpt.sql", file=sys.stderr)


def main() -> int:
    args = parse_args()

    if args.confirm_link:
        if not args.dsn or not args.confirmed_by:
            sys.exit("[FATAL] --confirm-link needs --dsn/DATABASE_URL and --confirmed-by NAME")
        import psycopg

        with psycopg.connect(args.dsn) as conn:
            try:
                print(confirm_link(conn, *args.confirm_link, args.confirmed_by, args.note))
            except (ValueError, psycopg.Error) as e:
                print(f"[FATAL] link not recorded: {e}", file=sys.stderr)
                _hint(e)
                return 1
        return 0

    if not args.file:
        print("[FATAL] no input file: pass --file PATH (a JSON array of planning leads) "
              "or set PLANNING_LEADS_FILE in .env.", file=sys.stderr)
        return 1

    try:
        records = load_file(args.file)
    except SchemaError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1
    rows, errors, notes = validate(records)
    if errors:
        print(f"[FATAL] {args.file} failed schema validation ({len(errors)} problem(s)); nothing was written.",
              file=sys.stderr)
        print(f"  Expected: a JSON array of objects with keys {list(FIELDS)} (optional: "
              f"{list(OPTIONAL_TEXT_FIELDS + OPTIONAL_DATE_FIELDS)}); application_id a non-empty string, "
              "dates 'YYYY-MM-DD' or null, the rest string or null.", file=sys.stderr)
        for e in errors[:MAX_ERRORS_SHOWN]:
            print(f"  - {e}", file=sys.stderr)
        if len(errors) > MAX_ERRORS_SHOWN:
            print(f"  ... and {len(errors) - MAX_ERRORS_SHOWN} more", file=sys.stderr)
        return 1

    stats: Counter = Counter(notes)
    stats["records_in_file"] = len(records)
    if args.days:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).date()
        kept = [r for r in rows if r["hearing_date"] and r["hearing_date"] >= since]
        stats["skipped_outside_days_window"] = len(rows) - len(kept)
        rows = kept
    if args.limit:
        rows = rows[: args.limit]

    # The same observation listed twice in one file: last record wins.
    by_obs: dict[tuple, dict] = {}
    for r in rows:
        k = (r["occurrence_key"] or (r["file_application_id"], r["meeting_date"]),
             r["source_document"], pid.norm_text(r["status"]))
        if k in by_obs:
            stats["duplicate_observation_in_file"] += 1
        by_obs[k] = r
    # Oldest meeting first, so pre-007 legacy IDs keep their earliest meeting
    # and later meetings split off deterministically.
    rows = sorted(by_obs.values(), key=lambda r: (r["meeting_date"] or date.min, r["_index"]))

    if args.dry_run:
        stats["valid_rows"] = len(rows)
        print(f"Dry run: {args.file} is valid.")
        for r in rows[:3]:
            print({k: r[k] for k in FIELDS if k != "application_id"} | {"application_id": r["file_application_id"]})
        for k in sorted(stats):
            print(f"  {k}: {stats[k]}")
        return 0

    if not args.dsn:
        sys.exit("[FATAL] no DSN: set DATABASE_URL or pass --dsn (or use --dry-run)")
    import psycopg
    from psycopg.types.json import Jsonb

    conn = psycopg.connect(args.dsn)
    cur = conn.cursor()
    budget = args.max_geocode
    print(f"Loading {len(rows)} planning observation(s) from {args.file}")
    try:
        if args.reset:
            cur.execute(f"TRUNCATE {', '.join(PLANNING_TABLES)}")
            conn.commit()
            print(f"Truncated {', '.join(PLANNING_TABLES)}")
        for i in range(0, len(rows), BATCH_SIZE):
            # One transaction per batch: applications, occurrences, events and
            # review flags commit together or not at all.
            for r in rows[i : i + BATCH_SIZE]:
                budget = write_row(cur, r, budget, stats, Jsonb)
            conn.commit()
            stats["upserted"] += len(rows[i : i + BATCH_SIZE])
            print(f"  upserted {stats['upserted']}")
    except Exception as e:
        conn.rollback()
        print(f"[FATAL] ingest failed, batch rolled back: {e}", file=sys.stderr)
        _hint(e)
        return 1
    finally:
        conn.close()

    print("Done.")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
