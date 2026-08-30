# ADR-0004 — Execution Choke Point

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

Policy enforcement, cost control, masking, lineage capture, and audit each need to happen on every source query. Implementing them per call site guarantees they will be incomplete: someone adds a code path and forgets one, and the gap is invisible until an incident.

This is also the single most important differentiator (`00-product/05-differentiation-and-whitespace.md`, D1). Competitors cannot adopt it because their existing surfaces — notebooks, BI passthrough, SDK query methods — are bypass paths by design.

## Decision

**Every source query passes through the Query Execution Gateway.** No exceptions:

- model-generated SQL,
- approved governed tool SQL,
- profiler SQL,
- lineage extraction SQL,
- data quality check SQL,
- administrator SQL.

The gateway requires: identity context, purpose, datasource, workload class, policy version, bounded timeout, row and byte limits, SQL AST validation against an allowlist derived from parsed references, and an audit correlation ID.

Connector execution methods are **module-private to the gateway**. The boundary is mechanically enforced by an import-linter contract, not by convention.

## Consequences

### Positive

- Policy, cost, masking, lineage, and audit are complete by construction rather than by diligence.
- Adding a control means changing one place.
- "Prove the model could not run unapproved SQL" has an architectural answer.
- New features cannot accidentally create a bypass — the import rule fails the build.

### Negative — costs accepted

- The gateway is on every hot path; its latency budget is tight (30 ms validation p95).
- It is a single point of failure for all analytical work. It must be highly available and cannot be casually refactored.
- Some legitimate operations (bulk profiling) pay validation cost they would not otherwise pay.
- Extracting it as a service later is delicate: a network-reachable gateway is a network-reachable target (see the extraction plan).

## Alternatives considered

| Option | Why rejected |
|---|---|
| Per-call-site enforcement | Guaranteed incomplete; the failure is silent |
| Middleware around connectors | Bypassable by constructing a connector directly |
| Source-side policy only | Not all sources support the needed granularity; loses uniform evidence |
| Gateway for user queries only | Profilers and tools are the paths most likely to be over-privileged |

## Revisit trigger

**Never.** Any proposal that creates a second execution path is rejected.

## Enforcement

- INV-2 in `10-architecture/01-principles-and-invariants.md`
- Test (planned, not written — 2026-08-30): `test_no_connector_execution_outside_gateway` (static import-graph analysis)
