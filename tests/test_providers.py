import asyncio
import json

import httpx
import pytest

from gilm.accounting import account
from gilm.app import create_app
from gilm.config import Principal, Settings
from gilm.models import ChatRequest, Message
from gilm.providers import HTTPProvider, MockProvider, ProviderError
from gilm.report_contract import REPORT_RESPONSE_FORMAT


def test_cost_provenance_cached_input_and_no_reasoning_double_count():
    prices = {"m": {"input": 10, "cached_input": 1, "output": 20, "version": "configured-v1"}}
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 900},
        "completion_tokens_details": {"reasoning_tokens": 80},
    }
    entry = account("http", "m", usage, "provider_reported", prices)
    assert entry["rate_card_estimate_usd"] == pytest.approx(0.0039)
    assert entry["uncached_input_tokens"] == 100 and entry["output_tokens"] == 100
    assert entry["invoice_reconciled_usd"] is None and entry["tool_cost_usd"] is None
    assert account("http", "unknown", usage, "provider_reported", prices)["rate_card_estimate_usd"] is None
    assert account("http", "m", None, "unknown", prices)["rate_card_estimate_usd"] is None
    small_uncached = account(
        "http",
        "m",
        {"prompt_tokens": 500, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}},
        "provider_reported",
        prices,
    )
    assert small_uncached["rate_card_estimate_usd"] > entry["rate_card_estimate_usd"]
    unobserved = account("http", "m", {"prompt_tokens": 500, "completion_tokens": 100}, "provider_reported", prices)
    assert unobserved["uncached_rate_upper_bound"] and unobserved["discounted_cached_input_tokens"] is None
    no_discount_rate = {"m": {"input": 10, "output": 20, "version": "no-discount-card"}}
    observed_zero = {"prompt_tokens": 500, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}
    assert account("http", "m", observed_zero, "provider_reported", no_discount_rate)[
        "rate_card_estimate_usd"
    ] == pytest.approx(0.007)
    assert account("http", "m", usage, "provider_reported", no_discount_rate)["rate_card_estimate_usd"] is None


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks, fail=False):
        self.chunks, self.fail, self.closed = chunks, fail, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadError("ambiguous upstream failure")

    async def aclose(self):
        self.closed = True


