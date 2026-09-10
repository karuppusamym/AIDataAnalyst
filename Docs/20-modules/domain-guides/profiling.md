# Domain guide — profiling

> Orientation, not specification. The full spec is
> [`../05-profiling-and-classification.md`](../05-profiling-and-classification.md);
> the counts and shape of the module are generated into
> [`../../10-architecture/14-generated-architecture-map.md`](../../10-architecture/14-generated-architecture-map.md).
> This page answers four questions in about a page: what does this context own,
> what must stay true inside it, how do you get into it, and what is deliberately
> somebody else's problem.

**Code:** `src/atlas/modules/profiling/` · **Spec:**
[`../05-profiling-and-classification.md`](../05-profiling-and-classification.md)

## What it owns

What the data looks like, established without retaining what it says. Nine
tables, in four groups:

- **The run ledger.** `analysis_run` (one discovery/profiling pass over a source,
  with its discovered/created/changed/deprecated counters), `analysis_task` (the
  operator-facing mirror of each Temporal activity's attempts, heartbeats and
  failure reason) and `scan_policy` (the schedule that produces runs, and the
  single `priority` column the fleet scheduler orders by).
- **The value-free profiles.** `table_profile` and `column_profile` — row
  estimates, null and non-null counts, approximate distinct counts, length
  bounds. No source value is stored in either.
- **The exception to that rule, and its leash.** `profiling_exception_policy` is
  a maker-checker gate scoped to exactly one
  `(organization, datasource, classification)` triple; only an approved,
  unrevoked policy unlocks `column_value_profile_artifact`, which does hold real
  values (an actual min/max and top-N) and carries a pinned `expires_at` so the
  purge sweep can enforce the retention that was committed to at capture time.
- **The classification ledgers.** `classification_evidence` (append-only: every
  rule-based classification and every authoritative-feed override, with
  `is_current` marking the row that matches the column right now) and
  `column_derived_classification` (a classification propagated along lineage from
  a more sensitive upstream column, kept strictly apart from the asserted one).

## Invariants it must uphold

- **ADR-0014, the value-free control plane.** `column_profile` and
  `table_profile` are statistics, never values. The only path to a stored value
  runs through an approved `ProfilingExceptionPolicy`, and the artifact it
  unlocks is a *separate table* precisely so nothing joins values in by accident.
- **Derived is not asserted.** A classification the lineage graph inferred must
  never become one a policy enforces on without passing through the shared
  maker-checker queue as a `COLUMN_CLASSIFICATION_PROMOTION` review. The two
  tables exist to keep that distinction structural rather than conventional.
- **Why, not just what.** Both classification tables are append-only ledgers with
  evidence — matched signal and rule id on one, the ordered `edge_chain` and the
  `graph_version` it traversed on the other. "Why is this column classified this
  way" is answerable without inference.
- **Maker ≠ checker.** `ProfilingExceptionPolicy` carries its own
  `requested_by` / `decided_by` / `revoked_by` fields rather than filing into the
  shared `governance_review` queue; the rule is the same, the queue is not.
- **INV-5, tenant isolation.** Every one of the nine tables carries
  `organization_id`.

## Entry points

- **HTTP** — none owned yet. The analysis-run, scan-policy, profile,
  classification-feed and profiling-exception endpoints still live in `aida.api`
  and `atlas.modules.connectivity.router`. `router.py` here is an empty container
  for the later ST-07 route move; `api.py` does not re-export it.
- **In-process** — everything reaches this context's models and DTOs through the
  `aida.models` / `aida.schemas` compatibility re-exports, which is what let the
  relocation change no caller. Both are recorded, with their removal conditions,
  in
  [`../../40-engineering/09-compatibility-shim-register.md`](../../40-engineering/09-compatibility-shim-register.md).
- **Workers** — `aida.workflows.activities` (`profile_table_task`), the fleet
  scheduler and `aida.task_tracking` write these tables today. None of them has
  moved into `workers/`.

## What it deliberately does not own

- **Executing the profiling SQL.** That is the query gateway's, and INV-2 is
  enforced by import-linter: nothing but `aida.query_gateway` may reach a
  `SqlExecutor`.
- **The asserted classification itself.** It lives on `MetadataColumn`, which is
  catalog's row. This context owns the *evidence* for it and the derived
  candidate, not the enforced value.
- **Whether the data is trustworthy.** Thresholds, observations and incidents are
  data quality's.
- **Cross-table relationships and key inference candidates.** The `*Candidate`
  models are relationship intelligence's (module 06), even though module 05's
  register line mentions "key inferences" — see the note in `models.py`.
- **Applying source-native policy DDL.** `PolicyNativeSyncRequest` copies this
  context's maker-checker shape and sits next to it in the old `aida.models`, but
  it gates a live write to an external source. Shape similarity is not ownership.

## Current shape, honestly

`models.py` (the nine tables) and `schemas.py` (15 DTOs) hold real content, moved
verbatim from `aida.models` and `aida.schemas` in the R04 pass. `api.py`,
`contracts.py`, `events.py`, `repository.py`, `service.py`, `router.py` and
`workers/` are **all still empty scaffolds** — this context publishes no typed
cross-module contract, owns no route and owns no worker today. That is a larger
scaffold fraction than any of the five contexts extracted before it, and the
guide says so rather than implying a completeness that is not there.

The classes here still declare no separate database schema; this was a
Python-source move, not a database migration, and `Base.metadata` is unchanged by
it. The `profiling module privacy` import-linter contract is what keeps the
boundary real in the meantime: nothing outside the module may import its
internals, except the two named compatibility shims.
