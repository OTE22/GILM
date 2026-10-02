from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

import httpx
from anyio import CancelScope

from .config import Settings
from .models import canonical


class ProviderError(Exception):
    def __init__(self, code="provider_failed", status=502):
        self.code = code
        self.status = status
        super().__init__("Upstream request failed; it was not retried")


@dataclass
class ProviderReply:
    body: dict
    usage: dict | None
    usage_source: str


class Provider(Protocol):
    async def complete(self, payload: dict) -> ProviderReply: ...
    async def open_stream(self, payload: dict) -> AsyncIterator[bytes]: ...


class HTTPStream:
    """Own the opened response even if the consumer never starts iterating."""

    def __init__(self, response):
        self.response = response
        self.iterator = response.aiter_raw()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await anext(self.iterator)
        except StopAsyncIteration:
            await self.aclose()
            raise
        except httpx.HTTPError as exc:
            await self.aclose()
            raise ProviderError("provider_stream_failed") from exc

    async def aclose(self):
        with CancelScope(shield=True):
            await self.response.aclose()


def reject_nonfinite(value):
    raise ValueError("Non-finite values are not valid JSON")


def answer_report(context):
    totals = dict.fromkeys(context["branches"], 0)
    if context["kind"] == "aggregate":
        totals.update(context["totals_cents"])
    else:
        for row in context["records"]:
            totals[row["branch"]] += row["amount_cents"]
    return {
        "sales_by_branch_cents": totals,
        "start": context["start"],
        "end": context["end"],
        "currency": context["currency"],
        "definition": "gross booked sales",
        "source_version": context["source_version"],
    }


class MockProvider:
    """A deterministic fixture interpreter, not a language model or real token meter."""

    def __init__(self, delay=0.0, fail=False):
        self.delay = delay
        self.fail = fail
        self.calls = 0
        self.closed_streams = 0

    async def complete(self, payload):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise ProviderError()
        if payload.get("reasoning") is not None:
            raise ProviderError("mock_reasoning_control_unsupported", 422)
        choice = payload.get("tool_choice")
        if payload.get("tools") and choice != "none":
            raise ProviderError("mock_tool_generation_unsupported", 422)
        response_format = payload.get("response_format", {}).get("type", "text")
        if response_format == "json_schema":
            raise ProviderError("mock_json_schema_generation_unsupported", 422)
        contexts = [
            msg["content"].split("\n", 1)[1]
            for msg in payload["messages"]
            if (msg.get("content") or "").startswith("GILM_REPORT_CONTEXT\n")
        ]
        try:
            content = (
                canonical(answer_report(json.loads(contexts[-1]))) if contexts else "Deterministic GILM mock response."
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise ProviderError("mock_context_invalid", 422) from exc
        if response_format == "json_object" and not contexts:
            content = canonical({"message": content})
        usage = {
            "prompt_tokens": (len(canonical(payload)) + 3) // 4,
            "completion_tokens": (len(content) + 3) // 4,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        body = {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": usage,
        }
        return ProviderReply(body, usage, "mock_character_estimate")

    async def open_stream(self, payload):
        reply = await self.complete(payload)

        async def generate():
            try:
                chunk = {
                    "id": reply.body["id"],
                    "object": "chat.completion.chunk",
                    "created": reply.body["created"],
                    "model": payload["model"],
                    "choices": [{"index": 0, "delta": reply.body["choices"][0]["message"], "finish_reason": None}],
                }
                yield ("data: " + canonical(chunk) + "\n\n").encode()
                await asyncio.sleep(0.01)
                yield b"data: [DONE]\n\n"
            finally:
                self.closed_streams += 1

        return generate()


class HTTPProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.dispatch_lock = asyncio.Lock()
        self.next_dispatch = 0.0

    async def _open(self, payload):
        if not self.settings.http_url or payload["model"] not in self.settings.http_models:
            raise ProviderError("provider_not_configured", 503)
        if self.settings.openrouter_free_only:
            if not payload["model"].endswith(":free"):
                raise ProviderError("paid_model_forbidden", 403)
            # Operator policy cannot be overridden by caller-supplied vendor fields.
            payload = {
                **payload,
                "provider": {
                    "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
            }
        request = self.client.build_request(
            "POST",
            self.settings.http_url,
            json=payload,
            headers={"Authorization": "Bearer " + self.settings.http_key, "Accept-Encoding": "identity"},
            timeout=self.settings.timeout_seconds,
        )
        try:
            async with self.dispatch_lock:
                await asyncio.sleep(max(0, self.next_dispatch - time.monotonic()))
                self.next_dispatch = time.monotonic() + self.settings.http_min_interval_seconds
            response = await self.client.send(request, stream=True, follow_redirects=False)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ProviderError("provider_transport_failed") from exc
        if response.status_code != 200:
            status = response.status_code
            await response.aclose()
            if self.settings.openrouter_free_only:
                raise ProviderError(f"openrouter_http_{status}", 429 if status == 429 else 502)
            raise ProviderError("provider_http_error")
        return response

    async def complete(self, payload):
        response = await self._open(payload)
        try:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > self.settings.max_response_bytes:
                    raise ProviderError("provider_response_too_large")
            try:
                body = json.loads(data, parse_constant=reject_nonfinite)
                # Preserve arbitrary response fields; only validate essential response shape.
                if not isinstance(body, dict) or not isinstance(body.get("choices"), list) or not body["choices"]:
                    raise ValueError("invalid response")
                if not isinstance(body["choices"][0].get("message"), dict):
                    raise ValueError("invalid message")
                usage = body.get("usage")
                if usage is not None and not isinstance(usage, dict):
                    raise ValueError("invalid usage")
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if usage is not None and key in usage and (type(usage[key]) is not int or usage[key] < 0):
                        raise ValueError("invalid token count")
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ProviderError("provider_invalid_response") from exc
            return ProviderReply(body, usage, "provider_reported" if usage else "unknown")
        except httpx.HTTPError as exc:
            raise ProviderError("provider_transport_failed") from exc
        finally:
            with CancelScope(shield=True):
                await response.aclose()

    async def open_stream(self, payload):
        response = await self._open(payload)
        if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
            await response.aclose()
            raise ProviderError("provider_invalid_stream")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise ProviderError("provider_compressed_stream_unsupported")

        return HTTPStream(response)
