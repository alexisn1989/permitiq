"""Build the reviewed hand-off folder for a real run: the minimum needed to
verify results outside this container, without shipping the whole database.

handoff/
  run/                   the run folder's manifest, logs and reports (copied)
  db_inventory.json      permits.db SHA-256, size, and per table: row count and
                         column names -- review this before deciding whether
                         the full database needs to travel at all
  extract/*.csv          bounded verification extract, NO raw_json:
                         decisions, decision_addresses, links (effective),
                         linked_permits (+ parcel match), decision_parcels,
                         source_dates, backfill_checks, review_audit and
                         review_imports (review history and its provenance)
The full permits.db is never copied here. If it is needed, upload it
privately and compare its SHA-256 with db_inventory.json.

Usage:
  python scripts/handoff_extract.py --run reports/run_<ts>_full_final --out handoff/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402

EXTRACTS = {
    "decisions": """SELECT d.decision_key, d.meeting_id, d.meeting_date, d.land_use_label, d.title, d.title_truncated,
        d.result_raw, d.yes_count, d.no_count, d.absent_count, d.abstain_count, d.outcome_evidence, s.status,
        s.verified_outcome, r.evidence AS review_evidence, r.reviewer
        FROM land_use_decisions d JOIN land_use_decision_status s ON s.decision_key = d.decision_key
        LEFT JOIN decision_outcome_review r ON r.decision_key = d.decision_key ORDER BY d.meeting_date, d.decision_key""",
    "decision_addresses": """SELECT decision_key, seq, source, raw_text, house_from, house_to, street_norm, gpin10,
        lat, lng, geocode_status FROM land_use_decision_addresses ORDER BY 1, 2""",
    "links": """SELECT k.decision_key, k.permit_id, k.match_tier, k.matched_on, k.link_state, k.is_new_construction,
        k.applied_date, k.meeting_date, lr.verdict AS review_verdict, lr.reviewer FROM decision_permit_links_effective k
        LEFT JOIN decision_link_review lr ON lr.decision_key = k.decision_key AND lr.permit_id = k.permit_id
        ORDER BY 1, 2""",
    "linked_permits": """SELECT p.id, p.source_permit_id, p.address, p.permit_type, p.status, p.applied_date,
        p.issued_date, p.lat, p.lng, e.effective_class AS point_flood_class, pp.parcel_state, pp.display_gpin,
        pp.disputed FROM permits p JOIN permit_flood_effective e ON e.permit_id = p.id
        JOIN permit_parcel_effective pp ON pp.permit_id = p.id
        WHERE p.id IN (SELECT permit_id FROM decision_permit_links) ORDER BY p.id""",
    "decision_parcels": """SELECT decision_key, seq, parcel_state, display_gpin, alt_gpin, dispute_reason,
        review_verdict, set_verdict, link_gpin, gpin_trusted FROM decision_parcel_effective ORDER BY 1, 2""",
    "source_dates": "SELECT source, as_of, detail_json, recorded_at FROM source_dates ORDER BY 1",
    "backfill_checks": """SELECT backfill_run_id, where_clause, source_count, source_ids, db_ids, truly_missing,
        dup_permit_number, no_permit_number, extra_in_db, dup_objectids_db, null_date_source, topped_up,
        per_year_json, verified, checked_at FROM permit_backfill_checks ORDER BY check_id""",
    # no raw blocks (they carry names of private individuals); the PDFs are public at their URLs
    "pc_documents": """SELECT doc_id, kind, url, meeting_date, sha256, bytes, fetched_at, parser_version, parse_status,
        n_items, warnings_json FROM pc_documents ORDER BY meeting_date, kind""",
    "pc_items": """SELECT i.item_key, i.meeting_date, i.item_label, i.request, i.addresses, i.gpins_json,
        i.council_district_text, i.accela_json, i.staff_planner, i.city_initiated, s.status, s.status_source,
        s.n_council_candidates, i.warnings_json FROM pc_items i JOIN pc_item_status s ON s.item_key = i.item_key
        ORDER BY i.meeting_date, i.item_key""",
    "pc_council_candidates": "SELECT * FROM pc_council_candidates ORDER BY item_key, decision_key",
    # no applicant or project names (they include private individuals); the layer is public at its URL
    "filings": """SELECT f.pln_id, f.date_sub, f.pln_type, f.gpins_json, s.pc_status, s.hearing_date,
        s.n_council_candidates, s.permits_after_filing FROM filings f JOIN filing_status s ON s.pln_id = f.pln_id
        ORDER BY f.date_sub""",
    "review_audit": """SELECT audit_id, import_id, review_table, subject_json, action, old_json, new_json,
        review_author, recorded_at FROM review_audit ORDER BY audit_id""",
    "review_imports": """SELECT import_id, kind, source_file, source_sha256, derived_file, derived_sha256,
        acting_user, started_at FROM review_imports ORDER BY import_id""",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build the reviewed hand-off folder")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    if not (args.run / "run_manifest.json").is_file():
        print(f"[FATAL] {args.run} is not a run folder (no run_manifest.json)", file=sys.stderr)
        return 1
    with db_module.db() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # so the file hash reflects every committed row
    path = Path(db_module.DB_PATH)
    try:
        with db_module.db_readonly() as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            inventory = {"db_path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path),
                         "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
                         "tables": {t: {"rows": conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0],
                                        "columns": [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')]}
                                    for t in tables}}
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "extract").mkdir(exist_ok=True)
            counts = {}
            for name, sql in EXTRACTS.items():
                cur = conn.execute(sql)
                cols = [d[0] for d in cur.description]
                assert "raw_json" not in cols
                with (args.out / "extract" / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(cols)
                    n = 0
                    for row in cur:
                        w.writerow(row)
                        n += 1
                counts[name] = n
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e})", file=sys.stderr)
        return 1
    inventory["extract_rows"] = counts
    (args.out / "db_inventory.json").write_text(json.dumps(inventory, indent=1), encoding="utf-8")
    dest = args.out / "run"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(args.run, dest)
    print(f"hand-off -> {args.out}: run folder, db_inventory.json (sha256 {inventory['sha256'][:16]}..., "
          f"{inventory['bytes']} bytes), extract {counts}")
    print("permits.db itself is NOT included; review db_inventory.json before deciding whether it must travel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
