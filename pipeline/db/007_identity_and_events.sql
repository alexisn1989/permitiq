-- PermitIQ 007: planning application identity + sourced status ledger.
--
-- Separates an APPLICATION (a land-use case, possibly heard at several
-- meetings) from an OCCURRENCE (one appearance on one meeting's agenda), and
-- records each sourced status observation as an EVENT. Identity rules live in
-- permitiq/planning_identity.py; resolution runs in ingest_planning.py.
--
-- Requires 005 and 006. Does not modify them: everything here is additive,
-- and the view keeps 006's columns in 006's order with gpin appended.
-- Idempotent: safe to re-run.

-- ── planning_applications / _raw: new columns ─────────────────────────────
-- gpin           parcel identifier exactly as printed (text: leading zeros
--                matter). Identifies a parcel, NOT an application.
-- case_number    official case number, only when printed on the source.
-- identity_basis how application_id was assigned: official_case_id |
--                occurrence_unresolved | confirmed_link | legacy_id |
--                legacy_split | input_id (see planning_identity.py).
-- superseded_by  set when a reviewer confirms this unresolved record is the
--                same application as another; its occurrences and events
--                move there and the record drops out of vw_planning_leads.
--                The row itself is kept (no deletes).
ALTER TABLE planning_applications     ADD COLUMN IF NOT EXISTS gpin VARCHAR;
ALTER TABLE planning_applications_raw ADD COLUMN IF NOT EXISTS gpin VARCHAR;
ALTER TABLE planning_applications     ADD COLUMN IF NOT EXISTS case_number TEXT;
ALTER TABLE planning_applications     ADD COLUMN IF NOT EXISTS identity_basis TEXT;
ALTER TABLE planning_applications     ADD COLUMN IF NOT EXISTS superseded_by TEXT
    REFERENCES planning_applications (application_id);

-- Rows that predate 007 keep their IDs; record how they were assigned.
UPDATE planning_applications
SET identity_basis = CASE WHEN application_id ~ '^VBPC-[0-9a-f]{12}$' THEN 'legacy_id' ELSE 'input_id' END
WHERE identity_basis IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'planning_applications_identity_basis_check') THEN
        ALTER TABLE planning_applications ADD CONSTRAINT planning_applications_identity_basis_check
            CHECK (identity_basis IN ('official_case_id', 'occurrence_unresolved', 'confirmed_link',
                                      'legacy_id', 'legacy_split', 'input_id'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_planning_applications_gpin ON planning_applications (gpin) WHERE gpin IS NOT NULL;

-- Same normalization as permitiq.planning_identity.norm_text(), for
-- review-candidate matching in SQL.
CREATE OR REPLACE FUNCTION planning_norm_text(t TEXT) RETURNS TEXT
    LANGUAGE sql IMMUTABLE AS
$$ SELECT btrim(regexp_replace(lower(t), '[^a-z0-9]+', ' ', 'g')) $$;

-- ── planning_occurrences ───────────────────────────────────────────────────
-- One row per agenda appearance, keyed by a deterministic occurrence_key.
-- Re-ingesting the same occurrence finds this row and resolves to the same
-- application_id, whatever the input file's own ID says.
CREATE TABLE IF NOT EXISTS planning_occurrences (
    occurrence_key     TEXT        PRIMARY KEY,
    application_id     TEXT        NOT NULL REFERENCES planning_applications (application_id),
    resolution_method  TEXT        NOT NULL
                       CHECK (resolution_method IN ('official_case_id', 'occurrence_unresolved', 'confirmed_link',
                                                    'legacy_id', 'legacy_split', 'input_id')),
    jurisdiction       TEXT,
    meeting_date       DATE,
    case_number        TEXT,
    agenda_item        TEXT,
    source_document    TEXT,
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_planning_occurrences_application ON planning_occurrences (application_id);

-- ── planning_identity_links ────────────────────────────────────────────────
-- Human-confirmed "this occurrence belongs to that application". The only
-- way two occurrences without a shared case number become one application.
-- No FK to planning_occurrences: a link may be confirmed before the
-- occurrence is ingested.
CREATE TABLE IF NOT EXISTS planning_identity_links (
    occurrence_key  TEXT        PRIMARY KEY,
    application_id  TEXT        NOT NULL REFERENCES planning_applications (application_id),
    confirmed_by    TEXT        NOT NULL,
    confirmed_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    note            TEXT
);

-- ── planning_identity_review ───────────────────────────────────────────────
-- Possible matches flagged for a human: same GPIN, same applicant+address,
-- or the pre-007 hash would have merged them. Flagged, never merged.
-- Pairs are stored once, smaller ID first.
CREATE TABLE IF NOT EXISTS planning_identity_review (
    application_id            TEXT        NOT NULL REFERENCES planning_applications (application_id),
    candidate_application_id  TEXT        NOT NULL REFERENCES planning_applications (application_id),
    reason                    TEXT        NOT NULL
                              CHECK (reason IN ('same_gpin', 'same_applicant_address', 'legacy_hash_match')),
    detected_at               TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolution                TEXT        CHECK (resolution IN ('confirmed_same', 'confirmed_distinct')),
    PRIMARY KEY (application_id, candidate_application_id, reason),
    CHECK (application_id < candidate_application_id)
);

-- ── planning_status_events ─────────────────────────────────────────────────
-- Ledger of sourced status observations. An event says "this source showed
-- this status for this occurrence"; consecutive events need not differ, and
-- an event is not proof of a status transition.
--   status_raw         wording as printed (e.g. the agenda section)
--   status_normalized  conservative code mapping; NULL when unknown
--   effective_date     only when the source explicitly establishes it
--   meeting_date       the meeting whose agenda was observed (not effective)
--   event_key          deterministic: occurrence + source document + status;
--                      UNIQUE so retries / concurrent runs cannot duplicate
CREATE TABLE IF NOT EXISTS planning_status_events (
    event_id           BIGSERIAL   PRIMARY KEY,
    application_id     TEXT        NOT NULL REFERENCES planning_applications (application_id),
    occurrence_key     TEXT        NOT NULL REFERENCES planning_occurrences (occurrence_key),
    status_raw         TEXT,
    status_normalized  TEXT        CHECK (status_normalized IN ('scheduled', 'deferred', 'withdrawn', 'approved',
                                                                'denied', 'recommended_approval', 'recommended_denial')),
    effective_date     DATE,
    observed_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_excerpt     TEXT,
    source_document    TEXT,
    meeting_date       DATE,
    agenda_item        TEXT,
    event_key          TEXT        NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_planning_status_events_history
    ON planning_status_events (application_id, meeting_date DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_planning_status_events_occurrence
    ON planning_status_events (occurrence_key);

-- ── vw_planning_leads ──────────────────────────────────────────────────────
-- 006's columns, in 006's order, with gpin appended (CREATE OR REPLACE VIEW
-- may only add columns at the end; grants survive it). Superseded records
-- are hidden; before any link is confirmed none exist, so existing rows are
-- returned exactly as before.
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
    a.gpin
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
WHERE a.superseded_by IS NULL;

-- ── Power BI ───────────────────────────────────────────────────────────────
-- Read access to the status ledger. 003_roles.sql re-grants it when re-run.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powerbi_reader') THEN
        GRANT SELECT ON planning_status_events TO powerbi_reader;
    END IF;
END
$$;
