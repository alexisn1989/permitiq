<p align="center">
  <img src="static/logo.png" alt="PermitIQ" width="360">
</p>

**A building-permit intelligence platform for Hampton Roads, Virginia — built on real government open data, with a hard rule against fabricating anything.**

PermitIQ pulls building and development permit records directly from each city's own open-data system, normalizes wildly different schemas into one, geocodes addresses that arrive without coordinates, and serves an interactive map + filterable dashboard over the result.

The interesting part isn't the CRUD. It's that **civic open data is messy in ways that punish naive implementations**: two cities publish the same concept under different field names, one stores dates as strings that silently break SQL date filters, a third exposes a service whose name matches what you want but whose contents are from a different *state*. This README documents both the system and the specific traps it was built to survive.

---

## At a glance

| | |
|---|---|
| **Live dataset** | 10,850 real permits — 3,882 Norfolk, 6,968 Virginia Beach |
| **Geocoded coverage** | **91.1%** (9,886 mapped) — every remaining record has an explicit, auditable reason |
| **Test suite** | **541 tests**, all passing, zero live network calls |
| **Stack** | Python · FastAPI · SQLite (WAL) · vanilla JS + Leaflet · no ORM, no build step |
| **Data sources** | Socrata SODA API (Norfolk) · ArcGIS REST (Virginia Beach) · US Census Geocoder · Nominatim |
| **Backlog geocoding** | ~6 minutes for 7,800 addresses (was ~3 hours before batch rearchitecture) |

---

## Why this is harder than it looks

Three real problems, each of which would have silently corrupted the dataset if handled naively:

**1. A correctly-named service containing the wrong state's data.**
Planning documentation pointed at an ArcGIS FeatureServer literally named `Building_Permits_Pending` for Portsmouth, VA. It returns individual permit records with point geometry — exactly the right *shape*. Querying its distinct `countylocation` values live returned `DURH` and `WAKE`: Durham and Wake Counties, **North Carolina**. It's a generic multi-tenant hosted layer that happens to share a name. Building against it would have put hundreds of North Carolina permits on a Virginia map with total confidence.

**2. Date fields that aren't dates.**
Every field in Virginia Beach's feature layer — including `ApplicationDate`, `IssueDate`, `FinalDate` — is typed `esriFieldTypeString`. A standard `WHERE ApplicationDate >= DATE '2024-01-01'` predicate doesn't error against a string column; it *silently mismatches* and returns unfiltered-looking results from the wrong year. An incremental sync built on that would appear to succeed forever while quietly ignoring its own date window.

**3. Status vocabularies that don't overlap.**
Norfolk publishes 25+ distinct raw status values (`C/O Issued`, `Additional Information Required`, `Portal Submitted`, `Reviews in progress`…). Virginia Beach publishes 11 completely different ones (`Pre-Issuance`, `Pending Revisions`, `Ready to Issue`…). Neither is a subset of the other, and the same English word can mean different things in different jurisdictions.

The response to all three is the same principle, applied throughout: **verify against the live source before writing code, and never let an unknown value become a confident-looking guess.**

---

## Architecture

```mermaid
flowchart LR
    subgraph sources["Government open data"]
        N["Norfolk<br/>Socrata SODA"]
        V["Virginia Beach<br/>ArcGIS REST"]
    end

    subgraph ingest["run_ingest.py — the ONLY writer"]
        A["Locality adapters<br/>schema validation<br/>normalization"]
        G["Geocoding pipeline<br/>Census → Nominatim"]
    end

    DB[("SQLite WAL<br/>permits · geocode_cache<br/>ingestion_runs")]

    subgraph serve["app.py — strictly read-only"]
        API["REST + GeoJSON API"]
        UI["Leaflet map + dashboard"]
    end

    N --> A
    V --> A
    A --> DB
    G --> DB
    DB --> API
    API --> UI
```

**The load-bearing constraint: ingestion and serving never share a process.**

`app.py` exposes no `POST`/`PUT`/`PATCH`/`DELETE` on permits, never imports `run_ingest.py`, and has no route that triggers a sync. Writes happen exclusively through a CLI you run by hand or on a schedule.

This isn't stylistic. A sibling project of mine hit an out-of-memory kill in production because a data-loading subprocess was spawned from a running web server's request path. The separation here is enforced structurally — there is no code path from an HTTP request to a write — so the failure mode is impossible rather than merely avoided.

### Repository layout

```
app.py                  Read-only FastAPI app (8 routes)
db.py                   Schema, WAL connection helper, schema versioning
run_ingest.py           CLI — the only process that writes permit data
ingest/
  common.py             NormalizedPermit, retry/backoff, coordinate-precedence upsert
  geocode.py            Census/Nominatim pipeline, rate limiter, batch geocoding
  norfolk.py            Socrata adapter
  virginia_beach.py     ArcGIS adapter
scripts/
  geocode_backlog.py    Batch-first backlog runner with full observability
  backfill_status_bucket.py   Recompute derived values after a mapping change
static/                 index.html · app.js · style.css (no build step)
tests/                  189 tests, fixture-based
```

---

## Engineering decisions worth explaining

### Coordinate precedence, not `COALESCE`

Permits arrive from multiple provenance tiers: some sources carry native geometry, most need geocoding, and a human might one day correct a bad pin. The naive upsert is `COALESCE(existing.lat, incoming.lat)` — keep what you have.

That's wrong in both directions. It permanently locks in the *first* coordinate ever written (including a bad low-confidence geocode), and it blocks a genuinely better source from ever taking over.

The upsert implements an explicit precedence ladder — **manual > native > Census > Nominatim > missing** — tracked in a `coordinate_source` column. A re-ingest can upgrade a Nominatim guess to native geometry, but can never downgrade, and never touches a manual correction. This is enforced in SQL via a registered `coord_rank()` function so the logic lives in exactly one place.

### Schema-drift detection at batch level, not per record

Adapters declare `REQUIRED_FIELDS` and validate the live response before processing. The subtlety: **Socrata omits null-valued keys per row**, so checking each record individually would false-positive constantly on legitimately-empty fields.

The check runs against the *union* of keys across a page. A field missing from one record is normal; a field missing from every record in a non-empty page means the upstream schema changed, and ingestion fails loudly instead of quietly writing degraded rows.

A related ordering bug surfaced during testing: the drift check originally ran *before* the empty-page check, so a legitimately-empty result (no permits in a given date window) was misdiagnosed as "every required field vanished." Caught by a test that asserted on the date-filter parameter and failed for an unrelated reason.

