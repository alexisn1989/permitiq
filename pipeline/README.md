# PermitIQ pipeline (PostgreSQL + PostGIS)

A separate, self-contained data pipeline that lives beside the main PermitIQ
app in this repository. It does **not** replace or import the app at the repo
root (FastAPI + SQLite + Leaflet); nothing here changes the app's behavior.

It adds what the app does not have: a PostGIS database, a spatial join of every
lead against FEMA flood zones, flattened views for Power BI, and three lead
tracks:

| Track | Source | Loader |
|---|---|---|
| Building permits | VB ArcGIS FeatureServer | `ingest_permits.py` |
| Code-enforcement violations | VB ArcGIS FeatureServer (URL/fields unverified) | `ingest_violations.py` |
| Planning applications | Planning Commission / Council agenda PDFs, read by Gemini | `scan_planning_gemini.py` → `ingest_planning.py` |
| FEMA flood zones | FEMA NFHL layer 28, DFIRM 51810C | `scripts/load_fema_nfhl.py` |

## Layout

```
db/001-007_*.sql          schema, views, Power BI role, identity + status ledger
permitiq/api.py           FastAPI: /health, /api/{permits,violations,planning}/leads, admin ingest
permitiq/planning_identity.py   application/occurrence/event identity rules
permitiq/geo_lite.py      Census -> Nominatim geocoder with a VB bounds check
tests/                    pytest (unit tests + PostGIS tests)
data/                     sample planning records (July 14, 2026 Council agenda, user-supplied)
```

## Run it

```
cd pipeline
cp .env.example .env        # set POSTGRES_PASSWORD, POWERBI_READER_PASSWORD, DATABASE_URL, ADMIN_TOKEN
pip install -r requirements.txt
docker compose up -d db     # first start applies db/001-007
python scripts/load_fema_nfhl.py
python ingest_permits.py --inspect && python ingest_permits.py
python ingest_planning.py --file data/july14_leads_ingest.json
uvicorn permitiq.api:app --reload
```

On a database created before a migration existed, apply it by hand, e.g.
`docker compose exec -T db psql -U permitiq -d permitiq -v ON_ERROR_STOP=1 < db/007_identity_and_events.sql`.

The Gemini scanner reads `GEMINI_API_KEY` from `pipeline/.env`, or only that
key from `../VoteIQ/.env`. Never commit `.env` (it is git-ignored).

## Tests

```
pip install -r requirements-dev.txt
pytest tests                                   # unit tests
PERMITIQ_TEST_ADMIN_DSN="host=127.0.0.1 port=5432 user=permitiq password=... dbname=postgres" pytest tests
```

The DSN enables the PostGIS tests (they create and drop throwaway databases;
needs `psql` on PATH). 51 tests pass against PostgreSQL 16 + PostGIS 3.

## Status — what has and has not been verified

- Verified: migrations 001-007, identity/ledger behavior, idempotent and
  concurrent ingestion, Power BI role grants -- with synthetic data and the two
  user-supplied July 14, 2026 records.
- Fixed against this repo's real VB fixture (`tests/fixtures/virginia_beach_page1.json`):
  VB permit dates are text `YYYY/MM/DD`; field names `PermitType` / `StreetAddress`.
- **Not yet run against live services**: the VB permit and violation layers,
  FEMA NFHL, and Gemini on a real agenda PDF. The violations layer URL and all
  violation field names are unverified (`ingest_violations.py --inspect`).
- Known gaps: one `gpin` value per record (multi-parcel items are stored as
  text); the Gemini prompt targets Planning Commission agendas, not Council
  agendas; VB agendas print no case numbers, so planning leads stay one record
  per meeting until a reviewer confirms a link (`ingest_planning.py --confirm-link`).
