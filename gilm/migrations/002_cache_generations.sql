CREATE TABLE IF NOT EXISTS cache_generations (
    tenant TEXT NOT NULL, scope TEXT NOT NULL, generation INTEGER NOT NULL,
    PRIMARY KEY (tenant,scope)
);
INSERT OR IGNORE INTO schema_migrations(version) VALUES(2);
