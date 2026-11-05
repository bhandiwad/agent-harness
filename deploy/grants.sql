-- Administrator provisions agent_runtime LOGIN NOSUPERUSER NOBYPASSRLS first.
-- Set its secret separately using the deployment secret manager.
-- Run migrations using a DIFFERENT owner role before applying these grants.
GRANT USAGE ON SCHEMA agent TO agent_runtime;
GRANT SELECT ON agent.schema_version TO agent_runtime;
GRANT SELECT, INSERT, UPDATE ON agent.sessions TO agent_runtime;
GRANT SELECT, INSERT ON agent.events, agent.commands TO agent_runtime;
