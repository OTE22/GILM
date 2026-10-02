# Implementation and verification record

Verified locally on 2026-10-02, using Windows, Python 3.13.1, uv 0.11.19, Docker Engine 29.0.1, and the already installed Ollama `qwen2.5:1.5b` model. The Docker image uses Python 3.13-slim and uv 0.11.19. All Python dependency installation and execution use uv and the generated `uv.lock`.

## Delivered files

| Area | Files and behavior |
| --- | --- |
| Dependency/runtime configuration | `pyproject.toml`, `uv.lock`, `.python-version`, `.env.example`, `.gitignore` |
| API and validation | `gilm/app.py`, `config.py`, `models.py`: authenticated scoped API, bounded requests, structured errors, cancellation |
| Execution | `engine.py`, `context.py`, `providers.py`, `report_contract.py`: validated transformations, real HTTP/SSE adapters, reporting output grammar |
| State and SQL | `store.py`, `adapter.py`, `migrations/*.sql`, `fixtures/*.json`: immutable plans, scoped state/cache, approved read-only reporting queries |
| Measurement | `accounting.py`, `budget.py`, `evaluation.py`: usage provenance, rate-card accounting, guarded live evaluation, JSON/JSONL export |
| CLI and UI | `__main__.py`, `static/*`: demo/benchmark/server/prune commands and dashboard using stored data |
| Verification | `tests/*.py`, `scripts/smoke.py`, `scripts/docker_smoke.py`, `scripts/real_smoke.py` |
| Packaging and examples | `Dockerfile`, `.dockerignore`, `compose.yaml`, `examples/*` |
| Documentation | `README.md`, `docs/architecture.md`, `docs/threat-model.md`, `docs/api-limitations.md`, `docs/live-evaluation.md`, this record |

The project began without an application. Generated databases, virtual environments, and artifacts are ignored by Git; the artifacts referenced below exist in this verified working directory and are reproducible with the commands below.

## Commands and actual results

```console
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python scripts/smoke.py
uv run --locked python -m gilm benchmark --output artifacts/benchmark.json
docker compose --env-file .env.example config --quiet
docker build --tag gilm:local .
uv run --locked python scripts/docker_smoke.py
```

- Latest full pytest run: **84 passed in 84.64 seconds**, with no warnings.
- Locked dependency sync, Ruff lint/format checks, and Compose configuration validation passed.
- Clean-data smoke: demo, actual loopback HTTP report, SSE, dashboard assets, and known September totals passed. The helper creates a temporary database and a separate real server process. Its demo artifact is [clean-demo.json](../artifacts/clean-demo.json).
- Mock benchmark: **24/24 fixture executions passed**, with zero critical failures; [JSON](../artifacts/benchmark.json) and [JSONL](../artifacts/benchmark.jsonl).
- Docker image built successfully. Its disposable authenticated container passed 24 fixture executions, activation, cache hits, SSE, persistent restart, and rollback. UID was **10001** and root filesystem was **read-only**. The helper removed its own container and volume. See [container result](../artifacts/docker-smoke.json) and [server log](../artifacts/docker-smoke.log).

Tests cover validation, protected/opaque context, dependencies, malicious retrieved text, permission/tenant isolation, source changes, expiry, rollback, SQL restrictions, accounting, pre-dispatch fallback, cancellation, and no unsafe replay. Dedicated concurrency tests cover invalidation while a provider response is in flight. Usage telemetry tests confirm that provider debug strings cannot enter metadata traces.

`tests/test_live_network.py` runs actual HTTP and HTTPS servers with a private test CA and certificate verification enabled. It checks the full evaluation lifecycle, invalid certificates, provider failures, budget/unknown-usage aborts, incremental SSE delivery while the upstream is blocked, real partial transport failures, and disconnect cleanup. Its upstream responses are test fixtures. Actual model inference was verified separately below.

## Actual local model runs

Both commands below were executed, in order, against local Ollama:

```console
uv run --env-file examples/ollama.env --locked python -m gilm benchmark-live --allow-paid --max-estimated-usd 0.01 --max-provider-attempts 24 --output artifacts/ollama-qwen.json
uv run --env-file examples/ollama.env --locked python -m gilm benchmark-live --allow-paid --max-estimated-usd 0.01 --max-provider-attempts 24 --structured-report --output artifacts/ollama-qwen-structured.json
```

