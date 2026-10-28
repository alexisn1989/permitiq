"""Chesapeake's published development records (Chesapeake publishes no building
permits): land-use cases (Development Tracking) with Planning Commission /
City Council / ARC actions, development plans, and right-of-way permits.

Profiled read-only on 2026-10-28:
  * cases: 3,789 polygons; action fields coded by the city's own domain
    (Status_1: P Pending, A Approved, D Denied, W Withdrawn, NA Not_Applicable,
    Rt Returned, Inc Incomplete, AA Approved Amended, AP Approved with Proffers,
    AS Approved with Stipulations, Rvk Revoke) -- read from the layer metadata at
    load time, never hard-coded; FINALAPPRO is the city's final disposition
    (Approved / Withdrawn / Denied / Pending / Incomplete, plus rare values kept
    as printed under stage 'other'); 627 legacy rows were added from a 2014
    shapefile and are flagged, never shown as recent leads.
  * development plans: 1,327 points (DPLAN; In Review -> Approved -> UnderConst
    -> CompConst / Closed).
  * right-of-way permits: 1,039 points (FRAN / CROW / MROW, all Issued).

Never stored: the plan layer's `contact` (a staff login), created / edited
user fields. Project and organization names pass the organization rule
(lead_facts.is_organization) at display time; individuals are withheld.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from shapely import union_all
from shapely.geometry import mapping, shape

from ingest import lead_facts
from ingest.flood import polygonal

LOCALITY = "chesapeake"
GIS = "https://gis.cityofchesapeake.net/mapping/rest/services"
AGOL = "https://services1.arcgis.com/OAphVRUvJVpBRovs/arcgis/rest/services"
SOURCES = {
    "ches_cases": f"{GIS}/OpenData/OpenData/MapServer/9",
    "ches_dev_plans": f"{AGOL}/BuildingActivityPlans_public/FeatureServer/0",
    "ches_row_permits": f"{AGOL}/BuildingActivity_public_882dacbef60b4def9c3ab6b66d349b29/FeatureServer/0",
}
CASE_FIELDS = ("APPLICATIO", "TYPE_", "PROJECT_NA", "ST_ADD", "MAP_PARCEL", "HOUSING_UN", "RESIDENTIAL", "ZONING_FRO",
               "ZONING_TO", "DATEOFAPPL", "YR_APP", "STF_RCMNDTNS", "PC_ACTION", "PCACTDATE", "CC_ACTION", "CCACTDATE",
               "ARC_ACTION", "ARCACTDATE", "FINALAPPRO", "FINALACTIONDATE", "PLANNER", "RESULTS", "EBUILD_LINK")
ACTION_FIELDS = ("STF_RCMNDTNS", "PC_ACTION", "CC_ACTION", "ARC_ACTION")
# required in both record layers (projtype / expiredt exist only on the ROW layer: optional)
RECORD_FIELDS = ("projectid", "permittype", "permitstat", "details", "applicant", "contractor", "fulladdr",
                 "submitdt", "approvedt", "parcelid")
FINAL_STAGE = {"approved": "approved", "denied": "denied", "withdrawn": "withdrawn", "pending": "pending",
               "incomplete": "incomplete"}
LEGACY_RE = re.compile(r"^Added from Dev & Permits Shapefile", re.I)


class DomainError(Exception):
    pass


def action_domain(meta: dict) -> dict[str, str]:
    """{code: label} for the action fields, from the layer's own coded domain."""
    out: dict[str, str] = {}
    for f in meta.get("fields", []):
        if f.get("name") in ACTION_FIELDS and (f.get("domain") or {}).get("codedValues"):
            for cv in f["domain"]["codedValues"]:
                out[str(cv["code"])] = str(cv["name"]).replace("_", " ")
    if not out:
        raise DomainError("the cases layer has no coded domain for its action fields; refusing to guess codes")
    return out


def _prop(props: dict, name: str):
    v = next((v for k, v in (props or {}).items() if k.upper() == name.upper()), None)
    if isinstance(v, str):
        v = v.strip() or None
    return v


