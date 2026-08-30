# ADR-0006 — Connector Deployment and Capability Negotiation

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

Bank sources live in different network zones. Some are reachable from an application zone; some are behind boundaries the network team will not open. Sources also differ in what they can do: not every engine supports EXPLAIN cost estimation, statement cancellation, or bounded sampling the same way.

Two failure modes must be avoided: assuming network reachability that does not exist, and advertising a capability the adapter does not actually implement.

## Decision

**Capability negotiation.** Connectors implement a capability-negotiated SDK. Capability flags are derived from the connector's **certification result**, not hand-declared. A connector advertises only behaviour that is implemented and passing certification; planned capability is displayed as `PLANNED`.

**Flexible placement.** Connectors may run centrally (worker pulls) or as source-side agents near restricted sources. Source-side agents establish **outbound-only** connections to Atlas over mTLS.

**Credential handling.** Credentials are opaque references resolved at runtime from an enterprise secret manager. Plaintext credentials are never persisted in platform tables, never returned through the API, and never logged.

## Consequences

### Positive

- The capability matrix is honest, which is a genuine differentiator against vendors whose connector lists overstate depth.
- Restricted zones are servable without inbound firewall exceptions — a product requirement for banks, not just an architecture preference.
- A third-party team can build a certified connector without core changes.
- Feature code can branch on capability rather than on source type.

### Negative — costs accepted

- Certification is real work per connector per version, which slows the connector-count number that buyers compare.
- Source-side agents are a distributed deployment to build, ship, upgrade, and monitor.
- Capability branching adds conditional complexity to feature code.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Assume a uniform SQL capability set | Breaks on real engines; produces confident wrong behaviour |
| Central pull only | Cannot serve restricted zones — disqualifying for the target buyer |
| Hand-declared capability flags | Drift between declaration and reality; dishonest matrix |
| Inbound agent connections | Network teams will not permit it |

## Revisit trigger

A source class that neither placement mode can serve.

## Enforcement

- INV-9 in `10-architecture/01-principles-and-invariants.md`
- Test (planned, not written — 2026-08-30): `test_capability_matrix_matches_certification`
