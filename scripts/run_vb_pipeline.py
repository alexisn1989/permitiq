"""Run the Virginia Beach pipeline end to end on real data, logged (real-data
hardening). Every step is one of the existing CLI scripts, run as a
subprocess; output goes to reports/run_<timestamp>/ with one log per step
and run_manifest.json (commands, exit codes, times, output tails, the order
executed, and the source dates). Stops at the first failure. A step that
refuses for a documented reason (e.g. the flood-report cohort is not
configured yet) is recorded as skipped with that reason, never as success.

Phase profile (first time, before the configs are filled):
  migrate -> permit ingest (--batch-geocode: without geocoding) -> verify the
  backfill against the source's own count and OBJECTID set, top up gaps ->
  batch geocode -> profile permit fields -> profile the Council export ->
  parcel layer --inspect
Stages (full / report): provisional until outcome reviews exist -- outputs
carry a provisional banner and no accuracy sample is drawn; final refuses
without reviews or with stale ones, and only it draws the sample (from the
reviewed snapshot). Phase report regenerates reports only, keeping the
existing sample and its recorded population (use after accuracy_tally.py).
Each phase records the database's SHA-256 (after a WAL checkpoint).
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

    def step(self, name: str, script: str, args: list[str], skip_if: tuple[str, ...] = (),
             gap_codes: set[int] = frozenset()) -> str:
        self.n += 1
        target = BASE / script if script == "run_ingest.py" else SCRIPTS / script
        cmd = [sys.executable, str(target), *[str(a) for a in args]]
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        proc = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True, env=os.environ.copy())
        ended = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log = self.run_dir / "logs" / f"{self.n:02d}_{name}.log"
        log.write_text(f"$ {' '.join(cmd)}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n", encoding="utf-8")
        status, reason = "ok", None
        if proc.returncode in gap_codes:
            status, reason = "gap", "completed with documented gaps (see log)"
        elif proc.returncode != 0:
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


def _db_state(fn):
    """Run fn(conn) against the run's own DB (DATA_DIR)."""
    sys.path.insert(0, str(BASE))
    import db as db_module  # noqa: E402
    with db_module.db() as conn:
        return fn(conn)


def _db_hash() -> dict:
    """Checkpoint the WAL, then hash the database file (recorded at each phase end)."""
    import hashlib
    sys.path.insert(0, str(BASE))
    import db as db_module  # noqa: E402
    with db_module.db() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    path = Path(db_module.DB_PATH)
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": h.hexdigest()}


