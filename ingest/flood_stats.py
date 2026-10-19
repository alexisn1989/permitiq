"""Flood-exposure statistics -- the ONE implementation behind both
GET /api/flood-stats and scripts/flood_report.py, so their numbers and
denominators cannot drift. Read-only: takes an open connection, runs SELECTs.

Definitions (fixed before any share is computed):
  population   Virginia Beach permits with an issued_date and status_bucket
               != 'expired_void'. Unissued and void permits are counted in
               `excluded`, never in a share.
  baseline     the population, any permit type.
  cohort       the population restricted to the explicit allowlist of observed
               (PermitType, WorkType, ConstructionType) combinations in
               config/flood_report.toml. Empty allowlist = unconfigured: no
               cohort numbers are produced (cohort_configured: false).
  context      issued_from, issued_to, zip -- applied identically to cohort AND
               baseline. These are the only filters; population filters
               (permit_type, status, ...) would redefine the cohort and are
               rejected by the callers.
  denominator  matched classes only (sfha_v, sfha_a, shaded_x,
               matched_non_sfha). no_polygon, not_geocoded and untagged are
               excluded from every denominator and reported beside every row
               as counts and as % of the row total, so coverage stays visible.
  suppression  shares are null when the matched count is below min_n.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

LOCALITY = "virginia_beach"
MATCHED = ("sfha_v", "sfha_a", "shaded_x", "matched_non_sfha")
COVERAGE = ("no_polygon", "not_geocoded", "untagged")
ALL_CLASSES = MATCHED + COVERAGE + ("out_of_scope",)
DEFAULT_MIN_N = 20

# Stage 3 parcel basis (basis="parcel"): the class is the parcel's area-share
# class. Screening classes, unmatched / ambiguous / disputed parcels and
# untagged rows are coverage classes -- shown beside every row, never in a
# denominator. MATCHED (the denominator) is the same four classes.
PARCEL_COVERAGE = ("screening_sliver", "screening_partial_coverage", "no_polygon", "no_parcel", "ambiguous",
                   "parcel_disputed", "not_geocoded", "untagged")
BASES = {
    "point": {"coverage": COVERAGE, "all": ALL_CLASSES},
    "parcel": {"coverage": PARCEL_COVERAGE, "all": MATCHED + PARCEL_COVERAGE + ("out_of_scope",)},
}
PARCEL_CLASS_SQL = """CASE
    WHEN pp.parcel_state = 'out_of_scope' THEN 'out_of_scope'
    WHEN pp.parcel_state = 'untagged' THEN 'untagged'
    WHEN pp.parcel_state IN ('no_parcel', 'ambiguous') THEN pp.parcel_state
    WHEN pp.parcel_state = 'not_geocoded_no_gpin' THEN 'not_geocoded'
    WHEN pp.disputed = 1 AND (pp.review_verdict IS NULL OR pp.review_verdict = 'unresolvable') THEN 'parcel_disputed'
    WHEN pf.gpin IS NULL THEN 'untagged'
    ELSE pf.parcel_flood_class END"""
PARCEL_JOIN_SQL = ("JOIN permit_parcel_effective pp ON pp.permit_id = p.id "
                   "LEFT JOIN parcel_flood_effective pf ON pf.gpin = pp.display_gpin")
_ZIP_RE = re.compile(r"^(\d{5}|unknown)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RAW_KEYS = {"permit_type": "$.PermitType", "work_type": "$.WorkType", "construction_type": "$.ConstructionType"}

# ZIP as reported by the source (raw_json.Zip), first 5 digits, else 'unknown'.
ZIP_SQL = ("CASE WHEN substr(trim(COALESCE(json_extract(p.raw_json,'$.Zip'),'')),1,5) "
           "GLOB '[0-9][0-9][0-9][0-9][0-9]' "
           "THEN substr(trim(json_extract(p.raw_json,'$.Zip')),1,5) ELSE 'unknown' END")


class ConfigError(ValueError):
    pass


def load_cohort_config(path: Path) -> dict:
    """{'name', 'include': [...], 'min_n'}; include == [] means unconfigured."""
    if not path.is_file():
        return {"name": None, "include": [], "min_n": DEFAULT_MIN_N}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    cohort = data.get("cohort", {})
    include = cohort.get("include", [])
    if not isinstance(include, list):
        raise ConfigError(f"{path}: cohort.include must be a list")
    clean = []
    for i, entry in enumerate(include):
        if not isinstance(entry, dict) or not entry or set(entry) - set(_RAW_KEYS):
            raise ConfigError(f"{path}: cohort.include[{i}] must use only keys {sorted(_RAW_KEYS)}")
        for k, v in entry.items():
            if not isinstance(v, str) or not v:
                raise ConfigError(f"{path}: cohort.include[{i}].{k} must be a non-empty string ('*' = any)")
        clean.append(entry)
    min_n = cohort.get("min_n", DEFAULT_MIN_N)
    if not isinstance(min_n, int) or min_n < 1:
        raise ConfigError(f"{path}: cohort.min_n must be a positive integer")
    return {"name": cohort.get("name"), "include": clean, "min_n": min_n}


def validate_context(issued_from: str | None, issued_to: str | None, zip_code: str | None) -> None:
    for name, v in (("issued_from", issued_from), ("issued_to", issued_to)):
        if v is not None and not _DATE_RE.match(v):
            raise ValueError(f"{name} must be in YYYY-MM-DD format (got {v!r})")
    if zip_code is not None and not _ZIP_RE.match(zip_code):
        raise ValueError(f"zip must be a 5-digit ZIP or 'unknown' (got {zip_code!r})")


def _cohort_sql(include: list[dict]) -> tuple[str, list]:
    if not include:
        return "0", []
    ors, params = [], []
    for entry in include:
        ands = []
        for key, value in entry.items():
            if value == "*":
                continue
            ands.append(f"json_extract(p.raw_json, '{_RAW_KEYS[key]}') = ?")
            params.append(value)
        ors.append("(" + (" AND ".join(ands) or "1") + ")")
    return "(" + " OR ".join(ors) + ")", params


def _block_row(key, counts: dict, min_n: int, basis: str = "point") -> dict:
    coverage, all_classes = BASES[basis]["coverage"], BASES[basis]["all"]
    total = sum(counts.get(c, 0) for c in all_classes)
    matched = sum(counts.get(c, 0) for c in MATCHED)
    shares = None
    if matched >= min_n and matched > 0:
        shares = {c: round(100 * counts.get(c, 0) / matched, 1) for c in MATCHED}
        shares["sfha_total"] = round(100 * (counts.get("sfha_v", 0) + counts.get("sfha_a", 0)) / matched, 1)
    return {
        "key": key,
        "total": total,
        "matched": matched,
        "counts": {c: counts.get(c, 0) for c in all_classes},
        "shares_of_matched": shares,
        "suppressed": shares is None,
        "coverage_pct_of_total": {c: (round(100 * counts.get(c, 0) / total, 1) if total else None) for c in coverage},
    }


def _block(rows: list[dict], min_n: int, basis: str = "point") -> dict:
    def agg(keyfn):
        acc: dict = {}
        for r in rows:
            k = keyfn(r)
            acc.setdefault(k, {}).setdefault(r["cls"], 0)
            acc[k][r["cls"]] += r["n"]
        return acc

    overall = agg(lambda r: "all").get("all", {})
    by_year = agg(lambda r: r["yr"])
    by_zip = agg(lambda r: r["zip"])
    return {
        "overall": _block_row("all", overall, min_n, basis),
        "by_year": [_block_row(k, v, min_n, basis) for k, v in sorted(by_year.items())],
        "by_zip": [_block_row(k, v, min_n, basis) for k, v in sorted(by_zip.items())],
    }


def active_load(conn) -> dict | None:
    row = conn.execute(
        "SELECT l.load_id, l.dfirm_id, l.source_vintage, l.content_hash, l.feature_count, l.loaded_at, "
        "l.simplify_tolerance FROM flood_zone_loads l JOIN flood_locality_dfirm m ON m.dfirm_id = l.dfirm_id "
        "WHERE m.locality = ? AND l.is_active = 1", (LOCALITY,)).fetchone()
    return dict(row) if row else None


def compute_flood_stats(conn, cohort_cfg: dict, issued_from: str | None = None,
                        issued_to: str | None = None, zip_code: str | None = None, basis: str = "point") -> dict:
    validate_context(issued_from, issued_to, zip_code)
    if basis not in BASES:
        raise ValueError(f"basis must be one of {tuple(BASES)} (got {basis!r})")
    cls_sql = "e.effective_class" if basis == "point" else PARCEL_CLASS_SQL
    join_sql = "" if basis == "point" else PARCEL_JOIN_SQL
    cohort_expr, cohort_params = _cohort_sql(cohort_cfg["include"])
    where, params = ["p.locality = ?"], [LOCALITY]
    if issued_from:
        where.append("p.issued_date >= ?")
        params.append(issued_from)
    if issued_to:
        where.append("p.issued_date <= ?")
        params.append(issued_to)
    if zip_code:
        where.append(f"{ZIP_SQL} = ?")
        params.append(zip_code)
    sql = f"""
        SELECT substr(p.issued_date, 1, 4) AS yr, {ZIP_SQL} AS zip, {cls_sql} AS cls,
               {cohort_expr} AS in_cohort,
               (p.issued_date IS NOT NULL) AS issued,
               (p.status_bucket = 'expired_void') AS void,
               COUNT(*) AS n
        FROM permits p JOIN permit_flood_effective e ON e.permit_id = p.id {join_sql}
        WHERE {' AND '.join(where)}
        GROUP BY yr, zip, cls, in_cohort, issued, void
    """
    rows = [dict(r) for r in conn.execute(sql, cohort_params + params).fetchall()]

    def population(cohort_only: bool):
        sel = [r for r in rows if (r["in_cohort"] or not cohort_only)]
        kept = [r for r in sel if r["issued"] and not r["void"]]
        excluded = {
            "unissued": sum(r["n"] for r in sel if not r["issued"]),
            "void_or_expired": sum(r["n"] for r in sel if r["issued"] and r["void"]),
        }
        return kept, excluded

    min_n = cohort_cfg["min_n"]
    base_rows, base_excl = population(False)
    result = {
        "definitions_version": 1,
        "basis": basis,
        "locality": LOCALITY,
        "context_filters": {"issued_from": issued_from, "issued_to": issued_to, "zip": zip_code},
        "min_n": min_n,
        "load": active_load(conn),
        "cohort_configured": bool(cohort_cfg["include"]),
        "cohort_definition": {"name": cohort_cfg["name"], "include": cohort_cfg["include"]} if cohort_cfg["include"] else None,
        "baseline": {**_block(base_rows, min_n, basis), "excluded": base_excl},
        "cohort": None,
    }
    if cohort_cfg["include"]:
        c_rows, c_excl = population(True)
        result["cohort"] = {**_block(c_rows, min_n, basis), "excluded": c_excl}
    return result
