"""Load a city's Council votes from a VoteIQ export (ingest/council_votes.py) and
cross-check them with the city's own case record. Chesapeake only for now.

The four CSVs must have their confirmed layouts, every member vote must belong to
a vote, and each vote's member rows must equal its votes_json; otherwise nothing is
written. An identical export writes nothing. The cross-check never changes a case:
disagreements are listed in reports/council_case_review_<city>.csv.

Usage:
  python scripts/load_council_votes.py --locality chesapeake --dir exports/ [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
from ingest import council_votes as CV  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
REVIEW_COLUMNS = ("case_id", "check", "city_council_action", "city_council_date", "city_final_stage", "vote_date",
                  "motion", "result",
                  "yes", "no", "abstain", "absent", "agenda_item", "title")


def cross_check(conn, locality: str) -> tuple[Counter, list[tuple]]:
    """Per case named by a dated or undated vote: the check, and review rows for disagreements."""
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM council_votes_effective WHERE locality = ?", (locality,)):
        v = dict(r)
        for cid in json.loads(v["case_ids_json"]):
            by_case[cid].append(v)
    cases = {r["case_id"]: dict(r) for r in conn.execute(
        "SELECT case_id, cc_action, cc_date, stage FROM land_use_cases_effective WHERE locality = ?", (locality,))}
    out, review = Counter(), []
    for cid, votes in sorted(by_case.items()):
        c = cases.get(cid)
        check = CV.case_check(votes, CV.city_action(locality, c["cc_action"], c["stage"]) if c else None, c is not None)
        out[check] += 1
        if check in ("disagrees", "city_not_recorded"):
            for v in sorted(votes, key=lambda x: (x["meeting_date"] or "", x["vote_id"])):
                review.append((cid, check, c["cc_action"], c["cc_date"], c["stage"], v["meeting_date"], v["motion"],
                               v["result"],
                               v["yes"], v["no"], v["abstain"], v["absent"], v["agenda_item"], v["title"]))
    return out, review


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load a city's Council votes from a VoteIQ export")
    p.add_argument("--locality", required=True, choices=sorted(CV.LAYOUTS))
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--reports-dir", type=Path, default=BASE / "reports")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    try:
        files = CV.LAYOUTS[args.locality]["files"]
        texts = {k: (args.dir / fname).read_text(encoding="utf-8-sig") for k, (fname, _) in files.items()}
        with db_module.db_readonly() as conn:
            known = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT case_id, address, final_date FROM land_use_cases_effective WHERE locality = ?", (args.locality,))}
        data = CV.build(texts, known, args.locality)
    except (CV.ExportError, OSError, ValueError) as e:
        print(f"[FATAL] {e}; nothing written", file=sys.stderr)
        return 1
    kinds = Counter(v["motion_kind"] for v in data["votes"])
    linked = sum(1 for v in data["votes"] if v["case_ids"])
    print(f"{len(data['votes'])} votes ({data['date_min']}..{data['date_max']}, {data['undated']} undated), "
          f"{len(data['member_votes'])} member votes, {len(data['members'])} members, {len(data['agenda'])} agenda "
          f"items; motions {dict(sorted(kinds.items()))}; tally mismatches "
          f"{sum(v['tally_mismatch'] for v in data['votes'])}; votes naming a case {linked}")
    if args.dry_run:
        print("--dry-run: nothing written")
        return 0
    with db_module.db() as conn:
        active = conn.execute("SELECT load_id, content_hash FROM council_vote_loads WHERE locality = ? AND is_active = 1",
                              (args.locality,)).fetchone()
        if active and active["content_hash"] == data["hash"]:
            print(f"unchanged: identical to active load {active['load_id']}")
        else:
            conn.execute("UPDATE council_vote_loads SET is_active = 0 WHERE locality = ? AND is_active = 1",
                         (args.locality,))
            file_hashes = {fname: hashlib.sha256(texts[k].encode()).hexdigest()
                           for k, (fname, _) in files.items()}
            load_id = conn.execute(
                "INSERT INTO council_vote_loads (locality, source, files_json, votes, member_votes, members, "
                "agenda_items, undated_votes, date_min, date_max, content_hash, loaded_at, is_active) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (args.locality, CV.LAYOUTS[args.locality]["source"], json.dumps(file_hashes), len(data["votes"]), len(data["member_votes"]),
                 len(data["members"]), len(data["agenda"]), data["undated"], data["date_min"], data["date_max"],
                 data["hash"], db_module.now_iso())).lastrowid
            conn.executemany(
                "INSERT INTO council_votes (load_id, vote_id, clip_id, meeting_date, agenda_item, title, motion, "
                "motion_kind, moved_by, seconded_by, result, yes, no, abstain, absent, printed_tally, tally_mismatch, "
                "category, case_ids_json, source_conflict) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(load_id, v["vote_id"], v["clip_id"], v["meeting_date"], v["agenda_item"], v["title"], v["motion"],
                  v["motion_kind"], v["moved_by"], v["seconded_by"], v["result"], v["yes"], v["no"], v["abstain"],
                  v["absent"], v["printed_tally"], v["tally_mismatch"], v["category"], json.dumps(v["case_ids"]),
                  v["source_conflict"]) for v in data["votes"]])
            conn.executemany("INSERT INTO council_member_votes (load_id, vote_id, member, member_raw, vote) "
                             "VALUES (?,?,?,?,?)", [(load_id, m["vote_id"], m["member"], m["member_raw"], m["vote"])
                                                    for m in data["member_votes"]])
            conn.executemany("INSERT INTO council_members (load_id, name, seat) VALUES (?,?,?)",
                             [(load_id, m["name"], m["seat"]) for m in data["members"]])
            conn.executemany(
                "INSERT INTO council_agenda_items (load_id, item_id, event_id, meeting_date, item_ref, section, title, "
                "category, agenda_url, case_ids_json, lat, lng, scraped_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(load_id, a["item_id"], a["event_id"], a["meeting_date"], a["item_ref"], a["section"], a["title"],
                  a["category"], a["agenda_url"], json.dumps(a["case_ids"]), a["lat"], a["lng"], a["scraped_at"])
                 for a in data["agenda"]])
            conn.executemany(  # counts only: no speaker name, address or coordinate exists past build()
                "INSERT INTO council_testimony_counts (load_id, meeting_date, agenda_item, support, oppose, comment, "
                "present) VALUES (?,?,?,?,?,?,?)",
                [(load_id, t["meeting_date"], t["agenda_item"], t["support"], t["oppose"], t["comment"], t["present"])
                 for t in data["testimony"]])
            print(f"activated load {load_id}")
        checks, review = cross_check(conn, args.locality)
    path = args.reports_dir / f"council_case_review_{args.locality}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(REVIEW_COLUMNS)
        w.writerows(review)
    conflicts = [v for v in data["votes"] if v["source_conflict"]]
    if conflicts:
        cpath = args.reports_dir / f"council_source_conflicts_{args.locality}.csv"
        with cpath.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(("vote_id", "meeting_date", "title", "result", "printed_tally", "members_listed"))
            w.writerows((v["vote_id"], v["meeting_date"], v["title"], v["result"], v["printed_tally"],
                         "; ".join(f"{m['member']} {m['vote']}" for m in data["member_votes"] if m["vote_id"] == v["vote_id"]))
                        for v in conflicts)
        print(f"{len(conflicts)} vote(s) whose own records disagree, kept as published and flagged -> {cpath}")
    print(f"cases named by Council votes: {dict(sorted(checks.items()))}; disagreements listed -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
