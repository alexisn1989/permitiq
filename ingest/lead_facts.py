"""Lead facts: who applied, what was asked for, how big, and when a permit
could be expected -- read-only, computed from data already stored.

Everything taken from a Council title is EXTRACTED TEXT, returned with the
exact span it came from; it is never a verified fact and never feeds status,
links or approval. Applicant names are shown only when every listed
applicant is clearly an organization; if any part could be a private
individual, the whole name is withheld.

Timing is a benchmark, not a prediction: days since approval are counted to
the last permit refresh (the end of what was observed), and the typical
approval-to-first-application range comes from confirmed links only, over
all verified approvals (not "similar" ones), suppressed below min_n.
"""

from __future__ import annotations

import re

from ingest import land_use_link, land_use_stats

APPLICANT_RE = re.compile(r"\bApplications?\s+of\s+(?P<name>.{2,160}?)\s*,?\s+for\s", re.IGNORECASE)
REQUEST_RE = re.compile(
    r"\bfor\s+(?:an?\s+)?(?P<req>(?:Change\s+of\s+Zoning|Conditional\s+(?:Change\s+of\s+Zoning|Use\s+Permit)|"
    r"Modification\s+of\s+(?:Conditions|Proffers)|Rezoning|Subdivision\s+Variance|Street\s+Closure|"
    r"Special\s+Exception|Variance)[^,;]{0,80}?)(?=\s+(?:at|re|on|for|located|from\s+[A-Z]-?\d)\b|\s*\(GPIN|\s+DISTRICT\b|[,;]|$)",
    re.IGNORECASE)
# One legal-form or institutional word is enough to call a part an organization.
ORG_RE = re.compile(
    r"\b(?:L\.?L\.?C|L\.?L\.?P|L\.?P|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|LTD|PLLC|PC|"
    r"HOLDINGS?|PROPERTIES|PARTNERS(?:HIP)?|ASSOCIATES|ASSOCIATION|GROUP|DEVELOPMENT|DEVELOPERS|"
    r"VENTURES?|INVESTMENTS?|REALTY|ENTERPRISES?|HOMES|BUILDERS|CHURCH|MINISTRIES|BANK|UNIVERSITY|"
    r"SCHOOLS?|FOUNDATION|AUTHORITY|HOSPITAL|CITY\s+OF|COUNTY|COMMONWEALTH|DIOCESE|PARISH|TEMPLE|MOSQUE|"
    r"SYNAGOGUE|ACADEMY|CREDIT\s+UNION|DEPARTMENT|STATE\s+OF|UNITED\s+STATES)\b\.?", re.IGNORECASE)
_PART_SPLIT = re.compile(r"\s+(?:and|&)\s+|\s*[;,]\s*(?:(?:and|&)\s+)?", re.IGNORECASE)
# ", LLC" / ", L.L.C." / ", Inc." after a comma is part of the name before it, not a separate party
_SUFFIX_ONLY = re.compile(r"^(?:L\.?L\.?C|L\.?L\.?P|L\.?P|INC|INCORPORATED|CORP|CORPORATION|CO|LTD|PLLC|PC|LC)\.?$",
                          re.IGNORECASE)

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
SCALE_RES = [
    ("acres", re.compile(rf"(?P<v>{_NUM})\s*(?:-|\s)?\s*(?:\+/-\s*|±\s*)?acres?\b", re.IGNORECASE)),
    ("units", re.compile(rf"(?P<v>{_NUM})(?:\s+|-\s?)(?:(?:dwelling|residential|multi-?family|single-?family|"
                         r"apartment|townhouse|condominium)\s+)?(?:units?|dwellings|homes|townhouses|apartments)\b",
                         re.IGNORECASE)),
    ("lots", re.compile(rf"(?P<v>{_NUM})\s+(?:residential\s+|single-?family\s+)?lots\b", re.IGNORECASE)),
    ("square_feet", re.compile(rf"(?P<v>{_NUM})\s*(?:-|\s)?\s*(?:square\s+feet|square\s+foot|sq\.?\s*ft\.?|s\.f\.|sf\b)",
                               re.IGNORECASE)),
]
APPROVED = ("approved", "approved_with_conditions")


