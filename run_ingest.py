"""Manual/scheduled data refresh for PermitIQ.

Never imported by app.py and never triggered by an HTTP route -- this is the
ONLY process that writes permit data, ever. (Lesson carried over from the
sibling VoteIQ project: spawning a data-loading subprocess from a running
web server's request path or startup event caused an OOM on its Render
deploy. PermitIQ isn't deployed yet, but the separation is built in from
day one so it's already safe when it is.)

Usage:
  python run_ingest.py --locality all --since 2024-01-01
  python run_ingest.py --locality norfolk --limit 20 --dry-run
  python run_ingest.py --geocode-only
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import db as db_module
from ingest.common import (
    SourceUnavailableError,
    finish_ingestion_run,
    start_ingestion_run,
    upsert_permits,
)
from ingest.geocode import geocode_missing

ALL_LOCALITIES = ("norfolk", "virginia_beach", "chesapeake", "portsmouth")

# Populated as each adapter is built (Phases 3-6). A locality with no adapter
# registered yet is skipped with a warning by run_locality(), not a crash.
ADAPTERS: dict = {}

try:
    from ingest import norfolk

    ADAPTERS["norfolk"] = norfolk
except ImportError:
    pass

try:
    from ingest import virginia_beach

    ADAPTERS["virginia_beach"] = virginia_beach
except ImportError:
    pass

try:
    from ingest import chesapeake

    ADAPTERS["chesapeake"] = chesapeake
except ImportError:
    pass

try:
    from ingest import portsmouth

    ADAPTERS["portsmouth"] = portsmouth
except ImportError:
    pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="PermitIQ data ingestion")
    parser.add_argument("--locality", choices=list(ALL_LOCALITIES) + ["all"], default="all")
    parser.add_argument(
        "--since", type=str, default=None, help="YYYY-MM-DD, default: 2 years back"
    )
    parser.add_argument("--limit", type=int, default=None, help="cap on records fetched from the source")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-geocode", action="store_true")
    parser.add_argument(
        "--geocode-limit", type=int, default=None,
        help="cap on addresses geocoded this run (independent of --limit -- geocoding "
             "is paced ~1/sec, so a large backlog is deliberately spread across runs; "
             "WHERE lat IS NULL makes this always resumable)",
    )
    return parser.parse_args(argv)


def run_locality(
    locality: str, since: date | None, limit: int | None, dry_run: bool,
    no_geocode: bool, geocode_limit: int | None = None,
) -> None:
    print(f"\n=== {locality} ===")
    adapter = ADAPTERS.get(locality)
    if adapter is None:
        print(f"  SKIPPED: no adapter registered yet for '{locality}' (source under review)")
        return

    since_str = since.isoformat() if since else None
    run_id = None
    if not dry_run:
        with db_module.db() as conn:
            run_id = start_ingestion_run(conn, locality, since_str)

    try:
        records = list(adapter.fetch(since=since, limit=limit))
    except SourceUnavailableError as exc:
        print(f"  SKIPPED (source unavailable): {exc}")
        if run_id is not None:
            with db_module.db() as conn:
                finish_ingestion_run(conn, run_id, "failed", error_message=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - record and re-raise so the CLI exits non-zero
        print(f"  FAILED: {exc}")
        if run_id is not None:
            with db_module.db() as conn:
                finish_ingestion_run(conn, run_id, "failed", error_message=str(exc))
        raise

    print(f"  fetched {len(records)} records")

    if dry_run:
        for r in records[:5]:
            print(
                f"    sample: id={r.source_permit_id!r} address={r.address!r} "
                f"type={r.permit_type!r} status={r.status!r} "
                f"applied={r.applied_date} bucket={r.status_bucket}"
            )
        missing_id = sum(1 for r in records if not r.source_permit_id)
        missing_address = sum(1 for r in records if not r.address)
        unparsed_applied = sum(1 for r in records if r.applied_date is None)
        invalid = sum(1 for r in records if r.validation_errors())
        print(
            f"  dry-run summary: missing_address={missing_address} "
            f"unparsed_applied_date={unparsed_applied} invalid={invalid} "
            f"(missing_id already filtered by the adapter: {missing_id})"
        )
        return

    with db_module.db() as conn:
        counts = upsert_permits(conn, records)

    print(
        f"  inserted={counts['inserted']} updated={counts['updated']} "
        f"unchanged={counts['unchanged']} skipped={counts['skipped']}"
    )

    geocoded_count = 0
    if not no_geocode:
        with db_module.db() as conn:
            geo_counts = geocode_missing(conn, locality=locality, limit=geocode_limit)
        geocoded_count = geo_counts["matched"]
        print(
            f"  geocode: attempted={geo_counts['attempted']} matched={geo_counts['matched']} "
            f"cache_hit={geo_counts['cache_hit']} failed={geo_counts['failed']} "
            f"no_address={geo_counts['no_address']}"
        )

    with db_module.db() as conn:
        finish_ingestion_run(
            conn, run_id, "success", fetched_count=len(records),
            counts=counts, geocoded_count=geocoded_count,
        )


def main(argv=None) -> None:
    args = parse_args(argv)
    db_module.init_db()

    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()
    else:
        since = date.today() - timedelta(days=730)

    localities = ALL_LOCALITIES if args.locality == "all" else (args.locality,)
    for locality in localities:
        run_locality(locality, since, args.limit, args.dry_run, args.no_geocode, args.geocode_limit)


if __name__ == "__main__":
    main()
