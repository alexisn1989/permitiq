-- PermitIQ: Virginia Beach Code Enforcement violations track.
-- Same shape as the permits track: raw payloads + normalized table + one
-- flattened view for Power BI. Idempotent: safe to re-run.

-- ── code_violations_raw ────────────────────────────────────────────────────
-- One row per source record, exactly as ArcGIS returned its attributes, so
-- the normalized table can be re-derived without re-fetching.
CREATE TABLE IF NOT EXISTS code_violations_raw (
    violation_id  TEXT        PRIMARY KEY,
    payload       JSONB       NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── code_violations ────────────────────────────────────────────────────────
-- Normalized fields, loaded by ingest_violations.py. Dates are plain DATEs
-- converted from ESRI epoch milliseconds.
-- compliance_date is stored as the source gives it. Whether it means "date
-- compliance was achieved" or "deadline to comply" is not known yet -- check
-- the layer before treating NULL as "still open".
CREATE TABLE IF NOT EXISTS code_violations (
    violation_id      TEXT        PRIMARY KEY,
    objectid          BIGINT,
    violation_type    TEXT,
    description       TEXT,
    complaint_date    DATE,
    compliance_date   DATE,
    address           TEXT,
    geom              geometry(Point, 4326),
    geocode_source    TEXT        NOT NULL DEFAULT 'none'
                      CHECK (geocode_source IN ('arcgis', 'census', 'nominatim', 'none')),
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_code_violations_geom            ON code_violations USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_code_violations_complaint_date  ON code_violations (complaint_date);
CREATE INDEX IF NOT EXISTS idx_code_violations_type            ON code_violations (violation_type);

-- ── vw_violation_leads ─────────────────────────────────────────────────────
-- One row per violation, ALL violations, with the same flood-zone join and
-- precedence as vw_permit_leads (VE/V > AE > other A > shaded X > other X/D),
-- so Power BI can slice both tracks the same way.
--   flood_match: 'matched' | 'no_polygon' | 'not_geocoded'
CREATE OR REPLACE VIEW vw_violation_leads AS
SELECT
    v.violation_id,
    v.violation_type,
    v.description,
    v.complaint_date,
    v.compliance_date,
    v.address,
    ST_Y(v.geom)       AS latitude,
    ST_X(v.geom)       AS longitude,
    v.geocode_source,
    z.fld_zone,
    z.zone_subty,
    z.sfha_tf,
    CASE
        WHEN v.geom IS NULL     THEN 'not_geocoded'
        WHEN z.fld_zone IS NULL THEN 'no_polygon'
        ELSE 'matched'
    END                AS flood_match,
    v.ingested_at
FROM code_violations v
LEFT JOIN LATERAL (
    SELECT fz.fld_zone, fz.zone_subty, fz.sfha_tf
    FROM fema_flood_zones fz
    WHERE ST_Intersects(fz.geom, v.geom)
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
        GRANT SELECT ON vw_violation_leads TO powerbi_reader;
    END IF;
END
$$;
