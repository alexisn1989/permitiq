"""Chesapeake building permits from Accela Citizen Access "Download results"
CSVs (aca-prod.accela.com/CHESAPEAKE, Building module, General Search).

Chesapeake publishes no permit dataset; a person runs the site's own search for
a date window and downloads the results. Nothing here queries Accela.

The export's columns (confirmed on a real download, 2026-10-29): Date, Record
Number, Record Type, Project Name, Address, Status, Related Records, and an
empty trailing column. Any other layout stops the import (no column guessed).

* Date is the record's date as Accela lists it in search results, the date
  the search's Start / End Date filter on; stored as applied_date.
* Status buckets come only from the statuses seen in real downloads; an
  unseen status stops the import so its meaning is checked, not guessed.
* Project Name is shown only when it is clearly an organization (the
  organization rule); otherwise it is withheld and not stored.
* The export has no parcel number and no coordinates: permits are geocoded
  from the address like any other permit.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

from ingest import lead_facts
from ingest.common import NormalizedPermit

LOCALITY = "chesapeake"
SOURCE_NAME = "Chesapeake Accela Citizen Access (downloaded search results)"
SOURCE_URL = "https://aca-prod.accela.com/CHESAPEAKE/Cap/CapHome.aspx?module=Building&TabName=Building"
COLUMNS = ("Date", "Record Number", "Record Type", "Project Name", "Address", "Status", "Related Records")

# every status seen in the real downloads; "other" where the label does not say
# which way the record went
STATUS_BUCKET_MAP = {
    "Permit Issued": "issued", "Issued": "issued",
    "Closed": "finaled_closed",
    "Expired": "expired_void", "Certificate Expired": "expired_void", "Withdrawn": "expired_void",
    "Denied": "expired_void",
    "Accepted": "active_pending", "Submitted": "active_pending", "Open": "active_pending",
    "In Review": "active_pending", "Ready for Review": "active_pending", "Routed for Electronic Review": "active_pending",
    "Revisions Required": "active_pending", "Revisions Received": "active_pending",
    "Resubmittal Received": "active_pending", "Missing Information": "active_pending",
    "Pending Additional Information": "active_pending", "Pending Payment": "active_pending",
    "Pending Client": "active_pending", "Awaiting Client Reply": "active_pending",
    "Awaiting Contractor Selection": "active_pending", "Ready to Issue": "active_pending",
    "Provisional Approval": "active_pending",
    "Model Approved": "other", "In Service": "other", "Temp CO": "other", "Closed - Incomplete": "other",
}


class ExportError(Exception):
    pass


def read_rows(text: str) -> list[dict]:
    """Rows of one download; the header must be exactly the confirmed layout."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration:
        raise ExportError("empty file") from None
    cols = tuple(h.strip() for h in header)
    if cols and cols[-1] == "":
        cols = cols[:-1]
    if cols != COLUMNS:
        raise ExportError(f"columns {list(cols)} are not the confirmed Accela export layout {list(COLUMNS)}")
    rows = []
    for n, r in enumerate(reader, start=2):
        if not any(c.strip() for c in r):
            continue
        if any(c.strip() for c in r[len(COLUMNS):]):
            raise ExportError(f"line {n}: values beyond the known columns")
        rows.append({k: (r[i].strip() if i < len(r) else "") for i, k in enumerate(COLUMNS)})
    return rows


def row_date(row: dict) -> date:
    try:
        return datetime.strptime(row["Date"], "%m/%d/%Y").date()
    except ValueError:
        raise ExportError(f"{row['Record Number']}: date {row['Date']!r} is not MM/DD/YYYY") from None


def unknown_statuses(rows: list[dict]) -> list[str]:
    return sorted({r["Status"] for r in rows if r["Status"] and r["Status"] not in STATUS_BUCKET_MAP})


def to_permit(row: dict) -> NormalizedPermit:
    """One export row. Project names that may be a private person are dropped
    here, so they reach neither the permits table nor raw_json."""
    project = row["Project Name"] or None
    shown = project if project and lead_facts.is_organization(project) else None
    raw = {"Date": row["Date"], "Record Number": row["Record Number"], "Record Type": row["Record Type"],
           "Project Name": shown, "project_withheld": bool(project) and shown is None,
           "Address": row["Address"] or None, "Status": row["Status"] or None,
           "Related Records": row["Related Records"] or None}
    status = row["Status"] or None
    return NormalizedPermit(
        locality=LOCALITY, source_permit_id=row["Record Number"], source_name=SOURCE_NAME, source_url=SOURCE_URL,
        source_record_url=None,  # the export carries no per-record link
        address=row["Address"] or None, permit_type=row["Record Type"] or None, status=status,
        status_bucket=STATUS_BUCKET_MAP.get(status, "other") if status else "other",
        applied_date=row_date(row).isoformat(), description=shown, raw=raw)
