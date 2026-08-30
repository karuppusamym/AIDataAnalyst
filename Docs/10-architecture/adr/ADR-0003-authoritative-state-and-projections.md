# ADR-0003 — Authoritative State and Projections

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

The platform needs transactional consistency and approval semantics (PostgreSQL), graph traversal (Neo4j), similarity search (vectors), and lexical search (an index). No single store serves all four well. The danger is not the polyglot persistence itself — it is that a projection silently becomes a second source of truth, and the two diverge without anyone noticing.

## Decision

**PostgreSQL is authoritative.** Neo4j, vector indexes, search indexes, Redis, and object-storage indexes are **rebuildable projections**.

- No decision — authorization, approval, correctness — is made from a projection.
- Authoritative transactions write an **outbox event in the same transaction** as the state change.
- Projectors consume outbox events idempotently.
- **No service dual-writes PostgreSQL and a projection.**
- Any projection may be deleted entirely and rebuilt from authoritative state.

## Consequences

### Positive

- Divergence is detectable and correctable: rebuild and compare.
- Projection loss is an availability event, never a data-loss event.
- Each store is used for what it is good at, without ambiguity about which is right.
- Adding a projection (a new index, a new graph shape) is additive and low-risk.

### Negative — costs accepted

- Eventual consistency between PostgreSQL and projections; lag must be measured and surfaced.
- Rebuild is a real operation with real duration, which must be measured and drilled.
- The outbox adds a write and a publisher process.
- Read models cannot join across the boundary; the application composes instead.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Neo4j as system of record | Poor fit for versioning, approval workflows, and transactional metadata |
| Vector store as system of record | Cannot represent authoritative PK/FK semantics, versions, or approvals |
| Dual-write to PostgreSQL and Neo4j | Unreconcilable divergence on partial failure |
| Single store (PostgreSQL only) | Graph traversal and similarity search become unacceptably slow at target scale |

## Revisit trigger

An approved enterprise metadata system of record replaces PostgreSQL. The projection pattern itself would survive that change.

## Enforcement

- INV-1 in `10-architecture/01-principles-and-invariants.md`
- Test (planned, not written — 2026-08-30): `test_projection_rebuild`
