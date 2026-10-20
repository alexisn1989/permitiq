"""Data quality for the real-data run: source dates ("data as of"), the data
snapshot that accuracy samples and tallies are frozen to, live coverage /
unresolved counts / refresh age, and the unmatched-reasons rows. Read-only
except record_source_dates() and import_tally(), used by CLI writers.

Freshness rule for accuracy figures: a tally carries the snapshot it was
computed on. If the current snapshot differs (permits, decisions, flood or
parcel load, coverage runs, Council export, links or any review changed),
the accuracy block is reported as stale and its figures are withheld, so
current coverage is never shown next to an obsolete accuracy figure.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

LOCALITY = "virginia_beach"


def _one(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _ms_to_iso(v):
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return v


# ---------- source dates ----------

def compute_source_dates(conn, council_export_meta: dict | None = None) -> dict:
    cov = _one(conn, "SELECT backfill_from, backfill_run_id, backfill_source, last_refresh_at, last_refresh_run_id "
                     "FROM permit_coverage WHERE locality = ?", (LOCALITY,))
    last_run = _one(conn, "SELECT id, since_date, completed_at, fetched_count FROM ingestion_runs WHERE locality = ? "
                          "AND status = 'success' ORDER BY completed_at DESC LIMIT 1", (LOCALITY,))
    flood = _one(conn, "SELECT l.load_id, l.source_vintage, l.content_hash, l.feature_count, l.loaded_at "
                       "FROM flood_zone_loads l JOIN flood_locality_dfirm m ON m.dfirm_id = l.dfirm_id "
                       "WHERE m.locality = ? AND l.is_active = 1", (LOCALITY,))
    parcels = _one(conn, "SELECT load_id, source_url, source_vintage, content_hash, feature_count, expected_count, "
                         "received_count, unique_source_ids, loaded_at FROM parcel_loads WHERE is_active = 1")
    if parcels:
        parcels["source_vintage_iso"] = _ms_to_iso(parcels["source_vintage"])
    council = _one(conn, "SELECT source_file_sha256, MAX(updated_at) AS ingested_at, COUNT(*) AS decisions "
                         "FROM land_use_decisions GROUP BY source_file_sha256 ORDER BY ingested_at DESC LIMIT 1")
    if council and council_export_meta:
        council.update({"exported_at": council_export_meta.get("exported_at"),
                        "export_rows": council_export_meta.get("rows")})
    geo = _one(conn, "SELECT MIN(updated_at) AS oldest, MAX(updated_at) AS newest, COUNT(*) AS entries FROM geocode_cache")
    return {
        "permits": {"as_of": (last_run or {}).get("completed_at"), "last_refresh_run": last_run, "coverage": cov},
        # as_of is the export time from the export's own header -- never the ingest time; None if unknown
        "council_export": {"as_of": (council or {}).get("exported_at"), **(council or {})},
        # FEMA's VERSION_ID is a version label, not a date: as_of is when this load was fetched
        "fema": {"as_of": (flood or {}).get("loaded_at"), **(flood or {})},
        "parcels": {"as_of": (parcels or {}).get("source_vintage_iso") or (parcels or {}).get("loaded_at"),
                    **(parcels or {})},
        "geocode_cache": {"as_of": (geo or {}).get("newest"), **(geo or {})},
    }


def record_source_dates(conn, dates: dict, ts: str) -> None:
    conn.executemany(
        """INSERT INTO source_dates (source, as_of, detail_json, recorded_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET as_of = excluded.as_of, detail_json = excluded.detail_json,
               recorded_at = excluded.recorded_at""",
        [(k, v.get("as_of"), json.dumps(v, sort_keys=True, default=str), ts) for k, v in dates.items()])


def data_as_of(conn) -> dict:
    """Recorded source dates (by the runbook), plus the live coverage window."""
    out = {r["source"]: {"as_of": r["as_of"], "recorded_at": r["recorded_at"], "detail": json.loads(r["detail_json"])}
           for r in conn.execute("SELECT * FROM source_dates ORDER BY source")}
    cov = _one(conn, "SELECT backfill_from, last_refresh_at FROM permit_coverage WHERE locality = ?", (LOCALITY,))
    return {"sources": out, "permit_window": cov}


# ---------- snapshot (what samples and tallies are frozen to) ----------

# Content (not timestamps) of everything an accuracy figure depends on. Re-running
# the pipeline with no real change (the linker rebuilds link_run_at, the ingest
# touches updated_at) keeps the same hashes, so a tally stays fresh; any real
# change -- a moved permit, a new decision fact, a relinked tier, a new or
# changed review -- changes them.
STATE_SQL = {
    "permits": "SELECT id, lat, lng, applied_date, issued_date, status_bucket, json_extract(raw_json, '$.GPIN'), "
               "json_extract(raw_json, '$.StreetAddress') FROM permits WHERE locality = 'virginia_beach' ORDER BY id",
    "decisions": "SELECT decision_key, facts_fingerprint, land_use_label FROM land_use_decisions ORDER BY 1",
    "decision_addresses": "SELECT decision_key, seq, gpin10, house_from, house_to, street_norm, lat, lng "
                          "FROM land_use_decision_addresses ORDER BY 1, 2",
    "links": "SELECT decision_key, permit_id, match_tier, matched_on, link_state, is_new_construction "
             "FROM decision_permit_links ORDER BY 1, 2",
    "link_reviews": "SELECT decision_key, permit_id, verdict FROM decision_link_review ORDER BY 1, 2",
    "outcome_reviews": "SELECT decision_key, verified_outcome, reviewed_fingerprint FROM decision_outcome_review ORDER BY 1",
    "parcel_reviews": "SELECT subject_kind, subject_key, verdict, reviewed_fp FROM parcel_match_review ORDER BY 1, 2",
    "parcel_set_reviews": "SELECT decision_key, verdict, reviewed_fp FROM decision_parcel_set_review ORDER BY 1",
    "permit_parcel": "SELECT permit_id, load_id, match_fp FROM permit_parcel ORDER BY 1",
    "decision_parcel": "SELECT decision_key, seq, match_fp, cross_row, set_fp FROM decision_parcel ORDER BY 1, 2",
    "parcel_flood": "SELECT parcel_load_id, flood_load_id, gpin, parcel_flood_class, min_share FROM parcel_flood "
                    "ORDER BY 1, 2, 3",
    "permit_flood": "SELECT permit_id, load_id, flood_class, tagged_lat, tagged_lng FROM permit_flood ORDER BY 1",
    "coverage": "SELECT locality, backfill_from, backfill_run_id, last_refresh_run_id, link_window_months, "
                "max_staleness_days FROM permit_coverage ORDER BY 1",
}


def _table_hash(conn, sql: str) -> str:
    h = hashlib.sha256()
    for row in conn.execute(sql):
        h.update(json.dumps(list(row), default=str).encode())
        h.update(b"\n")
    return h.hexdigest()


def snapshot(conn) -> dict:
    flood = _one(conn, "SELECT l.load_id FROM flood_zone_loads l JOIN flood_locality_dfirm m ON m.dfirm_id = l.dfirm_id "
                       "WHERE m.locality = ? AND l.is_active = 1", (LOCALITY,))
    parcel = _one(conn, "SELECT load_id FROM parcel_loads WHERE is_active = 1")
    cov = _one(conn, "SELECT backfill_run_id, last_refresh_run_id FROM permit_coverage WHERE locality = ?", (LOCALITY,))
    shas = sorted(r[0] for r in conn.execute("SELECT DISTINCT source_file_sha256 FROM land_use_decisions"))
    state = {name: _table_hash(conn, sql) for name, sql in STATE_SQL.items()}
    return {
        "flood_load_id": (flood or {}).get("load_id"),
        "parcel_load_id": (parcel or {}).get("load_id"),
        "coverage_runs": cov,
        "council_export_sha256": shas,
        "state_fingerprint": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
        "state": state,
    }


def snapshot_diff(old: dict, new: dict) -> list[str]:
    changed = [k for k in ("flood_load_id", "parcel_load_id", "coverage_runs", "council_export_sha256")
               if old.get(k) != new.get(k)]
    if old.get("state_fingerprint") != new.get("state_fingerprint"):
        o, n = old.get("state", {}), new.get("state", {})
        changed += [f"state.{k}" for k in sorted(set(o) | set(n)) if o.get(k) != n.get(k)] or ["state"]
    return changed


# ---------- coverage / unresolved / refresh age ----------

def coverage(conn) -> dict:
    q = lambda sql: dict(conn.execute(sql).fetchall())  # noqa: E731
    vb = "FROM permits p JOIN permit_parcel_effective e ON e.permit_id = p.id WHERE p.locality = 'virginia_beach'"
    return {
        "permits_total": conn.execute("SELECT COUNT(*) FROM permits WHERE locality = 'virginia_beach'").fetchone()[0],
        "permits_geocoded": conn.execute("SELECT COUNT(*) FROM permits WHERE locality = 'virginia_beach' "
                                         "AND lat IS NOT NULL").fetchone()[0],
        "permits_by_parcel_method": q(f"SELECT e.parcel_state, COUNT(*) {vb} GROUP BY e.parcel_state"),
        "permits_by_point_flood_class": q("SELECT e.effective_class, COUNT(*) FROM permits p JOIN permit_flood_effective e "
                                          "ON e.permit_id = p.id WHERE p.locality = 'virginia_beach' GROUP BY 1"),
        "decisions_total": conn.execute("SELECT COUNT(*) FROM land_use_decisions").fetchone()[0],
        "decisions_by_status": q("SELECT status, COUNT(*) FROM land_use_decision_status GROUP BY status"),
        "decision_rows_by_parcel_method": q("SELECT parcel_state, COUNT(*) FROM decision_parcel_effective GROUP BY 1"),
        "links_by_tier_state": q("SELECT match_tier || '/' || link_state, COUNT(*) FROM decision_permit_links_effective "
                                 "GROUP BY 1"),
    }


def unresolved(conn) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "outcome_unverified": one("SELECT COUNT(*) FROM land_use_decision_status WHERE status = 'outcome_unverified'"),
        "outcome_review_stale": one("SELECT COUNT(*) FROM land_use_decision_status WHERE status = 'outcome_review_stale'"),
        "quarantined": one("SELECT COUNT(*) FROM land_use_decision_status WHERE status = 'quarantined'"),
        "observation_uncertain": one("SELECT COUNT(*) FROM land_use_decision_status "
                                     "WHERE status = 'observation_uncertain'"),
        "candidate_links_unreviewed": one("SELECT COUNT(*) FROM decision_permit_links_effective "
                                          "WHERE link_state = 'candidate'"),
        "permit_parcel_disputes_open": one("SELECT COUNT(*) FROM permit_parcel_effective WHERE disputed = 1 AND "
                                           "(review_verdict IS NULL OR review_verdict = 'unresolvable')"),
        "decision_parcel_disputes_open_within_row": one(
            "SELECT COUNT(*) FROM decision_parcel_effective WHERE disputed = 1 AND "
            "(review_verdict IS NULL OR review_verdict = 'unresolvable')"),
        "decision_cross_row_conflicts_open": one(
            "SELECT COUNT(DISTINCT decision_key) FROM decision_parcel_effective WHERE cross_row_conflict = 1 AND "
            "(set_verdict IS NULL OR set_verdict = 'unresolvable')"),
    }


def refresh_age(conn, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    out = {}
    for r in conn.execute("SELECT source, as_of FROM source_dates"):
        days = None
        try:
            v = r["as_of"]
            if v and str(v).isdigit():
                v = _ms_to_iso(v)
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            days = round((now - d).total_seconds() / 86400, 1)
        except (TypeError, ValueError):
            pass
        out[r["source"]] = {"as_of": r["as_of"], "age_days": days}
    return out


# ---------- tallies ----------

def import_tally(conn, tally: dict, ts: str) -> int:
    return conn.execute("INSERT INTO quality_tallies (tallied_at, snapshot_json, result_json, synthetic) "
                        "VALUES (?, ?, ?, ?)",
                        (tally["tallied_at"], json.dumps(tally["snapshot"], sort_keys=True, default=str),
                         json.dumps(tally["results"], sort_keys=True), int(bool(tally.get("synthetic"))))).lastrowid


def accuracy_block(conn) -> dict:
    row = conn.execute("SELECT * FROM quality_tallies ORDER BY tally_id DESC LIMIT 1").fetchone()
    if row is None:
        return {"freshness": "none", "note": "no accuracy tally imported yet"}
    changed = snapshot_diff(json.loads(row["snapshot_json"]), snapshot(conn))
    if changed:
        return {"freshness": "stale", "tallied_at": row["tallied_at"], "changed_since_tally": changed,
                "note": "data changed since this tally; figures withheld -- re-sample and re-tally"}
    from ingest import accuracy  # local import: accuracy builds on flood_stats / parcels
    current = {k: len(v) for k, v in accuracy.populations(conn).items()}
    return {"freshness": "fresh", "tallied_at": row["tallied_at"], "synthetic": bool(row["synthetic"]),
            "results": json.loads(row["result_json"]),
            "current_population": current}  # today's counts, shown beside (never replacing) the sampled N


def backfill(conn) -> dict | None:
    row = conn.execute("SELECT b.* FROM permit_backfill_checks b JOIN permit_coverage c "
                       "ON c.backfill_run_id = b.backfill_run_id WHERE c.locality = ? "
                       "ORDER BY b.check_id DESC LIMIT 1", (LOCALITY,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["per_year"] = json.loads(out.pop("per_year_json"))
    return out


def quality(conn) -> dict:
    return {"coverage": coverage(conn), "unresolved": unresolved(conn), "refresh_age": refresh_age(conn),
            "backfill": backfill(conn), "accuracy": accuracy_block(conn), "data_as_of": data_as_of(conn)}


# ---------- unmatched reasons ----------

UNMATCHED_SQL = [
    # (entity, aspect, SQL returning key, reason, detail)
    ("permit", "geocode", """SELECT p.source_permit_id, 'not_geocoded:' || p.geocode_status, p.address FROM permits p
        WHERE p.locality = 'virginia_beach' AND p.lat IS NULL"""),
    ("permit", "parcel", """SELECT p.source_permit_id, 'parcel_' || e.parcel_state, p.address FROM permits p
        JOIN permit_parcel_effective e ON e.permit_id = p.id WHERE p.locality = 'virginia_beach'
        AND e.parcel_state IN ('no_parcel', 'ambiguous', 'not_geocoded_no_gpin', 'untagged')"""),
    ("permit", "parcel", """SELECT p.source_permit_id, 'parcel_disputed', p.address || ' (GPIN parcel '
        || COALESCE(e.display_gpin, '?') || ', point parcel ' || COALESCE(e.alt_gpin, 'none') || ')' FROM permits p
        JOIN permit_parcel_effective e ON e.permit_id = p.id WHERE p.locality = 'virginia_beach' AND e.disputed = 1
        AND (e.review_verdict IS NULL OR e.review_verdict = 'unresolvable')"""),
    ("permit", "flood_point", """SELECT p.source_permit_id, 'flood_point_' || e.effective_class, p.address FROM permits p
        JOIN permit_flood_effective e ON e.permit_id = p.id WHERE p.locality = 'virginia_beach'
        AND e.effective_class IN ('no_polygon', 'untagged')"""),
    ("permit", "flood_parcel", """SELECT p.source_permit_id, 'parcel_flood_' || COALESCE(pf.parcel_flood_class, 'untagged'),
        p.address || ' parcel ' || pp.display_gpin FROM permits p JOIN permit_parcel_effective pp ON pp.permit_id = p.id
        LEFT JOIN parcel_flood_effective pf ON pf.gpin = pp.display_gpin WHERE p.locality = 'virginia_beach'
        AND pp.display_gpin IS NOT NULL AND (pf.gpin IS NULL OR pf.parcel_flood_class IN
        ('screening_sliver', 'screening_partial_coverage', 'no_polygon'))"""),
    ("decision", "status", """SELECT decision_key, 'status_' || status, NULL FROM land_use_decision_status
        WHERE status IN ('quarantined', 'outcome_review_stale', 'outcome_unverified', 'address_unparsed',
                         'observation_uncertain')"""),
    ("decision", "title", """SELECT decision_key, 'title_truncated', substr(title, 1, 120) FROM land_use_decisions
        WHERE title_truncated = 1"""),
    ("decision", "parcel", """SELECT e.decision_key || '#' || e.seq, 'parcel_row_' || e.parcel_state, a.raw_text
        FROM decision_parcel_effective e JOIN land_use_decision_addresses a ON a.decision_key = e.decision_key
        AND a.seq = e.seq WHERE e.parcel_state IN ('no_parcel', 'ambiguous', 'untagged')"""),
    ("decision", "parcel", """SELECT e.decision_key || '#' || e.seq, 'parcel_row_disputed_' || e.dispute_reason, a.raw_text
        FROM decision_parcel_effective e JOIN land_use_decision_addresses a ON a.decision_key = e.decision_key
        AND a.seq = e.seq WHERE e.dispute_reason IS NOT NULL AND e.gpin_trusted = 0"""),
]


def unmatched_rows(conn) -> list[tuple]:
    out = []
    for entity, aspect, sql in UNMATCHED_SQL:
        out += [(entity, r[0], aspect, r[1], r[2]) for r in conn.execute(sql)]
    return out
