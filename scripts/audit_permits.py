"""Read-only audit: is there enough high-value permit volume for a daily
commercial-permit digest aimed at trade vendors (dumpster haulers, roofing
subs, fencing/site security)?

Opens permits.db in SQLite read-only mode (mode=ro) -- it cannot write, so
it is safe to point at the real database while ingestion is idle. Reports,
per locality over the last N days:

  1. Weekly permit counts by type, and how many "qualify" (commercial, or
     demolition/structural work, or valuation >= --min-valuation)
  2. Field completeness for qualifying permits
  3. Business vs individual applicant/contractor names (simple heuristic)
  4. Freshness: applied -> issued -> first_seen_at lag, plus ingestion cadence
  5. Source type per locality (API vs scraper) and how fragile it looks

Prints console tables and writes one CSV per section to --out-dir.

"Activity date" for weekly counts is COALESCE(issued_date, applied_date),
matching the API's default ordering. The daily-digest metrics use
issued_date only, because the digest is about permits issued yesterday.

Usage:
  python scripts/audit_permits.py
  python scripts/audit_permits.py --days 90 --min-valuation 50000
  python scripts/audit_permits.py --db /path/to/permits.db --as-of 2026-10-15
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402

DEFAULT_COMMERCIAL_TERMS = ["commercial"]
DEFAULT_STRUCTURAL_TERMS = [
    "demo", "demolition", "demolish", "structural", "new construction", "foundation",
]

# Word-boundary tokens that mark a name as a business. CO is matched only as a
# whole token so "COLLINS" doesn't count.
BUSINESS_TOKENS = [
    "LLC", "L.L.C", "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
    "LTD", "LP", "LLP", "PLLC", "PC", "GROUP", "BUILDERS", "BUILDING", "CONSTRUCTION",
    "CONTRACTING", "CONTRACTORS", "CONTRACTOR", "ROOFING", "HOMES", "DEVELOPMENT",
    "SERVICES", "SERVICE", "ELECTRIC", "ELECTRICAL", "PLUMBING", "MECHANICAL", "HVAC",
    "ENTERPRISES", "PROPERTIES", "ASSOCIATES", "SOLUTIONS", "INDUSTRIES", "PARTNERS",
    "HOLDINGS", "TRUST", "FENCE", "FENCING", "DEMOLITION", "EXCAVATING", "PAVING",
    "SOLAR", "POOLS", "RESTORATION", "REMODELING", "CITY", "COUNTY", "AUTHORITY",
    "CHURCH", "SCHOOL", "UNIVERSITY", "HOSPITAL", "BANK",
]
_BUSINESS_RE = re.compile(
    r"(?<![A-Z0-9])(" + "|".join(re.escape(t) for t in BUSINESS_TOKENS) + r")\.?(?![A-Z0-9])"
)
_LEGAL_SUFFIX_RE = re.compile(r",\s*(LLC|INC|CORP|LTD|LP|LLP)\b")

# raw_json keys whose name suggests a contractor/applicant or a cost field the
# adapters don't map yet -- surfaced so a missing column isn't mistaken for a
# missing source field.
_CANDIDATE_KEY_RE = re.compile(
    r"contract|applicant|owner|company|business|licen|valu|cost|amount|price|fee|sq|square",
    re.IGNORECASE,
)

# What each adapter actually does, from reading ingest/*.py. Keyed by locality
# so the report can join it with ingestion_runs history.
SOURCE_PROFILES = {
    "norfolk": {
        "module": "ingest.norfolk",
        "access_method": "Open-data API (Socrata SODA, dataset fahm-yuh4)",
        "fetch": "GET JSON, $limit/$offset paging (1000/page), $where application_date >= since",
        "kind": "api",
        "fragility": (
            "Low: documented Socrata API, stable dataset ID. Batch-level schema-drift "
            "check on REQUIRED_FIELDS. Unauthenticated (no app token) -> subject to "
            "shared throttling."
        ),
    },
    "virginia_beach": {
        "module": "ingest.virginia_beach",
        "access_method": "Open-data API (ArcGIS REST FeatureServer query, hosted *_view layer)",
        "fetch": "GET JSON, resultOffset paging (2000/page), where ApplicationDate >= 'YYYY/MM/DD' (string compare)",
        "kind": "api",
        "fragility": (
            "Low-medium: documented ArcGIS REST, but a hosted *_view layer can be renamed/"
            "republished without notice, and every field (dates included) is a string. "
            "Schema-drift check on REQUIRED_FIELDS."
        ),
    },
}


# ---------------------------------------------------------------- helpers


def parse_terms(text: str) -> list[str]:
    return [t.strip().lower() for t in text.split(",") if t.strip()]


def parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def classify_name(name: str | None) -> str | None:
    """'business' if the name carries a business/legal token, 'individual'
    otherwise, None if blank. Ambiguous names fall to 'individual' on
    purpose: the digest must only show names we're confident are businesses."""
    if name is None or not str(name).strip():
        return None
    upper = str(name).upper()
    if _BUSINESS_RE.search(upper) or _LEGAL_SUFFIX_RE.search(upper):
        return "business"
    return "individual"


