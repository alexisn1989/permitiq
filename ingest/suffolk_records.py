"""Suffolk's approved land-use items: the city's Development Restrictions layer
(Public_Website/Development_Restrictions/MapServer/0, profiled 2026-10-30) -- polygons
of approved conditional use permits, conditional zonings, rezonings, Board of Zoning
Appeals actions, variances and other restrictions, each with its APPLICATION number,
APPROVAL_DATE, Ordinance and a document link.

One case per application number (several polygons per application are merged). The
restriction type is kept as the city prints it (CUP, CZ, RZ, BZA, VAR ...); no label is
guessed. The free-text Comment and the staff editor fields are not stored.
"""

from __future__ import annotations

from shapely import union_all
from shapely.geometry import shape

from ingest.flood import polygonal
from ingest.norfolk_records import _blank, _date, _ordinance, _prop, content_hash, geojson  # noqa: F401

LOCALITY = "suffolk"
SOURCES = {"sf_restrictions": "https://suffolkgis.suffolk-va.net/hosting/rest/services/Public_Website/"
                              "Development_Restrictions/MapServer/0"}
FIELDS = ("APPLICATION", "APPROVAL_DATE", "AMEND_DATE", "RESTRICTION_TYPE", "Ordinance", "Document_Link")
OUT_FIELDS = "OBJECTID," + ",".join(FIELDS)  # Comment and editor fields are never requested


def normalize(features: list[dict], stats) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for f in features:
        p = f.get("properties") or {}
        oid = int(f.get("id") or _prop(p, "OBJECTID"))
        app = _blank(_prop(p, "APPLICATION"))
        key = f"SF-{app}" if app else f"SF-OID-{oid}"
        if not app:
            stats["restriction_without_application"] += 1
        geom = polygonal(shape(f["geometry"])) if f.get("geometry") else None
        if geom is None:
            stats["unusable_geometry"] += 1
        groups.setdefault(key, []).append({"oid": oid, "p": p, "geom": geom})
    out = []
    for key, fs in sorted(groups.items()):
        if len(fs) > 1:
            stats["multi_polygon_applications"] += 1
        polys = [x["geom"] for x in fs if x["geom"] is not None]
        geom = polygonal(union_all(polys)) if len(polys) > 1 else (polys[0] if polys else None)
        p = fs[0]["p"]
        rtype = _blank(_prop(p, "RESTRICTION_TYPE"))
        approved = _date(_prop(p, "APPROVAL_DATE"))
        amended = _date(_prop(p, "AMEND_DATE"))
        ordn = _ordinance(_prop(p, "Ordinance"))
        final_raw = f"{rtype or 'Restriction'} approved" + (f" {approved}" if approved else "") \
            + (f"; ordinance {ordn}" if ordn else "") + (f"; amended {amended}" if amended else "")
        pt = geom.representative_point() if geom is not None else None
        out.append({"case_id": key, "object_ids": sorted(x["oid"] for x in fs), "case_type": rtype, "project": None,
                    "address": None, "parcel_ids": [], "housing_units": None, "residential": None, "zoning_from": None,
                    "zoning_to": None, "applied_date": None, "year_applied": None, "staff_rec": None,
                    "pc_action": None, "pc_date": None, "cc_action": None, "cc_date": None, "arc_action": None,
                    "arc_date": None, "final_raw": final_raw, "stage": "approved", "final_date": approved,
                    "planner": None, "notes": None, "legacy": 0, "source_link": _blank(_prop(p, "Document_Link")),
                    "geom": geom, "lat": pt.y if pt is not None else None, "lng": pt.x if pt is not None else None})
    return out
