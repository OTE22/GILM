# Supported API contract

`POST /v1/chat/completions` supports a documented subset, not full OpenAI compatibility. Top-level and modeled nested unknown fields return 422; no recognized field is silently discarded. GILM's own `gilm` extension is consumed locally and excluded from provider payloads. Supported fields with null values are omitted; non-null supported values are preserved except explicit, applicable candidate model/generation overrides. Schema dictionaries under function `parameters` are faithfully passed through without validating JSON Schema semantics.

| Field | Supported behavior |
| --- | --- |
| `model` | Required model name (including provider prefixes with `/`); HTTP requires an operator allowlist |
| `messages` | 1–128 messages; roles `system`, `developer`, `user`, `assistant`, `tool` |
| `content` | Text string, at most 131,072 characters; null only for assistant tool-call messages |
| `name` | Optional identifier |
| `tool_calls` | Assistant function calls with unique IDs and string arguments; up to 16 |
| `tool_call_id` | Tool results must immediately follow and resolve a pending assistant call; orphan, duplicate, and unresolved results rejected |
| `tools` | Up to 16 function definitions with name, description, parameters, optional strict flag |
| `tool_choice` | `auto`, `none`, `required`, or a named function present in `tools` |
| `parallel_tool_calls` | Boolean, only with tools |
| `temperature` / `top_p` | 0–2 / greater than 0 through 1 |
| `max_tokens` | 1–8,192 |
| `seed` | Optional integer |
| `stop` | One string or up to four strings, each 1–256 characters |
| `response_format` | `text`, `json_object`, or `json_schema`; validated envelope with schema forwarded to the provider |
| `reasoning` | Explicit `{"enabled":true}` or `{"enabled":false}` control for compatible HTTP providers; other reasoning fields rejected |
| `stream` | Boolean; raw SSE forwarding with all optimization/caching bypassed |
| `gilm` | `workflow=chat`, optional scoped `plan_version`, context `blocks`, cache approval and sensitivity metadata |

Images, audio, arrays of content parts, legacy `functions`, `n`, log probabilities, `max_completion_tokens`, streaming usage options, and arbitrary vendor request fields are unsupported. Tool generation is unsupported in the deterministic mock: when supplying `tools`, set `tool_choice=none`. The HTTP adapter forwards tool fields; neither adapter causes GILM to execute tools. Do not expose its API as a promise of universal SDK compatibility.

JSON Schema semantics and grammar enforcement belong to the configured provider. GILM validates the response-format envelope and forwards the schema without pretending to implement a general schema engine. The deterministic mock explicitly rejects `json_schema`; it supports text and JSON-object output. Reporting independently checks actual answers, permissions, dates, units, definition, and provenance before caching or passing evaluation.

The mock also explicitly rejects reasoning controls. OpenRouter free-only mode adds operator-owned routing fields: zero maximum prices, no provider fallback, and required parameter support. These apply to regular and streaming calls. Clients cannot override them. Streaming still bypasses GILM plan transformations and cache. OpenRouter HTTP failures expose sanitized status codes such as `openrouter_http_429`; raw upstream errors do not enter request traces.

Nonstream HTTP response JSON preserves vendor response fields after essential choices/message/usage validation. The deterministic mock returns an OpenAI-shaped text completion with visibly estimated usage in its trace. Response IDs are provider IDs; GILM assigns a distinct `X-Request-ID`. A cache hit returns the stored completion (including original provider ID), has a new GILM request ID, and records zero provider attempts and no new provider usage. Billing fields are in traces rather than injected into provider response JSON.

Errors use `{"error":{"code":...,"message":...,"request_id":...,"details":...}}`. Validation details contain field locations/types, never input payloads. Before streaming begins, provider failures return structured errors. After headers/bytes are sent, a failure can only terminate the stream; the trace records the partial failure. No automatic retry occurs in either case. SSE event bytes are preserved, but arbitrary upstream HTTP headers are not forwarded.

The default body cap is 262,144 bytes, upstream response/stream cap 2 MiB, and whole provider deadline 30 seconds. JSON content type is required for nonempty request bodies. Cancellation propagates to provider work; no token usage or charges are invented for cancelled/ambiguous requests. Body upload time and proxy-wide concurrency controls are not implemented; deploy behind an appropriately configured ingress before exposing to hostile traffic.

`POST /api/reports/sales` is the separate upstream adapter contract: `start`, `end` (exclusive), optional authorized `branches`, optional candidate `plan_version`, and explicit `cache_approved`. The period must be 1–366 days. It always uses synthetic USD gross-booked-sales data in this MVP. It returns the completion, structured answer, request ID, effective plan, fallback reason, cache status, and mock flag. Dates are explicit; free-form SQL and model-authored SQL are never accepted.

For context blocks, supply `id`, `message_index`, SHA-256 of the UTF-8 message content, `source`, `source_version`, `provenance`, `role`, dependencies, and explicit eligibility/retention flags. Plain unannotated messages are opaque. The integrating application, not text inside retrieved data, is the authority that marks redundancy or protection. Ordinary chat is never response-cached even if its extension asks for caching.

The authenticated plan and trace API is described in `/docs`. Plan reads/previews are allowed within the caller's authorization scope; management actions additionally require `can_manage`. The dashboard shell, health endpoint, and OpenAPI schema are public, but plan/trace/source data requires authentication outside loopback development mode.

`POST /api/plans/{id}/evaluate-live` requires management permission and `{"allow_paid":true,"max_estimated_usd":...,"max_provider_attempts":16,"max_output_tokens":512}`. Both the reporting baseline and candidate must use configured HTTP providers and explicit output caps. The operator must provide versioned prices and a provider-enforced input token ceiling. A failure/unknown usage stops further provider calls. A completed but incorrect text response is scored as a quality failure. The result and JSONL rows distinguish actual provider-reported usage from mock estimates, and never treat a cache hit as newly billed usage. See [live evaluation](live-evaluation.md) for the precise spending assumptions.
