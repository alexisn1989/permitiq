-- PermitIQ: Virginia Beach planning applications track (rezonings,
-- conditional use permits, variances) -- earlier project signals than
-- building permits. Same shape as the permits and violations tracks: raw
-- payloads + normalized table + one flattened view for Power BI.
-- Idempotent: safe to re-run.

-- ── planning_applications_raw ──────────────────────────────────────────────
-- One row per source record, exactly as ArcGIS returned its attributes, so
-- the normalized table can be re-derived without re-fetching.
CREATE TABLE IF NOT EXISTS planning_applications_raw (
    application_id  TEXT        PRIMARY KEY,
    payload         JSONB       NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── planning_applications ──────────────────────────────────────────────────
-- Normalized fields, loaded by ingest_planning.py. hearing_date is a plain
-- DATE converted from ESRI epoch milliseconds; status is stored verbatim.
CREATE TABLE IF NOT EXISTS planning_applications (
    application_id    TEXT        PRIMARY KEY,
    objectid          BIGINT,
    project_name      TEXT,
    address           TEXT,
    application_type  TEXT,
    proposed_use      TEXT,
    hearing_date      DATE,
    status            TEXT,
    geom              geometry(Point, 4326),
    geocode_source    TEXT        NOT NULL DEFAULT 'none'
                      CHECK (geocode_source IN ('arcgis', 'census', 'nominatim', 'none')),
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_planning_applications_geom          ON planning_applications USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_planning_applications_hearing_date  ON planning_applications (hearing_date);
CREATE INDEX IF NOT EXISTS idx_planning_applications_type          ON planning_applications (application_type);

-- ── vw_planning_leads ──────────────────────────────────────────────────────
-- One row per application, ALL applications, with the same flood-zone join
-- and precedence as vw_permit_leads / vw_violation_leads
-- (VE/V > AE > other A > shaded X > other X/D).
--   flood_match: 'matched' | 'no_polygon' | 'not_geocoded'
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
    a.ingested_at
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

-- Power BI read access. 003_roles.sql also grants this when the view exists,
-- so either file can be re-run on its own without dropping the grant.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powerbi_reader') THEN
        GRANT SELECT ON vw_planning_leads TO powerbi_reader;
    END IF;
END
$$;
