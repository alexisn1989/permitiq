"""Tally a checked accuracy sample (scripts/accuracy_sample.py).

1. Integrity: the sheet's fact columns must hash to the meta's sample_hash
   (an edited sheet is refused). If the active flood / parcel load, coverage
   runs or Council export changed since the sample was drawn, the WHOLE sample
   is stale: nothing is tallied, imported or applied (exit 2).
2. Validation: each row uses only its stratum's judgment column (links:
   same_project; leads: lead_check + search_done, an independent search
   outside permits.db; flood: flood_check), a filled judgment needs a
   checker. Any invalid row => nothing is written (exit 1).
3. Per row, the current facts fingerprint is recomputed; a row whose facts
   changed is stale and excluded.
4. Results, kept narrow: per-tier adjudicated-sample precision
   yes / (yes + no) with a Wilson 95% interval, beside the full population N,
   n checked, cant_tell and stale counts. No unweighted overall link accuracy;
   a population-weighted estimate is shown only if every tier with candidates
   was adjudicated. Leads: share where an independent search found a permit.
   Flood: counts per judgment among disagreements (not error rates).
5. --apply-links: ONLY link rows with an explicit same_project yes / no whose
   facts are unchanged become confirmed / rejected link reviews, through
   review_land_use.py (window rule included). Lead and flood rows never do.
6. The tally (accuracy_tally.json) is imported into quality_tallies with the
   snapshot taken AFTER applying, so /api/quality can tell when later data
   changes make it stale.

Usage:
  python scripts/accuracy_tally.py reports/run_x/accuracy_sample.csv [--apply-links] [--no-import]
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import accuracy, quality  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
LOAD_KEYS = ("flood_load_id", "parcel_load_id", "coverage_runs", "council_export_sha256")


def _review_land_use():
    spec = importlib.util.spec_from_file_location("review_land_use", BASE / "scripts" / "review_land_use.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def validate(rows: list[dict]) -> list[str]:
    errors = []
    for i, r in enumerate(rows, 2):
        kind = accuracy.kind_of(r["stratum"])
        col, allowed = accuracy.JUDGMENTS[kind]
        for other_kind, (other_col, _) in accuracy.JUDGMENTS.items():
            if other_col != col and r.get(other_col):
                errors.append(f"line {i}: {r['stratum']} row must not fill {other_col} (use {col})")
        v = r.get(col, "")
        if not v:
            continue
        if v not in allowed:
            errors.append(f"line {i}: {col} must be one of {allowed} (got {v!r})")
        if not r.get("checker"):
            errors.append(f"line {i}: checker is required")
        if kind == "lead" and not r.get("search_done"):
            errors.append(f"line {i}: search_done is required -- describe the independent search outside permits.db "
                          "(city permit portal / ArcGIS layer by address or GPIN, Council minutes)")
    return errors


def tally(rows: list[dict], meta: dict, stale_ids: set) -> dict:
    by = {}
    for r in rows:
        by.setdefault(r["stratum"], []).append(r)
    links, pooled_parts = {}, []
    for tier in accuracy.TIERS:
        s = f"link_{tier}"
        rs = by.get(s, [])
        c = Counter("stale" if f"{s}:{r['row_id']}" in stale_ids else (r["same_project"] or "unchecked") for r in rs)
        adj = c["yes"] + c["no"]
        pop = meta["strata"].get(s, {}).get("population", 0)
        links[tier] = {"population": pop, "sampled": len(rs), "yes": c["yes"], "no": c["no"],
                       "cant_tell": c["cant_tell"], "unchecked": c["unchecked"], "stale": c["stale"],
                       "adjudicated": adj, "precision": round(c["yes"] / adj, 4) if adj else None,
                       "wilson95": accuracy.wilson(c["yes"], adj),
                       "cant_tell_share_of_checked": round(c["cant_tell"] / (adj + c["cant_tell"]), 4)
                       if adj + c["cant_tell"] else None}
        if pop:
            pooled_parts.append((pop, links[tier]["precision"]))
    if pooled_parts and all(p is not None for _, p in pooled_parts):
        total = sum(n for n, _ in pooled_parts)
        pooled = {"estimate": round(sum(n * p for n, p in pooled_parts) / total, 4),
                  "label": "population-weighted estimate across tiers (each tier sampled separately); "
                           "not a single random sample, no interval"}
    else:
        pooled = {"estimate": None, "label": "omitted: not every tier with candidates was adjudicated"}
    lr = by.get("lead", [])
    lc = Counter("stale" if f"lead:{r['row_id']}" in stale_ids else (r["lead_check"] or "unchecked") for r in lr)
    lead_adj = lc["permit_found_elsewhere"] + lc["no_permit_found"] + lc["not_an_approval"]
    fr = by.get("flood", [])
    fc = Counter("stale" if f"flood:{r['row_id']}" in stale_ids else (r["flood_check"] or "unchecked") for r in fr)
    return {
        "links_by_tier": links,
        "links_pooled": pooled,
        "leads": {"population": meta["strata"].get("lead", {}).get("population", 0), "sampled": len(lr),
                  **{k: lc[k] for k in ("permit_found_elsewhere", "no_permit_found", "not_an_approval", "cant_tell",
                                        "unchecked", "stale")},
                  "adjudicated": lead_adj,
                  "share_permit_found_elsewhere": round(lc["permit_found_elsewhere"] / lead_adj, 4) if lead_adj else None,
                  "wilson95_found_elsewhere": accuracy.wilson(lc["permit_found_elsewhere"], lead_adj),
                  "search_methods": sorted({r["search_done"] for r in lr if r.get("search_done")})[:20],
                  "note": "independent-search check of sampled leads; not recall"},
        "flood_disagreements": {"population": meta["strata"].get("flood", {}).get("population", 0),
                                "sampled": len(fr), **dict(fc),
                                "note": "judgments among point-vs-parcel disagreements; not error rates"},
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Tally a checked accuracy sample")
    p.add_argument("sheet", type=Path)
    p.add_argument("--meta", type=Path)
    p.add_argument("--apply-links", action="store_true")
    p.add_argument("--no-import", action="store_true")
    p.add_argument("--config", type=Path, default=BASE / "config" / "land_use.toml")
    args = p.parse_args(argv)
    meta_path = args.meta or args.sheet.with_name("accuracy_sample.meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    with args.sheet.open(newline="", encoding="utf-8") as fh:
        rows = [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]
    if accuracy.sample_hash(rows) != meta["sample_hash"]:
        print("[FATAL] the sheet's fact columns do not match the sample hash -- facts were edited, or rows "
              "added / removed / reordered; only judgment columns may be filled", file=sys.stderr)
        return 1
    errors = validate(rows)
    if errors:
        print("[FATAL] nothing written:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    with db_module.db_readonly() as conn:
        snap_now = quality.snapshot(conn)
        pops = accuracy.populations(conn)
    changed_loads = [k for k in LOAD_KEYS if meta["snapshot"].get(k) != snap_now.get(k)]
    if changed_loads:
        print(f"[STALE] the whole sample is stale: {changed_loads} changed since it was drawn. Draw a new sample.",
              file=sys.stderr)
        return 2
    stale = {f"{r['stratum']}:{r['row_id']}" for r in rows
             if pops.get(r["stratum"], {}).get(r["row_id"], {}).get("fp") != r["fp"]}
    results = tally(rows, meta, stale)
    print(json.dumps({"links_by_tier": {t: {k: v[k] for k in ("population", "adjudicated", "precision", "wilson95",
                                                              "cant_tell", "stale")}
                                        for t, v in results["links_by_tier"].items()},
                      "leads": {k: results["leads"][k] for k in ("population", "adjudicated",
                                                                  "share_permit_found_elsewhere", "cant_tell")},
                      "stale_rows": len(stale)}, indent=1))
    if args.apply_links:
        apply_rows = [r for r in rows if accuracy.kind_of(r["stratum"]) == "link"
                      and r["same_project"] in ("yes", "no") and f"{r['stratum']}:{r['row_id']}" not in stale]
        if apply_rows:
            path = args.sheet.with_name("accuracy_link_verdicts.csv")
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["decision_key", "permit_id", "verdict", "reviewer", "note"])
                for r in apply_rows:
                    w.writerow([r["decision_key"], r["permit_id"],
                                "confirmed" if r["same_project"] == "yes" else "rejected", r["checker"],
                                f"accuracy sample (seed {meta['seed']}): {r['note']}".strip()])
            rc = _review_land_use().main(["--links", str(path), "--config", str(args.config)])
            if rc != 0:
                print("[FATAL] link verdicts refused by review_land_use.py; tally not imported", file=sys.stderr)
                return rc
            print(f"{len(apply_rows)} checked link(s) applied as link reviews")
    with db_module.db() as conn:
        snap_after = quality.snapshot(conn)  # after applying: the state this accuracy describes
        tally_doc = {"tallied_at": db_module.now_iso(), "snapshot": snap_after, "sample_hash": meta["sample_hash"],
                     "seed": meta["seed"], "synthetic": bool(meta.get("synthetic")), "results": results,
                     "stale_rows": sorted(stale)}
        if not args.no_import:
            tid = quality.import_tally(conn, tally_doc, db_module.now_iso())
            print(f"tally imported as quality_tallies.{tid}")
    args.sheet.with_name("accuracy_tally.json").write_text(json.dumps(tally_doc, indent=1, default=str),
                                                           encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
