# ADR-0008 — No Agent Framework in the Core

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

LangGraph, Google ADK, and similar frameworks offer checkpointing, graph orchestration, and ecosystem integrations. They are attractive and would save initial work.

They also want to own the agent's state, its step transitions, and often its persistence. In Atlas, those are the audit record, the permission envelope, and the evidence trail — the things a bank regulator will inspect.

## Decision

**No agent framework is a core dependency.** The analytical state machine, permission envelope, and evidence model are implemented in Atlas and are framework-neutral.

A framework may be added **behind an adapter** for a specific approved workflow that needs its checkpointing or ecosystem features. Such an adapter is never a security boundary and never a required dependency.

## Consequences

### Positive

- State, permissions, and evidence stay portable and inspectable.
- A framework's breaking change, deprecation, or licence change cannot force a rewrite of the audit path.
- The state machine contains exactly the states Atlas needs — including `SCREENED`, which no framework provides.
- Reviewers can read the whole runtime path in one module.

### Negative — costs accepted

- Real engineering that a framework would have provided: state persistence, retry semantics, step orchestration.
- No access to framework ecosystem integrations without writing an adapter.
- Contributors familiar with those frameworks face a learning curve.

## Alternatives considered

| Option | Why rejected |
|---|---|
| LangGraph in the core | Framework owns checkpoint state, which is the audit record |
| Google ADK in the core | Same, plus Google-centric deployment assumptions |
| Framework with a custom persistence backend | Reduces but does not remove lifecycle coupling; still owns transitions |

## Revisit trigger

A specific approved workflow requires framework checkpointing or ecosystem features. Add behind an adapter; do not move the boundary.

## Related

- ADR-0002 (orchestration)
- `20-modules/13-agent-runtime.md`
