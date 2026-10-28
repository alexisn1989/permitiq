"""Frozen accuracy samples and their tally (real-data hardening).

Three strata, each with an explicit judgment -- never a generic "correct":
  link_<tier>   candidate links per tier      same_project: yes | no | cant_tell
  lead          no_candidate_permit decisions lead_check: permit_found_elsewhere |
                                              no_permit_found | not_an_approval | cant_tell
                                              + search_done (an independent search
                                              outside permits.db; required)
  flood         permits whose point and       flood_check: parcel_class_right |
                parcel flood classes differ   point_class_right | both_defensible | cant_tell
  council_outcome  Clerk land-use items with  outcome_check: right | wrong | cant_tell
                a mapped outcome              (outcome, deferral date and tally as read
                                              from the Clerk's page)
  council_link  non-ambiguous Clerk item ->   same_application: yes | no | cant_tell
                PC item links (they set the
                Council stage)

The two council strata are measurements only: no judgment is ever applied
back as a review. Their sheet rows carry no names or subject text; the
checker opens the Clerk's page and the agenda from the links.

A sample is frozen to a data snapshot: each row carries a fingerprint of the
facts it shows, and the sheet a hash over all fact columns. The tally refuses
an edited sheet, drops rows whose facts changed (stale), and reports per-tier
adjudicated-sample precision with Wilson 95% intervals next to the full
population size and the cant_tell count. It never reports an unweighted
overall link accuracy.
"""

from __future__ import annotations

import hashlib
import json
import math

from ingest import flood_stats
from ingest.parcels import fingerprint

TIERS = ("gpin", "address_exact", "same_parcel", "address_range")
JUDGMENTS = {
    "link": ("same_project", ("yes", "no", "cant_tell")),
    "lead": ("lead_check", ("permit_found_elsewhere", "no_permit_found", "not_an_approval", "cant_tell")),
    "flood": ("flood_check", ("parcel_class_right", "point_class_right", "both_defensible", "cant_tell")),
    "council_outcome": ("outcome_check", ("right", "wrong", "cant_tell")),
    "council_link": ("same_application", ("yes", "no", "cant_tell")),
}
FACT_COLUMNS = ("stratum", "row_id", "fp", "decision_key", "meeting_id", "meeting_date", "decision_title",
                "verified_outcome", "review_evidence", "permit_id", "source_permit_id", "permit_address",
                "applied_date", "issued_date", "permit_type", "permit_description", "match_tier", "matched_on",
                "decision_parcels", "permit_parcel", "coverage_from", "coverage_to", "lat", "lng", "point_class",
                "parcel_gpin", "parcel_address", "zoning", "parcel_class", "share_sfha_v", "share_sfha_a",
                "share_shaded_x", "share_other_mapped", "share_unmapped",
                "clerk_item_key", "council_date", "clerk_label", "clerk_page", "clerk_link", "motion_raw",
                "outcome", "deferred_to", "tally", "request_types", "clerk_addresses", "pc_item_key",
                "hearing_date", "pc_request", "pc_addresses", "agenda_url")
JUDGMENT_COLUMNS = ("same_project", "lead_check", "search_done", "flood_check", "outcome_check", "same_application",
                    "checker", "note")
COLUMNS = FACT_COLUMNS + JUDGMENT_COLUMNS


def kind_of(stratum: str) -> str:
    return "link" if stratum.startswith("link_") else stratum


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def _decision_parcels(conn) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT decision_key, group_concat(link_gpin) FROM (SELECT DISTINCT decision_key, link_gpin "
        "FROM decision_parcel_effective WHERE link_gpin IS NOT NULL ORDER BY 1, 2) GROUP BY decision_key")}


