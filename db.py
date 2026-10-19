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

SCHEMA_VERSION = 5

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
                         "not_geocoded_no_gpin")
DECISION_PARCEL_METHODS = ("gpin10_unique", "address_exact", "point_in_parcel", "ambiguous", "no_parcel")
PARCEL_REVIEW_VERDICTS = ("gpin_correct", "point_correct", "unresolvable")


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
    "INSERT OR IGNORE INTO flood_locality_dfirm (locality, dfirm_id) VALUES ('virginia_beach', '51810C')",
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
            ('gpin14','gpin10_unique','point_in_parcel','ambiguous','no_parcel','not_geocoded_no_gpin')),
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
        if current_version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def schema_version(db_path: Path | str | None = None) -> int:
    with db(db_path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]
