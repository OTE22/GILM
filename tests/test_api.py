import json

import pytest
from fastapi.testclient import TestClient

from gilm.app import create_app
from gilm.config import Settings

CHAT = {"model": "mock-report-v1", "messages": [{"role": "user", "content": "hello"}]}


def test_chat_health_dashboard_and_metadata(client):
    assert client.get("/health").json()["default_provider"] == "mock"
    assert client.get("/ready").status_code == 200
    result = client.post("/v1/chat/completions", json=CHAT)
    assert result.status_code == 200
    assert result.headers["x-request-id"].startswith("req_")
    assert result.json()["choices"][0]["message"]["content"] == "Deterministic GILM mock response."
    trace = client.get("/api/traces").json()[0]
    assert trace["mock"] and len(trace["attempts"]) == 1
    assert trace["attempts"][0]["usage_source"] == "mock_character_estimate"
    assert "hello" not in json.dumps(trace)
    assert trace["invoice_reconciled_usd"] is None
    assert client.get("/").status_code == 200
    assert "Synthetic reporting dataset" in client.get("/").text
    assert client.get("/static/dashboard.js").status_code == 200
    assert client.get("/static/unknown.txt").status_code == 404


@pytest.mark.parametrize("extra", ["n", "logprobs", "max_completion_tokens", "stream_options", "user"])
def test_unsupported_fields_rejected_without_prompt_echo(client, extra):
    request = {**CHAT, extra: "sensitive-do-not-log", "messages": [{"role": "user", "content": "sensitive-do-not-log"}]}
    result = client.post("/v1/chat/completions", json=request)
    assert result.status_code == 422
    assert "sensitive-do-not-log" not in result.text
    assert result.json()["error"]["request_id"] == result.headers["x-request-id"]


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": [{"type": "text", "text": "unsupported"}]}],
        [{"role": "tool", "content": "orphan", "tool_call_id": "missing"}],
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            }
        ],
        [{"role": "unknown", "content": "x"}],
    ],
)
def test_content_and_tool_relationship_validation(client, messages):
    assert client.post("/v1/chat/completions", json={**CHAT, "messages": messages}).status_code == 422


def test_bound_payloads_and_media_type(client):
    result = client.post("/v1/chat/completions", content=b"x" * 262145, headers={"Content-Type": "application/json"})
    assert result.status_code == 413
    assert client.post("/v1/chat/completions", content=json.dumps(CHAT)).status_code == 415
    assert (
        client.post("/v1/chat/completions", content="{", headers={"Content-Type": "application/json"}).status_code
        == 422
    )
    invalid = json.dumps({**CHAT, "temperature": float("inf")})
    assert (
        client.post("/v1/chat/completions", content=invalid, headers={"Content-Type": "application/json"}).status_code
        == 422
    )


def test_fail_closed_authentication_and_origin(client, settings):
    assert client.get("/api/traces", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/traces", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.get("/api/traces", headers={"Origin": "http://127.0.0.1"}).status_code == 200
    with TestClient(create_app(settings), base_url="http://127.0.0.1", client=("10.0.0.5", 12)) as remote:
        assert remote.get("/api/plans").status_code == 401
        assert remote.get("/api/plans", headers={"Authorization": "Bearer admin-key"}).status_code == 200
    assert client.get("/api/traces", headers={"Host": "evil.example"}).status_code == 401
    with pytest.raises(ValueError, match="loopback"):
        Settings(data_dir=settings.data_dir, host="0.0.0.0")
    with pytest.raises(ValueError, match="API_KEYS"):
        Settings(data_dir=settings.data_dir, dev_mode=False)


def test_tenant_and_authorization_scope_isolation(client, report):
    demo = client.post("/api/reports/sales", json=report).json()
    assert demo["answer"]["sales_by_branch_cents"] == {"North": 25100, "South": 30000}
    north = {"Authorization": "Bearer north-key", "X-Tenant-ID": "other"}
    own = client.post("/api/reports/sales", json=report, headers=north).json()
    assert own["answer"]["sales_by_branch_cents"] == {"North": 25100}
    denied = client.post("/api/reports/sales", json={**report, "branches": ["South"]}, headers=north)
    assert denied.status_code == 403
    traces = client.get("/api/traces", headers=north).json()
    assert len(traces) == 1 and traces[0]["request_id"] == own["request_id"]
    assert client.get(f"/api/plans/{demo['plan_version']}", headers=north).status_code == 404
    assert client.post("/api/cache/invalidate", headers=north).status_code == 403
    other = {"Authorization": "Bearer other-key"}
    result = client.post("/api/reports/sales", json=report, headers=other)
    assert result.json()["answer"]["sales_by_branch_cents"] == {"Central": 900000}
    assert client.get("/api/traces", headers=other).json()[0]["request_id"] == result.json()["request_id"]


def test_reporting_cannot_be_forged_on_proxy_route(client):
    assert client.post("/v1/chat/completions", json={**CHAT, "gilm": {"workflow": "sales_report"}}).status_code == 422


@pytest.mark.parametrize(
    "response_format",
    [
        {"type": "json_schema"},
        {"type": "json_object", "json_schema": {}},
        {"type": "json_schema", "json_schema": {"name": "x", "schema": {}, "discarded": 1}},
        {"type": "json_schema", "json_schema": {"name": "x", "schema": {}, "strict": "yes"}},
    ],
)
def test_response_format_validation(client, response_format):
    assert client.post("/v1/chat/completions", json={**CHAT, "response_format": response_format}).status_code == 422


def test_mock_json_object_and_explicit_schema_limitation(client):
    from gilm.report_contract import REPORT_RESPONSE_FORMAT

    result = client.post("/v1/chat/completions", json={**CHAT, "response_format": {"type": "json_object"}})
    assert result.status_code == 200 and json.loads(result.json()["choices"][0]["message"]["content"])["message"]
    result = client.post("/v1/chat/completions", json={**CHAT, "response_format": REPORT_RESPONSE_FORMAT})
    assert result.status_code == 422 and result.json()["error"]["code"] == "mock_json_schema_generation_unsupported"
