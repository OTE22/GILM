# OpenRouter configuration and real results

Verified on **2026-10-02**. GILM is configured to use **OpenRouter**, with `apodex/apodex-1.1-mini:free` served by **Novita** as the primary model. Only explicit free model IDs are allowed. The previously verified Ollama database remains separate.

## Start the configured application

```console
uv sync --locked
uv run --env-file .env --env-file examples/openrouter.env --locked python -m gilm serve --port 8000
```

Open **http://127.0.0.1:8000** for the dashboard or **http://127.0.0.1:8000/docs** for API documentation. The passing reporting plan is active in the current local database. If already running, do not start a second server on the same port.

The supplied credential is saved only in the local `.env` under `GILM_HTTP_KEY`. That file is excluded from Git and Docker's build context. It is not embedded in examples, code, logs, or artifacts. An exact-key scan of the delivered source, examples, documentation, and generated artifacts found no copies. A new checkout needs its own `.env` containing an OpenRouter key. Do not put the OpenRouter key into the dashboard: that field accepts a GILM client credential only when GILM authentication is enabled.

PowerShell requests against the running server:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/chat/completions -Method Post -ContentType application/json -InFile examples/chat-openrouter.json
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/reports/sales -Method Post -ContentType application/json -InFile examples/sales.json
```

## Free model discovery and guard

```console
uv run --locked python -m scripts.openrouter discover
uv run --env-file .env --locked python -m scripts.openrouter probe
uv run --env-file .env --locked python -m scripts.openrouter configure --models apodex/apodex-1.1-mini:free liquid/lfm-2.5-2.6b:free
```

The live [OpenRouter model catalog](https://openrouter.ai/api/v1/models) contained **17 `:free` variants** with zero listed fees. The helper checks every returned pricing component except the discount multiplier; missing token prices, unknown values, and nonzero fees are rejected. Catalog and endpoint evidence is saved in [free models](../artifacts/openrouter-free-models.json) and [provider endpoints](../artifacts/openrouter-endpoints.json).

`GILM_OPENROUTER_FREE_ONLY=true` requires the exact official HTTPS completion endpoint and explicit `:free` IDs. Every outbound request, including streaming, includes:

```json
{
  "provider": {
    "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
    "allow_fallbacks": false,
    "require_parameters": true
  }
}
```

These operator-owned fields cannot be overridden by callers. There is no automatic switch to a paid model or retry after a failed dispatch. OpenRouter documents [maximum prices and provider fallback controls](https://openrouter.ai/docs/guides/routing/provider-selection). The configured 3.2-second minimum interval spaces dispatches within one process; it is not an account-wide or distributed rate limiter. Free endpoints can still return quota, capacity, or availability errors. See [OpenRouter's limits](https://openrouter.ai/docs/api_reference/limits).

## Actual probe results

Each probe went through GILM's API and its real HTTP provider adapter, asking for a JSON arithmetic answer. These are connectivity/format checks, not a broad quality benchmark.

| Model | Actual provider | Result | Reported response cost |
| --- | --- | --- | --- |
| `liquid/lfm-2.5-2.6b:free` | Liquid | HTTP 200; correct JSON answer | $0 |
| `google/gemma-4-31b-it:free` | Google AI Studio listed in endpoint catalog | HTTP 429; rate limited | Unknown |
| `apodex/apodex-1.1-mini:free` | Novita | HTTP 200; correct JSON answer | $0 |

Evidence: [probe results](../artifacts/openrouter-probes.json). The Google request did not produce a successful completion and was not silently retried.

## Actual reporting evaluations

All calls used synthetic sales fixtures and a **zero-dollar rate-card budget**. The first two failed plans remain recorded and inactive.

| Run | Outcome | Executions / provider attempts |
| --- | --- | --- |
| Liquid, JSON schema, default reasoning | 0/24 correct; no activation | 24 / 24 |
| Apodex, JSON schema, reasoning disabled | Aborted on candidate HTTP 400; no activation | 5 / 5 |
| Apodex, JSON object, reasoning disabled | Candidate **8/8 correct**, zero critical failures; gate passed | 24 / 20 |

Liquid's report completions exhausted the 512-token budget on mandatory reasoning without producing valid report content. This is consistent with the provider-reported usage and [OpenRouter's combined reasoning/output budget](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens). Apodex's endpoint rejected `json_schema` despite the catalog advertising structured outputs. A separate bounded synthetic diagnostic confirmed that `json_object` and `reasoning.enabled=false` work. The diagnostic artifact contains only bounded explanations for these synthetic calls; regular request traces continue to omit raw provider errors.

The final candidate uses approved SQL aggregation, removal of explicitly duplicated definitions, temperature zero, JSON-object output, disabled optional reasoning, and an opt-in exact cache. GILM's required checks for structure, numbers, completeness, permissions, dates, currency, definition, and source provenance were unchanged. The baseline and manual control remained immutable/default-reasoning references and both scored **0/8**. The full final run therefore had **8/24 correct**, not 24/24. This evaluates the complete candidate, not the isolated benefit of aggregation or deduplication.

Final run measurements:

| Variant | Cold input tokens, four tasks | Mean cold latency | Mean warm latency | Correct cold / warm |
| --- | ---: | ---: | ---: | --- |
| Baseline | 2,365 | 4,956.81 ms | 3,364.42 ms | 0/4 / 0/4 |
| Manual aggregate control | 1,516 | 3,740.27 ms | 4,092.62 ms | 0/4 / 0/4 |
| Candidate | 1,372 | **2,043.71 ms** | **4.40 ms** | **4/4 / 4/4** |

Warm candidate repeats made zero provider calls. Cold refers to GILM's exact-response cache, not the upstream provider's prompt cache. Latencies include local work and configured dispatch pacing. Reused development/held-out regression fixtures and one run do not establish general model quality, performance guarantees, or paid API savings.

Evidence: [Liquid failure](../artifacts/openrouter-liquid.json), [Apodex schema failure](../artifacts/openrouter-apodex.json), [compatibility diagnostics](../artifacts/openrouter-diagnostics.json), [passing Apodex evaluation](../artifacts/openrouter-apodex-json.json), and matching benchmark `.jsonl` exports.

Reproduce the passing candidate evaluation:

```console
uv run --env-file .env --env-file examples/openrouter.env --locked python -m gilm benchmark-live --allow-free --max-provider-attempts 24 --structured-report --report-format json_object --disable-reasoning --output artifacts/openrouter-apodex-json.json
```

Evaluation never activates a plan automatically. A failed/aborted run exits nonzero. The CLI's `--allow-free` requires free-only configuration and sets the budget to zero; it rejects nonzero budget arguments. Underlying rate-card and observed response costs remain separate from invoice reconciliation, which is not implemented. Failed calls with unknown usage remain unknown.

## Running lifecycle and regression checks

With the server running, this explicit verification activates the passing version, sends real requests, checks cache reuse, rolls back, and reactivates the candidate:

```console
uv run --locked python scripts/real_smoke.py --evaluation artifacts/openrouter-apodex-json.json --model apodex/apodex-1.1-mini:free --output artifacts/openrouter-lifecycle.json
```

Actual result: **passed activation, cold report, warm cache hit, rollback, reactivation, and real SSE**. The cold report made one provider call and reported cost $0; the repeat made zero. September totals were **North $251 and South $300**. Plan `p_cc7b3963e3324bb7b0584b0684670692` was left active. See [lifecycle evidence](../artifacts/openrouter-lifecycle.json).

The full automated suite passed **98 tests in 36.44 seconds**, including free-only restrictions, zero-budget rejection of paid calls, preserved request payloads, reasoning controls, and sanitized rate-limit handling. Ruff passed. The Docker image was rebuilt and its authenticated lifecycle, persistent restart, SSE, non-root UID, and read-only root checks passed. All Python commands use uv.
