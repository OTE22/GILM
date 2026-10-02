CREATE TABLE IF NOT EXISTS sales (
    sale_id INTEGER PRIMARY KEY,
    tenant TEXT NOT NULL, branch TEXT NOT NULL, date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents>=0), currency TEXT NOT NULL,
    product TEXT NOT NULL, units INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS scoped_sales ON sales(tenant,branch,date);
PRAGMA user_version=1;
