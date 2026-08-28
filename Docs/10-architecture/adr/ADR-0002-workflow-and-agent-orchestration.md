# ADR-0002 — Workflow and Agent Orchestration

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

Two kinds of orchestration are needed and they have different requirements. Source onboarding, discovery, profiling DAGs, and batch ingestion run for hours, must survive process restarts, and need retries, heartbeats, and cancellation. The interactive analytical path runs for seconds and needs explicit, inspectable state transitions that can be audited.

Using one mechanism for both produces either an under-durable interactive path or an over-heavy background path.

## Decision

**Temporal owns durable, long-running enterprise workflows**: source onboarding, discovery, profiling, lineage extraction, batch ingestion, re-analysis, and projection rebuild.

**An internal typed analytical state machine owns the runtime query path**:

```text
RECEIVED → AUTHORIZED → SCREENED → RESOLVED → PLANNED → GENERATED
        → VALIDATED → COSTED → EXECUTED → EXPLAINED → COMPLETED
```

Each transition is explicit, recorded, and carries the versions pinned at that point. The state machine is framework-neutral: it depends on no external agent library.

## Consequences

### Positive

- Background work gets real durability without imposing workflow overhead on a 300 ms interactive path.
- The runtime state machine is auditable by construction — every run is a sequence of named states with evidence.
- The state machine is portable; it is not coupled to any agent framework's lifecycle.

### Negative — costs accepted

- Two orchestration models to understand and maintain.
- Temporal is significant operational surface (cluster, namespaces, history retention, failover).
- The state machine is hand-built rather than inherited from a library.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Temporal for the interactive path too | Workflow overhead inside a latency budget measured in hundreds of milliseconds |
| An agent framework's graph for both | Couples durability and audit evidence to a third-party lifecycle; see ADR-0008 |
| Celery/RQ for background work | No durable history, no built-in cancellation or heartbeat semantics |

## Revisit trigger

Enterprise platform standardization on a different durable workflow engine.

## Related

- `10-architecture/08-workers-and-workflows.md`
- ADR-0008 (no agent framework in core)
