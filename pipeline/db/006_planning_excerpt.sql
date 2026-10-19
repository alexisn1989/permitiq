-- PermitIQ: verbatim source excerpt for planning leads.
-- scan_planning_gemini.py copies each agenda entry's text verbatim from the
-- PDF (verified word-for-word before it is kept). Storing it lets Power BI
-- show the raw agenda text beside each lead, so anyone who doubts a lead
-- can read exactly what the agenda printed. Idempotent: safe to re-run.

ALTER TABLE planning_applications ADD COLUMN IF NOT EXISTS source_excerpt TEXT;

-- Same view as 005_planning.sql with source_excerpt appended. It must be the
-- LAST column: CREATE OR REPLACE VIEW can only add columns at the end. Grants
-- (powerbi_reader) survive CREATE OR REPLACE.
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
    a.source_excerpt
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
) z ON TRUE;
