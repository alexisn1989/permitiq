"""FEMA flood-zone classification shared by the Stage 1 CLI scripts.

Pure functions plus a spatial index -- no network, no database writes. The
loader (scripts/load_flood_zones.py) and the tagger
(scripts/tag_flood_zones.py) use it; the read-only API only reads what they
stored (and freshness comes from the permit_flood_effective view in db.py).

flood_class (stored per permit):
  sfha_v            matched VE / V
  sfha_a            matched AE / other A* (AO, AH, A99, AR, A)
  shaded_x          matched X with the 0.2 PCT annual chance subtype
  matched_non_sfha  matched any other FEMA polygon (unshaded X, D, open water...)
  no_polygon        geocoded, but NO FEMA polygon matched -- a possible
                    coverage gap, never reported as "outside the flood zone"
  not_geocoded      no coordinates
Two more are derived at read time only: untagged, out_of_scope.

When polygons overlap, the most hazardous wins: VE/V > AE > other A >
shaded X > other (same order as the PostGIS pipeline's views).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from shapely import STRtree
from shapely.geometry import MultiPolygon, Point, Polygon, box, mapping, shape
from shapely.validation import make_valid

# Display grid: fixed 0.05-degree cells (~5 km at 36.8N; Virginia Beach is ~110 cells).
CELL_SIZE = 0.05
# Hard per-cell payload limit for /api/flood-zones/{x}/{y}.geojson.
MAX_FLOOD_CELL_BYTES = 2 * 1024 * 1024
# Display simplification: start fine, double until every cell fits, stop at the cap.
SIMPLIFY_START = 0.00005
SIMPLIFY_CAP = 0.0008
# Only these classes are drawn: unshaded X covers most of the city and would
# bloat the layer without telling a reader anything.
DISPLAY_CLASSES = ("sfha_v", "sfha_a", "shaded_x")
SFHA_CLASSES = ("sfha_v", "sfha_a")
MATCHED_CLASSES = ("sfha_v", "sfha_a", "shaded_x", "matched_non_sfha")


def classify_zone(fld_zone: str | None, zone_subty: str | None) -> str:
    z = (fld_zone or "").strip().upper()
    if z in ("VE", "V"):
        return "sfha_v"
    if z.startswith("A") and not z.startswith("AREA"):  # 'AREA NOT INCLUDED' is not an A zone
        return "sfha_a"
    if z == "X" and "0.2 PCT" in (zone_subty or "").upper():
        return "shaded_x"
    return "matched_non_sfha"


def zone_rank(fld_zone: str | None, zone_subty: str | None) -> int:
    """Lower is more hazardous."""
    z = (fld_zone or "").strip().upper()
    if z in ("VE", "V"):
        return 1
    if z == "AE":
        return 2
    if z.startswith("A") and not z.startswith("AREA"):
        return 3
    if z == "X" and "0.2 PCT" in (zone_subty or "").upper():
        return 4
    return 5


def polygonal(geom) -> Polygon | MultiPolygon | None:
    """make_valid, then keep only polygonal parts (make_valid can return a
    GeometryCollection with stray lines/points). None if nothing is left."""
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = make_valid(geom)
    if isinstance(geom, (Polygon, MultiPolygon)):
        return None if geom.is_empty else geom
    parts = []
    for g in getattr(geom, "geoms", []):
        if isinstance(g, Polygon):
            parts.append(g)
        elif isinstance(g, MultiPolygon):
            parts.extend(g.geoms)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


@dataclass
class Zone:
    id: int
    fld_zone: str | None
    zone_subty: str | None
    sfha_tf: str | None
    geom: object

    @property
    def flood_class(self) -> str:
        return classify_zone(self.fld_zone, self.zone_subty)

    @property
    def sfha(self) -> int:
        return 1 if self.flood_class in SFHA_CLASSES else 0


class FloodIndex:
    """STRtree over a load's polygons. classify() answers one point."""

    def __init__(self, zones: list[Zone]):
        self.zones = zones
        self.tree = STRtree([z.geom for z in zones]) if zones else None

    def classify(self, lat: float | None, lng: float | None) -> tuple[str, Zone | None]:
        if lat is None or lng is None:
            return "not_geocoded", None
        if self.tree is None:
            return "no_polygon", None
        # covered_by: a point exactly on a polygon edge counts as inside.
        hits = [self.zones[i] for i in self.tree.query(Point(lng, lat), predicate="covered_by")]
        if not hits:
            return "no_polygon", None
        best = min(hits, key=lambda z: (zone_rank(z.fld_zone, z.zone_subty), z.fld_zone or "", z.id))
        return best.flood_class, best


