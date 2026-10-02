import asyncio
import sqlite3

import httpx
import pytest

from gilm.app import BoundaryMiddleware, create_app
from gilm.backup import backup, restore
from gilm.config import Principal, Settings
from gilm.providers import MockProvider
from gilm.runtime import Governor


def test_production_requires_authentication_and_strong_keys(tmp_path):
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, production=True)
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, production=True, dev_mode=False, keys={"short": Principal("demo", ("North",))})
    Settings(data_dir=tmp_path, production=True, dev_mode=False, keys={"k" * 32: Principal("demo", ("North",))})


async def test_slow_upload_deadline_releases_admission(settings):
    settings.upload_timeout_seconds = 0.02
    governor = Governor(settings)
    sent = []

    async def downstream(*args):
        raise AssertionError("Slow upload must not reach application")

    async def receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"{}"}

    async def send(message):
        sent.append(message)

    middleware = BoundaryMiddleware(downstream, settings, governor)
    await middleware({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}, receive, send)
    assert sent[0]["status"] == 408 and governor.active == 0


async def test_overload_rejected_before_provider_and_liveness_stays_available(settings):
    settings.max_inflight_requests = 1
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(MockProvider):
        async def complete(self, payload):
            entered.set()
            await release.wait()
            return await super().complete(payload)

    provider = WaitingProvider()
    app = create_app(settings, providers={"mock": provider})
    payload = {"model": "mock-report-v1", "messages": [{"role": "user", "content": "hello"}]}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)), base_url="http://localhost"
        ) as client,
    ):
        first = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
        await asyncio.wait_for(entered.wait(), 2)
        second = await client.post("/v1/chat/completions", json=payload)
        assert second.status_code == 503 and second.json()["error"]["code"] == "gateway_busy"
        assert (await client.get("/health")).status_code == 200
        release.set()
        assert (await first).status_code == 200
        assert provider.calls == 1 and app.state.governor.active == 0


def test_rate_limits_share_authorization_scope_but_isolate_tenants(settings):
    settings.requests_per_minute = 1
    governor = Governor(settings)
    assert governor.allow(Principal("a", ("North",), True))
    assert not governor.allow(Principal("a", ("North",), False))
    assert governor.allow(Principal("b", ("North",), True))


async def test_rate_limit_returns_retry_after_and_never_dispatches(settings):
    settings.requests_per_minute = 1
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)), base_url="http://localhost"
        ) as client,
    ):
        assert (await client.get("/api/plans")).status_code == 200
        result = await client.post(
            "/v1/chat/completions", json={"model": "mock-report-v1", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert result.status_code == 429 and result.headers["retry-after"] == "60"
        assert app.state.engine.providers["mock"].calls == 0


def test_backup_restore_and_tamper_detection(settings, tmp_path):
    app = create_app(settings)
    store = app.state.engine.store
    identity = Principal("demo", ("North", "South"), True)
    original = store.baseline(identity, "sales_report")
    snapshot = tmp_path / "snapshot"
    restored = tmp_path / "restored"
    backup(settings.data_dir, snapshot)
    restore(snapshot, restored)
    with sqlite3.connect(restored / "state.sqlite") as db:
        assert db.execute("SELECT id FROM plans").fetchone()[0] == original["id"]
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with pytest.raises(ValueError):
        restore(snapshot, restored)
    with (snapshot / "state.sqlite").open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValueError, match="checksum"):
        restore(snapshot, tmp_path / "tampered")
    assert not (tmp_path / "tampered").exists()
