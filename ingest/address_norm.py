"""Deterministic address / GPIN extraction for Stage 2 land-use linkage.

Council item titles carry the applicant, the property address(es) and
sometimes printed GPINs as free text, e.g.
  "Application of ACME LLC for a Change of Zoning ... at 3853 Shore Drive
   (GPIN 1489-28-6485) DISTRICT 4"
Permits carry raw_json.StreetAddress ("3853 SHORE DR") and raw_json.GPIN
(14 digits). Both sides are reduced to (house number, normalized street) so
they compare exactly. Nothing here is fuzzy: an address either parses into a
house number + a street ending in a known suffix, or it is not extracted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# USPS-style suffix abbreviations (the form VB permits use).
SUFFIXES = {
    "STREET": "ST", "ST": "ST", "AVENUE": "AVE", "AVE": "AVE", "AV": "AVE",
    "ROAD": "RD", "RD": "RD", "DRIVE": "DR", "DR": "DR", "BOULEVARD": "BLVD", "BLVD": "BLVD",
    "LANE": "LN", "LN": "LN", "COURT": "CT", "CT": "CT", "CIRCLE": "CIR", "CIR": "CIR",
    "PLACE": "PL", "PL": "PL", "PARKWAY": "PKWY", "PKWY": "PKWY", "HIGHWAY": "HWY", "HWY": "HWY",
    "TERRACE": "TER", "TER": "TER", "TRAIL": "TRL", "TRL": "TRL", "WAY": "WAY",
    "CRESCENT": "CRES", "CRES": "CRES", "LANDING": "LNDG", "LNDG": "LNDG", "QUAY": "QUAY",
    "ARCH": "ARCH", "RUN": "RUN", "LOOP": "LOOP", "POINT": "PT", "PT": "PT", "KEY": "KY",
    "SQUARE": "SQ", "SQ": "SQ", "TURNPIKE": "TPKE", "TPKE": "TPKE", "EXPRESSWAY": "EXPY",
    "COVE": "CV", "CV": "CV", "BEND": "BND", "BND": "BND", "CROSSING": "XING", "XING": "XING",
}
DIRECTIONALS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "N": "N", "S": "S", "E": "E", "W": "W"}

_SUFFIX_ALT = "|".join(sorted({re.escape(k) for k in SUFFIXES}, key=len, reverse=True))
_NUM = r"\d{1,6}"
# house numbers: "3853", "1200-1212", "1200 through 1212", "1205, 1209 and 1213"
_HOUSES = rf"(?P<houses>{_NUM}(?:\s*(?:-|through|thru|to)\s*{_NUM})?(?:\s*(?:,|&|and)\s*{_NUM})*)"
# street name: 1-4 words (letters, ordinals like 58th, apostrophes), then a known suffix
_STREET = rf"(?P<street>(?:(?:[A-Za-z][A-Za-z'.]*|\d{{1,3}}(?:st|nd|rd|th))\s+){{1,4}}?)(?P<suffix>{_SUFFIX_ALT})\b\.?"
_ADDRESS_RE = re.compile(rf"(?<![\w$.,-]){_HOUSES}\s+{_STREET}", re.IGNORECASE)
_GPIN_RE = re.compile(r"(?<!\d)(\d{4})-?(\d{2})-?(\d{4})(?:-?(\d{4}))?(?!\d)")
_GPIN_CONTEXT_RE = re.compile(r"GPINs?\b[^A-Za-z\d]{0,3}((?:[\d\s,;&-]|and)+)", re.IGNORECASE)
_PO_BOX_RE = re.compile(r"\bBOX\s*$", re.IGNORECASE)
_STOPWORDS = {"OF", "AT", "ON", "THE", "AND", "FROM", "TO", "FOR", "IN", "A", "AN"}


@dataclass(frozen=True)
class ParsedAddress:
    raw_text: str
    house_from: int | None
    house_to: int | None
    street_norm: str | None
    gpin10: str | None


def norm_street(name: str, suffix: str) -> str | None:
    words = [w.strip(".'").upper() for w in name.split() if w.strip(".'")]
    while words and words[0] in _STOPWORDS:
        words.pop(0)
    if not words:
        return None
    if words[0] in DIRECTIONALS and len(words) > 1:
        words[0] = DIRECTIONALS[words[0]]
    return " ".join(words + [SUFFIXES[suffix.upper().rstrip(".")]])


def gpin10(value: str | None) -> str | None:
    """First 10 digits of a GPIN (VB parcels: 10 printed, 14 stored)."""
    digits = re.sub(r"\D", "", value or "")
    return digits[:10] if len(digits) in (10, 14) else None


def _house_numbers(text: str) -> list[tuple[int, int]]:
    out = []
    for part in re.split(r"\s*(?:,|&|\band\b)\s*", text, flags=re.IGNORECASE):
        m = re.fullmatch(rf"({_NUM})(?:\s*(?:-|through|thru|to)\s*({_NUM}))?", part.strip(), re.IGNORECASE)
        if not m:
            continue
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if hi < lo:
            lo, hi = hi, lo
        out.append((lo, hi))
    return out


def parse_title_addresses(title: str | None) -> list[ParsedAddress]:
    """All street addresses and printed GPINs in a title, de-duplicated, in
    order of appearance. GPINs are only read after the word 'GPIN' so other
    10-digit numbers (phone numbers, ordinance numbers) are never taken."""
    if not title:
        return []
    out: list[ParsedAddress] = []
    seen = set()
    for m in _ADDRESS_RE.finditer(title):
        if _PO_BOX_RE.search(title[:m.start()]):
            continue  # "P.O. Box 55 Main Street" is a mailing address, not the property
        street = norm_street(m.group("street"), m.group("suffix"))
        if street is None:
            continue
        for lo, hi in _house_numbers(m.group("houses")):
            key = ("addr", lo, hi, street)
            if key not in seen:
                seen.add(key)
                out.append(ParsedAddress(m.group(0).strip(), lo, hi, street, None))
    for ctx in _GPIN_CONTEXT_RE.finditer(title):
        for g in _GPIN_RE.finditer(ctx.group(1)):
            g10 = "".join(g.group(1, 2, 3))
            if ("gpin", g10) not in seen:
                seen.add(("gpin", g10))
                out.append(ParsedAddress(g.group(0), None, None, None, g10))
    return out


_PERMIT_ADDR_RE = re.compile(rf"^\s*({_NUM})[A-Z]?\s+(.+?)\s+({_SUFFIX_ALT})\.?\b", re.IGNORECASE)


def parse_permit_street(street_address: str | None) -> tuple[int, str] | None:
    """raw_json.StreetAddress ("3853 SHORE DR", "110 58TH ST") -> (3853, 'SHORE DR')."""
    if not street_address:
        return None
    m = _PERMIT_ADDR_RE.match(street_address)
    if not m:
        return None
    street = norm_street(m.group(2), m.group(3))
    return (int(m.group(1)), street) if street else None
