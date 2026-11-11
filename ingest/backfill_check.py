"""Prove a Virginia Beach permit backfill is complete (first real run).

`--since` is a requested window, not evidence. The adapter pages by
resultOffset ordered on ApplicationDate, which is not a unique order, so a
page can skip or repeat rows. This check asks the source itself, with the
SAME date field and where clause, for its count and full OBJECTID set, and
compares them with the database:

  truly_missing      source OBJECTIDs whose PermitNumber is nowhere in the DB
                     -> fetched by ID and upserted (top-up), then re-checked
  dup_permit_number  source OBJECTIDs whose PermitNumber the DB already holds
                     under another OBJECTID (one DB row per permit number) --
                     not missing, reported
  no_permit_number   source rows without a PermitNumber -- the adapter skips
                     these by design; reported
  extra_in_db        DB rows in the window whose OBJECTID the source no longer
                     returns (deleted or re-dated at source); reported -- they
                     cannot create false "no permit yet" leads
  dup_objectids_db   the same OBJECTID on two DB rows (should never happen)
  null_date_source   source rows with no ApplicationDate: no date window can
                     ever return them; reported as outside the window
Verified only when the source is self-consistent (count = |OBJECTID set|),
truly_missing = 0 and dup_objectids_db = 0. Per application year, source and
DB counts are listed side by side.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date

from ingest import virginia_beach as vb

ID_BATCH = vb.PAGE_SIZE


def where_since(since: str) -> str:
    return f"ApplicationDate >= '{since.replace('-', '/')}'"


def source_count(where: str) -> int:
    return int(vb.query_json({"where": where, "returnCountOnly": "true"})["count"])


def source_ids(where: str) -> list[int]:
    return [int(x) for x in (vb.query_json({"where": where, "returnIdsOnly": "true"}).get("objectIds") or [])]


def source_permit_numbers(ids: list[int]) -> dict[int, str | None]:
    out = {}
    for i in range(0, len(ids), ID_BATCH):
        resp = vb.query_json({"objectIds": ",".join(str(x) for x in ids[i:i + ID_BATCH]),
                              "outFields": "OBJECTID,PermitNumber", "returnGeometry": "false"})
        for f in resp.get("features") or []:
            a = f.get("attributes", {})
            pn = a.get("PermitNumber")
            out[int(a["OBJECTID"])] = str(pn).strip() if pn and str(pn).strip() else None
    return out


def db_state(conn, since: str) -> tuple[Counter, set[str], dict[str, int]]:
    ids = Counter()
    for r in conn.execute("SELECT json_extract(raw_json, '$.OBJECTID') FROM permits WHERE locality = 'virginia_beach' "
                          "AND applied_date >= ?", (since,)):
        if r[0] is not None:
            ids[int(r[0])] += 1
    numbers = {r[0] for r in conn.execute("SELECT source_permit_id FROM permits WHERE locality = 'virginia_beach'")}
    per_year = dict(conn.execute("SELECT substr(applied_date, 1, 4), COUNT(*) FROM permits WHERE locality = "
                                 "'virginia_beach' AND applied_date >= ? GROUP BY 1", (since,)).fetchall())
    return ids, numbers, per_year


def check(conn, since: str, today: date | None = None) -> dict:
    where = where_since(since)
    n = source_count(where)
    s_ids = source_ids(where)
    s_set = set(s_ids)
    db_ids, numbers, db_year = db_state(conn, since)
    missing = sorted(s_set - set(db_ids))
    pn = source_permit_numbers(missing) if missing else {}
    no_number = [i for i in missing if pn.get(i) is None]
    dup_number = [i for i in missing if pn.get(i) is not None and pn[i] in numbers]
    truly = [i for i in missing if pn.get(i) is not None and pn[i] not in numbers]
    years = range(int(since[:4]), (today or date.today()).year + 1)
    per_year = [{"year": str(y), "source": source_count(f"{where} AND ApplicationDate LIKE '{y}/%'"),
                 "db_permits": db_year.get(str(y), 0)} for y in years]
    null_dates = source_count("ApplicationDate IS NULL OR ApplicationDate = ''")
    return {
        "where": where, "source_count": n, "source_ids": len(s_set), "source_ids_listed": len(s_ids),
        "db_ids": len(db_ids), "truly_missing": truly, "dup_permit_number": dup_number,
        "no_permit_number": no_number, "extra_in_db": sorted(set(db_ids) - s_set),
        "dup_objectids_db": sorted(k for k, c in db_ids.items() if c > 1), "null_date_source": null_dates,
        "per_year": per_year,
        "verified": int(n == len(s_set) == len(s_ids) and not truly and not any(c > 1 for c in db_ids.values())),
    }


def record(conn, run_id: int, result: dict, topped_up: int, ts: str) -> int:
    return conn.execute(
        """INSERT INTO permit_backfill_checks (backfill_run_id, where_clause, source_count, source_ids, db_ids,
               truly_missing, dup_permit_number, no_permit_number, extra_in_db, dup_objectids_db, null_date_source,
               topped_up, per_year_json, verified, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, result["where"], result["source_count"], result["source_ids"], result["db_ids"],
         len(result["truly_missing"]), len(result["dup_permit_number"]), len(result["no_permit_number"]),
         len(result["extra_in_db"]), len(result["dup_objectids_db"]), result["null_date_source"], topped_up,
         json.dumps(result["per_year"]), result["verified"], ts)).lastrowid


def latest(conn, run_id: int):
    return conn.execute("SELECT * FROM permit_backfill_checks WHERE backfill_run_id = ? ORDER BY check_id DESC LIMIT 1",
                        (run_id,)).fetchone()
