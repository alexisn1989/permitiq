"""Ingest Virginia Beach Council land-use decisions from a VoteIQ export
(tools/export_vb_council_votes.py) into land_use_decisions.

* Refuses while config/land_use.toml is unconfigured, and refuses if the
  export contains a vote or result string the config does not map (the
  config is incomplete; nothing is guessed).
* One decision per (meeting_id, resolution_id). Member rows that disagree on
  title / result / meeting_date are quarantined: those fields stay NULL and
  every variant is stored in land_use_decision_conflicts.
* Never writes an approved outcome. outcome_evidence is at most proposed_*;
  scripts/review_land_use.py is the only path to "approved".
* Idempotent upsert. Address rows keep their geocode when the parsed address
  is unchanged; addresses entered by review are never touched here.
* Writes reports/land_use_review.csv: every decision still needing an outcome
  review, plus quarantined items with all variants -- the template for
  scripts/review_land_use.py --outcomes.

Usage:
  python scripts/ingest_land_use_decisions.py --file vb_council_votes.jsonl --dry-run
  python scripts/ingest_land_use_decisions.py --file vb_council_votes.jsonl
  python scripts/ingest_land_use_decisions.py --geocode [--geocode-limit 200]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import geocode, land_use  # noqa: E402
from ingest.flood_stats import ConfigError  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
RUN_LOCALITY = "voteiq_land_use"
geocoder = geocode.geocode_address  # indirection so tests can stub the network

REVIEW_COLUMNS = ("decision_key", "meeting_date", "land_use_label", "outcome_evidence", "evidence_pattern",
                  "result_raw", "yes_count", "no_count", "absent_count", "abstain_count", "title", "conflicts",
                  "previous_verified_outcome",
                  # filled in by the reviewer, then imported with review_land_use.py --outcomes
                  "verified_outcome", "evidence", "reviewer", "conflict_resolution", "resolved_title")


def counts(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {"decisions": q("SELECT COUNT(*) FROM land_use_decisions"),
            "quarantined": q("SELECT COUNT(DISTINCT decision_key) FROM land_use_decision_conflicts"),
            "addresses": q("SELECT COUNT(*) FROM land_use_decision_addresses"),
            "stale_reviews": q("SELECT COUNT(*) FROM land_use_decision_status WHERE status = 'outcome_review_stale'")}


def upsert(conn, d: land_use.Decision, sha: str, ts: str) -> None:
    conn.execute(
        """INSERT INTO land_use_decisions (decision_key, meeting_id, resolution_id, meeting_date, title,
               title_truncated, result_raw, yes_count, no_count, absent_count, abstain_count, member_rows,
               outcome_evidence, evidence_pattern, land_use_label, matched_pattern, source_file_sha256,
               ingested_at, updated_at, facts_fingerprint)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(decision_key) DO UPDATE SET
               meeting_date = excluded.meeting_date, facts_fingerprint = excluded.facts_fingerprint, title = excluded.title,
               title_truncated = excluded.title_truncated, result_raw = excluded.result_raw,
               yes_count = excluded.yes_count, no_count = excluded.no_count,
               absent_count = excluded.absent_count, abstain_count = excluded.abstain_count,
               member_rows = excluded.member_rows, outcome_evidence = excluded.outcome_evidence,
               evidence_pattern = excluded.evidence_pattern, land_use_label = excluded.land_use_label,
               matched_pattern = excluded.matched_pattern, source_file_sha256 = excluded.source_file_sha256,
               updated_at = excluded.updated_at""",
        (d.decision_key, d.meeting_id, d.resolution_id, d.meeting_date, d.title, int(d.title_truncated),
         d.result_raw, d.counts["yes"], d.counts["no"], d.counts["absent"], d.counts["abstain"], d.member_rows,
         d.outcome_evidence, d.evidence_pattern, d.land_use_label, d.matched_pattern, sha, ts, ts,
         db_module.facts_fingerprint(d.title, d.result_raw, d.meeting_date, d.conflicts)))
    conn.execute("DELETE FROM land_use_decision_conflicts WHERE decision_key = ?", (d.decision_key,))
    conn.executemany(
        "INSERT INTO land_use_decision_conflicts (decision_key, field, variants_json, detected_at) VALUES (?, ?, ?, ?)",
        [(d.decision_key, f, json.dumps(v, sort_keys=True), ts) for f, v in d.conflicts.items()])
    land_use.sync_addresses(conn, d.decision_key, d.addresses, "title")


def write_review_csv(conn, path: Path) -> int:
    rows = conn.execute(
        """SELECT d.*, (SELECT json_group_object(x.field, json(x.variants_json)) FROM land_use_decision_conflicts x
                        WHERE x.decision_key = d.decision_key) AS conflicts,
                  s.reviewed_outcome_any AS previous_verified_outcome
           FROM land_use_decisions d JOIN land_use_decision_status s ON s.decision_key = d.decision_key
           WHERE s.verified_outcome IS NULL ORDER BY d.meeting_date, d.decision_key""").fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(REVIEW_COLUMNS)
        for r in rows:
            w.writerow([r[c] if c in r.keys() else "" for c in REVIEW_COLUMNS[:13]] + [""] * 5)
    return len(rows)


def geocode_addresses(conn, limit: int | None) -> Counter:
    """Geocode street addresses not yet attempted, skipping decisions that
    already have a linked permit with coordinates. Reuses geocode_cache."""
    todo = conn.execute(
        """SELECT a.decision_key, a.seq, a.house_from, a.street_norm FROM land_use_decision_addresses a
           WHERE a.street_norm IS NOT NULL AND a.geocode_status IS NULL
             AND NOT EXISTS (SELECT 1 FROM decision_permit_links_effective k JOIN permits p ON p.id = k.permit_id
                             WHERE k.decision_key = a.decision_key AND p.lat IS NOT NULL)
           ORDER BY a.decision_key, a.seq""").fetchall()
    out: Counter = Counter()
    for r in todo[:limit] if limit else todo:
        address = f"{r['house_from']} {r['street_norm']}, Virginia Beach, VA"
        key = geocode._normalize_address(address)
        cached = conn.execute("SELECT * FROM geocode_cache WHERE normalized_address = ?", (key,)).fetchone()
        if cached is not None:
            res = geocode._result_from_cache_row(cached)
            out["cache_hit"] += 1
        else:
            res = geocoder(address, locality="virginia_beach")
            geocode._persist_geocode_result(conn, key, res)
        out[res.status] += 1
        conn.execute("UPDATE land_use_decision_addresses SET lat = ?, lng = ?, geocode_status = ?, geocoded_at = ? "
                     "WHERE decision_key = ? AND seq = ?",
                     (res.lat if res.matched else None, res.lng if res.matched else None, res.status,
                      db_module.now_iso(), r["decision_key"], r["seq"]))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ingest VB Council land-use decisions from a VoteIQ export")
    p.add_argument("--file", type=Path)
    p.add_argument("--config", type=Path, default=BASE / "config" / "land_use.toml")
    p.add_argument("--reports-dir", type=Path, default=BASE / "reports")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--geocode", action="store_true", help="geocode extracted street addresses (network)")
    p.add_argument("--geocode-limit", type=int)
    args = p.parse_args(argv)
    if not args.file and not args.geocode:
        p.error("--file and/or --geocode is required")

    if args.file:
        try:
            cfg = land_use.load_config(args.config)
            land_use.require_ingest_config(cfg, args.config)
            meta, rows = land_use.read_export(args.file)
        except (ConfigError, OSError, ValueError) as e:
            print(f"[FATAL] {e}", file=sys.stderr)
            return 1
        decisions, stats, unmapped = land_use.aggregate(rows, cfg)
        if unmapped["vote"] or unmapped["result"]:
            print("[FATAL] export has values config/land_use.toml does not map -- add them from the profile, "
                  f"nothing written: votes {dict(unmapped['vote'])}, results {dict(unmapped['result'])}", file=sys.stderr)
            return 1
        print(f"{args.file}: {stats['member_rows']} member rows -> {stats['items']} items; "
              f"land use {stats['land_use']}, not land use {stats['not_land_use']}")
        print(f"  quarantined (member rows disagree): {stats['quarantined']}; truncated titles: "
              f"{stats['truncated_titles']}; unparseable meeting_date: {stats['unparseable_meeting_date']}")
        print(f"  with an extracted address or GPIN: {stats['with_address']} of {stats['land_use']}")
        print(f"  outcome_evidence: {dict(Counter(d.outcome_evidence for d in decisions))} "
              "(none of these is an approval; review decides)")
        if args.dry_run:
            print("--dry-run: nothing written")
            return 0
        sha, ts = land_use.file_sha256(args.file), db_module.now_iso()
        with db_module.db() as conn:
            before = counts(conn)
            for d in decisions:
                upsert(conn, d, sha, ts)
            after = counts(conn)
            conn.execute(
                """INSERT INTO ingestion_runs (locality, started_at, completed_at, status, fetched_count,
                       inserted_count, since_date, error_message) VALUES (?, ?, ?, 'success', ?, ?, NULL, ?)""",
                (RUN_LOCALITY, ts, db_module.now_iso(), stats["member_rows"], len(decisions), f"sha256={sha[:16]}"))
            n_review = write_review_csv(conn, args.reports_dir / "land_use_review.csv")
        print(f"  before: {before}\n  after:  {after}")
        made_stale = after["stale_reviews"] - before["stale_reviews"]
        if made_stale > 0:
            print(f"  {made_stale} outcome review(s) made stale: the reviewed title/result/date changed -- "
                  "re-review them (listed in land_use_review.csv with the previous outcome)")
        print(f"  {n_review} decision(s) await outcome review -> {args.reports_dir / 'land_use_review.csv'}")

    if args.geocode and not args.dry_run:
        with db_module.db() as conn:
            g = geocode_addresses(conn, args.geocode_limit)
        print(f"geocode: {dict(g)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
