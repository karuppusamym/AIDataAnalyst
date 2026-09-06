# Compatibility shim register

> **Generated file — do not edit by hand.**
> Regenerate with `python scripts/shim_register.py`; `--check` fails when it is stale.
> The judgement columns live in that script's `SHIMS` table, not in this file.

Review 2026-09-05 point **D03** asks for the thing this file is: every
compatibility shim with a replacement path, an owner, a measured caller count
and a removal condition. Its closing instruction is the one that matters most —
*do not remove them solely because they look redundant.* Nothing here authorises
a deletion; the register exists so that a deletion, when it happens, is a
decision against a stated condition rather than a guess against an appearance.

## Which columns are generated and which are not

| Column | Source |
|---|---|
| Shim, file, lines | Filesystem |
| Re-exported names | `ast` walk of the shim file |
| Caller files, caller count, import statements | `ast` walk of every `.py` under `src/`, `tests/`, `scripts/`, `sdk/`, `migrations/` |
| String references | `ast` string-constant scan over the same files |
| Import-linter contracts | `pyproject.toml`, parsed |
| Frontend caller counts | Import-specifier scan over `ui-next/src`, resolved relative to each importing file |
| **Replacement path** | Hand-written |
| **Owner area** | Hand-written |
| **Removal condition** | Hand-written — it is a judgement, not a fact |

There is no `CODEOWNERS` file in this repository, so *owner* names an **area of
the system**, not a person. An area is the unit that can actually satisfy the
removal condition.

## What the caller count does and does not see

Counted: `import X`, `from X import y`, and `from X.sub import y`. Counted
separately, in its own column: a string constant naming the module — a
`monkeypatch.setattr("aida.db.engine", ...)` target or an
`importlib.import_module` argument — because an import-only scan misses those
and they are real dependencies.

Not counted at all: consumers outside this repository, attribute access on an
already-imported package object, and a module path stored as data. **This is why
a zero count is evidence and not permission.** The review says so directly, and
the removal-condition column, not the count, is what a removal has to satisfy.

For the two *partial* shims (`aida.models`, `aida.schemas` — large files that
keep most of their own content and re-export a block on top) a file counts as a
caller only when it imports one of the re-exported names. Importing something
that still genuinely lives in the file is not use of the shim.

## Register

14 shims, 801 shim-to-caller-file relationships across 365 distinct files, 0 shim(s) with a measured caller count of zero.

