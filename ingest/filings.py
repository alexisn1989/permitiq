"""Land-use applications as filed (Virginia Beach "Planning Commission and City
Council Actions" layer): normalize, merge by Accela record, link parcels.

A filing is the earliest lead signal: about 11 weeks before its Planning
Commission hearing (median 77 days on the real 2026 agendas). The layer has no
status, so a filing never reads as approved; its stage comes from the PC
items that carry the same Accela record (view filing_status).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from shapely import make_valid, union_all
from shapely.geometry import mapping, shape

from ingest import arcgis, lead_facts, parcels as P

LAYER_URL = ("https://geo.vbgov.com/mapservices/rest/services/Planning_and_Development/"
             "Planning_and_Development/MapServer/1")
OUT_FIELDS = "OBJECTID,DATE_SUB,PLN_ID,PLN_FILE_NO,PROJ_NAME,APPL_NAME,PLN_TYPE,DEEPLINK"
# Only codes confirmed against real Planning Commission agenda request types (single-record
# items, 2021-2026 agendas, >= 2 agreeing items each); any other code is shown as itself.
PLAN_TYPE_LABELS = {
    "CUP": "Conditional Use Permit",
    "CRZ": "Conditional Rezoning",
    "SVR": "Subdivision Variance",
    "STC": "Street Closure",
    "MDC": "Modification of Conditions",
}


def where_since(since: str) -> str:
    return f"DATE_SUB >= DATE '{since}'"


def _prop(props: dict, name: str):
    for k, v in props.items():
        if k.upper() == name:
            if isinstance(v, str):
                v = v.strip()
            return v if v not in ("", None) else None
    return None


def _date(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(v)[:10]


def _polygonal(geom):
    geom = make_valid(geom)
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if g.geom_type in ("Polygon", "MultiPolygon")]
    return union_all(parts) if parts else None


def merge(features: list[dict], stats) -> list[dict]:
    """One filing per Accela record: polygons sharing a PLN_ID are unioned. Records
    with no filing date or no record ID are counted in stats, never returned."""
    groups: dict[str, list[dict]] = {}
    for f in features:
        p = f.get("properties") or {}
        pln = _prop(p, "PLN_ID")
        if not pln:
            stats["no_record_id"] += 1
            continue
        if not _prop(p, "DATE_SUB"):
            stats["no_date"] += 1
            continue
        groups.setdefault(pln, []).append(f)
    out = []
    for pln, fs in sorted(groups.items()):
        geoms = [g for g in (_polygonal(shape(f["geometry"])) for f in fs if f.get("geometry")) if g is not None]
        if not geoms:
            stats["no_geometry"] += 1
            continue
        geom = union_all(geoms) if len(geoms) > 1 else geoms[0]
        p = fs[0]["properties"]
        dates = sorted(_date(_prop(f["properties"], "DATE_SUB")) for f in fs)
        if len(fs) > 1:
            stats["multi_polygon_records"] += 1
        pt = geom.representative_point()
        minx, miny, maxx, maxy = geom.bounds
        out.append({
            "pln_id": pln, "objectids": sorted(f.get("id") or _prop(f["properties"], "OBJECTID") for f in fs),
            "date_sub": dates[0], "pln_type": _prop(p, "PLN_TYPE"), "pln_file_no": _prop(p, "PLN_FILE_NO"),
            "proj_name": _prop(p, "PROJ_NAME"), "appl_name": _prop(p, "APPL_NAME"), "deeplink": _prop(p, "DEEPLINK"),
            "geom": geom, "bounds": (minx, miny, maxx, maxy), "lat": pt.y, "lng": pt.x,
        })
    return out


def read_and_link(fetch, conn, url: str, where: str, label: str) -> tuple[list[dict], list[dict], Counter, int | None]:
    """Every record of an actions layer (this layer's schema: the filings layer and
    the CBPA / Wetlands Board / Historical Review Board action layers), complete
    or not at all (arcgis.IncompleteRead), merged per Accela record, with the
    parcels inside each boundary from the active parcel load.
    Returns (raw, merged, stats, parcel_load_id)."""
    raw = arcgis.fetch_all_by_ids(fetch, url, where, out_fields=OUT_FIELDS, label=label)
    stats: Counter = Counter()
    merged = merge(raw, stats)
    parcel_load = conn.execute("SELECT load_id FROM parcel_loads WHERE is_active = 1 "
                               "AND locality = 'virginia_beach'").fetchone()  # the filing layer is Virginia Beach's
    index = P.load_index(conn, parcel_load["load_id"]) if parcel_load else None
    for f in merged:
        f["gpins"] = index.within(f["geom"]) if index else []
    return raw, merged, stats, parcel_load["load_id"] if parcel_load else None


def content_hash(filings: list[dict]) -> str:
    h = hashlib.sha256()
    for f in filings:
        h.update(json.dumps([f["pln_id"], f["date_sub"], f["pln_type"], f["objectids"], f["appl_name"],
                             f["proj_name"]], default=str).encode())
        h.update(f["geom"].wkb)
    return h.hexdigest()


def geojson(geom) -> str:
    return json.dumps(mapping(geom), separators=(",", ":"))


def _public_name(name: str | None) -> dict:
    if not name:
        return {"name": None, "withheld": False}
    if lead_facts.is_organization(name):
        return {"name": name, "withheld": False}
    return {"name": None, "withheld": True}


def public_filing(row: dict) -> dict:
    """A filing as the API and CSV show it: names of private individuals withheld."""
    t = row.get("pln_type")
    return {
        "pln_id": row["pln_id"], "date_sub": row["date_sub"], "pln_type": t,
        "plan_type_label": PLAN_TYPE_LABELS.get(t or "", t or "type not recorded"),
        "pln_file_no": row.get("pln_file_no"), "applicant": _public_name(row.get("appl_name")),
        "project": _public_name(row.get("proj_name")), "deeplink": row.get("deeplink"),
        "lat": row.get("lat"), "lng": row.get("lng"), "gpins": json.loads(row.get("gpins_json") or "[]"),
    }
