"""Batch-first backlog geocoding runner.

Dedupes pending permit addresses and resolves them via Census's batch
endpoint (thousands of addresses per HTTP call) before falling through to a
sequential, rate-limited Nominatim pass only for the addresses Census
couldn't resolve. See ingest/geocode.py's geocode_backlog_batch() docstring
for the full flow and resumability guarantees.

Usage:
  python scripts/geocode_backlog.py --locality norfolk --limit 3000
  python scripts/geocode_backlog.py --locality all
  python scripts/geocode_backlog.py --locality all --analyze
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest.geocode import analyze_unmapped, geocode_backlog_batch  # noqa: E402


def _print_counts(label: str, counts: dict) -> None:
    print(f"\n=== {label} ===")
    print(f"  total_pending_considered = {counts['total_pending_considered']}")
    print(f"  no_address               = {counts['no_address']}")
    print(f"  cache_hits               = {counts['cache_hits']}")
    print(f"  unique_addresses_to_census = {counts['unique_addresses_sent_to_census']}")
    print(f"  census_matched           = {counts['census_matched']}")
    print(f"  census_rejected          = {counts['census_rejected']}  (bbox/locality mismatch)")
    print(f"  census_not_found         = {counts['census_not_found']}")
    print(f"  nominatim_attempted      = {counts['nominatim_attempted']}")
    print(f"  nominatim_matched        = {counts['nominatim_matched']}")
    print(f"  nominatim_rejected       = {counts['nominatim_rejected']}")
    print(f"  nominatim_not_found      = {counts['nominatim_not_found']}")
    print(f"  errors                   = {counts['errors']}")
    print(f"  census_failed (pending)  = {counts.get('census_failed', 0)}  (request failed; never recorded as a miss)")
    print(f"  newly_mapped             = {counts['newly_mapped']}")
    print(f"  mapped_before -> after   = {counts['mapped_before']} -> {counts['mapped_after']}")
    print(f"  remaining_pending        = {counts['remaining_pending']}")
    print(f"  elapsed_seconds          = {counts['elapsed_seconds']}")


def _print_analysis(label: str, analysis: dict) -> None:
    print(f"\n--- unmapped-reason breakdown: {label} ---")
    for status, n in sorted(analysis["by_geocode_status"].items(), key=lambda kv: -kv[1]):
        print(f"  {status:<20} {n}")
    print(f"  never_attempted_yet: {analysis['never_attempted']}")
    print(f"  empty_address_field: {analysis['empty_address_field']}")
    if analysis["sample_not_found_addresses"]:
        print("  sample not_found addresses:")
        for row in analysis["sample_not_found_addresses"]:
            print(f"    {row['source_permit_id']}: {row['address']!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-first PermitIQ geocoding backlog runner")
    parser.add_argument("--locality", choices=["norfolk", "virginia_beach", "chesapeake", "all"], default="all")
    parser.add_argument("--limit", type=int, default=None, help="max pending permits to consider per locality")
    parser.add_argument("--force", action="store_true", help="also retry recent not_found/rejected rows")
    parser.add_argument("--analyze", action="store_true", help="print unmapped-reason breakdown after the run")
    parser.add_argument("--census-only", action="store_true",
                        help="no Nominatim fallback (large catch-ups): Census misses stay not_found, failed requests pending")
    args = parser.parse_args()

    localities = ["norfolk", "virginia_beach", "chesapeake"] if args.locality == "all" else [args.locality]

    for loc in localities:
        with db_module.db() as conn:
            counts = geocode_backlog_batch(conn, locality=loc, limit=args.limit, force=args.force,
                                           census_only=args.census_only)
        _print_counts(loc, counts)

    if args.analyze:
        for loc in localities:
            with db_module.db() as conn:
                analysis = analyze_unmapped(conn, locality=loc)
            _print_analysis(loc, analysis)


if __name__ == "__main__":
    main()
