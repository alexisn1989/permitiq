# PermitIQ

A read-only building/development permit map and dashboard for Hampton Roads, Virginia. PermitIQ pulls permit records directly from each city's own open-data system, normalizes them into a single schema, geocodes the ones that need it, and serves a map + filterable dashboard over the result. It never edits, scrapes fragile HTML, or fabricates data — if a locality has no verified open dataset, the UI says so plainly instead of guessing.

## Locality coverage

| Locality | Status | Source |
|---|---|---|
| Norfolk | **Available** | Socrata SODA API (`data.norfolk.gov`, dataset `fahm-yuh4`), updated daily |
| Virginia Beach | **Available** | ArcGIS REST (`services2.arcgis.com/.../Building_Permits_Applications_view/FeatureServer/0`) |
| Chesapeake | Source under review | Real permit system (TRAKiT-based) exists but no verified point-level open dataset found yet |
| Portsmouth | Source under review | Real permit system exists but no verified point-level open dataset found yet |
| Hampton, Suffolk, Newport News | Unavailable | No confirmed open permit dataset located |

**"Source under review" and "unavailable" are never silently omitted from the UI** — the locality selector shows all 7 cities, with the unsupported ones visibly disabled and a tooltip explaining why. Missing coverage is stated, not hidden.

### Finding a source for Chesapeake or Portsmouth

Don't guess at a URL. Use `scripts/explore_source.py <url>` to inspect a candidate ArcGIS/Socrata endpoint (prints platform, fields, and sample records) before writing an adapter. Candidates already ruled out and why are documented in `ingest/chesapeake.py` and `ingest/portsmouth.py` if those adapter stubs exist, or in the project's planning history otherwise — check there first so you don't repeat a dead end. A same-named lookalike service or an aggregate-stats-only layer (no individual records, no coordinates) is not a usable source.

## Install

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Running locally

```powershell
python -m uvicorn app:app --port 8090
```

Open `http://localhost:8090`. `app.py` is **read-only** — it never writes to the database and never triggers ingestion or geocoding. Permit data is populated entirely by `run_ingest.py`, run separately.

## Ingesting data

**Ingestion never runs from an HTTP route or app startup.** This is deliberate: a runtime data-loading subprocess triggered by a web request is exactly the pattern that caused an out-of-memory incident on a sibling project's production deploy. `run_ingest.py` is the only process that ever writes permit rows, and you run it by hand or on a schedule (cron/Task Scheduler) — never through the API.

```powershell
# See what would be fetched without writing anything
python run_ingest.py --locality norfolk --limit 20 --dry-run

# First real ingest -- recommend a ~90 day window, not the full 2-year default,
# so the first run finishes in minutes rather than hours (see "Geocoding" below)
python run_ingest.py --locality all --since 2026-04-24

# Skip geocoding on a given run (just sync permit records)
python run_ingest.py --locality norfolk --since 2026-04-24 --no-geocode
```

`--locality` accepts `norfolk`, `virginia_beach`, `chesapeake`, `portsmouth`, or `all`. Localities without a working adapter yet are skipped with a warning, not a crash. Re-running is always safe: ingestion is idempotent (`UNIQUE(locality, source_permit_id)` + upsert), and running the exact same command twice produces zero duplicate rows and zero unnecessary updates — verified against the real 10,850-row database (see "Verification history" below).

### Widening the date window over time

Each ingest only pulls permits with `applied_date >= --since`. Start narrow (recent activity), then widen gradually in later runs — every run is additive and idempotent, so there's no harm in overlapping windows.

## Geocoding

Norfolk and Virginia Beach's sources have no native lat/lng — every permit needs a Census Geocoder (free, no key) → Nominatim (rate-limited to ~1 request/second) pass before it can appear on the map. This is slow by design (respecting Nominatim's usage policy), so:

- `run_ingest.py` geocodes only the newly-ingested/still-pending backlog each run, bounded by `--geocode-limit N` (independent of `--limit`, which caps *fetched* records).
- To work through the backlog **without** re-fetching from source (faster, useful after the initial ingest), use:
  ```powershell
  python scripts/geocode_backlog.py --locality norfolk --limit 600
  python scripts/geocode_backlog.py --locality all --limit 600
  ```
