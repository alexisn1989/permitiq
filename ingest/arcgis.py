"""Complete reads of an ArcGIS feature layer, by OBJECTID.

The layer's own count and OBJECTID list come first; features are then fetched
in ID batches (the IDs travel in the URL, so batches stay small). A batch the
server fails on is split in half until it succeeds -- FEMA's NFHL answered 500
for pages of large polygons that each downloaded fine alone. A single
feature that still fails, an ID that never arrives, or a count that disagrees
with the ID list raises IncompleteRead: callers write nothing.
"""

from __future__ import annotations

from typing import Callable


class IncompleteRead(Exception):
    pass


def feature_id(f: dict, oid_field: str = "OBJECTID"):
    if f.get("id") is not None:
        return f["id"]
    props = f.get("properties") or f.get("attributes") or {}
    return next((v for k, v in props.items() if k.upper() == oid_field.upper()), None)


def fetch_all_by_ids(fetch: Callable[..., dict], url: str, where: str, *, out_fields: str = "*",
                     batch: int = 200, oid_field: str = "OBJECTID", geometry: bool = True,
                     label: str = "layer") -> list[dict]:
    """Every feature matching `where` on `url` (a layer URL, without /query),
    as GeoJSON features in WGS84. `fetch(url, params)` is the JSON getter
    (fetch_json_retry, or a test stub); it raises RuntimeError on failure."""
    q = f"{url}/query"
    expected = int(fetch(q, {"where": where, "returnCountOnly": "true", "f": "json"})["count"])
    ids = sorted(fetch(q, {"where": where, "returnIdsOnly": "true", "f": "json"}).get("objectIds") or [])
    if len(set(ids)) != len(ids) or len(ids) != expected:
        raise IncompleteRead(f"{label}: count {expected} but {len(ids)} object IDs ({len(set(ids))} unique)")
    out: list[dict] = []

    def get(chunk: list[int]) -> None:
        try:
            data = fetch(q, {"objectIds": ",".join(str(i) for i in chunk), "outFields": out_fields,
                             "returnGeometry": "true" if geometry else "false", "outSR": 4326, "f": "geojson"})
        except RuntimeError:
            if len(chunk) == 1:
                raise IncompleteRead(f"{label}: feature {chunk[0]} could not be fetched") from None
            get(chunk[:len(chunk) // 2])
            get(chunk[len(chunk) // 2:])
            return
        out.extend(data.get("features") or [])

    step = max(1, batch)
    for i in range(0, len(ids), step):
        get(ids[i:i + step])
    got = [feature_id(f, oid_field) for f in out]
    missing, dups = set(ids) - set(got), len(got) - len(set(got))
    if missing or dups or len(got) != expected:
        raise IncompleteRead(f"{label}: expected {expected} features, received {len(got)} "
                             f"({len(missing)} missing, {dups} duplicated); nothing written")
    return out