async def test_http_exact_payload_and_unknown_usage(settings):
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}], "extra_vendor_field": 3}
        )

    configured = Settings(
        data_dir=settings.data_dir,
        http_url="https://provider.test/v1/chat/completions",
        http_key="secret",
        http_models=("model",),
    )
    request = ChatRequest(
        model="model",
        messages=[Message(role="system", content="policy"), Message(role="user", content="task")],
        temperature=0,
        seed=7,
        max_tokens=40,
        tools=[{"function": {"name": "f", "parameters": {"type": "object"}}}],
        tool_choice="none",
        parallel_tool_calls=False,
        response_format=REPORT_RESPONSE_FORMAT,
        reasoning={"enabled": False},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = HTTPProvider(configured, client)
        reply = await provider.complete(request.provider_payload())
    assert observed == [request.provider_payload()]
    assert observed[0]["response_format"] == REPORT_RESPONSE_FORMAT
    assert reply.body["extra_vendor_field"] == 3 and reply.usage is None and reply.usage_source == "unknown"


async def test_sse_raw_bytes_forwarding_and_close(settings):
    chunks = [b": heartbeat\r\n\r\n", b'event: message\ndata: {"delta":', b'"hi"}\n\n', b"data: [DONE]\n\n"]
    stream = ByteStream(chunks)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    configured = Settings(
        data_dir=settings.data_dir,
        http_url="https://provider.test/v1/chat/completions",
        http_key="secret",
        http_models=("model",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app = create_app(settings, providers={"mock": HTTPProvider(configured, upstream)})
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 3))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                payload = {
                    "model": "model",
                    "messages": [{"role": "user", "content": "same"}, {"role": "user", "content": "same"}],
                    "stream": True,
                    "temperature": 0.1,
                    "gilm": {"cache_approved": True, "sensitive": False},
                }
                response = await client.post("/v1/chat/completions", json=payload)
                assert response.status_code == 200 and response.content == b"".join(chunks)
                assert requests[0] == {key: value for key, value in payload.items() if key != "gilm"}
                traces = (await client.get("/api/traces")).json()
                assert traces[0]["cache_status"] == "bypass" and traces[0]["fallback_reason"] == "streaming_bypass"
                assert traces[0]["attempts"][0]["usage_source"] == "unknown"
    assert stream.closed and len(requests) == 1


async def test_partial_stream_failure_does_not_replay(settings):
    stream = ByteStream([b"data: partial\n\n"], fail=True)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    configured = Settings(
        data_dir=settings.data_dir,
        http_url="https://provider.test/v1/chat/completions",
        http_key="secret",
        http_models=("model",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app = create_app(settings, providers={"mock": HTTPProvider(configured, upstream)})
        async with app.router.lifespan_context(app):
            engine = app.state.engine
            principal = Principal("demo", ("North", "South"), True)
            iterator = await engine.stream(
                principal,
                ChatRequest(model="model", messages=[Message(role="user", content="x")], stream=True),
                "partial",
            )
            assert b"".join([chunk async for chunk in iterator]) == b"data: partial\n\n"
            assert engine.store.traces(principal)[0]["outcome"] == "partial_stream_failed"
    assert len(calls) == 1 and stream.closed


async def test_nonstream_cancellation_timeout_and_ambiguous_failure_no_replay(settings):
    provider = MockProvider(delay=10)
    app = create_app(settings, providers={"mock": provider})
    identity = Principal("demo", ("North", "South"), True)
    request = ChatRequest(model="mock-report-v1", messages=[Message(role="user", content="x")])
    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app.state.engine.chat(identity, request, "cancelled"))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app.state.engine.store.traces(identity)[0]["outcome"] == "cancelled"
        settings.timeout_seconds = 0.01
        with pytest.raises(ProviderError, match="not retried"):
            await app.state.engine.chat(identity, request, "timeout")
        assert app.state.engine.store.traces(identity)[0]["outcome"] == "timeout"
        provider.delay, provider.fail = 0, True
        with pytest.raises(ProviderError):
            await app.state.engine.chat(identity, request, "failed")
        assert provider.calls == 3
        assert all(len(t["attempts"]) == 1 for t in app.state.engine.store.traces(identity))


async def test_stream_cancellation_closes_upstream(settings):
    closed = asyncio.Event()

    class CancellableProvider:
        calls = 0

        async def open_stream(self, payload):
            self.calls += 1

            async def chunks():
                try:
                    yield b"data: first\n\n"
                    await asyncio.sleep(10)
                finally:
                    closed.set()

            return chunks()

    provider = CancellableProvider()
    app = create_app(settings, providers={"mock": provider})
    identity = Principal("demo", ("North", "South"), True)
    async with app.router.lifespan_context(app):
        iterator = await app.state.engine.stream(
            identity,
            ChatRequest(model="mock-report-v1", messages=[Message(role="user", content="x")], stream=True),
            "cancel-stream",
        )
        assert await anext(iterator) == b"data: first\n\n"
        task = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and provider.calls == 1
        assert app.state.engine.store.traces(identity)[0]["outcome"] == "cancelled"


async def test_open_stream_closed_before_first_iteration(settings):
    stream = ByteStream([b"data: pending\n\n"])
    configured = Settings(
        data_dir=settings.data_dir,
        http_url="https://provider.test/v1/chat/completions",
        http_key="secret",
        http_models=("model",),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
        )
    ) as upstream:
        app = create_app(settings, providers={"mock": HTTPProvider(configured, upstream)})
        identity = Principal("demo", ("North", "South"), True)
        async with app.router.lifespan_context(app):
            relay = await app.state.engine.stream(
                identity,
                ChatRequest(model="model", messages=[Message(role="user", content="x")], stream=True),
                "never-started",
            )
            await relay.aclose()
            await relay.aclose()
            assert stream.closed
            traces = app.state.engine.store.traces(identity)
            assert len(traces) == 1 and traces[0]["outcome"] == "cancelled" and traces[0]["stream_bytes"] == 0


def test_http_disabled_and_https_destination_validation(settings):
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(data_dir=settings.data_dir, http_url="http://example.com", http_key="secret", http_models=("m",))
    with pytest.raises(ValueError, match="key"):
        Settings(data_dir=settings.data_dir, http_url="https://example.com")


async def test_provider_pre_dispatch_configuration_and_shape_errors(settings):
    payload = {"model": "model", "messages": [{"role": "user", "content": "x"}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": []}))
    ) as client:
        with pytest.raises(ProviderError) as error:
            await HTTPProvider(settings, client).complete(payload)
        assert error.value.code == "provider_not_configured"
        configured = Settings(
            data_dir=settings.data_dir,
            http_url="https://provider.test/v1/chat/completions",
            http_key="secret",
            http_models=("model",),
        )
        with pytest.raises(ProviderError) as error:
            await HTTPProvider(configured, client).complete(payload)
        assert error.value.code == "provider_invalid_response"
