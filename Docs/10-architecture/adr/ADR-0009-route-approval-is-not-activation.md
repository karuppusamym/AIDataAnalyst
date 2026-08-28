# ADR-0009 — Model Route Approval Does Not Activate Generation

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture + Model Risk

## Context

There is a natural but dangerous assumption that approving a model route means the model is live. Governance intent, deployment selection, credential resolution, and adapter registration are **four independent facts**. Collapsing them into one means an approval in a governance UI can silently start sending bank metadata to an external provider.

## Decision

**Model-route approval is a governance record, not an activation switch.** Generation requires *all* of the following, independently:

1. An **approved** organization model-route version (maker-checker), carrying residency, retention, capability, budget, and model ID contracts.
2. That route **selected** as the runtime route.
3. Its **credential reference resolving** through a registered, non-environment credential provider.
4. A **registered adapter** for the route's provider.
5. Generation **explicitly enabled** by configuration.

If any is missing, natural-language generation returns an **explicit denial**, not a degraded answer. The activation posture is displayed honestly in the operator console: approved-but-inactive is a visible, normal state.

## Consequences

### Positive

- An approval click cannot start external data flow.
- The four facts are separately auditable and separately reversible.
- The kill switch is straightforward: break any one of the five conditions.
- Operators see the truth rather than an implied state.

### Negative — costs accepted

- Enabling generation is a five-step process, which is friction, and users will ask why approval "did not work."
- More state to model, display, and explain.
- Support burden from the approved-but-inactive state being misread as a bug.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Approval activates the route | An approval click starts external data flow — unacceptable |
| Single "enabled" flag | Cannot express residency, budget, or credential readiness independently |
| Auto-activate on credential resolution | Credential availability is not governance approval |

## Revisit trigger

No planned change.

## Related

- `20-modules/15-model-gateway.md`
- `50-security/03-ai-safety-controls.md`
