-- vw_permit_leads: the flattened view Power BI reads.
-- One row per permit, ALL permits (no flood filtering here -- Power BI slices
-- on fld_zone / sfha_tf).
--
-- flood_match explains a NULL fld_zone, so "no data" is never mistaken for
-- "not in a flood zone":
--   'matched'       permit point intersects at least one flood polygon
--   'no_polygon'    permit has coordinates but intersects no polygon
--   'not_geocoded'  permit has no coordinates
--
-- If a point falls in several overlapping polygons (or exactly on a shared
-- edge) one zone is chosen deterministically, most hazardous first:
--   VE/V  >  AE  >  other A zones (AH, AO, A99, ...)  >  shaded X  >  other X/D
CREATE OR REPLACE VIEW vw_permit_leads AS
SELECT
    p.permit_number,
    p.permit_type,
    p.application_date,
    p.issue_date,
    p.final_date,
    p.work_desc,
    p.address,
    ST_Y(p.geom)       AS latitude,
    ST_X(p.geom)       AS longitude,
    p.geocode_source,
    z.fld_zone,
    z.zone_subty,
    z.sfha_tf,
    CASE
        WHEN p.geom IS NULL     THEN 'not_geocoded'
        WHEN z.fld_zone IS NULL THEN 'no_polygon'
        ELSE 'matched'
    END                AS flood_match,
    p.ingested_at
FROM permits p
LEFT JOIN LATERAL (
    SELECT fz.fld_zone, fz.zone_subty, fz.sfha_tf
    FROM fema_flood_zones fz
    WHERE ST_Intersects(fz.geom, p.geom)
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
