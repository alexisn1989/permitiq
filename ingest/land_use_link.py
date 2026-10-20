"""Stage 2 linkage: land-use decisions -> later VB building permits.

Deterministic tiers, highest first; one link per (decision, permit):
  gpin           permit raw_json.GPIN's first 10 digits = a GPIN printed in the title
  address_exact  same house number and normalized street
  same_parcel    both matched to the same parcel (Stage 3) -- only through
                 link_gpin, which excludes ambiguous and unresolved disputed matches
A Stage 3 parcel dispute (GPIN contradicts the location) also gates the gpin
tier on that side (gpin_trusted): unresolved, 'unresolvable' and
'point_correct' block it; 'gpin_correct' restores it. Rows with no fresh
parcel match keep Stage 2 behaviour -- there is no parcel evidence to apply.
Address tiers compare street strings, independent of the GPIN, and stay.
  address_range  same normalized street, house number inside a printed range
Every automatic link is a CANDIDATE: a property match can connect an approval
to an unrelated permit at the same parcel. Only decision_link_review
(verdict 'confirmed') promotes a link; 'rejected' removes it; a confirmed
pair with no automatic match is added as tier 'manual'. Reviews live in
their own table, so rebuilding the links never loses them.

Coverage: the observation window is only as good as the permit data. The
backfill start is a NAMED ingestion run (config [coverage]) whose completeness
was proven against the source (permit_backfill_checks, verified = 1 --
scripts/verify_permit_backfill.py), never the earliest permit on file; the last refresh is the latest successful VB
run. See the land_use_decision_status view for how both gate the statuses.
"""

from __future__ import annotations

import json
from collections import defaultdict
from ingest.address_norm import gpin10, parse_permit_street
from ingest.flood_stats import _cohort_sql

LOCALITY = "virginia_beach"
TIER_RANK = {"gpin": 1, "address_exact": 2, "same_parcel": 3, "address_range": 4, "manual": 5}


class CoverageError(Exception):
    pass


def window_end(conn, meeting_date: str, months: int) -> str:
    """Last day a permit application can link: SQLite date() arithmetic, the
    same expression the decision_permit_links_effective view uses, so the
    writers and every reader agree on boundary dates (e.g. Jan 31 + 1 month)."""
    return conn.execute("SELECT date(?, '+' || ? || ' months')", (meeting_date, int(months))).fetchone()[0]


def in_window(conn, meeting_date: str | None, applied_date: str | None, months: int) -> bool:
    if not meeting_date or not applied_date:
        return False
    return meeting_date <= applied_date <= window_end(conn, meeting_date, months)


def verify_coverage(conn, cfg: dict) -> dict:
    runs = conn.execute(
        "SELECT id, since_date, status, completed_at, fetched_count FROM ingestion_runs WHERE locality = ? "
        "ORDER BY id DESC LIMIT 15", (LOCALITY,)).fetchall()
    listing = "; ".join(f"id={r['id']} since={r['since_date']} status={r['status']} fetched={r['fetched_count']} "
                        f"completed={r['completed_at']}" for r in runs) or "none"
    if not cfg["backfill_from"] or cfg["backfill_run_id"] is None:
        raise CoverageError("config [coverage] backfill_from and backfill_run_id are not set -- name the VB backfill "
                            f"run you actually ran. Recent VB runs: {listing}")
    run = conn.execute("SELECT * FROM ingestion_runs WHERE id = ?", (cfg["backfill_run_id"],)).fetchone()
    problems = []
    if run is None:
        problems.append("run not found")
    else:
        if run["locality"] != LOCALITY:
            problems.append(f"run locality is {run['locality']!r}")
        if run["status"] != "success":
            problems.append(f"run status is {run['status']!r}")
        if not run["since_date"] or run["since_date"] > cfg["backfill_from"]:
            problems.append(f"run since_date {run['since_date']!r} is later than backfill_from {cfg['backfill_from']}")
    if run is not None and not problems:
        chk = conn.execute("SELECT verified, where_clause, source_count, db_ids, truly_missing FROM "
                           "permit_backfill_checks WHERE backfill_run_id = ? ORDER BY check_id DESC LIMIT 1",
                           (run["id"],)).fetchone()
        if chk is None:
            problems.append("no completeness check recorded -- run scripts/verify_permit_backfill.py")
        elif not chk["verified"]:
            problems.append(f"completeness check not verified ({chk['where_clause']}: source {chk['source_count']}, "
                            f"DB {chk['db_ids']}, truly missing {chk['truly_missing']})")
    if problems:
        raise CoverageError(f"backfill_run_id {cfg['backfill_run_id']} does not back backfill_from "
                            f"{cfg['backfill_from']}: {', '.join(problems)}. Recent VB runs: {listing}")
    last = conn.execute(
        "SELECT id, completed_at FROM ingestion_runs WHERE locality = ? AND status = 'success' "
        "AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1", (LOCALITY,)).fetchone()
    return {"locality": LOCALITY, "backfill_from": cfg["backfill_from"], "backfill_run_id": run["id"],
            "backfill_source": f"ingestion_runs.id={run['id']} since={run['since_date']} fetched={run['fetched_count']}",
            "last_refresh_at": last["completed_at"] if last else None,
            "last_refresh_run_id": last["id"] if last else None,
            "link_window_months": cfg["link_window_months"], "max_staleness_days": cfg["max_staleness_days"]}


