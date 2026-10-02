CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, scope TEXT NOT NULL,
    workflow TEXT NOT NULL, parent TEXT, config TEXT NOT NULL, config_hash TEXT NOT NULL,
    description TEXT NOT NULL, sources TEXT NOT NULL, expires_at TEXT,
    created_at REAL NOT NULL, baseline INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_baseline ON plans(tenant,scope,workflow) WHERE baseline=1;
CREATE TABLE IF NOT EXISTS activations (
    tenant TEXT NOT NULL, scope TEXT NOT NULL, workflow TEXT NOT NULL, plan_id TEXT NOT NULL,
    PRIMARY KEY(tenant,scope,workflow)
);
CREATE TABLE IF NOT EXISTS activation_events (
    id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, scope TEXT NOT NULL,
    workflow TEXT NOT NULL, previous TEXT, current TEXT NOT NULL, action TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, scope TEXT NOT NULL, plan_id TEXT NOT NULL,
    source_version TEXT NOT NULL, passed INTEGER NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS traces (
    request_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, scope TEXT NOT NULL,
    metadata TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS response_cache (
    key TEXT PRIMARY KEY, tenant TEXT NOT NULL, scope TEXT NOT NULL, plan_id TEXT NOT NULL,
    response TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_migrations(version) VALUES(1);
CREATE TRIGGER IF NOT EXISTS immutable_plans_update BEFORE UPDATE ON plans
BEGIN SELECT RAISE(ABORT, 'plan versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_plans_delete BEFORE DELETE ON plans
BEGIN SELECT RAISE(ABORT, 'plan versions are immutable'); END;