- Results are cached by normalized address in `geocode_cache`, so the same address is never re-queried across permits or re-runs. Failed geocodes aren't retried every run either — only after a 30-day interval, or with `--force`.
- Coordinates use a precedence order (**manual > native > Census > Nominatim > missing**) so a re-ingest can never downgrade or erase an existing coordinate — see `ingest/common.py`'s `upsert_permits()` docstring.
- Some records genuinely have no usable street address (utility/infrastructure permits like `"Verizon - Poles 104, Virginia Beach, VA"`) — these get `geocode_status='no_address'` without ever hitting the network, distinct from `'not_found'` (a real address the geocoder couldn't resolve). Check `/api/stats` for the current `no_address` / `pending_geocode` / `geocode_not_found` breakdown.

Mapped coverage grows incrementally with every geocoding pass — check `/api/data-status` or the "Data sources" panel in the UI for current per-locality mapped percentages.

## Database

SQLite at `permits.db` next to `app.py` (override with the `DATA_DIR` env var). Two processes share it — `uvicorn` (read-only) and `run_ingest.py`/the geocode scripts (the only writers) — via WAL mode + a busy timeout, not a single-writer assumption.

Tables: `permits` (the normalized data), `ingestion_runs` (observability — one row per locality per ingest, with fetched/inserted/updated/skipped/geocoded counts and success/failure status), `geocode_cache` (address → coordinate memoization).

PermitIQ **never fabricates seed data**. Unlike some sibling portfolio projects that seed sample rows for a demo-ready empty database, `permits.db` starts genuinely empty — every row traces back to a real government open-data record, or the table has no rows for that locality yet.

## Tests

```powershell
python -m pytest tests/ -v
```

Fixture-based unit tests only — no live API calls except where explicitly run by hand (adapter dry-runs, live ingest, live geocoding). Covers: date/schema normalization, upsert coordinate-precedence logic, both locality adapters, geocoding (bbox/locality sanity checks, caching, retry gating, the `no_address` classification), and the full read-only API (filtering, sorting/SQL-injection safety, pagination, facets, stats, data-status, GeoJSON export).

## API

Read-only. No `POST`/`PUT`/`PATCH`/`DELETE` exist on `/api/permits*`, and `app.py` never imports `run_ingest.py`.

| Route | Purpose |
|---|---|
| `GET /health` | Liveness + schema check only |
| `GET /api/permits` | Filtered, paginated, sortable list (`locality`, `permit_type`, `status`, `status_bucket`, `q`, `issued_from`, `issued_to`, `sort`, `order`, `limit`, `offset`) |
| `GET /api/permits/{id}` | Single record |
| `GET /api/facets` | Locality availability + observed permit types/statuses (dynamic, not hardcoded) |
| `GET /api/stats` | Totals, mapped/unmapped, geocode-status breakdown, by-locality/by-bucket counts |
| `GET /api/data-status` | Per-locality sync history from `ingestion_runs` — never inferred from row counts alone |
| `GET /api/export.geojson` | GeoJSON of currently-mapped, currently-filtered permits (capped at 25,000 features; errors rather than silently truncates above that) |
| `GET /`, `GET /static/*` | The frontend |

`raw_json` (each source's full original record) is stored for auditability but never exposed through any API response.

## Frontend

Vanilla JS + Leaflet, no build step (`static/index.html`, `static/app.js`, `static/style.css`) — structural patterns (the `esc()` XSS helper, the marker diff/reconcile loop, `api()` fetch wrapper) adapted from the sibling AssetIQ project. Every externally-sourced string (address, description, contractor, status) is escaped before insertion — confirmed against a live injection payload during Phase 8 verification, not just asserted in isolation.

## Data limitations — read before treating this as authoritative

- PermitIQ republishes normalized public records from each city's own open-data system. **The original agency record is always authoritative** — if something here looks wrong, check the source link shown in each permit's detail panel or the "Data sources" modal.
- Some records lack coordinates (their address hasn't been geocoded yet, or isn't a mappable street address at all). **Missing data does not mean no permit exists.**
- Permit type and status vocabularies differ per city — the same word can mean different things in different jurisdictions. Filters are built dynamically from each city's own observed values, not a shared fixed list.
- `valuation` is `NULL` for essentially all Norfolk and Virginia Beach permits — neither confirmed source exposes a genuine project-cost field (Norfolk's `total_balance` is a fee balance, not a valuation).
- Chesapeake, Portsmouth, Hampton, Suffolk, and Newport News are not covered yet. This is stated in the UI, not hidden.

## Verification history

Every phase of this build (database, ingestion adapters, geocoding, the API, the frontend, and this hardening pass) was verified against the **real, live** government data sources and the real accumulating SQLite database — never mocked production data to make a test pass. As of the last hardening pass: 10,850 real permits (3,882 Norfolk, 6,968 Virginia Beach), idempotency confirmed via two identical back-to-back live ingests with zero duplicate rows, and the full read-only API and frontend exercised against that same real database.
