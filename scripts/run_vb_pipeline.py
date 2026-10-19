"""Run the Virginia Beach pipeline end to end on real data, logged (real-data
hardening). Every step is one of the existing CLI scripts, run as a
subprocess; output goes to reports/run_<timestamp>/ with one log per step
and run_manifest.json (commands, exit codes, times, output tails, the order
executed, and the source dates). Stops at the first failure. A step that
refuses for a documented reason (e.g. the flood-report cohort is not
configured yet) is recorded as skipped with that reason, never as success.

Phase profile (first time, before the configs are filled):
  migrate -> permit ingest -> profile permit fields -> profile the Council
  export -> parcel layer --inspect
Phase full, in dependency order:
  migrate -> [permit refresh] -> Council decisions ingest -> geocode them ->
  FEMA load -> tag permit points -> parcel load -> match parcels (permits AND
  the decisions just ingested, cross-row reconciliation) -> parcel flood ->
  tag decision points -> link -> import filled review files -> [re-link if
  any were imported] -> record source dates -> reports, unmatched reasons,
  accuracy sample, quality report

The Council export itself is produced on the VoteIQ host with
tools/export_vb_council_votes.py (read-only) and passed in with
--council-export.

Usage:
  python scripts/run_vb_pipeline.py --phase profile --council-export vb_council_votes.jsonl
  python scripts/run_vb_pipeline.py --phase full --council-export vb_council_votes.jsonl \\
      [--parcels-file vb_parcels.geojson --parcels-count 152345] [--reviews-dir reviews/] [--seed 20261019]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCRIPTS = BASE / "scripts"
TAIL = 40


class StepFailed(Exception):
    pass


class Runner:
    def __init__(self, run_dir: Path, manifest: dict):
        self.run_dir, self.manifest, self.n = run_dir, manifest, 0
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)

    def step(self, name: str, script: str, args: list[str], skip_if: tuple[str, ...] = ()) -> str:
        self.n += 1
        cmd = [sys.executable, str(SCRIPTS / script) if not script.endswith("run_ingest.py") else str(BASE / script),
               *[str(a) for a in args]]
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        proc = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True, env=os.environ.copy())
        ended = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log = self.run_dir / "logs" / f"{self.n:02d}_{name}.log"
        log.write_text(f"$ {' '.join(cmd)}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n", encoding="utf-8")
        status, reason = "ok", None
        if proc.returncode != 0:
            hit = next((pat for pat in skip_if if pat in proc.stdout + proc.stderr), None)
            status, reason = ("skipped", f"refused: {hit}") if hit else ("failed", proc.stderr.strip()[-500:])
        self.record(name, cmd, proc.returncode, started, ended, status, reason,
                    (proc.stdout + proc.stderr).strip().splitlines()[-TAIL:], log)
        if status == "failed":
            raise StepFailed(name)
        return status

    def skip(self, name: str, reason: str) -> None:
        self.n += 1
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.record(name, None, None, now, now, "skipped", reason, [], None)

    def record(self, name, cmd, code, started, ended, status, reason, tail, log):
        self.manifest["steps"].append({"n": self.n, "name": name, "command": cmd, "exit_code": code,
                                       "started": started, "ended": ended, "status": status, "reason": reason,
                                       "output_tail": tail, "log": str(log.relative_to(self.run_dir)) if log else None})
        self.manifest["order_executed"].append(name)
        self.save()
        print(f"[{status:>7}] {self.n:02d} {name}" + (f" -- {reason}" if reason else ""))

    def save(self):
        (self.run_dir / "run_manifest.json").write_text(json.dumps(self.manifest, indent=1, default=str),
                                                        encoding="utf-8")


def _filled(path: Path, column: str) -> bool:
    if not path.is_file():
        return False
    with path.open(newline="", encoding="utf-8") as fh:
        return any((r.get(column) or "").strip() for r in csv.DictReader(fh))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Run the Virginia Beach pipeline, logged")
    p.add_argument("--phase", choices=("profile", "full"), required=True)
    p.add_argument("--council-export", type=Path, required=True)
    p.add_argument("--since", help="permit ingest --since (YYYY-MM-DD)")
    p.add_argument("--refresh-permits", action="store_true", help="full phase: re-run the permit ingest first")
    p.add_argument("--nfhl-file", type=Path, help="FEMA GeoJSON instead of the service")
    p.add_argument("--parcels-file", type=Path)
    p.add_argument("--parcels-count", type=int, help="required with --parcels-file (load_parcels.py --inspect)")
    p.add_argument("--reviews-dir", type=Path, help="folder with filled review CSVs to import")
    p.add_argument("--land-use-config", type=Path, default=BASE / "config" / "land_use.toml")
    p.add_argument("--flood-config", type=Path, default=BASE / "config" / "flood_report.toml")
    p.add_argument("--seed", type=int, default=int(datetime.now(timezone.utc).strftime("%Y%m%d")))
    p.add_argument("--no-geocode", action="store_true", help="skip decision geocoding (recorded as skipped)")
    p.add_argument("--out-root", type=Path, default=BASE / "reports")
    p.add_argument("--synthetic", action="store_true", help="label everything as a fixture rehearsal")
    args = p.parse_args(argv)
    if args.parcels_file and args.parcels_count is None:
        p.error("--parcels-file requires --parcels-count")

    run_dir = args.out_root / f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{args.phase}"
    manifest = {"phase": args.phase, "synthetic": args.synthetic, "seed": args.seed, "run_dir": str(run_dir),
                "data_dir": os.environ.get("DATA_DIR", str(BASE)), "council_export": str(args.council_export),
                "steps": [], "order_executed": []}
    r = Runner(run_dir, manifest)
    lu = ["--config", args.land_use_config]
    try:
        r.step("migrate", "migrate_db.py", [])
        if args.phase == "profile" or args.refresh_permits:
            r.step("permit_ingest", "run_ingest.py",
                   ["--locality", "virginia_beach", *(["--since", args.since] if args.since else [])])
        if args.phase == "profile":
            r.step("profile_permit_fields", "profile_vb_fields.py", [])
            r.step("profile_council_export", "profile_land_use_export.py", ["--file", args.council_export, *lu])
            if args.parcels_file:
                r.skip("parcel_layer_inspect", "--parcels-file given; use load_parcels.py --inspect for the count")
            else:
                r.step("parcel_layer_inspect", "load_parcels.py", ["--inspect"])
            print(f"\nProfile done: {run_dir}. Fill config/flood_report.toml and config/land_use.toml from these "
                  "logs, then run --phase full.")
            return 0

        r.step("council_ingest", "ingest_land_use_decisions.py",
               ["--file", args.council_export, *lu, "--reports-dir", run_dir])
        if args.no_geocode:
            r.skip("council_geocode", "--no-geocode")
        else:
            r.step("council_geocode", "ingest_land_use_decisions.py", ["--geocode", *lu])
        r.step("fema_load", "load_flood_zones.py", ["--file", args.nfhl_file] if args.nfhl_file else [])
        r.step("tag_permit_points", "tag_flood_zones.py", [])
        r.step("parcel_load", "load_parcels.py",
               ["--file", args.parcels_file, "--expected-count", args.parcels_count] if args.parcels_file else [])
        r.step("match_parcels", "match_parcels.py", ["--reports-dir", run_dir])
        r.step("parcel_flood", "tag_parcel_flood.py", [])
        r.step("tag_decision_points", "tag_flood_zones.py", ["--decisions"])
        r.step("link", "link_decisions_permits.py", [*lu, "--reports-dir", run_dir])

        imported = 0
        rd = args.reviews_dir
        if rd and _filled(rd / "land_use_review.csv", "verified_outcome"):
            r.step("import_outcome_reviews", "review_land_use.py", ["--outcomes", rd / "land_use_review.csv", *lu])
            imported += 1
        if rd and _filled(rd / "parcel_review.csv", "verdict"):
            r.step("import_parcel_reviews", "review_parcels.py", [rd / "parcel_review.csv"])
            imported += 1
        if rd and _filled(rd / "link_review.csv", "verdict"):
            r.step("import_link_reviews", "review_land_use.py", ["--links", rd / "link_review.csv", *lu])
            imported += 1
        if imported:
            # verdicts change link_gpin / gpin_trusted / confirmed links: link again before any report
            r.step("relink_after_reviews", "link_decisions_permits.py", [*lu, "--reports-dir", run_dir])
        elif rd:
            r.skip("import_reviews", f"no filled review files in {rd}")

        r.step("record_source_dates", "record_source_dates.py", ["--council-export", args.council_export])
        r.step("flood_report", "flood_report.py", ["--config", args.flood_config, "--out", run_dir],
               skip_if=("cohort not configured",))
        r.step("land_use_report", "land_use_report.py", [*lu, "--out", run_dir])
        r.step("parcel_report", "parcel_report.py", ["--out", run_dir, "--config", args.flood_config])
        r.step("unmatched_reasons", "unmatched_reasons.py", ["--out", run_dir])
        r.step("accuracy_sample", "accuracy_sample.py",
               ["--seed", args.seed, "--out", run_dir, *(["--synthetic"] if args.synthetic else [])])
        r.step("quality_report", "quality_report.py", ["--out", run_dir, *(["--synthetic"] if args.synthetic else [])])
    except StepFailed as e:
        print(f"\nStopped at failed step {e}; see {run_dir / 'run_manifest.json'} and its log.", file=sys.stderr)
        return 1
    sys.path.insert(0, str(BASE))
    import db as db_module  # noqa: E402 - the run's own DB (DATA_DIR), read-only
    from ingest import quality  # noqa: E402
    with db_module.db_readonly() as conn:
        dates = quality.data_as_of(conn)
    manifest["source_dates"] = dates
    r.save()
    print(f"\nDone: {run_dir}. Check accuracy_sample.csv (an independent search for each lead row), then "
          "scripts/accuracy_tally.py --apply-links, then run --phase full again with --reviews-dir.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
