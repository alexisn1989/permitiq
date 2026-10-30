"""Newport News' land-use records from the city's Operational GIS (profiled 2026-10-30):

* Land Use Case (Operational/LandUseCase/MapServer/0): 1,187 polygons of approved cases
  (Conditional Use Permit, Conditional Zoning, Special Exception, Variance, Zoning Change ...),
  CASEID (trailing spaces trimmed), CASETYPE, APPROVEDT (filled on 91). One case per CASEID.
* Site Plan (Operational/SitePlan/MapServer/0): 713 polygons with PlanType, Status (In
  Progress, Initial / Revised - Under Review, Approved, Disapproved, Completed ...), Location
  and EnerGovID -- stored as development records, status as the city prints it.

The plan Name goes through the organization rule (public_record); Comments and the staff
editor fields are never requested.
"""

from __future__ import annotations

from shapely import union_all
from shapely.geometry import shape

from ingest.flood import polygonal
from ingest.norfolk_records import _blank, _date, _prop, content_hash, geojson  # noqa: F401

LOCALITY = "newport_news"
GIS = "https://maps.nnva.gov/gis/rest/services/Operational"
SOURCES = {"nn_cases": f"{GIS}/LandUseCase/MapServer/0", "nn_site_plans": f"{GIS}/SitePlan/MapServer/0"}
FIELDS = {"nn_cases": ("CASEID", "CASETYPE", "APPROVEDT"),
          "nn_site_plans": ("Name", "PlanType", "Location", "Status", "EnerGovID")}
OUT_FIELDS = {k: "OBJECTID," + ",".join(v) for k, v in FIELDS.items()}  # Comments / editors never requested


def _merge(features, key_of, stats, label):
    groups: dict[str, list[dict]] = {}
    for f in features:
        p = f.get("properties") or {}
        oid = int(f.get("id") or _prop(p, "OBJECTID"))
        geom = polygonal(shape(f["geometry"])) if f.get("geometry") else None
        if geom is None:
            stats["unusable_geometry"] += 1
        groups.setdefault(key_of(p, oid), []).append({"oid": oid, "p": p, "geom": geom})
    for key, fs in sorted(groups.items()):
        if len(fs) > 1:
            stats[f"multi_polygon_{label}"] += 1
        polys = [x["geom"] for x in fs if x["geom"] is not None]
        geom = polygonal(union_all(polys)) if len(polys) > 1 else (polys[0] if polys else None)
        yield key, fs, geom


def normalize_cases(features: list[dict], stats) -> list[dict]:
    def key(p, oid):
        cid = _blank(_prop(p, "CASEID"))
        return f"NN-{cid}" if cid else f"NN-OID-{oid}"
    out = []
    for key_, fs, geom in _merge(features, key, stats, "cases"):
        p = fs[0]["p"]
        ctype = _blank(_prop(p, "CASETYPE"))
        approved = _date(_prop(p, "APPROVEDT"))
        pt = geom.representative_point() if geom is not None else None
        out.append({"case_id": key_, "object_ids": sorted(x["oid"] for x in fs), "case_type": ctype, "project": None,
                    "address": None, "parcel_ids": [], "housing_units": None, "residential": None, "zoning_from": None,
                    "zoning_to": None, "applied_date": None, "year_applied": None, "staff_rec": None,
                    "pc_action": None, "pc_date": None, "cc_action": None, "cc_date": None, "arc_action": None,
                    "arc_date": None, "final_raw": f"{ctype or 'Case'} approved" + (f" {approved}" if approved else
                                                                                     " (approval date not recorded)"),
                    "stage": "approved", "final_date": approved, "planner": None, "notes": None, "legacy": 0,
                    "source_link": None, "geom": geom, "lat": pt.y if pt is not None else None,
                    "lng": pt.x if pt is not None else None})
    return out


def normalize_site_plans(features: list[dict], stats) -> list[dict]:
    def key(p, oid):
        eid = _blank(_prop(p, "EnerGovID"))
        return f"NN-SP-{eid}" if eid and eid != "None" else f"NN-SP-OID-{oid}"
    out = []
    for key_, fs, geom in _merge(features, key, stats, "plans"):
        p = fs[0]["p"]
        status = _blank(_prop(p, "Status"))
        location = _blank(_prop(p, "Location"))
        pt = geom.representative_point() if geom is not None else None
        out.append({"record_id": key_, "object_ids": sorted(x["oid"] for x in fs),
                    "record_type": _blank(_prop(p, "PlanType")), "project_type": None,
                    "status": None if status == "None" else status, "details": _blank(_prop(p, "Name")),
                    "organization": None, "address": None if location == "None" else location,
                    "submitted_date": None, "approved_date": None, "expires_date": None, "parcel_ids": [],
                    "geom": geom, "lat": pt.y if pt is not None else None, "lng": pt.x if pt is not None else None})
    return out