### Provider-aware geocoding (a ~30× speedup)

Nominatim's usage policy allows roughly one request per second. The original implementation applied that pacing after **every** geocoding attempt.

Inspecting the live cache showed why that was expensive: **Census resolved 97% of successful geocodes.** The system was paying Nominatim's rate limit on thousands of calls that never touched Nominatim.

The rearchitected pipeline:

```mermaid
flowchart TD
    P["Pending permits"] --> NA{"Usable street<br/>address?"}
    NA -->|No| NOADDR["status: no_address<br/>zero network calls"]
    NA -->|Yes| DEDUP["Normalize + dedupe<br/>by address"]
    DEDUP --> CACHE{"In geocode_cache?"}
    CACHE -->|Hit| APPLY["Apply to all permits<br/>sharing that address"]
    CACHE -->|Miss| BATCH["Census BATCH endpoint<br/>up to 5,000 addrs/request"]
    BATCH --> VAL{"Valid?<br/>bbox + locality check"}
    VAL -->|Yes| APPLY
    VAL -->|No| RL["Nominatim rate limiter<br/>~1 req/sec, single-threaded"]
    RL --> NOM["Nominatim fallback"]
    NOM --> APPLY
```

Three compounding wins: **deduplication** (many permits share an address), **cache reuse** (7,028 memoized addresses), and **Census batch geocoding** (thousands of addresses per HTTP request instead of one).

The rate limiter itself tracks elapsed time against a monotonic clock rather than blindly sleeping, so calls already naturally spaced by other work don't oversleep. It's injected as a dependency, which lets the test suite verify real pacing behaviour with a fake clock — **no test sleeps for a real second**.

Result: a 200-permit batch went from ~350s to **11.4s**. The full 7,800-address backlog completes in about 6 minutes instead of an estimated 3 hours.

Nominatim remains deliberately conservative — single-threaded, one process, identifying User-Agent, every result cached, no retry storms, no parallel fan-out. Speed came from *not calling it unnecessarily*, not from leaning on it harder.

### Every unmapped record has a reason

Chasing a coverage percentage creates pressure to fabricate. PermitIQ classifies instead, and the categories sum exactly to the row count:

| `geocode_status` | Count | Meaning |
|---|---:|---|
| `matched` | 9,886 | Geocoded and validated |
| `no_address` | 830 | Source record has no street address at all |
| `not_found` | 132 | Real address, geocoder couldn't resolve it |
| `rejected_outside_area` | 2 | Resolved outside the Hampton Roads bounding box |
| **Total** | **10,850** | |

`no_address` is a real category, not missing data. Virginia Beach issues utility permits with values like `"Verizon - Poles 104"` in the address field — there is no location to resolve. These are detected by shape *before* any network call, so they cost nothing and are reported honestly rather than counted as failures.

Finding this distinction exposed a genuine bug: permits with `address = NULL` were excluded by the query's `WHERE` clause *before* reaching classification, leaving 829 real records stuck in `pending` forever — invisible, never retried, never explained. The filter was removed so those rows reach the `no_address` branch instead.

### Derived values need a backfill path

`status_bucket` (the 5-value normalized enum powering map colors and filters) is computed at ingest time and stored, not derived at read time — a deliberate choice for query performance.

The consequence: expanding a status mapping only affects *future* ingests. When Norfolk's mapping grew from 6 entries to 26 after observing real production values, 1,030 already-stored rows were still bucketed as `other`.

`scripts/backfill_status_bucket.py` recomputes stored values against the current mapping — dry-run by default, values-only, no schema change. It reported the exact 1,030 rows and their before→after transitions for review before anything was written.

### Security and correctness in the read layer

- **Sort injection.** SQLite can't parameterize column or direction identifiers, so `?sort=` was a genuine injection surface. Both the column and the direction are validated against fixed allowlists before ever reaching the SQL string. `?sort=id;DROP TABLE permits;--` returns `422`, and the table is verifiably intact afterward.
- **Every filter is parameterized.** A search for `' OR '1'='1` is treated as a literal string and matches zero rows.
- **`raw_json` is never exposed.** Each source's complete original record is stored for auditability, but the API's returned-field list is an explicit allowlist — not a filter applied after the fact — so there's no refactor that can accidentally leak it.
- **Output escaping.** Every externally-sourced string is escaped before insertion into the DOM. Verified by running an `<img src=x onerror=...>` payload through the actual render path, not just the helper in isolation.
- **Stable pagination.** Default ordering is `COALESCE(issued_date, applied_date) DESC, id DESC` — the `id` tiebreaker prevents records shuffling between pages when timestamps collide.

### Idempotency, proven rather than assumed

Ingestion uses `UNIQUE(locality, source_permit_id)` with an upsert, and distinguishes *inserted* / *updated* / *unchanged* / *skipped* — a no-op conflict is never miscounted as an update.

This was verified by running the identical ingest command twice against live sources: **0 duplicate key pairs across 10,850 rows, 0 inserts on the second run, 0 false updates, and all 297 existing coordinates preserved byte-for-byte.**

The second run also surfaced something real: Norfolk's skip count changed between runs. Investigation (rather than assumption) showed the live feed itself had shifted within the ~15-minute gap, producing genuine in-batch duplicate permit numbers. Source behaviour, not an application bug — and the dedup logic handled it correctly.

### Observability that distinguishes failure modes

An `ingestion_runs` table records every sync with success/failure status and counts. Without it, a silently-broken ingest is indistinguishable from "no new permits this week." `/api/data-status` reads from it directly rather than inferring health from row counts.

The backlog runner reports cache hits, unique addresses sent to Census, Census matched/rejected/not-found, Nominatim attempted/matched/not-found, no-address, errors, coverage before and after, and remaining backlog — **per locality**. Provider failures are never collapsed into one vague "failed" number when the reason is known.

---

## Running it

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python scripts/migrate_db.py      # create / migrate the schema (the API never does)
python -m uvicorn app:app --port 8090
```

Open `http://localhost:8090`. The database starts **empty** — PermitIQ ships no seed data, because fabricated sample rows in a civic-data tool are exactly the failure this project is built to avoid.

### Populating data

```powershell
# Inspect what would be fetched, write nothing
python run_ingest.py --locality norfolk --limit 20 --dry-run

# Real ingest — a ~90-day window finishes fast; the 2-year default takes longer
python run_ingest.py --locality all --since 2026-04-24

# Work the geocoding backlog without re-fetching from source (batch-first, fast)
python scripts/geocode_backlog.py --locality all --analyze
```

