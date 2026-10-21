# Hampton Roads source inventory (first pass, 2026-10-21)

This is roadmap step 5: where each of the region's 17 localities publishes the data PermitIQ needs. It is an inventory, not a plan to ingest all 17. The detail, with one row per source and its evidence, is in [`source_inventory.csv`](source_inventory.csv).

## How this was gathered, and how far to trust it
- **Search only, pages not opened.** In this session, web search worked but direct requests to municipal hosts were refused (DNS / network policy). Rows marked `search` come from search-result summaries. Their pages were not opened, their fields were not inspected, and their record counts and history depth were not measured.
- **`code` rows** are endpoints PermitIQ already calls: the Virginia Beach permits, parcels and Council export, Norfolk permits, and FEMA NFHL.
- **`user` rows** are sources named in the roadmap and not yet profiled.
- **`unknown` means not found.** It is never a guess. History depth, update frequency and access terms are mostly unknown until each source is profiled directly, the same way the Virginia Beach sources will be.
- **Third-party aggregators** (assessorsearch.com, permitsaige.com, PermitMint, Regrid, Construction Monitor) are only used to point to an official URL, and are flagged as such in `gaps`. They are not sources.

## What's available, by locality
- **bulk:** an API or download of records.
- **portal:** a per-record lookup or application site, with no bulk export found.
- **docs:** agendas and minutes as HTML or PDF.
- **regional:** covered by the HRGEO regional parcel layer.
- **–:** nothing found.

| Locality | Permits | Inspections | Council / Board decisions | Planning | Parcels |
|---|---|---|---|---|---|
| Virginia Beach | **bulk** (ArcGIS, in use) | – | **structured votes** (VoteIQ export) + Clerk action summaries | **bulk** (PC & Council Actions layer, to profile) | **bulk** (in use) |
| Norfolk | **bulk** (Socrata, daily; adapter exists) | **bulk** (Socrata, weekdays) | docs (Agenda Center) | docs (PC public hearings) | regional |
| Chesapeake | portal (eBUILD) | portal | docs (Granicus + PDFs) | **bulk** (Level of Service, quarterly) + docs | **bulk** (city hub) |
| Newport News | portal | – | docs (CivicWeb) | docs | **bulk** (GeoHub; data Dec 2023) |
| Hampton | portal | portal | docs (Agenda Center; minutes to 1952) | docs | regional |
| Portsmouth | portal (POPS lookup) | portal | docs | docs | regional |
| Suffolk | portal (SOAP) | portal | docs (+ Granicus video) | – | regional |
| Poquoson | portal (iWorQ) | portal | docs | docs | viewer (CivQuest) + regional |
| Williamsburg | in person / paper | phone line | docs (CivicWeb, Feb 2019+) | docs (2011+) | regional |
| Franklin | email + PDF form | – | docs | – | regional |
| James City County | portal (PermitLink) | portal | docs (CivicClerk; older On Demand) | docs | regional |
| York County | portal (new; account required) | portal | docs (+ Swagit video) | **monthly PDF** Development Activity Report with GPINs | regional |
| Gloucester County | in office / email | – | docs (eScribe) | docs | viewer + regional |
| Isle of Wight County | not confirmed online | – | docs (CivicClerk) | docs | regional |
| Southampton County | phone / in person | – | docs (+ Swagit video) | docs | regional |
| Surry County | not confirmed online | – | docs | docs | regional |
| Smithfield (town) | zoning permits from the town; some building permits from Isle of Wight County | – | docs | docs | regional |

"Regional" assumes the locality is among the HRGEO layer's sources. The layer is built from 15 map services plus 1 shapefile, so 16 inputs for 17 localities. Which locality is missing, and which fields each one shares, is not yet known.

## What this means for the product
1. **Bulk permit records are rare.** Only Virginia Beach and Norfolk were found publishing permit records in bulk. Everywhere else permits sit behind application portals, or are handled on paper or by email. A portal is not a data feed: using one would mean records requests or a data agreement with the city, not an adapter.
2. **Council decisions are documents almost everywhere.** Outside Virginia Beach (structured votes via VoteIQ), decisions exist only as agendas and minutes (CivicEngage, CivicWeb, CivicClerk, Granicus, eScribe). The common schema (step 4) therefore has to support decisions read out of documents, with the same human outcome review Virginia Beach uses. Case IDs are a usable join key where printed: Chesapeake `PLN-REZ` / `PLN-USE`, Newport News `CU-2025-0001`, York GPINs.
3. **Parcels have a regional path.** HRGEO's regional layer (16 inputs) and VGIN's statewide layer (quarterly) cover most localities. Neither carries parcel history, though, so splits and merges (step 7) still need the localities' own records.
4. **Inspections** were found in bulk only for Norfolk.

## Recommendation for the second locality (step 6): Norfolk
- **The only other bulk permit source.** Norfolk has daily permits and inspections on Socrata, under a public-domain license per the state portal listing. PermitIQ already has a Norfolk permits adapter (`ingest/norfolk.py`).
- **A real test of the common model.** It differs from Virginia Beach where it counts: Socrata instead of ArcGIS, decisions as documents instead of structured votes, and parcels from the regional layer instead of a city service.
- **Unknowns to settle first:**
  - the depth of Norfolk's permit history;
  - whether the old iQM2 reference for Planning Commission records is still live;
  - which parcel fields Norfolk shares in HRGEO.

Chesapeake is the runner-up. It has a quarterly approvals dataset and its own parcel hub, but no bulk permits. It would need a data request.

Demand from customers still decides, per the roadmap. This recommendation covers data readiness only.

## Next verification, once hosts are reachable
For each row marked `search`, open the source and record:
- the record count;
- the earliest and latest dates;
- the fields;
- the update cadence;
- the license or terms;
- the URL as it actually resolves.

Profile the same way as Virginia Beach. Start with Norfolk:
- the Socrata metadata for `fahm-yuh4`, `bnrb-u445` and `ihzr-5x5n`;
- the HRGEO layer's list of sources and its fields.
