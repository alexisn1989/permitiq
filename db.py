"""PermitIQ database layer: schema, connection helper, schema versioning.

Two processes share this file — uvicorn (app.py, read-only) and run_ingest.py
(the only writer) — so every connection enables WAL + a busy timeout rather
than relying on a single-writer assumption like AssetIQ's db().

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

SCHEMA_VERSION = 1

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


def init_db(db_path: Path | str | None = None) -> None:
    """Create schema if absent and stamp the schema version.

    Uses sqlite's PRAGMA user_version as the version marker (no extra table
    needed) — a future migration step reads this before applying changes.
    """
    with db(db_path) as conn:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)

        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version == 0:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"permits.db schema version {current_version} is newer than this "
                f"code supports ({SCHEMA_VERSION}) — refusing to run against it."
            )
        # current_version in (1..SCHEMA_VERSION-1) would run migrations here;
        # none exist yet since this is the first schema version.


def schema_version(db_path: Path | str | None = None) -> int:
    with db(db_path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]