def populations(conn) -> dict[str, dict[str, dict]]:
    """{stratum: {row_id: facts}} for every eligible row (the full population).
    Each facts dict has 'fp', the fingerprint of the facts the row shows."""
    dparcels = _decision_parcels(conn)
    cov = conn.execute("SELECT backfill_from, last_refresh_at FROM permit_coverage "
                       "WHERE locality = 'virginia_beach'").fetchone()
    cov_from, cov_to = (cov["backfill_from"], cov["last_refresh_at"]) if cov else (None, None)
    out: dict[str, dict] = {f"link_{t}": {} for t in TIERS}
    out["lead"], out["flood"] = {}, {}
    out.update(council_populations(conn))
    for r in conn.execute(
            """SELECT k.decision_key, k.permit_id, k.match_tier, k.matched_on, k.link_state, d.facts_fingerprint,
                      d.meeting_id, d.meeting_date, d.title, rv.verified_outcome, rv.evidence, p.source_permit_id,
                      p.address, p.applied_date, p.issued_date, p.permit_type, p.description, pp.link_gpin
               FROM decision_permit_links_effective k JOIN land_use_decisions d ON d.decision_key = k.decision_key
               JOIN permits p ON p.id = k.permit_id LEFT JOIN permit_parcel_effective pp ON pp.permit_id = p.id
               LEFT JOIN decision_outcome_review rv ON rv.decision_key = d.decision_key
               WHERE k.link_state = 'candidate' ORDER BY k.decision_key, k.permit_id"""):
        if r["match_tier"] not in TIERS:
            continue
        rid = f"{r['decision_key']}|{r['permit_id']}"
        facts = {"decision_key": r["decision_key"], "meeting_id": r["meeting_id"], "meeting_date": r["meeting_date"],
                 "decision_title": r["title"], "verified_outcome": r["verified_outcome"],
                 "review_evidence": r["evidence"], "permit_id": r["permit_id"],
                 "source_permit_id": r["source_permit_id"], "permit_address": r["address"],
                 "applied_date": r["applied_date"], "issued_date": r["issued_date"], "permit_type": r["permit_type"],
                 "permit_description": r["description"], "match_tier": r["match_tier"], "matched_on": r["matched_on"],
                 "decision_parcels": dparcels.get(r["decision_key"]), "permit_parcel": r["link_gpin"]}
        facts["fp"] = fingerprint("link", r["facts_fingerprint"], r["permit_id"], r["match_tier"], r["matched_on"],
                                  r["link_state"], dparcels.get(r["decision_key"]), r["link_gpin"])
        out[f"link_{r['match_tier']}"][rid] = facts
    for r in conn.execute(
            """SELECT s.decision_key, d.facts_fingerprint, d.meeting_id, d.meeting_date, d.title, s.status,
                      rv.verified_outcome, rv.evidence,
                      (SELECT group_concat(raw_text, ' | ') FROM land_use_decision_addresses a
                       WHERE a.decision_key = s.decision_key) AS addresses
               FROM land_use_decision_status s JOIN land_use_decisions d ON d.decision_key = s.decision_key
               LEFT JOIN decision_outcome_review rv ON rv.decision_key = s.decision_key
               WHERE s.status = 'no_candidate_permit' ORDER BY 1"""):
        facts = {"decision_key": r["decision_key"], "meeting_id": r["meeting_id"], "meeting_date": r["meeting_date"],
                 "decision_title": r["title"], "verified_outcome": r["verified_outcome"],
                 "review_evidence": r["evidence"], "permit_address": r["addresses"],
                 "decision_parcels": dparcels.get(r["decision_key"]), "coverage_from": cov_from, "coverage_to": cov_to}
        facts["fp"] = fingerprint("lead", r["facts_fingerprint"], r["status"], cov_from, cov_to,
                                  dparcels.get(r["decision_key"]))
        out["lead"][r["decision_key"]] = facts
    for r in conn.execute(
            f"""SELECT p.id, p.source_permit_id, p.address, p.lat, p.lng, e.effective_class AS point_class,
                       e.load_id AS flood_load, pp.display_gpin, pp.load_id AS parcel_load, pa.prop_address, pa.zoning,
                       {flood_stats.PARCEL_CLASS_SQL} AS parcel_class, pf.share_sfha_v, pf.share_sfha_a,
                       pf.share_shaded_x, pf.share_other_mapped, pf.share_unmapped
                FROM permits p JOIN permit_flood_effective e ON e.permit_id = p.id {flood_stats.PARCEL_JOIN_SQL}
                LEFT JOIN parcels pa ON pa.load_id = pp.load_id AND pa.gpin = pp.display_gpin
                WHERE p.locality = 'virginia_beach' AND e.fresh = 1 AND pf.gpin IS NOT NULL
                ORDER BY p.id"""):
        if r["parcel_class"] == r["point_class"] or r["parcel_class"] in ("parcel_disputed", "untagged"):
            continue
        facts = {"permit_id": r["id"], "source_permit_id": r["source_permit_id"], "permit_address": r["address"],
                 "lat": r["lat"], "lng": r["lng"], "point_class": r["point_class"], "parcel_gpin": r["display_gpin"],
                 "parcel_address": r["prop_address"], "zoning": r["zoning"], "parcel_class": r["parcel_class"],
                 **{k: r[k] for k in ("share_sfha_v", "share_sfha_a", "share_shaded_x", "share_other_mapped",
                                      "share_unmapped")}}
        facts["fp"] = fingerprint("flood", r["id"], r["lat"], r["lng"], r["point_class"], r["flood_load"],
                                  r["display_gpin"], r["parcel_class"], r["parcel_load"])
        out["flood"][str(r["id"])] = facts
    return out


