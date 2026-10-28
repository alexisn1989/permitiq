"""Site constraints: city GIS layers that limit what a site can do, measured
per parcel (screening estimates, never a determination).

  polygon_share  acres and share of the parcel inside the layer's polygons
  line_crossing  length of the layer's lines inside the parcel, and the
                 distance from the parcel to the nearest line

The Chesapeake Bay RPA layer is a line layer: profiled on the real data
(2026-10-28) its 2,060 untyped lines are the RPA boundary, not closed buffer
areas (1 closed ring; polygonizing yields 72 fragments a median ~49 ft wide).
So no RPA acres are ever claimed -- only "the RPA boundary crosses / lies
within N ft of this parcel".

Distances and areas use a local projection in feet around Virginia Beach
(the same equirectangular approach as ingest/parcels.py; < 0.5% error at
the city's scale), not survey-grade geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely import STRtree, union_all
from shapely.geometry import mapping, shape

from ingest.flood import polygonal

FT_PER_DEG = 364_567.2  # feet per degree of latitude
COS0 = math.cos(math.radians(36.8))  # Virginia Beach
SQFT_PER_ACRE = 43_560
NEAR_FT = 100  # a line within this distance of a parcel is reported as near

ENV = "https://geo.vbgov.com/mapservices/rest/services/Planning_and_Development/Environmental/MapServer"
PLAN = "https://geo.vbgov.com/mapservices/rest/services/Planning_and_Development/Planning_and_Development/MapServer"
UTIL = "https://geo.vbgov.com/mapservices/rest/services/Public_Utilities/Other_Utility_Information/MapServer"


@dataclass(frozen=True)
class Layer:
    key: str
    url: str
    kind: str  # polygon_share | line_crossing
    label: str
    meaning: str
    attrs: tuple[str, ...] = field(default_factory=tuple)  # source fields kept; nothing else is stored
    group_by: str | None = None  # an attribute to break the overlap down by (e.g. soil type)
    classification: bool = False  # covers the whole city (soils): report what is mapped, not "inside the layer"
    near_ft: float | None = None  # polygon layers: also report the distance to the nearest polygon within this


LAYERS = {l.key: l for l in (
    Layer("rpa", f"{ENV}/1", "line_crossing", "Chesapeake Bay RPA boundary",
          "Chesapeake Bay Preservation Act Resource Protection Area boundary (the city's line). Land in the RPA "
          "(within 100 ft of tidal shores, tidal wetlands and perennial streams) cannot be cleared, graded or "
          "built on without an exception."),
    Layer("southern_watersheds_buffer", f"{ENV}/2", "polygon_share", "Southern Watersheds buffer area",
          "Buffer area of the Southern Watersheds Management Ordinance (development limits apply)."),
    Layer("special_floodplain", f"{ENV}/3", "polygon_share", "Floodplains subject to special restrictions",
          "City floodplain areas with development restrictions beyond FEMA's.", ("FLD_ZONE", "FLOODWAY")),
    Layer("shrink_swell_soils", f"{ENV}/4", "polygon_share", "Shrink-swell soils",
          "Soils that expand and shrink with moisture: foundation design and earthwork cost.",
          ("SOIL_NAME", "SOIL_DESCR", "SOIL_TYPE"), "SOIL_NAME"),
    Layer("soils", f"{ENV}/6", "polygon_share", "Soils (city soil survey)",
          "Soil map units from the city's soil survey. Hydric rating and depth to water table are not in this "
          "layer.", ("SOIL_NAME", "SOIL_DESCR", "SOIL_TYPE"), "SOIL_NAME", classification=True),
    Layer("historic_overlay", f"{PLAN}/24", "polygon_share", "Historic and Cultural Overlay District",
          "Work needs Historical Review Board review in addition to the standard process.", ("Name",), "Name"),
    Layer("historic_kempsville", f"{PLAN}/25", "polygon_share", "Historic Kempsville Area Overlay District",
          "Design standards and review apply in the overlay.", ("Name",), "Name"),
    # Utility project-file index (Public Utilities): areas of past or planned city projects, dates mostly not
    # recorded. A signal that utility work happened or is planned here -- never pipe capacity.
    Layer("city_sewer_projects", f"{UTIL}/3", "polygon_share", "City sewer project area",
          "Area of a city sewer project on file (project-file index; not pipe locations or capacity).",
          ("PU_NUM", "PROJECT_NAME"), "PROJECT_NAME"),
    Layer("city_water_projects", f"{UTIL}/6", "polygon_share", "City water project area",
          "Area of a city water project on file (project-file index; not pipe locations or capacity).",
          ("PU_NUM", "PROJECT_NAME"), "PROJECT_NAME"),
    Layer("water_sewer_projects", f"{UTIL}/9", "polygon_share", "City water and sewer project area",
          "Area of a combined city water and sewer project on file (project-file index).",
          ("PU_NUM", "PROJECT_NAME"), "PROJECT_NAME"),
    Layer("sewer_pump_station_projects", f"{UTIL}/18", "polygon_share", "Sewer pump station project",
          "Site of a city sewer pump station project on file. Distance is to the project site, not a service "
          "area; capacity is not published.", ("PU_NUM", "PROJECT_NAME"), "PROJECT_NAME", near_ft=1000),
    Layer("dam_break", f"{PLAN}/23", "polygon_share", "Dam break inundation zone",
          "Mapped inundation area if a dam fails (state dam-safety rules can limit development).",
          ("NAME", "HAZARD_LEVEL"), "NAME"),
)}

BOARD_LAYERS = {
    "cbpa": (f"{PLAN}/3", "Chesapeake Bay Preservation Area Board actions"),
    "wetlands_board": (f"{PLAN}/5", "Wetlands Board actions and waterfront permits"),
    "hrb": (f"{PLAN}/4", "Historical Review Board actions"),
}


def to_feet(geom):
    """Lon/lat geometry -> local feet (x east, y north)."""
    return shapely.transform(geom, lambda xy: xy * np.array([COS0 * FT_PER_DEG, FT_PER_DEG]))


def keep_attrs(layer: Layer, props: dict) -> dict:
    out = {}
    for name in layer.attrs:
        v = next((v for k, v in (props or {}).items() if k.upper() == name.upper()), None)
        if isinstance(v, str):
            v = v.strip() or None
        out[name] = v
    return out


def normalize(layer: Layer, features: list[dict], stats) -> list[dict]:
    """GeoJSON features -> {feature_id, geom (lon/lat), attrs}. Empty or wrong
    geometry is counted, never stored."""
    out = []
    for f in features:
        g = f.get("geometry")
        if not g:
            stats["no_geometry"] += 1
            continue
        geom = shape(g)
        if layer.kind == "polygon_share":
            geom = polygonal(geom)
        elif geom.geom_type not in ("LineString", "MultiLineString"):
            geom = None
        if geom is None or geom.is_empty:
            stats["unusable_geometry"] += 1
            continue
        fid = f.get("id")
        if fid is None:
            fid = next((v for k, v in (f.get("properties") or {}).items() if k.upper() == "OBJECTID"), None)
        out.append({"feature_id": int(fid), "geom": geom, "attrs": keep_attrs(layer, f.get("properties"))})
    return out


def content_hash(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: r["feature_id"]):
        h.update(json.dumps([r["feature_id"], r["attrs"]], sort_keys=True, default=str).encode())
        h.update(r["geom"].wkb)
    return h.hexdigest()


def geojson(geom) -> str:
    return json.dumps(mapping(geom), separators=(",", ":"))


class LayerIndex:
    """One active load of one layer, in feet, for per-parcel measurements."""

    def __init__(self, layer: Layer, rows: list[dict]):
        self.layer = layer
        self.rows = rows  # {feature_id, geom (lon/lat), attrs}
        self.ft = [to_feet(r["geom"]) for r in rows]
        self.tree = STRtree(self.ft) if rows else None

    def measure(self, parcel_geom) -> dict | None:
        """The parcel's measurement for this layer, or None when it does not touch
        it (and, for lines, no line is within NEAR_FT)."""
        if self.tree is None:
            return None
        p = to_feet(parcel_geom)
        area = p.area
        if self.layer.kind == "polygon_share":
            hits = [i for i in self.tree.query(p, predicate="intersects")]
            inter = union_all([self.ft[i] for i in hits]).intersection(p) if hits and area > 0 else None
            overlap = inter.area if inter is not None else 0.0
            if overlap <= 0:
                return self._nearest(p) if self.layer.near_ft else None
            detail = {}
            if self.layer.group_by:
                by: dict[str, float] = {}
                for i in hits:
                    part = self.ft[i].intersection(p).area
                    if part > 0:
                        k = self.rows[i]["attrs"].get(self.layer.group_by) or "unnamed"
                        by[k] = by.get(k, 0.0) + part
                detail["by"] = [{"name": k, "acres": round(v / SQFT_PER_ACRE, 3), "share": round(v / area, 4)}
                                for k, v in sorted(by.items(), key=lambda kv: -kv[1])]
            return {"overlap_sqft": round(overlap, 1), "share": round(min(1.0, overlap / area), 4),
                    "line_length_ft": None, "distance_ft": None, "detail": detail}
        near = [i for i in self.tree.query(p, predicate="dwithin", distance=NEAR_FT)]
        if not near:
            return None
        lines = union_all([self.ft[i] for i in near])
        inside = lines.intersection(p).length
        dist = 0.0 if inside > 0 else p.distance(lines)
        return {"overlap_sqft": None, "share": None, "line_length_ft": round(inside, 1),
                "distance_ft": round(dist, 1), "detail": {}}


    def _nearest(self, p) -> dict | None:
        """No overlap: the distance to the nearest polygon within the layer's near_ft."""
        near = list(self.tree.query(p, predicate="dwithin", distance=self.layer.near_ft))
        if not near:
            return None
        d = {i: p.distance(self.ft[i]) for i in near}
        i = min(d, key=d.get)
        return {"overlap_sqft": None, "share": 0.0, "line_length_ft": None, "distance_ft": round(d[i], 1),
                "detail": {"nearest": self.rows[i]["attrs"].get(self.layer.group_by) if self.layer.group_by else None}}