The first plan combined aggregation, explicit duplicate removal, and caching. It **failed** its gate and was never activated. The second created a different immutable plan, adding a fixed JSON output schema and temperature zero. It **passed** its candidate gate. Correctness requirements were unchanged, and failures in reference variants remain visible.

| Actual run | Baseline correct | Manual control correct | Candidate correct | Fixture executions | Actual model calls |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original unconstrained | 0/8 | 4/8 | 3/8 | 24 | 22 |
| Structured candidate | 0/8 | 5/8 | **8/8** | 24 | 18 |

Each variant includes four cold and four warm trials. The structured candidate had zero critical failures, four correct cold responses, and four correct warm cache hits. The overall second run was **13/24 correct**, not 24/24; activation evaluates the candidate's complete results independently of reference failures.

Structured-run measurements:

| Variant | Provider-reported input tokens, four cold trials | Mean cold latency | Mean warm latency |
| --- | ---: | ---: | ---: |
| Baseline | 2,297 | 30,773.84 ms | 10,481.27 ms |
| Manual aggregate control | 1,448 | 17,155.36 ms | 6,889.07 ms |
| Structured candidate | 1,316 | 22,012.79 ms | 10.89 ms |

These are observed regression-fixture measurements on a shared CPU machine, not a controlled performance comparison. The variants differ in correctness and generation configuration. Cold refers to GILM's response cache, not Ollama's model residency or provider prompt caching. The same held-out regression cases were reused between runs; this is not a fresh unseen generalization evaluation. No universal prompt-equivalence or billed-savings claim follows from these measurements.

The local rate card records zero provider API fees. **Hardware time, electricity, and local compute cost remain unknown; invoice-reconciled charges remain null.** No paid/cloud API calls were made and no model was downloaded. The existing Qwen 1.5B Q4_K_M model ran on CPU. The schema supplies structure, units, and definition, but neither the expected totals nor the actual source hash.

Evidence: [failed run](../artifacts/ollama-qwen.json), [passing candidate run](../artifacts/ollama-qwen-structured.json), and their corresponding `.jsonl` files. All provider attempts and quality failures are retained in the scoped SQLite metadata.

## Running application and real lifecycle

The app was started with:

```console
uv run --env-file examples/ollama.env --locked python -m gilm serve --port 8000
```

Dashboard: **http://127.0.0.1:8000**. API schema: **http://127.0.0.1:8000/docs**. It uses `data/ollama-qwen/`, the real local model, and explicit loopback development mode. After restarting, the active plan persists in that directory.

```console
uv run --locked python scripts/real_smoke.py --evaluation artifacts/ollama-qwen-structured.json
```

This command passed actual activation, a cold model report, a warm repeat with **zero provider attempts**, rollback, reactivation, and real model SSE forwarding (10 received transport chunks). It left passing plan `p_f086e44c51094b77b3342ede61b12bf8` active. See [real lifecycle result](../artifacts/real-lifecycle.json).

September answer: North **25,100 cents ($251)**, South **30,000 cents ($300)**. August fixture answer: North **10,000 cents**, South **20,000 cents**. These are synthetic gross booked sales in USD with an exclusive end date, not real company financial data.

The dashboard shell, JavaScript asset, CSP, and data endpoints passed HTTP checks. Visual browser verification was unavailable because no browser was connected to the session; no screenshot or visual-inspection claim is made.

## Remaining limitations

This is the implemented local MVP, with real provider transport and actual local inference. It has no unrestricted semantic compression, similarity cache, tool executor, automatic model routing, arbitrary SQL, coding/document adapter, invoice reconciliation, or competitor benchmark. SQLite work is bounded but synchronous; large-scale concurrency, migration locking, body-upload deadlines, distributed admission control, production load testing, and encryption at rest remain outside scope. The dashboard is an inspection interface; management actions use the API.

Remote-provider compatibility and production operations need validation against the intended provider and deployment. JSON Schema support varies by provider. The small local model's failed baseline/manual cases show that a transport integration alone does not ensure correct answers. Reporting checks enforce this fixture contract; they do not prove safety or correctness for arbitrary chat. See the [API boundaries](api-limitations.md), [architecture](architecture.md), and [threat model](threat-model.md).
