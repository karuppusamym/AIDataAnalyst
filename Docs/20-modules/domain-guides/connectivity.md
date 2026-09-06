# Domain guide — connectivity

> Orientation, not specification. The full spec is
> [`../02-connectivity.md`](../02-connectivity.md); the generated shape of the
> module is in
> [`../../10-architecture/14-generated-architecture-map.md`](../../10-architecture/14-generated-architecture-map.md).

**Code:** `src/atlas/modules/connectivity/` · **Spec:** [`../02-connectivity.md`](../02-connectivity.md)

## What it owns

The *registration* of a source and the record of what it can do — two tables:

- `datasource` — the source as the platform knows it: which project it belongs
  to, connection parameters, secret references, scan policy.
- `connector_certification_run` — the record of a certification attempt against a
  real source, which is what separates "a driver exists" from "it worked here".

Plus the scan policy that decides what a discovery run is allowed to look at, and
the connection test that answers *can we reach this at all* without running a
user's query.

## Invariants it must uphold

- **INV-5, tenant isolation.** A datasource belongs to a project inside a line of
  business inside an organization. Every route enforces the organization before
  it touches a row.
- **INV-9, honest capability reporting.** A connector's declared capabilities
  must describe what it actually does. A registration that parses is not a
  registration that has ever met a source — the certification run is the
  distinction, and it is a stored fact, not a flag somebody set.
- **Credentials are references, not values.** The `datasource` row carries a
  pointer into the secret store, never the secret.

## Entry points

- **HTTP** — 7 routes, mounted by `aida.main` through
  `atlas.modules.connectivity.api`, this context's public face: list/create
  datasources under a project, bulk onboard, patch a datasource, get and put its
  scan policy, and test the connection.
- **In-process** — `DataSource` is one of the most widely imported models in the
  tree. Most callers still reach it through the `aida.models` re-export block,
  which is a partial compatibility shim; see
  [`../../40-engineering/09-compatibility-shim-register.md`](../../40-engineering/09-compatibility-shim-register.md).

## What it deliberately does not own

- **The drivers.** The five real adapters live in `aida.connectors`, outside this
  context. This module records *that* a source exists and what it claims;
  `aida.connectors` is what actually speaks to it.
- **Executing anything against a source.** SQL execution has exactly one path,
  and it is not here — see INV-2 and the query gateway. The
  `INV-2 connector SQL execution is reachable only from the query gateway`
  import-linter contract permits a single importer of the execution module, and
  this context is not it.
- **Discovery and ingestion runs.** Scan policy is owned here; the run that obeys
  it belongs to ingestion.
- **Certification *scheduling*.** The certification-run rows are this context's;
  the routes that create them today sit in the ingestion router, under
  `/datasources/{datasource_id}/connector-certifications`. That split is a real
  seam, recorded here rather than tidied over.

## Current shape, honestly

`models.py`, `schemas.py` and `router.py` hold real content. `service.py`,
`repository.py`, `contracts.py`, `events.py` and `workers/` are still empty
scaffolds — the router talks to the ORM directly, so this context has no domain
layer of its own yet. That is the honest state, and it is why "what it owns"
above is a table list rather than a behaviour list.

The `connectivity module privacy` import-linter contract already protects the
internals, so the boundary exists ahead of the logic that will sit behind it.