def load_layer_index(conn, layer: Layer) -> tuple[int, LayerIndex] | None:
    load = conn.execute("SELECT load_id FROM constraint_loads WHERE layer_key = ? AND is_active = 1",
                        (layer.key,)).fetchone()
    if load is None:
        return None
    rows = [{"feature_id": r["feature_id"], "geom": shape(json.loads(r["geom_geojson"])),
             "attrs": json.loads(r["attrs_json"])}
            for r in conn.execute("SELECT feature_id, geom_geojson, attrs_json FROM constraint_features "
                                  "WHERE load_id = ? ORDER BY feature_id", (load["load_id"],))]
    return load["load_id"], LayerIndex(layer, rows)


def describe(layer_key: str, row: dict) -> str:
    """One measured fact, in plain words."""
    layer = LAYERS[layer_key]
    if layer.classification:
        by = json.loads(row.get("detail_json") or "{}").get("by", []) if isinstance(row.get("detail_json"), str) \
            else (row.get("detail") or {}).get("by", [])
        parts = [f"{b['name']} {b['acres']:,.2f} ac ({b['share'] * 100:.0f}%)" for b in by[:4]]
        return "Mapped: " + "; ".join(parts) if parts else "mapped"
    if layer.kind == "line_crossing":
        if row["line_length_ft"]:
            return f"{layer.label} crosses the parcel ({row['line_length_ft']:,.0f} ft of line inside it)"
        return f"{layer.label} lies {row['distance_ft']:,.0f} ft from the parcel"
    if not row["overlap_sqft"]:
        return f"{layer.label} {row['distance_ft']:,.0f} ft from the parcel"
    acres = row["overlap_sqft"] / SQFT_PER_ACRE
    return f"{acres:,.2f} ac ({row['share'] * 100:.0f}%) of the parcel inside the {layer.label.lower()}"


