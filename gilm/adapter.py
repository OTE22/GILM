from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import Principal
from .models import ReportRequest, digest


class Forbidden(Exception):
    pass


class AdapterError(Exception):
    pass


COLUMNS = {"sale_id", "tenant", "branch", "date", "amount_cents", "currency", "product", "units"}
PUBLIC_COLUMNS = COLUMNS - {"tenant"}
DEFINITIONS = "Sales are gross booked sales, not profit. Amounts are integer cents in USD. Dates use [start, end). Include every requested branch, including zero sales."
QUERY_IDS = {"sales.detail.v1", "sales.totals.v1"}


class ReportingAdapter:
    """A fixed query registry; SQL and file paths never come from API callers."""

    def __init__(self, path: Path, max_rows=1000, max_bytes=131_072, deadline_seconds=0.2):
        self.path = path.resolve()
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.deadline_seconds = deadline_seconds
        self.initialize()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with sqlite3.connect(self.path) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                return
            if version != 0:
                raise AdapterError("Unsupported reporting schema version")
        fixtures = Path(__file__).parent / "fixtures" / "sales.json"
        with sqlite3.connect(self.path) as db:
            db.executescript(
                "BEGIN IMMEDIATE;\n" + (Path(__file__).parent / "migrations" / "001_reporting.sql").read_text()
            )
            db.executemany(
                "INSERT INTO sales VALUES(:sale_id,:tenant,:branch,:date,:amount_cents,:currency,:product,:units)",
                json.loads(fixtures.read_text()),
            )

    @staticmethod
    def authorize(principal: Principal, branches: list[str] | None) -> list[str]:
        selected = sorted(branches if branches is not None else principal.branches)
        if not selected or not set(selected) <= set(principal.branches):
            raise Forbidden("Branch access denied")
        return selected

    @staticmethod
    def _authorizer(action, first, second, database, trigger):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and database == "main" and first == "sales" and second in COLUMNS:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in {"sum", "count", "coalesce"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def _connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
        db.row_factory = sqlite3.Row
        db.enable_load_extension(False)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA temp_store=MEMORY")
        # Pin snapshot before installing an authorizer that denies transactions too.
        db.execute("BEGIN")
        db.set_authorizer(self._authorizer)
        deadline = time.monotonic() + self.deadline_seconds
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
        return db

    def _read(self, db, sql, params):
        try:
            rows = db.execute(sql, params).fetchmany(self.max_rows + 1)
        except sqlite3.Error as exc:
            raise AdapterError("Approved query denied or exceeded its deadline") from exc
        if len(rows) > self.max_rows:
            raise AdapterError("Reporting row limit exceeded; choose a smaller range")
        result = [dict(row) for row in rows]
        if len(json.dumps(result).encode()) > self.max_bytes:
            raise AdapterError("Reporting output limit exceeded")
        return result

    def _snapshot(self, db, principal):
        branches = self.authorize(principal, None)
        placeholders = ",".join("?" for _ in branches)
        rows = self._read(
            db,
            f"SELECT sale_id,branch,date,amount_cents,currency,product,units FROM sales WHERE tenant=? AND branch IN ({placeholders}) ORDER BY sale_id",
            (principal.tenant, *branches),
        )
        return digest(rows)

    def snapshot(self, principal):
        db = self._connect()
        try:
            return self._snapshot(db, principal)
        finally:
            db.close()

    def fetch(self, principal: Principal, request: ReportRequest, query_id: str, fields=None) -> dict:
        started = time.perf_counter()
        branches = self.authorize(principal, request.branches)
        if query_id not in QUERY_IDS:
            raise AdapterError("Unknown approved query ID")
        if fields is not None and (
            not set(fields) <= PUBLIC_COLUMNS or not {"branch", "date", "amount_cents", "currency"} <= set(fields)
        ):
            raise AdapterError("Projection omits required fields or includes unapproved columns")
        db = self._connect()
        try:
            source = self._snapshot(db, principal)
            placeholders = ",".join("?" for _ in branches)
            where = f"tenant=? AND branch IN ({placeholders}) AND date>=? AND date<?"
            params = (principal.tenant, *branches, request.start.isoformat(), request.end.isoformat())
            if query_id == "sales.totals.v1":
                rows = self._read(
                    db,
                    f"SELECT branch,currency,SUM(amount_cents) AS amount_cents FROM sales WHERE {where} GROUP BY branch,currency ORDER BY branch",
                    params,
                )
                totals = {branch: 0 for branch in branches}
                for row in rows:
                    if row["currency"] != "USD":
                        raise AdapterError("Mixed or unsupported reporting currency")
                    totals[row["branch"]] = row["amount_cents"]
                payload = {"kind": "aggregate", "totals_cents": totals}
            else:
                projection = ",".join(
                    fields or ["sale_id", "branch", "date", "amount_cents", "currency", "product", "units"]
                )
                rows = self._read(db, f"SELECT {projection} FROM sales WHERE {where} ORDER BY sale_id", params)
                if any(row["currency"] != "USD" for row in rows):
                    raise AdapterError("Mixed or unsupported reporting currency")
                payload = {"kind": "detailed", "records": rows}
            return {
                **payload,
                "start": request.start.isoformat(),
                "end": request.end.isoformat(),
                "branches": branches,
                "currency": "USD",
                "definitions": DEFINITIONS,
                "source_version": source,
                "provenance": "synthetic-sqlite-sales",
                "query_id": query_id,
                "adapter_ms": round((time.perf_counter() - started) * 1000, 3),
                "rows_returned": len(rows),
            }
        finally:
            db.close()
