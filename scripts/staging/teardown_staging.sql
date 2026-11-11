-- Agent-X staging teardown. RUN BY A POSTGRESQL ADMINISTRATOR after the staging evidence is saved:
--   psql -h 127.0.0.1 -U postgres -d postgres -f scripts/staging/teardown_staging.sql
-- Drops only the staging database and its role. agentx and agentx_test are never touched.
\set ON_ERROR_STOP on
DROP DATABASE IF EXISTS agentx_staging WITH (FORCE);
DROP ROLE IF EXISTS agentx_staging_app;
