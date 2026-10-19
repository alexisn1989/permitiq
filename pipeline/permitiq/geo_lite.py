"""Address geocoding fallback for permits whose ArcGIS record has no usable
coordinates. Adapted from VoteIQ's voteiq/services/geo_lite.py: Census
Geocoder first (free, no key), Nominatim second, and a bounds check on every
result -- a wrong pin is worse than no pin, so anything outside Virginia
Beach is rejected rather than stored.

The returned dict carries "source" ('census' | 'nominatim') so the caller can
record geocode_source.
"""
from __future__ import annotations

import os
import time

import requests

_CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_UA = os.getenv("NOMINATIM_USER_AGENT", "permitiq/0.1")

# Padded bounding box around the City of Virginia Beach (lon/lat, WGS84).
# Coarse sanity check, not a boundary test.
VB_LON_MIN, VB_LON_MAX = -76.30, -75.80
VB_LAT_MIN, VB_LAT_MAX = 36.50, 37.05


def in_bounds(lat: float, lon: float) -> bool:
    return VB_LAT_MIN <= lat <= VB_LAT_MAX and VB_LON_MIN <= lon <= VB_LON_MAX


def geocode_lite(address: str) -> dict | None:
    """Return {"lat", "lng", "matched_address", "source"} or None."""
    try:
        r = requests.get(
            _CENSUS_URL,
            params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
            timeout=10,
        )
        matches = r.json().get("result", {}).get("addressMatches", [])
        if matches:
            c = matches[0]["coordinates"]
            lat, lng = float(c["y"]), float(c["x"])
            if in_bounds(lat, lng):
                return {
                    "lat": lat,
                    "lng": lng,
                    "matched_address": matches[0].get("matchedAddress", address),
                    "source": "census",
                }
    except Exception:
        pass

    try:
        time.sleep(1)  # Nominatim usage policy: max 1 req/sec
        r = requests.get(
            _NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": _NOMINATIM_UA},
            timeout=15,
        )
        results = r.json()
        if results:
            lat, lng = float(results[0]["lat"]), float(results[0]["lon"])
            if in_bounds(lat, lng):
                return {
                    "lat": lat,
                    "lng": lng,
                    "matched_address": results[0].get("display_name", address),
                    "source": "nominatim",
                }
    except Exception:
        pass

    return None