def write_coverage(conn, cov: dict, ts: str) -> None:
    conn.execute(
        """INSERT INTO permit_coverage (locality, backfill_from, backfill_run_id, backfill_source, last_refresh_at,
               last_refresh_run_id, link_window_months, max_staleness_days, computed_at)
           VALUES (:locality, :backfill_from, :backfill_run_id, :backfill_source, :last_refresh_at,
                   :last_refresh_run_id, :link_window_months, :max_staleness_days, :ts)
           ON CONFLICT(locality) DO UPDATE SET backfill_from = excluded.backfill_from,
               backfill_run_id = excluded.backfill_run_id, backfill_source = excluded.backfill_source,
               last_refresh_at = excluded.last_refresh_at, last_refresh_run_id = excluded.last_refresh_run_id,
               link_window_months = excluded.link_window_months,
               max_staleness_days = excluded.max_staleness_days, computed_at = excluded.computed_at""",
        {**cov, "ts": ts})


def new_construction_ids(conn, include: list[dict]) -> set[int] | None:
    """Permit ids in the new-construction cohort; None when unconfigured."""
    if not include:
        return None
    expr, params = _cohort_sql(include)
    return {r[0] for r in conn.execute(f"SELECT p.id FROM permits p WHERE p.locality = ? AND {expr}",
                                       [LOCALITY] + params)}


def _permit_index(conn):
    by_gpin, by_street, by_parcel = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in conn.execute(
            "SELECT p.id, p.applied_date, json_extract(p.raw_json, '$.StreetAddress') AS street, "
            "json_extract(p.raw_json, '$.GPIN') AS gpin, e.link_gpin, COALESCE(e.gpin_trusted, 1) AS gpin_trusted "
            "FROM permits p "
            "LEFT JOIN permit_parcel_effective e ON e.permit_id = p.id "
            "WHERE p.locality = ? AND p.applied_date IS NOT NULL", (LOCALITY,)):
        g = gpin10(r["gpin"])
        if g and r["gpin_trusted"]:  # a disputed GPIN (unresolved / unresolvable / point_correct) never links
            by_gpin[g].append(r)
        parsed = parse_permit_street(r["street"])
        if parsed:
            by_street[parsed[1]].append((parsed[0], r))
        if r["link_gpin"]:  # fresh, unambiguous, and undisputed (or dispute resolved by review)
            by_parcel[r["link_gpin"]].append(r)
    return by_gpin, by_street, by_parcel


def build_links(conn, cfg: dict) -> tuple[list[dict], list[dict]]:
    """Automatic candidate links + 'nearby, not linked' review candidates.
    A permit qualifies only if applied on/after the meeting date and within
    link_window_months of it."""
    by_gpin, by_street, by_parcel = _permit_index(conn)
    addrs = defaultdict(list)
    for a in conn.execute(
            "SELECT a.*, d.meeting_date, e.link_gpin, COALESCE(e.gpin_trusted, 1) AS gpin_trusted "
            "FROM land_use_decision_addresses a "
            "JOIN land_use_decisions d ON d.decision_key = a.decision_key "
            "LEFT JOIN decision_parcel_effective e ON e.decision_key = a.decision_key AND e.seq = a.seq "
            "WHERE d.meeting_date IS NOT NULL"):
        addrs[a["decision_key"]].append(a)
    best: dict[tuple[str, int], dict] = {}
    nearby: list[dict] = []
    for key, rows in addrs.items():
        start = rows[0]["meeting_date"]
        end = window_end(conn, start, cfg["link_window_months"])
        in_window = lambda p: start <= p["applied_date"] <= end  # noqa: E731
        hits = []
        for a in rows:
            if a["gpin10"] and a["gpin_trusted"]:
                hits += [(p, "gpin", f"gpin10={a['gpin10']}") for p in by_gpin.get(a["gpin10"], []) if in_window(p)]
            if a["street_norm"]:
                for house, p in by_street.get(a["street_norm"], []):
                    if not in_window(p):
                        continue
                    if a["house_from"] == a["house_to"] == house:
                        hits.append((p, "address_exact", f"{house} {a['street_norm']}"))
                    elif a["house_from"] <= house <= a["house_to"]:
                        hits.append((p, "address_range", f"{house} in {a['house_from']}-{a['house_to']} {a['street_norm']}"))
                    elif abs(house - a["house_from"]) <= 10 or abs(house - a["house_to"]) <= 10:
                        nearby.append({"decision_key": key, "permit_id": p["id"], "matched_on":
                                       f"{house} near {a['house_from']}-{a['house_to']} {a['street_norm']}"})
            # Stage 3: same parcel, by undisputed (or review-resolved) parcel matches
            # on BOTH sides. Still a candidate, still inside the window.
            if a["link_gpin"]:
                hits += [(p, "same_parcel", f"parcel {a['link_gpin']}") for p in by_parcel.get(a["link_gpin"], [])
                         if in_window(p)]
        for p, tier, on in hits:
            k = (key, p["id"])
            if k not in best or TIER_RANK[tier] < TIER_RANK[best[k]["match_tier"]]:
                best[k] = {"decision_key": key, "permit_id": p["id"], "match_tier": tier, "matched_on": on}
    linked = set(best)
    nearby = [n for n in {(n["decision_key"], n["permit_id"]): n for n in nearby}.values()
              if (n["decision_key"], n["permit_id"]) not in linked]
    return list(best.values()), nearby


