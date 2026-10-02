BEGIN IMMEDIATE;
CREATE INDEX IF NOT EXISTS trace_scope_time ON traces(tenant,scope,created_at DESC);
CREATE INDEX IF NOT EXISTS evaluation_scope_plan_time ON evaluations(tenant,scope,plan_id,created_at DESC);
CREATE INDEX IF NOT EXISTS plan_scope_workflow_time ON plans(tenant,scope,workflow,created_at);
CREATE INDEX IF NOT EXISTS cache_expiry ON response_cache(expires_at);
INSERT OR IGNORE INTO schema_migrations(version) VALUES(3);
COMMIT;
