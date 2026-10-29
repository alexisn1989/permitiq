"""Planning Commission hearings: store parsed documents, link items to Council
decisions (candidates only), and the public view of names.

Writers only (scripts/ingest_pc_documents.py, scripts/link_pc_council.py);
the API reads the tables read-only. Raw blocks are stored for review and
never served.
"""

from __future__ import annotations

import json

import db as db_module
from ingest import lead_facts, pc_agenda
from ingest.address_norm import parse_title_addresses

COUNCIL_WINDOW_MONTHS = 12


def kind_from_url(url: str) -> str | None:
    path = url.lower()
    if "/agendas/" in path or "agenda" in path.rsplit("/", 1)[-1]:
        return "agenda"
    if "/minutes/" in path or "minutes" in path.rsplit("/", 1)[-1]:
        return "minutes"
    return None


def store_document(conn, kind: str, url: str, sha256: str, size: int, text: str) -> dict:
    """Parse one document's text and write it. Returns a summary. An unchanged
    document (same URL, SHA-256 and parser version) is not re-parsed."""
    prev = conn.execute("SELECT doc_id, sha256, parser_version FROM pc_documents WHERE url = ?", (url,)).fetchone()
    if prev and prev["sha256"] == sha256 and prev["parser_version"] == pc_agenda.PARSER_VERSION:
        return {"url": url, "unchanged": True}
    parsed = pc_agenda.parse_agenda(text) if kind == "agenda" else pc_agenda.parse_minutes(text)
    now = db_module.now_iso()
    vals = (kind, parsed["meeting_date"], sha256, size, now, pc_agenda.PARSER_VERSION, parsed["parse_status"],
            len(parsed["items"]), json.dumps(parsed["warnings"]), json.dumps(parsed.get("notes", [])))
    if prev:
        conn.execute("UPDATE pc_documents SET kind = ?, meeting_date = ?, sha256 = ?, bytes = ?, fetched_at = ?, "
                     "parser_version = ?, parse_status = ?, n_items = ?, warnings_json = ?, notes_json = ? "
                     "WHERE doc_id = ?", (*vals, prev["doc_id"]))
        doc_id = prev["doc_id"]
    else:
        doc_id = conn.execute("INSERT INTO pc_documents (kind, meeting_date, sha256, bytes, fetched_at, parser_version, "
                              "parse_status, n_items, warnings_json, notes_json, url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (*vals, url)).lastrowid
    if parsed["parse_status"] == "failed":
        return {"url": url, "doc_id": doc_id, "parse_status": "failed", "warnings": parsed["warnings"]}
    date = parsed["meeting_date"]
    if kind == "agenda":
        for it in parsed["items"]:
            ar = it["agenda_result"] or {}
            conn.execute(
                """INSERT INTO pc_items (item_key, doc_id, meeting_date, item_label, item_numbers_json, section,
                       applicants_json, owners_json, request, notes_json, addresses, gpins_json, gpin10s_json,
                       council_district, council_district_text, accela_json, staff_planner, description,
                       city_initiated, agenda_result_raw, agenda_result, agenda_council_on, raw_block, warnings_json,
                       facts_fp, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(item_key) DO UPDATE SET doc_id = excluded.doc_id, item_label = excluded.item_label,
                       item_numbers_json = excluded.item_numbers_json, section = excluded.section,
                       applicants_json = excluded.applicants_json, owners_json = excluded.owners_json,
                       request = excluded.request, notes_json = excluded.notes_json, addresses = excluded.addresses,
                       gpins_json = excluded.gpins_json, gpin10s_json = excluded.gpin10s_json,
                       council_district = excluded.council_district,
                       council_district_text = excluded.council_district_text, accela_json = excluded.accela_json,
                       staff_planner = excluded.staff_planner, description = excluded.description,
                       city_initiated = excluded.city_initiated, agenda_result_raw = excluded.agenda_result_raw,
                       agenda_result = excluded.agenda_result, agenda_council_on = excluded.agenda_council_on,
                       raw_block = excluded.raw_block, warnings_json = excluded.warnings_json,
                       facts_fp = excluded.facts_fp, updated_at = excluded.updated_at""",
                (f"vbpc:{date}:{it['item_label']}", doc_id, date, it["item_label"], json.dumps(it["item_numbers"]),
                 it["section"], json.dumps(it["applicants"]), json.dumps(it["owners"]), it["request"],
                 json.dumps(it["notes"]), it["addresses"], json.dumps(it["gpins"]), json.dumps(it["gpin10s"]),
                 it["council_district"], it["council_district_text"], json.dumps(it["accela_records"]),
                 it["staff_planner"], it["description"], int(it["city_initiated"]), ar.get("result_raw"),
                 pc_agenda.normalize_recommendation(ar.get("result_raw")) if ar else None, ar.get("council_on"),
                 it["raw_block"], json.dumps(it["warnings"]), pc_agenda.facts_fp(it), now))
    else:
        conn.execute("DELETE FROM pc_recommendations WHERE meeting_date = ?", (date,))
        for sec in parsed["items"]:
            t = sec["tally"] or {}
            for n in sec["numbers"]:
                conn.execute(
                    "INSERT OR REPLACE INTO pc_recommendations (meeting_date, item_number, minutes_doc_id, heading, "
                    "recommendation_raw, recommendation, aye, nay, abstain, absent, raw_block) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (date, n, doc_id, sec["heading"], sec["recommendation_raw"], sec["recommendation"],
                     t.get("aye"), t.get("nay"), t.get("abstain"), t.get("absent"), sec["raw_block"]))
    return {"url": url, "doc_id": doc_id, "kind": kind, "meeting_date": date, "parse_status": parsed["parse_status"],
            "items": len(parsed["items"]), "warnings": parsed["warnings"]}


