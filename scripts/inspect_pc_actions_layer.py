"""Profile the Virginia Beach Planning Commission and Council Actions layer.
Read-only: writes one JSON file and nothing to the database.

Records what the layer actually holds before any ingestion is designed:
the layer description, every field (name, type, alias, domain), geometry
type, objectIdField, maxRecordCount, total count, min/max of each date
field, and sample rows (attributes only).

The meaning of a date field such as "Approval Date" is never asserted here.
With --compare-council the script adds evidence: for each date field, how many
of its values fall on a Council meeting date in land_use_decisions (read-only).
A Planning Commission hearing date would rarely coincide with a Council
meeting; a Council action date would. The conclusion is drawn by a person
from this evidence and the layer description, and recorded as such.

Usage:
  python scripts/inspect_pc_actions_layer.py --out reports/run_x/pc_actions_profile.json
  python scripts/inspect_pc_actions_layer.py --out profile.json --compare-council
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.common import fetch_json_retry  # noqa: E402

DEFAULT_URL = ("https://geo.vbgov.com/mapservices/rest/services/Planning_and_Development/"
               "Planning_and_Development/MapServer/1")
SAMPLE_ROWS = 20

fetch_json = fetch_json_retry  # indirection so tests can stub the network


class LayerError(Exception):
    pass


def _get(url: str, params: dict) -> dict:
    data = fetch_json(url, {**params, "f": "json"})
    if not isinstance(data, dict):
        raise LayerError(f"{url}: expected a JSON object, got {type(data).__name__}")
    if "error" in data:
        raise LayerError(f"{url}: ArcGIS error: {data['error']}")
    return data


def _ms_date(v) -> str | None:
    # ArcGIS dates are epoch milliseconds (UTC); the calendar date is taken in UTC
    if v is None:
        return None
    return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _date_stats(url: str, field: str) -> dict:
    try:
        data = _get(f"{url}/query", {"where": "1=1", "returnGeometry": "false", "outStatistics": json.dumps([
            {"statisticType": "min", "onStatisticField": field, "outStatisticFieldName": "mn"},
            {"statisticType": "max", "onStatisticField": field, "outStatisticFieldName": "mx"},
            {"statisticType": "count", "onStatisticField": field, "outStatisticFieldName": "n"}])})
        a = (data.get("features") or [{}])[0].get("attributes", {})
        lower = {k.lower(): v for k, v in a.items()}
        return {"min": _ms_date(lower.get("mn")), "max": _ms_date(lower.get("mx")), "non_null": lower.get("n")}
    except LayerError as e:
        return {"error": str(e)}


def _date_counts(url: str, field: str) -> dict | None:
    """value date -> row count, via groupBy statistics; None if the layer refuses it."""
    try:
        data = _get(f"{url}/query", {"where": f"{field} IS NOT NULL", "returnGeometry": "false",
                                     "groupByFieldsForStatistics": field, "outStatistics": json.dumps([
                                         {"statisticType": "count", "onStatisticField": field,
                                          "outStatisticFieldName": "n"}])})
    except LayerError:
        return None
    out: dict[str, int] = {}
    for f in data.get("features") or []:
        a = {k.lower(): v for k, v in f.get("attributes", {}).items()}
        d = _ms_date(a.get(field.lower()))
        if d:
            out[d] = out.get(d, 0) + int(a.get("n") or 0)
    if data.get("exceededTransferLimit"):
        out["_truncated"] = 1
    return out


def council_meeting_dates() -> set[str]:
    import db as db_module  # the run's DB (DATA_DIR), opened read-only
    with db_module.db_readonly() as conn:
        return {r[0][:10] for r in conn.execute("SELECT DISTINCT meeting_date FROM land_use_decisions "
                                                "WHERE meeting_date IS NOT NULL")}


def profile(url: str, compare: set[str] | None = None) -> dict:
    meta = _get(url, {})
    fields = [{"name": f.get("name"), "type": f.get("type"), "alias": f.get("alias"),
               "domain": (f.get("domain") or {}).get("name")} for f in meta.get("fields") or []]
    count = _get(f"{url}/query", {"where": "1=1", "returnCountOnly": "true"}).get("count")
    sample = _get(f"{url}/query", {"where": "1=1", "outFields": "*", "returnGeometry": "false",
                                   "resultRecordCount": SAMPLE_ROWS})
    date_fields = [f["name"] for f in fields if f["type"] == "esriFieldTypeDate"]
    out = {
        "url": url, "profiled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "name": meta.get("name"), "description": meta.get("description"),
        "copyright": meta.get("copyrightText"), "geometry_type": meta.get("geometryType"),
        "object_id_field": meta.get("objectIdField"), "max_record_count": meta.get("maxRecordCount"),
        "supports_statistics": (meta.get("advancedQueryCapabilities") or {}).get("supportsStatistics"),
        "last_edit_date": _ms_date((meta.get("editingInfo") or {}).get("lastEditDate")),
        "count": count, "fields": fields,
        "date_fields": {f: _date_stats(url, f) for f in date_fields},
        "sample": [{k: (_ms_date(v) if k in date_fields and isinstance(v, (int, float)) else v)
                    for k, v in (feat.get("attributes") or {}).items()}
                   for feat in sample.get("features") or []],
        "date_meaning": "undetermined -- decided by a person from the description and the evidence below",
    }
    if compare is not None:
        ev = {}
        for f in date_fields:
            counts = _date_counts(url, f)
            if counts is None:
                ev[f] = {"error": "groupBy statistics refused by the layer"}
                continue
            truncated = bool(counts.pop("_truncated", 0))
            total = sum(counts.values())
            on = sum(n for d, n in counts.items() if d in compare)
            ev[f] = {"values": total, "on_council_meeting_date": on,
                     "share": round(on / total, 4) if total else None, "truncated": truncated}
        out["council_date_evidence"] = {"council_meeting_dates": len(compare), "by_field": ev}
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Profile the Planning Commission and Council Actions layer (read-only)")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--compare-council", action="store_true",
                   help="add date evidence against land_use_decisions meeting dates (DB read-only)")
    args = p.parse_args(argv)
    try:
        compare = council_meeting_dates() if args.compare_council else None
        prof = profile(args.url, compare)
    except (LayerError, RuntimeError, ValueError, OSError) as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(prof, indent=1, default=str), encoding="utf-8")
    print(f"Wrote {args.out}: {prof['count']} features, geometry {prof['geometry_type']}, "
          f"{len(prof['fields'])} fields, date fields {list(prof['date_fields'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
