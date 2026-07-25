"""Norfolk building permits adapter.

Confirmed live source: Socrata SODA API, dataset "Permits" (fahm-yuh4),
updated daily. No native lat/lng -- the geocode pass fills coordinates in
later. No confirmed cost field ('total_balance' is a fee balance, not a
project valuation) so valuation is always None here.
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

LOCALITY = "norfolk"
SOURCE_NAME = "Norfolk Open Data Portal"
BASE_URL = "https://data.norfolk.gov/resource/fahm-yuh4.json"
SOURCE_RECORD_URL = BASE_URL  # no per-record public detail page confirmed; points at the dataset itself
PAGE_SIZE = 1000

# Fields the live schema must still expose, checked once against the union of
# keys seen across the first fetched page (Socrata omits null-valued keys per
# row, so a per-record check would false-positive on legitimate nulls).
REQUIRED_FIELDS = {"permit_number", "address", "type", "status", "application_date"}

# Extend as more real values are observed in production; unmapped values fall
# back to "other" rather than raising. The block below was built from the
# real distinct `status` values observed against the live 10,850-row
# database (2026-07-24) -- 1,030 Norfolk permits were landing in "other"
# because the original Phase 3 dry-run sample (20 records) never saw most
# of these. Judgment calls, not guesses -- based on standard permit-portal
# lifecycle terminology:
STATUS_BUCKET_MAP = {
    "Issued": "issued",
    "Authorized": "issued",  # authorized to proceed, functionally equivalent to issued
    "Finaled": "finaled_closed",
    "Closed": "finaled_closed",
    "C/O Issued": "finaled_closed",  # Certificate of Occupancy issued -- project complete
    "Paid Certificate Issued": "finaled_closed",
    "Cancelled": "expired_void",
    "Abandoned": "expired_void",
    "Withdrawn": "expired_void",
    "Denied": "expired_void",
    "Revoked": "expired_void",
    "Expired": "expired_void",
    "Void": "expired_void",
    "Under Review": "active_pending",
    "Pending": "active_pending",
    "Pending Payment": "active_pending",
    "Additional Information Required": "active_pending",
    "Reviews in progress": "active_pending",
    "Reviews complete": "active_pending",  # review done but not yet issued/finaled
    "Documents Submitted": "active_pending",
    "Incomplete": "active_pending",
    "Portal Submitted": "active_pending",
    "New": "active_pending",
    "Final Review": "active_pending",
    "Submit for Approval": "active_pending",
    "Overdue": "active_pending",  # a task/payment is overdue but the permit itself is still active
    "Completed 3rd Party Review": "active_pending",
}


def _status_bucket(raw_status: str | None) -> str:
    if raw_status is None:
        return "other"
    return STATUS_BUCKET_MAP.get(raw_status, "other")


def _build_address(raw: dict) -> str | None:
    address = raw.get("address")
    if not address or not str(address).strip():
        return None
    return f"{str(address).strip()}, Norfolk, VA"


def fetch(since: date | None = None, limit: int | None = None):
    """Yields NormalizedPermit records, paginating $limit/$offset until the
    source is exhausted or the caller's limit is reached. Raises
    SourceSchemaError if the live schema no longer contains a required field
    at all (not raised for individual nulls -- Socrata omits those per row)."""
    offset = 0
    yielded = 0
    schema_checked = False

    while True:
        params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": "application_date"}
        if since is not None:
            since_str = since.isoformat() if isinstance(since, date) else str(since)
            params["$where"] = f"application_date >= '{since_str}T00:00:00.000'"

        page = fetch_json_retry(BASE_URL, params=params)
        if not isinstance(page, list):
            raise SourceSchemaError(
                f"{SOURCE_NAME}: expected a JSON array from {BASE_URL}, got {type(page).__name__}"
            )

        if not page:
            # A legitimately empty result (e.g. no permits in this --since
            # window) is not schema drift -- there's nothing to check fields
            # against, so don't raise. Real schema drift is only detectable
            # when there IS data but a required field is absent from all of it.
            if offset == 0:
                print(f"  {SOURCE_NAME}: zero rows returned for this query window")
            break

        if not schema_checked:
            present_fields = set()
            for rec in page:
                present_fields.update(rec.keys())
            validate_required_fields({f: None for f in present_fields}, REQUIRED_FIELDS, SOURCE_NAME)
            schema_checked = True

        for raw in page:
            permit_id = raw.get("permit_number")
            if not permit_id or not str(permit_id).strip():
                # can't identify this record -- skip it rather than fail the whole page
                continue

            yield NormalizedPermit(
                locality=LOCALITY,
                source_permit_id=str(permit_id).strip(),
                source_name=SOURCE_NAME,
                source_url=BASE_URL,
                source_record_url=SOURCE_RECORD_URL,
                address=_build_address(raw),
                permit_type=raw.get("type"),
                status=raw.get("status"),
                status_bucket=_status_bucket(raw.get("status")),
                applied_date=normalize_date(raw.get("application_date")),
                issued_date=normalize_date(raw.get("issue_date")),
                finaled_date=normalize_date(raw.get("finaled_date")),
                expiration_date=normalize_date(raw.get("expiration_date")),
                valuation=None,  # no verified cost field -- total_balance is a fee balance
                description=raw.get("work_type") or raw.get("use_type"),
                contractor=None,
                applicant=None,
                raw=raw,
            )
            yielded += 1
            if limit is not None and yielded >= limit:
                return

        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
