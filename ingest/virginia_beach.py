"""Virginia Beach building permits adapter.

Confirmed live source: ArcGIS FeatureServer (hosted feature layer)
Building_Permits_Applications_view/FeatureServer/0. Live-verified against the
real service (2026-07-23): every field is esriFieldTypeString, INCLUDING the
date fields -- ApplicationDate/IssueDate/FinalDate are plain strings shaped
"YYYY/MM/DD", not ArcGIS Date-typed epoch values. A SQL `DATE 'YYYY-MM-DD'`
literal in the where clause silently mismatches against this string field
(confirmed live: it returned unfiltered-looking results from a different
year); the date filter below uses a same-format string comparison instead,
which was confirmed live to filter correctly. No native geometry -- the
layer's field list has no lat/lng-like fields, so coordinates are always
None here and filled in by the geocode pass.
"""

from __future__ import annotations

from datetime import date

from ingest.common import (
    NormalizedPermit,
    SourceSchemaError,
    fetch_json_retry,
    normalize_date,
    validate_required_fields,
)

LOCALITY = "virginia_beach"
SOURCE_NAME = "Virginia Beach Building Permit Applications (ArcGIS Hub)"
BASE_URL = (
    "https://services2.arcgis.com/CyVvlIiUfRBmMQuu/arcgis/rest/services/"
    "Building_Permits_Applications_view/FeatureServer/0/query"
)
SOURCE_RECORD_URL = BASE_URL
PAGE_SIZE = 2000  # confirmed live: this service's maxRecordCount is 2000
# objectIds lists go in a GET URL: 2,000 IDs made the URL too long (the service answered
# 400 Bad Request on the real backfill check); 200 IDs keep it near 1.5 KB.
ID_BATCH = 200

OUT_FIELDS = [
    "PermitNumber", "PermitType", "ApplicationDate", "IssueDate", "FinalDate",
    "Status", "WorkDesc", "StreetAddress", "AddressUnit", "City", "State", "Zip",
    "GPIN", "ConstructionType", "WorkType", "CreatedBy", "OBJECTID",
]

REQUIRED_FIELDS = {"PermitNumber", "StreetAddress", "ApplicationDate", "Status"}

# Confirmed live via returnDistinctValues=true against the real Status field
# (2026-07-23): Active, Closed, Expired, Final, Pending, Pending Revisions,
# Pre-Issuance, Ready to Issue, Rejected, Void, Withdrawn -- this is the
# complete observed vocabulary, not a guess.
STATUS_BUCKET_MAP = {
    "Active": "issued",
    "Closed": "finaled_closed",
    "Final": "finaled_closed",
    "Expired": "expired_void",
    "Void": "expired_void",
    "Rejected": "expired_void",
    "Withdrawn": "expired_void",
    "Pending": "active_pending",
    "Pending Revisions": "active_pending",
    "Pre-Issuance": "active_pending",
    "Ready to Issue": "active_pending",
}


def _status_bucket(raw_status: str | None) -> str:
    if raw_status is None:
        return "other"
    return STATUS_BUCKET_MAP.get(raw_status, "other")


def _build_address(attrs: dict) -> str | None:
    street = (attrs.get("StreetAddress") or "").strip()
    if not street:
        return None
    unit = (attrs.get("AddressUnit") or "").strip()
    city = (attrs.get("City") or "").strip()
    state = (attrs.get("State") or "").strip()
    zip_code = (attrs.get("Zip") or "").strip()

    line1 = f"{street} {unit}" if unit else street
    state_zip = f"{state} {zip_code}".strip()
    tail_parts = [p for p in (city, state_zip) if p]
    tail = ", ".join(tail_parts)
    return f"{line1}, {tail}" if tail else line1


