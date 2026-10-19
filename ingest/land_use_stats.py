"""Stage 2 statistics -- the ONE implementation behind GET /api/land-use/stats
and scripts/land_use_report.py. Read-only.

Status comes only from the land_use_decision_status view. Time to permit is
defined per decision, one observation per measure (MIN over its linked
permits), over verified-approved decisions whose observation window is
certain (status confirmed_permit, candidate_permit, no_candidate_permit,
no_candidate_window_expired):

  t_first_application                   days from meeting_date to the earliest
                                        applied_date of a linked permit
  t_first_issued                        ... earliest non-null issued_date
  t_first_new_construction_application  ... earliest applied_date of a linked
                                        new-construction permit

Each is reported twice and never mixed: confirmed links only (headline), and
including candidate links ("unconfirmed property matches"). Decisions with no
qualifying permit are counted as n_open -- right-censored at the last permit
refresh, never imputed. Median and IQR use the nearest-rank method; blocks
with n < min_n are suppressed; no means.
"""

from __future__ import annotations

import math

TIMING_STATUSES = ("confirmed_permit", "candidate_permit", "no_candidate_permit", "no_candidate_window_expired")
MEASURES = {
    "t_first_application": ("p.applied_date", ""),
    "t_first_issued": ("p.issued_date", ""),
    "t_first_new_construction_application": ("p.applied_date", "AND k.is_new_construction = 1"),
}
FLOOD_RANK = {c: i for i, c in enumerate(
    ("sfha_v", "sfha_a", "shaded_x", "matched_non_sfha", "no_polygon", "not_geocoded", "untagged"))}


def nearest_rank(sorted_vals: list[int], q: float) -> int:
    return sorted_vals[max(1, math.ceil(q * len(sorted_vals))) - 1]


def coverage(conn) -> dict | None:
    row = conn.execute("SELECT backfill_from, backfill_source, last_refresh_at, link_window_months, "
                       "max_staleness_days, computed_at FROM permit_coverage WHERE locality = 'virginia_beach'").fetchone()
    return dict(row) if row else None


def _timing_block(conn, measure: str, states: tuple[str, ...], min_n: int, nc_configured: bool) -> dict:
    col, extra = MEASURES[measure]
    if measure == "t_first_new_construction_application" and not nc_configured:
        return {"configured": False}
    marks = ",".join("?" * len(states))
    tmarks = ",".join("?" * len(TIMING_STATUSES))
    rows = conn.execute(
        f"""SELECT s.decision_key,
                   (SELECT MIN(julianday({col}) - julianday(s.meeting_date))
                    FROM decision_permit_links k JOIN permits p ON p.id = k.permit_id
                    WHERE k.decision_key = s.decision_key AND k.link_state IN ({marks})
                      AND {col} IS NOT NULL AND {col} >= s.meeting_date {extra}) AS days
            FROM land_use_decision_status s WHERE s.status IN ({tmarks})""",
        (*states, *TIMING_STATUSES)).fetchall()
    vals = sorted(int(r["days"]) for r in rows if r["days"] is not None)
    out = {"configured": True, "n": len(vals), "n_open": sum(r["days"] is None for r in rows),
           "suppressed": len(vals) < min_n, "median_days": None, "p25_days": None, "p75_days": None}
    if vals and not out["suppressed"]:
        out.update(median_days=nearest_rank(vals, 0.5), p25_days=nearest_rank(vals, 0.25),
                   p75_days=nearest_rank(vals, 0.75))
    return out


def compute_land_use_stats(conn, cfg: dict, min_n: int | None = None) -> dict:
    min_n = min_n or cfg["new_construction"]["min_n"]
    nc_configured = bool(cfg["new_construction"]["include"])
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM land_use_decision_status GROUP BY status").fetchall())
    by_label = [dict(r) for r in conn.execute(
        "SELECT land_use_label, status, COUNT(*) AS n FROM land_use_decision_status "
        "GROUP BY land_use_label, status ORDER BY land_use_label, status")]
    by_year = [dict(r) for r in conn.execute(
        "SELECT COALESCE(substr(meeting_date, 1, 4), 'unknown') AS year, status, COUNT(*) AS n "
        "FROM land_use_decision_status GROUP BY year, status ORDER BY year, status")]
    timing = {}
    for name, states in (("confirmed_only", ("confirmed",)), ("including_candidates", ("confirmed", "candidate"))):
        timing[name] = {m: _timing_block(conn, m, states, min_n, nc_configured) for m in MEASURES}
    return {
        "definitions_version": 1,
        "coverage": coverage(conn),
        "min_n": min_n,
        "by_status": by_status,
        "by_label": by_label,
        "by_year": by_year,
        "timing": timing,
        "timing_population": list(TIMING_STATUSES),
        "percentile_method": "nearest-rank",
    }


def decision_flood(conn, keys: list[str]) -> dict[str, dict]:
    """Reported flood class per decision: the most hazardous fresh class of
    its own geocoded address points; otherwise of its confirmed linked
    permits; otherwise of its candidate links. Source is always recorded."""
    out: dict[str, dict] = {}
    if not keys:
        return out
    marks = ",".join("?" * len(keys))
    own = conn.execute(
        f"""SELECT a.decision_key, e.effective_class, e.fld_zone FROM land_use_decision_addresses a
            JOIN decision_flood_effective e ON e.decision_key = a.decision_key AND e.seq = a.seq
            WHERE a.decision_key IN ({marks}) AND a.lat IS NOT NULL AND e.fresh = 1""", keys).fetchall()
    linked = conn.execute(
        f"""SELECT k.decision_key, k.link_state, e.effective_class, e.fld_zone FROM decision_permit_links k
            JOIN permit_flood_effective e ON e.permit_id = k.permit_id
            WHERE k.decision_key IN ({marks}) AND e.fresh = 1""", keys).fetchall()
    for source, rows in (("decision_point", own),
                         ("confirmed_permit", [r for r in linked if r["link_state"] == "confirmed"]),
                         ("candidate_permit", [r for r in linked if r["link_state"] == "candidate"])):
        for r in rows:
            k = r["decision_key"]
            if k in out and out[k]["flood_source"] != source:
                continue
            if k not in out or FLOOD_RANK[r["effective_class"]] < FLOOD_RANK[out[k]["flood_class"]]:
                out[k] = {"flood_class": r["effective_class"], "fld_zone": r["fld_zone"], "flood_source": source}
    for k in keys:
        out.setdefault(k, {"flood_class": None, "fld_zone": None, "flood_source": None})
    return out
