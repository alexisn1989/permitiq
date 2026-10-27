"""Find every Planning Commission agenda and minutes PDF the city links to.

Reads the city's Planning Commission page(s) and collects links to agenda and
minutes PDFs on the city's S3 bucket; one level of same-site links whose path
mentions the Planning Commission is followed (year / archive pages). Writes
pc_index.json and records the check in pc_index_checks, so coverage can be
called verified only after a successful read.

Exit codes: 0 index read; 3 index unreachable or empty (recorded as a gap,
not an error -- the hand-made --urls list, if given, is still written); 1 error.

Usage:
  python scripts/pc_index.py --out reports/run_x/pc_index.json
  python scripts/pc_index.py --out pc_index.json --urls my_list.txt     # one URL per line
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import db as db_module  # noqa: E402
from ingest import pc_hearings  # noqa: E402

DEFAULT_PAGES = ["https://planning.virginiabeach.gov/boards-commissions/planning-commission"]
PDF_RE = re.compile(r"/Planning-Commission/(?:agendas|Minutes)/.+\.pdf$", re.IGNORECASE)
TIMEOUT = 60


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def get_html(url: str) -> str:  # indirection so tests can stub the network
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def links(url: str, html: str) -> list[str]:
    p = _Links()
    p.feed(html)
    return [urljoin(url, h) for h in p.hrefs]


def crawl(pages: list[str]) -> tuple[dict[str, str], list[str]]:
    found, errors, seen = {}, [], set()
    queue = [(p, 0) for p in pages]
    while queue:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            html = get_html(url)
        except (requests.RequestException, OSError) as e:
            errors.append(f"{url}: {type(e).__name__}: {str(e)[:200]}")
            continue
        for href in links(url, html):
            if PDF_RE.search(urlparse(href).path):
                kind = pc_hearings.kind_from_url(href)
                if kind:
                    found[href] = kind
            elif depth == 0 and urlparse(href).netloc == urlparse(url).netloc and \
                    "planning-commission" in urlparse(href).path.lower():
                queue.append((href.split("#")[0], depth + 1))
    return found, errors


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Index Planning Commission agenda and minutes PDFs")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--page", action="append", help="index page(s) to read (default: the city's PC page)")
    p.add_argument("--urls", type=Path, help="a hand-made list of PDF URLs, one per line (merged in)")
    args = p.parse_args(argv)
    pages = args.page or DEFAULT_PAGES
    found, errors = crawl(pages)
    status = "ok" if found and not errors else ("blocked" if not found else "error")
    manual = {}
    if args.urls:
        for ln in args.urls.read_text(encoding="utf-8").splitlines():
            u = ln.strip()
            if u and not u.startswith("#"):
                kind = pc_hearings.kind_from_url(u)
                if not kind:
                    print(f"[FATAL] cannot tell agenda from minutes: {u}", file=sys.stderr)
                    return 1
                manual[u] = kind
    entries = [{"url": u, "kind": k, "source": "index"} for u, k in sorted(found.items())]
    entries += [{"url": u, "kind": k, "source": "manual"} for u, k in sorted(manual.items()) if u not in found]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"pages": pages, "status": status, "errors": errors, "documents": entries},
                                   indent=1), encoding="utf-8")
    with db_module.db() as conn:
        conn.execute("INSERT INTO pc_index_checks (index_url, status, links_found, detail, checked_at) "
                     "VALUES (?, ?, ?, ?, ?)", (" ".join(pages), status, len(found),
                                                "; ".join(errors)[:1000] or None, db_module.now_iso()))
    print(f"index {status}: {len(found)} PDF link(s) from {len(pages)} page(s); {len(manual)} manual; "
          f"wrote {args.out}" + (f"\n  errors: {errors[:3]}" if errors else ""))
    return 0 if status == "ok" else 3


if __name__ == "__main__":
    sys.exit(main())
