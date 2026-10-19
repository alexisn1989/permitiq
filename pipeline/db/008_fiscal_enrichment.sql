-- PermitIQ 008: parcel + assessment reference data synced nightly from the
-- FiscalMapIQ database (Neon) by sync_fiscal_data.py, and joined onto
-- vw_planning_leads. Requires 001-007; does not modify them. Idempotent.
--
-- Source (FiscalMapIQ alembic revision dbf3adb70c64):
--   parcels(parcel_id, city_code, gpin, geom MultiPolygon/2284, acreage_stated,
--           acreage_computed, zoning, land_use_code, property_class, data_year, ...)
--   assessments(parcel_id -> parcels, city_code, tax_year, land_value,
--               improvement_value, total_value, ...)
-- The sync keeps one row per GPIN: the latest parcel data_year and the latest
-- assessment tax_year.

-- ── reference_parcels ──────────────────────────────────────────────────────
-- gpin        FiscalMapIQ parcels.gpin as stored (the city's PAR_GPIN text)
-- land_use    parcels.land_use_code
-- acreage     acreage_stated, falling back to acreage_computed
-- gpin_digits digits only, for matching GPINs printed with dashes/spaces
-- city_code / data_year / source_parcel_id / synced_at   provenance
CREATE TABLE IF NOT EXISTS reference_parcels (
    gpin              VARCHAR PRIMARY KEY,
    geom              geometry(MultiPolygon, 2284),
    zoning            VARCHAR,
    land_use          VARCHAR,
    property_class    VARCHAR,
    acreage           NUMERIC,
    gpin_digits       VARCHAR GENERATED ALWAYS AS (regexp_replace(gpin, '\D', '', 'g')) STORED,
    city_code         TEXT,
    data_year         INTEGER,
    source_parcel_id  BIGINT,
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_reference_parcels_digits ON reference_parcels (gpin_digits);
CREATE INDEX IF NOT EXISTS idx_reference_parcels_base10 ON reference_parcels (left(gpin_digits, 10));
CREATE INDEX IF NOT EXISTS idx_reference_parcels_geom ON reference_parcels USING GIST (geom);

-- ── reference_assessments ──────────────────────────────────────────────────
-- market_value       FiscalMapIQ assessments.total_value: the assessor's total
--                    assessed value. FiscalMapIQ has no separate market-value
--                    column; this is not an independent market estimate.
-- improvement_value  assessments.improvement_value (already one combined
--                    figure in FiscalMapIQ; no dwelling/other split exists)
-- tax_year / synced_at   provenance
CREATE TABLE IF NOT EXISTS reference_assessments (
    gpin               VARCHAR PRIMARY KEY,
    market_value       NUMERIC,
    land_value         NUMERIC,
    improvement_value  NUMERIC,
    tax_year           INTEGER,
    synced_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── vw_planning_leads ──────────────────────────────────────────────────────
-- 007's columns in 007's order, then: zoning, land_use, market_value,
-- land_value, reference_parcel_gpin (which parcel the enrichment came from).
--
-- Matching is NOT a plain gpin = gpin join: planning GPINs are stored as
-- printed ('1494-34-4919', or several in one string), while FiscalMapIQ holds
-- 14-character PAR_GPIN text. A lead is enriched only when its GPIN, reduced to
-- digits, is a single 14-digit GPIN equal to a parcel's, or a single 10-digit
-- GPIN equal to the first 10 digits of EXACTLY ONE parcel. Ambiguous matches
-- (e.g. several condo units sharing a base GPIN) and multi-GPIN strings get no
-- enrichment rather than a guessed parcel. The aggregate subquery always
-- returns one row, so the view never gains duplicate rows.
CREATE OR REPLACE VIEW vw_planning_leads AS
SELECT
    a.application_id,
    a.project_name,
    a.address,
    a.application_type,
    a.proposed_use,
    a.hearing_date,
    a.status,
    ST_Y(a.geom)       AS latitude,
    ST_X(a.geom)       AS longitude,
    a.geocode_source,
    z.fld_zone,
    z.zone_subty,
    z.sfha_tf,
    CASE
        WHEN a.geom IS NULL     THEN 'not_geocoded'
        WHEN z.fld_zone IS NULL THEN 'no_polygon'
        ELSE 'matched'
    END                AS flood_match,
    a.ingested_at,
    a.source_excerpt,
    a.gpin,
    rp.zoning,
    rp.land_use,
    ra.market_value,
    ra.land_value,
    rp.gpin            AS reference_parcel_gpin
FROM planning_applications a
LEFT JOIN LATERAL (
    SELECT fz.fld_zone, fz.zone_subty, fz.sfha_tf
    FROM fema_flood_zones fz
    WHERE ST_Intersects(fz.geom, a.geom)
    ORDER BY
        CASE
            WHEN fz.fld_zone IN ('VE', 'V')                     THEN 1
            WHEN fz.fld_zone = 'AE'                             THEN 2
            WHEN fz.fld_zone LIKE 'A%'                          THEN 3
            WHEN fz.zone_subty ILIKE '%0.2 PCT%'                THEN 4
            ELSE 5
        END,
        fz.fld_zone,
        fz.id
    LIMIT 1
) z ON TRUE
CROSS JOIN LATERAL (SELECT regexp_replace(a.gpin, '\D', '', 'g') AS digits) g
LEFT JOIN LATERAL (
    SELECT CASE WHEN count(*) = 1 THEN max(p.gpin)     END AS gpin,
           CASE WHEN count(*) = 1 THEN max(p.zoning)   END AS zoning,
           CASE WHEN count(*) = 1 THEN max(p.land_use) END AS land_use
    FROM reference_parcels p
    WHERE (length(g.digits) = 14 AND p.gpin_digits = g.digits)
       OR (length(g.digits) = 10 AND left(p.gpin_digits, 10) = g.digits)
) rp ON TRUE
LEFT JOIN reference_assessments ra ON ra.gpin = rp.gpin
WHERE a.superseded_by IS NULL;

-- ── Power BI ───────────────────────────────────────────────────────────────
-- Re-running 003_roles.sql revokes these (it predates them); re-run this file
-- afterwards to restore them.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powerbi_reader') THEN
        GRANT SELECT ON reference_parcels, reference_assessments TO powerbi_reader;
    END IF;
END
$$;
