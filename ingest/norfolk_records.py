"""Norfolk's approved land-use items from the city's OpenData GIS (profiled 2026-10-29):

* Conditional Use Permits (Planning/MapServer/5): points, one per site, each with the
  City Council ORDINANCE that granted it, EFFECTIVE_DATE and, for some, EXPIRATION_DATE.
* Conditional Zoning (Planning/MapServer/3): polygons, each with its ORDINANCE.

Both layers hold approved items only (an ordinance is the Council's approval); Norfolk
publishes no pending-case pipeline. One case per ordinance: the 30 CUP ordinances that
cover several sites are merged. A CUP without an ordinance is kept by its object id with
stage "other". The applicant is not stored (the layer's APPLICANT can be a private
person and there is no field it would honestly fill); NOTES are not stored either.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from shapely.geometry import MultiPoint, shape
from shapely import union_all

from ingest.chesapeake_records import _prop, content_hash, geojson  # noqa: F401  (re-exported for the loader)
from ingest.flood import polygonal

LOCALITY = "norfolk"
GIS = "https://gisshare.norfolk.gov/pubserver/rest/services/OpenData/Planning/MapServer"
SOURCES = {"nf_cups": f"{GIS}/5", "nf_conditional_zoning": f"{GIS}/3"}
CUP_FIELDS = ("ORDINANCE", "TYPE", "ADDRESS", "EFFECTIVE_DATE", "EXPIRATION_DATE", "LINK", "CPCITEM")
CZ_FIELDS = ("ORDINANCE", "ITEMNO", "URL")


def _date(ms) -> str | None:
    if ms in (None, ""):
        return None
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).date().isoformat()


def _ordinance(v) -> str | None:
    if v is None or not str(v).strip():
        return None
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(v).strip()
    except ValueError:
        return str(v).strip()


def _blank(v) -> str | None:
    return str(v).strip() if v is not None and str(v).strip() else None


def _row(case_id, object_ids, case_type, address, ordinance, effective, expires, link, geom, today: date):
    if ordinance:
        final_raw = f"Approved by City Council ordinance {ordinance}"
        if expires:
            final_raw += f"; {'expired' if expires < today.isoformat() else 'expires'} {expires}"
        stage = "approved"
    else:
        final_raw, stage = "No ordinance recorded in the city layer", "other"
    pt = geom.representative_point() if geom is not None else None
    return {"case_id": case_id, "object_ids": object_ids, "case_type": case_type, "project": None, "address": address,
            "parcel_ids": [], "housing_units": None, "residential": None, "zoning_from": None, "zoning_to": None,
            "applied_date": None, "year_applied": None, "staff_rec": None, "pc_action": None, "pc_date": None,
            "cc_action": "Approved by ordinance" if ordinance else None, "cc_date": effective,
            "arc_action": None, "arc_date": None, "final_raw": final_raw, "stage": stage, "final_date": effective,
            "planner": None, "notes": None, "legacy": 0, "source_link": link, "geom": geom,
            "lat": pt.y if pt is not None else None, "lng": pt.x if pt is not None else None}


def normalize_cups(features: list[dict], stats, today: date | None = None) -> list[dict]:
    today = today or date.today()
    groups: dict[str, list[dict]] = {}
    for f in features:
        p = f.get("properties") or {}
        oid = int(f.get("id") or _prop(p, "OBJECTID"))
        ordn = _ordinance(_prop(p, "ORDINANCE"))
        key = f"NF-CUP-{ordn}" if ordn else f"NF-CUP-OID-{oid}"
        if not ordn:
            stats["cup_without_ordinance"] += 1
        groups.setdefault(key, []).append({"oid": oid, "p": p, "geom": shape(f["geometry"]) if f.get("geometry") else None})
    out = []
    for key, fs in sorted(groups.items()):
        if len(fs) > 1:
            stats["multi_site_ordinances"] += 1
        p = fs[0]["p"]
        pts = [x["geom"] for x in fs if x["geom"] is not None]
        geom = pts[0] if len(pts) == 1 else (MultiPoint(pts) if pts else None)
        addresses = sorted({a for a in (_blank(_prop(x["p"], "ADDRESS")) for x in fs) if a})
        out.append(_row(key, sorted(x["oid"] for x in fs), _blank(_prop(p, "TYPE")) or "Conditional Use Permit",
                        "; ".join(addresses) or None, _ordinance(_prop(p, "ORDINANCE")),
                        _date(_prop(p, "EFFECTIVE_DATE")), _date(_prop(p, "EXPIRATION_DATE")),
                        _blank(_prop(p, "LINK")), geom, today))
    return out


def normalize_conditional_zoning(features: list[dict], stats, today: date | None = None) -> list[dict]:
    today = today or date.today()
    groups: dict[str, list[dict]] = {}
    for f in features:
        p = f.get("properties") or {}
        oid = int(f.get("id") or _prop(p, "OBJECTID"))
        ordn = _ordinance(_prop(p, "ORDINANCE"))
        key = f"NF-CZ-{ordn}" if ordn else f"NF-CZ-OID-{oid}"
        geom = polygonal(shape(f["geometry"])) if f.get("geometry") else None
        if geom is None:
            stats["cz_unusable_geometry"] += 1
        groups.setdefault(key, []).append({"oid": oid, "p": p, "geom": geom})
    out = []
    for key, fs in sorted(groups.items()):
        if len(fs) > 1:
            stats["multi_polygon_ordinances"] += 1
        polys = [x["geom"] for x in fs if x["geom"] is not None]
        geom = polygonal(union_all(polys)) if len(polys) > 1 else (polys[0] if polys else None)
        p = fs[0]["p"]
        out.append(_row(key, sorted(x["oid"] for x in fs), "Conditional Zoning", None, _ordinance(_prop(p, "ORDINANCE")),
                        None, None, _blank(_prop(p, "URL")), geom, today))
    return out
