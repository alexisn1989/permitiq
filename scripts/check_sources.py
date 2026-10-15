"""Read-only live check of every permit source PermitIQ uses or is evaluating.

Pulls each source's own metadata (Socrata view JSON / ArcGIS layer JSON) and a
small row sample, then reports: field list with types, which fields look like
contractor/applicant names or project cost, distinct values for key category
columns, last-updated time, and the license/attribution the portal declares.

Nothing is ingested and the database is never opened. Use this before writing
an adapter (or after a source changes) so field decisions rest on the live
schema, not on documentation or search snippets.

Usage:
  python scripts/check_sources.py                    # all sources
  python scripts/check_sources.py --source norfolk_row
  python scripts/check_sources.py --json reports/sources_check.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.common import fetch_json_retry  # noqa: E402

SOURCES = {
    "norfolk_permits": {
        "kind": "socrata",
        "domain": "data.norfolk.gov",
        "dataset_id": "fahm-yuh4",
        "status": "ingested (ingest/norfolk.py)",
        "distinct_fields": ["type", "use_class", "work_type", "status"],
    },
    "norfolk_permits_inspections": {
        "kind": "socrata",
        "domain": "data.norfolk.gov",
        "dataset_id": "bnrb-u445",
        "status": "candidate -- may carry permit_project_cost / contractor (unverified)",
        "distinct_fields": [],  # filled from live schema: any column named like type/kind/class
    },
    "norfolk_row": {
        "kind": "socrata",
        "domain": "data.norfolk.gov",
        "dataset_id": "mi4h-8pn3",
        "status": "candidate -- Right of Way (dumpsters, hauling/cranes, closures)",
        "distinct_fields": [],
    },
    "virginia_beach_permits": {
        "kind": "arcgis",
        "layer_url": (
            "https://services2.arcgis.com/CyVvlIiUfRBmMQuu/arcgis/rest/services/"
            "Building_Permits_Applications_view/FeatureServer/0"
        ),
        "hub_url": "https://gis.data.vbgov.com/datasets/15292e05ab3a42d5984e6033b2f6a6d4/about",
        "status": "ingested (ingest/virginia_beach.py)",
        "distinct_fields": ["PermitType", "WorkType", "ConstructionType", "Status"],
    },
}

NAME_RE = re.compile(r"contract|applicant|owner|company|business|licens|performed_for|for_whom", re.I)
COST_RE = re.compile(r"cost|valu|amount|price|estimat", re.I)
FEE_RE = re.compile(r"fee|balance|paid|payment", re.I)
CATEGORY_RE = re.compile(r"(^|_)(type|kind|class|category|use)($|_)", re.I)
SAMPLE_ROWS = 200
MAX_DISTINCT = 40


def classify_field(name: str) -> str | None:
    """'name', 'cost' or 'fee' for fields worth a human look, else None.
    Fees are kept separate on purpose: a fee is not a project valuation."""
    if NAME_RE.search(name):
        return "name"
    if FEE_RE.search(name):
        return "fee"
    if COST_RE.search(name):
        return "cost"
    return None


def fill_rate(rows: list[dict], field: str) -> float | None:
    if not rows:
        return None
    filled = sum(1 for r in rows if str(r.get(field) or "").strip() not in ("", "0", "0.0"))
    return filled / len(rows)


# ------------------------------------------------------------- socrata


def check_socrata(cfg: dict, fetch=fetch_json_retry) -> dict:
    domain, ds = cfg["domain"], cfg["dataset_id"]
    meta = fetch(f"https://{domain}/api/views/{ds}.json")
    resource = f"https://{domain}/resource/{ds}.json"
    rows = fetch(resource, params={"$limit": SAMPLE_ROWS, "$order": ":updated_at DESC"})

    fields = []
    for c in meta.get("columns", []):
        name = c.get("fieldName", "")
        if name.startswith(":"):
            continue
        fields.append({
            "name": name,
            "label": c.get("name"),
            "type": c.get("dataTypeName"),
            "description": (c.get("description") or "").strip(),
            "flag": classify_field(name) or classify_field(c.get("name") or ""),
            "sample_fill": fill_rate(rows, name),
        })

    distinct_targets = cfg["distinct_fields"] or [f["name"] for f in fields if CATEGORY_RE.search(f["name"])]
    distinct = {}
    for field in distinct_targets:
        if field not in {f["name"] for f in fields}:
            distinct[field] = "FIELD NOT IN LIVE SCHEMA"
            continue
        res = fetch(resource, params={
            "$select": f"{field}, count(*) AS n", "$group": field,
            "$order": "n DESC", "$limit": MAX_DISTINCT,
        })
        distinct[field] = [(r.get(field), int(r.get("n", 0))) for r in res]

    updated = meta.get("rowsUpdatedAt")
    license_ = meta.get("license") or {}
    return {
        "access": f"Socrata SODA API: {resource}",
        "portal_page": f"https://{domain}/d/{ds}",
        "title": meta.get("name"),
        "description": (meta.get("description") or "").strip(),
        "rows_updated_at": (datetime.fromtimestamp(updated, tz=timezone.utc).isoformat()
                            if isinstance(updated, (int, float)) else None),
        "license": license_.get("name") or meta.get("licenseId"),
        "license_link": license_.get("termsLink"),
        "attribution": meta.get("attribution"),
        "attribution_link": meta.get("attributionLink"),
        "sample_size": len(rows),
        "fields": fields,
        "distinct": distinct,
    }


# ------------------------------------------------------------- arcgis


def check_arcgis(cfg: dict, fetch=fetch_json_retry) -> dict:
    layer = cfg["layer_url"]
    meta = fetch(layer, params={"f": "json"})
    if "error" in meta:
        raise RuntimeError(f"ArcGIS error: {meta['error']}")
    q = fetch(f"{layer}/query", params={
        "where": "1=1", "outFields": "*", "f": "json",
        "resultRecordCount": SAMPLE_ROWS, "orderByFields": "OBJECTID DESC",
    })
    rows = [f.get("attributes", {}) for f in q.get("features", [])]

    fields = [{
        "name": f["name"],
        "label": f.get("alias"),
        "type": f.get("type"),
        "description": "",
        "flag": classify_field(f["name"]) or classify_field(f.get("alias") or ""),
        "sample_fill": fill_rate(rows, f["name"]),
    } for f in meta.get("fields", [])]

    distinct = {}
    names = {f["name"] for f in fields}
    for field in cfg["distinct_fields"]:
        if field not in names:
            distinct[field] = "FIELD NOT IN LIVE SCHEMA"
            continue
        res = fetch(f"{layer}/query", params={
            "where": "1=1", "outFields": field, "returnDistinctValues": "true",
            "f": "json", "resultRecordCount": MAX_DISTINCT,
        })
        distinct[field] = [(f["attributes"].get(field), None) for f in res.get("features", [])]

    last_edit = (meta.get("editingInfo") or {}).get("lastEditDate")
    return {
        "access": f"ArcGIS REST FeatureServer: {layer}/query",
        "portal_page": cfg.get("hub_url"),
        "title": meta.get("name"),
        "description": (meta.get("description") or "").strip(),
        "rows_updated_at": (datetime.fromtimestamp(last_edit / 1000, tz=timezone.utc).isoformat()
                            if isinstance(last_edit, (int, float)) else None),
        "license": "see portal page (ArcGIS layers carry license text in the Hub item)",
        "license_link": cfg.get("hub_url"),
        "attribution": meta.get("copyrightText"),
        "attribution_link": None,
        "max_record_count": meta.get("maxRecordCount"),
        "sample_size": len(rows),
        "fields": fields,
        "distinct": distinct,
    }


CHECKERS = {"socrata": check_socrata, "arcgis": check_arcgis}


def check_source(key: str, fetch=fetch_json_retry) -> dict:
    cfg = SOURCES[key]
    try:
        result = CHECKERS[cfg["kind"]](cfg, fetch=fetch)
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 -- report every source, don't stop at the first failure
        result = {"ok": False, "error": str(exc)}
    result["key"] = key
    result["status"] = cfg["status"]
    return result


def render(result: dict) -> str:
    out = [f"\n=== {result['key']}  [{result['status']}]"]
    if not result["ok"]:
        out.append(f"  FAILED: {result['error']}")
        return "\n".join(out)
    out.append(f"  title:        {result['title']}")
    out.append(f"  access:       {result['access']}")
    out.append(f"  portal page:  {result['portal_page']}")
    out.append(f"  updated:      {result['rows_updated_at']}")
    out.append(f"  license:      {result['license']}  {result['license_link'] or ''}")
    out.append(f"  attribution:  {result['attribution']}")
    out.append(f"  sample rows:  {result['sample_size']} (most recent)")
    out.append("  fields:")
    for f in result["fields"]:
        fill = "" if f["sample_fill"] is None else f"{f['sample_fill']:.0%}"
        flag = f"  <-- {f['flag'].upper()}" if f["flag"] else ""
        out.append(f"    {f['name']:<32} {str(f['type']):<24} fill={fill:<5}{flag}")
    for field, values in result["distinct"].items():
        out.append(f"  distinct {field}:")
        if isinstance(values, str):
            out.append(f"    {values}")
            continue
        for v, n in values:
            out.append(f"    {n if n is not None else '':>7}  {v}")
    return "\n".join(out)


def main(argv=None, fetch=fetch_json_retry) -> list[dict]:
    p = argparse.ArgumentParser(description="Live, read-only schema check of permit sources")
    p.add_argument("--source", choices=list(SOURCES) + ["all"], default="all")
    p.add_argument("--json", type=Path, default=None, help="also write full results as JSON")
    args = p.parse_args(argv)

    keys = list(SOURCES) if args.source == "all" else [args.source]
    results = [check_source(k, fetch=fetch) for k in keys]
    for r in results:
        print(render(r))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results, indent=2, default=str))
        print(f"\nwrote {args.json}")
    return results


if __name__ == "__main__":
    main()
