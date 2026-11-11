"""Write the Virginia Beach flood-exposure report: reports/flood_exposure_vb.md
plus CSVs (overall / by year / by ZIP, cohort and baseline).

Numbers come from ingest/flood_stats.compute_flood_stats -- the same function
behind GET /api/flood-stats, so the report and the API cannot disagree. Only
context filters exist (--issued-from, --issued-to, --zip); the cohort itself
is defined in config/flood_report.toml.

Refuses to run when:
  * the cohort is unconfigured (empty include list) -- run
    scripts/profile_vb_fields.py and fill the allowlist from observed values;
  * no flood load is active -- run scripts/load_flood_zones.py;
  * any VB permit is untagged -- run scripts/tag_flood_zones.py (or pass
    --allow-untagged, which reports them as a coverage column).

Usage:
  python scripts/flood_report.py
  python scripts/flood_report.py --issued-from 2020-01-01 --out reports/
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import flood_stats  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path(os.getenv("FLOOD_REPORT_CONFIG", str(BASE / "config" / "flood_report.toml")))
CSV_COLUMNS = (["population", "dimension", "key", "total", "matched"]
               + [f"n_{c}" for c in flood_stats.ALL_CLASSES]
               + [f"pct_{c}" for c in flood_stats.MATCHED] + ["pct_sfha_total", "suppressed"]
               + [f"coverage_pct_{c}" for c in flood_stats.COVERAGE])

CAVEATS = [
    "A permit is located by its address point, not its building footprint; a parcel can straddle a zone boundary.",
    "Zones come from the effective FIRM in the active load (vintage below), not preliminary maps or future conditions.",
    "Issued is not built: a permit's issue date is when work was authorized.",
    "SFHA (1% annual chance) is a regulatory line, not full flood risk; shaded X (0.2%) is shown separately and "
    "many flood claims occur outside the SFHA.",
    "Shares use matched permits only. 'no_polygon' (geocoded, no FEMA polygon matched) is a possible coverage "
    "gap and is never counted as outside the flood zone.",
]


def _csv_rows(pop: str, block: dict):
    for dim, rows in (("overall", [block["overall"]]), ("year", block["by_year"]), ("zip", block["by_zip"])):
        for r in rows:
            shares = r["shares_of_matched"] or {}
            yield [pop, dim, r["key"] if r["key"] is not None else "unknown", r["total"], r["matched"]] \
                + [r["counts"][c] for c in flood_stats.ALL_CLASSES] \
                + [shares.get(c, "") for c in flood_stats.MATCHED] + [shares.get("sfha_total", ""), int(r["suppressed"])] \
                + [r["coverage_pct_of_total"][c] if r["coverage_pct_of_total"][c] is not None else ""
                   for c in flood_stats.COVERAGE]


def _pct(v):
    return "–" if v is None else f"{v:.1f}%"


def _md_table(rows: list[dict], label: str) -> list[str]:
    out = [f"| {label} | permits | matched | % SFHA | % V | % A | % shaded X | % other matched | no polygon | "
           "not geocoded | untagged |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        s = r["shares_of_matched"] or {}
        c = r["counts"]
        out.append(f"| {r['key'] if r['key'] is not None else 'unknown'} | {r['total']} | {r['matched']} | "
                   f"{_pct(s.get('sfha_total'))} | {_pct(s.get('sfha_v'))} | {_pct(s.get('sfha_a'))} | "
                   f"{_pct(s.get('shaded_x'))} | {_pct(s.get('matched_non_sfha'))} | {c['no_polygon']} | "
                   f"{c['not_geocoded']} | {c['untagged']} |")
    return out


def render_markdown(stats: dict, date_range: tuple) -> str:
    load, cd = stats["load"], stats["cohort_definition"]
    lines = ["# Flood exposure of Virginia Beach building permits", "",
             f"Cohort: **{cd['name'] or 'unnamed'}** vs baseline (all issued permits).", "",
             "## Method", "",
             "- Population: Virginia Beach permits with an issue date, excluding void/expired/withdrawn.",
             "- Cohort: permits whose (PermitType, WorkType, ConstructionType) matches the allowlist below; "
             "`*` matches any value.",
             f"- Denominator for every share: permits matched to a FEMA polygon. Shares are suppressed "
             f"(shown as –) when fewer than {stats['min_n']} are matched.",
             "- Context filters, applied to cohort and baseline alike: "
             + ", ".join(f"{k}={v}" for k, v in stats["context_filters"].items() if v) if any(stats["context_filters"].values())
             else "- Context filters: none.",
             f"- Issue dates in the data: {date_range[0]} to {date_range[1]}.",
             f"- FEMA load {load['load_id']}: DFIRM {load['dfirm_id']}, vintage {load['source_vintage'] or 'unknown'}, "
             f"{load['feature_count']} polygons, content hash `{load['content_hash'][:16]}`, loaded {load['loaded_at']}.",
             "", "Cohort allowlist:", ""]
    for entry in cd["include"]:
        lines.append("- " + ", ".join(f"{k} = `{v}`" for k, v in entry.items()))
    for title, block in (("Cohort", stats["cohort"]), ("Baseline (all issued permits)", stats["baseline"])):
        lines += ["", f"## {title}", "", *_md_table([block["overall"]], "scope"), "",
                  f"Excluded from this population: {block['excluded']['unissued']} unissued, "
                  f"{block['excluded']['void_or_expired']} void/expired.", "",
                  "### By issue year", "", *_md_table(block["by_year"], "year"), "",
                  "### By ZIP", "", *_md_table(block["by_zip"], "ZIP")]
    lines += ["", "## Caveats", "", *[f"- {c}" for c in CAVEATS], ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Virginia Beach flood-exposure report")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--issued-from")
    p.add_argument("--issued-to")
    p.add_argument("--zip")
    p.add_argument("--out", type=Path, default=BASE / "reports")
    p.add_argument("--allow-untagged", action="store_true")
    args = p.parse_args(argv)
    try:
        cfg = flood_stats.load_cohort_config(args.config)
    except flood_stats.ConfigError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1
    if not cfg["include"]:
        print(f"[FATAL] cohort not configured in {args.config}: run scripts/profile_vb_fields.py and add the "
              "observed (permit_type, work_type, construction_type) values to [cohort].include", file=sys.stderr)
        return 1
    try:
        with db_module.db_readonly() as conn:
            if flood_stats.active_load(conn) is None:
                print("[FATAL] no active flood load: run scripts/load_flood_zones.py", file=sys.stderr)
                return 1
            untagged = conn.execute(
                "SELECT COUNT(*) FROM permits p JOIN permit_flood_effective e ON e.permit_id = p.id "
                "WHERE p.locality = ? AND e.effective_class = 'untagged'", (flood_stats.LOCALITY,)).fetchone()[0]
            if untagged and not args.allow_untagged:
                print(f"[FATAL] {untagged} permit(s) untagged: run scripts/tag_flood_zones.py "
                      "(or --allow-untagged to report them as a coverage column)", file=sys.stderr)
                return 1
            stats = flood_stats.compute_flood_stats(conn, cfg, args.issued_from, args.issued_to, args.zip)
            date_range = tuple(conn.execute(
                "SELECT MIN(issued_date), MAX(issued_date) FROM permits WHERE locality = ?",
                (flood_stats.LOCALITY,)).fetchone())
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    md = args.out / "flood_exposure_vb.md"
    md.write_text(render_markdown(stats, date_range), encoding="utf-8")
    csv_path = args.out / "flood_exposure_vb.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for pop in ("cohort", "baseline"):
            w.writerows(_csv_rows(pop, stats[pop]))
    o = stats["cohort"]["overall"]
    print(f"Wrote {md} and {csv_path}")
    print(f"cohort: {o['total']} permits, {o['matched']} matched, "
          f"SFHA {_pct((o['shares_of_matched'] or {}).get('sfha_total'))}; untagged {untagged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
