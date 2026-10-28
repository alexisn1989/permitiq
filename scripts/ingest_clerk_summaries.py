"""Read the City Clerk's Summary of Council Actions (Virginia Beach) for the
given years, store each meeting's items, and link land-use items to the
Planning Commission items they decide.

Source: the Clerk's Laserfiche archive (edocs.vbgov.com), folder SUMMARY
ACTIONS / SUMMARIES <year>; each page's OCR text layer is read through the
archive's own JSON service (ingest/edocs.py). Page text is cached under
DATA_DIR/clerk_docs/<entry id>.json (untrusted data in its own folder) and
re-read when the archive's modified time changes.

Per year the archive's listing is the expected set: a year is 'ok' only
when every listed summary was stored; otherwise 'incomplete' and this exits
3. Items whose motion, tally, date or address cannot be read go to
clerk_parse_review.csv (key, link, page, warnings; no names or raw text).

Usage:
  python scripts/ingest_clerk_summaries.py --years 2021-2026 --reports-dir reports/run_x
  python scripts/ingest_clerk_summaries.py --years 2026 --link-only
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import council_actions, edocs  # noqa: E402

make_client = edocs.Client  # indirection so tests can stub the archive


def cache_dir() -> Path:
    d = Path(db_module.DB_PATH).parent / "clerk_docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def parse_years(text: str) -> list[int]:
    if "-" in text:
        a, b = (int(x) for x in text.split("-", 1))
        years = list(range(a, b + 1))
    else:
        years = [int(x) for x in text.split(",")]
    if not years or any(y < 1981 or y > date.today().year + 1 for y in years):
        raise ValueError(f"bad --years {text!r}")
    return years


def pages_for(client, entry: dict, cache: Path) -> list[str]:
    path = cache / f"{entry['entry_id']}.json"
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("modified") == entry.get("modified") and len(cached.get("pages", [])) == entry["page_count"]:
                return cached["pages"]
        except (OSError, ValueError):
            pass
    pages = [client.page_text(entry["entry_id"], i) for i in range(1, (entry["page_count"] or 0) + 1)]
    path.write_text(json.dumps({"entry_id": entry["entry_id"], "name": entry["name"], "modified": entry.get("modified"),
                                "pages": pages}), encoding="utf-8")
    return pages


def write_review(conn, out: Path) -> int:
    rows = conn.execute(
        "SELECT 'item' AS what, i.item_key AS key, i.entry_id, i.page, i.warnings_json AS warnings "
        "FROM clerk_items i WHERE i.is_land_use = 1 AND i.warnings_json != '[]' "
        "UNION ALL SELECT 'document', d.name, d.entry_id, NULL, d.warnings_json FROM clerk_documents d "
        "WHERE d.parse_status = 'failed' OR d.warnings_json != '[]' ORDER BY 2").fetchall()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["what", "key", "url", "page", "warnings"])  # reviewers open the Clerk's page
        for r in rows:
            w.writerow([r["what"], r["key"], edocs.doc_url(r["entry_id"]), r["page"] or "",
                        "; ".join(json.loads(r["warnings"]))])
    return len(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ingest the City Clerk's Summary of Council Actions")
    p.add_argument("--years", default=f"2021-{date.today().year}")
    p.add_argument("--link-only", action="store_true", help="re-link stored items; no download")
    p.add_argument("--reports-dir", type=Path, default=Path(__file__).resolve().parent.parent / "reports")
    args = p.parse_args(argv)
    try:
        years = parse_years(args.years)
    except ValueError as e:
        p.error(str(e))
    summary = {"documents": 0, "unchanged": 0, "failed": 0, "errors": 0, "land_use_items": 0}
    incomplete = []
    try:
        with db_module.db() as conn:
            if not args.link_only:
                client, cache = make_client(), cache_dir()
                try:
                    folders = edocs.year_folders(client, years)
                except edocs.EdocsError as e:
                    print(f"[FATAL] Clerk archive not readable: {e}", file=sys.stderr)
                    for y in years:
                        conn.execute("INSERT INTO clerk_index_checks (year, status, detail, checked_at) "
                                     "VALUES (?, 'error', ?, ?)", (y, str(e)[:300], db_module.now_iso()))
                    return 3
                for y in years:
                    fid = folders.get(y)
                    listed = stored = 0
                    detail = None
                    try:
                        if fid is None:
                            raise edocs.EdocsError(f"no 'SUMMARIES {y}' folder in the archive")
                        entries = [e for e in client.folder_listing(fid) if e["page_count"]]
                        listed = len(entries)
                        for e in entries:
                            try:
                                r = council_actions.store_document(conn, e, fid, y, pages_for(client, e, cache))
                            except edocs.EdocsError as ex:
                                summary["errors"] += 1
                                print(f"  [error] {y} {e['name']}: {ex}", file=sys.stderr)
                                continue
                            conn.commit()
                            stored += 1
                            summary["documents"] += 1
                            summary["unchanged"] += int(bool(r.get("unchanged")))
                            summary["failed"] += int(r.get("parse_status") == "failed")
                            summary["land_use_items"] += r.get("land_use", 0)
                    except edocs.EdocsError as ex:
                        detail = str(ex)[:300]
                    status = "ok" if detail is None and listed and stored == listed else (
                        "error" if detail else "incomplete")
                    if status != "ok":
                        incomplete.append(y)
                    conn.execute("INSERT INTO clerk_index_checks (year, folder_id, listed, stored, status, detail, "
                                 "checked_at) VALUES (?,?,?,?,?,?,?)",
                                 (y, fid, listed, stored, status, detail, db_module.now_iso()))
                    conn.commit()
                    print(f"  {y}: {stored}/{listed} summaries stored ({status})")
            links = council_actions.link_pc(conn)
            dlinks = council_actions.link_decisions(conn)
            n_review = write_review(conn, args.reports_dir / "clerk_parse_review.csv")
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    if not args.link_only:
        print(f"clerk summaries: {summary}")
    print(f"links: {links}; Council decisions checked against the Clerk: {dlinks['links']}")
    print(f"{n_review} row(s) for review -> {args.reports_dir / 'clerk_parse_review.csv'}")
    return 3 if incomplete or summary["errors"] or summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