def public_constraints(conn, gpins: list[str]) -> dict:
    """The site-constraint block for a set of parcels (API and CSV): measured
    facts per layer, the boards' history on those parcels, which layers were
    checked and what is not covered. No raw attributes beyond each layer's
    allowlist; applicant names in board actions only for organizations."""
    from ingest import filings as F  # local import: filings pulls in lead_facts
    loaded = {r["layer_key"]: r["loaded_at"] for r in conn.execute(
        "SELECT layer_key, loaded_at FROM constraint_loads WHERE is_active = 1")}
    items = []
    if gpins:
        marks = ",".join("?" * len(gpins))
        for r in conn.execute(
                f"SELECT gpin, layer_key, overlap_sqft, share, line_length_ft, distance_ft, detail_json, "
                f"layer_loaded_at, source_url FROM parcel_constraints_effective WHERE gpin IN ({marks}) "
                f"ORDER BY layer_key, gpin", gpins):
            d = dict(r)
            if d["layer_key"] not in LAYERS:
                continue
            layer = LAYERS[d["layer_key"]]
            items.append({"gpin": d["gpin"], "layer": layer.key, "classification": layer.classification, "label": layer.label, "kind": layer.kind,
                          "meaning": layer.meaning, "fact": describe(layer.key, d),
                          "acres": round(d["overlap_sqft"] / SQFT_PER_ACRE, 3) if d["overlap_sqft"] else None,
                          "share": d["share"], "line_length_ft": d["line_length_ft"], "distance_ft": d["distance_ft"],
                          "detail": json.loads(d["detail_json"]), "as_of": d["layer_loaded_at"],
                          "source_url": d["source_url"]})
    items.sort(key=lambda x: (x["classification"], x["layer"], x["gpin"]))  # constraints first, soils last
    history = []
    if gpins:
        for r in conn.execute(
                f"SELECT DISTINCT a.layer_key, a.pln_id, a.date_sub, a.pln_type, a.appl_name, a.deeplink "
                f"FROM board_action_parcels bp JOIN board_actions_effective a ON a.load_id = bp.load_id "
                f"AND a.pln_id = bp.pln_id WHERE bp.gpin IN ({marks}) ORDER BY a.date_sub DESC", gpins):
            history.append({"board": r["layer_key"], "board_label": BOARD_LAYERS.get(r["layer_key"], ("", ""))[1],
                            "pln_id": r["pln_id"], "date_sub": r["date_sub"], "pln_type": r["pln_type"],
                            "applicant": F._public_name(r["appl_name"]), "deeplink": r["deeplink"]})
    boards = {r["layer_key"]: r["loaded_at"] for r in conn.execute(
        "SELECT layer_key, loaded_at FROM board_action_loads WHERE is_active = 1")}
    return {
        "constraints": items, "board_history": history,
        "layers_checked": [{"layer": k, "label": LAYERS[k].label, "as_of": v} for k, v in sorted(loaded.items())
                           if k in LAYERS],
        "boards_checked": [{"board": k, "label": BOARD_LAYERS[k][1], "as_of": v} for k, v in sorted(boards.items())
                           if k in BOARD_LAYERS],
        "not_covered": ["sewer / water capacity and pipe locations (not published)", "hydric soils and water table (USDA, pending)",
                        "NWI wetlands (pending)", "VDOT traffic (pending)", "state / National Register historic "
                        "boundaries (pending)"],
        "notice": "Screening estimates from the city's GIS layers, not a determination: confirm with a site survey "
                  "and the city's RPA / wetlands determination. Names of private individuals are not shown.",
    }


