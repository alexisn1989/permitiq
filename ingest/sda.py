"""USDA NRCS Soil Data Access (SSURGO), read-only: the soil map units of one
survey area with the attributes that matter for earthwork and wetness.

  hydclprs     percent of the map unit made of hydric components
  wtdepannmin  minimum annual depth to the water table (cm)
  hydgrpdcd    hydrologic soil group (dominant condition)
  drclassdcd   drainage class (dominant condition)

Complete or not at all: the polygon count is read first, polygons are fetched
by map unit, and the total must match (SdaIncomplete otherwise). The survey's
own version date (sacatalog.saverest) is returned with the data.
"""

from __future__ import annotations

import re

import requests
from shapely import wkt

URL = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
TIMEOUT = 180
ATTRS = ("musym", "muname", "hydclprs", "wtdepannmin", "hydgrpdcd", "drclassdcd")
MUKEY_BATCH = 4


class SdaIncomplete(Exception):
    pass


def post(query: str) -> list[dict]:
    """One SQL query -> rows as dicts (indirection so tests can stub it)."""
    r = requests.post(URL, json={"query": query, "format": "JSON+COLUMNNAME"}, timeout=TIMEOUT)
    r.raise_for_status()
    table = (r.json() or {}).get("Table") or []
    if not table:
        return []
    cols = table[0]
    return [dict(zip(cols, row)) for row in table[1:]]


def _area(areasymbol: str) -> str:
    if not re.fullmatch(r"[A-Z]{2}\d{3}", areasymbol):
        raise ValueError(f"bad survey area symbol {areasymbol!r}")
    return areasymbol


def survey_version(areasymbol: str, query=None) -> str | None:
    query = query or post
    rows = query(f"SELECT saverest FROM sacatalog WHERE areasymbol = '{_area(areasymbol)}'")
    return rows[0]["saverest"] if rows else None


def map_units(areasymbol: str, query=None) -> dict[str, dict]:
    query = query or post
    rows = query("SELECT mu.mukey, mu.musym, mu.muname, ma.hydclprs, ma.wtdepannmin, ma.hydgrpdcd, ma.drclassdcd "
                 "FROM legend l JOIN mapunit mu ON mu.lkey = l.lkey LEFT JOIN muaggatt ma ON ma.mukey = mu.mukey "
                 f"WHERE l.areasymbol = '{_area(areasymbol)}'")
    out = {}
    for r in rows:
        a = {k: r.get(k) for k in ATTRS}
        for k in ("hydclprs", "wtdepannmin"):
            a[k] = None if a[k] in (None, "") else float(a[k])
        out[str(r["mukey"])] = a
    return out


def polygons(areasymbol: str, query=None) -> tuple[list[dict], dict]:
    """Every soil polygon of the survey area: {feature_id, geom (lon/lat), attrs}.
    Returns (rows, {"expected", "received", "version"})."""
    query = query or post
    area = _area(areasymbol)
    expected = int(query(f"SELECT COUNT(*) AS n FROM mupolygon WHERE areasymbol = '{area}'")[0]["n"])
    units = map_units(area, query)
    mukeys = sorted({str(r["mukey"]) for r in query(
        f"SELECT DISTINCT mukey FROM mupolygon WHERE areasymbol = '{area}'")})
    rows, seen = [], set()
    for i in range(0, len(mukeys), MUKEY_BATCH):
        batch = mukeys[i:i + MUKEY_BATCH]
        keys = ",".join(str(int(k)) for k in batch)
        for r in query(f"SELECT mupolygonkey, mukey, mupolygongeo.STAsText() AS wkt FROM mupolygon "
                       f"WHERE areasymbol = '{area}' AND mukey IN ({keys})"):
            fid = int(r["mupolygonkey"])
            if fid in seen:
                continue
            seen.add(fid)
            attrs = units.get(str(r["mukey"]))
            if attrs is None:
                raise SdaIncomplete(f"{area}: polygon {fid} has map unit {r['mukey']} with no attributes")
            rows.append({"feature_id": fid, "geom": wkt.loads(r["wkt"]), "attrs": {"mukey": str(r["mukey"]), **attrs}})
    if len(rows) != expected:
        raise SdaIncomplete(f"{area}: {expected} polygons counted, {len(rows)} received; nothing written")
    return rows, {"expected": expected, "received": len(rows), "version": survey_version(area, query)}