| Shim | Kind | Replacement path | Owner area | Callers | Import stmts | String refs |
|---|---|---|---|---:|---:|---:|
| [`aida.db`](#aidadb) | python | `atlas.platform.db` | Platform infrastructure | 221 | 221 | 1 |
| [`aida.config`](#aidaconfig) | python | `atlas.platform.config` | Platform infrastructure | 179 | 184 | 1 |
| [`aida.context`](#aidacontext) | python | `atlas.platform.context` | Platform infrastructure | 63 | 63 | 0 |
| [`aida.logging`](#aidalogging) | python | `atlas.platform.logging` | Platform infrastructure | 5 | 5 | 0 |
| [`aida.models`](#aidamodels) | python-partial | `atlas.modules.<context>.models` (re-exported classes only) | Bounded contexts (catalog, connectivity, identity_tenancy, ingestion, observability_audit) jointly | 278 | 351 | 1 |
| [`aida.schemas`](#aidaschemas) | python-partial | `atlas.modules.<context>.schemas` (re-exported DTOs only) | Bounded contexts (catalog, connectivity, identity_tenancy, ingestion, observability_audit) jointly | 17 | 17 | 0 |
| [`aida.catalog_read_model`](#aidacatalogreadmodel) | python | `atlas.modules.catalog.service` / `.repository` | Bounded context: catalog | 8 | 8 | 0 |
| [`aida.catalog_bulk_actions`](#aidacatalogbulkactions) | python | `atlas.modules.catalog.service` | Bounded context: catalog | 6 | 6 | 0 |
| [`aida.workspace_api`](#aidaworkspaceapi) | python | `atlas.modules.identity_tenancy.router` | Bounded context: identity_tenancy | 1 | 1 | 1 |
| [`aida.ingestion_api`](#aidaingestionapi) | python | `atlas.modules.ingestion.router` | Bounded context: ingestion | 2 | 2 | 0 |
| [`aida.observability_api`](#aidaobservabilityapi) | python | `atlas.modules.observability_audit.router` | Bounded context: observability_audit | 4 | 4 | 0 |
| [`_api_append.ts`](#apiappendts) | typescript | `ui-next/src/lib/api/glossary.ts` | Experience shell (ui-next) | 6 | 17 | 0 |
| [`_cross_source_api.ts`](#crosssourceapits) | typescript | `ui-next/src/lib/api/crossSource.ts` | Experience shell (ui-next) | 5 | 5 | 0 |
| [`_column_documentation_api.ts`](#columndocumentationapits) | typescript | `ui-next/src/lib/api/columnDocumentation.ts` | Experience shell (ui-next) | 6 | 6 | 0 |

### Shims with no measured caller

None: every shim in the register has at least one in-repository import
caller today.

## Detail

### aida.db

- **File** — `src/aida/db.py` (43 lines)
- **Replacement path** *(hand-written)* — `atlas.platform.db` -- the same objects, moved, not copied.
- **Owner area** *(hand-written)* — Platform infrastructure
- **Introduced by** — ST-04, Phase 1 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 6 name(s) from `atlas.platform.db`
- **Callers** — 221 file(s), 221 import statement(s): `migrations/env.py`, `scripts/agent_kill_switch_drill.py`, `scripts/backfill_certification_evidence.py`, `scripts/quality_benchmark.py`, `scripts/scale_harness/gp1_measure_projection_memory.py`, `src/aida/access_review_api.py`, +215 more
- **String references** — 1 file(s): `tests/test_siem_wiring.py`
- **Removal condition** *(hand-written)* — Import callers and string references both reach zero, **and** `migrations/env.py` imports `Base` from the canonical module instead. Alembic's environment is the caller most easily forgotten: it is not under `src/`, and breaking it breaks every migration rather than a test.
- **Note** — Lazy `__getattr__` for `engine`/`session_factory`/`settings`, so importing the shim does not construct an engine. A rewrite of the shim must keep that.

### aida.config

- **File** — `src/aida/config.py` (11 lines)
- **Replacement path** *(hand-written)* — `atlas.platform.config`.
- **Owner area** *(hand-written)* — Platform infrastructure
- **Introduced by** — ST-04, Phase 1 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 2 name(s) from `atlas.platform.config`
- **Callers** — 179 file(s), 184 import statement(s): `migrations/env.py`, `scripts/model_risk_benchmark.py`, `scripts/quality_benchmark.py`, `src/aida/agent_contract_api.py`, `src/aida/agent_evals.py`, `src/aida/agent_intelligence.py`, +173 more
- **String references** — 1 file(s): `scripts/generate_destination_inventory.py`
- **Removal condition** *(hand-written)* — Import callers and string references both reach zero, **and** `migrations/env.py` reads settings from the canonical module. `Settings` is also the type annotation on FastAPI dependency callables, so a caller count here undercounts nothing only because those callers import the name.

### aida.context

- **File** — `src/aida/context.py` (11 lines)
- **Replacement path** *(hand-written)* — `atlas.platform.context`.
- **Owner area** *(hand-written)* — Platform infrastructure
- **Introduced by** — ST-04, Phase 1 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 2 name(s) from `atlas.platform.context`
- **Callers** — 63 file(s), 63 import statement(s): `src/aida/access_review_api.py`, `src/aida/agent_contract_api.py`, `src/aida/agent_contract_request_api.py`, `src/aida/agent_eval_gate.py`, `src/aida/ai_governance_api.py`, `src/aida/ai_registry_api.py`, +57 more
- **Removal condition** *(hand-written)* — Import callers and string references both reach zero. The `ContextVar` identity matters: correlation ids set through one import path must be visible through the other, which they are because the shim re-exports the same object rather than defining a second one. Any migration must move callers, never copy the variable.

### aida.logging

- **File** — `src/aida/logging.py` (11 lines)
- **Replacement path** *(hand-written)* — `atlas.platform.logging`.
- **Owner area** *(hand-written)* — Platform infrastructure
- **Introduced by** — ST-04, Phase 1 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 1 name(s) from `atlas.platform.logging`
- **Callers** — 5 file(s), 5 import statement(s): `src/aida/main.py`, `src/aida/projectors/graph_projector.py`, `src/aida/projectors/outbox_publisher.py`, `src/aida/workflows/scheduler.py`, `src/aida/workflows/worker.py`
- **Removal condition** *(hand-written)* — Import callers and string references both reach zero. `configure_logging` is called once per process from each of the five entry points, so this shim cannot go before every entry point has moved.

### aida.models

- **File** — `src/aida/models.py` (5748 lines)
- **Replacement path** *(hand-written)* — Each re-exported class has moved to the `models` module of the bounded context that owns it; import it from there. The rest of the file -- the large majority of it -- has not moved and has no replacement path yet.
- **Owner area** *(hand-written)* — Bounded contexts (catalog, connectivity, identity_tenancy, ingestion, observability_audit) jointly
- **Introduced by** — ST-05, Phase 3 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 44 name(s) from `atlas.modules.catalog.models`, `atlas.modules.connectivity.models`, `atlas.modules.identity_tenancy.models`, `atlas.modules.ingestion.models`, `atlas.modules.observability_audit.models`, `atlas.platform.db`
- **Callers** — 278 file(s), 351 import statement(s): `scripts/agent_kill_switch_drill.py`, `scripts/quality_benchmark.py`, `scripts/scale_harness/ct2_cleanup.py`, `scripts/scale_harness/ct2_generate_catalog.py`, `scripts/scale_harness/ct2_measure_pagination.py`, `scripts/scale_harness/gp1_measure_projection_memory.py`, +272 more
- **String references** — 1 file(s): `tests/test_lineage_edge_kind_vocabulary.py`
- **Named in import-linter contracts** — `catalog module privacy`, `connectivity module privacy`, `identity_tenancy module privacy`, `ingestion module privacy`, `observability_audit module privacy`
- **Removal condition** *(hand-written)* — **Not removable as a file at all** until every remaining class in it has moved to a context -- it is a partial shim, not a shim. The re-export *block* can go when no caller imports a re-exported name and the `aida.models` entry disappears from every `allowed_importers` list in `pyproject.toml`. Note that `Base.metadata` must keep seeing all of these classes for Alembic autogenerate to be correct, so removing the block requires `migrations/env.py` to import the context model modules directly.
- **Note** — Named in the `allowed_importers` list of all five module-privacy contracts, which is what lets the shim import the private module it re-exports from.

### aida.schemas

- **File** — `src/aida/schemas.py` (3946 lines)
- **Replacement path** *(hand-written)* — Each re-exported DTO has moved to the `schemas` module of the bounded context that owns it. The rest of the file has not moved.
- **Owner area** *(hand-written)* — Bounded contexts (catalog, connectivity, identity_tenancy, ingestion, observability_audit) jointly
- **Introduced by** — ST-05, Phase 3 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 66 name(s) from `atlas.modules.catalog.schemas`, `atlas.modules.connectivity.schemas`, `atlas.modules.identity_tenancy.schemas`, `atlas.modules.ingestion.schemas`, `atlas.modules.observability_audit.schemas`
- **Callers** — 17 file(s), 17 import statement(s): `src/aida/access_review_api.py`, `src/aida/api.py`, `src/aida/batch_ingestion.py`, `src/aida/ingestion.py`, `src/aida/operational_api.py`, `src/atlas/modules/connectivity/router.py`, +11 more
- **Named in import-linter contracts** — `catalog module privacy`, `connectivity module privacy`, `identity_tenancy module privacy`, `ingestion module privacy`, `observability_audit module privacy`
- **Removal condition** *(hand-written)* — Same shape as `aida.models`, plus one hard constraint: the moved DTO modules import `ApiModel` back from this file, so the re-export block cannot be removed before `ApiModel` moves somewhere neither side owns. The circular import resolves today only because the block sits below `ApiModel`'s definition.

### aida.catalog_read_model

- **File** — `src/aida/catalog_read_model.py` (73 lines)
- **Replacement path** *(hand-written)* — `compose_catalog_rows` moved to `atlas.modules.catalog.service`; the underscore-prefixed batch helpers moved to `atlas.modules.catalog.repository`, where they are still private.
- **Owner area** *(hand-written)* — Bounded context: catalog
- **Introduced by** — ST-07 Commit A, Phase 5 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 12 name(s) from `atlas.modules.catalog.repository`, `atlas.modules.catalog.service`
- **Callers** — 8 file(s), 8 import statement(s): `src/aida/asset_context.py`, `src/aida/asset_evidence.py`, `src/aida/column_documentation_api.py`, `src/aida/description_withdrawal.py`, `src/aida/model_export.py`, `src/aida/model_import.py`, +2 more
- **Named in import-linter contracts** — `catalog module privacy`
- **Removal condition** *(hand-written)* — Blocked on a decision, not on a count. Four `aida` modules import the underscore-prefixed helpers, which are private in the canonical location too -- so moving those callers to the canonical path would only relocate a private-name dependency, not remove it. The shim can go once those helpers are promoted to named functions on `atlas.modules.catalog.api` and the callers move to that public surface. Until then the shim is the boundary.

### aida.catalog_bulk_actions

- **File** — `src/aida/catalog_bulk_actions.py` (63 lines)
- **Replacement path** *(hand-written)* — The bulk-action constants, DTOs and per-item apply functions all live in the "Bulk actions" section of `atlas.modules.catalog.service`.
- **Owner area** *(hand-written)* — Bounded context: catalog
- **Introduced by** — ST-07 Commit B, Phase 5 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 13 name(s) from `atlas.modules.catalog.service`
- **Callers** — 6 file(s), 6 import statement(s): `src/aida/playbooks.py`, `src/aida/playbooks_api.py`, `src/aida/schemas.py`, `src/aida/stewardship_service.py`, `tests/test_catalog_bulk_actions.py`, `tests/test_catalog_bulk_actions_endpoints.py`
- **Named in import-linter contracts** — `catalog module privacy`
- **Removal condition** *(hand-written)* — The bulk endpoints that dispatch to these functions are scheduled to move into `atlas.modules.catalog.router` under the rest of ST-07. This shim can go once they have, and once `aida.schemas` no longer imports `ALLOWED_CLASSIFICATIONS` from it -- that import is what puts the shim on the transitive path of nearly every module in the tree.

### aida.workspace_api

- **File** — `src/aida/workspace_api.py` (75 lines)
- **Replacement path** *(hand-written)* — Handlers live in `atlas.modules.identity_tenancy.router`. The router *object* should be taken from that module's `api` face instead, which is what the module-privacy contract expects an app-assembly file to import.
- **Owner area** *(hand-written)* — Bounded context: identity_tenancy
- **Introduced by** — ST-07 Commit C, Phase 5 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 16 name(s) from `atlas.modules.identity_tenancy.router`
- **Callers** — 1 file(s), 1 import statement(s): `src/aida/main.py`
- **String references** — 1 file(s): `tests/test_inv4_authorization_wiring.py`
- **Named in import-linter contracts** — `identity_tenancy module privacy`
- **Removal condition** *(hand-written)* — `aida.main` mounts the router through this path. It can go once `main.py` imports the router from `atlas.modules.identity_tenancy.api` (the module's public face -- not `.router`, which the module-privacy contract protects) the way it already does for catalog and connectivity, and once no test imports a handler function from here.
- **Note** — Re-exports every handler, not only the ones with callers, so a future test that wants to bypass HTTP does not have to change import paths first.

### aida.ingestion_api

- **File** — `src/aida/ingestion_api.py` (57 lines)
- **Replacement path** *(hand-written)* — Handlers live in `atlas.modules.ingestion.router`; the router object should come from that module's `api` face.
- **Owner area** *(hand-written)* — Bounded context: ingestion
- **Introduced by** — ST-07 Commit C, Phase 5 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 16 name(s) from `atlas.modules.ingestion.router`
- **Callers** — 2 file(s), 2 import statement(s): `src/aida/main.py`, `tests/test_in2_batch_controls.py`
- **Named in import-linter contracts** — `ingestion module privacy`
- **Removal condition** *(hand-written)* — Same as `aida.workspace_api`: `main.py` mounts through it, and `tests/test_in2_batch_controls.py` imports four batch-control handlers from it directly to test transitions without HTTP. Both have to move.

### aida.observability_api

- **File** — `src/aida/observability_api.py` (42 lines)
- **Replacement path** *(hand-written)* — Handlers live in `atlas.modules.observability_audit.router`; the router object should come from that module's `api` face.
- **Owner area** *(hand-written)* — Bounded context: observability_audit
- **Introduced by** — ST-07 Commit C, Phase 5 of Docs/40-engineering/06-refactor-plan.md
- **Re-exports** — 6 name(s) from `atlas.modules.observability_audit.router`
- **Callers** — 4 file(s), 4 import statement(s): `src/aida/main.py`, `tests/test_cost_showback.py`, `tests/test_worm_archive_lifecycle.py`, `tests/test_worm_archive_wiring.py`
- **Named in import-linter contracts** — `observability_audit module privacy`
- **Removal condition** *(hand-written)* — Same as `aida.workspace_api`: `main.py` mounts through it, and two tests import handler functions from it directly. Both have to move.

### _api_append.ts

- **File** — `ui-next/src/lib/_api_append.ts` (7 lines)
- **Replacement path** *(hand-written)* — `ui-next/src/lib/api/glossary.ts`, or `ui-next/src/lib/api.ts`, which re-exports it and is the intended import surface for screens.
- **Owner area** *(hand-written)* — Experience shell (ui-next)
- **Introduced by** — R05, Docs/review-2026-09-05
- **Re-exports** — 1 name(s) from `./api/glossary`
- **Callers** — 6 file(s), 17 import statement(s): `ui-next/src/lib/api.test.ts`, `ui-next/src/screens/BusinessMeaningDialogs.tsx`, `ui-next/src/screens/BusinessMeaningScreen.test.tsx`, `ui-next/src/screens/BusinessMeaningScreen.tsx`, `ui-next/src/screens/ContextProductsScreen.test.tsx`, `ui-next/src/screens/ContextProductsScreen.tsx`
- **Removal condition** *(hand-written)* — Zero importers of the `_api_append` specifier remain -- screens and their tests import it directly today. Unlike the Python shims this one has no out-of-repo consumer and no dynamic-import path, so for the frontend a measured zero is close to sufficient; the bundler resolves specifiers statically and `tsc --noEmit` fails on a missing one.

### _cross_source_api.ts

- **File** — `ui-next/src/lib/_cross_source_api.ts` (7 lines)
- **Replacement path** *(hand-written)* — `ui-next/src/lib/api/crossSource.ts`, or the `ui-next/src/lib/api.ts` barrel that re-exports it.
- **Owner area** *(hand-written)* — Experience shell (ui-next)
- **Introduced by** — R05, Docs/review-2026-09-05
- **Re-exports** — 1 name(s) from `./api/crossSource`
- **Callers** — 5 file(s), 5 import statement(s): `ui-next/src/components/CrossBoundaryGrants.tsx`, `ui-next/src/screens/CrossSourceScreen.test.tsx`, `ui-next/src/screens/CrossSourceScreen.tsx`, `ui-next/src/screens/UnifiedLineageScreen.test.tsx`, `ui-next/src/screens/UnifiedLineageScreen.tsx`
- **Removal condition** *(hand-written)* — Same as `_api_append.ts`: zero importers of the specifier.

### _column_documentation_api.ts

- **File** — `ui-next/src/lib/_column_documentation_api.ts` (7 lines)
- **Replacement path** *(hand-written)* — `ui-next/src/lib/api/columnDocumentation.ts`, or the `ui-next/src/lib/api.ts` barrel that re-exports it.
- **Owner area** *(hand-written)* — Experience shell (ui-next)
- **Introduced by** — R05, Docs/review-2026-09-05
- **Re-exports** — 1 name(s) from `./api/columnDocumentation`
- **Callers** — 6 file(s), 6 import statement(s): `ui-next/src/components/ColumnPanel.test.tsx`, `ui-next/src/components/ColumnPanel.tsx`, `ui-next/src/components/DescriptionActionDialog.tsx`, `ui-next/src/components/WorkbookImport.test.tsx`, `ui-next/src/components/WorkbookImport.tsx`, `ui-next/src/screens/SourcesScreen.tsx`
- **Removal condition** *(hand-written)* — Same as `_api_append.ts`: zero importers of the specifier.

## Discovery guard

The generator also *finds* shim-shaped files — a Python module whose docstring
announces a backward-compatible re-export, a Python module that re-exports
`atlas` names in the explicit `X as X` form, or a TypeScript file whose entire
body is a comment plus `export * from` — and fails `--check` if one is not in
the register. A new shim therefore cannot be added without appearing here.

Files the discovery pass matches that are **not** compatibility seams. An entry
here that the pass stops matching also fails `--check`, so this list cannot
quietly outlive its subject:

- `src/atlas/modules/catalog/api.py` — the module's PUBLIC interface. Re-exporting `router` here is what keeps `aida.main` from reaching past `api.py` into the contract-protected `router.py`.
- `src/atlas/modules/connectivity/api.py` — same: the module's PUBLIC interface.

Two near-misses worth knowing about, neither of them a shim:

- `ui-next/src/lib/api.ts` ends with four `export * from` lines but is not
  matched, because the rest of the file is real code. It is the canonical client
  barrel — the intended import surface for screens — not a compatibility path.
- Three of the five bounded contexts' `api.py` files do not re-export their
  router, so they are not matched either. That is not tidiness: `aida.main` still
  mounts those three routers through the `aida.*` shims above, which is exactly
  what their removal conditions say has to change first.
