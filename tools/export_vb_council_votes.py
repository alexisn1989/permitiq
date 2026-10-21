"""Read-only export of VoteIQ's Virginia Beach Council member votes, for
PermitIQ Stage 2. Standalone (stdlib only): copy this one file to the VoteIQ
host (e.g. the Render shell) and run it there. It changes nothing in VoteIQ.

  python export_vb_council_votes.py /var/data/polls.db > vb_council_votes.jsonl 2> vb_schema.txt

* Opens the database with SQLite's mode=ro URI: no statement can write.
* Checks vb_council_member_votes has the expected columns and exits (code 2)
  listing the real ones if not -- no column is guessed.
* stdout: a {"_meta": ...} line (with the row count, read in the same
  snapshot as the rows), then one JSON object per member vote row
  with only meeting_id, resolution_id, meeting_date, title, result,
  member_name, district, vote (no user ids).
* stderr: the column NAMES of every vb_% table (no data), so we can see
  whether VoteIQ stores motion text or an official outcome anywhere.
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

TABLE = "vb_council_member_votes"
COLUMNS = ("meeting_id", "resolution_id", "meeting_date", "title", "result", "member_name", "district", "vote")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[0])
    if not path.is_file():
        print(f"[FATAL] {path} not found", file=sys.stderr)
        return 2
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'vb\\_%' ESCAPE '\\' ORDER BY name")]
        print("# vb_% tables and their columns (names only)", file=sys.stderr)
        for t in tables:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
            print(f"{t}: {', '.join(cols)}", file=sys.stderr)
        if TABLE not in tables:
            print(f"[FATAL] table {TABLE} not found", file=sys.stderr)
            return 2
        actual = [r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")]
        missing = [c for c in COLUMNS if c not in actual]
        if missing:
            print(f"[FATAL] {TABLE} lacks {missing}; actual columns: {actual}", file=sys.stderr)
            return 2
        # One read transaction: the header count describes exactly the snapshot
        # written below, even if VoteIQ writes meanwhile. PermitIQ refuses a
        # file whose row count differs from this header (truncation check).
        conn.execute("BEGIN")
        n = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        out = sys.stdout
        out.write(json.dumps({"_meta": {"table": TABLE, "rows": n, "columns": list(COLUMNS),
                                        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}}) + "\n")
        cur = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} ORDER BY meeting_date, meeting_id, resolution_id, member_name")
        written = 0
        for row in cur:
            out.write(json.dumps(dict(zip(COLUMNS, row)), ensure_ascii=False) + "\n")
            written += 1
        conn.execute("COMMIT")
        if written != n:  # cannot happen inside one snapshot; fail loudly if it does
            print(f"[FATAL] wrote {written} rows but counted {n}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