def is_organization(name: str | None) -> bool:
    """True only when every listed party ("A and B", "A & B", "A; B") is clearly an
    organization. Any part that may be a private person makes the whole name private."""
    parts: list[str] = []
    for p in (x.strip() for x in _PART_SPLIT.split(name or "")):
        if not p:
            continue
        if parts and _SUFFIX_ONLY.match(p):
            parts[-1] = f"{parts[-1]}, {p}"
        else:
            parts.append(p)
    # "LC" is a Virginia limited-company suffix ("Real Estate Pursuits LC")
    return bool(parts) and all(ORG_RE.search(p) or re.search(r"\bL\.?C\.?$", p) for p in parts)


def applicant(title: str | None) -> dict:
    """{"name": str|None, "kind": "organization"|"withheld"|None, "text": span}."""
    m = APPLICANT_RE.search(title or "")
    if not m:
        return {"name": None, "kind": None, "text": None}
    name = m.group("name").strip(" ,")
    if is_organization(name):
        return {"name": name, "kind": "organization", "text": m.group(0).strip()}
    # any part that may be a private person: withhold the whole name (and its span)
    return {"name": None, "kind": "withheld", "text": None}


def request(title: str | None) -> dict:
    m = REQUEST_RE.search(title or "")
    return {"text": m.group("req").strip() if m else None}


def scale(title: str | None) -> list[dict]:
    out = []
    for kind, rx in SCALE_RES:
        for m in rx.finditer(title or ""):
            out.append({"kind": kind, "value": float(m.group("v").replace(",", "")), "text": m.group(0).strip()})
    return out


def timing(conn, decision: dict, cfg: dict | None) -> dict:
    """Days since approval (to the last permit refresh), the link-window end,
    and the confirmed-links benchmark. Shown for verified approvals only."""
    cov = land_use_stats.coverage(conn)
    out = {"shown": False, "reason": None, "approved_on": decision.get("meeting_date"),
           "observed_to": cov["last_refresh_at"][:10] if cov and cov.get("last_refresh_at") else None,
           "days_since_approval": None, "window_end": None, "benchmark": None, "typical_first_application": None,
           "past_typical_range": None}
    if decision.get("verified_outcome") not in APPROVED:
        out["reason"] = "outcome not verified as approved: no timing shown"
        return out
    if not cov or not decision.get("meeting_date"):
        out["reason"] = "no permit observation window recorded"
        return out
    out["shown"] = True
    meeting = decision["meeting_date"]
    if out["observed_to"]:
        out["days_since_approval"] = int(conn.execute("SELECT julianday(?) - julianday(?)",
                                                      (out["observed_to"], meeting)).fetchone()[0])
    out["window_end"] = land_use_link.window_end(conn, meeting, cov["link_window_months"])
    if cfg is None:
        out["benchmark"] = {"configured": False}
        return out
    b = land_use_stats.timing_benchmark(conn, cfg)
    out["benchmark"] = b
    if not b.get("suppressed") and b.get("median_days") is not None:
        lo, mid, hi = (conn.execute("SELECT date(?, '+' || ? || ' days')", (meeting, d)).fetchone()[0]
                       for d in (b["p25_days"], b["median_days"], b["p75_days"]))
        out["typical_first_application"] = {"from": lo, "median": mid, "to": hi}
        # an observation, not a diagnosis: no permit was observed and this lead is older than the typical range
        out["past_typical_range"] = (decision.get("status") == "no_candidate_permit"
                                     and out["days_since_approval"] is not None
                                     and out["days_since_approval"] > b["p75_days"])
    return out


def lead_facts(conn, decision: dict, parcel_rows: list[dict], cfg: dict | None) -> dict:
    title = decision.get("title")
    acres = [r["acreage"] for r in parcel_rows if r.get("acreage") is not None]
    gpins = {r["display_gpin"] for r in parcel_rows if r.get("display_gpin")}
    seen, parcel_acres = set(), 0.0
    for r in parcel_rows:  # one parcel can appear on several address rows: count it once
        if r.get("display_gpin") and r.get("acreage") is not None and r["display_gpin"] not in seen:
            seen.add(r["display_gpin"])
            parcel_acres += r["acreage"]
    return {
        "source_note": "Applicant, request and size are extracted from the Council title text; not verified.",
        "applicant": applicant(title),
        "request": request(title),
        "scale_from_title": scale(title),
        "title_truncated": bool(decision.get("title_truncated")),
        "parcel_acres": round(parcel_acres, 2) if acres else None,
        "parcels": len(gpins),
        "timing": timing(conn, decision, cfg),
    }
