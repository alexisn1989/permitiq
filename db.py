"""PermitIQ database layer: schema, connection helper, schema versioning.

Two kinds of process share this file. CLI writers (run_ingest.py and
scripts/*) use db(), which enables WAL + a busy timeout and may apply schema
migrations via init_db(). The API (app.py) uses db_readonly() only: a
SQLite mode=ro connection that never sets a pragma that writes and never
migrates -- if the schema isn't ready it raises DatabaseNotReady instead.

Permit data is never fabricated: init_db() only creates schema. The permits
table starts empty until run_ingest.py populates it from a verified source.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATA_DIR", str(BASE_DIR))) / "permits.db"

SCHEMA_VERSION = 2

# Flood classes stored in permit_flood.flood_class, and the two derived only
# at read time by the permit_flood_effective view (see ingest/flood.py).
FLOOD_CLASSES_STORED = ("sfha_v", "sfha_a", "shaded_x", "matched_non_sfha", "no_polygon", "not_geocoded")
FLOOD_CLASSES = FLOOD_CLASSES_STORED + ("untagged", "out_of_scope")


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
        if current_version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def schema_version(db_path: Path | str | None = None) -> int:
    with db(db_path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]
