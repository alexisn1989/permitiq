"""Write the data-quality report: reports/quality_vb.md + quality_vb.json.

Coverage, unresolved cases and refresh age are computed from the current
database; accuracy comes from the latest imported tally and is shown only
if that tally's data snapshot still matches the current one -- otherwise it
is reported as stale and its figures are withheld (same rule as
GET /api/quality). Per-tier precision only; no unweighted overall figure.

With --manifest (the run's run_manifest.json; the runbook passes it), the
report adds a Run record (every failed, skipped or gap step) and fills the
locality acceptance checklist's run line.

Usage:
  python scripts/quality_report.py --out reports/
  python scripts/quality_report.py --out reports/run_x --manifest reports/run_x/run_manifest.json
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


MARK = {True: "pass", False: "**FAIL**", None: "not yet"}


def render(q: dict, synthetic: bool = False, provisional: bool = False) -> str:
    acc = q["accuracy"]
    lines = ["# PermitIQ data quality — Virginia Beach", ""]
    if provisional:
        lines += ["> **PROVISIONAL** — Council outcomes not yet reviewed; lead counts are not final.", ""]
    if synthetic or acc.get("synthetic"):
        lines += ["> **SYNTHETIC REHEARSAL** — fixture data, not real results.", ""]
    if q.get("checklist"):
        lines += ["## Locality acceptance checklist", "",
                  "The standard each locality must meet. \"not yet\" is never a pass.", "",
                  "| check | result | detail |", "|---|---|---|"]
        lines += [f"| {c['item']} | {MARK[c['passed']]} | {c['detail']} |" for c in q["checklist"]]
        lines += [""]
    rec = q.get("run_record")
    if rec is not None:
        lines += ["## Run record", "", f"Phase {rec['phase']}, stage {rec['stage']}: {rec['steps_total']} steps.", ""]
        if rec["not_ok"]:
            lines += ["| step | status | reason |", "|---|---|---|"]
            lines += [f"| {s['name']} | {s['status']} | {(s['reason'] or '').replace('|', '/')[:300]} |"
                      for s in rec["not_ok"]]
        else:
            lines += ["Every step finished ok."]
        lines += [""]
    pc = q.get("planning_commission")
    if pc:
        c = pc["coverage"]
        lines += ["## Planning Commission hearings", "",
                  f"Agendas found for {c['agenda_found']} of {c['months']} expected monthly hearings "
                  f"({c['from']}–{c['to']}); minutes for {c['minutes_found']}. "
                  + ("Coverage verified against the city's agenda index."
                     if c["verified"] else f"**Coverage unverified** — {c['note']}."), "",
                  f"- Documents: {pc['documents_by_status']}",
                  f"- Items: {pc['items']} ({pc['items_with_warnings']} parsed with warnings); by status: "
                  f"{pc['items_by_status']}",
                  f"- Items with a possible Council decision: {pc['items_with_council_candidate']}",
                  "- A Planning Commission recommendation is advice to City Council, not an approval.", ""]
    fq = q.get("filings")
    if fq:
        ld = fq["load"]
        lines += ["## Applications filed", "",
                  f"{ld['received_count']} of {ld['expected_count']} source records ({ld['where_clause']}) loaded as "
                  f"{ld['filings']} applications (polygons sharing an Accela record merged); newest filed "
                  f"{ld['max_date_sub']}. Parcels found for {fq['with_parcels']}.", "",
                  f"- By stage: {fq['by_stage']}",
                  f"- Planning Commission agenda records found in the filing layer: {fq['agenda_records_in_filings']} "
                  f"of {fq['agenda_records']}",
                  f"- Filed over 6 months ago with no hearing found: {fq['filed_over_6_months_no_hearing']} "
                  "(the layer has no status: these may be withdrawn or still in review)",
                  "- A filed application is not an approval.", ""]
    cq = q.get("council_actions")
    if cq:
        years = cq["coverage"]["years"]
        lines += ["## City Council actions (Clerk's Summary of Council Actions)", "",
                  f"{sum(y['stored'] for y in years)} summaries stored ({cq['date_range'][0]}–{cq['date_range'][1]}); "
                  + ("every summary listed in the Clerk's archive for those years was read."
                     if cq["coverage"]["verified"] else "**coverage incomplete** — see years below."), "",
                  "| year | listed | stored | status |", "|---|---:|---:|---|"]
        lines += [f"| {y['year']} | {y['listed']} | {y['stored']} | {y['status']} |" for y in years]
        lines += ["",
                  f"- Land-use items: {cq['land_use_items']}; by outcome: {cq['land_use_by_outcome']}",
                  f"- Read with gaps (OCR): tally not readable {cq['tally_not_readable']}, no street address "
                  f"{cq['no_address']}, item label not readable {cq['label_not_readable']} (listed in "
                  "clerk_parse_review.csv)",
                  f"- Planning Commission items with a Council stage: {cq['pc_items_with_council_stage']} of "
                  f"{cq['pc_items']}; {cq['pc_items_by_council_stage']}",
                  f"- Possible matches not used for a stage: {cq['ambiguous_links'] or 'none'}",
                  f"- Council decisions (vote data) matched to a Clerk item: {cq['decisions_checked']}",
                  "- Outcomes are read from scanned pages (OCR text); per-member votes are not read.", ""]
    sq = q.get("site_constraints")
    if sq:
        lines += ["## Site constraint layers (screening)", "",
                  "| layer | features (expected = received) | loaded at | lead parcels touching |", "|---|---:|---|---:|"]
        lines += [f"| {k} | {v['features']} ({v['expected_count']} = {v['received_count']}) | {v['loaded_at']} | "
                  f"{v['parcels']} |" for k, v in sorted(sq["layers"].items())]
        lines += ["", "| board history | actions (records read) | without a date | newest |", "|---|---:|---:|---|"]
        lines += [f"| {k} | {v['actions']} ({v['received_count']}) | {v['no_date']} | {v['max_date_sub']} |"
                  for k, v in sorted(sq["boards"].items())]
        lines += ["", "- The RPA layer is the city's boundary line: parcels are reported as crossed by it or within "
                      "100 ft of it, never as RPA acres.",
                  "- Screening estimates from city GIS layers, not determinations. Not covered: sewer / water "
                  "capacity (no public data); hydric soils, NWI wetlands, VDOT, state / National Register "
                  "historic boundaries (pending source access).", ""]
    if q.get("source_rows"):
        lines += ["## Source rows", "", "| source | rows |", "|---|---:|"]
        lines += [f"| {k} | {v} |" for k, v in q["source_rows"].items()]
        lines += [""]
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
        council = [(name, r.get(key)) for name, key in (("Council outcome read correctly", "council_outcomes"),
                                                         ("Council item ↔ hearing item", "council_links"))]
        if any(v for _, v in council):
            lines += ["", "| check | population N | checked | precision | 95% interval | can't tell | stale |",
                      "|---|---:|---:|---:|---|---:|---:|"]
            for name, v in council:
                if v:
                    ci = f"{_pct(v['wilson95'][0])}–{_pct(v['wilson95'][1])}" if v["wilson95"] else "–"
                    lines.append(f"| {name} | {v['population']} | {v['adjudicated']} | {_pct(v['precision'])} | "
                                 f"{ci} | **{v['cant_tell']}** | {v['stale']} |")
            lines += ["", "Checked against the Clerk's page and the Planning Commission agenda linked on each row."]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Data-quality report")
    p.add_argument("--out", type=Path, default=BASE / "reports")
    p.add_argument("--synthetic", action="store_true", help="label as a fixture rehearsal")
    p.add_argument("--provisional", action="store_true", help="outcome reviews not done yet: provisional banner")
    p.add_argument("--manifest", type=Path, help="run_manifest.json: adds the run record")
    p.add_argument("--min-n", type=int, default=30, help="checklist target per stratum: min(this, N) checked")
    args = p.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else None
    try:
        with db_module.db_readonly() as conn:
            q = quality.quality(conn)
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    q["synthetic"] = args.synthetic or bool(q["accuracy"].get("synthetic"))
    q["stage"] = "provisional" if args.provisional else "final"
    q["run_record"] = quality.run_record(manifest)
    q["checklist"] = quality.acceptance_checklist(q, q["run_record"], args.min_n)
    (args.out / "quality_vb.md").write_text(render(q, args.synthetic, args.provisional), encoding="utf-8")
    (args.out / "quality_vb.json").write_text(json.dumps(q, indent=1, default=str), encoding="utf-8")
    print(f"Wrote {args.out / 'quality_vb.md'} (accuracy: {q['accuracy']['freshness']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
