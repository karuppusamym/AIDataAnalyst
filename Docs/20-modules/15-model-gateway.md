# Module 15 — Model Gateway

> Layer L3 · Schema `model_gateway` · Owner: AI Platform + Model Risk

## 1. Purpose

The only path from Atlas to a language model. Provider-neutral, budgeted, governed, and **fail closed**. Its central design commitment (ADR-0009): **approving a model route does not activate it.**

## 2. Jobs served

P5 (stop AI immediately), R1 (approve routes), U2 (prove model constraints), P6 (explain cost).

## 3. Responsibilities

- Immutable model route versions carrying residency, retention, capability, budget, and model-ID contracts.
- Maker-checker route lifecycle.
- Provider adapters (OpenAI Responses, Google Gemini GenerateContent, private endpoints).
- Structured output with strict schema validation.
- Bounded retries, timeouts, and token budgets.
- Credential-reference resolution and redaction.
- Non-content generation evidence.
- Durable evaluations.
- **Kill switch.**

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Deciding what to ask | 13 agent-runtime, 07 semantic-layer |
| Interpreting output as authority | Nobody — INV-3 forbids it |
| Hosting models | Provider or private endpoint |
| Secret values | 01 identity → secret manager |

## 5. Domain model

```text
model_route, model_route_version (immutable)
model_budget, provider_adapter_registration
generation_evidence (non-content), model_evaluation, evaluation_finding
kill_switch_state
```

## 6. The five independent activation conditions

Generation requires **all five**. Missing any one produces an explicit denial, never a degraded answer.

| # | Condition | Owner |
|---|---|---|
| 1 | An **approved** route version (maker-checker) with residency, retention, capability, budget, and model ID | Governance |
| 2 | That route **selected** as the runtime route | Deployment |
| 3 | Its **credential reference resolving** through a registered, non-environment provider | Platform |
| 4 | A **registered adapter** for the provider | Engineering |
| 5 | Generation **explicitly enabled** by configuration | Deployment |

Approved-but-inactive is a **visible, normal state** in the operator console. Displaying it honestly generates support questions; hiding it would mean an approval click could start external data flow.

## 7. Kill switch

| Property | Requirement |
|---|---|
| Scope | Halts all model traffic, organization-wide or per route |
| Effect on deterministic paths | **None** — tool-first execution, catalog, lineage, and quality continue |
| Latency | Full stop within 60 seconds |
| Authorization | Platform operator; audited |
| Reversal | Requires the same authorization; audited |
| Drill | Quarterly, timed, evidence retained |

**An undrilled kill switch is not a kill switch.** Drill currency is tracked in `60-delivery/03-tracker.md`.

## 8. What crosses to a provider

| Sent | Never sent |
|---|---|
| Bounded structural metadata (names, types, constraints) | Sample row values |
| Deterministic profile statistics | Result rows |
| Classifications | Credentials |
| The user's question | Other tenants' metadata |
| Approved semantic annotations | Anything outside the requester's authorization scope |

Masked-value mode, if ever enabled, is per classification and per route, approved, and never a default (ADR-0014).

## 9. Evidence

Generation evidence is **non-content**: route version, model ID, token counts, latency, retry count, validation outcome, budget consumption, and a content fingerprint. Prompt and response text are not retained.

This makes seven-year retention of AI evidence safe, which is what a model-risk function needs.

## 10. Public interface

```python
# model_gateway/api.py
def generate(scope, route_key, request: StructuredRequest) -> Proposal | Denial
def get_activation_posture(scope) -> ActivationPostureDTO
def list_routes(scope) -> list[ModelRouteDTO]
def create_route_version(scope, spec) -> ModelRouteVersionDTO
def submit_route_for_approval(scope, version_id) -> ProposalDTO   # via module 17
def engage_kill_switch(scope, reason, route_key=None) -> KillSwitchStateDTO
def run_evaluation(scope, suite_id, route_key) -> EvaluationRunDTO
```

`generate` returns a `Proposal` — a structurally inert type that cannot be coerced into an executable command (INV-3).

## 11. Events

Emits `model.route_version_created|approved`, `model.generation_denied`, `model.budget_exceeded`, `model.kill_switch_engaged|released`, `model.evaluation_completed`.

## 12. Dependencies

17 policy-governance.

## 13. Competitive note

Databricks' **Unity AI Gateway** — model/MCP/agent/skill registration, contextual service policies, hard spend caps, unified agent tracing — is the closest competitor capability and the clearest signal that this ground is contested. The distinction that remains: Unity's gateway governs models *and lets them act*; Atlas's gateway governs models *and never lets them act* (INV-3). Budgets and tracing are parity features; the inert-proposal boundary is not.

## 14. Current state → target

| Aspect | Now | Target |
|---|---|---|
| Route versions | Implemented — immutable, residency/retention/capability/budget contracts, credential redaction, maker-checker, honest activation states, **plus config-selected `route_key` gating (bank-approved route selection) in `model_gateway.py`/`ai_governance_api.py`** | Private endpoint adapters/routing |
| Adapters | Implemented — OpenAI Responses, Gemini GenerateContent, structured output, bounded retries/timeouts | Private endpoint adapters |
| Evidence | Implemented — non-content | Unchanged |
| Evaluations | Implemented — durable control evaluations | Bank model-risk corpus |
| Kill switch | Implemented and drilled once (MG-2, 2026-08-31) — `KillSwitchState` + governed `engage_kill_switch`/`release_kill_switch` endpoints (`ai_governance_api.py`), checked first in `ProviderNeutralModelGateway.structured_completion` ahead of every other activation condition, org-wide or per-route scope, audited both directions. Drill is in-process/local only so far (`tests/test_kill_switch_drill.py`) | Drilled quarterly against a deployed gateway, with retained evidence |
| Credentials | `env://` for development; production rejects it | Workload identity, private routing |
| Monitoring | Not connected | Spend, latency, refusal-rate, drift monitoring |

## 15. Open work

| ID | Item | Priority |
|---|---|---|
| MG-1 | Rotate development credentials; move to workload identity | P0 |
| MG-2 | Kill-switch drill with retained evidence — **done in-process (2026-08-31); a timed run against a deployed gateway is not** | P0 |
| MG-3 | Private routing (bank-approved route selection itself is implemented — see §14) | P0 |
| MG-4 | Residency and retention contract certification | P0 |
| MG-5 | Model-risk evaluation corpus | P0 |
| MG-6 | Spend, latency, and drift monitoring with alerts | P1 |
| MG-7 | Private / self-hosted adapter | P1 |
