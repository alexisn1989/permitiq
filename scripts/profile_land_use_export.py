"""Read-only profile of a VoteIQ Council-vote export, run BEFORE filling
config/land_use.toml. Writes nothing. Every config value must come from what
this prints, never from a guess.

Prints: distinct vote and result strings with counts, meeting_date formats
and range, items per year, keyword hits in titles, likely-truncated titles,
items whose member rows disagree, and address/GPIN extraction rates with
sample failures (on titles matching the configured land-use patterns, or on
keyword hits while the config is still empty).

Usage:
  python scripts/profile_land_use_export.py --file vb_council_votes.jsonl
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest import land_use  # noqa: E402
from ingest.address_norm import parse_title_addresses  # noqa: E402
from ingest.flood_stats import ConfigError  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
KEYWORDS = ("rezon", "change of zoning", "conditional use", "conditional rezoning", "modification of condition",
            "variance", "street closure", "closure of", "subdivision", "comprehensive plan", "application of",
            "motion", "approve", "deny", "defer", "withdraw")


def _shape(value) -> str:
    return re.sub(r"\d", "9", str(value)) if value is not None else "<null>"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Profile a VoteIQ Council-vote export (read-only)")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--config", type=Path, default=BASE / "config" / "land_use.toml")
    p.add_argument("--samples", type=int, default=20)
    args = p.parse_args(argv)
    try:
        meta, rows = land_use.read_export(args.file)
    except (OSError, ValueError) as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1
    try:
        cfg = land_use.load_config(args.config)
    except ConfigError:
        cfg = None

    print(f"{args.file}: {len(rows)} member rows; meta={meta}")
    for col in ("vote", "result"):
        print(f"\n{col} values:")
        for v, n in Counter(r[col] for r in rows).most_common():
            print(f"  {n:>7}  {v!r}")
    print("\nmeeting_date shapes:")
    for v, n in Counter(_shape(r["meeting_date"]) for r in rows).most_common(10):
        print(f"  {n:>7}  {v}")
    dates = sorted(d for d in (land_use.parse_meeting_date(r["meeting_date"]) for r in rows) if d)
    print(f"parsed date range: {dates[0] if dates else None} .. {dates[-1] if dates else None}; "
          f"unparseable rows: {len(rows) - len(dates)}")

    items: dict = defaultdict(list)
    for r in rows:
        items[(str(r["meeting_id"]), str(r["resolution_id"]))].append(r)
    print(f"\nitems (meeting_id, resolution_id): {len(items)}")
    per_year = Counter((land_use.parse_meeting_date(m[0]["meeting_date"]) or "????")[:4] for m in items.values())
    print("items per year: " + ", ".join(f"{y}: {n}" for y, n in sorted(per_year.items())))
    disagree = Counter()
    for members in items.values():
        for f in ("title", "result", "meeting_date"):
            if len({str(m[f]) for m in members}) > 1:
                disagree[f] += 1
    print(f"items whose member rows disagree: {dict(disagree) or 'none'}")

    titles = {k: m[0]["title"] or "" for k, m in items.items()}
    print("\ntitle keyword hits (items):")
    for kw in KEYWORDS:
        print(f"  {sum(kw in t.lower() for t in titles.values()):>7}  {kw!r}")
    print(f"titles at {land_use.TITLE_MAX}+ chars (likely truncated): "
          f"{sum(len(t) >= land_use.TITLE_MAX for t in titles.values())}")

    if cfg and cfg["land_use_patterns"]:
        scope = {k: t for k, t in titles.items() if land_use.classify(t, cfg)}
        label = "configured land_use_patterns"
    else:
        scope = {k: t for k, t in titles.items() if any(kw in t.lower() for kw in KEYWORDS[:10])}
        label = "land-use keywords (config still empty)"
    with_addr = {k: parse_title_addresses(t) for k, t in scope.items()}
    n_addr = sum(any(a.street_norm for a in v) for v in with_addr.values())
    n_gpin = sum(any(a.gpin10 for a in v) for v in with_addr.values())
    n_none = sum(not v for v in with_addr.values())
    print(f"\nextraction on {len(scope)} items matching {label}: street address {n_addr}, "
          f"printed GPIN {n_gpin}, neither {n_none}")
    print(f"sample titles with nothing extracted (up to {args.samples}):")
    for k in [k for k, v in with_addr.items() if not v][:args.samples]:
        print(f"  {k}: {scope[k][:200]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
