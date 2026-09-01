# ADR-0005 — Enterprise Tenancy Hierarchy

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

A large bank is not one tenant. It has legal entities with distinct regulatory obligations, lines of business with distinct data-access rules, domains with distinct stewardship, and projects with distinct lifecycles. Cross-LOB data access is a compliance event, not a convenience.

A flat tenant model forces every isolation rule into application code, where it is applied inconsistently and cannot be tested exhaustively.

## Decision

Adopt a six-level hierarchy:

```text
organization
  └── legal_entity
      └── line_of_business
          └── data_domain
              └── project
                  └── datasource
```

- Every governed record carries `organization_id`, plus `legal_entity_id`, `lob_id`, and `project_id` where applicable.
- **Authorization defaults to deny.** Cross-LOB access is explicitly granted, never inherited.
- Tenancy boundaries are preserved in cache keys, graph nodes, vector documents, artifacts, events, logs, and metrics.
- The repository base class **requires** a tenant scope argument. There is no unscoped query helper.

## Consequences

### Positive

- Isolation is structural, testable, and exhaustively verifiable — every endpoint and worker can be exercised with a foreign tenant context.
- Showback and chargeback work at the boundary the bank actually budgets at.
- Data residency can be bound to a level of the hierarchy.
- A leak requires a deliberate bypass, not an omission.

### Negative — costs accepted

- Every query carries tenancy predicates; index design must account for them.
- Six levels is more than most deployments need; small tenants carry unused structure.
- Cross-boundary features (enterprise-wide search, cross-LOB lineage) need explicit, audited grant paths.
- Migration to a different legal-entity model would touch every table.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Flat tenant | Cannot express legal entity vs. LOB vs. project distinctions a bank requires |
| Database-per-tenant | Operationally unmanageable at 500+ LOBs; cross-tenant governance becomes impossible |
| Row-level security only | Good defence in depth, but insufficient alone for cache, graph, events, and logs |

## Revisit trigger

The bank supplies a legal-entity or entitlement model that this hierarchy cannot express.

## Enforcement

- INV-5 in `10-architecture/01-principles-and-invariants.md`
- Test: `test_cross_tenant_denial` over every endpoint and worker (`tests/test_inv5_tenant_isolation.py`, plus a second in `tests/test_tier0_invariants.py`)
