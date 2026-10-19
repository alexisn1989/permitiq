-- PermitIQ schema: permits, raw payloads, FEMA flood zones.
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS postgis;

-- ── permits_raw ────────────────────────────────────────────────────────────
-- One row per source record, exactly as ArcGIS returned its attributes, so
-- the normalized table can be re-derived without re-fetching.
CREATE TABLE IF NOT EXISTS permits_raw (
    permit_number  TEXT        PRIMARY KEY,
    payload        JSONB       NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── permits ────────────────────────────────────────────────────────────────
-- Normalized permit fields. Dates are plain DATEs (ESRI epoch-millisecond
-- values are converted by ingest_permits.py; see esri_to_date()).
-- geom is NULL when the record has no usable location; geocode_source says
-- where a non-NULL geom came from ('none' = no geom).
CREATE TABLE IF NOT EXISTS permits (
    permit_number     TEXT        PRIMARY KEY,
    objectid          BIGINT,
    permit_type       TEXT,
    application_date  DATE,
    issue_date        DATE,
    final_date        DATE,
    work_desc         TEXT,
    address           TEXT,
    geom              geometry(Point, 4326),
    geocode_source    TEXT        NOT NULL DEFAULT 'none'
                      CHECK (geocode_source IN ('arcgis', 'census', 'nominatim', 'none')),
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_permits_geom        ON permits USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_permits_issue_date  ON permits (issue_date);
CREATE INDEX IF NOT EXISTS idx_permits_app_date    ON permits (application_date);

-- ── fema_flood_zones ───────────────────────────────────────────────────────
-- FEMA NFHL flood hazard polygons, loaded by scripts/load_fema_nfhl.py from
-- the NFHL S_FLD_HAZ_AR layer (the loader checks the live layer's fields).
--   dfirm_id   FIRM panel set, e.g. 51810C (Virginia Beach)
--   fld_zone   e.g. AE, VE, X, AO, AH
--   zone_subty e.g. '0.2 PCT ANNUAL CHANCE FLOOD HAZARD' (shaded X)
--   sfha_tf    Special Flood Hazard Area flag (T/F)
CREATE TABLE IF NOT EXISTS fema_flood_zones (
    id              BIGSERIAL PRIMARY KEY,
    dfirm_id        TEXT,
    fld_zone        TEXT,
    zone_subty      TEXT,
    sfha_tf         TEXT,
    source_vintage  TEXT,
    geom            geometry(MultiPolygon, 4326) NOT NULL
);

-- For volumes created before dfirm_id existed.
ALTER TABLE fema_flood_zones ADD COLUMN IF NOT EXISTS dfirm_id TEXT;

CREATE INDEX IF NOT EXISTS idx_fema_flood_zones_geom ON fema_flood_zones USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_fema_flood_zones_dfirm ON fema_flood_zones (dfirm_id);
CREATE INDEX IF NOT EXISTS idx_fema_flood_zones_zone ON fema_flood_zones (fld_zone);
