# ADR-0007 — Eventing Split Between Temporal and Kafka

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

Both Temporal and Kafka are present. Without an explicit rule, teams will use whichever is nearest, and the system will end up reconstructing process position by replaying events — which does not work, because event replay and durable process state have different failure semantics.

## Decision

**Temporal owns workflow command and state semantics.** Kafka owns integration events, lineage events, replayable projection events, and high-volume decoupled consumers.

The rule, stated so it can be applied without thinking:

- *"Where are we in this process?"* → **Temporal**.
- *"This happened."* → **Kafka**.

They are complementary and must never be used as competing workflow engines. Kafka is never the record of process position; Temporal is never a broadcast mechanism to unknown consumers.

## Consequences

### Positive

- Each system is used where its guarantees fit.
- Kafka outage causes projection lag, not process loss.
- Temporal outage blocks new durable work but does not lose published facts.
- Consumers can be added without touching workflow code.

### Negative — costs accepted

- Two pieces of infrastructure to run, monitor, and upgrade.
- Engineers must internalize the distinction; it is not self-evident.
- Some information exists in both places (a workflow completed; an event says so), requiring care not to let them disagree.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Kafka only, with process state in a table | Reimplements Temporal badly: retries, heartbeats, cancellation, timers |
| Temporal only, signalling consumers | Workflows would need to know every consumer; couples producer to consumer |
| Neither; synchronous calls | No durability, no decoupling, no replay |

## Revisit trigger

No planned change.

## Related

- `10-architecture/07-event-and-messaging-model.md`
