# Real-provider evaluation

The ordinary `benchmark` command is deterministic mock mode. `benchmark-live` and `/api/plans/{id}/evaluate-live` execute real HTTP provider calls. They never activate a plan automatically. The bundled fixture dataset remains synthetic in both modes.

## Free OpenRouter inference

With `GILM_OPENROUTER_FREE_ONLY=true`, use `--allow-free`. This sets the rate-card budget to exactly zero and rejects a nonzero supplied budget. OpenRouter routing additionally enforces zero maximum prices and no fallback. The rate card comes from a live catalog check. See [OpenRouter setup](openrouter.md).

```console
uv run --env-file .env --env-file examples/openrouter.env --locked python -m gilm benchmark-live --allow-free --max-provider-attempts 24 --structured-report --report-format json_object --disable-reasoning --output artifacts/openrouter-apodex-json.json
```

`--report-format json_object` requests JSON-object output instead of the default fixed JSON schema. GILM still checks the full answer contract. `--disable-reasoning` adds `reasoning.enabled=false` to the new immutable candidate; use it only for models that support disabling reasoning. Baselines remain unchanged. OpenRouter's catalog can overstate individual endpoints' capabilities, so the live result is authoritative for the tested request. Reasoning can consume the entire output cap without a visible answer; that is a quality failure, not success.

The HTTP evaluation API retains its existing authorization field `allow_paid:true`; for a free-only server send `max_estimated_usd:0`. The CLI's `--allow-free` supplies this legacy dispatch-authorization field internally without authorizing paid routing. Zero budgets reject any positive per-call rate-card reservation before dispatch.

## Local inference

`examples/ollama.env` points to the installed local `qwen2.5:1.5b` model. Run:

```console
uv run --env-file examples/ollama.env --locked python -m gilm benchmark-live --allow-paid --max-estimated-usd 0.01 --max-provider-attempts 24 --output artifacts/ollama-qwen.json
```

The CLI does not install models, access a cloud Ollama model, or change the user's model files. The named model must already be installed and the local service available. Plain HTTP is an explicit loopback-only exception; external endpoints still require HTTPS. Ollama ignores the example's nonsecret placeholder key. The rate card represents zero local provider API fees, not zero hardware/electricity cost. Local compute cost remains unknown and separate.

For the optional structured-output candidate, add `--structured-report` and use a separate output path such as `artifacts/ollama-qwen-structured.json`. This creates a new immutable version with temperature zero and the fixed schema in `gilm/report_contract.py`, in addition to aggregation, deduplication, and exact caching. The schema specifies field names, types, date/hash shapes, currency, and the reporting definition. It supplies neither branch totals nor the actual source hash. The provider must support `response_format.json_schema`. Correctness checks remain unchanged. This flag cannot be combined with `--plan-version`, which evaluates an existing immutable version.

With the server running from `examples/ollama.env`, the following explicitly activates the evaluated Qwen plan, makes real local requests, verifies cache behavior and SSE, rolls back, and reactivates the passing candidate:

```console
uv run --locked python scripts/real_smoke.py --evaluation artifacts/ollama-qwen-structured.json
```

This helper targets the local development server at port 8000 and the local Qwen configuration. It leaves the passing plan active. It is separate from the evaluator so that evaluation itself never activates a plan.

## Remote provider

Set `GILM_DEFAULT_PROVIDER=http`, a full `GILM_HTTP_URL`, `GILM_HTTP_KEY`, `GILM_HTTP_MODELS`, a fresh data directory, and `GILM_HTTP_PRICING`. Each evaluated model's rate card needs nonnegative USD-per-million `input` and `output` rates, a `version`, and an integer `input_token_ceiling` backed by the provider's enforced maximum. Supply `cached_input` if discounted cached usage can be reported. Missing prices remain unknown; do not copy hypothetical example rates into a paid evaluation.

Fresh reporting baselines explicitly cap `max_tokens` at 512. A candidate may override generation settings, but both baseline and candidate must stay within the run's authorized `--max-output-tokens`. Existing immutable baselines are not modified when defaults change. In authenticated mode, `GILM_BENCHMARK_KEY` must match a configured management credential. The API uses its normal bearer authentication and management permission instead.

```console
uv run --env-file .env --locked python -m gilm benchmark-live --allow-paid --max-estimated-usd 1.00 --max-provider-attempts 16 --output artifacts/live-benchmark.json
```

The dollar value above is an example authorization, not a provider-price claim. Optional `--plan-version` evaluates an existing candidate. Otherwise the CLI creates a candidate that combines the approved aggregate, explicit duplicate removal, and exact cache. Passing results can later be explicitly activated through the management API. A failed/aborted run exports its results and exits with status 2.

## What the limit means

Before each provider call, GILM reserves:

`(input_token_ceiling × max(input_rate, cached_input_rate) + max_tokens × output_rate) / 1,000,000`.

It rejects a call if adding the reservation would exceed the authorized rate-card budget or attempt count. It also checks serialized request byte size plus conservative message framing against the declared input ceiling. That check is not a provider tokenizer. Reservations are not refunded, even when observed usage is smaller or a request fails. Provider-reported usage exceeding the declared ceilings stops further calls. Unknown usage, timeout, cancellation, or provider failure also stops further calls; no automatic retry occurs.

This guards estimated spending under the configured rates and provider-enforced limits. It cannot guarantee invoices when an operator supplies wrong prices, a provider violates its limits, or undisclosed fees apply. Use an actual provider-side hard spending cap when an invoice-level maximum is required. Reported rate-card estimates, reserved upper bounds, invoice charges, and local compute are separate fields.

## Evaluation semantics

Four known-answer cases are split into development and held-out sets. Each runs baseline, manual aggregate control, and candidate under cold and warm GILM-cache conditions. The normal successful path is 24 fixture executions and 16 provider calls because only the two optimized variants cache their four warm repeats. Quality failures are never cached, so a run may require up to 24 calls; the attempt guard can abort earlier. Cold-cache here means GILM's exact response cache, not the provider's own prompt cache or the local model's RAM residency.

Completed text responses with incorrect facts, formatting, permissions, or provenance are scored as failures. Remaining preplanned fixtures may continue while usage is known and limits permit. Transport failures and unknown usage halt the run. Candidate activation requires complete passing candidate fixtures against the current source snapshot; baseline/manual outcomes remain visible independently. This allows a correct candidate to improve on a weaker baseline without hiding baseline failures.

The held-out fixtures are regression cases distinct from the development cases, not a claim of unseen production representativeness. Repeatedly tuning against them would invalidate a fresh held-out generalization claim. The exported results make no universal equivalence, novelty, competitor, or real billing-savings claim.
