import json

import httpx
import pytest

from gilm.budget import BudgetedProvider
from gilm.config import Settings
from gilm.models import ChatRequest, LiveEvaluation
from gilm.providers import HTTPProvider, MockProvider, ProviderError
from scripts.openrouter import is_free

MODEL = "test/model:free"
PAYLOAD = {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 32}


def configured(tmp_path, **overrides):
    return Settings(
        **{
            "data_dir": tmp_path,
            "http_url": "https://openrouter.ai/api/v1/chat/completions",
            "http_key": "not-a-real-key",
            "http_models": (MODEL,),
            "openrouter_free_only": True,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "change",
    [
        {"http_url": "https://other.example/v1/chat/completions"},
        {"http_models": ("test/paid-model",)},
        {"http_models": ("openrouter/free",)},
        {"http_pricing": {MODEL: {"input": 0, "output": 1, "version": "paid"}}},
    ],
)
def test_free_only_rejects_paid_or_ambiguous_configuration(tmp_path, change):
    with pytest.raises(ValueError):
        configured(tmp_path, **change)


async def test_free_only_enforces_zero_prices_without_mutating_request(tmp_path):
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = HTTPProvider(configured(tmp_path), client)
        original = {**PAYLOAD, "provider": {"allow_fallbacks": True, "max_price": {"prompt": 100}}}
        await provider.complete(original)
        assert original["provider"]["allow_fallbacks"]
        assert observed[0]["provider"] == {
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
        }
        with pytest.raises(ProviderError):
            await provider.complete({**PAYLOAD, "model": "test/paid-model"})
        assert len(observed) == 1


async def test_free_provider_rate_limit_is_sanitized_and_not_retried(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": {"message": "private upstream debug"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            await HTTPProvider(configured(tmp_path), client).complete(PAYLOAD)
        assert error.value.code == "openrouter_http_429" and error.value.status == 429
        assert "private" not in str(error.value) and calls == 1


async def test_zero_budget_permits_free_calls_but_blocks_paid_calls():
    provider = MockProvider()
    rates = {MODEL: {"input": 0, "output": 0, "version": "free", "input_token_ceiling": 8192}}
    authorization = LiveEvaluation(allow_paid=True, max_estimated_usd=0)
    guard = BudgetedProvider(provider, rates, authorization)
    await guard.complete(PAYLOAD)
    assert provider.calls == 1 and guard.reserved == 0
    paid = {MODEL: {**rates[MODEL], "output": 1}}
    with pytest.raises(ProviderError):
        await BudgetedProvider(provider, paid, authorization).complete(PAYLOAD)
    assert provider.calls == 1


@pytest.mark.parametrize("price", ["0.001", "NaN", "Infinity", "unknown", None])
def test_discovery_rejects_nonzero_or_unknown_fees(price):
    model = {"id": MODEL, "pricing": {"prompt": "0", "completion": "0", "request": price}}
    assert not is_free(model)


def test_discovery_requires_explicit_free_id_and_known_zero_token_prices():
    assert is_free({"id": MODEL, "pricing": {"prompt": "0", "completion": "0", "discount": 0.5}})
    assert not is_free({"id": "test/model", "pricing": {"prompt": "0", "completion": "0"}})
    assert not is_free({"id": MODEL, "pricing": {"completion": "0"}})


async def test_reasoning_is_explicit_validated_and_not_silently_discarded():
    request = ChatRequest.model_validate({**PAYLOAD, "reasoning": {"enabled": False}})
    assert request.provider_payload()["reasoning"] == {"enabled": False}
    with pytest.raises(ValueError):
        ChatRequest.model_validate({**PAYLOAD, "reasoning": {"enabled": False, "arbitrary": True}})
    with pytest.raises(ProviderError) as error:
        await MockProvider().complete(request.provider_payload())
    assert error.value.code == "mock_reasoning_control_unsupported"
