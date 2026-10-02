# Threat model

The protected assets are tenant data, authorization constraints, protected instructions, tool relationships, provider credentials, plan integrity, and honest accounting. Trusted inputs are deployment configuration, locally provisioned credential mappings, approved query definitions, and application-authored context annotations. Retrieved strings, tool outputs, request tenant headers, and provider errors are untrusted.

| Threat | Implemented control | Remaining boundary |
| --- | --- | --- |
| Cross-tenant or cross-branch access | Server-side bearer mapping, scoped state queries, bound SQL predicates, authorization before fallback | Operators and local filesystem administrators remain trusted |
| Prompt injection authorizing plan/data changes | No parsing retrieved text as policy; required/protected blocks; separate management permission; fixed SQL registry | Real LLMs may still follow hostile text; reporting output checks only validate the documented answer contract |
| SQL write, file read, extension load, ATTACH | Controlled path, read-only URI, no extension loading, query-only mode, action/table/column/function authorizer, registry-only SQL | A malicious local administrator can replace code or files |
| Resource exhaustion | Payload/message/output/row limits, progress-handler SQL deadline, provider timeout, bounded HTTP pool | No global rate limiter, body-upload deadline, or distributed admission control |
| Destructive context reduction | Hash/role/source checks, explicit duplicate annotation, retention/protection/dependencies, original-request fallback | Annotation authors must correctly identify business-critical data |
| Cached action or cross-scope answer reuse | Exact key includes semantic request and scope; reporting-only opt-in; no tool cache; no tool execution; TTL/invalidation | Cached synthetic responses reside unencrypted in SQLite |
| Duplicate charges or actions on failure | One provider attempt; no retry/fallback after dispatch; raw SSE close on failure/cancellation | An upstream may complete work after a disconnect; charges are unknown without invoices |
| Unauthorized production plan changes | Explicit management API, offline gate, source freshness, immutable versions, activation audit, ancestry rollback | Database administrators can alter schema; this is not an external tamper-proof audit system |
| Secret/prompt exposure | Sanitized errors, metadata traces, no raw provider errors, key held only in dashboard memory, no access logs in CLI server | Plan descriptions/IDs are operator metadata and must not include secrets |
| Browser attacks | Loopback Host checks, origin rejection, JSON writes, CSP, no CORS, text-only DOM rendering | A hostile local process is outside the dev-mode trust boundary |
| Request-controlled SSRF | Endpoint/model allowlist from operator config only, HTTPS/TLS verification, redirects disabled, no ambient proxy; HTTP exception requires explicit opt-in and exact loopback hostname | Operator-configured URLs remain trusted; network egress controls are deployment-specific |
| Unbounded evaluation spending | Explicit management authorization, per-call rate-card reservations, output bounds, total reservation/attempt limits, stop on transport failure or unknown usage | Incorrect operator rates or provider token ceilings cannot guarantee invoice charges; configure a provider-side hard cap |

Read-only operations fail closed when authentication or branch authorization fails. Only validated, authorized requests can use pre-dispatch original-request fallback. A failed plan never expands data scope. Source changes invalidate cache identity and pinned plan applicability.

API keys are configured at startup; rotate them by updating the secret configuration and restarting. There is no JWT/OAuth identity service, encrypted database, tamper-resistant audit sink, external invoice reconciliation, or penetration-test claim. The bundled adversarial tests establish concrete behavior of this implementation, not safety of arbitrary downstream LLM outputs.
