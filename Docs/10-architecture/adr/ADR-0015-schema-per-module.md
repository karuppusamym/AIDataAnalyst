# ADR-0015 — Schema Per Module, No Cross-Schema Foreign Keys

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

ADR-0011 chose a modular monolith with a planned extraction path. That plan only works if extracting a module does not require a data migration. If modules share a schema and hold foreign keys into each other's tables, extraction means untangling referential integrity across a live database — the most expensive step, and the one that usually stops the extraction.

## Decision

- **One PostgreSQL schema per module**, named for the module.
- **No cross-schema foreign keys**, with one exception: references into `identity` (the tenancy root) are permitted, because tenancy is universal and `identity` will never be extracted independently.
- A module referencing another module's entity **stores its ID** and resolves it through the published interface.
- Cross-module referential integrity is **eventual**, maintained by projectors and reconciliation jobs, and **surfaced as measurable lag**.
- Repository classes are module-private and require a tenant scope argument.

## Consequences

### Positive

- Extraction becomes a deployment change: move the schema to its own database and swap the in-process adapter for a remote one.
- Boundary violations are visible in migrations, which are reviewed.
- Each module's migrations are independent; a schema change does not force every module's tests to re-baseline.
- Blast radius of a bad migration is one module.

### Negative — costs accepted

- **Cannot join across modules in SQL.** The application composes instead, which is more code and can mean N+1 patterns if written carelessly.
- The database cannot enforce cross-module referential integrity; orphan detection is a reconciliation job.
- Some reports that would be one query become several calls.
- Developers used to a single schema will find this restrictive, and there will be pressure to make exceptions.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Single schema | The current defect; makes extraction a data migration |
| Database per module now | Distributed transactions before boundaries are proven; operational cost |
| Cross-schema FKs allowed | Removes the extraction insurance, which is the entire point |
| Views to simulate joins | Recreates the coupling with extra indirection |

## Revisit trigger

**Never.** Every exception granted here is repaid at extraction time with interest.

## Related

- ADR-0011
- `10-architecture/04-module-decomposition.md` §6
