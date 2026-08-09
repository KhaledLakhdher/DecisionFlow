#!/bin/bash
# ---------------------------------------------------------------------------
# Runs once, on first boot of an empty Postgres data directory.
#
# Creates the extensions and the least-privileged application role.
#
# Why a separate app role: Postgres row-level security is bypassed by the table
# owner unless FORCE ROW LEVEL SECURITY is set. Migrations run as the owner (so
# they can create/alter tables), while the API connects as this non-owner role,
# which RLS policies always apply to. That gives us defense-in-depth tenant
# isolation at the database level, independent of whatever the application
# layer does or forgets to do.
#
# Two deliberate mechanics here:
#   * .sh rather than .sql, so the APP_DB_* environment variables are in scope
#     (plain .sql files are piped to psql without them).
#   * \gexec rather than a DO $$...$$ block, because psql does NOT interpolate
#     :'var' inside dollar-quoted strings — it silently leaves the literal text
#     in place, producing a syntax error at the colon.
# ---------------------------------------------------------------------------
set -euo pipefail

APP_DB_USER="${APP_DB_USER:-decisionflow_app}"
APP_DB_PASSWORD="${APP_DB_PASSWORD:-decisionflow_app}"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v app_user="$APP_DB_USER" \
     -v app_password="$APP_DB_PASSWORD" \
     -v db_name="$POSTGRES_DB" <<-'EOSQL'
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
    CREATE EXTENSION IF NOT EXISTS pg_trgm;

    -- CREATE ROLE has no IF NOT EXISTS; generate the statement only when the
    -- role is absent, then execute whatever rows came back.
    SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
    WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user');
    \gexec

    GRANT CONNECT ON DATABASE :"db_name" TO :"app_user";
    GRANT USAGE ON SCHEMA public TO :"app_user";

    -- Tables created later by Alembic (running as the owner) must be reachable
    -- by the app role without a manual grant after every migration.
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
EOSQL

echo "DecisionFlow: extensions installed and app role '${APP_DB_USER}' provisioned."
