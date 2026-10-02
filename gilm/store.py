from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .config import Principal
from .models import PlanConfig, canonical, digest


class NotFound(Exception):
    pass


class PlanConflict(Exception):
    pass


def expired(plan: dict) -> bool:
    return bool(plan["expires_at"] and datetime.fromisoformat(plan["expires_at"]) <= datetime.now(UTC))


class Store:
    def __init__(self, path: Path, default_provider="mock", default_model="mock-report-v1"):
        self.path = path
        self.default_provider = default_provider
        self.default_model = default_model
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript((Path(__file__).parent / "migrations" / "001_state.sql").read_text())
            db.executescript((Path(__file__).parent / "migrations" / "002_cache_generations.sql").read_text())
            db.executescript((Path(__file__).parent / "migrations" / "003_operational_indexes.sql").read_text())

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=0.1)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def baseline(self, principal: Principal, workflow: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT id FROM plans WHERE tenant=? AND scope=? AND workflow=? AND baseline=1",
                (principal.tenant, principal.scope, workflow),
            ).fetchone()
            if not row:
                config = PlanConfig(
                    workflow=workflow,
                    provider=self.default_provider,
                    model=self.default_model,
                    generation={"max_tokens": 512} if workflow == "sales_report" else {},
                )
                ident = "p_" + uuid.uuid4().hex
                db.execute(
                    "INSERT OR IGNORE INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                    (
                        ident,
                        principal.tenant,
                        principal.scope,
                        workflow,
                        None,
                        canonical(config.model_dump(mode="json")),
                        digest(config.model_dump(mode="json")),
                        "Unchanged baseline (" + self.default_provider + ")",
                        "{}",
                        None,
                        time.time(),
                    ),
                )
                row = db.execute(
                    "SELECT id FROM plans WHERE tenant=? AND scope=? AND workflow=? AND baseline=1",
                    (principal.tenant, principal.scope, workflow),
                ).fetchone()
        return self.plan(principal, row["id"])

    def plan(self, principal: Principal, ident: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM plans WHERE id=? AND tenant=? AND scope=?", (ident, principal.tenant, principal.scope)
            ).fetchone()
        if row is None:
            raise NotFound("Plan not found")
        result = dict(row)
        result["config"] = json.loads(result["config"])
        result["sources"] = json.loads(result["sources"])
        result["applicability"] = {"workflow": result["workflow"], "authorization_scope": result["scope"]}
        result["integrity_valid"] = digest(result["config"]) == result["config_hash"]
        with self.connection() as db:
            active = db.execute(
                "SELECT plan_id FROM activations WHERE tenant=? AND scope=? AND workflow=?",
                (principal.tenant, principal.scope, result["workflow"]),
            ).fetchone()
        result["active"] = result["id"] == (active[0] if active else self._baseline_id(principal, result["workflow"]))
        return result

    def _baseline_id(self, principal, workflow):
        with self.connection() as db:
            row = db.execute(
                "SELECT id FROM plans WHERE tenant=? AND scope=? AND workflow=? AND baseline=1",
                (principal.tenant, principal.scope, workflow),
            ).fetchone()
        return row[0] if row else None

    def active(self, principal: Principal, workflow: str) -> dict:
        baseline = self.baseline(principal, workflow)
        with self.connection() as db:
            row = db.execute(
                "SELECT plan_id FROM activations WHERE tenant=? AND scope=? AND workflow=?",
                (principal.tenant, principal.scope, workflow),
            ).fetchone()
        return self.plan(principal, row[0]) if row else baseline

    def create(self, principal, candidate) -> dict:
        parent = self.plan(principal, candidate.parent_version)
        if parent["workflow"] != candidate.config.workflow:
            raise PlanConflict("Parent and candidate workflows differ")
        ident = "p_" + uuid.uuid4().hex
        config = candidate.config.model_dump(mode="json")
        with self.connection() as db:
            db.execute(
                "INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    ident,
                    principal.tenant,
                    principal.scope,
                    candidate.config.workflow,
                    parent["id"],
                    canonical(config),
                    digest(config),
                    candidate.description,
                    canonical(candidate.source_versions),
                    candidate.expires_at.isoformat() if candidate.expires_at else None,
                    time.time(),
                ),
            )
        return self.plan(principal, ident)

    def plans(self, principal, workflow):
        self.baseline(principal, workflow)
        with self.connection() as db:
            rows = db.execute(
                "SELECT id FROM plans WHERE tenant=? AND scope=? AND workflow=? ORDER BY created_at",
                (principal.tenant, principal.scope, workflow),
            ).fetchall()
        return [self.plan(principal, row[0]) for row in rows]

    def save_evaluation(self, principal, plan, source, result):
        ident = "e_" + uuid.uuid4().hex
        with self.connection() as db:
            db.execute(
                "INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    principal.tenant,
                    principal.scope,
                    plan["id"],
                    source,
                    int(result["passed"]),
                    canonical(result),
                    time.time(),
                ),
            )
        return {"id": ident, "plan_id": plan["id"], "source_version": source, **result}

    def evaluations(self, principal, ident):
        self.plan(principal, ident)
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM evaluations WHERE tenant=? AND scope=? AND plan_id=? ORDER BY created_at DESC",
                (principal.tenant, principal.scope, ident),
            ).fetchall()
        return [{**dict(row), "result": json.loads(row["result"])} for row in rows]

    def activate(self, principal, ident, source, rollback=False):
        plan = self.plan(principal, ident)
        if expired(plan) or not plan["integrity_valid"]:
            raise PlanConflict("Plan is expired or failed integrity validation")
        if not plan["baseline"]:
            evaluations = self.evaluations(principal, ident)
            if not evaluations or not evaluations[0]["passed"] or evaluations[0]["source_version"] != source:
                raise PlanConflict("Activation requires a passing evaluation for the current source snapshot")
        previous = self.active(principal, plan["workflow"])
        if rollback:
            cursor = previous
            ancestors = set()
            while cursor["parent"]:
                ancestors.add(cursor["parent"])
                cursor = self.plan(principal, cursor["parent"])
            if ident not in ancestors and ident != previous["id"]:
                raise PlanConflict("Rollback target must be an ancestor of the active plan")
        with self.connection() as db:
            db.execute(
                "INSERT INTO activations VALUES(?,?,?,?) ON CONFLICT(tenant,scope,workflow) DO UPDATE SET plan_id=excluded.plan_id",
                (principal.tenant, principal.scope, plan["workflow"], ident),
            )
            db.execute(
                "INSERT INTO activation_events(tenant,scope,workflow,previous,current,action,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    principal.tenant,
                    principal.scope,
                    plan["workflow"],
                    previous["id"],
                    ident,
                    "rollback" if rollback else "activate",
                    time.time(),
                ),
            )
            db.execute("DELETE FROM response_cache WHERE tenant=? AND scope=?", (principal.tenant, principal.scope))
            db.execute(
                "INSERT INTO cache_generations VALUES(?,?,1) ON CONFLICT(tenant,scope) DO UPDATE SET generation=generation+1",
                (principal.tenant, principal.scope),
            )
        return self.plan(principal, ident)

    def trace(self, principal, metadata):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO traces VALUES(?,?,?,?,?)",
                (metadata["request_id"], principal.tenant, principal.scope, canonical(metadata), time.time()),
            )

    def traces(self, principal, limit=100):
        with self.connection() as db:
            rows = db.execute(
                "SELECT metadata FROM traces WHERE tenant=? AND scope=? ORDER BY created_at DESC LIMIT ?",
                (principal.tenant, principal.scope, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def trace_by_id(self, principal, request_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT metadata FROM traces WHERE request_id=? AND tenant=? AND scope=?",
                (request_id, principal.tenant, principal.scope),
            ).fetchone()
        if row is None:
            raise NotFound("Trace not found")
        return json.loads(row[0])

    def cache_generation(self, principal):
        with self.connection() as db:
            row = db.execute(
                "SELECT generation FROM cache_generations WHERE tenant=? AND scope=?",
                (principal.tenant, principal.scope),
            ).fetchone()
        return row[0] if row else 0

    def cache_get(self, principal, key):
        with self.connection() as db:
            row = db.execute(
                "SELECT response FROM response_cache WHERE key=? AND tenant=? AND scope=? AND expires_at>?",
                (key, principal.tenant, principal.scope, time.time()),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, principal, key, plan_id, response, ttl, generation=0):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO response_cache SELECT ?,?,?,?,?,? WHERE COALESCE((SELECT generation FROM cache_generations WHERE tenant=? AND scope=?),0)=?",
                (
                    key,
                    principal.tenant,
                    principal.scope,
                    plan_id,
                    canonical(response),
                    time.time() + ttl,
                    principal.tenant,
                    principal.scope,
                    generation,
                ),
            )

    def invalidate(self, principal):
        with self.connection() as db:
            db.execute(
                "INSERT INTO cache_generations VALUES(?,?,1) ON CONFLICT(tenant,scope) DO UPDATE SET generation=generation+1",
                (principal.tenant, principal.scope),
            )
            result = db.execute(
                "DELETE FROM response_cache WHERE tenant=? AND scope=?", (principal.tenant, principal.scope)
            )
        return result.rowcount

    def prune(self, retention_days):
        with self.connection() as db:
            db.execute("DELETE FROM traces WHERE created_at<?", (time.time() - retention_days * 86400,))
            db.execute("DELETE FROM response_cache WHERE expires_at<=?", (time.time(),))
