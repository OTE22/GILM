"""Real sockets, TLS, HTTP, SQLite persistence and SSE; the upstream text is a test fixture, not an LLM."""

import json
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
import trustme
import uvicorn

from gilm.app import create_app
from gilm.config import Principal, Settings


@pytest.fixture
def tls_upstream(tmp_path):
    state = SimpleNamespace(requests=[], mode="normal", release=threading.Event())
    authority = trustme.CA()
    certificate = authority.issue_cert("127.0.0.1", "localhost")
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certificate.configure_cert(server_context)
    ca_file = tmp_path / "test-ca.pem"
    authority.cert_pem.write_to_path(ca_file)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            if (
                self.path != "/v1/chat/completions"
                or self.headers.get("Authorization") != "Bearer local-transport-fixture"
            ):
                self.send_error(401)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(body)
            if state.mode == "http-error":
                self.send_error(503)
                return
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(value):
                    self.wfile.write(f"{len(value):X}\r\n".encode() + value + b"\r\n")
                    self.wfile.flush()

                try:
                    chunk(b": transport fixture\r\n\r\ndata: first\n\n")
                    if state.mode == "stream-error":
                        self.close_connection = True  # Incomplete chunked body is an observable transport failure.
                        return
                    if state.mode == "stream-wait":
                        state.release.wait(timeout=5)
                    chunk(b"data: [DONE]\n\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (OSError, ssl.SSLError):
                    self.close_connection = True
                return
            contexts = [
                m["content"].split("\n", 1)[1]
                for m in body["messages"]
                if (m.get("content") or "").startswith("GILM_REPORT_CONTEXT\n")
            ]
            content = "HTTPS transport fixture response"
            if contexts:
                context = json.loads(contexts[-1])
                if "records" in context:
                    totals = {
                        branch: sum(
                            record["amount_cents"] for record in context["records"] if record["branch"] == branch
                        )
                        for branch in context["branches"]
                    }
                else:
                    totals = context["totals_cents"]
                content = json.dumps(
                    {
                        "sales_by_branch_cents": totals,
                        "start": context["start"],
                        "end": context["end"],
                        "currency": context["currency"],
                        "definition": "gross booked sales",
                        "source_version": context["source_version"],
                    }
                )
            reply = {
                "id": "tls-fixture",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 500, "completion_tokens": 80, "total_tokens": 580},
            }
            if state.mode == "unknown-usage":
                reply.pop("usage")
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    state.url = f"https://127.0.0.1:{server.server_port}/v1/chat/completions"
    state.ca_file = ca_file
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def running_gilm(settings):
    app = create_app(settings)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=3))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("GILM server did not start")
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            timeout=20,
            headers={"Authorization": "Bearer network-admin"},
        ) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive(), "GILM server did not stop"


def network_settings(tmp_path, upstream):
    return Settings(
        data_dir=tmp_path / "network-state",
        dev_mode=False,
        keys={"network-admin": Principal("demo", ("North", "South"), True)},
        default_provider="http",
        http_url=upstream.url,
        http_key="local-transport-fixture",
        http_ca_file=upstream.ca_file,
        http_models=("vendor/model",),
        http_pricing={
            "vendor/model": {"input": 1, "output": 2, "version": "transport-test-only", "input_token_ceiling": 8192}
        },
    )


def test_real_tls_plan_evaluation_activation_persistence_and_rollback(tmp_path, tls_upstream):
    settings = network_settings(tmp_path, tls_upstream)
    report = {"start": "2026-09-01", "end": "2026-10-01", "cache_approved": True}
    with running_gilm(settings) as client:
        assert client.get("/api/plans", headers={"Authorization": "Bearer invalid"}).status_code == 401
        baseline = client.get("/api/plans").json()[0]
        source = client.get("/api/source").json()["version"]
        created = client.post(
            "/api/plans",
            json={
                "parent_version": baseline["id"],
                "description": "Real TLS transport fixture plan",
                "config": {
                    **baseline["config"],
                    "adapter_mode": "aggregate",
                    "remove_redundant": True,
                    "cache": {"enabled": True},
                },
                "source_versions": {"sales": source},
            },
        )
        assert created.status_code == 201, created.text
        plan = created.json()
        assert client.post(f"/api/plans/{plan['id']}/evaluate").status_code == 409
        assert (
            client.post(
                f"/api/plans/{plan['id']}/evaluate-live", json={"allow_paid": False, "max_estimated_usd": 1}
            ).status_code
            == 422
        )
        assert tls_upstream.requests == []
        evaluation = client.post(
            f"/api/plans/{plan['id']}/evaluate-live", json={"allow_paid": True, "max_estimated_usd": 1}
        )
        assert evaluation.status_code == 200, evaluation.text
        result = evaluation.json()
        assert result["passed"] and not result["mock_only"] and len(result["rows"]) == 24
        assert len(tls_upstream.requests) == 16
        assert result["spending_guard"]["reserved_upper_bound_usd"] == pytest.approx(0.147456)
        assert result["metrics"]["candidate"]["cold"]["token_source"] == "provider_reported"
        assert result["metrics"]["candidate"]["cold"]["input_token_estimates"] is None
        assert client.get(f"/api/plans/{plan['id']}").json()["active"] is False
        assert client.post(f"/api/plans/{plan['id']}/activate").json()["active"]
        first = client.post("/api/reports/sales", json=report).json()
        assert first["answer"]["sales_by_branch_cents"] == {"North": 25100, "South": 30000}
        assert first["cache_status"] == "miss" and not first["mock"]
    calls = len(tls_upstream.requests)
    with running_gilm(settings) as restarted:
        assert restarted.get(f"/api/plans/{plan['id']}").json()["active"]
        hit = restarted.post("/api/reports/sales", json=report).json()
        assert hit["cache_status"] == "hit" and len(tls_upstream.requests) == calls
        assert restarted.post(f"/api/plans/{baseline['id']}/rollback").json()["active"]
        rollback = restarted.post("/api/reports/sales", json=report).json()
        assert rollback["plan_version"] == baseline["id"] and rollback["answer"] == first["answer"]