def link_council(conn) -> dict:
    """Rebuild pc_council_candidates: a Council land-use decision on the same
    parcel (GPIN10) or street address, decided after the hearing and within
    COUNCIL_WINDOW_MONTHS. Candidates only -- shown as "possible", never used
    to change any status."""
    conn.execute("DELETE FROM pc_council_candidates")
    now, n = db_module.now_iso(), 0
    items = conn.execute("SELECT item_key, meeting_date, gpin10s_json, addresses FROM pc_items").fetchall()
    for it in items:
        window = ("SELECT d.decision_key, CAST(julianday(d.meeting_date) - julianday(?) AS INTEGER) AS days "
                  "FROM land_use_decisions d WHERE d.meeting_date > ? AND d.meeting_date <= date(?, '+' || ? || ' months')")
        args = (it["meeting_date"], it["meeting_date"], it["meeting_date"], COUNCIL_WINDOW_MONTHS)
        found: dict[str, tuple[str, int]] = {}
        g10 = json.loads(it["gpin10s_json"])
        if g10:
            marks = ",".join("?" * len(g10))
            for r in conn.execute(
                    f"{window} AND (EXISTS (SELECT 1 FROM land_use_decision_addresses a WHERE "
                    f"a.decision_key = d.decision_key AND a.gpin10 IN ({marks})) OR EXISTS (SELECT 1 FROM "
                    f"decision_parcel_effective e WHERE e.decision_key = d.decision_key AND "
                    f"substr(e.display_gpin, 1, 10) IN ({marks})))", (*args, *g10, *g10)):
                found[r["decision_key"]] = ("gpin", r["days"])
        for a in parse_title_addresses(it["addresses"] or ""):
            if not a.street_norm:
                continue
            for r in conn.execute(
                    f"{window} AND EXISTS (SELECT 1 FROM land_use_decision_addresses x WHERE "
                    f"x.decision_key = d.decision_key AND x.street_norm = ? AND x.house_from <= ? AND x.house_to >= ?)",
                    (*args, a.street_norm, a.house_to, a.house_from)):
                found.setdefault(r["decision_key"], ("address", r["days"]))
        for dk, (how, days) in found.items():
            conn.execute("INSERT INTO pc_council_candidates (item_key, decision_key, matched_on, council_after_days, "
                         "linked_at) VALUES (?, ?, ?, ?, ?)", (it["item_key"], dk, how, days, now))
            n += 1
    return {"items": len(items), "candidates": n}


def public_names(names: list[str]) -> dict:
    """Organizations only; any name that may be a private person is withheld."""
    shown = [n for n in names if lead_facts.is_organization(n)]
    return {"names": shown, "withheld": len(names) - len(shown)}


