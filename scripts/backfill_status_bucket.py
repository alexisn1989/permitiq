"""One-off backfill (Phase 9 demo-readiness hardening): recompute
status_bucket for already-ingested rows whose adapter's STATUS_BUCKET_MAP
was expanded after they were first ingested.

status_bucket is computed once at ingest time and stored, not derived at
read time, so expanding ingest/norfolk.py's STATUS_BUCKET_MAP only affects
rows ingested (or re-ingested) after the change -- this script catches up
the rows already sitting in the real database. Values-only UPDATE, no
schema change, no re-fetch from source, dry-run by default.

Usage:
  python scripts/backfill_status_bucket.py --locality norfolk --dry-run
  python scripts/backfill_status_bucket.py --locality norfolk
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import norfolk  # noqa: E402

# locality -> (module, function computing the correct bucket for a raw status)
ADAPTERS = {
    "norfolk": norfolk,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute stale status_bucket values")
    parser.add_argument("--locality", choices=list(ADAPTERS), default="norfolk")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    adapter = ADAPTERS[args.locality]

    with db_module.db() as conn:
        rows = conn.execute(
            "SELECT id, status, status_bucket FROM permits WHERE locality = ?", (args.locality,)
        ).fetchall()

        changes: dict[str, int] = {}
        updates: list[tuple[str, int]] = []
        for row in rows:
            correct = adapter._status_bucket(row["status"])
            if correct != row["status_bucket"]:
                key = f"{row['status_bucket']} -> {correct} (status={row['status']!r})"
                changes[key] = changes.get(key, 0) + 1
                updates.append((correct, row["id"]))

        print(f"{args.locality}: {len(rows)} rows checked, {len(updates)} need correction")
        for key, count in sorted(changes.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>5}  {key}")

        if args.dry_run:
            print("\n--dry-run: no changes written")
            return

        for correct_bucket, permit_id in updates:
            conn.execute("UPDATE permits SET status_bucket = ? WHERE id = ?", (correct_bucket, permit_id))
        conn.commit()
        print(f"\nUpdated {len(updates)} rows.")


if __name__ == "__main__":
    main()
