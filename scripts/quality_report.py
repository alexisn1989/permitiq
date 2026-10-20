"""Write the data-quality report: reports/quality_vb.md + quality_vb.json.

Coverage, unresolved cases and refresh age are computed from the current
database; accuracy comes from the latest imported tally and is shown only
if that tally's data snapshot still matches the current one -- otherwise it
is reported as stale and its figures are withheld (same rule as
GET /api/quality). Per-tier precision only; no unweighted overall figure.

Usage:
  python scripts/quality_report.py --out reports/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import quality  # noqa: E402

BASE = Path(__file__).resolve().parent.parent


def _pct(v):
    return "–" if v is None else f"{100 * v:.1f}%"


def render(q: dict, synthetic: bool = False, provisional: bool = False) -> str:
    acc = q["accuracy"]
    lines = ["# PermitIQ data quality — Virginia Beach", ""]
    if provisional:
        lines += ["> **PROVISIONAL** — Council outcomes not yet reviewed; lead counts are not final.", ""]
    if synthetic or acc.get("synthetic"):
        lines += ["> **SYNTHETIC REHEARSAL** — fixture data, not real results.", ""]
    lines += ["## Refresh age", "", "| source | as of | age (days) |", "|---|---|---:|"]
    lines += [f"| {k} | {v['as_of']} | {v['age_days'] if v['age_days'] is not None else '–'} |"
              for k, v in q["refresh_age"].items()] or ["| (no source dates recorded) | | |"]
    c = q["coverage"]
    lines += ["", "## Match coverage", "",
              f"- Virginia Beach permits: {c['permits_total']} ({c['permits_geocoded']} geocoded)",
              f"- Permits by parcel method: {c['permits_by_parcel_method']}",
              f"- Permits by point flood class: {c['permits_by_point_flood_class']}",
              f"- Council land-use decisions: {c['decisions_total']}; by status: {c['decisions_by_status']}",
              f"- Decision address rows by parcel method: {c['decision_rows_by_parcel_method']}",
              f"- Links (effective) by tier/state: {c['links_by_tier_state']}",
              "", "## Unresolved cases", ""]
    lines += [f"- {k}: {v}" for k, v in q["unresolved"].items()]
    lines += ["", "## Reviewed accuracy (adjudicated samples)", ""]
    if q.get("backfill"):
        b = q["backfill"]
        lines += [f"Permit backfill ({b['where_clause']}): source {b['source_count']}, DB {b['db_ids']}, truly missing "
                  f"{b['truly_missing']}, duplicate permit numbers {b['dup_permit_number']}, no permit number "
                  f"{b['no_permit_number']}, no application date {b['null_date_source']} — "
                  f"{'verified' if b['verified'] else '**NOT verified**'}.", "",
                  "| application year | source | DB permits |", "|---|---:|---:|"]
        lines += [f"| {y['year']} | {y['source']} | {y['db_permits']} |" for y in b["per_year"]]
        lines += [""]
    if acc["freshness"] != "fresh":
        lines += [f"**{acc['freshness'].upper()}** — {acc.get('note', '')}",
                  *([f"Changed since tally: {', '.join(acc['changed_since_tally'])}"] if acc.get("changed_since_tally") else [])]
    else:
        r = acc["results"]
        lines += [f"Tallied {acc['tallied_at']}. Precision = same project yes / (yes + no) among checked rows; "
                  "Wilson 95% interval. Per tier only.", "",
                  "| tier | sampled population N (at sampling) | current candidates | checked | precision | "
                  "95% interval | can't tell | stale |",
                  "|---|---:|---:|---:|---:|---|---:|---:|"]
        cur = acc.get("current_population", {})
        for t, v in r["links_by_tier"].items():
            ci = f"{_pct(v['wilson95'][0])}–{_pct(v['wilson95'][1])}" if v["wilson95"] else "–"
            lines.append(f"| {t} | {v['population']} | {cur.get('link_' + t, '–')} | {v['adjudicated']} | "
                         f"{_pct(v['precision'])} | {ci} | **{v['cant_tell']}** | {v['stale']} |")
        lines += ["", "N is the population recorded when the sample was drawn; current candidates is today's count "
                      "(checked links that were confirmed or rejected leave the candidate pool)."]
        lines += ["", f"Pooled: {r['links_pooled']['label']}"
                  + (f" — {_pct(r['links_pooled']['estimate'])}" if r['links_pooled']['estimate'] is not None else ""),
                  "", f"Leads (N={r['leads']['population']}, checked {r['leads']['adjudicated']}, "
                      f"can't tell **{r['leads']['cant_tell']}**): an independent search found a permit for "
                      f"{_pct(r['leads']['share_permit_found_elsewhere'])}; not an approval: "
                      f"{r['leads']['not_an_approval']}. {r['leads']['note']}.",
                  f"Flood disagreements (N={r['flood_disagreements']['population']}): "
                  f"{ {k: v for k, v in r['flood_disagreements'].items() if k not in ('population', 'note')} } — "
                  f"{r['flood_disagreements']['note']}."]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Data-quality report")
    p.add_argument("--out", type=Path, default=BASE / "reports")
    p.add_argument("--synthetic", action="store_true", help="label as a fixture rehearsal")
    p.add_argument("--provisional", action="store_true", help="outcome reviews not done yet: provisional banner")
    args = p.parse_args(argv)
    try:
        with db_module.db_readonly() as conn:
            q = quality.quality(conn)
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    q["synthetic"] = args.synthetic or bool(q["accuracy"].get("synthetic"))
    q["stage"] = "provisional" if args.provisional else "final"
    (args.out / "quality_vb.md").write_text(render(q, args.synthetic, args.provisional), encoding="utf-8")
    (args.out / "quality_vb.json").write_text(json.dumps(q, indent=1, default=str), encoding="utf-8")
    print(f"Wrote {args.out / 'quality_vb.md'} (accuracy: {q['accuracy']['freshness']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