`--locality` accepts `norfolk`, `virginia_beach`, or `all`. Re-running is always safe. `--analyze` prints the unmapped-reason breakdown.

### Flood exposure (Virginia Beach, FEMA NFHL)

The API is read-only: it opens SQLite with `mode=ro`, never creates or migrates schema, and answers **503** with a hint until `scripts/migrate_db.py` has run. Flood data is loaded and tagged by CLI writers, in this order:

```powershell
python scripts/migrate_db.py                 # schema v2: flood tables + permit_flood_effective view
python scripts/profile_vb_fields.py          # observed PermitType / WorkType / ConstructionType values
#   -> fill [cohort].include in config/flood_report.toml from that output (ships empty on purpose)
python scripts/load_flood_zones.py --dry-run # FEMA NFHL layer 28, DFIRM 515531: feature count, zone mix, cell sizes
python scripts/load_flood_zones.py
python scripts/tag_flood_zones.py --dry-run  # before -> after class transitions
python scripts/tag_flood_zones.py
python scripts/flood_report.py               # reports/flood_exposure_vb.md + .csv
```

- **Classes:** `sfha_v` (VE/V), `sfha_a` (AE/A*), `shaded_x` (0.2% annual chance), `matched_non_sfha`, `no_polygon` (geocoded but no FEMA polygon matched — a possible coverage gap, **never** reported as "outside"), `not_geocoded`; derived at read time: `untagged`, `out_of_scope` (localities without a DFIRM, i.e. Norfolk). Overlaps resolve VE/V > AE > other A > shaded X > other; a point on a boundary is inside.
- **Revisions:** each load is a `flood_zone_loads` row identified by a content hash, not FEMA's vintage label. One active load per DFIRM is enforced by a partial unique index. A tag is current only if it was made against the active load at the permit's current coordinates — defined once in the `permit_flood_effective` view, which every read and the tagger use. Stale tags read as `untagged` with no zone details.
- **Map layer:** the loader pre-builds 0.05° grid cells (SFHA + shaded X only), each under a hard 2 MB limit after progressive simplification; a load with an oversized cell is stored inactive and the previous load keeps serving. Cells are served as stored bytes with an ETag (304 on `If-None-Match`).
- **Report definitions:** population = issued, non-void VB permits; cohort = the explicit allowlist in `config/flood_report.toml`; baseline = all issued permits. Shares use matched permits only as the denominator; `no_polygon`, `not_geocoded` and `untagged` are shown beside every row as counts. Shares are suppressed below `min_n` (20). Only context filters (`issued_from`, `issued_to`, `zip`) apply, to cohort and baseline alike. `/api/flood-stats` and `flood_report.py` share one implementation (`ingest/flood_stats.py`).
- **Caveats:** address point ≠ building footprint; effective FIRM only; issued ≠ built; SFHA ≠ full flood risk.

### Land-use approvals → permits (Virginia Beach, Stage 2)

Links City Council land-use decisions (from a read-only VoteIQ export) to later building permits at the same property. It answers three questions: which approvals have no permit yet, how long approval-to-permit takes, and what flood class each approval falls in.

```powershell
# on the VoteIQ host -- read-only, standalone, changes nothing in VoteIQ
python tools/export_vb_council_votes.py /var/data/polls.db > vb_council_votes.jsonl 2> vb_schema.txt

python scripts/profile_land_use_export.py --file vb_council_votes.jsonl
#   -> fill config/land_use.toml from that output (ships empty on purpose)
python scripts/migrate_db.py                                      # schema v3
python scripts/ingest_land_use_decisions.py --file vb_council_votes.jsonl --dry-run
python scripts/ingest_land_use_decisions.py --file vb_council_votes.jsonl
python scripts/ingest_land_use_decisions.py --geocode
#   -> set [coverage] backfill_from + backfill_run_id to the VB backfill you actually ran
python scripts/link_decisions_permits.py --dry-run
python scripts/link_decisions_permits.py
python scripts/review_land_use.py --outcomes reports/land_use_review.csv   # after filling it in
python scripts/review_land_use.py --links reports/link_review.csv          # after filling it in
python scripts/tag_flood_zones.py --decisions
python scripts/land_use_report.py                                 # reports/land_use_vb.md + CSVs
```

- **Approval is never inferred.** "Vote Complete" with more yes than no votes could be a motion to deny or defer. Vote counts and the result string are stored only as evidence. A decision is approved only through `review_land_use.py --outcomes`, which requires evidence: the minutes, the IQM2 item, or the verbatim motion. Until that review is done, the lead list is empty.
- **Source conflicts are quarantined.** If member rows for one item disagree on title, result or date, nothing is picked. Every variant is kept, and review must resolve the conflict.
- **Coverage is verified.** The observation window starts at a named VB backfill run, checked against `ingestion_runs`, never at the oldest permit on file. It ends at the latest successful refresh. If either end is uncertain (approval before the backfill, no refresh, or a refresh older than 14 days), the status is `observation_uncertain`, never "no permit yet".
- **Links are candidates.** Matching uses a printed GPIN (first 10 digits), an exact house number and street, or a printed house-number range. A property match may not belong to the approved project, so only `review_land_use.py --links` confirms a link. Nearby addresses go to the review file and are never linked automatically.
- **Time to permit** has three measures: first permit application, first issued permit, and first new-construction application. Each counts once per decision (its earliest permit) and is reported twice: confirmed links (the headline) and including candidates. The report shows the median and IQR (nearest-rank); open decisions are counted but never imputed.
- **Exports are checked.** The exporter writes its row count in the same read snapshot as the rows. The ingest and the profile refuse any file whose header is missing or whose row count does not match, so a truncated export can't ingest as complete.
- **Reviews expire when their facts change.** Each review stores a fingerprint of the title, result, meeting date and source conflicts it was made on. If a re-ingest changes any of these, the decision becomes `outcome_review_stale` and needs a fresh review. The old review is kept for audit. The v4 migration computes fingerprints for existing decisions, and reviews made before v4 also count as stale.
- **One window rule.** A link counts only if the permit was applied for on or after the approval and within `link_window_months`, using SQLite `date()` arithmetic everywhere. The review import refuses confirmed links outside the window, and the linker drops them. Every read goes through the `decision_permit_links_effective` view: status, link counts, timing, the flood fallback, the API, and the ingest's geocode skip.
- Status is defined once, in the `land_use_decision_status` view.

### Parcel enrichment (Virginia Beach, Stage 3)

