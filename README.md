# GILM

A runnable local MVP of a provider-neutral LLM proxy and a separate upstream reporting adapter. Complete execution plans are immutable, evaluated offline, explicitly activated, and reversible. The default provider and dataset are synthetic. No credentials or external model calls are needed for the demo.

**Dependency management uses uv throughout:** `pyproject.toml`, the generated `uv.lock`, and Python 3.13 in `.python-version`. Follow the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) if uv is not installed. Dependencies and environments follow [uv's project workflow](https://docs.astral.sh/uv/guides/projects/).

## Run locally

From this directory, on PowerShell, macOS, or Linux:

```console
uv sync --locked
uv run --locked pytest -q
uv run --locked python -m gilm demo
uv run --locked python -m gilm serve
```

Open **http://127.0.0.1:8000** for the dashboard and **http://127.0.0.1:8000/docs** for the documented API subset. The dashboard reads actual stored plans, evaluations, and traces. Startup initializes the versioned SQLite schemas and synthetic dataset in `data/`.

The demo runs an explicit baseline, creates a combined aggregation/deduplication/cache plan, compares it with the baseline and a manually optimized control over development and held-out fixtures, activates it, demonstrates a warm cache hit, and rolls back to the baseline. The artifacts are `artifacts/demo.json` and `artifacts/demo.jsonl`. Demo activation is an explicit CLI action; production requests never activate plans.

September 2026 fixture answers are **North: 25,100 cents ($251.00)** and **South: 30,000 cents ($300.00)**. August held-out answers are **North: 10,000 cents** and **South: 20,000 cents**. The range is inclusive at the start and exclusive at the end. The demo's “last month” is fixed to September 2026 for reproducibility, rather than depending on the host clock.

```console
uv run --locked python -m gilm benchmark --output artifacts/benchmark.json
uv run --locked ruff check .
uv run --locked ruff format --check .
```

JSONL exports contain one row per fixture, variant, and cold/warm condition, with correctness checks, success, cache status, latency, usage provenance, and estimated cost. They form an export interface for later external comparisons. No competitor results are supplied. The `benchmark` command is mock-only. The separate `benchmark-live` command requires explicit authorization and a rate-card spending guard; see [live evaluation](docs/live-evaluation.md).

For **real OpenRouter inference using free models**, the configured provider for this project:

```console
uv run --env-file .env --locked python -m scripts.openrouter discover
uv run --env-file .env --locked python -m scripts.openrouter probe
uv run --env-file .env --env-file examples/openrouter.env --locked python -m gilm serve
```

The private, ignored `.env` contains `GILM_HTTP_KEY`; `examples/openrouter.env` contains only nonsecret provider configuration. The free-only guard requires explicit `:free` model IDs, sets maximum upstream token/request/image prices to zero, and disables provider fallback. Catalog availability does not guarantee endpoint availability or feature support. See [OpenRouter setup and actual results](docs/openrouter.md). Use `examples/chat-openrouter.json` for chat and `examples/sales.json` for reporting.

For **real local model inference** using an already-installed `qwen2.5:1.5b` in Ollama:

```console
uv run --env-file examples/ollama.env --locked python -m gilm benchmark-live --allow-paid --max-estimated-usd 0.01 --max-provider-attempts 24 --structured-report --output artifacts/ollama-qwen-structured.json
uv run --env-file examples/ollama.env --locked python -m gilm serve
```

This configuration points only to local Ollama, does not download a model, and keeps its database separate from mock mode. The rates are zero **provider API fees**; electricity, hardware time, and local compute cost are not estimated. Actual model output is scored against the fixtures and may fail. Failed candidates remain inactive. The flag authorizes the guarded HTTP evaluation path; it does not route this local configuration to a paid service. [Ollama documents this completion endpoint and its ignored placeholder API key](https://docs.ollama.com/api/openai-compatibility#local-server-usage).

The structured-report candidate uses a fixed JSON output schema and temperature zero alongside aggregation, explicit deduplication, and caching. Its recorded local Qwen run passed all eight candidate checks; the earlier unconstrained candidate failed and remains recorded. Baseline and manual-control failures are retained. See the [verification record](docs/verification.md) for actual results and limitations. Evaluation never activates a plan automatically.

## Example requests

In PowerShell, with the server running:

`examples/chat.json` uses the default mock model. For the local Ollama server, use `examples/chat-ollama.json` instead. The sales request works in either mode and follows the active reporting plan.

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/chat/completions -Method Post -ContentType application/json -InFile examples/chat.json
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/reports/sales -Method Post -ContentType application/json -InFile examples/sales.json
```

For curl (use `curl.exe` on Windows PowerShell):

```console
curl -H "Content-Type: application/json" --data-binary @examples/chat.json http://127.0.0.1:8000/v1/chat/completions
curl -H "Content-Type: application/json" --data-binary @examples/sales.json http://127.0.0.1:8000/api/reports/sales
```

Proxy mode handles already assembled messages. Ordinary messages are opaque; only explicitly annotated redundant blocks may be removed. Adapter mode calls approved SQL before assembling context. The proxy cannot recover the cost of upstream work already performed.

## Plan lifecycle

1. `GET /api/plans?workflow=sales_report` returns the immutable baseline and candidate versions.
2. `GET /api/source` returns the authorized synthetic source snapshot.
3. `POST /api/plans` accepts `parent_version`, `description`, a complete `config`, optional `source_versions`, and an optional timezone-aware `expires_at`. Start from an existing plan's `config`. Set `adapter_mode` to `aggregate`, `remove_redundant` to `true`, and `cache` to `{"enabled":true,"ttl_seconds":60}` for the demo candidate. Pin `source_versions` to `{"sales":"the-returned-version"}`.
4. Inspect `GET /api/plans/{id}/diff`; run `POST /api/plans/{id}/evaluate`; inspect `GET /api/plans/{id}/evaluations`.
5. `POST /api/plans/{id}/activate` requires a passing evaluation against the current snapshot. `POST /api/plans/{ancestor-id}/rollback` explicitly returns to an ancestor, including the unchanged baseline.

An explicit `plan_version` on a report or `gilm.plan_version` on a chat previews a candidate within the caller's own scope. A failed applicability/integrity/expiry/context check falls back before dispatch. Authentication and authorization failures return an error. There are no automatic retries after provider dispatch, including ambiguous failures and partial SSE streams. GILM never executes tools.

## Configuration and authentication

See `.env.example`. The CLI reads environment variables; it does **not** automatically load `.env`. PowerShell example:

```powershell
$env:GILM_DEV_MODE = 'false'
$env:GILM_HOST = '127.0.0.1'
# Generate and securely store your own credential; this command is an example shape.
$env:GILM_API_KEYS = '{"replace-with-a-random-secret":{"tenant":"demo","branches":["North","South"],"can_manage":true}}'
uv run --locked python -m gilm serve
```

Use `Authorization: Bearer <your-key>` on API requests or enter the key in the dashboard (held only in page memory). `X-Tenant-ID` has no authority. Tokens with `can_manage=false` can request reports and inspect their own scope, but cannot create/evaluate/activate plans or invalidate caches. Scopes include the complete authenticated branch permission set. Development mode permits unauthenticated loopback clients with a loopback Host, enforces a loopback bind, and rejects cross-origin requests. Bind outside loopback only with development mode disabled and configured credentials.

The default baseline is created once per tenant/scope/workflow and is immutable. To use an external provider, explicitly configure `GILM_HTTP_URL` (the full HTTPS completion endpoint), `GILM_HTTP_KEY`, `GILM_HTTP_MODELS`, and `GILM_DEFAULT_PROVIDER=http` with a **fresh data directory**. Existing baselines retain their original provider. Send an allowlisted model. This enables ordinary HTTP proxy traffic and can incur provider charges. Live benchmarks require additional explicit authorization and a complete rate card. TLS verification remains enabled; redirects and ambient HTTP proxies are disabled. `GILM_HTTP_CA_FILE` can select a trusted private CA. Plain HTTP is permitted only with explicit `GILM_ALLOW_LOOPBACK_HTTP=true` and an exact loopback hostname, as used by the local Ollama example.

`GILM_HTTP_PRICING` accepts an operator-supplied versioned USD-per-million rate card. It is not fetched or assumed. Missing usage or prices remain unknown. When cached-input usage is unavailable, an estimate at full input rates is flagged as an upper bound. Reasoning tokens already counted in completion usage are not added again. Invoice-reconciled charges remain null. Mock counts are explicitly character estimates, and mock rates are hypothetical. Local compute cost stays separate and unknown. Optional `GILM_EVALUATION_AMORTIZATION_TASKS` allocates the latest complete evaluation's rate-card estimate across that many tasks; without that assumption, lifecycle cost is unknown.

## Storage and retention

`data/state.sqlite` stores immutable plans, activation history, evaluations, metadata-only traces, and opt-in exact responses. `data/reporting.sqlite` holds synthetic records. HTTP prompts, provider credentials, and raw provider errors are not stored in traces. Identifiers and source hashes are metadata and should not contain secrets. The response cache necessarily stores the approved response; it is enabled only for the synthetic reporting workflow with explicit request consent, never ordinary chat or tool conversations. Keys include tenant/scope, complete provider payload, provider/model, generation settings, plan/config identity, source versions, and context hashes.

Cache TTL is 1–3,600 seconds; expiry is checked on every lookup. `POST /api/cache/invalidate` clears only the caller's scope and increments its persistent cache generation, so an in-flight response cannot repopulate an invalidated generation. Activation and rollback invalidate that scope too. Source changes produce new identities. Traces expire after seven days by default; pruning runs at startup, trace reads, and `uv run --locked python -m gilm prune`. Schedule the prune command for unattended use. Plans and evaluation summaries are retained for traceability; there is no automatic plan deletion. SQLite files and cached responses require OS filesystem protection; encryption at rest is not implemented.

## Docker

The image uses uv and the locked runtime dependencies, runs as UID 10001, and requires authentication. It binds inside the container to `0.0.0.0`, while Compose publishes only host loopback. Set `GILM_API_KEYS` to a real JSON credential map, then:

```console
docker compose up --build
```

The Compose data volume persists both databases. No external provider is configured in Compose. Container deployment and paid provider calls are not part of the local demo.

To reproduce the real process and container checks:

```console
uv run --locked python scripts/smoke.py
docker build --tag gilm:local .
uv run --locked python scripts/docker_smoke.py
```

The Docker script creates and removes only its own uniquely named test container and volume. It checks authentication, plan evaluation/activation/rollback, SSE, a restart with persisted state, UID 10001, and the read-only container root. Network tests in `tests/test_live_network.py` use separate real HTTP and HTTPS servers with certificate verification enabled. Their upstream answers are explicitly test fixtures; the Ollama run above is the actual local-model evaluation.

## Design and limits

See [architecture](docs/architecture.md), [threat model](docs/threat-model.md), [API contract](docs/api-limitations.md), and [verification record](docs/verification.md). Implemented: deterministic duplicate removal, approved projection/aggregation, versioned plans, tenant-scoped exact cache, mock and configurable HTTP adapters, SSE forwarding, metadata accounting, fixture evaluations, and the dashboard.

Deferred: semantic compression, automatic routing, arbitrary SQL, similarity caching, executing tools, coding/document adapters, invoice ingestion, distributed coordination, production abuse controls, and proof of general prompt equivalence. Fixture reductions do not establish real billing savings, novelty, or production readiness.
