# Domain guide — ingestion

> Orientation, not specification. The full spec is
> [`../03-ingestion.md`](../03-ingestion.md); the generated shape of the module is
> in
> [`../../10-architecture/14-generated-architecture-map.md`](../../10-architecture/14-generated-architecture-map.md).

**Code:** `src/atlas/modules/ingestion/` · **Spec:** [`../03-ingestion.md`](../03-ingestion.md)

## What it owns

Getting metadata in, idempotently, at any scale. Three tables:

- `metadata_ingestion_job` — one discovery or envelope-ingest run.
- `metadata_ingestion_batch` — a large submission split into pieces, with a state
  machine an operator can act on.
- `metadata_ingestion_chunk` — a piece of a batch, uploaded independently.

The batch state machine is the substance of this context. A batch can be paused
from `QUEUED`/`RUNNING`/`PROCESSING`, resumed only from `PAUSED`, cancelled from
any pre-terminal state, and replayed only from `FAILED`, `SUBMISSION_FAILED` or
`CANCELLED`. Those transitions are declared as explicit source-state sets, not
inferred, so an illegal transition is refused rather than silently accepted.

## Invariants it must uphold

- **Ingesting the same thing twice changes nothing.** Idempotence is the property
  the whole context exists to provide; a retried chunk or a replayed batch must
  not duplicate catalog rows.
- **INV-5, tenant isolation.** Batches and chunks are scoped to a datasource,
  which is scoped to a project, which is scoped to an organization.
- **INV-7, attributability.** The router audits its mutating routes — pause,
  cancel, resume and replay are operator decisions and are recorded as such.
- **A chunk is accepted or it is not.** Partial acceptance with a success
  response is the failure mode this context is shaped to prevent; finalisation is
  a separate, explicit step.

## Entry points

- **HTTP** — 15 routes: the connector capability matrix, connector certification
  runs for a datasource, metadata ingestions, and the batch lifecycle (create,
  get, list, upload chunk, list chunks, finalize, pause, cancel, resume, replay).
- **Mounted through a shim.** `aida.main` imports this router as
  `aida.ingestion_api`. `tests/test_in2_batch_controls.py` also imports four
  batch-control handlers from that path directly, to test transitions without
  HTTP. Both callers and the removal condition are in
  [`../../40-engineering/09-compatibility-shim-register.md`](../../40-engineering/09-compatibility-shim-register.md).
- **Workers** — the discovery and ingestion workflows that feed this context run
  in the Temporal worker process (`aida.workflows.ingestion`,
  `aida.workflows.discovery`), outside the module.

## What it deliberately does not own

- **Where the metadata lands.** The catalog owns the seven `metadata_*` tables
  this context writes into. Ingestion is the pipeline; catalog is the store.
- **How a source is reached.** The drivers are `aida.connectors`; the source
  registration and scan policy belong to connectivity.
- **What the metadata means.** No inference, classification or description
  happens here.
- **`connector_certification_run` the table.** The certification routes are in
  this router, but the row belongs to connectivity's models. That is a real seam
  between the two contexts, stated rather than smoothed over.

## Current shape, honestly

`models.py`, `schemas.py` and `router.py` hold real content — and `schemas.py` is
substantial, because the envelope format is a genuine contract with external
producers. `service.py`, `repository.py`, `contracts.py`, `events.py` and
`workers/` are empty scaffolds: the batch state machine currently lives in the
router alongside the HTTP translation, and the background work that drives
ingestion lives in `aida.workflows`, not in this module's `workers/` directory.

The `ingestion module privacy` import-linter contract protects the internals and
names `aida.ingestion_api` as a permitted importer, which is what makes the shim
legal rather than a hole in the boundary.
