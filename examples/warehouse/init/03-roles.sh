#!/bin/sh
# Per-unit, read-only roles: the control that holds when every application guard fails
# (ADR 0006). Each role can read its own unit's schema and nothing else; a statement that
# names another unit's table fails with "permission denied" however it got past the guard.
#
# Passwords come from the environment (examples/warehouse/.env, never committed).
set -eu
: "${GM_UNIT_A_PASSWORD:?set GM_UNIT_A_PASSWORD in examples/warehouse/.env}"
: "${GM_UNIT_B_PASSWORD:?set GM_UNIT_B_PASSWORD in examples/warehouse/.env}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v unit_a_pw="$GM_UNIT_A_PASSWORD" -v unit_b_pw="$GM_UNIT_B_PASSWORD" <<'SQL'
SELECT current_database() AS db \gset

-- Nothing is readable because it exists: start from no access at all.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CONNECT ON DATABASE :"db" FROM PUBLIC;

CREATE ROLE gm_unit_a_ro LOGIN PASSWORD :'unit_a_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE gm_unit_b_ro LOGIN PASSWORD :'unit_b_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

ALTER ROLE gm_unit_a_ro SET default_transaction_read_only = on;
ALTER ROLE gm_unit_b_ro SET default_transaction_read_only = on;
ALTER ROLE gm_unit_a_ro SET statement_timeout = '30s';
ALTER ROLE gm_unit_b_ro SET statement_timeout = '30s';

GRANT CONNECT ON DATABASE :"db" TO gm_unit_a_ro, gm_unit_b_ro;

GRANT USAGE ON SCHEMA unit_a TO gm_unit_a_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA unit_a TO gm_unit_a_ro;

GRANT USAGE ON SCHEMA unit_b TO gm_unit_b_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA unit_b TO gm_unit_b_ro;
SQL
