"""Create or migrate the PermitIQ database schema -- the explicit CLI step the
read-only API depends on (app.py never creates or migrates schema).

Additive and idempotent: safe to re-run. run_ingest.py also calls init_db(),
since it is a writer too.

Usage:
  python scripts/migrate_db.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402


def main() -> None:
    path = Path(db_module.DB_PATH)
    before = 0
    if path.is_file():
        with sqlite3.connect(path) as conn:
            before = conn.execute("PRAGMA user_version").fetchone()[0]
    db_module.init_db(path)
    after = db_module.schema_version(path)
    print(f"{path}: schema version {before} -> {after}"
          + ("" if before != after else " (already current)"))


if __name__ == "__main__":
    main()