def gpin_gate_counts(conn) -> dict:
    """For the linker output: how many GPIN-bearing rows are blocked by a
    dispute, and how many had no fresh parcel match (Stage 2 behaviour)."""
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "permits_gpin_blocked": q("SELECT COUNT(*) FROM permit_parcel_effective WHERE gpin_trusted = 0"),
        "permits_without_fresh_parcel_match": q(
            "SELECT COUNT(*) FROM permits p JOIN permit_parcel_effective e ON e.permit_id = p.id "
            "WHERE p.locality = 'virginia_beach' AND e.fresh = 0"),
        "decision_rows_gpin_blocked": q("SELECT COUNT(*) FROM decision_parcel_effective WHERE gpin_trusted = 0"),
        "decision_rows_without_fresh_parcel_match": q("SELECT COUNT(*) FROM decision_parcel_effective WHERE fresh = 0"),
    }


def rebuild_links(conn, cfg: dict, ts: str) -> dict:
    """Replace decision_permit_links (derived) and apply the human reviews.
    Must run inside the caller's transaction."""
    links, nearby = build_links(conn, cfg)
    nc = new_construction_ids(conn, cfg["new_construction"]["include"])
    reviews = {(r["decision_key"], r["permit_id"]): r["verdict"]
               for r in conn.execute("SELECT decision_key, permit_id, verdict FROM decision_link_review")}
    conn.execute("DELETE FROM decision_permit_links")
    rows, auto = [], {(l["decision_key"], l["permit_id"]) for l in links}
    for l in links:
        verdict = reviews.get((l["decision_key"], l["permit_id"]))
        if verdict == "rejected":
            continue
        rows.append({**l, "link_state": "confirmed" if verdict == "confirmed" else "candidate"})
    meeting = dict(conn.execute("SELECT decision_key, meeting_date FROM land_use_decisions").fetchall())
    applied = dict(conn.execute("SELECT id, applied_date FROM permits WHERE locality = ?", (LOCALITY,)).fetchall())
    out_of_window = []
    for (dk, pid), verdict in reviews.items():
        if verdict != "confirmed" or (dk, pid) in auto or dk not in meeting or pid not in applied:
            continue
        # A confirmed pair must still satisfy the window: the window may have
        # shrunk, or the meeting/applied date changed, since the review.
        if not in_window(conn, meeting[dk], applied[pid], cfg["link_window_months"]):
            out_of_window.append({"decision_key": dk, "permit_id": pid, "meeting_date": meeting[dk],
                                  "applied_date": applied[pid]})
            continue
        rows.append({"decision_key": dk, "permit_id": pid, "match_tier": "manual",
                     "matched_on": "review", "link_state": "confirmed"})
    conn.executemany(
        """INSERT INTO decision_permit_links (decision_key, permit_id, match_tier, matched_on, link_state,
               is_new_construction, link_run_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(r["decision_key"], r["permit_id"], r["match_tier"], r["matched_on"], r["link_state"],
          None if nc is None else int(r["permit_id"] in nc), ts) for r in rows])
    return {"links": rows, "nearby": nearby, "review_out_of_window": out_of_window}


def status_counts(conn) -> dict:
    return dict(conn.execute("SELECT status, COUNT(*) FROM land_use_decision_status GROUP BY status").fetchall())


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True)
