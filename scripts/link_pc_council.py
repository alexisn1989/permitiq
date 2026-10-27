"""Link Planning Commission items to later Council land-use decisions on the
same parcel (GPIN10) or street address, within 12 months. Candidates only:
they are shown as "possible Council decision" and never change any status.

Usage:
  python scripts/link_pc_council.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import pc_hearings  # noqa: E402


def main(argv=None) -> int:
    try:
        with db_module.db() as conn:
            r = pc_hearings.link_council(conn)
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    print(f"pc items: {r['items']}; Council decision candidates: {r['candidates']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
