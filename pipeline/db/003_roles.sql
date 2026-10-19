-- powerbi_reader: read-only login for Power BI.
-- It can CONNECT, and SELECT from vw_permit_leads, vw_violation_leads,
-- vw_planning_leads and planning_status_events -- nothing else of ours.
-- (The view runs with its owner's privileges, so no grants on the
-- underlying tables are needed. PostGIS itself grants SELECT on the
-- spatial_ref_sys reference table to PUBLIC; that is left as is.)
--
-- Password: taken from POWERBI_READER_PASSWORD in the db container's
-- environment (set in .env). If it is unset, the role exists but has no
-- password, so it cannot log in until you run:
--   ALTER ROLE powerbi_reader PASSWORD '...';
--
-- Must run through psql (uses \getenv / \if). Idempotent. Re-run it if
-- vw_permit_leads is ever DROPped and recreated (CREATE OR REPLACE keeps grants):
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < db/003_roles.sql

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powerbi_reader') THEN
        CREATE ROLE powerbi_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

-- Belt and braces: even a mistaken future GRANT can't let this role write.
ALTER ROLE powerbi_reader SET default_transaction_read_only = on;
ALTER ROLE powerbi_reader SET statement_timeout = '120s';

\getenv powerbi_pw POWERBI_READER_PASSWORD
SELECT :{?powerbi_pw} AS has_powerbi_pw \gset
\if :has_powerbi_pw
    ALTER ROLE powerbi_reader PASSWORD :'powerbi_pw';
\else
    \echo 'POWERBI_READER_PASSWORD not set: powerbi_reader password left unchanged (a role with no password cannot log in)'
\endif

DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM powerbi_reader', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO powerbi_reader', current_database());
END
$$;

REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM powerbi_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM powerbi_reader;
REVOKE CREATE ON SCHEMA public FROM powerbi_reader;
GRANT USAGE ON SCHEMA public TO powerbi_reader;
GRANT SELECT ON vw_permit_leads TO powerbi_reader;

-- The REVOKE above also strips views added by later migrations; re-grant
-- them here when they exist (004/005 grant their own view on first create).
DO $$
BEGIN
    IF to_regclass('public.vw_violation_leads') IS NOT NULL THEN
        GRANT SELECT ON vw_violation_leads TO powerbi_reader;
    END IF;
    IF to_regclass('public.vw_planning_leads') IS NOT NULL THEN
        GRANT SELECT ON vw_planning_leads TO powerbi_reader;
    END IF;
    IF to_regclass('public.planning_status_events') IS NOT NULL THEN
        GRANT SELECT ON planning_status_events TO powerbi_reader;
    END IF;
END
$$;