def _haystack(row: dict) -> str:
    parts = [row.get("permit_type"), row.get("description")]
    raw = row.get("raw") or {}
    # Norfolk carries use_class/use_type/work_type/structure; VB carries
    # WorkType/ConstructionType. Include whatever is there.
    for key in ("use_class", "use_type", "work_type", "structure", "WorkType", "ConstructionType"):
        if raw.get(key):
            parts.append(str(raw[key]))
    return " ".join(str(p) for p in parts if p).lower()


def _has_term(text: str, terms: list[str]) -> bool:
    return any(re.search(r"(?<![a-z])" + re.escape(t), text) for t in terms)


def qualify(row: dict, commercial_terms, structural_terms, min_valuation) -> list[str]:
    """Return the reasons this permit is high-value ([] if it isn't)."""
    text = _haystack(row)
    reasons = []
    if _has_term(text, commercial_terms):
        reasons.append("commercial")
    if _has_term(text, structural_terms):
        reasons.append("demo_structural")
    val = row.get("valuation")
    if val is not None and min_valuation is not None and val >= min_valuation:
        reasons.append("valuation")
    return reasons


def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def summarize_lags(values: list[int]) -> dict:
    if not values:
        return {"n": 0, "median": None, "p90": None, "min": None, "max": None}
    s = sorted(values)
    p90 = s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]
    return {"n": len(s), "median": statistics.median(s), "p90": p90, "min": s[0], "max": s[-1]}