def _epoch(v) -> str | None:
    if isinstance(v, (int, float)) and v > 0:
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return None


def _mdy(v) -> str | None:
    """'4/10/2026' -> '2026-04-10'; anything else -> None (counted by the caller)."""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _action(code, domain: dict, stats, field: str):
    if code is None:
        return None
    code = str(code)
    if code not in domain:
        stats[f"unknown_{field.lower()}_code"] += 1
        raise DomainError(f"{field} code {code!r} is not in the layer's domain; nothing written")
    return code


def stage_of(final_raw: str | None) -> str:
    if final_raw is None:
        return "pending"
    return FINAL_STAGE.get(final_raw.strip().lower(), "other")


def merge_cases(features: list[dict], domain: dict, stats) -> list[dict]:
    """One case per application number; polygons sharing it are unioned."""
    groups: dict[str, list[dict]] = {}
    for f in features:
        p = f.get("properties") or {}
        cid = _prop(p, "APPLICATIO")
        if not cid:
            stats["no_case_id"] += 1
            continue
        groups.setdefault(cid, []).append(f)
    out = []
    for cid, fs in sorted(groups.items()):
        p = fs[0]["properties"]
        if len(fs) > 1:
            stats["multi_polygon_cases"] += 1
        geoms = [g for g in (polygonal(shape(f["geometry"])) for f in fs if f.get("geometry")) if g is not None]
        geom = (union_all(geoms) if len(geoms) > 1 else geoms[0]) if geoms else None
        if geom is None:
            stats["no_geometry"] += 1
        applied_raw = _prop(p, "DATEOFAPPL")
        applied = _mdy(applied_raw)
        if applied_raw and not applied:
            stats["unparsed_application_date"] += 1
        final_raw = _prop(p, "FINALAPPRO")
        stage = stage_of(final_raw)
        if stage == "other":
            stats[f"final_other:{final_raw}"] += 1
        parcels = sorted({str(_prop(f["properties"], "MAP_PARCEL")) for f in fs if _prop(f["properties"], "MAP_PARCEL")})
        pt = geom.representative_point() if geom is not None else None
        out.append({
            "case_id": cid, "object_ids": sorted(int(f.get("id") or _prop(f["properties"], "OBJECTID")) for f in fs),
            "case_type": _prop(p, "TYPE_"), "project": _prop(p, "PROJECT_NA"), "address": _prop(p, "ST_ADD"),
            "parcel_ids": parcels, "housing_units": _prop(p, "HOUSING_UN"), "residential": _prop(p, "RESIDENTIAL"),
            "zoning_from": _prop(p, "ZONING_FRO"), "zoning_to": _prop(p, "ZONING_TO"),
            "applied_date": applied, "year_applied": _prop(p, "YR_APP"),
            "staff_rec": _action(_prop(p, "STF_RCMNDTNS"), domain, stats, "STF_RCMNDTNS"),
            "pc_action": _action(_prop(p, "PC_ACTION"), domain, stats, "PC_ACTION"), "pc_date": _epoch(_prop(p, "PCACTDATE")),
            "cc_action": _action(_prop(p, "CC_ACTION"), domain, stats, "CC_ACTION"), "cc_date": _epoch(_prop(p, "CCACTDATE")),
            "arc_action": _action(_prop(p, "ARC_ACTION"), domain, stats, "ARC_ACTION"),
            "arc_date": _epoch(_prop(p, "ARCACTDATE")),
            "final_raw": final_raw, "stage": stage, "final_date": _epoch(_prop(p, "FINALACTIONDATE")),
            "planner": _prop(p, "PLANNER"), "notes": _prop(p, "RESULTS"),
            "legacy": int(bool(LEGACY_RE.match(_prop(p, "RESULTS") or ""))), "source_link": _prop(p, "EBUILD_LINK"),
            "geom": geom, "lat": pt.y if pt is not None else None, "lng": pt.x if pt is not None else None,
        })
    return out


