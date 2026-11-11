-- Agent-X staging provisioning (B9). RUN BY A POSTGRESQL ADMINISTRATOR, e.g.:
--   psql -h 127.0.0.1 -U postgres -d postgres -f scripts/staging/provision_staging.sql
-- Creates a least-privilege role and a dedicated database it owns. Idempotent: existing objects are left alone.
-- The password is prompted for and never written to a file. agentx_app is NOT given CREATEDB.
\set ON_ERROR_STOP on

SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentx_staging_app') AS need_role \gset
\if :need_role
  \prompt 'Password for the new role agentx_staging_app: ' staging_password
  CREATE ROLE agentx_staging_app LOGIN PASSWORD :'staging_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\else
  \echo 'role agentx_staging_app already exists (left unchanged)'
\endif

SELECT NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'agentx_staging') AS need_db \gset
\if :need_db
  CREATE DATABASE agentx_staging OWNER agentx_staging_app;
\else
  \echo 'database agentx_staging already exists (left unchanged)'
\endif

REVOKE CONNECT, TEMPORARY ON DATABASE agentx_staging FROM PUBLIC;
GRANT CONNECT ON DATABASE agentx_staging TO agentx_staging_app;

-- Verification: the role must have no elevated attributes and must own the staging database.
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin
  FROM pg_roles WHERE rolname = 'agentx_staging_app';
SELECT datname, pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname = 'agentx_staging';
