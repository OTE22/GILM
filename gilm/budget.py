"""Conservative rate-card reservations for explicitly authorized live evaluations."""

import asyncio
from decimal import Decimal

from .accounting import account
from .models import canonical
from .providers import ProviderError


class BudgetedProvider:
    def __init__(self, provider, pricing, authorization):
        self.provider = provider
        self.pricing = pricing
        self.authorization = authorization
        self.reserved = Decimal(0)
        self.attempts = 0
        self.reported_estimate = Decimal(0)
        self.unknown_usage = False
        self.aborted_reason = None
        self.lock = asyncio.Lock()

    def validate_model(self, model):
        rates = self.pricing.get(model, {})
        if not all(key in rates for key in ("input", "output", "version", "input_token_ceiling")):
            raise ProviderError("evaluation_rate_card_incomplete", 409)
        return rates

    async def complete(self, payload):
        async with self.lock:
            if self.aborted_reason:
                raise ProviderError(self.aborted_reason, 409)
            rates = self.validate_model(payload["model"])
            output_limit = payload.get("max_tokens")
            if (
                payload.get("stream")
                or payload.get("tools")
                or not output_limit
                or output_limit > self.authorization.max_output_tokens
            ):
                raise ProviderError("evaluation_requires_bounded_text_generation", 409)
            # The operator supplies a provider-enforced input ceiling. Byte size plus
            # message framing is an extra conservative preflight check, not a tokenizer.
            preflight_units = len(canonical(payload).encode()) + 128 * len(payload["messages"])
            if preflight_units > rates["input_token_ceiling"]:
                raise ProviderError("evaluation_input_ceiling_exceeded", 409)
            worst_input_rate = max(rates["input"], rates.get("cached_input", 0))
            reservation = (
                Decimal(rates["input_token_ceiling"]) * Decimal(str(worst_input_rate))
                + Decimal(output_limit) * Decimal(str(rates["output"]))
            ) / Decimal(1_000_000)
            if (
                self.attempts >= self.authorization.max_provider_attempts
                or self.reserved + reservation > self.authorization.max_estimated_usd
            ):
                self.aborted_reason = "evaluation_budget_exhausted"
                raise ProviderError(self.aborted_reason, 409)
            # Reservations are never refunded, even on timeout/cancellation/unknown usage.
            self.reserved += reservation
            self.attempts += 1
        try:
            reply = await self.provider.complete(payload)
        except BaseException:
            self.aborted_reason = "evaluation_provider_failure"
            self.unknown_usage = True
            raise
        accounting = account("http", payload["model"], reply.usage, reply.usage_source, self.pricing)
        cost = accounting["rate_card_estimate_usd"]
        if cost is None:
            self.unknown_usage = True
            self.aborted_reason = "evaluation_usage_unknown"
        else:
            self.reported_estimate += Decimal(str(cost))
        usage = reply.usage or {}
        if (
            usage.get("prompt_tokens", 0) > rates["input_token_ceiling"]
            or usage.get("completion_tokens", 0) > output_limit
        ):
            self.aborted_reason = "evaluation_provider_exceeded_declared_ceiling"
        return reply

    def summary(self):
        return {
            "max_estimated_usd": float(self.authorization.max_estimated_usd),
            "reserved_upper_bound_usd": float(self.reserved),
            "provider_attempts": self.attempts,
            "max_provider_attempts": self.authorization.max_provider_attempts,
            "reported_rate_card_estimate_usd": None if self.unknown_usage else float(self.reported_estimate),
            "aborted_reason": self.aborted_reason,
            "invoice_reconciled_usd": None,
            "assumptions": "Configured rates and provider-enforced token ceilings; excludes undisclosed provider fees. Use a provider-side hard cap for an invoice-level spending guarantee.",
        }
