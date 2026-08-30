# Repository Layout

> Status: Authoritative target. Owner: Engineering.
> The current layout and the migration to it are in `40-engineering/06-refactor-plan.md`.

## 1. Current layout (the problem)

> **Implementation status (2026-08-30).** Line counts re-measured against the tree and
> corrected; the previous figures were roughly half of actual. `src/aida/` is ~36,500 lines
> across 87 modules. Note also what this section omits: `src/atlas/` already exists alongside
> `src/aida/` — `platform/` (four real files) and one scaffold module — so the repository is
> currently in the *middle* of §2's migration, with 1 of 21 modules cut.

```text
src/aida/
├── api.py                    1,837 lines — most of the HTTP surface
├── models.py                 2,721 lines — EVERY module's ORM models
├── schemas.py                2,222 lines — EVERY module's DTOs
├── intelligence_api.py       1,379 lines — three domains tangled
├── semantic_intelligence_api.py, semantic_api.py, semantic_inference.py
├── ingestion_api.py, ingestion.py, batch_ingestion.py
├── agent_orchestrator.py, agent_runtime.py, agent_intelligence.py, agent_evals.py
├── query_gateway.py, sql_guard.py, model_gateway.py, tool_api.py, tool_rendering.py
├── quality_api.py, quality_service.py, data_quality.py, dbt_api.py, dbt_artifacts.py
├── connectors/, projectors/, workflows/
└── security.py, oidc.py, secrets.py, config.py, db.py, main.py
```

Three specific defects: no enforceable boundaries (any module can import any model), high change amplification (one shared migration space), and no extraction seam.

## 2. Target layout

> **Implementation status (2026-08-30). Target.** Built today, of the tree below:
> `src/atlas/platform/` (`config.py`, `db.py`, `context.py`, `logging.py` — the `telemetry/`,
> `errors/`, `pagination/`, `idempotency/`, `outbox/`, `workflow/` and `http/` packages do not
> exist), and `src/atlas/modules/identity_tenancy/` as a 69-line scaffold. `src/atlas/entrypoints/`
> does not exist — entrypoints are `aida.main`, `aida.workflows.worker`,
> `aida.workflows.scheduler`, `aida.projectors.outbox_publisher` and
> `aida.projectors.graph_projector`. `tests/` is flat: 44 files, 339 test functions, no
> `invariants/`, `integration/`, `contract/`, `performance/` or `fixtures/` subdirectories.

```text
atlas/
├── pyproject.toml                  # deps, ruff, mypy, import-linter contracts
├── alembic.ini
├── compose.yaml
├── Dockerfile
├── Docs/                           # this documentation set
├── infra/                          # local fixtures, init SQL
├── scripts/                        # verify-local, drills, benchmarks
├── src/atlas/
│   ├── platform/                   # infrastructure — NO domain knowledge
│   │   ├── db/                     # session, unit of work, tenant-scoped repository base
│   │   ├── config/                 # settings, production posture validation
│   │   ├── logging/                # structured logging + scrubbing middleware
│   │   ├── telemetry/              # OpenTelemetry
│   │   ├── errors/                 # error taxonomy and HTTP mapping
│   │   ├── pagination/             # cursor pagination
│   │   ├── idempotency/
│   │   ├── outbox/                 # transactional outbox write + publish
│   │   ├── workflow/               # Temporal client and worker scaffolding
│   │   └── http/                   # app assembly, middleware, dependency wiring
│   ├── modules/
│   │   ├── identity/               # 01
│   │   ├── connectivity/           # 02
│   │   ├── ingestion/              # 03
│   │   ├── catalog/                # 04
│   │   ├── profiling/              # 05
│   │   ├── relationships/          # 06
│   │   ├── semantics/              # 07
│   │   ├── glossary/               # 08
│   │   ├── lineage/                # 09
│   │   ├── knowledge_graph/        # 10
│   │   ├── quality/                # 11
│   │   ├── retrieval/              # 12
│   │   ├── agent_runtime/          # 13
│   │   ├── tools/                  # 14
│   │   ├── model_gateway/          # 15
│   │   ├── query_gateway/          # 16
│   │   ├── governance/             # 17
│   │   ├── studio/                 # 18
│   │   ├── context_products/       # 19
│   │   ├── observability/          # 20
│   │   └── experience/             # 21 (server side)
│   └── entrypoints/
│       ├── api.py                  # atlas-api
│       ├── worker.py               # atlas-worker
│       ├── projector.py            # atlas-projector
│       └── scheduler.py            # atlas-scheduler
├── ui/                             # Atlas portal
└── tests/
    ├── invariants/                 # INV-1..INV-9 — the safety net
    ├── integration/                # cross-module, real database
    ├── contract/                   # OpenAPI diff, event schemas, SDK
    ├── performance/                # regression gates
    └── fixtures/                   # synthetic banking datasets
```

