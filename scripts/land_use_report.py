"""Write the Virginia Beach land-use -> permit report: reports/land_use_vb.md
plus reports/land_use_vb.csv (time-to-permit blocks) and
reports/land_use_leads.csv (status no_candidate_permit).

Numbers come from ingest/land_use_stats.compute_land_use_stats -- the same
function behind GET /api/land-use/stats. Refuses until the linker has
recorded the permit observation window (scripts/link_decisions_permits.py).

Usage:
  python scripts/land_use_report.py
  python scripts/land_use_report.py --out reports/
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import land_use, land_use_stats, lead_facts  # noqa: E402
from ingest.flood_stats import ConfigError  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
MEASURE_LABELS = {
    "t_first_application": "first permit application",
    "t_first_issued": "first issued permit",
    "t_first_new_construction_application": "first new-construction application",
}
CAVEATS = [
    "Outcomes are Council outcomes verified by a person against the minutes or IQM2 item; vote counts alone are "
    "never treated as approval. Planning Commission recommendations are not in the source.",
    "A link is a property match (printed GPIN or street address), not proof the permit belongs to the approved "
    "project. Headline timings use confirmed links only; candidate-inclusive timings are labeled separately.",
    "'No candidate permit' means no matching permit appeared in the permit data observed between the dates above. "
    "It does not mean nothing was built, and it is suppressed when either end of that window is uncertain.",
    "Council titles are stored cut to 400 characters; addresses past the cut are lost (see truncated count).",
    "ingestion_runs does not record whether a permit refresh was capped (--limit); the last refresh is taken at face value.",
]


def _days(block: dict) -> str:
    if not block.get("configured", True):
        return "not configured"
    if block["suppressed"]:
        return f"suppressed (n={block['n']})"
    return f"median {block['median_days']} days (IQR {block['p25_days']}–{block['p75_days']}), n={block['n']}"


def render_markdown(stats: dict, truncated: int) -> str:
    cov = stats["coverage"]
    lines = ["# Virginia Beach land-use approvals → building permits", "",
             f"Permits observed from **{cov['backfill_from']}** to **{cov['last_refresh_at']}** "
             f"({cov['backfill_source']}). Link window: {cov['link_window_months']} months.", "",
             "## Decisions by status", "", "| status | decisions |", "|---|---:|"]
    lines += [f"| {s} | {stats['by_status'].get(s, 0)} |" for s in db_module.LAND_USE_STATUSES]
    lines += ["", f"Titles likely truncated at 400 characters: {truncated}.", "",
              "## Time to permit", "",
              f"One observation per decision per measure (earliest qualifying permit). Population: verified approvals "
              f"with a certain observation window ({', '.join(stats['timing_population'])}). Percentiles: "
              f"{stats['percentile_method']}; suppressed below n={stats['min_n']}. Open decisions are right-censored "
              "at the last refresh and counted, never imputed.", ""]
    for name, title in (("confirmed_only", "Confirmed links (headline)"),
                        ("including_candidates", "Including candidate links — unconfirmed property matches")):
        lines += [f"### {title}", "", "| measure | result | open |", "|---|---|---:|"]
        for m, label in MEASURE_LABELS.items():
            b = stats["timing"][name][m]
            lines.append(f"| {label} | {_days(b)} | {b.get('n_open', '–')} |")
        lines.append("")
    lines += ["## By land-use label", "", "| label | status | decisions |", "|---|---|---:|"]
    lines += [f"| {r['land_use_label']} | {r['status']} | {r['n']} |" for r in stats["by_label"]]
    lines += ["", "## Caveats", "", *[f"- {c}" for c in CAVEATS], ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Virginia Beach land-use -> permit report")
    p.add_argument("--config", type=Path, default=BASE / "config" / "land_use.toml")
    p.add_argument("--out", type=Path, default=BASE / "reports")
    p.add_argument("--provisional", action="store_true",
                   help="mark every output provisional (outcome reviews not done yet)")
    args = p.parse_args(argv)
    try:
        cfg = land_use.load_config(args.config)
        with db_module.db_readonly() as conn:
            stats = land_use_stats.compute_land_use_stats(conn, cfg)
            if stats["coverage"] is None:
                print("[FATAL] no permit observation window recorded: run scripts/link_decisions_permits.py",
                      file=sys.stderr)
                return 1
            truncated = conn.execute("SELECT COUNT(*) FROM land_use_decisions WHERE title_truncated = 1").fetchone()[0]
            leads = conn.execute(
                "SELECT d.decision_key, d.meeting_date, d.land_use_label, d.title, d.title_truncated, s.verified_outcome, s.status, "
                "(SELECT group_concat(a.raw_text, ' | ') FROM land_use_decision_addresses a "
                " WHERE a.decision_key = d.decision_key) AS addresses "
                "FROM land_use_decisions d JOIN land_use_decision_status s ON s.decision_key = d.decision_key "
                "WHERE s.status = 'no_candidate_permit' ORDER BY d.meeting_date").fetchall()
            timings = {r["decision_key"]: lead_facts.timing(conn, dict(r), cfg) for r in leads}
    except ConfigError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    md = render_markdown(stats, truncated)
    if args.provisional:
        md = md.replace("\n", "\n\n> **PROVISIONAL** — Council outcomes not yet reviewed; lead counts are not final.\n", 1)
    (args.out / "land_use_vb.md").write_text(md, encoding="utf-8")
    with (args.out / "land_use_vb.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["links", "measure", "configured", "n", "n_open", "suppressed", "median_days", "p25_days", "p75_days"])
        for name, blocks in stats["timing"].items():
            for m, b in blocks.items():
                w.writerow([name, m, int(b.get("configured", True)), b.get("n"), b.get("n_open"),
                            int(b.get("suppressed", False)) if "suppressed" in b else "", b.get("median_days"),
                            b.get("p25_days"), b.get("p75_days")])
    cov = stats["coverage"]
    with (args.out / "land_use_leads.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        # applicant / request / size are extracted from the title (unverified); applicant only when an organization
        w.writerow(["stage", "decision_key", "meeting_date", "land_use_label", "verified_outcome", "addresses", "title",
                    "permits_observed_from", "permits_observed_to", "applicant_from_title", "request_from_title",
                    "size_from_title", "days_since_approval", "link_window_end", "typical_first_application_from",
                    "typical_first_application_to", "past_typical_range"])
        for r in leads:
            a, t = lead_facts.applicant(r["title"]), timings[r["decision_key"]]
            typ = t["typical_first_application"] or {}
            w.writerow(["provisional" if args.provisional else "final", r["decision_key"], r["meeting_date"],
                        r["land_use_label"], r["verified_outcome"],
                        r["addresses"], r["title"], cov["backfill_from"], cov["last_refresh_at"],
                        a["name"] or ("(individual: withheld)" if a["kind"] == "withheld" else ""),
                        lead_facts.request(r["title"])["text"] or "",
                        "; ".join(x["text"] for x in lead_facts.scale(r["title"])),
                        t["days_since_approval"], t["window_end"], typ.get("from", ""), typ.get("to", ""),
                        "" if t["past_typical_range"] is None else int(t["past_typical_range"])])
    print(f"Wrote {args.out / 'land_use_vb.md'}, land_use_vb.csv, land_use_leads.csv ({len(leads)} leads)")
    print(f"status: {stats['by_status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
