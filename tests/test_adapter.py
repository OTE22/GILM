import sqlite3

import pytest

from gilm.adapter import AdapterError, Forbidden, ReportingAdapter
from gilm.config import Principal
from gilm.models import ReportRequest


@pytest.fixture
def adapter(tmp_path):
    return ReportingAdapter(tmp_path / "reporting.sqlite")


def test_known_results_both_paths_and_projection(adapter):
    principal = Principal("demo", ("North", "South"))
    request = ReportRequest(start="2026-09-01", end="2026-10-01")
    aggregate = adapter.fetch(principal, request, "sales.totals.v1")
    detail = adapter.fetch(principal, request, "sales.detail.v1", ["branch", "date", "amount_cents", "currency"])
    assert aggregate["totals_cents"] == {"North": 25100, "South": 30000}
    assert len(detail["records"]) == 8 and all("product" not in row for row in detail["records"])
    assert aggregate["source_version"] == detail["source_version"]
    assert aggregate["definitions"] == detail["definitions"]
    assert aggregate["start"] == "2026-09-01" and aggregate["end"] == "2026-10-01"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM sales",
        "UPDATE sales SET amount_cents=0",
        "CREATE TABLE leak(x)",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA table_info(sales)",
        "SELECT * FROM sqlite_master",
        "SELECT load_extension('evil')",
        "SELECT readfile('secrets')",
        "SELECT randomblob(100000000)",
        "SELECT sale_id FROM sales; DELETE FROM sales",
        "INSERT INTO sales SELECT * FROM sales",
    ],
)
def test_sql_authorizer_denies_writes_files_extensions_and_multistatements(adapter, sql):
    db = adapter._connect()
    try:
        with pytest.raises(AdapterError):
            adapter._read(db, sql, ())
    finally:
        db.close()


def test_query_registry_parameters_and_limits(adapter):
    principal = Principal("demo", ("North",))
    request = ReportRequest(start="2026-09-01", end="2026-10-01", branches=["North"])
    with pytest.raises(AdapterError, match="query ID"):
        adapter.fetch(principal, request, "SELECT * FROM sales")
    with pytest.raises(AdapterError, match="Projection"):
        adapter.fetch(principal, request, "sales.detail.v1", ["branch", "tenant"])
    with pytest.raises(Forbidden):
        adapter.fetch(principal, request.model_copy(update={"branches": ["North') OR 1=1 --"]}), "sales.detail.v1")
    adapter.max_rows = 1
    with pytest.raises(AdapterError, match="row limit"):
        adapter.snapshot(principal)
    adapter.max_rows = 1000
    adapter.max_bytes = 10
    with pytest.raises(AdapterError, match="output limit"):
        adapter.fetch(principal, request, "sales.detail.v1")


def test_execution_deadline(adapter):
    adapter.deadline_seconds = -1
    db = adapter._connect()
    try:
        with pytest.raises(AdapterError, match="deadline"):
            adapter._read(db, "SELECT SUM(a.amount_cents*b.amount_cents) FROM sales a,sales b,sales c", ())
    finally:
        db.close()


def test_source_changes_and_permissions_change_cache_identity(adapter):
    full = Principal("demo", ("North", "South"))
    restricted = Principal("demo", ("North",))
    before = adapter.snapshot(full)
    assert before != adapter.snapshot(restricted)
    with sqlite3.connect(adapter.path) as db:
        db.execute("UPDATE sales SET amount_cents=amount_cents+1 WHERE sale_id=1")
    assert adapter.snapshot(full) != before