def _stage_check(conn) -> str | None:
    """None if a final run is allowed, else the reason it is not."""
    reviews = conn.execute("SELECT COUNT(*) FROM decision_outcome_review").fetchone()[0]
    stale = conn.execute("SELECT COUNT(*) FROM land_use_decision_status WHERE status = 'outcome_review_stale'").fetchone()[0]
    if not reviews:
        return "no outcome reviews imported yet"
    if stale:
        return f"{stale} outcome review(s) are stale -- re-review them first"
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Run the Virginia Beach pipeline, logged")
    p.add_argument("--phase", choices=("profile", "full", "report"), required=True)
    p.add_argument("--stage", choices=("provisional", "final"), default="provisional",
                   help="full/report: provisional until outcome reviews exist; only final draws the accuracy sample")
    p.add_argument("--council-export", type=Path, required=True)
    p.add_argument("--since", help="permit ingest --since (YYYY-MM-DD); also the backfill completeness window")
    p.add_argument("--refresh-permits", action="store_true", help="full phase: re-run the permit ingest first")
    p.add_argument("--batch-geocode", action="store_true",
                   help="ingest with --no-geocode, then geocode with the Census-batch-first backlog geocoder")
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
    p.add_argument("--pc", action="store_true",
                   help="also index, ingest and link Planning Commission agendas and minutes (earliest leads)")
    p.add_argument("--pc-urls", type=Path, help="hand-made list of PC PDF URLs, merged into the index")
    p.add_argument("--clerk", action="store_true",
                   help="also read the City Clerk's Summary of Council Actions -- the official Council outcomes")
    p.add_argument("--clerk-years", default=None, help="years for --clerk, e.g. 2021-2026 (default 2021-this year)")
    p.add_argument("--constraints", action="store_true",
                   help="also load the site-constraint layers and board histories and measure lead parcels")
    p.add_argument("--filings", action="store_true",
                   help="also load applications as filed (the city's filing layer) -- the earliest leads")
    args = p.parse_args(argv)
    if args.parcels_file and args.parcels_count is None:
        p.error("--parcels-file requires --parcels-count")

    stage = "profile" if args.phase == "profile" else args.stage
    run_dir = args.out_root / f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{args.phase}_{stage}"
    manifest = {"phase": args.phase, "stage": stage, "synthetic": args.synthetic, "seed": args.seed,
                "run_dir": str(run_dir), "data_dir": os.environ.get("DATA_DIR", str(BASE)),
                "council_export": str(args.council_export), "steps": [], "order_executed": []}
    r = Runner(run_dir, manifest)
    lu = ["--config", args.land_use_config]
    stage_flag = ["--provisional"] if stage == "provisional" else []
    synth = ["--synthetic"] if args.synthetic else []
    try:
        r.step("migrate", "migrate_db.py", [])
        if args.phase == "profile" or args.refresh_permits:
            since = ["--since", args.since] if args.since else []
            r.step("permit_ingest", "run_ingest.py",
                   ["--locality", "virginia_beach", *since, *(["--no-geocode"] if args.batch_geocode else [])])
            manifest["backfill_run_id"] = _db_state(lambda c: c.execute(
                "SELECT id FROM ingestion_runs WHERE locality = 'virginia_beach' AND status = 'success' "
                "ORDER BY id DESC LIMIT 1").fetchone()[0])
            if args.since:
                # exit 3 = gaps remain after top-up: recorded as a gap, never as success
                r.step("verify_backfill", "verify_permit_backfill.py",
                       ["--since", args.since, "--run-id", manifest["backfill_run_id"]], gap_codes={3})
            else:
                r.skip("verify_backfill", "no --since given: the backfill window is unknown")
            if args.batch_geocode:  # after verification, so topped-up permits are geocoded too
                r.step("permit_geocode_batch", "geocode_backlog.py", ["--locality", "virginia_beach"])
        if args.phase == "profile":
            r.step("profile_permit_fields", "profile_vb_fields.py", [])
            r.step("profile_council_export", "profile_land_use_export.py", ["--file", args.council_export, *lu])
            if args.parcels_file:
                r.skip("parcel_layer_inspect", "--parcels-file given; use load_parcels.py --inspect for the count")
            else:
                r.step("parcel_layer_inspect", "load_parcels.py", ["--inspect"])
            manifest["db"] = _db_hash()
            r.save()
            print(f"\nProfile done: {run_dir}. Fill config/flood_report.toml and config/land_use.toml from these "
                  "logs (backfill_run_id = the verified run), then run --phase full --stage provisional.")
            return 0

        if stage == "final":
            why = _db_state(_stage_check)
            if why:
                r.skip("stage_check", f"final refused: {why}")
                raise StepFailed("stage_check")
        if args.phase == "full":
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
            if args.filings:
                r.step("load_filings", "load_filings.py", [])  # after the parcel load: filings need its parcels
            else:
                r.skip("load_filings", "applications as filed not requested (--filings)")
            r.step("parcel_flood", "tag_parcel_flood.py", [])
            if args.constraints:  # exit 3 = a layer could not be read completely: a gap, never success
                r.step("constraint_layers", "load_constraints.py", ["--all"], gap_codes={3})
                r.step("board_actions", "load_board_actions.py", ["--all"], gap_codes={3})
            else:
                r.skip("constraint_layers", "site constraints not requested (--constraints)")
            r.step("tag_decision_points", "tag_flood_zones.py", ["--decisions"])
            r.step("link", "link_decisions_permits.py", [*lu, "--reports-dir", run_dir])
            if args.pc:
                # exit 3 = index unreachable / documents failed: recorded as a gap, never as success
                r.step("pc_index", "pc_index.py", ["--out", run_dir / "pc_index.json",
                                                   *(["--urls", args.pc_urls] if args.pc_urls else [])], gap_codes={3})
                r.step("pc_ingest", "ingest_pc_documents.py",
                       ["--index", run_dir / "pc_index.json", "--reports-dir", run_dir], gap_codes={3})
                r.step("pc_link", "link_pc_council.py", [])
            else:
                r.skip("pc_hearings", "Planning Commission hearings not requested (--pc)")
            if args.constraints:  # after filings, PC items and board actions: their parcels are measured
                r.step("tag_parcel_constraints", "tag_parcel_constraints.py", [])
            if args.clerk:  # after the PC items: Council outcomes are linked to them
                r.step("clerk_summaries", "ingest_clerk_summaries.py",
                       ["--reports-dir", run_dir, *(["--years", args.clerk_years] if args.clerk_years else [])],
                       gap_codes={3})
            else:
                r.skip("clerk_summaries", "Clerk's Summary of Council Actions not requested (--clerk)")

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
            if stage == "final":  # reviews may have been imported just now: re-check before drawing anything
                why = _db_state(_stage_check)
                if why:
                    r.skip("stage_check_after_reviews", f"final refused: {why}")
                    raise StepFailed("stage_check_after_reviews")

        r.step("record_source_dates", "record_source_dates.py", ["--council-export", args.council_export])
        r.step("flood_report", "flood_report.py", ["--config", args.flood_config, "--out", run_dir],
               skip_if=("cohort not configured",))
        r.step("land_use_report", "land_use_report.py", [*lu, "--out", run_dir, *stage_flag])
        r.step("parcel_report", "parcel_report.py", ["--out", run_dir, "--config", args.flood_config])
        r.step("unmatched_reasons", "unmatched_reasons.py", ["--out", run_dir])
        if args.pc:
            r.step("pc_report", "pc_report.py", ["--out", run_dir, *stage_flag])
        if args.filings:
            r.step("filing_report", "filing_report.py", ["--out", run_dir])
        if args.clerk:
            r.step("clerk_report", "clerk_report.py", ["--out", run_dir])
        if args.phase == "report":
            r.skip("accuracy_sample", "report phase: the existing sample and its recorded population are kept")
        elif stage == "provisional":
            r.skip("accuracy_sample", "provisional stage: samples are drawn only from the reviewed (final) snapshot")
        else:
            r.step("accuracy_sample", "accuracy_sample.py", ["--seed", args.seed, "--out", run_dir, *synth])
        r.step("quality_report", "quality_report.py",
               ["--out", run_dir, "--manifest", run_dir / "run_manifest.json", *synth, *stage_flag])
    except StepFailed as e:
        manifest["db"] = _db_hash()
        r.save()
        print(f"\nStopped at step {e}; see {run_dir / 'run_manifest.json'} and its log.", file=sys.stderr)
        return 1
    sys.path.insert(0, str(BASE))
    import db as db_module  # noqa: E402 - the run's own DB (DATA_DIR), read-only
    from ingest import quality  # noqa: E402
    with db_module.db_readonly() as conn:
        manifest["source_dates"] = quality.data_as_of(conn)
        manifest["backfill_checks"] = [dict(x) for x in conn.execute(
            "SELECT backfill_run_id, where_clause, source_count, db_ids, truly_missing, dup_permit_number, "
            "no_permit_number, extra_in_db, null_date_source, verified, per_year_json, checked_at "
            "FROM permit_backfill_checks ORDER BY check_id")]
    manifest["db"] = _db_hash()
    r.save()
    nxt = {"provisional": "fill land_use_review.csv (outcomes + evidence), then --phase full --stage final "
                          "--reviews-dir <folder>",
           "final": "check accuracy_sample.csv (independent search for each lead row), then "
                    "scripts/accuracy_tally.py --apply-links, then --phase report --stage final"}
    print(f"\nDone ({stage}): {run_dir}. Next: {nxt[stage]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
