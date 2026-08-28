# ADR-0012 — Single Metadata Envelope for All Ingestion Transports

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

Metadata arrives four ways: native pull adapters, authenticated push producers (a bank metadata bridge or CMDB), broker intake, and source-side agents. If each transport has its own persistence path, then object identity, drift detection, privacy filtering, and graph projection behave differently per transport — and a bug fixed in one path persists in three others.

## Decision

**All transports converge on one versioned canonical metadata envelope and one authoritative persistence path.**

- `envelope_version` is explicit; evolution is backward-compatible.
- `idempotency_key` is unique per datasource. Same key + same payload returns the original job; same key + different payload returns HTTP 409.
- `INCREMENTAL` creates and updates objects present in the envelope and **never** retires omitted objects.
- `FULL` is authoritative for the complete datasource scope and soft-deprecates omitted active objects. It requires explicit confirmation and **runs reconciliation only after every chunk has succeeded**.
- Deliveries acquire a datasource row lock, serializing competing snapshots for one source without blocking others.
- Delivery and catalog changes commit atomically, with a graph snapshot event in the same transaction.
- Raw payloads are not retained after successful processing; only fingerprints, counts, statuses, and timestamps remain.

## Consequences

### Positive

- One place for identity, drift, privacy, and projection semantics — a fix applies everywhere.
- A new transport is an adapter, not a new persistence design.
- Idempotency and replay semantics are uniform.
- External producers build against a stable, documented boundary.

### Negative — costs accepted

- The envelope is a versioned public contract; changing it is a compatibility exercise.
- Transport-specific optimizations are constrained by the common shape.
- Very large estates need the batch/chunk mechanism rather than a simple call.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Per-transport persistence | Divergent behaviour; bugs multiply by transport |
| Direct writes from adapters | No common privacy filter, no uniform idempotency |
| Unversioned envelope | Cannot evolve without breaking producers |

## Revisit trigger

Evolution only through backward-compatible envelope versions and a schema-registry compatibility policy.

## Related

- `30-contracts/05-metadata-ingestion-envelope.md`
- `20-modules/03-ingestion.md`