## 3. Module internal layout

> **Implementation status (2026-08-30). Target, realised once.** Only
> `src/atlas/modules/identity_tenancy/` has this shape, and it is a scaffold: `service.py` is
> 7 lines. It has no `migrations/` directory — all 34 Alembic revisions live in the
> repository-root `migrations/versions/`. `scripts/generate_module.py` generates this shape.

Identical for every module — uniformity is what lets an engineer work in any module on day one.

```text
modules/<name>/
├── __init__.py
├── api.py            # PUBLIC
├── contracts.py      # PUBLIC
├── router.py
├── service.py
├── models.py         # PRIVATE
├── schemas.py        # PRIVATE
├── repository.py     # PRIVATE
├── events.py
├── workers/
├── migrations/
└── tests/
```

## 4. Entrypoints

One image, four entrypoints (ADR-0011).

| Entrypoint | Mounts | Runs |
|---|---|---|
| `api.py` | All module routers + MCP server | Uvicorn |
| `worker.py` | Module `workers/` packages | Temporal worker, task queues per worker class |
| `projector.py` | Projection consumers | Kafka consumer group |
| `scheduler.py` | Fleet scheduler | Leader-elected loop |

## 5. Test placement

> **Implementation status (2026-08-30). Target.** `pyproject.toml` sets
> `testpaths = ["tests"]` and `tests/` is flat — none of the four directories below exists,
> and `pytest src/atlas/modules/<name>` is not a supported invocation. The one module-local
> test file, `src/atlas/modules/identity_tenancy/tests/test_module_scaffold.py`, is outside
> the collected path.

| Test kind | Location | Runs |
|---|---|---|
| Module unit | `modules/<name>/tests/` | Standalone, other modules faked |
| Invariant | `tests/invariants/` | Every CI run — never skipped |
| Integration | `tests/integration/` | Real database, real module wiring |
| Contract | `tests/contract/` | OpenAPI diff, event schema, SDK suite |
| Performance | `tests/performance/` | Regression gates |

## 6. Naming conventions

| Thing | Convention | Example |
|---|---|---|
| Module package | `snake_case`, singular or natural plural | `catalog`, `tools` |
| Database schema | Matches the module package | `catalog`, `tools` |
| Table | `snake_case` singular | `table_annotation` |
| DTO | `<Thing>DTO` | `TableDTO` |
| Event type | `<domain>.<entity>.<past_tense>` | `catalog.object.changed` |
| ID prefix | Short, unique per type | `tbl_`, `ds_`, `run_`, `tool_` |
| Temporal workflow | `PascalCase` | `BatchIngestion` |
| Metric | `atlas_<module>_<measure>_<unit>` | `atlas_catalog_objects_total` |

## 7. Dependency management

| Rule | Detail |
|---|---|
| Pinned | Lockfile committed; digests for images |
| Minimal | A new dependency needs a justification in the PR |
| Audited | SBOM per build; vulnerability policy fails on critical |
| Layer-appropriate | `platform/` may not depend on domain packages |

## Related documents

- Module decomposition: `10-architecture/04-module-decomposition.md`
- Refactor plan: `40-engineering/06-refactor-plan.md`
- Coding standards: `40-engineering/03-coding-standards.md`