def content_hash(features: list[dict]) -> str:
    """SHA-256 over the canonical, sorted feature list (zone attributes +
    geometry). Identifies a dataset revision independently of FEMA's vintage
    labels: same hash = same data."""
    canon = sorted(
        json.dumps({"z": f["fld_zone"], "s": f["zone_subty"], "t": f["sfha_tf"], "g": f["geometry"]},
                   sort_keys=True, separators=(",", ":"))
        for f in features
    )
    return hashlib.sha256("\n".join(canon).encode()).hexdigest()


def cell_of(lng: float, lat: float) -> tuple[int, int]:
    return math.floor(lng / CELL_SIZE), math.floor(lat / CELL_SIZE)


def cell_bbox(cx: int, cy: int) -> tuple[float, float, float, float]:
    return (round(cx * CELL_SIZE, 6), round(cy * CELL_SIZE, 6),
            round((cx + 1) * CELL_SIZE, 6), round((cy + 1) * CELL_SIZE, 6))


def _round_coords(obj, nd: int = 6):
    if isinstance(obj, (list, tuple)):
        return [_round_coords(v, nd) for v in obj]
    return round(obj, nd) if isinstance(obj, float) else obj


def build_display_cells(zones: list[Zone], tolerance: float) -> dict[tuple[int, int], bytes]:
    """Clip the drawable zones to the fixed grid at one simplification
    tolerance. Returns {(cx, cy): exact response bytes} for non-empty cells."""
    drawable = [z for z in zones if z.flood_class in DISPLAY_CLASSES]
    simplified = []
    for z in drawable:
        g = polygonal(z.geom.simplify(tolerance, preserve_topology=True))
        if g is not None:
            simplified.append((z, g))
    per_cell: dict[tuple[int, int], list[dict]] = {}
    for z, g in simplified:
        minx, miny, maxx, maxy = g.bounds
        x0, y0 = cell_of(minx, miny)
        x1, y1 = cell_of(maxx, maxy)
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                part = polygonal(g.intersection(box(*cell_bbox(cx, cy))))
                if part is None:
                    continue
                m = mapping(part)
                per_cell.setdefault((cx, cy), []).append({
                    "type": "Feature",
                    "properties": {"flood_class": z.flood_class, "fld_zone": z.fld_zone, "zone_subty": z.zone_subty},
                    "geometry": {"type": m["type"], "coordinates": _round_coords(m["coordinates"])},
                })
    out = {}
    for key, feats in per_cell.items():
        feats.sort(key=lambda f: (f["properties"]["flood_class"], f["properties"]["fld_zone"] or ""))
        out[key] = json.dumps({"type": "FeatureCollection", "features": feats},
                              sort_keys=True, separators=(",", ":")).encode()
    return out


def fit_display_cells(zones: list[Zone], max_cell_bytes: int = MAX_FLOOD_CELL_BYTES):
    """Escalate the tolerance (x2) until every cell fits under the limit.
    Returns (cells, tolerance, ok). ok is False if even SIMPLIFY_CAP leaves an
    oversized cell -- such a load must not be activated."""
    tol = SIMPLIFY_START
    while True:
        cells = build_display_cells(zones, tol)
        if all(len(b) <= max_cell_bytes for b in cells.values()):
            return cells, tol, True
        if tol >= SIMPLIFY_CAP:
            return cells, tol, False
        tol = min(tol * 2, SIMPLIFY_CAP)


def etag_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def empty_cell_bytes() -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": []}, sort_keys=True, separators=(",", ":")).encode()


def zone_from_feature(i: int, feature: dict) -> Zone | None:
    """GeoJSON feature (properties already normalized by the loader) -> Zone."""
    geom = polygonal(shape(feature["geometry"])) if feature.get("geometry") else None
    if geom is None:
        return None
    return Zone(i, feature["fld_zone"], feature["zone_subty"], feature["sfha_tf"], geom)
