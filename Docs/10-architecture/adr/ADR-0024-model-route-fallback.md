# ADR-0024 — Cross-Route Model Fallback on Transient Provider Failure

**Status:** Accepted | **Date:** 2026-09-03 | **Owner:** Architecture + Model Risk

## Context

`ProviderNeutralModelGateway.post_with_retry` already retries transient provider failures within a single approved route (`model_provider_max_attempts`, exponential backoff, `Retry-After` honored up to 30s). But a sustained provider outage — an OpenAI quota window that resets in 60s, a Gemini regional degradation, a Vault-issued credential that has minutes-scale reissuance delay — exhausts all in-route retries and surfaces to the caller as `HTTP 429` / `HTTP 503` from a route that is otherwise correctly approved and configured.

Operators running the bank product with **both** provider credentials in the environment reasonably expected the runtime to try the other provider on this shape. It didn't: `settings.model_route` is a single string, and `_approved_model_route` returned exactly one `ApprovedModelRoute`. This ADR records the small feature that closes that gap without violating ADR-0001 or ADR-0009.

## Decision

**The runtime may fall back to an alternate approved route on transient provider failure, within a strict per-organization allow-list.**

`Settings.model_route_fallbacks` (comma-separated `route_keys`, `AIDA_MODEL_ROUTE_FALLBACKS=` env var) declares additional route_keys the orchestrator is allowed to try, in preference order, when the primary route's post-retry outcome is a transient provider failure. Every entry must itself be a governance-approved `ModelRouteConfiguration` (residency, retention, capabilities, budget, model ID all reviewed) — same five facts ADR-0009 requires for the primary. Entries that are not `APPROVED`, do not carry `SQL_GENERATION`, or lack a `credential_reference` are silently skipped by `_approved_model_routes`, so revoking a fallback via governance is a no-op for callers that had it in their list, not an outage.

**Transient** means one of `{429, 502, 503, 504}` from the provider. Non-transient failures — `401` / `403` (broken credential), `400` (malformed payload), or `ModelOutputInvalid` (provider answered but shape was wrong) — do **not** trigger fallback: they signal that the route itself is broken, and switching would just move the failure to a differently-broken route.

`agent_run.plan_evidence.model_call_attempts` records every attempt (route_key, provider_type, attempt_ordinal, outcome, provider_status_code on failure) whenever more than one attempt fired or a fallback was declared, so the audit trail explains how the eventual success or refusal was reached.

## Consequences

### Positive

- Sustained outage in one provider no longer surfaces to end users when the operator has approved a second provider — availability improves without a policy change.
- Governance is preserved: iteration walks only routes already APPROVED via `ModelRouteConfiguration`. The runtime never discovers a route on its own; the operator's decision to approve a second provider is exactly the switch that enables fallback.
- The audit trail (`model_call_attempts`) is stronger than before: a bank auditor can answer "how often did we fall back last month, and to what" without spelunking gateway logs.
- Extends naturally to N routes (comma-separated list) — no code change to go from 2 routes to 5.
- Revoking a fallback in governance is instantaneous and requires no redeploy.

### Negative — costs accepted

- Two states now need to be reasoned about: "answered by primary" and "answered by fallback". Answers are still deterministically validated by `QueryExecutionGateway`, but a customer question routed to Gemini instead of OpenAI has different provider-side context handling (privacy posture, tokenization, output style) — the fallback route's approval covers this, but operators must remember approval means "safe to use on real traffic", not "same behavior as the primary".
- Fallback windows can mask a broken primary: a permanent primary outage keeps working because every request quietly falls back. Mitigated by `model_call_attempts` surfacing in evidence and by the existing per-route observability, but callers should watch fallback rate rather than only success rate.
- Cost accounting is more complex: a request may draw budget from either route depending on outcome. `ModelRouteConfiguration.input_cost_per_million` / `output_cost_per_million` are already per-route, so this reports honestly downstream — but budget alerts have to know they can fire on either route.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Route pool / health-based routing | Requires per-provider health state, an SLO on how quickly a provider is declared unhealthy, and a re-probe policy. Larger surface, harder to audit ("why did this request go to route B when route A was fine?"). Not needed for the current problem. |
| Auto-discover providers from env keys | Governance violation. Presence of `OPENAI_API_KEY` does not equal approval to send bank metadata to OpenAI on this workload — ADR-0009 explicitly forbids this collapse. |
| Fall back on any error (including 401/403) | A broken credential is not a busy provider. Silently switching would let a bank operate on the wrong provider without noticing the primary is misconfigured. Fail loudly on hard errors is the safer default. |
| Fall back on `ModelOutputInvalid` (bad output shape) | The output-schema contract is provider-agnostic; a different provider is not more likely to produce valid structured output for the same broken prompt. Retrying at the prompt level is a different problem. |
| Single-string `model_route_fallback` (only one alternate) | Would need a config change to move from 2 routes to 3. The comma-list scales to N without code. |

## Revisit trigger

- Sustained fallback rate above 5% of generation requests in production for a week: consider health-based routing (route-pool) so the primary is not being retried on every request during a known outage window.
- Cost dashboards show cross-route drift: revisit whether the fallback route's per-token cost warrants a soft preference (route with the cheapest matching capability first) rather than the operator-declared preference order.
- A regulator requires provider-of-record predictability per request (some jurisdictions do): revisit whether fallback should be an opt-in per organization/workspace instead of an org-wide setting.

## Related

- `10-architecture/adr/ADR-0001-hybrid-deterministic-llm.md` — models propose; deterministic services decide. Fallback is still deterministic control over which model gets asked.
- `10-architecture/adr/ADR-0009-route-approval-is-not-activation.md` — every fallback route must independently satisfy the five conditions ADR-0009 requires.
- `20-modules/15-model-gateway.md`
- `60-delivery/03-tracker.md` — filed under the multi-route failover follow-up.