def _to_permit(attrs: dict, permit_id) -> NormalizedPermit:
    return NormalizedPermit(
        locality=LOCALITY,
        source_permit_id=str(permit_id).strip(),
        source_name=SOURCE_NAME,
        source_url=BASE_URL,
        source_record_url=SOURCE_RECORD_URL,
        address=_build_address(attrs),
        permit_type=attrs.get("PermitType"),
        status=attrs.get("Status"),
        status_bucket=_status_bucket(attrs.get("Status")),
        applied_date=normalize_date(attrs.get("ApplicationDate")),
        issued_date=normalize_date(attrs.get("IssueDate")),
        finaled_date=normalize_date(attrs.get("FinalDate")),
        expiration_date=None,  # no confirmed expiration field in this schema
        valuation=None,  # no verified cost field in this schema
        description=attrs.get("WorkDesc"),
        contractor=None,  # CreatedBy is a portal username, not a contractor name
        applicant=None,
        raw=attrs,
    )


def query_json(params: dict) -> dict:
    """One ArcGIS query against the permits layer (shape-checked)."""
    response = fetch_json_retry(BASE_URL, params={**params, "f": "json"})
    if not isinstance(response, dict):
        raise SourceSchemaError(f"{SOURCE_NAME}: expected a JSON object, got {type(response).__name__}")
    if "error" in response:
        raise SourceSchemaError(f"{SOURCE_NAME}: ArcGIS query error: {response['error']}")
    return response


def fetch_by_ids(object_ids: list[int]):
    """Re-fetch specific source rows by OBJECTID (backfill top-up), in batches of
    ID_BATCH, through the same normalizer as fetch(). Rows without a
    PermitNumber are skipped exactly as fetch() skips them."""
    ids = sorted(set(object_ids))
    for i in range(0, len(ids), ID_BATCH):
        response = query_json({"objectIds": ",".join(str(x) for x in ids[i:i + ID_BATCH]),
                               "outFields": ",".join(OUT_FIELDS)})
        for feat in response.get("features") or []:
            attrs = feat.get("attributes", {})
            permit_id = attrs.get("PermitNumber")
            if permit_id and str(permit_id).strip():
                yield _to_permit(attrs, permit_id)


def fetch(since: date | None = None, limit: int | None = None):
    """Yields NormalizedPermit records, paginating resultOffset/resultRecordCount
    while exceededTransferLimit is true. Raises SourceSchemaError if the live
    response shape changes or a required field vanishes from an entire
    (non-empty) page."""
    offset = 0
    yielded = 0
    schema_checked = False

    while True:
        where = "1=1"
        if since is not None:
            since_str = since.strftime("%Y/%m/%d") if isinstance(since, date) else str(since)
            where = f"ApplicationDate >= '{since_str}'"

        params = {
            "where": where,
            "outFields": ",".join(OUT_FIELDS),
            "f": "json",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
            "orderByFields": "ApplicationDate",
        }
        response = fetch_json_retry(BASE_URL, params=params)

        if not isinstance(response, dict):
            raise SourceSchemaError(
                f"{SOURCE_NAME}: expected a JSON object from {BASE_URL}, got {type(response).__name__}"
            )
        if "error" in response:
            raise SourceSchemaError(f"{SOURCE_NAME}: ArcGIS query error: {response['error']}")
        if "features" not in response:
            raise SourceSchemaError(
                f"{SOURCE_NAME}: response is missing 'features' -- unexpected ArcGIS response shape"
            )

        features = response["features"]

        if not features:
            if offset == 0:
                print(f"  {SOURCE_NAME}: zero rows returned for this query window")
            break

        if not schema_checked:
            present_fields = set()
            for feat in features:
                present_fields.update(feat.get("attributes", {}).keys())
            validate_required_fields({f: None for f in present_fields}, REQUIRED_FIELDS, SOURCE_NAME)
            schema_checked = True

        for feat in features:
            attrs = feat.get("attributes", {})
            permit_id = attrs.get("PermitNumber")
            if not permit_id or not str(permit_id).strip():
                continue

            yield _to_permit(attrs, permit_id)
            yielded += 1
            if limit is not None and yielded >= limit:
                return

        if not response.get("exceededTransferLimit"):
            break
        offset += PAGE_SIZE
