"""A city's City Council votes from a VoteIQ export (Chesapeake: VoteIQ's scrape of
the city's Granicus meeting videos). Four CSVs, layouts confirmed on the real
export of 2026-10-29: council votes, member votes, members, upcoming agenda.

The city's own case record (Development Tracking) stays authoritative: a vote is
shown next to it and cross-checked, never used to set a case stage.

* Motion strings map to approve / deny / continue only from strings seen in the
  real export; anything else is "other" (never guessed).
* Tallies are computed from the member rows; a printed vote_count that disagrees
  is kept and flagged, not corrected.
* Member names: only the spelling pair seen in the export is merged.
* No email is stored; council members are public officials.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime

VOTE_COLUMNS = ("id", "clip_id", "meeting_date", "title", "agenda_item", "motion", "moved_by", "seconded_by",
                "result", "vote_count", "votes_json", "category", "scraped_at")
MEMBER_VOTE_COLUMNS = ("id", "vote_id", "clip_id", "meeting_date", "member_name", "vote", "agenda_item", "title",
                       "result", "category")
MEMBER_COLUMNS = ("id", "name", "district", "district_num", "email", "jurisdiction", "scraped_date")
AGENDA_COLUMNS = ("id", "event_id", "meeting_date", "item_ref", "section", "title", "category", "agenda_url",
                  "how_to_participate", "scraped_at", "lat", "lng", "geocoded_address")
FILES = {"votes": ("chesapeake_council_votes.csv", VOTE_COLUMNS),
         "member_votes": ("chesapeake_council_member_votes.csv", MEMBER_VOTE_COLUMNS),
         "members": ("chesapeake_council_members.csv", MEMBER_COLUMNS),
         "agenda": ("chesapeake_upcoming_agenda.csv", AGENDA_COLUMNS)}
SOURCE = "VoteIQ export of Chesapeake City Council meeting videos (Granicus)"

# every motion string in the real export that plainly approves, denies or continues
_APPROVE = {"approve", "approval", "approved", "approval of the request", "approval of the resolution",
            "approval of the ordinance", "approve (substitute motion)", "approve (original motion)",
            "approve (as amended)"}
_DENY = {"deny"}
_CONTINUE = {"continue", "continued"}
MEMBER_ALIASES = {"deTriquet": "de Triquet"}  # the one spelling pair seen in the export

# modern case numbers are distinctive; the older short forms (UP-10-01, S-12-34) count
# only when that exact number is in the city's case record
CASE_ID_RE = re.compile(r"\b(?:PLN|DEV)(?:-[A-Z]{2,5}){1,2}-\d{4}-\d{3,5}\b")
OLD_CASE_ID_RE = re.compile(r"\b(?:UP|SP|S|R|R\(C\)|S\(V\))-\d{2}-\d{2,3}\b")


class ExportError(Exception):
    pass


def read_csv(text: str, columns: tuple, name: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if tuple(reader.fieldnames or ()) != columns:
        raise ExportError(f"{name}: columns {reader.fieldnames} are not the confirmed layout {list(columns)}")
    return list(reader)


def motion_kind(motion: str | None) -> str:
    m = (motion or "").strip().lower()
    if m in _APPROVE:
        return "approve"
    if m in _DENY:
        return "deny"
    if m in _CONTINUE:
        return "continue"
    return "other"


def meeting_date(raw: str | None) -> str | None:
    try:
        return datetime.strptime((raw or "").strip(), "%b %d, %Y").date().isoformat()
    except ValueError:
        return None  # "unknown" in the export: kept, excluded from date logic


def case_ids(text: str, known: set[str]) -> list[str]:
    found = set(CASE_ID_RE.findall(text or ""))
    found |= {m for m in OLD_CASE_ID_RE.findall(text or "") if m in known}
    return sorted(found)


def member(name: str) -> str:
    n = (name or "").strip()
    return MEMBER_ALIASES.get(n, n)


def build(texts: dict[str, str], known_cases: set[str]) -> dict:
    """Validated rows for one export, or ExportError. Nothing is written here."""
    raw = {k: read_csv(texts[k], cols, fname) for k, (fname, cols) in FILES.items()}
    problems = []
    votes = {}
    for r in raw["votes"]:
        if r["id"] in votes:
            problems.append(f"vote id {r['id']} twice")
        if r["result"] not in ("passed", "failed", "tabled", "withdrawn"):
            problems.append(f"vote {r['id']}: result {r['result']!r} not seen before")
        try:
            listed = json.loads(r["votes_json"])
        except ValueError:
            problems.append(f"vote {r['id']}: votes_json unreadable")
            listed = {}
        votes[r["id"]] = {"row": r, "listed": {member(k): v for k, v in listed.items()}, "members": {}}
    for m in raw["member_votes"]:
        v = votes.get(m["vote_id"])
        if v is None:
            problems.append(f"member vote {m['id']} for unknown vote {m['vote_id']}")
            continue
        if m["vote"] not in ("yes", "no", "abstain", "absent"):
            problems.append(f"member vote {m['id']}: vote {m['vote']!r} not seen before")
        name = member(m["member_name"])
        if name in v["members"]:
            problems.append(f"vote {m['vote_id']}: {name} listed twice")
        v["members"][name] = (m["vote"], m["member_name"])
    for vid, v in votes.items():
        if {k: x[0] for k, x in v["members"].items()} != v["listed"]:
            problems.append(f"vote {vid}: member rows differ from votes_json")
    if problems:
        raise ExportError(f"{len(problems)} problem(s): {problems[:8]}")
    out_votes, out_members = [], []
    for vid, v in votes.items():
        r = v["row"]
        tally = {k: sum(x[0] == k for x in v["members"].values()) for k in ("yes", "no", "abstain", "absent")}
        out_votes.append({
            "vote_id": vid, "clip_id": r["clip_id"] or None, "meeting_date": meeting_date(r["meeting_date"]),
            "agenda_item": r["agenda_item"] or None, "title": r["title"] or None, "motion": r["motion"] or None,
            "motion_kind": motion_kind(r["motion"]), "moved_by": r["moved_by"] or None,
            "seconded_by": r["seconded_by"] or None, "result": r["result"], **tally,
            "printed_tally": r["vote_count"] or None,
            "tally_mismatch": int(bool(r["vote_count"]) and r["vote_count"] != f"{tally['yes']}-{tally['no']}"),
            "category": r["category"] or None,
            "case_ids": case_ids(f"{r['title']} {r['agenda_item']}", known_cases)})
        out_members += [{"vote_id": vid, "member": k, "member_raw": raw_name, "vote": x}
                        for k, (x, raw_name) in sorted(v["members"].items())]
    members = [{"name": m["name"].strip(), "seat": m["district"] or None} for m in raw["members"]]  # no email
    agenda = [{"item_id": a["id"], "event_id": a["event_id"] or None, "meeting_date": a["meeting_date"] or None,
               "item_ref": a["item_ref"] or None, "section": a["section"] or None, "title": a["title"] or None,
               "category": a["category"] or None, "agenda_url": a["agenda_url"] or None,
               "case_ids": case_ids(a["title"], known_cases),
               "lat": float(a["lat"]) if a["lat"] else None, "lng": float(a["lng"]) if a["lng"] else None,
               "scraped_at": a["scraped_at"] or None} for a in raw["agenda"]]
    dated = sorted(v["meeting_date"] for v in out_votes if v["meeting_date"])
    digest = hashlib.sha256(json.dumps([out_votes, out_members, members, agenda], sort_keys=True).encode()).hexdigest()
    return {"votes": out_votes, "member_votes": out_members, "members": members, "agenda": agenda,
            "undated": sum(1 for v in out_votes if not v["meeting_date"]),
            "date_min": dated[0] if dated else None, "date_max": dated[-1] if dated else None, "hash": digest}


# ---------- cross-check with the city's case record ----------

CITY_APPROVED = {"A", "AS", "AP", "AA"}  # the city's coded domain: Approved / w. Stipulations / w. Proffers / Amended
CITY_DENIED = {"D"}


def vote_outcome(v: dict) -> str | None:
    """approved | not_approved | None (the vote decided nothing). A failed motion to deny
    says only that the case was not denied at that vote, so it decides nothing."""
    if v["motion_kind"] == "approve" and v["result"] == "passed":
        return "approved"
    if v["motion_kind"] == "approve" and v["result"] == "failed":
        return "not_approved"
    if v["motion_kind"] == "deny" and v["result"] == "passed":
        return "not_approved"
    return None


def case_check(votes: list[dict], cc_action: str | None, in_record: bool) -> str:
    """agrees | disagrees | city_not_recorded | continued | no_decision | undated | vote_only:
    the latest dated deciding Council vote on a case against the city's own Council action.
    Never changes the case."""
    if not in_record:
        return "vote_only"
    dated = sorted((v for v in votes if v["meeting_date"]), key=lambda v: (v["meeting_date"], v["vote_id"]))
    if not dated:
        return "undated"
    deciding = [v for v in dated if vote_outcome(v)]
    if not deciding:
        return "continued" if any(v["motion_kind"] == "continue" for v in dated) else "no_decision"
    outcome = vote_outcome(deciding[-1])
    if not cc_action:
        return "city_not_recorded"  # a Council vote the city's case record does not (yet) show
    if (outcome == "approved" and cc_action in CITY_APPROVED) or (outcome == "not_approved" and cc_action in CITY_DENIED):
        return "agrees"
    return "disagrees"