@pytest.mark.parametrize(
    "mode,reason,expected_calls",
    [
        ("http-error", "evaluation_provider_failure", 1),
        ("unknown-usage", "evaluation_usage_unknown", 1),
        ("normal", "evaluation_budget_exhausted", 0),
    ],
)
def test_real_tls_evaluation_stops_on_failure_unknown_usage_or_budget(
    tmp_path, tls_upstream, mode, reason, expected_calls
):
    settings = network_settings(tmp_path, tls_upstream)
    tls_upstream.mode = mode
    with running_gilm(settings) as client:
        baseline = client.get("/api/plans").json()[0]
        plan = client.post(
            "/api/plans",
            json={"parent_version": baseline["id"], "description": "Abort test", "config": baseline["config"]},
        ).json()
        result = client.post(
            f"/api/plans/{plan['id']}/evaluate-live",
            json={"allow_paid": True, "max_estimated_usd": "0.000001" if mode == "normal" else "1"},
        ).json()
        assert not result["passed"] and result["spending_guard"]["aborted_reason"] == reason
        assert len(tls_upstream.requests) == expected_calls
        assert client.post(f"/api/plans/{plan['id']}/activate").status_code == 409
        if mode == "unknown-usage":
            assert result["spending_guard"]["reported_rate_card_estimate_usd"] is None


def test_tls_verification_is_not_disabled(tmp_path, tls_upstream):
    settings = network_settings(tmp_path, tls_upstream)
    settings.http_ca_file = None
    with running_gilm(settings) as client:
        response = client.post(
            "/v1/chat/completions", json={"model": "vendor/model", "messages": [{"role": "user", "content": "test"}]}
        )
        assert response.status_code == 502 and response.json()["error"]["code"] == "provider_transport_failed"
        assert tls_upstream.requests == []


def test_network_stream_incremental_forwarding_failure_and_disconnect(tmp_path, tls_upstream):
    settings = network_settings(tmp_path, tls_upstream)
    request = {"model": "vendor/model", "messages": [{"role": "user", "content": "opaque"}], "stream": True}
    first = b": transport fixture\r\n\r\ndata: first\n\n"
    with running_gilm(settings) as client:
        tls_upstream.mode = "stream-wait"
        with client.stream("POST", "/v1/chat/completions", json=request) as stream:
            iterator = stream.iter_raw()
            received = next(iterator)
            assert received == first  # Upstream is still waiting: the proxy did not buffer to completion.
            tls_upstream.release.set()
            assert b"".join(iterator) == b"data: [DONE]\n\n"
        tls_upstream.mode = "stream-error"
        response = client.post("/v1/chat/completions", json=request)
        assert response.content == first
        assert client.get("/api/traces").json()[0]["outcome"] == "partial_stream_failed"
        tls_upstream.mode = "stream-wait"
        tls_upstream.release.clear()
        with client.stream("POST", "/v1/chat/completions", json=request) as stream:
            assert next(stream.iter_raw()) == first
            request_id = stream.headers["x-request-id"]
        for _ in range(100):
            matching = [trace for trace in client.get("/api/traces").json() if trace["request_id"] == request_id]
            if matching:
                break
            time.sleep(0.01)
        assert matching[0]["outcome"] == "cancelled"
        assert len(tls_upstream.requests) == 3
        tls_upstream.release.set()