def public_item(row: dict) -> dict:
    """An item as the API and CSV show it: no raw block, individuals withheld."""
    out = {k: row[k] for k in ("item_key", "meeting_date", "item_label", "section", "request", "addresses",
                               "council_district", "council_district_text", "staff_planner", "description",
                               "agenda_result_raw", "agenda_council_on") if k in row}
    out["city_initiated"] = bool(row.get("city_initiated"))
    for k in ("item_numbers", "gpins", "gpin10s", "notes", "warnings"):
        out[k] = json.loads(row.get(f"{k}_json") or "[]")
    out["accela_records"] = json.loads(row.get("accela_json") or "[]")
    out["applicants"] = public_names(json.loads(row.get("applicants_json") or "[]"))
    out["owners"] = public_names(json.loads(row.get("owners_json") or "[]"))
    out["size_from_description"] = lead_facts.scale(row.get("description"))
    return out


def item_parcels(conn, gpins: list[str]) -> list[dict]:
    """Parcels of the active Virginia Beach load for an item's GPINs (read-only).
    A 14-digit GPIN matches exactly; a 10-digit one matches its GPIN10, and is
    marked ambiguous when that is shared by several parcels (condominium units).
    Hearing, filing and Council items are Virginia Beach's: another city's
    10-digit parcel ids (Norfolk) must never answer a VB GPIN10."""
    out = []
    for g in gpins:
        exact = len(g) == 14
        rows = conn.execute(
            "SELECT p.gpin, p.zoning, p.land_use, p.prop_address, ROUND(p.tax_area_sqft / 43560.0, 4) AS acreage, "
            "(p.minx + p.maxx) / 2 AS lng, (p.miny + p.maxy) / 2 AS lat, pf.parcel_flood_class, pf.share_sfha_v, "
            "pf.share_sfha_a, pf.share_shaded_x FROM parcels p JOIN parcel_loads l ON l.load_id = p.load_id "
            "AND l.is_active = 1 AND l.locality = 'virginia_beach' LEFT JOIN parcel_flood_effective pf ON pf.gpin = p.gpin "
            f"WHERE {'p.gpin_digits = ?' if exact else 'p.gpin10 = ?'} ORDER BY p.gpin LIMIT 25",
            (g if exact else g[:10],)).fetchall()
        for r in rows:
            out.append({"agenda_gpin": g, "ambiguous": len(rows) > 1, **dict(r)})
        if not rows:
            out.append({"agenda_gpin": g, "ambiguous": False, "gpin": None})
    return out


def expected_hearings(start: str, end: str) -> list[str]:
    """Second Wednesday of every month from start to end (YYYY-MM-DD), inclusive by month."""
    from datetime import date, timedelta
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    out = []
    while (y, m) <= (ey, em):
        d = date(y, m, 1)
        first_wed = d + timedelta(days=(2 - d.weekday()) % 7)
        out.append((first_wed + timedelta(days=7)).isoformat())
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def coverage(conn, start: str = "2021-01-01", end: str | None = None) -> dict:
    """Months with an agenda / minutes found vs the expected monthly hearing.
    'verified' only when the city's index was read successfully: otherwise
    missing months may simply not have been discovered."""
    end = end or db_module.now_iso()[:10]
    found = {}
    for r in conn.execute("SELECT kind, substr(meeting_date, 1, 7) AS ym FROM pc_documents "
                          "WHERE parse_status != 'failed' AND meeting_date IS NOT NULL"):
        found.setdefault(r["ym"], set()).add(r["kind"])
    months = [d[:7] for d in expected_hearings(start, end)]
    idx = conn.execute("SELECT status, checked_at, links_found FROM pc_index_checks ORDER BY check_id DESC LIMIT 1"
                       ).fetchone()
    return {
        "from": start[:7], "to": end[:7], "months": len(months),
        "agenda_found": sum("agenda" in found.get(m, ()) for m in months),
        "minutes_found": sum("minutes" in found.get(m, ()) for m in months),
        "months_without_agenda": [m for m in months if "agenda" not in found.get(m, ())],
        "index": dict(idx) if idx else None,
        "verified": bool(idx and idx["status"] == "ok"),
        "note": None if idx and idx["status"] == "ok" else
        "the city's agenda index has not been read: missing months may exist but be undiscovered",
    }

