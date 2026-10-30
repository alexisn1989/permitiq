"""Read-only client for the FiscalMapIQ Neon database (parcels + assessments).

Uses Neon's HTTPS SQL endpoint (POST https://<host>/sql with the connection
string in a Neon-Connection-String header) rather than a Postgres driver:
it needs only `requests`, and it works from environments that allow HTTPS
egress but not raw Postgres on 5432. Only SELECTs are ever sent -- PermitIQ
never writes to Neon.

The connection string comes from NEON_DATABASE_URL and is never logged.
Neon returns bigint/numeric columns as strings, so every column has an
explicit converter below; NULL stays None and is never filled in.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Iterator
from urllib.parse import urlparse

import requests

from ingest.common import SourceSchemaError, validate_required_fields

SOURCE_NAME = "FiscalMapIQ Neon (parcels/assessments)"
ENV_VAR = "NEON_DATABASE_URL"
DEFAULT_PAGE_SIZE = 5000


def _int(value):
    return None if value is None else int(value)


def _float(value):
    return None if value is None else float(value)


def _text(value):
    return None if value is None else str(value)


# Neon column -> (SQLite column, converter). Order is the SELECT order.
PARCEL_COLUMNS: dict[str, tuple[str, Callable]] = {
    "parcel_id": ("parcel_id", _int),
    "city_code": ("city_code", _text),
    "gpin": ("gpin", _text),
    "recorded_gpin": ("recorded_gpin", _text),
    "prop_address": ("prop_address", _text),
    "latitude": ("latitude", _float),
    "longitude": ("longitude", _float),
    "acreage_computed": ("acreage_computed", _float),
    "acreage_stated": ("acreage_stated", _float),
    "tax_area_sqft": ("tax_area_sqft", _float),
    "zoning": ("zoning", _text),
    "land_use_code": ("land_use_code", _text),
    "property_class": ("property_class", _text),
    "tax_district": ("tax_district", _text),
    "tax_district_description": ("tax_district_description", _text),
    "exempt_source": ("exempt_source", _text),
    "fema_zone_bldg": ("fema_zone_bldg", _text),
    "watershed": ("watershed", _text),
    "census_tract": ("census_tract", _text),
    "census_blkgrp": ("census_blkgrp", _text),
    "census_block": ("census_block", _text),
    "source_id": ("source_id", _int),
    "data_year": ("data_year", _int),
    "loaded_at": ("source_loaded_at", _text),
}

ASSESSMENT_COLUMNS: dict[str, tuple[str, Callable]] = {
    "assessment_id": ("assessment_id", _int),
    "parcel_id": ("parcel_id", _int),
    "city_code": ("city_code", _text),
    "tax_year": ("tax_year", _int),
    "land_value": ("land_value", _float),
    "improvement_value": ("improvement_value", _float),
    "other_value": ("other_value", _float),
    "total_value": ("total_value", _float),
    "taxable_land_value": ("taxable_land_value", _float),
    "taxable_improvement_value": ("taxable_improvement_value", _float),
    "taxable_other_value": ("taxable_other_value", _float),
    "taxable_total_value": ("taxable_total_value", _float),
    "assessment_effective_date": ("assessment_effective_date", _text),
    "value_reconciliation_status": ("value_reconciliation_status", _text),
    "source_lrsn": ("source_lrsn", _text),
    "source_history_slot": ("source_history_slot", _int),
    "methodology_version_id": ("methodology_version_id", _int),
    "source_id": ("source_id", _int),
    "data_year": ("data_year", _int),
    "loaded_at": ("source_loaded_at", _text),
}

# table -> (column spec, primary key used for keyset pagination)
TABLES = {
    "parcels": (PARCEL_COLUMNS, "parcel_id"),
    "assessments": (ASSESSMENT_COLUMNS, "assessment_id"),
}


def connection_string() -> str:
    conn_str = os.getenv(ENV_VAR)
    if not conn_str:
        raise RuntimeError(f"{ENV_VAR} is not set -- cannot reach the Neon source")
    return conn_str


def run_query(conn_str: str, query: str, params: list, tries: int = 3, timeout: int = 120) -> list[dict]:
    """POST one read-only query to Neon's HTTPS SQL endpoint, with the same
    retry/backoff shape as ingest.common.fetch_json_retry. A SQL error (e.g.
    an undefined column after a source schema change) is not retried."""
    host = urlparse(conn_str).hostname
    if not host:
        raise RuntimeError(f"{ENV_VAR} has no host")
    url = f"https://{host}/sql"
    headers = {"Neon-Connection-String": conn_str, "Content-Type": "application/json"}

    last_err = None
    for attempt in range(tries):
        try:
            resp = requests.post(url, json={"query": query, "params": params}, headers=headers, timeout=timeout)
        except requests.RequestException as err:
            last_err = err
        else:
            body = resp.json() if resp.content else {}
            if resp.ok and "rows" in body:
                return body["rows"]
            if 400 <= resp.status_code < 500:
                raise SourceSchemaError(
                    f"{SOURCE_NAME}: query rejected ({resp.status_code}): {body.get('message', '')}"
                )
            last_err = RuntimeError(f"HTTP {resp.status_code}: {body.get('message', '')}")
        if attempt < tries - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed to query {SOURCE_NAME}: {last_err}")


def convert_row(raw: dict, columns: dict[str, tuple[str, Callable]]) -> dict:
    validate_required_fields(raw, columns.keys(), SOURCE_NAME)
    return {dest: convert(raw[src]) for src, (dest, convert) in columns.items()}


def fetch_table(
    table: str,
    conn_str: str | None = None,
    city_code: str = "VB",
    limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Iterator[dict]:
    """Yield converted rows from a Neon table, paging by primary key."""
    columns, key = TABLES[table]
    conn_str = conn_str or connection_string()
    select = ", ".join(columns)

    fetched = 0
    last_key = -1
    while limit is None or fetched < limit:
        batch = page_size if limit is None else min(page_size, limit - fetched)
        rows = run_query(
            conn_str,
            f"SELECT {select} FROM {table} WHERE city_code = $1 AND {key} > $2 ORDER BY {key} LIMIT $3",
            [city_code, last_key, batch],
        )
        for raw in rows:
            yield convert_row(raw, columns)
        fetched += len(rows)
        if len(rows) < batch:
            break
        last_key = int(rows[-1][key])