def print_table(title: str, headers: list[str], rows: list[list]) -> None:
    print(f"\n{title}")
    if not rows:
        print("  (no rows)")
        return
    cells = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    line = "  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  " + "  ".join("-" * w for w in widths))
    for r in cells:
        print("  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)))


def write_csv(path: Path, headers: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        w.writerows(rows)


# ---------------------------------------------------------------- data access


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(conn: sqlite3.Connection, start: date, end: date) -> list[dict]:
    """Permits whose applied OR issued date falls in [start, end]."""
    sql = """
        SELECT id, locality, source_permit_id, permit_type, status, status_bucket,
               applied_date, issued_date, valuation, description, contractor,
               applicant, lat, lng, geocode_status, first_seen_at, raw_json
        FROM permits
        WHERE (applied_date BETWEEN ? AND ?) OR (issued_date BETWEEN ? AND ?)
    """
    s, e = start.isoformat(), end.isoformat()
    out = []
    for r in conn.execute(sql, (s, e, s, e)):
        row = dict(r)
        try:
            row["raw"] = json.loads(row.pop("raw_json") or "{}")
        except (TypeError, ValueError):
            row["raw"] = {}
        out.append(row)
    return out


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ---------------------------------------------------------------- sections


def build_report(rows: list[dict], conn: sqlite3.Connection, start: date, end: date,
                 commercial_terms, structural_terms, min_valuation) -> dict:
    """Pure-ish aggregation (only reads ingestion_runs from conn) so tests can
    assert on numbers without parsing console output."""
    localities = sorted({r["locality"] for r in rows} | set(
        x[0] for x in conn.execute("SELECT DISTINCT locality FROM permits")
    ))

    for r in rows:
        r["activity_date"] = parse_day(r["issued_date"]) or parse_day(r["applied_date"])
        r["reasons"] = qualify(r, commercial_terms, structural_terms, min_valuation)
        r["qualifies"] = bool(r["reasons"])

    in_window = [r for r in rows if r["activity_date"] and start <= r["activity_date"] <= end]

    # 1. weekly counts by type
    weekly_type = Counter()
    weekly_totals = defaultdict(lambda: {"total": 0, "qualifying": 0, "issued_qualifying": 0})
    reason_counts = defaultdict(Counter)
    for r in in_window:
        wk = week_start(r["activity_date"])
        ptype = r["permit_type"] or "(blank)"
        weekly_type[(r["locality"], wk, ptype, r["qualifies"])] += 1
        t = weekly_totals[(r["locality"], wk)]
        t["total"] += 1
        if r["qualifies"]:
            t["qualifying"] += 1
            if parse_day(r["issued_date"]) and start <= parse_day(r["issued_date"]) <= end:
                t["issued_qualifying"] += 1
            for reason in r["reasons"]:
                reason_counts[r["locality"]][reason] += 1

    # daily-digest view: qualifying permits by issued_date, per weekday
    weekdays = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    weekdays = [d for d in weekdays if d.weekday() < 5]
    issued_by_day = defaultdict(Counter)
    for r in rows:
        d = parse_day(r["issued_date"])
        if r["qualifies"] and d and start <= d <= end:
            issued_by_day[r["locality"]][d] += 1

    daily = {}
    for loc in localities + ["ALL"]:
        if loc == "ALL":
            counts = [sum(issued_by_day[l][d] for l in localities) for d in weekdays]
        else:
            counts = [issued_by_day[loc][d] for d in weekdays]
        daily[loc] = {
            "weekdays": len(weekdays),
            "days_with_match": sum(1 for c in counts if c > 0),
            "median_per_weekday": statistics.median(counts) if counts else 0,
            "mean_per_weekday": (sum(counts) / len(counts)) if counts else 0,
            "max_per_weekday": max(counts) if counts else 0,
        }

    # 2. completeness for qualifying permits
    completeness = {}
    raw_keys = defaultdict(Counter)
    for loc in localities:
        q = [r for r in in_window if r["locality"] == loc and r["qualifies"]]
        n = len(q)

        def has(field):
            return sum(1 for r in q if r[field] not in (None, "") and str(r[field]).strip())

        completeness[loc] = {
            "qualifying": n,
            "valuation": has("valuation"),
            "contractor": has("contractor"),
            "applicant": has("applicant"),
            "contractor_or_applicant": sum(
                1 for r in q if (r["contractor"] or "").strip() or (r["applicant"] or "").strip()
            ),
            "description": has("description"),
            "description_useful": sum(1 for r in q if len((r["description"] or "").strip()) >= 15),
            "geocoded": sum(1 for r in q if r["lat"] is not None and r["lng"] is not None),
        }
        for r in q:
            for k, v in r["raw"].items():
                if _CANDIDATE_KEY_RE.search(k) and v not in (None, "", "0", "0.0"):
                    raw_keys[loc][k] += 1

    # 3. business vs individual names
    names = {}
    for loc in localities:
        c = Counter()
        for r in in_window:
            if r["locality"] != loc:
                continue
            for field in ("contractor", "applicant"):
                kind = classify_name(r[field])
                if kind:
                    c[(field, kind)] += 1
        names[loc] = c

    # 4. freshness
    freshness = {}
    for loc in localities:
        loc_rows = [r for r in in_window if r["locality"] == loc]
        # A permit applied/issued before our first ingest of this locality
        # could only arrive via backfill, so its first_seen lag measures when
        # we started ingesting, not source latency. Only events after that
        # day count toward the *_to_first_seen lags.
        first_days = [parse_day(r["first_seen_at"]) for r in conn.execute(
            "SELECT MIN(first_seen_at) AS first_seen_at FROM permits WHERE locality = ?", (loc,)
        )]
        backfill_day = first_days[0] if first_days else None

        app_to_issue, issue_to_seen, app_to_seen = [], [], []
        excluded_backfill = 0
        for r in loc_rows:
            a, i, s = parse_day(r["applied_date"]), parse_day(r["issued_date"]), parse_day(r["first_seen_at"])
            if a and i and i >= a:
                app_to_issue.append((i - a).days)
            if not s or not backfill_day:
                continue
            if (a and a <= backfill_day) and not (i and i > backfill_day):
                excluded_backfill += 1
            if i and s and i > backfill_day:
                issue_to_seen.append((s - i).days)
            if a and s and a > backfill_day:
                app_to_seen.append((s - a).days)
        freshness[loc] = {
            "applied_to_issued": summarize_lags(app_to_issue),
            "issued_to_first_seen": summarize_lags(issue_to_seen),
            "applied_to_first_seen": summarize_lags(app_to_seen),
            "excluded_backfill": excluded_backfill,
            "backfill_day": backfill_day,
        }

    # ingestion cadence from ingestion_runs
    runs = {}
    if table_exists(conn, "ingestion_runs"):
        for loc in localities:
            rs = conn.execute(
                "SELECT started_at, status FROM ingestion_runs WHERE locality = ? ORDER BY started_at",
                (loc,),
            ).fetchall()
            ok = [parse_day(r["started_at"]) for r in rs if r["status"] == "success"]
            ok = [d for d in ok if d]
            gaps = [(b - a).days for a, b in zip(ok, ok[1:])]
            runs[loc] = {
                "total": len(rs),
                "success": len(ok),
                "failed": sum(1 for r in rs if r["status"] == "failed"),
                "last_success": ok[-1].isoformat() if ok else None,
                "median_gap_days": statistics.median(gaps) if gaps else None,
                "success_in_window": sum(1 for d in ok if start <= d <= end),
            }

    return {
        "localities": localities,
        "in_window_count": len(in_window),
        "weekly_type": weekly_type,
        "weekly_totals": weekly_totals,
        "reason_counts": reason_counts,
        "daily": daily,
        "completeness": completeness,
        "raw_keys": raw_keys,
        "names": names,
        "freshness": freshness,
        "runs": runs,
    }


def viability_hint(daily_all: dict) -> str:
    """Rule of thumb, not a verdict: a daily email is worth it when most
    weekdays have something to send and there's typically more than a couple
    of leads."""
    wd = daily_all["weekdays"]
    if not wd:
        return "No weekdays in window."
    hit_rate = daily_all["days_with_match"] / wd
    weekly = daily_all["mean_per_weekday"] * 5
    if hit_rate >= 0.8 and daily_all["median_per_weekday"] >= 3:
        return f"DAILY looks viable ({hit_rate:.0%} of weekdays have matches, ~{weekly:.0f}/week)."
    if weekly >= 5:
        return (f"WEEKLY recommended: ~{weekly:.0f} qualifying/week but only {hit_rate:.0%} "
                "of weekdays have any match -- a daily send would often be empty or thin.")
    return (f"BROADEN FILTERS: only ~{weekly:.1f} qualifying/week across all localities. "
            "Widen criteria (e.g. more permit types) or add sources before any digest.")


def emit(report: dict, start: date, end: date, out_dir: Path, args) -> list[Path]:
    stamp = end.isoformat()
    written = []

    def save(name, headers, rows):
        p = out_dir / f"audit_{stamp}_{name}.csv"
        write_csv(p, headers, rows)
        written.append(p)

    print(f"PermitIQ digest-viability audit  window {start} .. {end}  ({args.days} days)")
    print(f"Qualifying = commercial terms {args.commercial_terms!r} OR demo/structural terms "
          f"{args.structural_terms!r} OR valuation >= {args.min_valuation:,.0f}")
    print(f"Localities in DB: {', '.join(report['localities']) or '(none)'};  "
          f"permits in window: {report['in_window_count']}")

    # 1. weekly totals
    rows = []
    for (loc, wk), t in sorted(report["weekly_totals"].items()):
        rows.append([loc, wk.isoformat(), t["total"], t["qualifying"], pct(t["qualifying"], t["total"]),
                     t["issued_qualifying"]])
    hdr = ["locality", "week_of", "permits", "qualifying", "qual_%", "qual_issued"]
    print_table("1a. Weekly permits vs qualifying (week starts Monday, by issued-or-applied date)", hdr, rows)
    save("weekly_totals", hdr, rows)

    by_type = defaultdict(lambda: [0, 0])
    type_rows = []
    for (loc, wk, ptype, q), n in sorted(report["weekly_type"].items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        type_rows.append([loc, wk.isoformat(), ptype, "yes" if q else "no", n])
        by_type[(loc, ptype)][0] += n
        if q:
            by_type[(loc, ptype)][1] += n
    save("weekly_by_type", ["locality", "week_of", "permit_type", "qualifies", "count"], type_rows)
    nweeks = max(1, args.days / 7)
    rows = [[loc, ptype, tot, f"{tot / nweeks:.1f}", q, pct(q, tot)]
            for (loc, ptype), (tot, q) in sorted(by_type.items(), key=lambda kv: (kv[0][0], -kv[1][0]))]
    hdr = ["locality", "permit_type", "total", "per_week", "qualifying", "qual_%"]
    print_table("1b. Permit types over window (weekly breakdown in weekly_by_type CSV)", hdr, rows)

    rows = [[loc, c.get("commercial", 0), c.get("demo_structural", 0), c.get("valuation", 0)]
            for loc, c in sorted(report["reason_counts"].items())]
    print_table("1c. Why permits qualified (a permit can match several)",
                ["locality", "commercial", "demo_structural", "valuation"], rows)

    rows = [[loc, d["weekdays"], d["days_with_match"], pct(d["days_with_match"], d["weekdays"]),
             d["median_per_weekday"], f"{d['mean_per_weekday']:.1f}", d["max_per_weekday"]]
            for loc, d in report["daily"].items()]
    hdr = ["locality", "weekdays", "days_w_match", "hit_rate", "median/day", "mean/day", "max/day"]
    print_table("1d. Daily-digest simulation: qualifying permits by ISSUED date, per weekday", hdr, rows)
    save("daily_digest_sim", hdr, rows)

    # 2. completeness
    rows = []
    for loc, c in report["completeness"].items():
        n = c["qualifying"]
        rows.append([loc, n, pct(c["valuation"], n), pct(c["contractor"], n), pct(c["applicant"], n),
                     pct(c["contractor_or_applicant"], n), pct(c["description"], n),
                     pct(c["description_useful"], n), pct(c["geocoded"], n)])
    hdr = ["locality", "qualifying", "valuation", "contractor", "applicant", "contr_or_appl",
           "description", "desc>=15ch", "geocoded"]
    print_table("2. Field completeness, qualifying permits only", hdr, rows)
    save("completeness", hdr, rows)

    rows = [[loc, k, n] for loc, c in sorted(report["raw_keys"].items()) for k, n in c.most_common()]
    print_table("2b. Non-empty raw_json keys that look like names/costs (unmapped source fields)",
                ["locality", "raw_key", "qualifying_rows_with_value"], rows)
    save("raw_candidate_keys", ["locality", "raw_key", "qualifying_rows_with_value"], rows)

    # 3. names
    rows = []
    for loc, c in report["names"].items():
        for field in ("contractor", "applicant"):
            b, i = c.get((field, "business"), 0), c.get((field, "individual"), 0)
            rows.append([loc, field, b + i, b, i, pct(b, b + i)])
    hdr = ["locality", "field", "non_blank", "business", "individual", "business_%"]
    print_table("3. Business vs individual names (all permits in window)", hdr, rows)
    save("names", hdr, rows)

    # 4. freshness
    rows = []
    for loc, f in report["freshness"].items():
        for label in ("applied_to_issued", "issued_to_first_seen", "applied_to_first_seen"):
            s = f[label]
            rows.append([loc, label, s["n"], s["median"], s["p90"], s["min"], s["max"]])
    hdr = ["locality", "lag (days)", "n", "median", "p90", "min", "max"]
    print_table("4a. Freshness lags (first_seen_at = when our DB first stored the record)", hdr, rows)
    save("freshness", hdr, rows)
    for loc, f in report["freshness"].items():
        if f["excluded_backfill"]:
            print(f"  note: {loc}: {f['excluded_backfill']} records dated on/before the first "
                  f"ingest ({f['backfill_day']}) excluded from *_to_first_seen lags (backfill)")

    rows = [[loc, r["total"], r["success"], r["failed"], r["success_in_window"],
             r["last_success"], r["median_gap_days"]] for loc, r in report["runs"].items()]
    hdr = ["locality", "runs", "success", "failed", "success_in_window", "last_success", "median_gap_days"]
    print_table("4b. Ingestion cadence (ingestion_runs)", hdr, rows)
    save("ingestion_cadence", hdr, rows)

    # 5. sources
    rows = []
    for loc in report["localities"]:
        p = SOURCE_PROFILES.get(loc)
        if p is None:
            rows.append([loc, "UNKNOWN (no profile -- inspect ingest/)", "", "", ""])
            continue
        try:
            mod = __import__(p["module"], fromlist=["BASE_URL"])
            url = getattr(mod, "BASE_URL", "")
        except ImportError:
            url = "(adapter not importable)"
        rows.append([loc, p["access_method"], url, p["fetch"], p["fragility"]])
    hdr = ["locality", "access_method", "url", "fetch", "fragility"]
    print_table("5. Sources", hdr[:3], [r[:3] for r in rows])
    for r in rows:
        print(f"  {r[0]}: fetch = {r[3]}\n  {' ' * len(r[0])}  fragility = {r[4]}")
    save("sources", hdr, rows)

    print("\nViability hint (rule of thumb):", viability_hint(report["daily"]["ALL"]))
    print("\nCSVs written:")
    for p in written:
        print(f"  {p}")
    return written


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Read-only digest-viability audit of permits.db")
    p.add_argument("--db", type=Path, default=db_module.DB_PATH, help=f"default: {db_module.DB_PATH}")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--as-of", type=str, default=None, help="window end, YYYY-MM-DD (default: today)")
    p.add_argument("--min-valuation", type=float, default=50000.0)
    p.add_argument("--commercial-terms", type=str, default=",".join(DEFAULT_COMMERCIAL_TERMS),
                   help="comma-separated, matched against type/description/use class")
    p.add_argument("--structural-terms", type=str, default=",".join(DEFAULT_STRUCTURAL_TERMS),
                   help="comma-separated demolition/structural terms")
    p.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent.parent / "reports")
    return p.parse_args(argv)


def main(argv=None) -> dict:
    args = parse_args(argv)
    end = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()
    start = end - timedelta(days=args.days - 1)
    commercial, structural = parse_terms(args.commercial_terms), parse_terms(args.structural_terms)

    conn = connect_readonly(args.db)
    try:
        rows = load_rows(conn, start, end)
        report = build_report(rows, conn, start, end, commercial, structural, args.min_valuation)
    finally:
        conn.close()
    emit(report, start, end, args.out_dir, args)
    return report


if __name__ == "__main__":
    main()