def _clerk_facts(r) -> dict:
    from ingest import edocs  # local import: keeps this module free of the archive client
    return {"clerk_item_key": r["item_key"], "council_date": r["meeting_date"], "clerk_label": r["item_label"],
            "clerk_page": r["page"], "clerk_link": edocs.doc_url(r["entry_id"]), "motion_raw": r["motion_raw"],
            "outcome": r["outcome"], "deferred_to": r["deferred_to"],
            "tally": "" if r["aye"] is None else f"{r['aye']}-{r['nay']}",
            "request_types": " ".join(json.loads(r["request_types_json"])), "clerk_addresses": r["addresses"]}


def council_populations(conn) -> dict[str, dict[str, dict]]:
    """The two council strata (Clerk's Summary of Council Actions). Public facts only."""
    items = "(SELECT group_concat(raw_text, ' | ') FROM clerk_item_addresses a WHERE a.item_key = c.item_key)"
    out: dict[str, dict] = {"council_outcome": {}, "council_link": {}}
    for r in conn.execute(f"SELECT c.*, {items} AS addresses FROM clerk_items c WHERE c.is_land_use = 1 "
                          "AND c.outcome != 'unmapped' ORDER BY c.item_key"):
        facts = _clerk_facts(r)
        facts["fp"] = fingerprint("council_outcome", r["item_key"], r["outcome"], r["deferred_to"], r["aye"],
                                  r["nay"], r["motion_raw"], r["entry_id"], r["page"])
        out["council_outcome"][r["item_key"]] = facts
    for r in conn.execute(
            f"""SELECT c.*, {items} AS addresses, l.pc_item_key, l.matched_on, i.meeting_date AS hearing_date,
                       i.request AS pc_request, i.addresses AS pc_addresses, d.url AS agenda_url
                FROM clerk_pc_links l JOIN clerk_items c ON c.item_key = l.clerk_item_key
                JOIN pc_items i ON i.item_key = l.pc_item_key JOIN pc_documents d ON d.doc_id = i.doc_id
                WHERE l.ambiguous = 0 ORDER BY l.clerk_item_key, l.pc_item_key"""):
        facts = _clerk_facts(r)
        facts.update({"pc_item_key": r["pc_item_key"], "hearing_date": r["hearing_date"], "pc_request": r["pc_request"],
                      "pc_addresses": r["pc_addresses"], "agenda_url": r["agenda_url"], "matched_on": r["matched_on"]})
        facts["fp"] = fingerprint("council_link", r["item_key"], r["pc_item_key"], r["matched_on"], r["addresses"],
                                  r["pc_addresses"], r["pc_request"], r["request_types_json"])
        out["council_link"][f"{r['item_key']}|{r['pc_item_key']}"] = facts
    return out


def row_values(stratum: str, row_id: str, facts: dict) -> dict:
    vals = {c: "" for c in COLUMNS}
    vals.update({k: "" if v is None else v for k, v in facts.items() if k in vals})
    vals.update(stratum=stratum, row_id=row_id)
    return vals


def sample_hash(rows: list[dict]) -> str:
    canon = [[str(r.get(c, "")) for c in FACT_COLUMNS] for r in rows]
    return hashlib.sha256(json.dumps(canon, sort_keys=False).encode()).hexdigest()
