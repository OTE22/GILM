import asyncio
from decimal import Decimal

import pytest

from gilm.app import create_app
from gilm.budget import BudgetedProvider
from gilm.config import Principal, Settings
from gilm.models import Candidate, LiveEvaluation, PlanConfig, ReportRequest
from gilm.providers import MockProvider, ProviderError

PRICES = {
    "m": {"input": 1, "cached_input": 0.1, "output": 2, "input_token_ceiling": 8192, "version": "fixture-rate-card"}
}
PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "bounded task"}], "max_tokens": 512}


async def test_budget_reserves_before_dispatch_and_does_not_refund():
    provider = MockProvider()
    guard = BudgetedProvider(provider, PRICES, LiveEvaluation(allow_paid=True, max_estimated_usd=Decimal("0.01")))
    await guard.complete(PAYLOAD)
    with pytest.raises(ProviderError) as error:
        await guard.complete(PAYLOAD)
    assert error.value.code == "evaluation_budget_exhausted"
    assert provider.calls == 1
    assert guard.reserved == Decimal("0.009216")
    assert guard.summary()["reported_rate_card_estimate_usd"] < float(guard.reserved)


async def test_attempt_limit_and_ambiguous_failure_halt_future_calls():
    provider = MockProvider(fail=True)
    guard = BudgetedProvider(provider, PRICES, LiveEvaluation(allow_paid=True, max_estimated_usd=1))
    for _ in range(2):
        with pytest.raises(ProviderError):
            await guard.complete(PAYLOAD)
    assert provider.calls == 1 and guard.reserved == Decimal("0.009216")
    assert guard.summary()["reported_rate_card_estimate_usd"] is None
    provider = MockProvider()
    guard = BudgetedProvider(
        provider, PRICES, LiveEvaluation(allow_paid=True, max_estimated_usd=1, max_provider_attempts=1)
    )
    await guard.complete(PAYLOAD)
    with pytest.raises(ProviderError):
        await guard.complete(PAYLOAD)
    assert provider.calls == 1


async def test_invalid_budget_payloads_never_dispatch():
    provider = MockProvider()
    guard = BudgetedProvider(provider, PRICES, LiveEvaluation(allow_paid=True, max_estimated_usd=1))
    for change in (
        {"max_tokens": None},
        {"max_tokens": 513},
        {"stream": True},
        {"tools": [{"function": {"name": "side_effect"}}]},
        {"messages": [{"role": "user", "content": "x" * 9000}]},
    ):
        with pytest.raises(ProviderError):
            await guard.complete({**PAYLOAD, **change})
    assert provider.calls == 0 and guard.reserved == 0


async def test_inflight_response_cannot_repopulate_invalidated_cache(settings):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(MockProvider):
        async def complete(self, payload):
            entered.set()
            await release.wait()
            return await super().complete(payload)

    provider = WaitingProvider()
    app = create_app(settings, providers={"mock": provider})
    principal = Principal("demo", ("North", "South"), True)
    async with app.router.lifespan_context(app):
        engine = app.state.engine
        base = engine.store.baseline(principal, "sales_report")
        plan = engine.store.create(
            principal,
            Candidate(
                parent_version=base["id"],
                description="In-flight invalidation",
                config=PlanConfig(workflow="sales_report", adapter_mode="aggregate", cache={"enabled": True}),
            ),
        )
        request = ReportRequest(start="2026-09-01", end="2026-10-01", plan_version=plan["id"], cache_approved=True)
        first = asyncio.create_task(engine.report(principal, request, "inflight"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        engine.store.invalidate(principal)
        release.set()
        await first
        second = await engine.report(principal, request, "after-invalidation")
        assert second["cache_status"] == "miss" and provider.calls == 2
        third = await engine.report(principal, request, "cache-current-generation")
        assert third["cache_status"] == "hit" and provider.calls == 2


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), -1, True])
def test_nonfinite_or_invalid_rate_cards_fail_at_startup(tmp_path, rate):
    with pytest.raises(ValueError, match="Rate cards"):
        Settings(data_dir=tmp_path, http_models=("m",), http_pricing={"m": {"version": "v1", "input": rate}})


def test_empty_credentials_and_ambiguous_permissions_rejected(tmp_path):
    with pytest.raises(ValueError, match="API keys"):
        Settings(data_dir=tmp_path, dev_mode=False, keys={"": Principal("demo", ("North",), True)})
    with pytest.raises(ValueError, match="boolean"):
        Principal("demo", ("North",), "false")


def test_plain_http_requires_explicit_loopback_opt_in(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(
            data_dir=tmp_path,
            http_url="http://127.0.0.1:11434/v1/chat/completions",
            http_key="local",
            http_models=("qwen2.5:1.5b",),
        )
    allowed = Settings(
        data_dir=tmp_path,
        allow_loopback_http=True,
        http_url="http://127.0.0.1:11434/v1/chat/completions",
        http_key="local",
        http_models=("qwen2.5:1.5b",),
    )
    assert allowed.allow_loopback_http
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(
            data_dir=tmp_path,
            allow_loopback_http=True,
            http_url="http://remote.example/v1/chat/completions",
            http_key="local",
            http_models=("m",),
        )
