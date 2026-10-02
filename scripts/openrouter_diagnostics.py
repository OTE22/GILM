"""Bounded synthetic compatibility checks for the configured free OpenRouter model."""

import asyncio
import json
from pathlib import Path

import httpx

from gilm.config import Settings
from gilm.report_contract import REPORT_RESPONSE_FORMAT


async def main():
    settings = Settings.from_env()
    if not settings.openrouter_free_only:
        raise SystemExit("This diagnostic requires explicit OpenRouter free-only configuration")
    cases = [
        (
            "reasoning_disabled_json_object",
            {"reasoning": {"enabled": False}, "response_format": {"type": "json_object"}},
        ),
        ("report_schema", {"response_format": REPORT_RESPONSE_FORMAT}),
    ]
    rows = []
    async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as client:
        for name, extra in cases:
            result = await client.post(
                settings.http_url,
                headers={"Authorization": "Bearer " + settings.http_key},
                json={
                    "model": settings.http_models[0],
                    "messages": [{"role": "user", "content": 'Return JSON with key "sum" for 19 + 23.'}],
                    "max_tokens": 128,
                    "provider": {
                        "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
                        "allow_fallbacks": False,
                        "require_parameters": True,
                    },
                    **extra,
                },
            )
            body = result.json()
            error = body.get("error", {})
            # Only this synthetic diagnostic records the bounded error explanation.
            message = str(error.get("message", "")).replace(settings.http_key, "[redacted]")[:500]
            raw = error.get("metadata", {}).get("raw", "")
            raw = str(raw).replace(settings.http_key, "[redacted]")[:500]
            row = {"case": name, "status": result.status_code, "message": message, "diagnostic": raw}
            rows.append(row)
            print(json.dumps(row), flush=True)
            await asyncio.sleep(3.2)
    Path("artifacts/openrouter-diagnostics.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
