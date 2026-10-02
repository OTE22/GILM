"""Discover, configure, and probe free OpenRouter models without exporting credentials."""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from gilm.app import create_app
from gilm.config import Settings

API = "https://openrouter.ai/api/v1"
DEFAULT_MODELS = ["liquid/lfm-2.5-2.6b:free", "google/gemma-4-31b-it:free", "apodex/apodex-1.1-mini:free"]


def is_free(model):
    pricing = model.get("pricing", {})
    try:
        return (
            model.get("id", "").endswith(":free")
            and "prompt" in pricing
            and "completion" in pricing
            and all(Decimal(str(value)) == 0 for name, value in pricing.items() if name != "discount")
        )
    except (InvalidOperation, TypeError, ValueError):
        return False


def write_artifact(name, value):
    path = Path("artifacts") / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def settings_for(models, key):
    primary = models[0]["id"]
    return Settings(
        data_dir=Path("data/openrouter-" + hashlib.sha256(primary.encode()).hexdigest()[:12]),
        default_provider="http",
        http_url=API + "/chat/completions",
        http_key=key,
        http_models=tuple(model["id"] for model in models),
        openrouter_free_only=True,
        http_min_interval_seconds=3.2,
        timeout_seconds=120,
        http_pricing={
            model["id"]: {
                "input": 0,
                "output": 0,
                "cached_input": 0,
                "version": "openrouter-catalog-" + datetime.now(UTC).date().isoformat(),
                "input_token_ceiling": model["context_length"],
            }
            for model in models
        },
    )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["discover", "probe", "configure"])
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    args = parser.parse_args()
    async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
        response = await client.get(API + "/models")
        response.raise_for_status()
        catalog = [model for model in response.json()["data"] if is_free(model)]
        write_artifact("openrouter-free-models.json", catalog)
        by_id = {model["id"]: model for model in catalog}
        if args.command == "discover":
            print(json.dumps({"free_model_count": len(catalog), "models": sorted(by_id)}, indent=2))
            return
        if not 1 <= len(args.models) <= 5 or len(set(args.models)) != len(args.models):
            raise SystemExit("Choose one to five distinct free model IDs")
        if any(model not in by_id for model in args.models):
            raise SystemExit("Every selected model must currently be listed with zero pricing and a :free ID")
        models = [by_id[model] for model in args.models]
        key = os.getenv("GILM_HTTP_KEY")
        if not key:
            raise SystemExit("Supply GILM_HTTP_KEY using the ignored .env or process environment")
        auth = await client.get(API + "/key", headers={"Authorization": "Bearer " + key})
        if auth.status_code != 200:
            raise SystemExit(f"OpenRouter authentication failed: HTTP {auth.status_code}")
        settings = settings_for(models, key)
        if args.command == "configure":
            values = {
                "GILM_DEV_MODE": "true",
                "GILM_HOST": "127.0.0.1",
                "GILM_DATA_DIR": settings.data_dir.relative_to(Path.cwd()).as_posix(),
                "GILM_DEFAULT_PROVIDER": "http",
                "GILM_HTTP_URL": settings.http_url,
                "GILM_HTTP_MODELS": ",".join(settings.http_models),
                "GILM_OPENROUTER_FREE_ONLY": "true",
                "GILM_HTTP_MIN_INTERVAL_SECONDS": "3.2",
                "GILM_ALLOW_LOOPBACK_HTTP": "false",
                "GILM_TIMEOUT_SECONDS": "120",
                "GILM_API_KEYS": "{}",
                "GILM_HTTP_PRICING": "'" + json.dumps(settings.http_pricing) + "'",
            }
            # The secret stays in .env; this generated file contains no key.
            Path("examples/openrouter.env").write_text(
                "# Load together with the ignored .env containing GILM_HTTP_KEY.\n"
                + "\n".join(f"{name}={value}" for name, value in values.items())
                + "\n",
                encoding="utf-8",
            )
            print(json.dumps({"configured_models": settings.http_models, "free_only": True, "secret_exported": False}))
            return
        results = []
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)), base_url="http://127.0.0.1"
            ) as proxy,
        ):
            for model in models:
                started = time.perf_counter()
                result = await proxy.post(
                    "/v1/chat/completions",
                    json={
                        "model": model["id"],
                        "messages": [{"role": "user", "content": 'Return only JSON with key "sum" for 19 + 23.'}],
                        "max_tokens": 512,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                    },
                )
                body = result.json()
                correct = False
                if result.status_code == 200:
                    with contextlib.suppress(ValueError, KeyError, TypeError):
                        correct = json.loads(body["choices"][0]["message"]["content"]) == {"sum": 42}
                row = {
                    "requested_model": model["id"],
                    "http_status": result.status_code,
                    "success": correct,
                    "returned_model": body.get("model"),
                    "provider": body.get("provider"),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "provider_reported_cost_usd": body.get("usage", {}).get("cost"),
                    "prompt_tokens": body.get("usage", {}).get("prompt_tokens"),
                    "completion_tokens": body.get("usage", {}).get("completion_tokens"),
                    "error_code": body.get("error", {}).get("code"),
                    "finish_reason": body.get("choices", [{}])[0].get("finish_reason"),
                }
                results.append(row)
                write_artifact("openrouter-probes.json", results)
                print(json.dumps(row), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
