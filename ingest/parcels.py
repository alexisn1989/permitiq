"""Stage 3 parcel enrichment: Virginia Beach parcels (city Property_Information
layer) -- field mapping, normalization, matching rules and parcel-polygon
flood area shares. Pure functions plus a spatial index; the CLI scripts do
all writes.

Matching (deterministic, never guessed; same GPIN rule as pipeline 008):
  permit   raw GPIN = a parcel GPIN (14 digits)          -> gpin14
           else 10-digit GPIN matching exactly one parcel -> gpin10_unique
           else point inside exactly one parcel           -> point_in_parcel
           several candidates                             -> ambiguous
  decision printed GPIN10 matching exactly one parcel     -> gpin10_unique
           else house + street = exactly one parcel's
                PROP_ADDRESS                               -> address_exact
           else geocoded point inside exactly one parcel  -> point_in_parcel
When two pieces of evidence point at different parcels the match is DISPUTED:
the primary parcel is still shown, the other is kept as alt_gpin, and the
match is excluded from same_parcel linking until a person resolves it.

Parcel flood (screening estimate, never a determination -- the city's terms
say its parcels are not valid for flood-zone determination): area shares per
class after Stage 1 precedence (VE/V > A > shaded X > other mapped), then
  hazard class  the most hazardous of V / A / shaded X with share >= min_share
  screening_sliver            hazard area present, but every hazard share < min_share
  matched_non_sfha            no hazard area and mapped share >= 1 - min_share
  screening_partial_coverage  no hazard area, mapped >= min_share, unmapped >= min_share
  no_polygon                  mapped share < min_share
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass

from shapely import STRtree, union_all
from shapely.affinity import scale
from shapely.geometry import Point, mapping, shape

from ingest.address_norm import parse_permit_street
from ingest.flood import classify_zone, polygonal

# Source field names (city layer / bulk download, per fiscalmapiq/docs/data_inventory.md).
# The loader refuses a source missing any of them; nothing here is guessed.
PARCEL_FIELD_MAP = {
    "gpin": "PAR_GPIN",
    "zoning": "ZONING",
    "land_use": "LAND_USE",
    "prop_class": "PROP_CLASS",
    "tax_area_sqft": "TAX_AREA",
    "prop_address": "PROP_ADDRESS",
    "fema_zone_bldg": "FEMA_ZONE_BLDG",
}
# Field maps per locality: a city is added here only after its parcel layer is profiled.
PARCEL_FIELD_MAPS = {"virginia_beach": PARCEL_FIELD_MAP}
LAT0 = math.radians(36.8)  # Virginia Beach; local equal-area scaling for share ratios
HAZARD_CLASSES = ("sfha_v", "sfha_a", "shaded_x")
SHARE_ORDER = ("sfha_v", "sfha_a", "shaded_x", "matched_non_sfha")


def digits(value) -> str:
    return re.sub(r"\D", "", "" if value is None else str(value))


def _prop(props: dict, name: str):
    for k, v in props.items():
        if k.upper() == name.upper():
            if v is None:
                return None
            s = str(v).strip()
            return s or None
    return None


def missing_fields(available: set[str]) -> list[str]:
    upper = {f.upper() for f in available}
    return [src for src in PARCEL_FIELD_MAP.values() if src.upper() not in upper]


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class Parcel:
    gpin: str
    attrs: dict
    geom: object
    source_rows: int

    @property
    def gpin_digits(self) -> str:
        return digits(self.gpin)

    @property
    def gpin10(self) -> str | None:
        d = self.gpin_digits
        return d[:10] if len(d) >= 10 else None


def normalize(features: list[dict], stats) -> list[Parcel]:
    """Features -> one Parcel per GPIN. Rows sharing a GPIN (multi-part
    parcels) are unioned; if their attributes differ the first row's are kept
    and the conflict is counted for the report."""
    groups: dict[str, list[tuple[dict, object]]] = defaultdict(list)
    for f in features:
        props = f.get("properties") or {}
        gpin = _prop(props, PARCEL_FIELD_MAP["gpin"])
        if not gpin or len(digits(gpin)) < 10:
            stats["skipped_no_gpin"] += 1
            continue
        geom = polygonal(shape(f["geometry"])) if f.get("geometry") else None
        if geom is None:
            stats["skipped_no_polygon"] += 1
            continue
        attrs = {k: _prop(props, src) for k, src in PARCEL_FIELD_MAP.items() if k != "gpin"}
        attrs["tax_area_sqft"] = _num(attrs["tax_area_sqft"])
        groups[gpin].append((attrs, geom))
    out = []
    for gpin, rows in sorted(groups.items()):
        if len(rows) > 1:
            stats["multipart_gpins"] += 1
            if any(a != rows[0][0] for a, _ in rows[1:]):
                stats["multipart_attr_conflicts"] += 1
        geom = polygonal(union_all([g for _, g in rows])) if len(rows) > 1 else rows[0][1]
        out.append(Parcel(gpin, rows[0][0], geom, len(rows)))
    return out


def content_hash(parcels: list[Parcel]) -> str:
    canon = [json.dumps({"g": p.gpin, "a": p.attrs, "geom": mapping(p.geom)}, sort_keys=True, default=str)
             for p in parcels]
    return hashlib.sha256("\n".join(sorted(canon)).encode()).hexdigest()


def address_parts(prop_address: str | None) -> tuple[int | None, str | None]:
    parsed = parse_permit_street(prop_address)
    return parsed if parsed else (None, None)


class ParcelIndex:
    """GPIN lookups + STRtree over one parcel load."""

    def __init__(self, rows: list[dict]):
        self.rows = rows  # dicts with gpin, gpin_digits, gpin10, house, street_norm, geom
        self.by_digits = {r["gpin_digits"]: r["gpin"] for r in rows}
        self.by_gpin10: dict[str, list[str]] = defaultdict(list)
        self.by_address: dict[tuple[int, str], list[str]] = defaultdict(list)
        for r in rows:
            if r["gpin10"]:
                self.by_gpin10[r["gpin10"]].append(r["gpin"])
            if r["house"] is not None and r["street_norm"]:
                self.by_address[(r["house"], r["street_norm"])].append(r["gpin"])
        self.tree = STRtree([r["geom"] for r in rows]) if rows else None

    def at_point(self, lat, lng) -> list[str]:
        if lat is None or lng is None or self.tree is None:
            return []
        return sorted(self.rows[i]["gpin"] for i in self.tree.query(Point(lng, lat), predicate="covered_by"))

    def within(self, geom) -> list[str]:
        """Parcels whose representative point lies inside `geom` (an application's
        boundary): a parcel only touching the boundary is not counted."""
        if geom is None or geom.is_empty or self.tree is None:
            return []
        return sorted(self.rows[i]["gpin"] for i in self.tree.query(geom)
                      if geom.covers(self.rows[i]["geom"].representative_point()))

    def by_gpin(self, raw) -> tuple[str | None, str]:
        """(gpin, how) with how in exact14 | unique10 | ambiguous10 | none."""
        d = digits(raw)
        if len(d) == 14 and d in self.by_digits:
            return self.by_digits[d], "exact14"
        if len(d) in (10, 14):
            cands = self.by_gpin10.get(d[:10], [])
            if len(cands) == 1:
                return cands[0], "unique10"
            if len(cands) > 1:
                return None, "ambiguous10"
        return None, "none"


def load_index(conn, load_id: int) -> ParcelIndex:
    rows = [{"gpin": r["gpin"], "gpin_digits": r["gpin_digits"], "gpin10": r["gpin10"], "house": r["house"],
             "street_norm": r["street_norm"], "geom": shape(json.loads(r["geom_geojson"]))}
            for r in conn.execute("SELECT gpin, gpin_digits, gpin10, house, street_norm, geom_geojson FROM parcels "
                                  "WHERE load_id = ? ORDER BY gpin", (load_id,))]
    return ParcelIndex(rows)


def fingerprint(*facts) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()


def match_permit(index: ParcelIndex, raw_gpin, lat, lng) -> dict:
    gpin, how = index.by_gpin(raw_gpin)
    pts = index.at_point(lat, lng)
    point = pts[0] if len(pts) == 1 else None
    out = {"gpin": None, "alt_gpin": None, "disputed": 0, "gpin_point_agree": None}
    if gpin is not None:
        out.update(method="gpin14" if how == "exact14" else "gpin10_unique", gpin=gpin)
        if pts:
            agree = gpin in pts
            out["gpin_point_agree"] = int(agree)
            if not agree:
                out.update(disputed=1, alt_gpin=point)
        return out
    if how == "ambiguous10":
        cands = set(index.by_gpin10[digits(raw_gpin)[:10]])
        if point is not None and point in cands:
            return {**out, "method": "point_in_parcel", "gpin": point, "gpin_point_agree": 1}
        return {**out, "method": "ambiguous"}
    if lat is None or lng is None:
        return {**out, "method": "not_geocoded_no_gpin"}
    if point is not None:
        return {**out, "method": "point_in_parcel", "gpin": point}
    return {**out, "method": "ambiguous" if len(pts) > 1 else "no_parcel"}


def match_decision_address(index: ParcelIndex, gpin10, house_from, house_to, street_norm, lat, lng) -> dict:
    evidence = []  # (method, gpin) in priority order; None gpin = ambiguous evidence
    ambiguous = False
    if gpin10:
        cands = index.by_gpin10.get(gpin10, [])
        if len(cands) == 1:
            evidence.append(("gpin10_unique", cands[0]))
        ambiguous |= len(cands) > 1
    if street_norm and house_from is not None and house_from == house_to:
        cands = index.by_address.get((house_from, street_norm), [])
        if len(cands) == 1:
            evidence.append(("address_exact", cands[0]))
        ambiguous |= len(cands) > 1
    pts = index.at_point(lat, lng)
    if len(pts) == 1:
        evidence.append(("point_in_parcel", pts[0]))
    ambiguous |= len(pts) > 1
    out = {"gpin": None, "alt_gpin": None, "disputed": 0}
    if not evidence:
        return {**out, "method": "ambiguous" if ambiguous else "no_parcel"}
    method, gpin = evidence[0]
    other = next((g for _, g in evidence[1:] if g != gpin), None)
    return {**out, "method": method, "gpin": gpin, "alt_gpin": other, "disputed": int(other is not None)}


def _area(geom) -> float:
    return scale(geom, xfact=math.cos(LAT0), yfact=1.0, origin=(0, 0)).area


def zone_class(fld_zone, zone_subty) -> str:
    return classify_zone(fld_zone, zone_subty)


class FloodShareIndex:
    """Flood zones of one load, grouped by class, for parcel area shares."""

    def __init__(self, zones: list[tuple[str, object]]):
        self.zones = zones  # (class, geom)
        self.tree = STRtree([g for _, g in zones]) if zones else None

    def shares(self, parcel_geom) -> dict:
        total = _area(parcel_geom)
        result = {c: 0.0 for c in SHARE_ORDER}
        if total <= 0:
            return {**result, "unmapped": 1.0}
        by_class = defaultdict(list)
        if self.tree is not None:
            for i in self.tree.query(parcel_geom, predicate="intersects"):
                cls, g = self.zones[i]
                by_class[cls].append(g)
        remaining = parcel_geom
        for cls in SHARE_ORDER:  # precedence: a higher class's area is removed before the next
            if not by_class[cls] or remaining.is_empty:
                continue
            part = union_all(by_class[cls]).intersection(remaining)
            result[cls] = _area(part) / total
            remaining = remaining.difference(part)
        result["unmapped"] = max(0.0, _area(remaining) / total) if not remaining.is_empty else 0.0
        return {k: round(v, 6) for k, v in result.items()}


def parcel_flood_class(shares: dict, min_share: float) -> str:
    qualifying = [c for c in HAZARD_CLASSES if shares[c] >= min_share]
    if qualifying:
        return qualifying[0]
    if any(shares[c] > 0 for c in HAZARD_CLASSES):
        return "screening_sliver"
    mapped = shares["matched_non_sfha"]
    if mapped < min_share:
        return "no_polygon"
    if shares["unmapped"] >= min_share:
        return "screening_partial_coverage"
    return "matched_non_sfha"
