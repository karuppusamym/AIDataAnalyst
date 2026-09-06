# Domain guide — catalog

> Orientation, not specification. The full spec is [`../04-catalog.md`](../04-catalog.md);
> the counts and shape of the module are generated into
> [`../../10-architecture/14-generated-architecture-map.md`](../../10-architecture/14-generated-architecture-map.md).
> This page answers four questions in about a page: what does this context own,
> what must stay true inside it, how do you get into it, and what is deliberately
> somebody else's problem.

**Code:** `src/atlas/modules/catalog/` · **Spec:** [`../04-catalog.md`](../04-catalog.md)

## What it owns

The authoritative inventory of the estate — the seven metadata tables that record
what exists in a source, one level of the hierarchy each: `metadata_catalog`,
`metadata_schema`, `metadata_table`, `metadata_column`, `metadata_constraint`,
`metadata_index`, `metadata_partition`.

It also owns two things built on that inventory:

- **The catalog read model.** One page of catalog rows carries description,
  proposal state, owner, certification, quality state, glossary terms and a row
  estimate — facts that live on five different subsystems. The composer fetches
  them in a fixed, small number of batched queries per page rather than per row.
  Page-size independence is the property, not an optimisation.
- **Bulk stewardship actions.** Tag, classify, own and certify, applied to many
  tables in one request, capped at 500 items with a 10,000-row filter scan cap.

## Invariants it must uphold

- **INV-5, tenant isolation.** Every read and every write is scoped by
  organization. Catalog rows are the widest-fanout read in the product, which
  makes this the easiest place to leak one tenant's estate into another's page.
- **INV-7, attributability.** The router records an audit event for each of its
  mutating routes; bulk actions attribute per item, not per request, so a
  partially-applied bulk action is still fully attributable.
- **Bulk is not a lesser kind of change.** A bulk classify runs the same per-item
  apply function a single classify runs. There is no faster path that skips the
  rules.

## Entry points

- **HTTP** — 10 routes, mounted by `aida.main` through
  `atlas.modules.catalog.api`, this context's public face. The main ones are
  `GET /v1/organizations/{organization_id}/catalog/rows` (the read model), the
  table certification routes, and the four `bulk-*` routes.
- **In-process** — other modules call the composer and the per-item apply
  functions. Several still reach them through the `aida.catalog_read_model` and
  `aida.catalog_bulk_actions` compatibility shims; both are recorded, with their
  removal conditions, in
  [`../../40-engineering/09-compatibility-shim-register.md`](../../40-engineering/09-compatibility-shim-register.md).
- **Ingest** — catalog rows are written by ingestion, never created directly
  through this context's API. That is why its DTOs are read-only.

## What it deliberately does not own

- **Getting metadata in.** Discovery, batching and idempotence belong to
  ingestion. This context is the destination, not the pipeline.
- **What the data means.** Descriptions, business annotations, glossary terms and
  semantic models live in the semantic and stewardship modules. The read model
  *reads* them to compose a row; it does not decide them.
- **Whether the data is trustworthy.** Quality state and incidents belong to data
  quality; certification records a decision made elsewhere.
- **Profiling and classification values.** The catalog stores the classification
  a decision produced; it does not compute one.

## Current shape, honestly

The module directory holds real content — `models.py` (the seven tables),
`repository.py` (the batch helpers), `service.py` (the composer and the bulk
apply functions), `router.py` (the 10 routes), `schemas.py` (read DTOs only).
`contracts.py`, `events.py` and `workers/` are still empty scaffolds: this
context publishes no typed cross-module contract and owns no background worker
today. Work that touches catalog tables in the background runs from `aida.*`
modules instead.

The classes here still declare no separate database schema — this was a
Python-source move, not a database migration. The `catalog module privacy`
import-linter contract is what keeps the boundary real in the meantime: nothing
outside the module may import its internals, except the named compatibility
shims.