CSV_COLUMNS = ("rpa", "southern_watersheds_buffer_share", "special_floodplain_share", "shrink_swell_share",
               "sewer_pump_station_project_ft", "utility_project_area",
               "historic_overlay", "dam_break", "prior_cbpa_actions", "prior_wetlands_board_actions",
               "prior_hrb_actions")


def csv_summary(conn, gpins: list[str]) -> dict:
    """Flat constraint columns for a lead's parcels (CSV): the largest share over
    the parcels, RPA as crosses / within N ft / blank, counts of prior board
    actions. Blank means not touching (or the layer not loaded)."""
    out = {c: "" for c in CSV_COLUMNS}
    if not gpins:
        return out
    marks = ",".join("?" * len(gpins))
    best: dict[str, dict] = {}
    for r in conn.execute(f"SELECT layer_key, share, line_length_ft, distance_ft FROM parcel_constraints_effective "
                          f"WHERE gpin IN ({marks})", gpins):
        k = r["layer_key"]
        if k == "rpa":
            cur = best.get(k)
            if cur is None or (r["distance_ft"] or 0) < (cur["distance_ft"] or 0):
                best[k] = dict(r)
        elif (r["share"] or 0) > (best.get(k, {}).get("share") or 0):
            best[k] = dict(r)
    ps = conn.execute(f"SELECT MIN(distance_ft), MAX(overlap_sqft) FROM parcel_constraints_effective WHERE "
                      f"layer_key = 'sewer_pump_station_projects' AND gpin IN ({marks})", gpins).fetchone()
    if ps[0] is not None or ps[1]:
        out["sewer_pump_station_project_ft"] = 0 if ps[1] else round(ps[0])
    out["utility_project_area"] = " ".join(k for k in ("city_sewer_projects", "city_water_projects",
                                                       "water_sewer_projects") if (best.get(k) or {}).get("share"))
    if "rpa" in best:
        b = best["rpa"]
        out["rpa"] = "crosses" if b["line_length_ft"] else f"within {b['distance_ft']:.0f} ft"
    for col, key in (("southern_watersheds_buffer_share", "southern_watersheds_buffer"),
                     ("special_floodplain_share", "special_floodplain"), ("shrink_swell_share", "shrink_swell_soils")):
        if key in best:
            out[col] = round(best[key]["share"], 4)
    for col, keys in (("historic_overlay", ("historic_overlay", "historic_kempsville")), ("dam_break", ("dam_break",))):
        hit = [best[k]["share"] for k in keys if k in best]
        if hit:
            out[col] = round(max(hit), 4)
    for col, board in (("prior_cbpa_actions", "cbpa"), ("prior_wetlands_board_actions", "wetlands_board"),
                       ("prior_hrb_actions", "hrb")):
        out[col] = conn.execute(
            f"SELECT COUNT(DISTINCT a.pln_id) FROM board_action_parcels bp JOIN board_actions_effective a "
            f"ON a.load_id = bp.load_id AND a.pln_id = bp.pln_id WHERE a.layer_key = ? AND bp.gpin IN ({marks})",
            (board, *gpins)).fetchone()[0]
    return out
