"""Download, parse and store Planning Commission agendas and minutes.

Each PDF is saved under DATA_DIR/pc_docs/ by its SHA-256 (untrusted data in its
own folder) and its text is extracted in an isolated interpreter. A document
whose URL, hash and parser version are unchanged is skipped. Items that parse
with warnings, and documents that fail, go to pc_parse_review.csv -- nothing
is dropped silently.

Usage:
  python scripts/ingest_pc_documents.py --index reports/run_x/pc_index.json --reports-dir reports/run_x
  python scripts/ingest_pc_documents.py --file local.pdf --url <its URL> [--kind agenda|minutes]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import db as db_module  # noqa: E402
from ingest import pc_agenda, pc_hearings  # noqa: E402

TIMEOUT = 120
pdf_text = pc_agenda.pdf_text  # indirection so tests can stub extraction


def download(url: str) -> bytes:  # indirection so tests can stub the network
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


def docs_dir() -> Path:
    d = Path(db_module.DB_PATH).parent / "pc_docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ingest_one(conn, url: str, kind: str, data: bytes) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    path = docs_dir() / f"{sha}.pdf"
    if not path.exists():
        path.write_bytes(data)
    try:
        text = pdf_text(path)
    except ValueError as e:
        text = ""
        print(f"  [warn] {url}: {e}", file=sys.stderr)
    return pc_hearings.store_document(conn, kind, url, sha, len(data), text)


def write_review(conn, out: Path) -> int:
    rows = conn.execute(
        "SELECT 'item' AS what, i.item_key AS key, d.url, i.warnings_json AS warnings FROM pc_items i "
        "JOIN pc_documents d ON d.doc_id = i.doc_id WHERE i.warnings_json != '[]' "
        "UNION ALL SELECT 'document', d.kind || ':' || COALESCE(d.meeting_date, '?'), d.url, d.warnings_json "
        "FROM pc_documents d WHERE d.parse_status != 'ok' ORDER BY 2").fetchall()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["what", "key", "url", "warnings"])  # no raw blocks or names: reviewers open the PDF
        for r in rows:
            w.writerow([r["what"], r["key"], r["url"], "; ".join(json.loads(r["warnings"]))])
    return len(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ingest Planning Commission agendas and minutes")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--index", type=Path, help="pc_index.json from scripts/pc_index.py")
    g.add_argument("--file", type=Path, help="one local PDF (needs --url)")
    p.add_argument("--url", help="the URL the --file came from")
    p.add_argument("--kind", choices=("agenda", "minutes"))
    p.add_argument("--reports-dir", type=Path, default=Path(__file__).resolve().parent.parent / "reports")
    args = p.parse_args(argv)
    if args.file and not args.url:
        p.error("--file requires --url")
    if args.index:
        idx = json.loads(args.index.read_text(encoding="utf-8"))
        todo = [(d["url"], d["kind"], None) for d in idx.get("documents", [])]
    else:
        kind = args.kind or pc_hearings.kind_from_url(args.url)
        if not kind:
            p.error("cannot tell agenda from minutes from the URL: pass --kind")
        todo = [(args.url, kind, args.file.read_bytes())]
    summary = {"documents": 0, "unchanged": 0, "failed": 0, "download_errors": 0, "items": 0}
    try:
        with db_module.db() as conn:
            for url, kind, data in todo:
                if data is None:
                    try:
                        data = download(url)
                    except (requests.RequestException, OSError) as e:
                        summary["download_errors"] += 1
                        print(f"  [error] {url}: {type(e).__name__}", file=sys.stderr)
                        continue
                r = ingest_one(conn, url, kind, data)
                conn.commit()
                summary["documents"] += 1
                summary["unchanged"] += int(bool(r.get("unchanged")))
                summary["failed"] += int(r.get("parse_status") == "failed")
                summary["items"] += r.get("items", 0)
            n_review = write_review(conn, args.reports_dir / "pc_parse_review.csv")
    except db_module.DatabaseNotReady as e:
        print(f"[FATAL] database not ready ({e}); run scripts/migrate_db.py", file=sys.stderr)
        return 1
    print(f"pc documents: {summary}; {n_review} row(s) for review -> {args.reports_dir / 'pc_parse_review.csv'}")
    # failed documents and download errors are gaps, recorded and reported, not crashes
    return 3 if summary["failed"] or summary["download_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
