# Permit data sources

Every source PermitIQ ingests or is evaluating, with how each fact was
established. **Nothing here is confirmed against the live API unless it says
`LIVE`** — the 2026-10-15 review ran in an environment that blocked
data.norfolk.gov and services2.arcgis.com, so newer facts come from the
adapters' own (earlier live-verified) code or from web-search snippets.

Run `python scripts/check_sources.py --json reports/sources_check.json` from
a machine with normal internet access to replace every `SEARCH` item below
with the live schema, update time and license.

Verification tags:

| Tag | Meaning |
|---|---|
| `CODE` | Stated in this repo's adapter, live-verified when the adapter was built (2026-07) |
| `SEARCH` | From a web-search snippet of the portal or a mirror — **not authoritative** |
| `OPEN` | Not determined yet |

---

## Summary

| Source | Locality | Access | Status | Name field | Cost field | Redistribution |
|---|---|---|---|---|---|---|
| Permits (`fahm-yuh4`) | Norfolk | Socrata SODA API | Ingested | none `CODE` | none (`total_balance` is a fee) `CODE` | Open-data policy favourable; per-dataset license `OPEN` |
| Permits and Inspections (`bnrb-u445`) | Norfolk | Socrata SODA API | **Candidate** | possibly `SEARCH` | `permit_project_cost` `SEARCH` | as above |
| Permits - Right of Way (`mi4h-8pn3`) | Norfolk | Socrata SODA API | **Candidate** | Applicant Name, Contractor `SEARCH` | none (Fee only) `SEARCH` | as above; ⚠ contact numbers |
| Building Permits Applications | Virginia Beach | ArcGIS REST FeatureServer | Ingested | none mapped (`CreatedBy` = portal user) `CODE` | none `CODE`/`SEARCH` | ⚠ Custom license, see below |

**No jurisdiction is scraped from HTML.** Both ingested sources are
documented open-data APIs, so there is no scraper to replace.

---

## Norfolk — Permits (ingested)

