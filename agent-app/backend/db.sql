-- Read-only role for the agent's SQL tool (guide section 4.1, adapted for the existing Neon database).
-- Run ONCE as the database owner (e.g. in the Neon SQL editor). Choose your own strong password; never commit it.
-- Then put the connection string in agent-app/backend/.env as READONLY_DATABASE_URL (same host and query string as DATABASE_URL,
-- user agent_ro, this password). Until that is set, the agent's SQL tools stay disabled (they never use the read/write role).

CREATE ROLE agent_ro LOGIN PASSWORD 'CHANGE_ME_STRONG_PASSWORD';
GRANT CONNECT ON DATABASE neondb TO agent_ro;
GRANT USAGE ON SCHEMA public TO agent_ro;

-- SELECT only on the business data the agent may see (the 8 source tables). Deliberately NOT granted:
--   conversations, messages, schedules, events (app internals)  and  ALTER DEFAULT PRIVILEGES (future tables).
GRANT SELECT ON
  src_dim_machine, src_dim_product_routing, src_dim_shift_calendar, src_fact_bom_component,
  src_fact_customer_order, src_fact_production_order, src_fact_hourly_production, src_fact_inventory_daily_snapshot
TO agent_ro;

-- Quick check, connected as agent_ro:
--   SELECT count(*) FROM src_dim_machine;          -- works (15)
--   SELECT count(*) FROM schedules;                -- permission denied
--   INSERT INTO src_dim_machine(machine_id) VALUES ('x');   -- permission denied