Matches permits and Council decisions to city parcels (`Property_Information/MapServer/12`). It adds zoning, land use, property class and acreage, and classifies flood exposure by the **parcel polygon** next to the Stage 1 address point. Parcel flood classes are a **screening estimate**: the city's parcel terms say the data is "not valid for … flood-zone determination".

```powershell
python scripts/migrate_db.py                       # schema v5
python scripts/load_parcels.py --inspect           # fields, maxRecordCount, lastEditDate, returnCountOnly
python scripts/inspect_pc_actions_layer.py --out p.json --compare-council  # Planning/Council Actions layer, read-only
python scripts/load_parcels.py --dry-run
python scripts/load_parcels.py                     # or: --file vb_parcels.geojson --expected-count N
python scripts/match_parcels.py --dry-run
python scripts/match_parcels.py
python scripts/review_parcels.py reports/parcel_review.csv   # after filling it in (disputed matches)
python scripts/tag_parcel_flood.py
python scripts/link_decisions_permits.py           # adds the same_parcel tier
python scripts/parcel_report.py                    # reports/parcel_vb.md + .csv
```

- **Complete loads only.** The loader reads the layer's count and the full list of object IDs, then fetches in batches of at most 2,000 by object ID. A load is activated only if the received IDs equal that list exactly (none missing, none duplicated, none extra) and the count hasn't changed during the fetch. Otherwise nothing is written and the previous load keeps serving. `--file` requires `--expected-count`.
- **Matching rules.**
  - Permits: an exact 14-digit GPIN; else a 10-digit GPIN that matches exactly one parcel; else the point inside exactly one parcel. Several candidates means `ambiguous`, never a guess.
  - Decisions: the printed GPIN, else the exact parcel address, else the geocoded point.
- **Disputes.** When a GPIN and a point land on different parcels, the match is **disputed**. It is shown with both parcels and excluded from `same_parcel` links and from the `gpin` link tier until `review_parcels.py` resolves it:
  - `gpin_correct` re-enables both;
  - `point_correct` switches the parcel to the point's;
  - `unresolvable` keeps the match excluded.
- **Review freshness.** A review records the facts it judged: GPIN, address, house-number range, point and parcel load. If any of those change, the review stops applying.
- **Parcel flood class.** The class is the most hazardous of V, A or shaded X covering at least `min_share` of the parcel (`config/parcels.toml`, default 1%).
  - `screening_sliver`: a hazard zone touches the parcel but none reaches the threshold.
  - `screening_partial_coverage`: part of the parcel has no FEMA polygon.
  - `matched_non_sfha`: only when the whole parcel is mapped outside the SFHA.
  - All five area shares are always shown.
  - `/api/flood-stats?basis=parcel` reports the parcel basis; screening, unmatched, ambiguous and disputed parcels are coverage classes and never enter a denominator.
- **Cross-check.** The report compares our parcel class with the city's own `FEMA_ZONE_BLDG` field.
- **No assessed values or lead score yet.** There is no assessor bulk source.

### Real-data run, measured accuracy, lead screen

One logged runbook runs the whole Virginia Beach chain on real data. The Council export comes from the VoteIQ host via `tools/export_vb_council_votes.py`.

```powershell
python scripts/run_vb_pipeline.py --phase profile --council-export vb_council_votes.jsonl
#   -> fill config/flood_report.toml and config/land_use.toml from the profile logs
python scripts/run_vb_pipeline.py --phase full --council-export vb_council_votes.jsonl `
    --parcels-file vb_parcels.geojson --parcels-count N --reviews-dir reviews\
