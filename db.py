"""PermitIQ database layer: schema, connection helper, schema versioning.

Two kinds of process share this file. CLI writers (run_ingest.py and
scripts/*) use db(), which enables WAL + a busy timeout and may apply schema
migrations via init_db(). The API (app.py) uses db_readonly() only: a
SQLite mode=ro connection that never sets a pragma that writes and never
migrates -- if the schema isn't ready it raises DatabaseNotReady instead.

Permit data is never fabricated: init_db() only creates schema. The permits
table starts empty until run_ingest.py populates it from a verified source.
"""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATA_DIR", str(BASE_DIR))) / "permits.db"

SCHEMA_VERSION = 20

# Flood classes stored in permit_flood.flood_class, and the two derived only
# at read time by the permit_flood_effective view (see ingest/flood.py).
FLOOD_CLASSES_STORED = ("sfha_v", "sfha_a", "shaded_x", "matched_non_sfha", "no_polygon", "not_geocoded")
FLOOD_CLASSES = FLOOD_CLASSES_STORED + ("untagged", "out_of_scope")

# Stage 2 land-use linkage (schema v3). See the land_use_decision_status view.
LAND_USE_STATUSES = (
    "quarantined", "outcome_review_stale", "outcome_unverified", "not_approved", "address_unparsed", "observation_uncertain",
    "confirmed_permit", "candidate_permit", "no_candidate_permit", "no_candidate_window_expired",
)
VERIFIED_OUTCOMES = ("approved", "approved_with_conditions", "denied", "deferred", "withdrawn", "other")
OUTCOME_EVIDENCE = ("unverified", "proposed_approved", "proposed_not_approved", "unknown")
MATCH_TIERS = ("gpin", "address_exact", "same_parcel", "address_range", "manual")

# Stage 3 parcel enrichment (schema v5). Parcel flood classes are separate from
# FLOOD_CLASSES (point basis): the two screening classes mark sub-threshold
# hazard slivers and partly unmapped parcels, never "outside".
PARCEL_FLOOD_CLASSES = ("sfha_v", "sfha_a", "shaded_x", "matched_non_sfha",
                        "screening_sliver", "screening_partial_coverage", "no_polygon")
PERMIT_PARCEL_METHODS = ("gpin14", "gpin10_unique", "point_in_parcel", "ambiguous", "no_parcel",
                         "not_geocoded_no_gpin", "address_exact")
DECISION_PARCEL_METHODS = ("gpin10_unique", "address_exact", "point_in_parcel", "ambiguous", "no_parcel")
PARCEL_REVIEW_VERDICTS = ("gpin_correct", "point_correct", "unresolvable")
# Decision-level verdicts for a cross-row conflict (a street row's parcel is
# not among the decision's printed-GPIN parcels).
PARCEL_SET_VERDICTS = ("rows_consistent", "gpin_rows_correct", "street_rows_correct", "unresolvable")


class DatabaseNotReady(Exception):
    """The database file is missing, or its schema is older than this code
    needs. Raised by db_readonly(); the API turns it into a 503. Only a CLI
    writer (scripts/migrate_db.py, run_ingest.py) can fix it."""

