"""Council actions from the City Clerk's Summary of Council Actions: store
parsed summaries, link them to Planning Commission items, and derive each
item's Council stage.

Writers only (scripts/ingest_clerk_summaries.py); the API reads the tables
read-only. Subject text, applicant text and raw blocks are stored for review
and never served -- they hold names of private individuals.

Link rule (link_pc): same street address (street + overlapping house
number), Council date after the PC hearing and within COUNCIL_WINDOW_MONTHS,
and the most recent such hearing for that address. A link is ambiguous --
kept, shown as possible, never used for a stage -- when the request types
of the two records are both known and share none, or when several PC items
share the address and hearing and the Council item's request types do not
cover this one's, or when both name unit numbers and none agree.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

import db as db_module
from ingest import clerk_summary, lead_facts
from ingest.address_norm import parse_title_addresses

COUNCIL_WINDOW_MONTHS = 12
STAGE_OF = {"approved": "council_approved", "approved_with_conditions": "council_approved_with_conditions",
            "denied": "council_denied", "withdrawn": "council_withdrawn", "deferred": "council_deferred",
            "deferred_indefinitely": "council_deferred"}


def store_document(conn, entry: dict, folder_id: int, year: int, pages: list[str]) -> dict:
    """Parse one summary's pages and write it with its items. An unchanged
    summary (same text hash and parser version) is not re-parsed."""
    sha = clerk_summary.text_sha256(pages)
    prev = conn.execute("SELECT text_sha256, parser_version FROM clerk_documents WHERE entry_id = ?",
                        (entry["entry_id"],)).fetchone()
    if prev and prev["text_sha256"] == sha and prev["parser_version"] == clerk_summary.PARSER_VERSION:
        return {"entry_id": entry["entry_id"], "unchanged": True}
    parsed = clerk_summary.parse_summary(entry["name"], pages)
    now = db_module.now_iso()
    items = parsed["items"]
    conn.execute("DELETE FROM clerk_items WHERE entry_id = ?", (entry["entry_id"],))
    conn.execute(
        """INSERT INTO clerk_documents (entry_id, folder_id, year, name, meeting_date, page_count, pages_with_text,
               text_sha256, source_modified, fetched_at, parser_version, parse_status, n_items, n_land_use,
               warnings_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(entry_id) DO UPDATE SET folder_id = excluded.folder_id, year = excluded.year,
               name = excluded.name, meeting_date = excluded.meeting_date, page_count = excluded.page_count,
               pages_with_text = excluded.pages_with_text, text_sha256 = excluded.text_sha256,
               source_modified = excluded.source_modified, fetched_at = excluded.fetched_at,
               parser_version = excluded.parser_version, parse_status = excluded.parse_status,
               n_items = excluded.n_items, n_land_use = excluded.n_land_use, warnings_json = excluded.warnings_json""",
        (entry["entry_id"], folder_id, year, entry["name"], parsed["meeting_date"], len(pages),
         parsed["pages_with_text"], sha, entry.get("modified"), now, clerk_summary.PARSER_VERSION,
         parsed["parse_status"], len(items), sum(1 for i in items if i["is_land_use"]),
         json.dumps(parsed["warnings"])))
    for it in items:
        conn.execute(
            """INSERT INTO clerk_items (item_key, entry_id, meeting_date, page, item_label, label_certain, is_land_use,
                   request_types_json, applicant_text, council_district, motion_raw, outcome, deferred_to, aye, nay,
                   tally_raw, subject, raw_block, warnings_json, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (it["item_key"], entry["entry_id"], parsed["meeting_date"], it["page"], it["item_label"],
             int(it["label_certain"]), int(it["is_land_use"]), json.dumps(it["request_types"]), it["applicant_text"],
             it["council_district"], it["motion_raw"], it["outcome"], it["deferred_to"], it["aye"], it["nay"],
             it["tally_raw"], it["subject"], it["raw_block"], json.dumps(it["warnings"]), now))
        seq = 0
        for a in it["addresses"]:
            if not a["street_norm"]:
                continue
            seq += 1
            conn.execute("INSERT INTO clerk_item_addresses (item_key, seq, raw_text, house_from, house_to, "
                         "street_norm) VALUES (?,?,?,?,?,?)",
                         (it["item_key"], seq, a["raw_text"], a["house_from"], a["house_to"], a["street_norm"]))
    return {"entry_id": entry["entry_id"], "meeting_date": parsed["meeting_date"],
            "parse_status": parsed["parse_status"], "items": len(items),
            "land_use": sum(1 for i in items if i["is_land_use"]), "warnings": parsed["warnings"]}


_UNIT_RE = re.compile(r"\b(?:Units?|Suites?|Ste\.?|Apt\.?)\s*#?\s*([A-Z0-9-]{1,6}(?:\s*(?:&|and|,)\s*[A-Z0-9-]{1,6})*)",
                      re.IGNORECASE)


def units(text: str | None) -> set[str]:
    """Unit / suite numbers printed in a text ("Units A&B" -> {"A", "B"})."""
    out = set()
    for m in _UNIT_RE.finditer(text or ""):
        out.update(u.upper() for u in re.split(r"\s*(?:&|and|,)\s*", m.group(1), flags=re.IGNORECASE) if u)
    return out


def _pc_types(row) -> set[str]:
    return set(clerk_summary.request_types(" ".join(x for x in (row["request"], row["description"]) if x)))


def link_pc(conn) -> dict:
    """Rebuild clerk_pc_links and pc_council_outcomes from the stored items."""
    conn.execute("DELETE FROM pc_council_outcomes")
    conn.execute("DELETE FROM clerk_pc_links")
    now = db_module.now_iso()
    pcs = conn.execute("SELECT item_key, meeting_date, addresses, request, description FROM pc_items").fetchall()
    by_street: dict[str, list[tuple]] = defaultdict(list)
    for r in pcs:
        for a in parse_title_addresses(r["addresses"] or ""):
            if a.street_norm:
                by_street[a.street_norm].append((r, a.house_from, a.house_to))
    clerk = conn.execute("SELECT item_key, meeting_date, request_types_json, subject FROM clerk_items WHERE is_land_use = 1 "
                         "AND outcome != 'unmapped'").fetchall()
    n_links = n_amb = 0
    for c in clerk:
        cands: dict[str, object] = {}
        for a in conn.execute("SELECT house_from, house_to, street_norm FROM clerk_item_addresses WHERE item_key = ?",
                              (c["item_key"],)):
            for r, lo, hi in by_street.get(a["street_norm"], []):
                if lo is None or a["house_from"] is None or lo > a["house_to"] or hi < a["house_from"]:
                    continue
                days = conn.execute("SELECT CAST(julianday(?) - julianday(?) AS INTEGER)",
                                    (c["meeting_date"], r["meeting_date"])).fetchone()[0]
                if days <= 0 or days > COUNCIL_WINDOW_MONTHS * 31:
                    continue
                cands[r["item_key"]] = (r, days)
        if not cands:
            continue
        latest = max(r["meeting_date"] for r, _ in cands.values())
        ctypes = set(json.loads(c["request_types_json"]))
        at_latest = [k for k, (r, _) in cands.items() if r["meeting_date"] == latest]
        typed = {k: _pc_types(cands[k][0]) for k in at_latest}
        # several PC items at that hearing and address: the ones whose request types the
        # Council item covers (one combined item can decide a rezoning and its CUP)
        covered = [k for k in at_latest if typed[k] and ctypes and typed[k] <= ctypes]
        cunits = units(c["subject"])
        for key, (r, days) in cands.items():
            ptypes = _pc_types(r)
            punits = units(r["addresses"])
            reason = None
            if cunits and punits and not cunits & punits:
                reason = f"different unit ({', '.join(sorted(cunits))} vs {', '.join(sorted(punits))})"
            elif r["meeting_date"] != latest:
                reason = "a later Planning Commission hearing at the same address"
            elif ctypes and ptypes and not ctypes & ptypes:
                reason = f"request types differ ({', '.join(sorted(ctypes))} vs {', '.join(sorted(ptypes))})"
            elif len(at_latest) > 1 and key not in covered:
                reason = "several Planning Commission items at the same address and hearing"
            conn.execute("INSERT INTO clerk_pc_links (clerk_item_key, pc_item_key, matched_on, days_after_hearing, "
                         "ambiguous, reason, linked_at) VALUES (?,?,?,?,?,?,?)",
                         (c["item_key"], key, "address_type" if ctypes & ptypes else "address", days,
                          int(reason is not None), reason, now))
            n_links += 1
            n_amb += reason is not None
    n_out = derive_outcomes(conn, now)
    return {"pc_items": len(pcs), "clerk_land_use_items": len(clerk), "links": n_links, "ambiguous": n_amb,
            "pc_items_with_council_stage": n_out}


def derive_outcomes(conn, now: str) -> int:
    """The latest Council action per PC item (non-ambiguous links only). If
    actions on that date disagree (e.g. a rezoning approved and a CUP
    denied), the stage is council_conflicting -- never one of them."""
    rows = conn.execute(
        "SELECT l.pc_item_key, c.item_key, c.meeting_date, c.outcome, c.deferred_to FROM clerk_pc_links l "
        "JOIN clerk_items c ON c.item_key = l.clerk_item_key WHERE l.ambiguous = 0 "
        "ORDER BY l.pc_item_key, c.meeting_date, c.item_key").fetchall()
    per: dict[str, list] = defaultdict(list)
    for r in rows:
        per[r["pc_item_key"]].append(r)
    for key, acts in per.items():
        last_date = acts[-1]["meeting_date"]
        last = [a for a in acts if a["meeting_date"] == last_date]
        stages = {STAGE_OF[a["outcome"]] for a in last}
        stage = stages.pop() if len(stages) == 1 else "council_conflicting"
        conn.execute("INSERT INTO pc_council_outcomes (pc_item_key, clerk_item_key, council_date, stage, deferred_to, "
                     "n_actions, computed_at) VALUES (?,?,?,?,?,?,?)",
                     (key, last[-1]["item_key"], last_date, stage,
                      last[-1]["deferred_to"] if stage == "council_deferred" else None, len(acts), now))
    return len(per)


def link_decisions(conn) -> dict:
    """Rebuild clerk_decision_links: a Council land-use decision (VoteIQ data)
    on the same meeting date and street address as a Clerk item -- the
    official record to check that decision against."""
    conn.execute("DELETE FROM clerk_decision_links")
    now = db_module.now_iso()
    n = conn.execute(
        "INSERT OR IGNORE INTO clerk_decision_links (clerk_item_key, decision_key, linked_at) "
        "SELECT DISTINCT c.item_key, d.decision_key, ? FROM clerk_items c "
        "JOIN clerk_item_addresses ca ON ca.item_key = c.item_key "
        "JOIN land_use_decisions d ON d.meeting_date = c.meeting_date "
        "JOIN land_use_decision_addresses da ON da.decision_key = d.decision_key AND da.street_norm = ca.street_norm "
        "AND da.house_from <= ca.house_to AND da.house_to >= ca.house_from WHERE c.is_land_use = 1", (now,)).rowcount
    return {"links": n}


def public_item(row: dict) -> dict:
    """A Clerk item as the API shows it: no subject text, raw block or names."""
    from ingest import edocs
    out = {k: row[k] for k in ("item_key", "meeting_date", "page", "item_label", "council_district", "motion_raw",
                               "outcome", "deferred_to", "aye", "nay") if k in row}
    out["label_certain"] = bool(row.get("label_certain", 1))
    out["request_types"] = json.loads(row.get("request_types_json") or "[]")
    out["warnings"] = json.loads(row.get("warnings_json") or "[]")
    text = re.sub(r"\s+for\s*(?:an?)?\s*$", "", row.get("applicant_text") or "")  # OCR leftovers ("... for a")
    parts = [x.strip(" ,") for x in text.split("/") if x.strip(" ,")]
    out["applicant"] = public_names(parts)
    if row.get("entry_id"):
        out["source_url"] = edocs.doc_url(row["entry_id"])
    return out


def public_names(parts: list[str]) -> dict:
    """Organizations only ("APPLICANT / OWNER" parts checked one by one)."""
    shown = [n for n in parts if lead_facts.is_organization(n)]
    return {"names": shown, "withheld": len(parts) - len(shown)}


def coverage(conn) -> dict:
    """Summaries stored vs listed in the Clerk's archive, per year (latest check)."""
    rows = conn.execute(
        "SELECT c.year, c.listed, c.stored, c.status, c.checked_at FROM clerk_index_checks c "
        "WHERE c.check_id = (SELECT MAX(x.check_id) FROM clerk_index_checks x WHERE x.year = c.year) "
        "ORDER BY c.year").fetchall()
    years = [dict(r) for r in rows]
    return {"years": years, "verified": bool(years) and all(y["status"] == "ok" for y in years),
            "note": None if years else "the Clerk's archive has not been read"}