def normalize_records(features: list[dict], source: str, stats) -> list[dict]:
    """Development plans / right-of-way permits: one row per project id."""
    out, seen = [], set()
    for f in features:
        p = f.get("properties") or {}
        rid = _prop(p, "projectid")
        if not rid:
            stats["no_record_id"] += 1
            continue
        if rid in seen:
            stats["duplicate_record_id"] += 1
            continue
        seen.add(rid)
        g = f.get("geometry")
        pt = shape(g) if g else None
        if pt is None or pt.is_empty:
            stats["no_geometry"] += 1
        orgs = [x for x in (_prop(p, "applicant"), _prop(p, "contractor")) if x]
        out.append({
            "record_id": rid, "object_ids": [int(f.get("id") or _prop(p, "objectid"))],
            "record_type": _prop(p, "permittype"), "project_type": _prop(p, "projtype"),
            "status": _prop(p, "permitstat"), "details": _prop(p, "details"),
            "organization": "; ".join(orgs) or None, "address": _prop(p, "fulladdr"),
            "submitted_date": _epoch(_prop(p, "submitdt")), "approved_date": _epoch(_prop(p, "approvedt")),
            "expires_date": _epoch(_prop(p, "expiredt")),
            "parcel_ids": [str(_prop(p, "parcelid"))] if _prop(p, "parcelid") else [],
            "lat": pt.y if pt is not None and not pt.is_empty else None,
            "lng": pt.x if pt is not None and not pt.is_empty else None,
        })
    return out


def content_hash(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps({k: v for k, v in r.items() if k != "geom"}, sort_keys=True, default=str).encode())
        if r.get("geom") is not None:
            h.update(r["geom"].wkb)
    return h.hexdigest()


def geojson(geom) -> str | None:
    return json.dumps(mapping(geom), separators=(",", ":")) if geom is not None else None


def public_name(name: str | None) -> dict:
    if not name:
        return {"name": None, "withheld": False}
    parts = [x.strip() for x in re.split(r"\s*;\s*", name) if x.strip()]
    shown = [x for x in parts if lead_facts.is_organization(x)]
    return {"name": "; ".join(shown) or None, "withheld": len(parts) - len(shown) > 0}


def public_case(row: dict, domain: dict) -> dict:
    label = lambda c: domain.get(c, c) if c else None  # noqa: E731
    return {
        "locality": row["locality"], "case_id": row["case_id"], "case_type": row["case_type"],
        "project": public_name(row["project"]), "address": row["address"],
        "parcel_ids": json.loads(row["parcel_ids_json"]), "gpins": json.loads(row["gpins_json"]),
        "housing_units": row["housing_units"], "residential": row["residential"],
        "zoning_from": row["zoning_from"], "zoning_to": row["zoning_to"], "applied_date": row["applied_date"],
        "year_applied": row["year_applied"], "staff_recommendation": label(row["staff_rec"]),
        "planning_commission": {"action": label(row["pc_action"]), "date": row["pc_date"]},
        "city_council": {"action": label(row["cc_action"]), "date": row["cc_date"]},
        "architectural_review": {"action": label(row["arc_action"]), "date": row["arc_date"]},
        "final": {"stage": row["stage"], "as_printed": row["final_raw"], "date": row["final_date"]},
        "planner": row["planner"], "notes": row["notes"], "legacy": bool(row["legacy"]),
        "source_link": row["source_link"], "lat": row["lat"], "lng": row["lng"],
    }


def public_record(row: dict) -> dict:
    return {
        "locality": row["locality"], "source": row["source"], "record_id": row["record_id"],
        "record_type": row["record_type"], "project_type": row["project_type"], "status": row["status"],
        "details": row["details"], "organization": public_name(row["organization"]), "address": row["address"],
        "submitted_date": row["submitted_date"], "approved_date": row["approved_date"],
        "expires_date": row["expires_date"], "parcel_ids": json.loads(row["parcel_ids_json"]),
        "gpins": json.loads(row["gpins_json"]), "lat": row["lat"], "lng": row["lng"],
    }