STATUS_BUCKETS = ("active_pending", "issued", "finaled_closed", "expired_void", "other")
GEOCODE_STATUSES = (
    "pending",
    "matched",
    "low_confidence",
    "not_found",
    "no_address",
    "rejected_outside_area",
    "locality_mismatch",
    "manual_override",
)
COORDINATE_SOURCES = ("manual", "native", "census", "nominatim")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def db(db_path: Path | str | None = None):
    conn = sqlite3.connect(db_path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_readonly(db_path: Path | str | None = None):
    """Read-only connection for the API. Opens with SQLite's mode=ro URI, so
    no statement on it can write; sets only connection-local pragmas
    (busy_timeout is not persisted; journal_mode is NOT touched -- WAL mode is
    a persistent property of the file that a writer already set).

    Caveat: a mode=ro reader of a WAL database needs the -shm file to exist or
    the directory to be writable, which holds wherever a writer has run."""
    path = Path(db_path or DB_PATH)
    if not path.is_file():
        raise DatabaseNotReady("database_missing")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            raise DatabaseNotReady("schema_outdated" if version else "schema_missing")
        if version > SCHEMA_VERSION:
            raise DatabaseNotReady("schema_newer_than_code")
        yield conn
    finally:
        conn.close()


_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS permits (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        locality             TEXT NOT NULL,
        source_permit_id     TEXT NOT NULL,

        source_name          TEXT NOT NULL,
        source_url           TEXT,
        source_record_url    TEXT,
        source_updated_at    TEXT,

        address              TEXT,
        lat                  REAL,
        lng                  REAL,
        geocoded_address     TEXT,
        coordinate_source    TEXT,

        geocode_status       TEXT NOT NULL DEFAULT 'pending',
        geocode_provider     TEXT,
        geocode_confidence   TEXT,
        geocode_attempted_at TEXT,
        geocode_error        TEXT,

        permit_type          TEXT,
        status               TEXT,
        status_bucket        TEXT NOT NULL DEFAULT 'other',

        applied_date         TEXT,
        issued_date          TEXT,
        finaled_date         TEXT,
        expiration_date      TEXT,

        valuation            REAL,
        description          TEXT,
        contractor           TEXT,
        applicant            TEXT,

        raw_json             TEXT NOT NULL DEFAULT '{}',

        first_seen_at        TEXT NOT NULL,
        ingested_at          TEXT NOT NULL,

        UNIQUE(locality, source_permit_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_permits_locality ON permits(locality)",
    "CREATE INDEX IF NOT EXISTS idx_permits_status_bucket ON permits(status_bucket)",
    "CREATE INDEX IF NOT EXISTS idx_permits_issued_date ON permits(issued_date)",
    "CREATE INDEX IF NOT EXISTS idx_permits_latlng ON permits(lat, lng)",
    "CREATE INDEX IF NOT EXISTS idx_permits_locality_issued ON permits(locality, issued_date)",
    "CREATE INDEX IF NOT EXISTS idx_permits_locality_status ON permits(locality, status_bucket)",
    "CREATE INDEX IF NOT EXISTS idx_permits_locality_type ON permits(locality, permit_type)",
    """
    CREATE TABLE IF NOT EXISTS ingestion_runs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        locality         TEXT NOT NULL,
        started_at       TEXT NOT NULL,
        completed_at     TEXT,
        status           TEXT NOT NULL,

        fetched_count    INTEGER NOT NULL DEFAULT 0,
        inserted_count   INTEGER NOT NULL DEFAULT 0,
        updated_count    INTEGER NOT NULL DEFAULT 0,
        skipped_count    INTEGER NOT NULL DEFAULT 0,
        geocoded_count   INTEGER NOT NULL DEFAULT 0,
        error_count      INTEGER NOT NULL DEFAULT 0,

        since_date       TEXT,
        error_message    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ingestion_runs_locality ON ingestion_runs(locality, started_at)",
    """
    CREATE TABLE IF NOT EXISTS geocode_cache (
        normalized_address TEXT PRIMARY KEY,
        lat                REAL,
        lng                REAL,
        matched_address    TEXT,
        provider           TEXT,
        confidence         TEXT,
        status             TEXT NOT NULL,
        error_message      TEXT,
        updated_at         TEXT NOT NULL
    )
    """,
]


# Schema v2 (Stage 1 flood exposure). Additive only; every statement is
# idempotent. permit_flood is a side table so ingest/common.py's upsert and
# coordinate-precedence logic never see it.
_SCHEMA_V2_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS flood_zone_loads (
        load_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        dfirm_id           TEXT NOT NULL,
        source_url         TEXT,
        source_vintage     TEXT,
        content_hash       TEXT NOT NULL,
        feature_count      INTEGER NOT NULL,
        display_bytes      INTEGER NOT NULL DEFAULT 0,
        max_cell_bytes     INTEGER NOT NULL DEFAULT 0,
        simplify_tolerance REAL,
        display_ok         INTEGER NOT NULL DEFAULT 0,
        loaded_at          TEXT NOT NULL,
        is_active          INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
    )
    """,
    # Exactly one active load per DFIRM, enforced by the database itself.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_flood_zone_loads_active ON flood_zone_loads(dfirm_id) WHERE is_active = 1",
    """
    CREATE TABLE IF NOT EXISTS flood_zones (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        load_id       INTEGER NOT NULL REFERENCES flood_zone_loads(load_id),
        fld_zone      TEXT,
        zone_subty    TEXT,
        sfha_tf       TEXT,
        flood_class   TEXT NOT NULL,
        geom_geojson  TEXT NOT NULL,
        minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL, maxy REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_flood_zones_load ON flood_zones(load_id)",
    """
    CREATE TABLE IF NOT EXISTS flood_display_cells (
        load_id  INTEGER NOT NULL REFERENCES flood_zone_loads(load_id),
        cell_x   INTEGER NOT NULL,
        cell_y   INTEGER NOT NULL,
        geojson  BLOB NOT NULL,
        bytes    INTEGER NOT NULL,
        etag     TEXT NOT NULL,
        PRIMARY KEY (load_id, cell_x, cell_y)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS flood_locality_dfirm (
        locality TEXT PRIMARY KEY,
        dfirm_id TEXT NOT NULL
    )
    """,
    # Virginia Beach's flood zones are filed under DFIRM_ID '515531' in FEMA's NFHL layer 28 (checked
    # against the live service 2026-10-28: 3,647 zones; '51810C' returns none). Databases seeded with
    # the old id are corrected by init_db on every migration; a load stored under '51810C' must be re-run.
    "INSERT OR IGNORE INTO flood_locality_dfirm (locality, dfirm_id) VALUES ('virginia_beach', '515531')",
    """
    CREATE TABLE IF NOT EXISTS permit_flood (
        permit_id    INTEGER PRIMARY KEY REFERENCES permits(id),
        load_id      INTEGER NOT NULL REFERENCES flood_zone_loads(load_id),
        tagged_lat   REAL,
        tagged_lng   REAL,
        fld_zone     TEXT,
        zone_subty   TEXT,
        sfha         INTEGER,
        flood_class  TEXT NOT NULL CHECK (flood_class IN
                     ('sfha_v','sfha_a','shaded_x','matched_non_sfha','no_polygon','not_geocoded')),
        tagged_at    TEXT NOT NULL
    )
    """,
    # The single definition of "is this tag current?" for every read path.
    # fresh requires an ACTIVE load to exist and coalesces to 0, so a NULL
    # comparison can never make an old tag look current. Zone details are
    # NULL unless fresh, so a stale zone can never sit beside 'untagged'.
    """
    CREATE VIEW IF NOT EXISTS permit_flood_effective AS
    SELECT s.permit_id, s.fresh,
      CASE WHEN NOT s.in_scope THEN 'out_of_scope'
           WHEN s.fresh = 1    THEN s.flood_class
           ELSE 'untagged' END                    AS effective_class,
      CASE WHEN s.fresh = 1 THEN s.fld_zone   END AS fld_zone,
      CASE WHEN s.fresh = 1 THEN s.zone_subty END AS zone_subty,
      CASE WHEN s.fresh = 1 THEN s.sfha       END AS sfha,
      CASE WHEN s.fresh = 1 THEN s.load_id    END AS load_id
    FROM (
      SELECT p.id AS permit_id,
             (m.locality IS NOT NULL) AS in_scope,
             COALESCE(l.load_id IS NOT NULL AND f.permit_id IS NOT NULL
                      AND f.load_id = l.load_id
                      AND f.tagged_lat IS p.lat AND f.tagged_lng IS p.lng, 0) AS fresh,
             f.flood_class, f.fld_zone, f.zone_subty, f.sfha, f.load_id
      FROM permits p
      LEFT JOIN flood_locality_dfirm m ON m.locality = p.locality
      LEFT JOIN flood_zone_loads    l ON l.dfirm_id = m.dfirm_id AND l.is_active = 1
      LEFT JOIN permit_flood        f ON f.permit_id = p.id
    ) s
    """,
]


# Schema v3 (Stage 2 land-use decisions -> permits). Additive only. Approval
# is never inferred from vote counts: decision_outcome_review (human, with
# evidence) is the only path to an approved outcome. decision_permit_links is
# fully derived (rebuilt by scripts/link_decisions_permits.py); the review
# tables are human input and survive rebuilds.
_SCHEMA_V3_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS land_use_decisions (
        decision_key       TEXT PRIMARY KEY,
        meeting_id         TEXT NOT NULL,
        resolution_id      TEXT NOT NULL,
        meeting_date       TEXT,
        title              TEXT,
        title_truncated    INTEGER NOT NULL DEFAULT 0,
        result_raw         TEXT,
        yes_count          INTEGER NOT NULL DEFAULT 0,
        no_count           INTEGER NOT NULL DEFAULT 0,
        absent_count       INTEGER NOT NULL DEFAULT 0,
        abstain_count      INTEGER NOT NULL DEFAULT 0,
        member_rows        INTEGER NOT NULL DEFAULT 0,
        outcome_evidence   TEXT NOT NULL DEFAULT 'unverified'
            CHECK (outcome_evidence IN ('unverified','proposed_approved','proposed_not_approved','unknown')),
        evidence_pattern   TEXT,
        land_use_label     TEXT NOT NULL,
        matched_pattern    TEXT NOT NULL,
        source_file_sha256 TEXT NOT NULL,
        ingested_at        TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_land_use_decisions_date ON land_use_decisions(meeting_date)",
    """
    CREATE TABLE IF NOT EXISTS land_use_decision_conflicts (
        decision_key  TEXT NOT NULL REFERENCES land_use_decisions(decision_key) ON DELETE CASCADE,
        field         TEXT NOT NULL CHECK (field IN ('title','result','meeting_date')),
        variants_json TEXT NOT NULL,
        detected_at   TEXT NOT NULL,
        PRIMARY KEY (decision_key, field)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS land_use_decision_addresses (
        decision_key   TEXT NOT NULL REFERENCES land_use_decisions(decision_key) ON DELETE CASCADE,
        seq            INTEGER NOT NULL,
        source         TEXT NOT NULL DEFAULT 'title' CHECK (source IN ('title','review')),
        raw_text       TEXT NOT NULL,
        house_from     INTEGER,
        house_to       INTEGER,
        street_norm    TEXT,
        gpin10         TEXT,
        lat            REAL,
        lng            REAL,
        geocode_status TEXT,
        geocoded_at    TEXT,
        PRIMARY KEY (decision_key, seq),
        CHECK (street_norm IS NOT NULL OR gpin10 IS NOT NULL)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_outcome_review (
        decision_key        TEXT PRIMARY KEY REFERENCES land_use_decisions(decision_key) ON DELETE CASCADE,
        verified_outcome    TEXT NOT NULL CHECK (verified_outcome IN
            ('approved','approved_with_conditions','denied','deferred','withdrawn','other')),
        evidence            TEXT NOT NULL CHECK (length(trim(evidence)) > 0),
        reviewer            TEXT NOT NULL,
        conflict_resolution TEXT,
        resolved_title      TEXT,
        reviewed_at         TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS permit_coverage (
        locality            TEXT PRIMARY KEY,
        backfill_from       TEXT NOT NULL,
        backfill_run_id     INTEGER NOT NULL,
        backfill_source     TEXT NOT NULL,
        last_refresh_at     TEXT,
        last_refresh_run_id INTEGER,
        link_window_months  INTEGER NOT NULL,
        max_staleness_days  INTEGER NOT NULL,
        computed_at         TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_permit_links (
        decision_key        TEXT NOT NULL REFERENCES land_use_decisions(decision_key) ON DELETE CASCADE,
        permit_id           INTEGER NOT NULL REFERENCES permits(id),
        match_tier          TEXT NOT NULL CHECK (match_tier IN ('gpin','address_exact','address_range','manual')),
        matched_on          TEXT,
        link_state          TEXT NOT NULL CHECK (link_state IN ('candidate','confirmed')),
        is_new_construction INTEGER,
        link_run_at         TEXT NOT NULL,
        PRIMARY KEY (decision_key, permit_id),
        CHECK (match_tier != 'manual' OR link_state = 'confirmed')
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decision_permit_links_permit ON decision_permit_links(permit_id)",
    """
    CREATE TABLE IF NOT EXISTS decision_link_review (
        decision_key TEXT NOT NULL,
        permit_id    INTEGER NOT NULL,
        verdict      TEXT NOT NULL CHECK (verdict IN ('confirmed','rejected')),
        note         TEXT,
        reviewer     TEXT NOT NULL,
        reviewed_at  TEXT NOT NULL,
        PRIMARY KEY (decision_key, permit_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_flood (
        decision_key TEXT NOT NULL,
        seq          INTEGER NOT NULL,
        load_id      INTEGER NOT NULL REFERENCES flood_zone_loads(load_id),
        tagged_lat   REAL,
        tagged_lng   REAL,
        fld_zone     TEXT,
        zone_subty   TEXT,
        sfha         INTEGER,
        flood_class  TEXT NOT NULL CHECK (flood_class IN
            ('sfha_v','sfha_a','shaded_x','matched_non_sfha','no_polygon','not_geocoded')),
        tagged_at    TEXT NOT NULL,
        PRIMARY KEY (decision_key, seq)
    )
    """,
    # Same freshness rule as permit_flood_effective: an active VB load must
    # exist, the tag must come from it, and the address coordinates must be
    # the ones tagged. Zone fields are NULL unless fresh.
    """
    CREATE VIEW IF NOT EXISTS decision_flood_effective AS
    SELECT s.decision_key, s.seq, s.fresh,
      CASE WHEN s.fresh = 1 THEN s.flood_class ELSE 'untagged' END AS effective_class,
      CASE WHEN s.fresh = 1 THEN s.fld_zone END AS fld_zone,
      CASE WHEN s.fresh = 1 THEN s.sfha     END AS sfha
    FROM (
      SELECT a.decision_key, a.seq,
             COALESCE(l.load_id IS NOT NULL AND f.decision_key IS NOT NULL AND f.load_id = l.load_id
                      AND f.tagged_lat IS a.lat AND f.tagged_lng IS a.lng, 0) AS fresh,
             f.flood_class, f.fld_zone, f.sfha
      FROM land_use_decision_addresses a
      LEFT JOIN flood_locality_dfirm m ON m.locality = 'virginia_beach'
      LEFT JOIN flood_zone_loads    l ON l.dfirm_id = m.dfirm_id AND l.is_active = 1
      LEFT JOIN decision_flood      f ON f.decision_key = a.decision_key AND f.seq = a.seq
    ) s
    """,
    # The single definition of a decision's status (first matching rule wins).
    # observation_uncertain precedes the link states on purpose: before the
    # verified backfill start, or with a stale/unknown refresh, even a found
    # permit may not be the FIRST one, so neither leads nor timings use it.
    """
    CREATE VIEW IF NOT EXISTS land_use_decision_status AS
    SELECT d.decision_key, d.meeting_date, d.land_use_label, d.outcome_evidence,
      r.verified_outcome,
      (SELECT COUNT(*) FROM decision_permit_links k WHERE k.decision_key = d.decision_key
                                                    AND k.link_state = 'candidate') AS n_candidate_links,
      (SELECT COUNT(*) FROM decision_permit_links k WHERE k.decision_key = d.decision_key
                                                    AND k.link_state = 'confirmed') AS n_confirmed_links,
      CASE
        WHEN EXISTS (SELECT 1 FROM land_use_decision_conflicts x WHERE x.decision_key = d.decision_key)
             AND (r.decision_key IS NULL OR r.conflict_resolution IS NULL
                  OR length(trim(r.conflict_resolution)) = 0)                  THEN 'quarantined'
        WHEN r.decision_key IS NULL                                            THEN 'outcome_unverified'
        WHEN r.verified_outcome NOT IN ('approved','approved_with_conditions') THEN 'not_approved'
        WHEN NOT EXISTS (SELECT 1 FROM land_use_decision_addresses a
                         WHERE a.decision_key = d.decision_key)                THEN 'address_unparsed'
        WHEN c.locality IS NULL OR d.meeting_date IS NULL OR d.meeting_date < c.backfill_from
             OR c.last_refresh_at IS NULL
             OR julianday('now') - julianday(c.last_refresh_at) > c.max_staleness_days
                                                                               THEN 'observation_uncertain'
        WHEN EXISTS (SELECT 1 FROM decision_permit_links k WHERE k.decision_key = d.decision_key
                                                              AND k.link_state = 'confirmed') THEN 'confirmed_permit'
        WHEN EXISTS (SELECT 1 FROM decision_permit_links k WHERE k.decision_key = d.decision_key)
                                                                               THEN 'candidate_permit'
        WHEN date(d.meeting_date, '+' || c.link_window_months || ' months') < date(c.last_refresh_at)
                                                                               THEN 'no_candidate_window_expired'
        ELSE 'no_candidate_permit'
      END AS status
    FROM land_use_decisions d
    LEFT JOIN decision_outcome_review r ON r.decision_key = d.decision_key
    LEFT JOIN permit_coverage c ON c.locality = 'virginia_beach'
    """,
]


def facts_fingerprint(title, result_raw, meeting_date, conflicts: dict | None) -> str:
    """SHA-256 of the facts an outcome review relies on: title, result,
    meeting date and the source-conflict variants ({field: {value: count}}).
    A review is valid only while its copy equals the decision's current
    value. Lives here (pure, no imports from ingest/) so the ingest and the
    v4 migration backfill hash identically."""
    payload = {"title": title, "result_raw": result_raw, "meeting_date": meeting_date,
               "conflicts": conflicts or {}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _backfill_fingerprints(conn) -> None:
    conflicts: dict = {}
    for r in conn.execute("SELECT decision_key, field, variants_json FROM land_use_decision_conflicts"):
        conflicts.setdefault(r["decision_key"], {})[r["field"]] = json.loads(r["variants_json"])
    rows = conn.execute("SELECT decision_key, title, result_raw, meeting_date FROM land_use_decisions "
                        "WHERE facts_fingerprint IS NULL").fetchall()
    conn.executemany("UPDATE land_use_decisions SET facts_fingerprint = ? WHERE decision_key = ?",
                     [(facts_fingerprint(r["title"], r["result_raw"], r["meeting_date"],
                                         conflicts.get(r["decision_key"])), r["decision_key"]) for r in rows])


# Schema v4 (Stage 2 fixes). Additive. (1) A review is valid only while the
# facts it was made on are unchanged (fingerprints); older reviews have no
# fingerprint and read as stale. (2) decision_permit_links_effective is the
# ONLY read path for links: a link counts only if its permit was applied for
# on/after the meeting date and within link_window_months of it (SQLite
# date() arithmetic -- the same function every writer uses).
_SCHEMA_V4_COLUMNS = (("land_use_decisions", "facts_fingerprint", "TEXT"),
                       ("decision_outcome_review", "reviewed_fingerprint", "TEXT"))
_SCHEMA_V4_STATEMENTS = [
    "DROP VIEW IF EXISTS land_use_decision_status",
    """
    CREATE VIEW IF NOT EXISTS decision_permit_links_effective AS
    SELECT k.decision_key, k.permit_id, k.match_tier, k.matched_on, k.link_state, k.is_new_construction,
           k.link_run_at, p.applied_date, p.issued_date, d.meeting_date
    FROM decision_permit_links k
    JOIN permits p ON p.id = k.permit_id
    JOIN land_use_decisions d ON d.decision_key = k.decision_key
    JOIN permit_coverage c ON c.locality = 'virginia_beach'
    WHERE d.meeting_date IS NOT NULL AND p.applied_date IS NOT NULL
      AND p.applied_date >= d.meeting_date
      AND p.applied_date <= date(d.meeting_date, '+' || c.link_window_months || ' months')
    """,
    """
    CREATE VIEW IF NOT EXISTS land_use_decision_status AS
    SELECT s.decision_key, s.meeting_date, s.land_use_label, s.outcome_evidence,
      CASE WHEN s.review_valid THEN s.review_outcome END AS verified_outcome,
      s.review_outcome AS reviewed_outcome_any,
      (SELECT COUNT(*) FROM decision_permit_links_effective k WHERE k.decision_key = s.decision_key
                                                              AND k.link_state = 'candidate') AS n_candidate_links,
      (SELECT COUNT(*) FROM decision_permit_links_effective k WHERE k.decision_key = s.decision_key
                                                              AND k.link_state = 'confirmed') AS n_confirmed_links,
      CASE
        WHEN EXISTS (SELECT 1 FROM land_use_decision_conflicts x WHERE x.decision_key = s.decision_key)
             AND NOT (s.review_valid AND length(trim(COALESCE(s.conflict_resolution, ''))) > 0) THEN 'quarantined'
        WHEN s.has_review AND NOT s.review_valid                                             THEN 'outcome_review_stale'
        WHEN NOT s.has_review                                                                THEN 'outcome_unverified'
        WHEN s.review_outcome NOT IN ('approved','approved_with_conditions')                 THEN 'not_approved'
        WHEN NOT EXISTS (SELECT 1 FROM land_use_decision_addresses a
                         WHERE a.decision_key = s.decision_key)                              THEN 'address_unparsed'
        WHEN c.locality IS NULL OR s.meeting_date IS NULL OR s.meeting_date < c.backfill_from
             OR c.last_refresh_at IS NULL
             OR julianday('now') - julianday(c.last_refresh_at) > c.max_staleness_days       THEN 'observation_uncertain'
        WHEN EXISTS (SELECT 1 FROM decision_permit_links_effective k WHERE k.decision_key = s.decision_key
                                                                     AND k.link_state = 'confirmed') THEN 'confirmed_permit'
        WHEN EXISTS (SELECT 1 FROM decision_permit_links_effective k
                     WHERE k.decision_key = s.decision_key)                                  THEN 'candidate_permit'
        WHEN date(s.meeting_date, '+' || c.link_window_months || ' months') < date(c.last_refresh_at)
                                                                                             THEN 'no_candidate_window_expired'
        ELSE 'no_candidate_permit'
      END AS status
    FROM (
      SELECT d.decision_key, d.meeting_date, d.land_use_label, d.outcome_evidence,
             (r.decision_key IS NOT NULL) AS has_review,
             COALESCE(r.reviewed_fingerprint IS NOT NULL AND d.facts_fingerprint IS NOT NULL
                      AND r.reviewed_fingerprint = d.facts_fingerprint, 0) AS review_valid,
             r.verified_outcome AS review_outcome, r.conflict_resolution
      FROM land_use_decisions d
      LEFT JOIN decision_outcome_review r ON r.decision_key = d.decision_key
    ) s
    LEFT JOIN permit_coverage c ON c.locality = 'virginia_beach'
    """,
]


# Schema v5 (Stage 3 parcel enrichment). Additive, except one copy-and-rename
# of the derived decision_permit_links table to widen its tier CHECK (adds
# 'same_parcel'): every row is copied, because an emptied links table would
# turn decisions into false no_candidate_permit leads.
_SCHEMA_V5_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS parcel_loads (
        load_id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source_url        TEXT NOT NULL,
        source_vintage    TEXT,
        content_hash      TEXT NOT NULL,
        feature_count     INTEGER NOT NULL,
        expected_count    INTEGER NOT NULL,
        received_count    INTEGER NOT NULL,
        unique_source_ids INTEGER NOT NULL,
        loaded_at         TEXT NOT NULL,
        is_active         INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
        CHECK (is_active = 0 OR (expected_count = received_count AND received_count = unique_source_ids))
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_parcel_loads_active ON parcel_loads(is_active) WHERE is_active = 1",
    """
    CREATE TABLE IF NOT EXISTS parcels (
        load_id        INTEGER NOT NULL REFERENCES parcel_loads(load_id),
        gpin           TEXT NOT NULL,
        gpin_digits    TEXT NOT NULL,
        gpin10         TEXT,
        zoning         TEXT,
        land_use       TEXT,
        prop_class     TEXT,
        tax_area_sqft  REAL,
        prop_address   TEXT,
        house          INTEGER,
        street_norm    TEXT,
        fema_zone_bldg TEXT,
        source_rows    INTEGER NOT NULL,
        geom_geojson   TEXT NOT NULL,
        minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL, maxy REAL NOT NULL,
        PRIMARY KEY (load_id, gpin)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_parcels_gpin10 ON parcels(load_id, gpin10)",
    "CREATE INDEX IF NOT EXISTS idx_parcels_digits ON parcels(load_id, gpin_digits)",
    "CREATE INDEX IF NOT EXISTS idx_parcels_street ON parcels(load_id, street_norm, house)",
    # One row per VB permit, written by scripts/match_parcels.py. match_fp is a
    # hash of the facts the match was made on (load, raw GPIN, coordinates,
    # result); a dispute review applies only while it equals reviewed_fp.
    """
    CREATE TABLE IF NOT EXISTS permit_parcel (
        permit_id        INTEGER PRIMARY KEY REFERENCES permits(id),
        load_id          INTEGER NOT NULL REFERENCES parcel_loads(load_id),
        method           TEXT NOT NULL CHECK (method IN
            ('gpin14','gpin10_unique','point_in_parcel','ambiguous','no_parcel','not_geocoded_no_gpin',
             'address_exact')),
        gpin             TEXT,
        alt_gpin         TEXT,
        disputed         INTEGER NOT NULL DEFAULT 0 CHECK (disputed IN (0, 1)),
        gpin_point_agree INTEGER,
        tagged_lat       REAL,
        tagged_lng       REAL,
        tagged_gpin_raw  TEXT,
        match_fp         TEXT NOT NULL,
        matched_at       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_parcel (
        decision_key  TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        load_id       INTEGER NOT NULL REFERENCES parcel_loads(load_id),
        method        TEXT NOT NULL CHECK (method IN
            ('gpin10_unique','address_exact','point_in_parcel','ambiguous','no_parcel')),
        gpin          TEXT,
        alt_gpin      TEXT,
        disputed      INTEGER NOT NULL DEFAULT 0 CHECK (disputed IN (0, 1)),
        tagged_gpin10 TEXT,
        tagged_house  INTEGER,
        tagged_house_to INTEGER,
        tagged_street TEXT,
        tagged_lat    REAL,
        tagged_lng    REAL,
        match_fp      TEXT NOT NULL,
        matched_at    TEXT NOT NULL,
        PRIMARY KEY (decision_key, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS parcel_match_review (
        subject_kind TEXT NOT NULL CHECK (subject_kind IN ('permit','decision')),
        subject_key  TEXT NOT NULL,
        verdict      TEXT NOT NULL CHECK (verdict IN ('gpin_correct','point_correct','unresolvable')),
        reviewed_fp  TEXT NOT NULL,
        reviewer     TEXT NOT NULL,
        note         TEXT,
        reviewed_at  TEXT NOT NULL,
        PRIMARY KEY (subject_kind, subject_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS parcel_flood (
        parcel_load_id       INTEGER NOT NULL REFERENCES parcel_loads(load_id),
        flood_load_id        INTEGER NOT NULL REFERENCES flood_zone_loads(load_id),
        gpin                 TEXT NOT NULL,
        share_sfha_v         REAL NOT NULL,
        share_sfha_a         REAL NOT NULL,
        share_shaded_x       REAL NOT NULL,
        share_other_mapped   REAL NOT NULL,
        share_unmapped       REAL NOT NULL,
        min_share            REAL NOT NULL,
        parcel_flood_class   TEXT NOT NULL CHECK (parcel_flood_class IN ('sfha_v','sfha_a','shaded_x',
            'matched_non_sfha','screening_sliver','screening_partial_coverage','no_polygon')),
        computed_at          TEXT NOT NULL,
        PRIMARY KEY (parcel_load_id, flood_load_id, gpin)
    )
    """,
    # Fresh only when the match was made against the ACTIVE parcel load and
    # the permit's coordinates and raw GPIN are the ones matched. link_gpin is
    # the parcel the same_parcel tier may use: NULL for ambiguous/no_parcel,
    # and NULL for a disputed match unless a current review resolved it.
    """
    CREATE VIEW IF NOT EXISTS permit_parcel_effective AS
    SELECT s.permit_id, s.fresh,
      CASE WHEN NOT s.in_scope THEN 'out_of_scope' WHEN s.fresh = 1 THEN s.method ELSE 'untagged' END AS parcel_state,
      CASE WHEN s.fresh = 1 THEN s.load_id END AS load_id,
      CASE WHEN s.fresh = 1 THEN s.method END AS method,
      CASE WHEN s.fresh = 1 THEN s.disputed END AS disputed,
      CASE WHEN s.fresh = 1 THEN s.verdict END AS review_verdict,
      CASE WHEN s.fresh = 1 THEN
        CASE WHEN s.verdict = 'point_correct' THEN s.alt_gpin ELSE s.gpin END END AS display_gpin,
      CASE WHEN s.fresh = 1 THEN s.alt_gpin END AS alt_gpin,
      CASE WHEN s.fresh = 1 AND s.gpin IS NOT NULL AND s.method NOT IN ('ambiguous','no_parcel') THEN
        CASE WHEN s.disputed = 0 THEN s.gpin
             WHEN s.verdict = 'gpin_correct' THEN s.gpin
             WHEN s.verdict = 'point_correct' THEN s.alt_gpin END END AS link_gpin,
      CASE WHEN s.fresh = 1 AND s.disputed = 1
                AND (s.verdict IS NULL OR s.verdict IN ('unresolvable', 'point_correct')) THEN 0 ELSE 1 END
        AS gpin_trusted
    FROM (
      SELECT p.id AS permit_id, (p.locality = 'virginia_beach') AS in_scope,
             COALESCE(l.load_id IS NOT NULL AND x.permit_id IS NOT NULL AND x.load_id = l.load_id
                      AND x.tagged_lat IS p.lat AND x.tagged_lng IS p.lng
                      AND x.tagged_gpin_raw IS json_extract(p.raw_json, '$.GPIN'), 0) AS fresh,
             x.load_id, x.method, x.gpin, x.alt_gpin, x.disputed, r.verdict
      FROM permits p
      LEFT JOIN parcel_loads l ON l.is_active = 1
      LEFT JOIN permit_parcel x ON x.permit_id = p.id
      LEFT JOIN parcel_match_review r ON r.subject_kind = 'permit' AND r.subject_key = CAST(p.id AS TEXT)
                                     AND r.reviewed_fp = x.match_fp
    ) s
    """,
    """
    CREATE VIEW IF NOT EXISTS decision_parcel_effective AS
    SELECT s.decision_key, s.seq, s.fresh,
      CASE WHEN s.fresh = 1 THEN s.method ELSE 'untagged' END AS parcel_state,
      CASE WHEN s.fresh = 1 THEN s.load_id END AS load_id,
      CASE WHEN s.fresh = 1 THEN s.method END AS method,
      CASE WHEN s.fresh = 1 THEN s.disputed END AS disputed,
      CASE WHEN s.fresh = 1 THEN s.verdict END AS review_verdict,
      CASE WHEN s.fresh = 1 THEN
        CASE WHEN s.verdict = 'point_correct' THEN s.alt_gpin ELSE s.gpin END END AS display_gpin,
      CASE WHEN s.fresh = 1 THEN s.alt_gpin END AS alt_gpin,
      CASE WHEN s.fresh = 1 AND s.gpin IS NOT NULL AND s.method NOT IN ('ambiguous','no_parcel') THEN
        CASE WHEN s.disputed = 0 THEN s.gpin
             WHEN s.verdict = 'gpin_correct' THEN s.gpin
             WHEN s.verdict = 'point_correct' THEN s.alt_gpin END END AS link_gpin,
      CASE WHEN s.fresh = 1 AND s.disputed = 1
                AND (s.verdict IS NULL OR s.verdict IN ('unresolvable', 'point_correct')) THEN 0 ELSE 1 END
        AS gpin_trusted
    FROM (
      SELECT a.decision_key, a.seq,
             COALESCE(l.load_id IS NOT NULL AND x.decision_key IS NOT NULL AND x.load_id = l.load_id
                      AND x.tagged_gpin10 IS a.gpin10 AND x.tagged_house IS a.house_from
                      AND x.tagged_house_to IS a.house_to AND x.tagged_street IS a.street_norm
                      AND x.tagged_lat IS a.lat AND x.tagged_lng IS a.lng, 0) AS fresh,
             x.load_id, x.method, x.gpin, x.alt_gpin, x.disputed, r.verdict
      FROM land_use_decision_addresses a
      LEFT JOIN parcel_loads l ON l.is_active = 1
      LEFT JOIN decision_parcel x ON x.decision_key = a.decision_key AND x.seq = a.seq
      LEFT JOIN parcel_match_review r ON r.subject_kind = 'decision'
                                     AND r.subject_key = a.decision_key || '#' || a.seq
                                     AND r.reviewed_fp = x.match_fp
    ) s
    """,
    # Parcel flood rows count only for the ACTIVE parcel load and the ACTIVE
    # Virginia Beach flood load.
    """
    CREATE VIEW IF NOT EXISTS parcel_flood_effective AS
    SELECT f.* FROM parcel_flood f
    JOIN parcel_loads pl ON pl.load_id = f.parcel_load_id AND pl.is_active = 1
    JOIN flood_locality_dfirm m ON m.locality = 'virginia_beach'
    JOIN flood_zone_loads fl ON fl.load_id = f.flood_load_id AND fl.dfirm_id = m.dfirm_id AND fl.is_active = 1
    """,
]
_LINKS_V5_TABLE = """
    CREATE TABLE decision_permit_links_v5 (
        decision_key        TEXT NOT NULL REFERENCES land_use_decisions(decision_key) ON DELETE CASCADE,
        permit_id           INTEGER NOT NULL REFERENCES permits(id),
        match_tier          TEXT NOT NULL CHECK (match_tier IN
            ('gpin','address_exact','same_parcel','address_range','manual')),
        matched_on          TEXT,
        link_state          TEXT NOT NULL CHECK (link_state IN ('candidate','confirmed')),
        is_new_construction INTEGER,
        link_run_at         TEXT NOT NULL,
        PRIMARY KEY (decision_key, permit_id),
        CHECK (match_tier != 'manual' OR link_state = 'confirmed')
    )
"""
_LINK_COLUMNS = "decision_key, permit_id, match_tier, matched_on, link_state, is_new_construction, link_run_at"


def _widen_link_tiers(conn) -> None:
    """Copy-and-rename decision_permit_links with 'same_parcel' allowed. The
    views over it are dropped first and recreated from the v4 definitions."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'decision_permit_links'").fetchone()[0]
    if "same_parcel" in sql:
        return
    before = conn.execute("SELECT COUNT(*) FROM decision_permit_links").fetchone()[0]
    conn.execute("DROP VIEW IF EXISTS land_use_decision_status")
    conn.execute("DROP VIEW IF EXISTS decision_permit_links_effective")
    conn.execute(_LINKS_V5_TABLE)
    conn.execute(f"INSERT INTO decision_permit_links_v5 ({_LINK_COLUMNS}) "
                 f"SELECT {_LINK_COLUMNS} FROM decision_permit_links")
    after = conn.execute("SELECT COUNT(*) FROM decision_permit_links_v5").fetchone()[0]
    if after != before:
        raise RuntimeError(f"decision_permit_links copy lost rows ({before} -> {after}); aborting migration")
    conn.execute("DROP TABLE decision_permit_links")
    conn.execute("ALTER TABLE decision_permit_links_v5 RENAME TO decision_permit_links")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_permit_links_permit ON decision_permit_links(permit_id)")
    for statement in _SCHEMA_V4_STATEMENTS[1:]:  # the two CREATE VIEW statements
        conn.execute(statement)


# Schema v6 (real-data hardening). (1) Cross-row reconciliation: a decision
# whose street-address parcel is not among its printed-GPIN parcels is a
# cross-row conflict; those rows feed no parcel or GPIN link until a
# decision-level verdict (decision_parcel_set_review, tied to set_fp) keeps
# them. (2) Status order: a stale outcome review reads outcome_review_stale
# even when the item also has source conflicts (has_conflicts shows both).
# (3) source_dates / quality_tallies for the runbook and /api/quality.
_SCHEMA_V6_COLUMNS = (("decision_parcel", "cross_row", "INTEGER NOT NULL DEFAULT 0"),
                      ("decision_parcel", "set_fp", "TEXT"))
_SCHEMA_V6_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS decision_parcel_set_review (
        decision_key TEXT PRIMARY KEY,
        verdict      TEXT NOT NULL CHECK (verdict IN
            ('rows_consistent','gpin_rows_correct','street_rows_correct','unresolvable')),
        reviewed_fp  TEXT NOT NULL,
        reviewer     TEXT NOT NULL,
        note         TEXT,
        reviewed_at  TEXT NOT NULL
    )
    """,
    # Source dates recorded by the CLI writers (runbook), served by /api/data-as-of.
    """
    CREATE TABLE IF NOT EXISTS source_dates (
        source      TEXT PRIMARY KEY,
        as_of       TEXT,
        detail_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """,
    # Accuracy tallies imported by scripts/accuracy_tally.py with the data
    # snapshot they were computed on; /api/quality withholds them when stale.
    """
    CREATE TABLE IF NOT EXISTS quality_tallies (
        tally_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        tallied_at    TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        result_json   TEXT NOT NULL,
        synthetic     INTEGER NOT NULL DEFAULT 0
    )
    """,
    "DROP VIEW IF EXISTS land_use_decision_status",
    """
    CREATE VIEW IF NOT EXISTS land_use_decision_status AS
    SELECT s.decision_key, s.meeting_date, s.land_use_label, s.outcome_evidence,
      CASE WHEN s.review_valid THEN s.review_outcome END AS verified_outcome,
      s.review_outcome AS reviewed_outcome_any,
      s.has_conflicts,
      (SELECT COUNT(*) FROM decision_permit_links_effective k WHERE k.decision_key = s.decision_key
                                                              AND k.link_state = 'candidate') AS n_candidate_links,
      (SELECT COUNT(*) FROM decision_permit_links_effective k WHERE k.decision_key = s.decision_key
                                                              AND k.link_state = 'confirmed') AS n_confirmed_links,
      CASE
        WHEN s.has_review AND NOT s.review_valid                                             THEN 'outcome_review_stale'
        WHEN s.has_conflicts
             AND NOT (s.review_valid AND length(trim(COALESCE(s.conflict_resolution, ''))) > 0) THEN 'quarantined'
        WHEN NOT s.has_review                                                                THEN 'outcome_unverified'
        WHEN s.review_outcome NOT IN ('approved','approved_with_conditions')                 THEN 'not_approved'
        WHEN NOT EXISTS (SELECT 1 FROM land_use_decision_addresses a
                         WHERE a.decision_key = s.decision_key)                              THEN 'address_unparsed'
        WHEN c.locality IS NULL OR s.meeting_date IS NULL OR s.meeting_date < c.backfill_from
             OR c.last_refresh_at IS NULL
             OR julianday('now') - julianday(c.last_refresh_at) > c.max_staleness_days       THEN 'observation_uncertain'
        WHEN EXISTS (SELECT 1 FROM decision_permit_links_effective k WHERE k.decision_key = s.decision_key
                                                                     AND k.link_state = 'confirmed') THEN 'confirmed_permit'
        WHEN EXISTS (SELECT 1 FROM decision_permit_links_effective k
                     WHERE k.decision_key = s.decision_key)                                  THEN 'candidate_permit'
        WHEN date(s.meeting_date, '+' || c.link_window_months || ' months') < date(c.last_refresh_at)
                                                                                             THEN 'no_candidate_window_expired'
        ELSE 'no_candidate_permit'
      END AS status
    FROM (
      SELECT d.decision_key, d.meeting_date, d.land_use_label, d.outcome_evidence,
             (r.decision_key IS NOT NULL) AS has_review,
             COALESCE(r.reviewed_fingerprint IS NOT NULL AND d.facts_fingerprint IS NOT NULL
                      AND r.reviewed_fingerprint = d.facts_fingerprint, 0) AS review_valid,
             r.verified_outcome AS review_outcome, r.conflict_resolution,
             EXISTS (SELECT 1 FROM land_use_decision_conflicts x WHERE x.decision_key = d.decision_key) AS has_conflicts
      FROM land_use_decisions d
      LEFT JOIN decision_outcome_review r ON r.decision_key = d.decision_key
    ) s
    LEFT JOIN permit_coverage c ON c.locality = 'virginia_beach'
    """,
    "DROP VIEW IF EXISTS decision_parcel_effective",
    """
    CREATE VIEW IF NOT EXISTS decision_parcel_effective AS
    SELECT s.decision_key, s.seq, s.fresh,
      CASE WHEN s.fresh = 1 THEN s.method ELSE 'untagged' END AS parcel_state,
      CASE WHEN s.fresh = 1 THEN s.load_id END AS load_id,
      CASE WHEN s.fresh = 1 THEN s.method END AS method,
      CASE WHEN s.fresh = 1 THEN s.disputed END AS disputed,
      CASE WHEN s.fresh = 1 THEN s.cross_row END AS cross_row_conflict,
      CASE WHEN s.fresh = 1 THEN
        CASE WHEN s.disputed = 1 AND s.cross_row = 1 THEN 'both' WHEN s.disputed = 1 THEN 'within_row'
             WHEN s.cross_row = 1 THEN 'cross_row' END END AS dispute_reason,
      CASE WHEN s.fresh = 1 THEN s.verdict END AS review_verdict,
      CASE WHEN s.fresh = 1 THEN s.set_verdict END AS set_verdict,
      CASE WHEN s.fresh = 1 THEN
        CASE WHEN s.verdict = 'point_correct' THEN s.alt_gpin ELSE s.gpin END END AS display_gpin,
      CASE WHEN s.fresh = 1 THEN s.alt_gpin END AS alt_gpin,
      CASE WHEN s.fresh = 1 AND s.gpin IS NOT NULL AND s.method NOT IN ('ambiguous','no_parcel') AND s.set_ok = 1 THEN
        CASE WHEN s.disputed = 0 THEN s.gpin
             WHEN s.verdict = 'gpin_correct' THEN s.gpin
             WHEN s.verdict = 'point_correct' THEN s.alt_gpin END END AS link_gpin,
      CASE WHEN s.fresh = 1 AND (s.set_ok = 0 OR (s.disputed = 1
                AND (s.verdict IS NULL OR s.verdict IN ('unresolvable', 'point_correct')))) THEN 0 ELSE 1 END
        AS gpin_trusted
    FROM (
      SELECT t.*,
             -- a cross-row conflicted row is usable only with a current decision-level verdict that keeps it
             CASE WHEN COALESCE(t.cross_row, 0) = 0 THEN 1
                  WHEN t.set_verdict = 'rows_consistent' THEN 1
                  WHEN t.set_verdict = 'gpin_rows_correct' AND t.is_gpin_row = 1 THEN 1
                  WHEN t.set_verdict = 'street_rows_correct' AND t.is_gpin_row = 0 THEN 1
                  ELSE 0 END AS set_ok
      FROM (
        SELECT a.decision_key, a.seq, (a.gpin10 IS NOT NULL) AS is_gpin_row,
               COALESCE(l.load_id IS NOT NULL AND x.decision_key IS NOT NULL AND x.load_id = l.load_id
                        AND x.tagged_gpin10 IS a.gpin10 AND x.tagged_house IS a.house_from
                        AND x.tagged_house_to IS a.house_to AND x.tagged_street IS a.street_norm
                        AND x.tagged_lat IS a.lat AND x.tagged_lng IS a.lng
                        -- not reconciled across rows yet (e.g. right after the v6 migration) => not current
                        AND x.set_fp IS NOT NULL, 0) AS fresh,
               x.load_id, x.method, x.gpin, x.alt_gpin, x.disputed, x.cross_row, r.verdict, sr.verdict AS set_verdict
        FROM land_use_decision_addresses a
        LEFT JOIN parcel_loads l ON l.is_active = 1
        LEFT JOIN decision_parcel x ON x.decision_key = a.decision_key AND x.seq = a.seq
        LEFT JOIN parcel_match_review r ON r.subject_kind = 'decision'
                                       AND r.subject_key = a.decision_key || '#' || a.seq
                                       AND r.reviewed_fp = x.match_fp
        LEFT JOIN decision_parcel_set_review sr ON sr.decision_key = a.decision_key AND sr.reviewed_fp = x.set_fp
      ) t
    ) s
    """,
]


# Schema v7 (first real run). permit_backfill_checks records the proof that a
# permit backfill is complete: the source's own count and OBJECTID set for the
# backfill's where clause vs what the database holds. The linker accepts a
# backfill run only with a verified check (land_use_link.verify_coverage).
_SCHEMA_V7_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS permit_backfill_checks (
        check_id           INTEGER PRIMARY KEY AUTOINCREMENT,
        backfill_run_id    INTEGER NOT NULL,
        where_clause       TEXT NOT NULL,
        source_count       INTEGER NOT NULL,
        source_ids         INTEGER NOT NULL,
        db_ids             INTEGER NOT NULL,
        truly_missing      INTEGER NOT NULL,
        dup_permit_number  INTEGER NOT NULL,
        no_permit_number   INTEGER NOT NULL,
        extra_in_db        INTEGER NOT NULL,
        dup_objectids_db   INTEGER NOT NULL,
        null_date_source   INTEGER NOT NULL,
        topped_up          INTEGER NOT NULL DEFAULT 0,
        per_year_json      TEXT NOT NULL,
        verified           INTEGER NOT NULL CHECK (verified IN (0, 1)),
        checked_at         TEXT NOT NULL
    )
    """,
]


# Schema v8 (review history). Every change to a human review is recorded by
# triggers in review_audit, attributed to the import (file + SHA-256 + acting
# user) that made it. A review write outside db.review_import() aborts, so a
# write can never inherit another import's file. review_audit and
# review_imports are append-only: UPDATE, DELETE and INSERT OR REPLACE onto an
# existing row abort.
#
# Not authenticated and not tamper-proof: acting_user is self-reported, and
# anyone who can write the file can drop the triggers. A future migration
# that rebuilds a review table (copy-and-rename) must recreate its triggers.
#
# INSERT OR REPLACE on a review table fires BEFORE INSERT and AFTER INSERT
# (no delete trigger), and an upsert fires BEFORE INSERT then UPDATE: so the
# BEFORE INSERT trigger only stashes the existing row in review_audit_pending,
# and the AFTER INSERT ("replace" when a row was stashed) or AFTER UPDATE
# ("update") trigger writes the audit row and clears the stash.
REVIEW_TABLES = {
    # table: (primary-key columns -> the subject, every column)
    "decision_outcome_review": (("decision_key",),
                                ("conflict_resolution", "decision_key", "evidence", "resolved_title", "reviewed_at",
                                 "reviewed_fingerprint", "reviewer", "verified_outcome")),
    "decision_link_review": (("decision_key", "permit_id"),
                             ("decision_key", "note", "permit_id", "reviewed_at", "reviewer", "verdict")),
    "parcel_match_review": (("subject_key", "subject_kind"),
                            ("note", "reviewed_at", "reviewed_fp", "reviewer", "subject_key", "subject_kind",
                             "verdict")),
    "decision_parcel_set_review": (("decision_key",),
                                   ("decision_key", "note", "reviewed_at", "reviewed_fp", "reviewer", "verdict")),
}
REVIEW_IMPORT_KINDS = ("outcomes", "links", "parcel", "accuracy_apply", "migration_baseline")
REVIEW_AUDIT_ACTIONS = ("insert", "update", "delete", "replace", "baseline_snapshot")
_NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
_NO_CONTEXT = "SELECT RAISE(ABORT, 'review write outside an import (use db.review_import)') " \
              "WHERE NOT EXISTS (SELECT 1 FROM review_write_context)"
_CONTEXT_ID = "(SELECT import_id FROM review_write_context WHERE id = 1)"


def _json_obj(alias: str, cols) -> str:
    # canonical JSON: keys in sorted order (cols are listed sorted), no spaces -- same as
    # json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "json_object(" + ", ".join(f"'{c}', {alias}.{c}" for c in sorted(cols)) + ")"


def _review_triggers(table: str) -> list[str]:
    keys, cols = REVIEW_TABLES[table]
    subj = lambda a: _json_obj(a, keys)  # noqa: E731
    match = " AND ".join(f"t.{k} = NEW.{k}" for k in keys)
    audit = ("INSERT INTO review_audit (import_id, review_table, subject_json, action, old_json, new_json, "
             "review_author, recorded_at)")
    pending = f"review_audit_pending WHERE review_table = '{table}' AND subject_json = {subj('NEW')}"
    return [
        f"""CREATE TRIGGER IF NOT EXISTS {table}_audit_bi BEFORE INSERT ON {table} BEGIN
              {_NO_CONTEXT};
              INSERT INTO review_audit_pending (review_table, subject_json, old_json, review_author)
                SELECT '{table}', {subj('t')}, {_json_obj('t', cols)}, t.reviewer FROM {table} t WHERE {match};
            END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_audit_ai AFTER INSERT ON {table} BEGIN
              {audit} VALUES ({_CONTEXT_ID}, '{table}', {subj('NEW')},
                CASE WHEN EXISTS (SELECT 1 FROM {pending}) THEN 'replace' ELSE 'insert' END,
                (SELECT old_json FROM {pending}), {_json_obj('NEW', cols)}, NEW.reviewer, {_NOW_SQL});
              DELETE FROM {pending};
            END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_audit_bu BEFORE UPDATE ON {table} BEGIN {_NO_CONTEXT}; END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_audit_au AFTER UPDATE ON {table} BEGIN
              DELETE FROM {pending};
              {audit} VALUES ({_CONTEXT_ID}, '{table}', {subj('NEW')}, 'update',
                {_json_obj('OLD', cols)}, {_json_obj('NEW', cols)}, NEW.reviewer, {_NOW_SQL});
            END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_audit_bd BEFORE DELETE ON {table} BEGIN {_NO_CONTEXT}; END""",
        # review_author on a delete is the author of the REMOVED review, not whoever deleted it
        f"""CREATE TRIGGER IF NOT EXISTS {table}_audit_ad AFTER DELETE ON {table} BEGIN
              {audit} VALUES ({_CONTEXT_ID}, '{table}', {subj('OLD')}, 'delete',
                {_json_obj('OLD', cols)}, NULL, OLD.reviewer, {_NOW_SQL});
            END""",
    ]


def _append_only(table: str, pk: str) -> list[str]:
    return [
        f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
        f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
        # INSERT OR REPLACE / REPLACE INTO onto an existing row would delete it without a
        # delete trigger (recursive_triggers is off by default): refuse it here
        f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} "
        f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {pk} = NEW.{pk}) "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
    ]


_SCHEMA_V8_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS review_imports (
        import_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        kind           TEXT NOT NULL CHECK (kind IN {REVIEW_IMPORT_KINDS}),
        source_file    TEXT,
        source_sha256  TEXT,
        derived_file   TEXT,
        derived_sha256 TEXT,
        acting_user    TEXT,  -- self-reported (--actor or the OS user); not authenticated
        started_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_write_context (
        id        INTEGER PRIMARY KEY CHECK (id = 1),
        import_id INTEGER NOT NULL REFERENCES review_imports(import_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_audit_pending (
        review_table  TEXT NOT NULL,
        subject_json  TEXT NOT NULL,
        old_json      TEXT NOT NULL,
        review_author TEXT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS review_audit (
        audit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        import_id     INTEGER NOT NULL REFERENCES review_imports(import_id),
        review_table  TEXT NOT NULL CHECK (review_table IN {tuple(REVIEW_TABLES)}),
        subject_json  TEXT NOT NULL,
        action        TEXT NOT NULL CHECK (action IN {REVIEW_AUDIT_ACTIONS}),
        old_json      TEXT,
        new_json      TEXT,
        review_author TEXT,  -- the reviewer named in the review row (on delete: the removed row's)
        recorded_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_review_audit_subject ON review_audit(review_table, subject_json)",
    *_append_only("review_audit", "audit_id"),
    *_append_only("review_imports", "import_id"),
    # audit rows only ever come from an import, and carry that import's id
    f"""CREATE TRIGGER IF NOT EXISTS review_audit_context BEFORE INSERT ON review_audit
        WHEN NEW.import_id IS NOT {_CONTEXT_ID}
        BEGIN SELECT RAISE(ABORT, 'review_audit rows must belong to the current import'); END""",
    *[t for table in REVIEW_TABLES for t in _review_triggers(table)],
]


def review_subject(table: str, **key) -> str:
    """The canonical subject_json for one review row (same bytes the triggers write)."""
    keys, _ = REVIEW_TABLES[table]
    if set(key) != set(keys):
        raise ValueError(f"{table} is keyed by {list(keys)}, got {sorted(key)}")
    return json.dumps(key, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def default_actor() -> str:
    import getpass
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


@contextmanager
def review_import(conn, kind: str, source_file: Path | str | None, *, derived_file: Path | str | None = None,
                  actor: str | None = None):
    """One transaction for an import's review writes and its provenance: the
    review_imports row, the write context, the caller's writes, then the
    context cleared -- committed together or rolled back together."""
    if conn.in_transaction:
        raise RuntimeError("review_import must start its own transaction (commit pending writes first)")
    conn.execute("BEGIN IMMEDIATE")
    try:
        import_id = conn.execute(
            "INSERT INTO review_imports (kind, source_file, source_sha256, derived_file, derived_sha256, acting_user, "
            "started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, None if source_file is None else str(source_file),
             None if source_file is None else file_sha256(source_file),
             None if derived_file is None else str(derived_file),
             None if derived_file is None else file_sha256(derived_file),
             actor or default_actor(), now_iso())).lastrowid
        conn.execute("INSERT INTO review_write_context (id, import_id) VALUES (1, ?)", (import_id,))
        yield import_id
        if conn.execute("SELECT COUNT(*) FROM review_audit_pending").fetchone()[0]:
            raise RuntimeError("review_audit_pending not empty at the end of an import")
        conn.execute("DELETE FROM review_write_context")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _baseline_review_history(conn) -> None:
    """v8: one baseline_snapshot per existing review. Earlier history and the
    original files were never recorded, so provenance stays NULL."""
    if not any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in REVIEW_TABLES):
        return
    import_id = conn.execute("INSERT INTO review_imports (kind, started_at) VALUES ('migration_baseline', ?)",
                             (now_iso(),)).lastrowid
    conn.execute("INSERT INTO review_write_context (id, import_id) VALUES (1, ?)", (import_id,))
    for table, (keys, cols) in REVIEW_TABLES.items():
        conn.execute(
            f"INSERT INTO review_audit (import_id, review_table, subject_json, action, old_json, new_json, "
            f"review_author, recorded_at) SELECT ?, '{table}', {_json_obj('t', keys)}, 'baseline_snapshot', NULL, "
            f"{_json_obj('t', cols)}, t.reviewer, ? FROM {table} t", (import_id, now_iso()))
    conn.execute("DELETE FROM review_write_context")


# Schema v9 (Planning Commission hearings, Virginia Beach). Agendas and minutes
# are PDFs; pc_documents records each one (URL, SHA-256, parser version,
# parse status), pc_items one row per agenda item with its raw block, and
# pc_recommendations one row per item number from the minutes. A PC
# recommendation is advice to City Council: pc_item_status never yields an
# approval, and nothing here touches Council decision status or links.
PC_STATUSES = ("scheduled", "recommended_approval", "recommended_denial", "deferred", "withdrawn",
               "recommendation_unmapped", "no_recommendation_found")
_SCHEMA_V9_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS pc_documents (
        doc_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind            TEXT NOT NULL CHECK (kind IN ('agenda', 'minutes')),
        url             TEXT NOT NULL UNIQUE,
        meeting_date    TEXT,
        sha256          TEXT NOT NULL,
        bytes           INTEGER NOT NULL,
        fetched_at      TEXT NOT NULL,
        parser_version  TEXT NOT NULL,
        parse_status    TEXT NOT NULL CHECK (parse_status IN ('ok', 'partial', 'failed')),
        n_items         INTEGER NOT NULL DEFAULT 0,
        warnings_json   TEXT NOT NULL DEFAULT '[]',
        notes_json      TEXT NOT NULL DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_items (
        item_key              TEXT PRIMARY KEY,
        doc_id                INTEGER NOT NULL REFERENCES pc_documents(doc_id),
        meeting_date          TEXT NOT NULL,
        item_label            TEXT NOT NULL,
        item_numbers_json     TEXT NOT NULL,
        section               TEXT,
        applicants_json       TEXT NOT NULL,
        owners_json           TEXT NOT NULL,
        request               TEXT,
        notes_json            TEXT NOT NULL DEFAULT '[]',
        addresses             TEXT,
        gpins_json            TEXT NOT NULL,
        gpin10s_json          TEXT NOT NULL,
        council_district      INTEGER,
        council_district_text TEXT,
        accela_json           TEXT NOT NULL,
        staff_planner         TEXT,
        description           TEXT,
        city_initiated        INTEGER NOT NULL DEFAULT 0,
        agenda_result_raw     TEXT,
        agenda_result         TEXT,
        agenda_council_on     TEXT,
        raw_block             TEXT NOT NULL,
        warnings_json         TEXT NOT NULL DEFAULT '[]',
        facts_fp              TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pc_items_meeting ON pc_items(meeting_date)",
    """
    CREATE TABLE IF NOT EXISTS pc_recommendations (
        meeting_date        TEXT NOT NULL,
        item_number         INTEGER NOT NULL,
        minutes_doc_id      INTEGER NOT NULL REFERENCES pc_documents(doc_id),
        heading             TEXT,
        recommendation_raw  TEXT,
        recommendation      TEXT NOT NULL,
        aye INTEGER, nay INTEGER, abstain INTEGER, absent INTEGER,
        raw_block           TEXT,
        PRIMARY KEY (meeting_date, item_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_council_candidates (
        item_key            TEXT NOT NULL REFERENCES pc_items(item_key) ON DELETE CASCADE,
        decision_key        TEXT NOT NULL REFERENCES land_use_decisions(decision_key) ON DELETE CASCADE,
        matched_on          TEXT NOT NULL CHECK (matched_on IN ('gpin', 'address')),
        council_after_days  INTEGER NOT NULL,
        linked_at           TEXT NOT NULL,
        PRIMARY KEY (item_key, decision_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_index_checks (
        check_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        index_url    TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ('ok', 'blocked', 'error')),
        links_found  INTEGER NOT NULL DEFAULT 0,
        detail       TEXT,
        checked_at   TEXT NOT NULL
    )
    """,
    """
    CREATE VIEW IF NOT EXISTS pc_item_status AS
    SELECT i.item_key, i.meeting_date, r.recommendation AS minutes_recommendation, r.recommendation_raw,
           r.aye, r.nay, r.abstain, r.absent,
           (SELECT COUNT(*) FROM pc_council_candidates c WHERE c.item_key = i.item_key) AS n_council_candidates,
           CASE
             WHEN r.recommendation = 'unmapped' THEN 'recommendation_unmapped'
             WHEN r.recommendation IS NOT NULL THEN r.recommendation
             WHEN i.agenda_result = 'unmapped' THEN 'recommendation_unmapped'
             WHEN i.agenda_result IS NOT NULL THEN i.agenda_result
             WHEN EXISTS (SELECT 1 FROM pc_documents d WHERE d.kind = 'minutes'
                          AND d.meeting_date = i.meeting_date AND d.parse_status != 'failed')
               THEN 'no_recommendation_found'
             ELSE 'scheduled'
           END AS status,
           CASE WHEN r.recommendation IS NOT NULL THEN 'minutes'
                WHEN i.agenda_result IS NOT NULL THEN 'annotated_agenda' END AS status_source
    FROM pc_items i
    LEFT JOIN pc_recommendations r ON r.rowid = (
        SELECT p.rowid FROM pc_recommendations p, json_each(i.item_numbers_json) j
        WHERE p.meeting_date = i.meeting_date AND p.item_number = j.value ORDER BY p.item_number LIMIT 1)
    """,
]


# Schema v10 (filing-date leads, Virginia Beach). The city's "Planning Commission
# and City Council Actions" layer maps every land-use application with its
# filing date and Accela record. A load activates only when complete (count
# and OBJECTID set match); polygons sharing an Accela ID become one filing.
# The layer has no status: a filing's stage comes only from the Planning
# Commission items that carry its Accela ID -- never "approved".
_SCHEMA_V10_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS filing_loads (
        load_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        source_url      TEXT NOT NULL,
        where_clause    TEXT NOT NULL,
        expected_count  INTEGER NOT NULL,
        received_count  INTEGER NOT NULL,
        unique_ids      INTEGER NOT NULL,
        no_date         INTEGER NOT NULL,
        filings         INTEGER NOT NULL,
        content_hash    TEXT NOT NULL,
        max_date_sub    TEXT,
        parcel_load_id  INTEGER,
        loaded_at       TEXT NOT NULL,
        is_active       INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
        CHECK (is_active = 0 OR (expected_count = received_count AND received_count = unique_ids))
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_filing_loads_active ON filing_loads(is_active) WHERE is_active = 1",
    """
    CREATE TABLE IF NOT EXISTS filings (
        load_id            INTEGER NOT NULL REFERENCES filing_loads(load_id),
        pln_id             TEXT NOT NULL,
        objectids_json     TEXT NOT NULL,
        date_sub           TEXT NOT NULL,
        pln_type           TEXT,
        pln_file_no        TEXT,
        proj_name          TEXT,
        appl_name          TEXT,
        deeplink           TEXT,
        geom_geojson       TEXT NOT NULL,
        minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL, maxy REAL NOT NULL,
        lat REAL NOT NULL, lng REAL NOT NULL,
        gpins_json         TEXT NOT NULL,
        PRIMARY KEY (load_id, pln_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_filings_date ON filings(load_id, date_sub)",
    """
    CREATE VIEW IF NOT EXISTS filing_status AS
    SELECT f.pln_id, f.date_sub, f.pln_type,
           (SELECT i.item_key FROM pc_items i, json_each(i.accela_json) a WHERE a.value = f.pln_id
            ORDER BY i.meeting_date DESC LIMIT 1) AS pc_item_key,
           (SELECT s.status FROM pc_items i, json_each(i.accela_json) a
                JOIN pc_item_status s ON s.item_key = i.item_key
            WHERE a.value = f.pln_id ORDER BY i.meeting_date DESC LIMIT 1) AS pc_status,
           (SELECT i.meeting_date FROM pc_items i, json_each(i.accela_json) a WHERE a.value = f.pln_id
            ORDER BY i.meeting_date DESC LIMIT 1) AS hearing_date,
           (SELECT COALESCE(SUM(s.n_council_candidates), 0) FROM pc_items i, json_each(i.accela_json) a
                JOIN pc_item_status s ON s.item_key = i.item_key WHERE a.value = f.pln_id) AS n_council_candidates,
           (SELECT COUNT(DISTINCT p.id) FROM json_each(f.gpins_json) g
                JOIN permit_parcel_effective pp ON pp.display_gpin = g.value
                JOIN permits p ON p.id = pp.permit_id
            WHERE p.applied_date >= f.date_sub) AS permits_after_filing
    FROM filings f JOIN filing_loads l ON l.load_id = f.load_id AND l.is_active = 1
    """,
]


# Schema v11 (City Clerk's Summary of Council Actions, Virginia Beach). The
# official record of what Council did with each item, read from the Clerk's
# archive (OCR text of scanned summaries, ingest/clerk_summary.py). Each
# summary is one clerk_documents row; clerk_items one row per agenda item
# (the subject, applicant text and raw block are kept for review and never
# served). Links to Planning Commission items need the same street address,
# a Council date within 12 months after the hearing, and no disagreement on
# request type; anything else is stored as ambiguous and changes no stage.
# pc_council_outcomes is fully derived (scripts/link_clerk.py rebuilds it).
CLERK_OUTCOMES = ("approved", "approved_with_conditions", "denied", "deferred", "deferred_indefinitely", "withdrawn",
                  "unmapped")
COUNCIL_STAGES = ("council_approved", "council_approved_with_conditions", "council_denied", "council_withdrawn",
                  "council_deferred", "council_conflicting")
_SCHEMA_V11_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS clerk_documents (
        entry_id        INTEGER PRIMARY KEY,
        folder_id       INTEGER NOT NULL,
        year            INTEGER NOT NULL,
        name            TEXT NOT NULL,
        meeting_date    TEXT,
        page_count      INTEGER NOT NULL,
        pages_with_text INTEGER NOT NULL,
        text_sha256     TEXT NOT NULL,
        source_modified TEXT,
        fetched_at      TEXT NOT NULL,
        parser_version  TEXT NOT NULL,
        parse_status    TEXT NOT NULL CHECK (parse_status IN ('ok', 'partial', 'failed')),
        n_items         INTEGER NOT NULL DEFAULT 0,
        n_land_use      INTEGER NOT NULL DEFAULT 0,
        warnings_json   TEXT NOT NULL DEFAULT '[]'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clerk_documents_date ON clerk_documents(meeting_date)",
    f"""
    CREATE TABLE IF NOT EXISTS clerk_items (
        item_key           TEXT PRIMARY KEY,
        entry_id           INTEGER NOT NULL REFERENCES clerk_documents(entry_id) ON DELETE CASCADE,
        meeting_date       TEXT NOT NULL,
        page               INTEGER NOT NULL,
        item_label         TEXT,
        label_certain      INTEGER NOT NULL DEFAULT 1,
        is_land_use        INTEGER NOT NULL DEFAULT 0,
        request_types_json TEXT NOT NULL DEFAULT '[]',
        applicant_text     TEXT,
        council_district   INTEGER,
        motion_raw         TEXT,
        outcome            TEXT NOT NULL CHECK (outcome IN ({",".join(repr(o) for o in CLERK_OUTCOMES)})),
        deferred_to        TEXT,
        aye                INTEGER,
        nay                INTEGER,
        tally_raw          TEXT,
        subject            TEXT NOT NULL,
        raw_block          TEXT NOT NULL,
        warnings_json      TEXT NOT NULL DEFAULT '[]',
        updated_at         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clerk_items_date ON clerk_items(meeting_date)",
    """
    CREATE TABLE IF NOT EXISTS clerk_item_addresses (
        item_key    TEXT NOT NULL REFERENCES clerk_items(item_key) ON DELETE CASCADE,
        seq         INTEGER NOT NULL,
        raw_text    TEXT NOT NULL,
        house_from  INTEGER,
        house_to    INTEGER,
        street_norm TEXT NOT NULL,
        PRIMARY KEY (item_key, seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clerk_item_addresses_street ON clerk_item_addresses(street_norm)",
    """
    CREATE TABLE IF NOT EXISTS clerk_pc_links (
        clerk_item_key     TEXT NOT NULL REFERENCES clerk_items(item_key) ON DELETE CASCADE,
        pc_item_key        TEXT NOT NULL REFERENCES pc_items(item_key) ON DELETE CASCADE,
        matched_on         TEXT NOT NULL CHECK (matched_on IN ('address', 'address_type')),
        days_after_hearing INTEGER NOT NULL,
        ambiguous          INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),
        reason             TEXT,
        linked_at          TEXT NOT NULL,
        PRIMARY KEY (clerk_item_key, pc_item_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clerk_pc_links_pc ON clerk_pc_links(pc_item_key)",
    f"""
    CREATE TABLE IF NOT EXISTS pc_council_outcomes (
        pc_item_key    TEXT PRIMARY KEY REFERENCES pc_items(item_key) ON DELETE CASCADE,
        clerk_item_key TEXT NOT NULL REFERENCES clerk_items(item_key) ON DELETE CASCADE,
        council_date   TEXT NOT NULL,
        stage          TEXT NOT NULL CHECK (stage IN ({",".join(repr(s) for s in COUNCIL_STAGES)})),
        deferred_to    TEXT,
        n_actions      INTEGER NOT NULL,
        computed_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clerk_decision_links (
        clerk_item_key TEXT NOT NULL REFERENCES clerk_items(item_key) ON DELETE CASCADE,
        decision_key   TEXT NOT NULL REFERENCES land_use_decisions(decision_key) ON DELETE CASCADE,
        linked_at      TEXT NOT NULL,
        PRIMARY KEY (clerk_item_key, decision_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clerk_index_checks (
        check_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        year        INTEGER NOT NULL,
        folder_id   INTEGER,
        listed      INTEGER NOT NULL DEFAULT 0,
        stored      INTEGER NOT NULL DEFAULT 0,
        status      TEXT NOT NULL CHECK (status IN ('ok', 'incomplete', 'error')),
        detail      TEXT,
        checked_at  TEXT NOT NULL
    )
    """,
    # filing_status gains the Council stage (from the Clerk's record, via the PC item)
    "DROP VIEW IF EXISTS filing_status",
    """
    CREATE VIEW filing_status AS
    SELECT x.*, COALESCE(o.stage, x.pc_status, 'filed') AS stage, o.council_date, o.clerk_item_key
    FROM (
      SELECT f.pln_id, f.date_sub, f.pln_type,
             (SELECT i.item_key FROM pc_items i, json_each(i.accela_json) a WHERE a.value = f.pln_id
              ORDER BY i.meeting_date DESC LIMIT 1) AS pc_item_key,
             (SELECT s.status FROM pc_items i, json_each(i.accela_json) a
                  JOIN pc_item_status s ON s.item_key = i.item_key
              WHERE a.value = f.pln_id ORDER BY i.meeting_date DESC LIMIT 1) AS pc_status,
             (SELECT i.meeting_date FROM pc_items i, json_each(i.accela_json) a WHERE a.value = f.pln_id
              ORDER BY i.meeting_date DESC LIMIT 1) AS hearing_date,
             (SELECT COALESCE(SUM(s.n_council_candidates), 0) FROM pc_items i, json_each(i.accela_json) a
                  JOIN pc_item_status s ON s.item_key = i.item_key WHERE a.value = f.pln_id) AS n_council_candidates,
             (SELECT COUNT(DISTINCT p.id) FROM json_each(f.gpins_json) g
                  JOIN permit_parcel_effective pp ON pp.display_gpin = g.value
                  JOIN permits p ON p.id = pp.permit_id
              WHERE p.applied_date >= f.date_sub) AS permits_after_filing
      FROM filings f JOIN filing_loads l ON l.load_id = f.load_id AND l.is_active = 1
    ) x LEFT JOIN pc_council_outcomes o ON o.pc_item_key = x.pc_item_key
    """,
]


# Schema v12 (site constraints, Virginia Beach). City GIS layers that limit what
# a site can do -- Chesapeake Bay RPA boundary, Southern Watersheds buffer,
# floodplains subject to special restrictions, soils, historic overlays, dam
# break zones -- loaded complete or not at all (one active load per layer),
# and measured per parcel: area share for polygon layers, crossing length and
# distance for line layers (screening estimates, never a determination).
# Board actions (CBPA, Wetlands Board, Historical Review Board) are the
# parcel's history with those boards, merged per Accela record.
CONSTRAINT_KINDS = ("polygon_share", "line_crossing")
_SCHEMA_V12_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS constraint_loads (
        load_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        layer_key       TEXT NOT NULL,
        source_url      TEXT NOT NULL,
        where_clause    TEXT NOT NULL,
        expected_count  INTEGER NOT NULL,
        received_count  INTEGER NOT NULL,
        unique_ids      INTEGER NOT NULL,
        features        INTEGER NOT NULL,
        content_hash    TEXT NOT NULL,
        loaded_at       TEXT NOT NULL,
        is_active       INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
        CHECK (is_active = 0 OR (expected_count = received_count AND received_count = unique_ids))
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_constraint_loads_active ON constraint_loads(layer_key) WHERE is_active = 1",
    """
    CREATE TABLE IF NOT EXISTS constraint_features (
        load_id      INTEGER NOT NULL REFERENCES constraint_loads(load_id),
        feature_id   INTEGER NOT NULL,
        geom_geojson TEXT NOT NULL,
        minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL, maxy REAL NOT NULL,
        attrs_json   TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (load_id, feature_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS parcel_constraints (
        parcel_load_id     INTEGER NOT NULL REFERENCES parcel_loads(load_id),
        constraint_load_id INTEGER NOT NULL REFERENCES constraint_loads(load_id),
        gpin               TEXT NOT NULL,
        layer_key          TEXT NOT NULL,
        overlap_sqft       REAL,
        share              REAL,
        line_length_ft     REAL,
        distance_ft        REAL,
        detail_json        TEXT NOT NULL DEFAULT '{}',
        computed_at        TEXT NOT NULL,
        PRIMARY KEY (parcel_load_id, constraint_load_id, gpin)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_parcel_constraints_gpin ON parcel_constraints(gpin)",
    """
    CREATE VIEW IF NOT EXISTS parcel_constraints_effective AS
    SELECT pc.*, cl.loaded_at AS layer_loaded_at, cl.source_url
    FROM parcel_constraints pc
    JOIN constraint_loads cl ON cl.load_id = pc.constraint_load_id AND cl.is_active = 1
    JOIN parcel_loads pl ON pl.load_id = pc.parcel_load_id AND pl.is_active = 1
    """,
    """
    CREATE TABLE IF NOT EXISTS board_action_loads (
        load_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        layer_key       TEXT NOT NULL,
        source_url      TEXT NOT NULL,
        where_clause    TEXT NOT NULL,
        expected_count  INTEGER NOT NULL,
        received_count  INTEGER NOT NULL,
        unique_ids      INTEGER NOT NULL,
        no_date         INTEGER NOT NULL,
        actions         INTEGER NOT NULL,
        content_hash    TEXT NOT NULL,
        max_date_sub    TEXT,
        parcel_load_id  INTEGER,
        loaded_at       TEXT NOT NULL,
        is_active       INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
        CHECK (is_active = 0 OR (expected_count = received_count AND received_count = unique_ids))
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_board_action_loads_active ON board_action_loads(layer_key) WHERE is_active = 1",
    """
    CREATE TABLE IF NOT EXISTS board_actions (
        load_id        INTEGER NOT NULL REFERENCES board_action_loads(load_id),
        layer_key      TEXT NOT NULL,
        pln_id         TEXT NOT NULL,
        objectids_json TEXT NOT NULL,
        date_sub       TEXT NOT NULL,
        pln_type       TEXT,
        pln_file_no    TEXT,
        proj_name      TEXT,
        appl_name      TEXT,
        deeplink       TEXT,
        geom_geojson   TEXT NOT NULL,
        minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL, maxy REAL NOT NULL,
        lat REAL NOT NULL, lng REAL NOT NULL,
        gpins_json     TEXT NOT NULL,
        PRIMARY KEY (load_id, pln_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS board_action_parcels (
        load_id INTEGER NOT NULL REFERENCES board_action_loads(load_id),
        pln_id  TEXT NOT NULL,
        gpin    TEXT NOT NULL,
        PRIMARY KEY (load_id, pln_id, gpin)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_board_action_parcels_gpin ON board_action_parcels(gpin)",
    """
    CREATE VIEW IF NOT EXISTS board_actions_effective AS
    SELECT a.*, l.loaded_at AS layer_loaded_at FROM board_actions a
    JOIN board_action_loads l ON l.load_id = a.load_id AND l.is_active = 1
    """,
]


# Schema v13 (a second city). Parcel loads carry their locality, and "one active
# parcel load" becomes one active load per locality. The views that joined
# "the" active parcel load (or Virginia Beach's flood load) now join the load of
# the row's own locality -- without this, a second city's active load would
# duplicate every permit / decision row. Virginia Beach results are unchanged.
LOCALITY_DEFAULT = "virginia_beach"
_VIEW_LOCALITY_PATCHES = {
    "permit_parcel_effective": [
        ("(p.locality = 'virginia_beach') AS in_scope",
         "(p.locality = 'virginia_beach' OR p.locality IN (SELECT DISTINCT locality FROM parcel_loads)) AS in_scope"),
        ("LEFT JOIN parcel_loads l ON l.is_active = 1",
         "LEFT JOIN parcel_loads l ON l.is_active = 1 AND l.locality = p.locality"),
    ],
    "decision_parcel_effective": [
        ("LEFT JOIN parcel_loads l ON l.is_active = 1",
         "LEFT JOIN parcel_loads l ON l.is_active = 1 AND l.locality = 'virginia_beach'"),
    ],
    "parcel_flood_effective": [
        ("JOIN flood_locality_dfirm m ON m.locality = 'virginia_beach'",
         "JOIN flood_locality_dfirm m ON m.locality = pl.locality"),
    ],
}


def _localize_views(conn) -> None:
    """Recreate the views above with locality-aware joins. Each pattern must be
    found exactly once in the current definition, or the migration stops."""
    for view, patches in _VIEW_LOCALITY_PATCHES.items():
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = ?", (view,)).fetchone()[0]
        for old, new in patches:
            if new in sql:
                continue
            if sql.count(old) != 1:
                raise RuntimeError(f"v13 migration: {view} definition changed; expected one {old!r}")
            sql = sql.replace(old, new)
        conn.execute(f"DROP VIEW {view}")
        conn.execute(sql)


def _schema_v13(conn) -> None:
    if "locality" not in {r[1] for r in conn.execute("PRAGMA table_info(parcel_loads)")}:
        conn.execute(f"ALTER TABLE parcel_loads ADD COLUMN locality TEXT NOT NULL DEFAULT '{LOCALITY_DEFAULT}'")
    conn.execute("DROP INDEX IF EXISTS idx_parcel_loads_active")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_parcel_loads_active_locality ON parcel_loads(locality) "
                 "WHERE is_active = 1")
    _localize_views(conn)


# Schema v14 (Chesapeake as a "pipeline city"). Chesapeake publishes no building
# permits; it publishes its land-use cases (Development Tracking) with Planning
# Commission / City Council actions coded by the city's own domain, development
# plans and right-of-way permits. Cases and records are loaded complete-only,
# one active load per (locality, source); records are kept out of `permits`.
CASE_STAGES = ("approved", "denied", "withdrawn", "pending", "incomplete", "other")
_SCHEMA_V14_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS city_record_loads (
        load_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        locality        TEXT NOT NULL,
        source          TEXT NOT NULL,
        source_url      TEXT NOT NULL,
        expected_count  INTEGER NOT NULL,
        received_count  INTEGER NOT NULL,
        unique_ids      INTEGER NOT NULL,
        records         INTEGER NOT NULL,
        content_hash    TEXT NOT NULL,
        domain_json     TEXT NOT NULL DEFAULT '{}',
        parcel_load_id  INTEGER,
        loaded_at       TEXT NOT NULL,
        is_active       INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
        CHECK (is_active = 0 OR (expected_count = received_count AND received_count = unique_ids))
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_city_record_loads_active ON city_record_loads(locality, source) "
    "WHERE is_active = 1",
    f"""
    CREATE TABLE IF NOT EXISTS land_use_cases (
        load_id          INTEGER NOT NULL REFERENCES city_record_loads(load_id),
        locality         TEXT NOT NULL,
        case_id          TEXT NOT NULL,
        object_ids_json  TEXT NOT NULL,
        case_type        TEXT,
        project          TEXT,
        address          TEXT,
        parcel_ids_json  TEXT NOT NULL DEFAULT '[]',
        housing_units    INTEGER,
        residential      TEXT,
        zoning_from      TEXT,
        zoning_to        TEXT,
        applied_date     TEXT,
        year_applied     INTEGER,
        staff_rec        TEXT,
        pc_action        TEXT,
        pc_date          TEXT,
        cc_action        TEXT,
        cc_date          TEXT,
        arc_action       TEXT,
        arc_date         TEXT,
        final_raw        TEXT,
        stage            TEXT NOT NULL CHECK (stage IN ({",".join(repr(x) for x in CASE_STAGES)})),
        final_date       TEXT,
        planner          TEXT,
        notes            TEXT,
        legacy           INTEGER NOT NULL DEFAULT 0,
        source_link      TEXT,
        geom_geojson     TEXT,
        minx REAL, miny REAL, maxx REAL, maxy REAL, lat REAL, lng REAL,
        gpins_json       TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (load_id, case_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_land_use_cases_stage ON land_use_cases(locality, stage)",
    """
    CREATE TABLE IF NOT EXISTS development_records (
        load_id        INTEGER NOT NULL REFERENCES city_record_loads(load_id),
        locality       TEXT NOT NULL,
        source         TEXT NOT NULL,
        record_id      TEXT NOT NULL,
        object_ids_json TEXT NOT NULL,
        record_type    TEXT,
        project_type   TEXT,
        status         TEXT,
        details        TEXT,
        organization   TEXT,
        address        TEXT,
        submitted_date TEXT,
        approved_date  TEXT,
        expires_date   TEXT,
        parcel_ids_json TEXT NOT NULL DEFAULT '[]',
        lat REAL, lng REAL,
        gpins_json     TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (load_id, record_id)
    )
    """,
    """
    CREATE VIEW IF NOT EXISTS land_use_cases_effective AS
    SELECT c.*, l.loaded_at, l.source_url FROM land_use_cases c
    JOIN city_record_loads l ON l.load_id = c.load_id AND l.is_active = 1
    """,
    """
    CREATE VIEW IF NOT EXISTS development_records_effective AS
    SELECT r.*, l.loaded_at, l.source_url FROM development_records r
    JOIN city_record_loads l ON l.load_id = r.load_id AND l.is_active = 1
    """,
    "INSERT OR IGNORE INTO flood_locality_dfirm (locality, dfirm_id) VALUES ('chesapeake', '510034')",
]


# Schema v15: Chesapeake permits carry no parcel number, so a permit may match its
# parcel by exact (house, street) -- method 'address_exact', as decisions already
# do. permit_parcel is copied into a table whose CHECK allows it; every row is
# kept as is, and permit_parcel_effective is recreated from its own current SQL.
_PERMIT_PARCEL_COLUMNS = ("permit_id, load_id, method, gpin, alt_gpin, disputed, gpin_point_agree, tagged_lat, "
                          "tagged_lng, tagged_gpin_raw, match_fp, matched_at")


def _schema_v15(conn) -> None:
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'permit_parcel'").fetchone()[0]
    if "address_exact" in sql:
        return
    view = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'permit_parcel_effective'"
                        ).fetchone()
    before = conn.execute("SELECT COUNT(*) FROM permit_parcel").fetchone()[0]
    conn.execute("DROP VIEW IF EXISTS permit_parcel_effective")
    old_check = "'not_geocoded_no_gpin'))"
    if old_check not in sql:
        raise RuntimeError("permit_parcel's method CHECK is not the expected v3 form; aborting migration")
    conn.execute(sql.replace("CREATE TABLE permit_parcel", "CREATE TABLE permit_parcel_v15", 1)
                 .replace(old_check, "'not_geocoded_no_gpin','address_exact'))", 1))
    conn.execute(f"INSERT INTO permit_parcel_v15 ({_PERMIT_PARCEL_COLUMNS}) "
                 f"SELECT {_PERMIT_PARCEL_COLUMNS} FROM permit_parcel")
    after = conn.execute("SELECT COUNT(*) FROM permit_parcel_v15").fetchone()[0]
    if after != before:
        raise RuntimeError(f"permit_parcel copy lost rows ({before} -> {after}); aborting migration")
    conn.execute("DROP TABLE permit_parcel")
    # other views (filing_status ...) read permit_parcel_effective, which is recreated just below; the
    # legacy rename does not re-check every view meanwhile
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("ALTER TABLE permit_parcel_v15 RENAME TO permit_parcel")
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
    if view is not None:
        conn.execute(view[0])


# Schema v16: a city's Council votes from a VoteIQ export (Chesapeake). The city's own
# case record stays authoritative: votes are shown and cross-checked, never used to set
# a case stage. Council members are public officials; no email is stored.
COUNCIL_VOTE_RESULTS = ("passed", "failed", "tabled", "withdrawn")
COUNCIL_MEMBER_VOTES = ("yes", "no", "abstain", "absent")
MOTION_KINDS = ("approve", "deny", "continue", "other")
_SCHEMA_V16_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS council_vote_loads (
        load_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        locality       TEXT NOT NULL,
        source         TEXT NOT NULL,
        files_json     TEXT NOT NULL,
        votes          INTEGER NOT NULL,
        member_votes   INTEGER NOT NULL,
        members        INTEGER NOT NULL,
        agenda_items   INTEGER NOT NULL,
        undated_votes  INTEGER NOT NULL,
        date_min       TEXT,
        date_max       TEXT,
        content_hash   TEXT NOT NULL,
        loaded_at      TEXT NOT NULL,
        is_active      INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_council_vote_loads_active ON council_vote_loads(locality) WHERE is_active = 1",
    f"""
    CREATE TABLE IF NOT EXISTS council_votes (
        load_id        INTEGER NOT NULL REFERENCES council_vote_loads(load_id) ON DELETE CASCADE,
        vote_id        TEXT NOT NULL,
        clip_id        TEXT,
        meeting_date   TEXT,
        agenda_item    TEXT,
        title          TEXT,
        motion         TEXT,
        motion_kind    TEXT NOT NULL CHECK (motion_kind IN ({",".join(repr(x) for x in MOTION_KINDS)})),
        moved_by       TEXT,
        seconded_by    TEXT,
        result         TEXT NOT NULL CHECK (result IN ({",".join(repr(x) for x in COUNCIL_VOTE_RESULTS)})),
        yes            INTEGER NOT NULL,
        no             INTEGER NOT NULL,
        abstain        INTEGER NOT NULL,
        absent         INTEGER NOT NULL,
        printed_tally  TEXT,
        tally_mismatch INTEGER NOT NULL CHECK (tally_mismatch IN (0, 1)),
        category       TEXT,
        case_ids_json  TEXT NOT NULL,
        PRIMARY KEY (load_id, vote_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS council_member_votes (
        load_id     INTEGER NOT NULL REFERENCES council_vote_loads(load_id) ON DELETE CASCADE,
        vote_id     TEXT NOT NULL,
        member      TEXT NOT NULL,
        member_raw  TEXT NOT NULL,
        vote        TEXT NOT NULL CHECK (vote IN ({",".join(repr(x) for x in COUNCIL_MEMBER_VOTES)})),
        PRIMARY KEY (load_id, vote_id, member)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS council_members (
        load_id   INTEGER NOT NULL REFERENCES council_vote_loads(load_id) ON DELETE CASCADE,
        name      TEXT NOT NULL,
        seat      TEXT,
        PRIMARY KEY (load_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS council_agenda_items (
        load_id        INTEGER NOT NULL REFERENCES council_vote_loads(load_id) ON DELETE CASCADE,
        item_id        TEXT NOT NULL,
        event_id       TEXT,
        meeting_date   TEXT,
        item_ref       TEXT,
        section        TEXT,
        title          TEXT,
        category       TEXT,
        agenda_url     TEXT,
        case_ids_json  TEXT NOT NULL,
        lat            REAL,
        lng            REAL,
        scraped_at     TEXT,
        PRIMARY KEY (load_id, item_id)
    )
    """,
    """
    CREATE VIEW IF NOT EXISTS council_votes_effective AS
    SELECT l.locality, v.* FROM council_votes v
    JOIN council_vote_loads l ON l.load_id = v.load_id AND l.is_active = 1
    """,
    """
    CREATE VIEW IF NOT EXISTS council_agenda_effective AS
    SELECT l.locality, a.* FROM council_agenda_items a
    JOIN council_vote_loads l ON l.load_id = a.load_id AND l.is_active = 1
    """,
]


def init_db(db_path: Path | str | None = None) -> None:
    """Create schema if absent and stamp the schema version.

    Uses sqlite's PRAGMA user_version as the version marker (no extra table
    needed) — a future migration step reads this before applying changes.
    """
    with db(db_path) as conn:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)

        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"permits.db schema version {current_version} is newer than this "
                f"code supports ({SCHEMA_VERSION}) — refusing to run against it."
            )
        # 0 = brand-new file (v1 tables were just created above); 1 = existing
        # v1 database. Both get the additive v2 statements.
        if current_version < 2:
            for statement in _SCHEMA_V2_STATEMENTS:
                conn.execute(statement)
        if current_version < 3:
            for statement in _SCHEMA_V3_STATEMENTS:
                conn.execute(statement)
        if current_version < 4:
            for table, column, decl in _SCHEMA_V4_COLUMNS:  # idempotent ADD COLUMN
                if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            for statement in _SCHEMA_V4_STATEMENTS:
                conn.execute(statement)
            _backfill_fingerprints(conn)  # old reviews keep NULL -> stale until re-reviewed
        if current_version < 5:
            for statement in _SCHEMA_V5_STATEMENTS:
                conn.execute(statement)
            _widen_link_tiers(conn)
        if current_version < 6:
            for table, column, decl in _SCHEMA_V6_COLUMNS:  # idempotent ADD COLUMN
                if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            for statement in _SCHEMA_V6_STATEMENTS:
                conn.execute(statement)
        if current_version < 7:
            for statement in _SCHEMA_V7_STATEMENTS:
                conn.execute(statement)
        if current_version < 8:
            for statement in _SCHEMA_V8_STATEMENTS:
                conn.execute(statement)
            _baseline_review_history(conn)
        if current_version < 9:
            for statement in _SCHEMA_V9_STATEMENTS:
                conn.execute(statement)
        if current_version < 10:
            for statement in _SCHEMA_V10_STATEMENTS:
                conn.execute(statement)
        if current_version < 11:
            for statement in _SCHEMA_V11_STATEMENTS:
                conn.execute(statement)
        if current_version < 12:
            for statement in _SCHEMA_V12_STATEMENTS:
                conn.execute(statement)
        if current_version < 13:
            _schema_v13(conn)
        if current_version < 14:
            for statement in _SCHEMA_V14_STATEMENTS:
                conn.execute(statement)
        if current_version < 15:
            _schema_v15(conn)
        if current_version < 16:
            for statement in _SCHEMA_V16_STATEMENTS:
                conn.execute(statement)
        if current_version < 17:
            # Norfolk's NFHL id, confirmed with a point query on the FEMA service (2026-10-29)
            conn.execute("INSERT OR IGNORE INTO flood_locality_dfirm (locality, dfirm_id) VALUES ('norfolk', '510104')")
        if current_version < 18:
            # Portsmouth's NFHL id, confirmed with two point queries on the FEMA service (2026-10-30)
            conn.execute("INSERT OR IGNORE INTO flood_locality_dfirm (locality, dfirm_id) VALUES ('portsmouth', '515529')")
        if current_version < 19:
            # Suffolk's NFHL id, confirmed with two point queries on the FEMA service (2026-10-30)
            conn.execute("INSERT OR IGNORE INTO flood_locality_dfirm (locality, dfirm_id) VALUES ('suffolk', '510156')")
        if current_version < 20:
            # public-hearing testimony as counts only (speakers' names / addresses are never stored)
            conn.execute("""CREATE TABLE IF NOT EXISTS council_testimony_counts (
                load_id INTEGER NOT NULL REFERENCES council_vote_loads(load_id) ON DELETE CASCADE,
                meeting_date TEXT, agenda_item TEXT NOT NULL, support INTEGER NOT NULL, oppose INTEGER NOT NULL,
                comment INTEGER NOT NULL, present INTEGER NOT NULL, PRIMARY KEY (load_id, meeting_date, agenda_item))""")
        # data correction, idempotent: Virginia Beach's NFHL id (see flood_locality_dfirm above)
        conn.execute("UPDATE flood_locality_dfirm SET dfirm_id = '515531' "
                     "WHERE locality = 'virginia_beach' AND dfirm_id = '51810C'")
        if current_version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def schema_version(db_path: Path | str | None = None) -> int:
    with db(db_path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]