#   -> check reports\run_*\accuracy_sample.csv (an independent search for each lead row)
python scripts/accuracy_tally.py reports\run_<ts>\accuracy_sample.csv --apply-links
python scripts/run_vb_pipeline.py --phase full ... --reviews-dir reviews\   # re-link + reports after reviews
```

- **Order.** Council decisions are ingested and geocoded before parcel matching, flood tagging and linking. Imported reviews trigger a re-link before any report.
- **Run manifest.** `reports/run_<ts>/run_manifest.json` records each step's command, exit code, times and output tail, the order executed, and the source dates.
- **Skipped steps.** A refused step (e.g. the cohort not configured yet) is recorded as skipped, with its reason.
- **Unmatched reasons.** `unmatched_reasons.csv` lists every entity per aspect that did not fully match, with its reason code; `unmatched_reasons_counts.csv` totals them.
- **Cross-row conflicts.** If a decision's street-address parcel is not among its printed-GPIN parcels, that is a cross-row conflict. Those rows feed no parcel or GPIN link until `review_parcels.py` records a decision-set verdict (`rows_consistent`, `gpin_rows_correct`, `street_rows_correct`, `unresolvable`). A multi-parcel application, with more GPINs than addresses, is not a conflict.
- **Stale reviews come first.** A review whose facts changed reads `outcome_review_stale` even when the item also has source conflicts; `has_conflicts` shows both.
- **Frozen accuracy samples** (`accuracy_sample.py`) record:
  - the source-load IDs and Council export SHA;
  - a content fingerprint of permits, decisions, links and reviews;
  - each stratum's population N, the seed and the selection rule;
  - per-row fact fingerprints and a sheet hash.
- **Judgments are explicit.** Links use `same_project`. Leads use `lead_check` plus a required `search_done`, an independent search outside `permits.db`. Flood disagreements use `flood_check`.
- **The Council chain has its own strata** (from the Clerk's Summary of Council Actions, so they do not wait on the VoteIQ export):
  - `council_outcome` (`outcome_check`: right / wrong / cant_tell): were the outcome, deferral date and tally read correctly from the Clerk's page?
  - `council_link` (`same_application`: yes / no / cant_tell): are the Council item and the Planning Commission item the same application? Ambiguous links are not sampled; they set no stage.
  - Rows carry links to the Clerk's page and the agenda PDF, never names or subject text. They are measurements only, never applied as reviews; the checklist counts them toward "Council outcomes checked" and "Council-to-hearing links checked".
- **The tally** (`accuracy_tally.py`):
  - refuses an edited sheet;
  - drops rows whose facts changed;
  - reports per-tier adjudicated-sample precision with Wilson 95% intervals, beside N, `cant_tell` and the stale count;
  - gives a pooled figure only as a labeled population-weighted estimate, when every tier was adjudicated;
  - with `--apply-links`, turns only fresh `same_project` yes/no link rows into link reviews.
- **`/api/quality` and `quality_report.py`** show live coverage, unresolved cases and refresh age. Accuracy is withheld (`"freshness": "stale"`) whenever the data it was computed on has changed. A re-run that changes nothing keeps the tally fresh.
- **Locality acceptance checklist and run record.** The quality report opens with the standard a locality must meet. Each line is pass, fail or "not yet", filled from the report's own numbers: backfill verified, source dates, official cross-check, outcome reviews, link tiers / leads / flood disagreements checked to min(30, N), unresolved cases, and no failed or gap steps. Every non-ok step from `run_manifest.json`, plus source row counts, is listed beneath it. "Not yet" is never a pass.
- **Lead screen.** `/static/lead.html?key=<decision_key>` (also "Open lead details" in the map popup) shows on one screen:
  - the Council decision with its review evidence;
  - the parcel(s) with dispute state and an outline;
  - later permits with match reason and review state;
  - flood screening;
  - data as of.
- **Marking links.** "Mark bad link" / "Confirm" queue verdicts in the browser, and "Download review file" saves a CSV for `review_land_use.py --links`. The API stays read-only.
- **Lead summary** (`ingest/lead_facts.py`), on the lead screen, in `GET /api/land-use/decisions/{key}` as `lead_facts`, and as extra columns in `land_use_leads.csv`:
  - **Applicant, request and size** (acres, units, lots, square feet) are extracted from the Council title and shown with the exact text they came from, labeled unverified.
  - **Applicant names** are shown only when every listed applicant is an organization (LLC, Inc, Corp…); otherwise the name is withheld. Trusts count as individuals, because they are often named after a person.
  - **Parcel acreage** comes from the parcel layer and is shown separately.
  - **Timing, for verified approvals only:**
    - days from approval to the last permit refresh;
    - the end of the link window;
    - a benchmark range for the first permit application, from confirmed links only, over all verified approvals. It is suppressed below `min_n`.
  - **"Older than the typical range"** is flagged when no permit has been observed and the lead is past that range. Extracted values never affect status, links or approval.
- **Backfill completeness.** `verify_permit_backfill.py --since …` compares the source's own count and OBJECTID set for the same `ApplicationDate` window with the database.
  - Truly missing permits are fetched by ID and topped up. Duplicate permit numbers, rows without a permit number, and rows with no application date are reported.
  - Counts by application year are listed side by side.
  - The linker accepts a backfill run only with a verified check.
- **Stages.** `--stage provisional` (the default) marks reports and lead files provisional and draws no accuracy sample. `--stage final` refuses without outcome reviews, or with stale ones, and draws the sample from the reviewed snapshot.
- **Report-only phase.** `--phase report` regenerates reports only, keeping the sample's recorded N beside today's counts.
- **Hand-off.** `handoff_extract.py` builds a hand-off folder: the run, a database inventory with its SHA-256, and CSV extracts without `raw_json`. The database file itself is not included.
- **Review history (schema v8).** Database triggers record every change to a human review in `review_audit`: outcomes, link verdicts, parcel verdicts and decision-set verdicts.
  - **Each change is tied to its import** in `review_imports`: the CSV file and its SHA-256, and the acting user (`--actor`, or the OS user).
  - **Accuracy imports** (`accuracy_tally.py --apply-links`) record the checked sample sheet as the source. The generated verdict CSV is recorded as a derived file.
  - **Writes outside an import are refused.** Review writes are allowed only inside `db.review_import()`, one transaction with the provenance. `review_audit` and `review_imports` refuse `UPDATE`, `DELETE` and `INSERT OR REPLACE` onto existing rows.
  - **Existing reviews** get one `baseline_snapshot` at migration, with no provenance: earlier changes were never recorded.
  - **Each history entry** names the review's author separately from the person who ran the import.
  - **Limits:** this is not authenticated and not tamper-proof. The acting user is self-reported, and anyone who can write the database file can alter it.
  - **Where to see it:** `GET /api/audit/{review_table}` with exactly that table's key, and "Review history" on the lead screen.

### Planning Commission hearings (earliest leads, Virginia Beach)

Every land-use application goes to a Planning Commission public hearing weeks before City Council votes on it. The Commission's agendas and minutes are PDFs on the city's S3 bucket.

```bash
python scripts/pc_index.py --out reports/pc_index.json        # the city's agenda index (needs virginiabeach.gov)
python scripts/ingest_pc_documents.py --index reports/pc_index.json --reports-dir reports
python scripts/link_pc_council.py                             # possible later Council decisions
python scripts/pc_report.py --out reports                     # pc_leads.csv, pc_coverage.json
python scripts/run_vb_pipeline.py --phase full --pc ...       # the same steps inside the runbook
```

- **Parsing** (`ingest/pc_agenda.py`): the 2021 (annotated), 2024 and 2026 agenda layouts and the minutes.
  - **Agendas:** each item yields the applicant and owner, request, address, GPINs, Council district, Accela records, staff planner and description.
  - **Minutes:** each item number yields the recommendation and vote tally.
  - **Text extraction:** `pypdf`, in an isolated interpreter.
- **Never dropped:** every item keeps its raw block. An item that doesn't parse is kept with its warnings and listed in `pc_parse_review.csv`.
- **A recommendation is not an approval.** `pc_item_status` gives `scheduled`, `recommended_approval`, `recommended_denial`, `deferred`, `withdrawn`, `recommendation_unmapped` or `no_recommendation_found`. Unknown recommendation text maps to `unmapped`, never to approval. Nothing here changes Council statuses or links.
- **Council links are candidates:** a later Council decision on the same GPIN or street address, within 12 months of the hearing.
- **Privacy:**
  - names of private individuals are withheld in the API, UI and CSV: organizations only, and any person in a list withholds the whole name;
  - raw blocks are never served and stay out of the hand-off extract.
- **Coverage:** agendas found are compared month by month against the expected hearings (the 2nd Wednesday of each month). Coverage reads "unverified" until the city's agenda index has been read.
- **Where to see it:** the lead screen (`/static/lead.html?pc=<item_key>`), the map layer "Planning Commission hearings", and `GET /api/land-use/pc-items[/{key}]`.

### Applications as filed (earliest leads, Virginia Beach)

The city's "Planning Commission and City Council Actions" layer maps every land-use application with its filing date and Accela record number.
- On the real 2026 agendas, applications were filed a **median of 77 days before their Planning Commission hearing**.
- 81 of 92 agenda record numbers appear in the layer.

```bash
python scripts/load_filings.py --inspect          # fields and counts; writes nothing
python scripts/load_filings.py --since 2023-01-01  # after load_parcels.py: the parcels inside each boundary are linked
python scripts/filing_report.py --out reports      # filing_leads.csv: filed or scheduled, newest first
python scripts/run_vb_pipeline.py --phase full --filings ...
```

- **Complete loads only:** the layer's count and OBJECTID list must match what was received (`ingest/arcgis.py`, shared with the FEMA loader). Otherwise nothing is written.
- **One row per Accela record:** polygons sharing a record are merged. Records with no filing date are counted, never shown.
- **Parcels:** the parcels whose representative point lies inside the boundary are linked. Flood class is a screening estimate from those parcels.
- **No status in the layer:** a filing reads "Filed — outcome not yet known" until a Planning Commission record with the same Accela number gives a stage. It never reads "approved". The layer's "Approval Date" field is empty (2 values in 6,463) and is not used.
- **Plan types:** only codes confirmed against real agenda request types get a label (CUP, CRZ, SVR, STC, MDC); others are shown as recorded.
- **Names:** applicant and project names are shown only for organizations.
- **Where to see it:** the map layer "Applications filed", the lead screen (`/static/lead.html?filing=<accela id>`, with the boundary drawn), and `GET /api/land-use/filings[/{pln_id}]`.

### City Council outcomes (the Clerk's official record, Virginia Beach)

The City Clerk publishes a **Summary of Council Actions** for every meeting in its Laserfiche archive (`edocs.vbgov.com`, folder SUMMARY ACTIONS). The summaries are scanned tables with an OCR text layer, which the archive's own JSON service returns page by page (`ingest/edocs.py`).

```bash
python scripts/ingest_clerk_summaries.py --years 2021-2026 --reports-dir reports   # after the PC items are stored
python scripts/clerk_report.py --out reports    # council_actions.csv: every land-use item, newest first
python scripts/run_vb_pipeline.py --phase full --pc --clerk ...
```

- **Measured on the real archive (2021 – Oct 2026):**
  - 150 summaries, every one listed for those years read;
  - 739 land-use items: 635 approved (594 with conditions or proffers), 27 denied, 65 deferred, 10 withdrawn, 2 not recognized;
  - 73 of 97 Planning Commission items reached a Council stage; 8 of 8 sampled links matched the source.
- **What is read per item:** label, request type, street addresses, district, the motion as printed and its outcome, and the tally (`11-0`). Per-member votes are not read: in the OCR text they are often garbled.
- **OCR is handled, never guessed:**
  - known misreads are mapped: `II-u` = 11-0, `J.S.` = J.5., `fora` = for a, `22'd Street` = 22nd Street;
  - motion words that land inside an address are removed before addresses are read;
  - an item whose tally, address, label or deferral date cannot be read keeps that field empty and goes to `clerk_parse_review.csv` (with the Clerk's page link).
- **Coverage:** each year's archive listing is the expected set. A year is `ok` only when every listed summary was stored; otherwise `incomplete`, and the step exits 3 (a gap, never success).
- **Link to the Planning Commission item:**
  - same street address;
  - Council date within 12 months after the hearing;
  - the most recent hearing at that address.
  
  A link is only "possible" when the request types disagree, when unit numbers differ (two short-term rentals in one building), or when several items at that hearing share the address. A possible link never sets a stage.
- **Council stage:** the latest Council action decides the stage: `council_approved`, `council_approved_with_conditions`, `council_denied`, `council_withdrawn` or `council_deferred`. If actions on that date disagree, the stage is `council_conflicting`. Filings take the stage of their PC item. The PC recommendation itself is unchanged and still never reads "approved".
- **Names:** "APPLICANT / OWNER" parts are checked one by one (and unspaced `&` splits parties). Only organizations are shown. Subject text and raw blocks are stored for review and never served.
- **Where to see it:** the "City Council action (Clerk's record)" card on the hearing and filing lead screens, the map popups, and `GET /api/land-use/council-actions[/{key}]`.

### Site constraints (screening, Virginia Beach)

City GIS layers that stop, delay or add cost to a project, measured per parcel next to flood and Council data (`ingest/constraints.py`).

```bash
python scripts/load_constraints.py --inspect     # layer metadata and counts; writes nothing
python scripts/load_constraints.py --all         # each layer complete or not at all
python scripts/load_board_actions.py --all       # CBPA / Wetlands Board / Historical Review Board history
python scripts/tag_parcel_constraints.py         # lead parcels (--all: every parcel)
python scripts/run_vb_pipeline.py --phase full --constraints ...
```

- **Federal and state layers:**
  - **USDA soils** (SSURGO, survey area VA810, Soil Data Access): map units by acreage, water table within 18 in (`wtdepannmin` < 46 cm) and hydric map units (≥ 50% hydric components). This replaces the city's older soil map, 2 of whose 45 units are no longer in the survey.
  - **USFWS National Wetlands Inventory:** mapped wetland share by type (not a delineation).
  - **National Register of Historic Places** (NPS): listed properties and sites within 500 ft.
    - NPS often maps a listing as a "circumscribed" box rather than its boundary (e.g. a 159-acre box around one house). A box around a single building is not used as an area: its point gives the location.
    - A box around a district is reported as "the approximate mapping area … the listed boundary may be smaller", never as "inside a listed property".
  - **NWI excludes open estuarine / marine deepwater:** the bay and ocean are one 2.75-million-acre polygon the service cannot return, and they are open water, not a wetland on a parcel.
  - **VDOT:**
    - 2025 traffic counts: the nearest counted segment within 300 ft, with route, ADT, date and VDOT's quality code;
    - approved Six-Year Improvement Program projects within 1,000 ft.

    No Traffic Impact Analysis trigger is claimed: that needs a threshold rule.
  - **Not reachable:** the state's DHR service (Virginia Landmarks Register) answered 502; it is listed as not covered.
- **Layers** (city GIS):
  - Chesapeake Bay RPA boundary;
  - Southern Watersheds buffer;
  - floodplains subject to special restrictions;
  - shrink-swell soils, city soils;
  - Historic and Cultural Overlay, Historic Kempsville Overlay;
  - dam break inundation zones.
- **Board history:** 2,697 CBPA Board actions, 2,117 Wetlands Board actions and 154 Historical Review Board actions, merged per Accela record. Each parcel's history with those boards is shown.
- **Measured, never estimated:**
  - polygon layers give acres and share of the parcel;
  - line layers give the length inside the parcel and the distance to it;
  - soils give the mapped soil names with acres.
  
  There are no unit-yield or density estimates and no invented scores.
- **The RPA layer is a boundary line, not buffer areas.** Profiled on the real data: 2,060 untyped lines, 1 closed ring; polygonizing gives 72 fragments about 49 ft wide. So the card says "the RPA boundary crosses / lies N ft from this parcel" and never claims RPA acres.
- **Complete loads only:**
  - each layer's count and OBJECTID set must match;
  - a layer that fails leaves the others loaded and the step exits 3 (a gap);
  - measurements are keyed by (parcel load, layer load), so a new load reads as not measured until re-tagged.
- **Checked against the source:** on 4 lead parcels × 4 layers, the city's own server-side intersection queries agreed with every measurement.
- **Utilities: what is public and what is not.**
  - The city publishes its utility project-file index: 665 sewer, 444 water, 234 combined and 416 pump-station project areas, dates mostly unrecorded.
  - These are loaded as "a city sewer / water project is on file here", and pump-station projects as the distance to the nearest one within 1,000 ft. They show that utility work happened or is planned nearby. They are not pipe locations, service areas or capacity, and the card never presents them as such.
- **Not covered, said so on every card:**
  - sewer / water capacity and pipe locations (not published; a Virginia FOIA request is the route, and capacity is never inferred from pipe size);
  - hydric soils and water table (USDA), NWI wetlands, VDOT traffic, state and National Register historic boundaries: these plug into the same engine once their hosts are reachable.
- **Where to see it:**
  - the "Site constraints (screening)" card on the hearing and filing lead screens;
  - the map's "Site constraint layer" selector;
  - `GET /api/parcels/{gpin}/constraints`;
  - constraint columns in `filing_leads.csv` and `pc_leads.csv`;
  - a quality-report section and checklist line.

### Chesapeake: a pipeline city (no building permits published)

Chesapeake publishes no individual building permits. Its `BuildingActivity` layer is right-of-way and franchise utility permits. It does publish its development pipeline, so Chesapeake is loaded as a pipeline city (`ingest/chesapeake_records.py`, schema v14):

```bash
python scripts/load_parcels.py --locality chesapeake         # 92,805 parcel features (MAP_PARCEL, 13 digits)
python scripts/load_flood_zones.py --dfirm 510034           # FEMA NFHL for Chesapeake
python scripts/load_chesapeake.py --all                     # cases, development plans, ROW permits
python scripts/tag_parcel_flood.py --locality chesapeake
python scripts/load_constraints.py --layer ches_rpa_buffer  # ... each ches_ layer
python scripts/tag_parcel_constraints.py --locality chesapeake
python scripts/chesapeake_report.py --out reports/          # chesapeake_cases.csv, development_plans.csv
```

- **Land-use cases** (Development Tracking): use permits, subdivisions, rezonings, site plans, PUDs.
  - 3,789 features become 3,738 cases (multi-polygon cases are merged).
  - Each case carries its Planning Commission, City Council and architectural-review actions and dates, the staff recommendation, and the final action.
  - Action codes are labelled from the layer's own coded domain. An unknown code stops the load.
  - 627 rows were imported from a 2014 shapefile. They are marked legacy and hidden from lists unless asked for.
- **Development plans** (1,327) and **right-of-way permits** (1,035) live in `development_records`. They are never stored as or counted as permits.
- **Parcel links:**
  - by parcel number first, then spatially;
  - 97.6% of cases, 98.9% of plans and 98.7% of ROW permits have a parcel.
- **Site constraints for Chesapeake** (`ches_` layers, measured only on Chesapeake parcels):
  - RPA buffer polygons: acres are measurable here, unlike Virginia Beach's boundary line;
  - Chesapeake Bay Preservation Areas, proffered rezoning areas, zoning;
  - the local historic district, the Great Bridge gateway, the Watershed Protection District;
  - the Fentress APZ, overlay and restricted easement;
  - shrink-swell soils;
  - USDA soils (VA550), NWI, National Register, VDOT.
- **Public sewer and water, as published:**
  - the HRSD service area;
  - pump-station service areas;
  - areas without city sewer or water;
  - the nearest gravity sewer main and water line, with size, material and year.

  Capacity is never inferred.
- **Checked against the sources:**
  - 5 cases and 5 plans matched the city's live layers field for field;
  - 4 parcels × 6 layers (RPA buffer, unserved sewer, Fentress APZ, pump-station areas, CBPA, gravity sewer within 300 ft) agreed with the city server's own spatial queries;
  - 4 parcels' flood classes agreed with FEMA's zones;
  - Virginia Beach results stayed identical with Chesapeake loaded.
- **Privacy:**
  - the city's free-text case notes are never served, since they can name owners or requesters;
  - project names and plan titles are shown only when clearly an organization;
  - staff login IDs on the plan layer are never stored.
- **Building permits** come from Accela Citizen Access search results, downloaded by hand (the site's own "Download results", one year per file; nothing queries Accela):

  ```bash
  python scripts/import_chesapeake_permits.py --dir downloads/ --counts downloads/counts.csv   # file,start,end,page_count
  python scripts/geocode_backlog.py --locality chesapeake --census-only
  python scripts/match_parcels.py --locality chesapeake
  ```
  - **Complete or nothing:** each file's rows must equal the count the search page showed, dates must fall in the file's window, and windows must be contiguous.
  - **Statuses** are mapped only for those seen in real downloads; an unseen one stops the import.
  - **Project names** are mostly owners' names, so they are kept only when clearly an organization.
  - **Parcels:** the export has no parcel number, so a permit matches a Chesapeake parcel by exact address, with the geocoded point as a check. Disagreement is a dispute (no link until reviewed); several parcels at one address stay ambiguous unless the point picks one.
  - **Real run (10/29/2024 – 10/28/2026):**
    - 33,989 permits, both years equal to the page counts;
    - 74% geocoded by Census;
    - 85.1% on a parcel (28,352 by address, 583 by point);
    - 2,070 disputes (median 139 ft between the Census point and the address parcel);
    - 1,050 approved cases have a same-parcel permit since their final action (candidates, not confirmed links);
    - Virginia Beach results hash-identical.

---

## API

Read-only. No mutation routes exist on `/api/permits*`.

| Route | Purpose |
|---|---|
| `GET /health` | Liveness + schema check |
| `GET /api/permits` | Filter, search, sort, paginate |
| `GET /api/permits/{id}` | Single record |
| `GET /api/facets` | Locality availability + observed types/statuses (dynamic) |
| `GET /api/stats` | Totals, mapped/unmapped, geocode + status breakdowns |
| `GET /api/data-status` | Per-locality sync history from `ingestion_runs` |
| `GET /api/export.geojson` | GeoJSON of mapped permits (capped; errors rather than truncating) |
| `GET /api/export.csv` | Same filters, all permits incl. unmapped, with flood fields — Power BI-ready |
| `GET /api/flood-stats` | Flood-exposure shares: cohort vs baseline, by year and ZIP (context filters only; population filters ⇒ 422) |
| `GET /api/flood-zones/index.json` | Active flood load's display cells (bbox, bytes, etag) |
| `GET /api/flood-zones/{x}/{y}.geojson` | One display cell (ETag / 304; 413 over the size limit) |
| `GET /api/land-use/decisions` | Council land-use decisions with status, links and flood class (filters: `status`, `outcome`, `land_use_label`, `flood_class`, `decided_from/to`, `match_tier`) |
| `GET /api/land-use/decisions/{key}` | One decision: addresses, linked permits with tier and state, conflicts, review |
| `GET /api/land-use/export.csv` | Same filters; the lead list is `?status=no_candidate_permit`; every row carries the permit observation window |
| `GET /api/land-use/stats` | Counts by status, label and year; time-to-permit blocks |
| `GET /api/parcels/{gpin}.geojson` | One parcel outline from the active parcel load (ETag / 304; 404 if absent) |
| `GET /api/quality` | Coverage, unresolved cases, refresh age; accuracy only while its data snapshot is current |
| `GET /api/data-as-of` | Recorded source dates and the permit observation window |
| `GET /api/land-use/pc-items` | Planning Commission agenda items with status; filters `status`, `district`, `date_from/to`, `include_city`; coverage summary |
| `GET /api/land-use/pc-items/{key}` | One item: request, parcels and flood class by GPIN, recommendation and vote, source PDFs, possible Council decisions |
| `GET /api/land-use/filings` | Applications as filed, with stage; filters `stage`, `pln_type`, `filed_from/to`; active load summary |
| `GET /api/land-use/filings/{pln_id}` | One application: boundary, parcels, Planning Commission items, Council actions, same-parcel permits since filing |
| `GET /api/parcels/{gpin}/constraints` | Site constraints of one parcel: measured facts per layer, board history, layers checked, what is not covered |
| `GET /api/constraints/{layer}.geojson?bbox=` | One constraint layer inside a map view (display only; 413 above 2,000 features) |
| `GET /api/land-use/cases` | A pipeline city's land-use cases (Chesapeake); filters `stage`, `case_type`, `applied_from/to`, `include_legacy` |
| `GET /api/land-use/cases/{case_id}` | One case: city actions, parcels, flood, site constraints, development records on the same parcels |
| `GET /api/development-records` | Development plans and right-of-way permits (not building permits); filters `source`, `status` |
| `GET /api/land-use/council-actions` | Land-use items from the Clerk's Summary of Council Actions; filters `outcome`, `district`, `date_from/to`; archive coverage |
| `GET /api/land-use/council-actions/{key}` | One Council action: outcome, tally, addresses, the PC items it decides (or possibly decides), the Clerk's page |
| `GET /api/audit/{review_table}` | Change history of one review; query parameters are exactly the table's key (e.g. `decision_link_review?decision_key=…&permit_id=…`); 400 otherwise |

Filters: `locality`, `permit_type`, `status`, `status_bucket`, `flood_class` (comma list or repeated; each value validated), `q`, `issued_from`, `issued_to`, `sort`, `order`, `limit`, `offset`.

---

## Tests

```powershell
python -m pytest tests/ -v
```

**541 tests**, run by CI (`.github/workflows/ci.yml`) on every push and PR, together with a scan for `.env` files and keys. No live network calls — fixtures are sanitized captures of real responses. Sleeps and clocks are injected, so rate-limiter behaviour is verified deterministically without the suite ever waiting a real second (full run: ~20s).

Coverage includes date/schema normalization, ArcGIS epoch conversion, coordinate-precedence rules, both adapters, geocoding (bbox and locality validation, cache reuse, retry gating, `no_address` classification, batch result mapping, resumability), and the full API surface (SQL-injection safety, pagination stability, export caps, `raw_json` non-exposure).

---

## Coverage and honest limitations

| Locality | Status |
|---|---|
| **Norfolk** | Live — Socrata SODA API, updated daily |
| **Virginia Beach** | Live — ArcGIS REST feature layer |
| Chesapeake | Land-use cases, development plans, ROW permits, parcels, flood, constraints; building permits from Accela search results downloaded by hand (2024-10-29 → 2026-10-28) |
| Portsmouth | Source under review — see the North Carolina lookalike above |
| Hampton · Suffolk · Newport News | Unavailable — no confirmed open dataset located |

**Unsupported localities are shown in the UI, greyed out with an explanatory tooltip — not silently omitted.** A user who doesn't see Suffolk permits should learn that Suffolk isn't covered, not conclude Suffolk issues no permits.

Other limitations stated plainly:

- The original agency record is always authoritative. PermitIQ republishes; it doesn't adjudicate.
- 8.9% of permits have no map pin, each with a specific published reason (table above). **Missing data never implies no permit exists.**
- `valuation` is null for effectively all records — neither source exposes a genuine project-cost field. Norfolk's `total_balance` is a *fee* balance and is deliberately not mapped to it.
- Permit type and status vocabularies are per-city and are not harmonized beyond the coarse `status_bucket` enum.

---

## What I'd build next

- **Chesapeake building permits** (Accela export or FOIA) and a **Portsmouth adapter**: blocked on source access, not implementation. The adapter interface is a single `fetch(since, limit)` generator; adding a verified locality is a contained change.
- **Address normalization pass** for the 132 `not_found` records — several are formatting artifacts (`"1234 Gwaltney Way : -"`) that a cleanup step would likely resolve.
- **Split `skipped_count`** in `ingestion_runs` into `skipped_duplicate` vs `skipped_invalid` — currently one aggregate where the distinction is already known at runtime.
- **Scheduled ingestion** via Task Scheduler/cron, keeping the strict no-HTTP-triggered-writes rule intact.

---

## Verification note

Every phase of this project was verified against the **live** government APIs and the real accumulating database — not mocked and not assumed. The specific bugs described above (the North Carolina lookalike, the string-typed date fields, the `NULL`-address records stuck pending, the 1,030 mis-bucketed statuses) were all found that way, several of them only *after* the code appeared to be working correctly.
