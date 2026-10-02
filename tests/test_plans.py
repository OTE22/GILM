import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_candidate


def test_evaluation_activation_cache_rollback(client, report):
    baseline, plan = make_candidate(client)
    assert client.post(f"/api/plans/{plan['id']}/activate").status_code == 409
    evaluation = client.post(f"/api/plans/{plan['id']}/evaluate")
    assert evaluation.status_code == 200, evaluation.text
    data = evaluation.json()
    assert data["passed"] and len(data["rows"]) == 24
    assert {r["split"] for r in data["rows"]} == {"development", "held_out"}
    assert data["metrics"]["candidate"]["warm"]["provider_rate_card_estimate_usd"] == 0
    assert (
        data["metrics"]["candidate"]["cold"]["input_token_estimates"]
        < data["metrics"]["baseline"]["cold"]["input_token_estimates"]
    )
    assert client.get(f"/api/plans/{plan['id']}").json()["active"] is False
    assert client.post(f"/api/plans/{plan['id']}/activate").json()["active"]
    first = client.post("/api/reports/sales", json=report).json()
    second = client.post("/api/reports/sales", json=report).json()
    assert first["cache_status"] == "miss" and second["cache_status"] == "hit"
    assert first["answer"] == second["answer"]
    hit = client.get("/api/traces").json()[0]
    assert hit["attempts"] == [] and hit["provider_rate_card_estimate_usd"] == 0
    assert client.post(f"/api/plans/{baseline['id']}/rollback").json()["active"]
    rolled_back = client.post("/api/reports/sales", json=report).json()
    assert rolled_back["plan_version"] == baseline["id"] and rolled_back["answer"] == first["answer"]
    assert rolled_back["cache_status"] == "bypass"
    diff = client.get(f"/api/plans/{plan['id']}/diff").json()
    assert diff["changes"]["adapter_mode"] == {"before": "detailed", "after": "aggregate"}
    assert diff["source_changes"]["after"] == plan["sources"]


def test_immutable_versions(app, client):
    baseline, plan = make_candidate(client)
    with app.state.engine.store.connection() as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE plans SET description='changed' WHERE id=?", (plan["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM plans WHERE id=?", (baseline["id"],))


def test_expired_candidate_fallback(client, report):
    baseline, plan = make_candidate(client)
    expired_plan = client.post(
        "/api/plans",
        json={"parent_version": baseline["id"], "description": "Expired", "config": plan["config"], "sources": {}},
    )
    assert expired_plan.status_code == 422
    expired_plan = client.post(
        "/api/plans",
        json={
            "parent_version": baseline["id"],
            "description": "Expired",
            "config": plan["config"],
            "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        },
    ).json()
    result = client.post("/api/reports/sales", json={**report, "plan_version": expired_plan["id"]}).json()
    assert result["fallback_reason"] == "plan_expired" and result["plan_version"] == baseline["id"]
    assert client.post(f"/api/plans/{expired_plan['id']}/activate").status_code == 409
    assert client.post(f"/api/plans/{expired_plan['id']}/evaluate").json()["passed"] is False


def test_source_change_fallback_and_activation_stale(app, client, report):
    _, plan = make_candidate(client)
    assert client.post(f"/api/plans/{plan['id']}/evaluate").json()["passed"]
    assert client.post(f"/api/plans/{plan['id']}/activate").status_code == 200
    client.post("/api/reports/sales", json=report)
    with sqlite3.connect(app.state.engine.adapter.path) as db:
        db.execute("UPDATE sales SET amount_cents=amount_cents+100 WHERE sale_id=1")
    response = client.post("/api/reports/sales", json=report).json()
    assert response["fallback_reason"] == "source_version_changed" and response["cache_status"] == "bypass"
    assert response["answer"]["sales_by_branch_cents"]["North"] == 25200
    assert client.post(f"/api/plans/{plan['id']}/activate").status_code == 409


def test_projection_must_preserve_required_facts(client):
    baseline = client.get("/api/plans").json()[0]
    invalid = {**baseline["config"], "selected_fields": ["branch", "amount_cents"]}
    assert (
        client.post(
            "/api/plans", json={"parent_version": baseline["id"], "description": "bad", "config": invalid}
        ).status_code
        == 422
    )


def test_cache_ttl_invalidation_scope_and_request_parameters(app, client, report):
    _, plan = make_candidate(client)
    client.post(f"/api/plans/{plan['id']}/evaluate")
    client.post(f"/api/plans/{plan['id']}/activate")
    client.post("/api/reports/sales", json=report)
    assert client.post("/api/reports/sales", json=report).json()["cache_status"] == "hit"
    changed = client.post("/api/reports/sales", json={**report, "branches": ["North"]}).json()
    assert changed["cache_status"] == "miss" and set(changed["answer"]["sales_by_branch_cents"]) == {"North"}
    restricted = client.post("/api/reports/sales", json=report, headers={"Authorization": "Bearer north-key"}).json()
    assert restricted["cache_status"] == "bypass"
    with app.state.engine.store.connection() as db:
        db.execute("UPDATE response_cache SET expires_at=0")
    assert client.post("/api/reports/sales", json=report).json()["cache_status"] == "miss"
    assert client.post("/api/cache/invalidate").json()["invalidated"] >= 1
    assert client.post("/api/reports/sales", json=report).json()["cache_status"] == "miss"
    assert (
        client.post("/api/reports/sales", json={**report, "cache_approved": False}).json()["cache_status"] == "bypass"
    )


def test_retention_and_no_prompt_or_answer_logging(app, client, report):
    client.post("/api/reports/sales", json=report)
    metadata = client.get("/api/traces").json()
    encoded = json.dumps(metadata)
    assert "Office supplies" not in encoded and "sales_by_branch_cents" not in encoded
    with app.state.engine.store.connection() as db:
        db.execute("UPDATE traces SET created_at=0")
    assert client.get("/api/traces").json() == []