- **URL:** https://data.norfolk.gov/resource/fahm-yuh4.json (portal page https://data.norfolk.gov/d/fahm-yuh4) `CODE`
- **Access:** Socrata SODA API, unauthenticated, `$limit`/`$offset` paging of 1,000, `$where application_date >= …` `CODE`
- **Update frequency:** daily `CODE` (adapter docstring)
- **Fields (fixture capture):** permit_number, address, gpin, tax_account, type, status, application_date, issue_date, expiration_date, finaled_date, total_balance, square_footage, code_edition, use_class, work_type, use_type, structure `CODE`
- **Gaps for the digest:** no contractor/applicant name; no project cost (`total_balance` is a fee balance, deliberately unmapped); description is `work_type`/`use_type` (e.g. "New"), so it's thin. `use_class` gives a reliable Commercial/Residential split. `CODE`
- **Terms:** see [Norfolk terms](#norfolk-terms-all-datanorfolkgov-datasets).

## Norfolk — Permits and Inspections (candidate)

- **URL:** https://data.norfolk.gov/resource/bnrb-u445.json (portal page https://data.norfolk.gov/d/bnrb-u445) `SEARCH`
- **Access:** Socrata SODA API (same as above) `SEARCH`
- **Update frequency:** `OPEN`
- **Fields:** 38 columns, snake_case (e.g. `permit_address`), including **`permit_project_cost`** and `permit_total_fee`. The word "Contractor" appears in sample rows, but which column holds it is unconfirmed. `SEARCH`
- **Why it matters:** this is the only candidate project-cost field found for either city. If `permit_project_cost` is populated, the `valuation >= $50k` rule becomes usable for Norfolk.
- **Risk:** the dataset joins permits *and* inspections, so it is probably one row per inspection. It would need de-duplication by permit number before it can enrich `fahm-yuh4`. `OPEN`
- **Recommendation:** use it as an **enrichment join** onto existing Norfolk permits (valuation, contractor), not as a replacement source. Run the live check first.

## Norfolk — Permits - Right of Way (candidate, for dumpster haulers)

- **URL:** https://data.norfolk.gov/resource/mi4h-8pn3.json (portal page https://data.norfolk.gov/Permits/Permits-Right-of-Way/mi4h-8pn3) `SEARCH`
- **Access:** Socrata SODA API `SEARCH`
- **Update frequency:** "updated daily on weekdays" (dataset description) `SEARCH`
- **Contents (description):** permits from the Right of Way Management Division: ROW excavations/installations, residential and commercial driveway entrances, temporary parking/storage/**dumpsters**, **hauling oversize and cranes**, lane/street/sidewalk closures. `SEARCH`
- **Fields (CSV header snippet, 13 columns):** Permit Number, Type, Permit Kind, Street Number, Street, Civic League, **Applicant Name**, **Contractor**, Start Date, End Date, Issued Date, Fee, Coordinates. The description also mentions "work performed for", "inspector" and "contact number", which don't appear in the header snippet. Either they're mapped to the columns above or the snippet was truncated. `SEARCH`
- **Size:** ~27.6k rows per the data.virginia.gov mirror (may be stale) `SEARCH`
- **Why it matters:** it has **native coordinates** (no geocoding), **start/end dates** (a dumpster's on-street window), and the **only contractor/applicant names** found in any Hampton Roads source so far.
- **Caveats:**
  - ⚠ **Personal data.** Applicant Name will include homeowners (residential driveways, home dumpsters). The digest must apply the business-name filter (`classify_name` in `scripts/audit_permits.py`) and **never** publish a contact number.
  - Street address is split (Street Number + Street) and needs assembling.
  - Not every dumpster needs a ROW permit. Bulk containers on private property are permitted by the Norfolk Department of Public Health (Bulk Container Program), so this dataset covers **on-street** dumpsters only. `SEARCH`
  - Permit Kind values (to filter dumpster/crane rows) are `OPEN`. `check_sources.py` lists them.
- **Not ingested.** Per the Phase 2 brief, fields are reported first. Ingestion waits for approval and a live schema check.

## Virginia Beach — Building Permits Applications (ingested)

- **Endpoint type, verified rather than assumed:** **ArcGIS REST FeatureServer** on an ArcGIS Hub portal, **not Socrata**. `CODE` + `SEARCH`
  - The adapter queries `https://services2.arcgis.com/CyVvlIiUfRBmMQuu/arcgis/rest/services/Building_Permits_Applications_view/FeatureServer/0/query`. Its ArcGIS response shape was live-verified in 2026-07. `CODE`
  - The Hub item is https://gis.data.vbgov.com/datasets/15292e05ab3a42d5984e6033b2f6a6d4/about (also on data.virginiabeach.gov). It offers a GeoServices endpoint plus CSV/GeoJSON downloads. `SEARCH`
  - A third-party permit site claims Virginia Beach publishes on "Socrata". No evidence supports that. The older portal at data.vbgov.com is **CKAN** (weekly CSV from S3), not Socrata. `SEARCH`
  - `OPEN`: that the `services2…/Building_Permits_Applications_view` layer is the same item as Hub ID `15292e05…`. The names match but the link is unconfirmed. `check_sources.py` prints the layer name and edit date.
- **Access:** unauthenticated GET, `resultOffset` paging of 2,000 (`maxRecordCount`), `where ApplicationDate >= 'YYYY/MM/DD'` (string compare; all fields are strings) `CODE`
- **Update frequency:** `OPEN`. The Hub page showed a September 2026 update; legacy CKAN says weekly. Phase 1 audit section 4 measures actual lag. `SEARCH`
- **Fields:** PermitNumber, PermitType, ApplicationDate, IssueDate, FinalDate, Status, WorkDesc, StreetAddress, AddressUnit, City, State, Zip, GPIN, ConstructionType, WorkType, CreatedBy, OBJECTID `CODE`. The public preview shows the same set, with no contractor/cost field. `SEARCH`
- **Gaps for the digest:** no contractor name, no cost, no commercial/residential field (commercial detection relies on `WorkDesc`/`PermitType` keywords). `WorkDesc` is real free text, which helps.
- **Fragility:** low-medium. It's a documented API, but `*_view` hosted layers can be republished or renamed without notice.

### Virginia Beach terms

- Hub datasets carry a **"Custom License"**: data provided "as is", the city disclaims all warranties and liability, and maps are approximate. It may not be used to determine property lines, for engineering/construction site design, or for flood-zone determinations. (Seen on other VB Hub datasets; this dataset's own license text is `OPEN`.) `SEARCH`
- The legacy CKAN listing labels the permits CSV **Creative Commons Attribution** → attribution required. `SEARCH`
- The city's IT open-data page says GIS datasets may be available "for personal or commercial use": https://it.virginiabeach.gov/digital-resources/open-data `SEARCH`
- ⚠ **Flag:** no explicit redistribution clause found, and the two portals disagree (Custom vs CC-BY). **Before selling a digest built on VB data, read the dataset's license tab on the Hub page and keep a "Source: City of Virginia Beach Open Data" attribution line in every email.** Nothing found *prohibits* commercial redistribution.

### Norfolk terms (all data.norfolk.gov datasets)

- **Open Data Policy** (City Council, July 2017) defines open data as available "with no legal encumbrances on use or reuse": https://norfolk.gov/DocumentCenter/View/31912/Open-Data-Policy `SEARCH`
- City open-data page describes the data as usable and distributable "by anyone without restriction or cost": https://norfolk.gov/3885/Open-Data-Norfolk `SEARCH`
- The **Data & Records** page links to a disclaimer on what you can and can't do with City data; its text was not retrieved: https://www.norfolk.gov/5609/Data-Records `OPEN`
- The portal also hosts third-party datasets (state agencies, Census) with their own terms. All three permit datasets above are City-owned. `SEARCH`
- ⚠ **Flag:** the per-dataset Socrata `license` field is `OPEN` (`check_sources.py` prints it). Nothing found restricts commercial redistribution, but read the Data & Records disclaimer before launch. Questions: OpenData@norfolk.gov.

---

## Not covered

Chesapeake, Portsmouth, Hampton, Suffolk, Newport News: no verified
point-level permit dataset (see README). Portsmouth's similarly named
`Building_Permits_Pending` ArcGIS layer contains **North Carolina** data and
must not be used.
