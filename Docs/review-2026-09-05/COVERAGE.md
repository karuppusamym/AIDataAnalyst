# Review coverage and verification record

Initial review: 5 September 2026, baseline `3799457`. Documents restored/completed: 6 September 2026. Workspace revision seen on resumption: `e4a3cfb`. The existing remediation tracker and unrelated work were preserved.

## 1. Scope and limitations

The review combined repository inventory, targeted source reading, all-route action/structure inspection, import/function analysis, architecture/configuration review, and lightweight executable checks. It covers the project at subsystem and screen level. It does **not** claim manual examination of every line or production security certification.

File discovery used `rg --files`, respecting root/nested `.gitignore`. Dependency/vendor trees, private `.env` contents, caches, scratch, and generated artifacts were excluded as application source. Test files were excluded from substantive review and no test suite was run. Generated Vitest timestamp filenames were counted but not read. Runtime SDK/Studio validation remains application functionality even when names refer to tests.

Generated schemas/types were inspected selectively; the approximately 94,000-line OpenAPI baseline was not dumped. Historical/superseded/competitor documents were not exhaustively read. There was no external market research or current dependency-vulnerability assessment.

The reviewer made documentation-only changes. During the first review, independent semantic_api formatting and test edits appeared; only the source diff was inspected, and both were preserved. On resumption, later commits and a remediation tracker existed but the four referenced report files were absent. They were restored without overwriting the tracker or unrelated scripts/source. Original measurements/line references remain baseline evidence; subsequent remediation requires separate verification.

## 2. Inventory

Counts include comments/blank lines and non-test text files, excluding test directories/files, vendor code, fixture-named files, lockfiles, and timestamp artifacts in the measurement script. Size does not establish delivered capability.

| Tree | Files | Lines |
|---|---:|---:|
| `src/aida` | 260 | 111,879 |
| `src/atlas` | 62 | 9,420 |
| `ui-next/src` | 134 | 40,108 |
| `ui` at baseline | 27 | 6,461 |
| `sdk` | 6 | 509 |
| `scripts` | 20 | 7,325 |
| `migrations` including environment/template | 145 | 15,483 |

Additional measurements:

- 40 navigation entries; 41 screen TSX files including the embedded ownership banner.
- 451 HTTP route decorators in AST analysis: declaration count, not certified distinct live endpoints.
- 60 tracked generated Vitest timestamp files.
- One static Alembic head: `e4b8d2f71a95`.
- 155 direct importers of aida.models; 89 of aida.schemas.
- Largest files: models 5,744 lines; schemas 3,936; intelligence_api 3,148; semantic_api 2,789; mcp_server 2,565; React types 4,943; React api 4,576.

## 3. Subsystem coverage

| Subsystem | Evidence / inspection | Outcome |
|---|---|---|
| Shell and scope | App/main, persona/org/scope, navigation/location hooks, primitives | Link/state/auth/status/accessibility infrastructure gaps |
| All React routes | Headings, functions/actions/API calls, targeted workflow bodies | 40-route matrix, consolidation and seven journeys |
| Legacy UI | File/entrypoint inventory, deployment/docs | Baseline active surface; later removal decision acknowledged |
| Identity/workspace | Security/OIDC/persona/settings/gate references | Browser contract gap, shadow posture, vocabulary consistency |
| Ownership | Emitters/handlers, callers and process reachability | Missing lifecycle trigger candidate |
| Governance | Single/bulk/shared decisions, diffs, read model, notifications | Concurrency, dispatch complexity, N+1, delivery durability |
| Catalog/evidence | Catalog screen, evidence links, module/read-model boundaries | Asset workspace and sharing improvements |
| Semantics/glossary | APIs/diffs, frontend workflow, generated/handwritten types | Metric clarity, broader diffs, reduced coupling |
| Connectivity | Registry/capabilities, connectors, Sources/Admin configuration | First-source journey and real certification distinction |
| Ingestion/workflows | Retry/heartbeat/control patterns, batches, Operations | Preserve durable processing; improve recovery and stage visibility |
| Query/SQL | Gateway imports/gates, authorization, response schemas | Preserve execution authority; present returned results |
| Agents/retrieval | Function/import analysis, orchestration/retrieval structure, supervision | Stage decomposition and task-first UX |
| Tools/plans/SDK | Screen workflows, schemas, SDK/package/build declarations | Parameter/version journey and packaging alignment |
| Model governance | Settings/route controls and AI actions | Configured/approved/active/healthy distinction |
| Lineage/relationships | Graph composition/projector and narrated/unified/cross-source screens | One investigation workspace, bounds and evidence |
| Transformations/BI | Artifacts/parser references, transformation and access screens | Source lifecycle placement and lineage provenance |
| Quality | Incident UI, trust/freshness references | Evidence-based incident recovery and honest unknowns |
| Products/context/MCP | Marketplace/context/gateway, server mount, proxies | Publication-to-consumption and production endpoint gap |
| Audit/SIEM | Complete archive/SIEM functions, funnel, loop | No transport, narrow integrity, progress omissions |
| Notifications | Relay/transport/session lifecycle | Durable intent, retries, receipts and commit ownership |
| Events/projections | Outbox and graph projector, process entrypoint graph | Idempotency, memory/batching/lag targets |
| CI/deployment | Dockerfiles, Compose/Kubernetes references, workflow jobs, packages | Existing gates plus missing runtime certification |
| Documentation | README/status/product/refactor references, broken links | Capability truth register and generated checks |

Structural/reference inspection is not exhaustive validation of every algorithm within a subsystem.

## 4. Checks actually performed

These results belong to the initial review window. They are not a claim that later code changes were all retested.

| Check | Result | Limit |
|---|---|---|
| `npm.cmd run build` in ui-next | PASS: TypeScript and Vite build, 163 modules | No interactive/API certification |
| `python -m mypy src sdk/aida_tool_sdk --no-incremental` | PASS: 333 source files; unused override warnings for asyncpg/pytds submodules | Static typing only |
| `python -m ruff check src sdk --output-format concise` | Final PASS; initial two E501 errors corrected by independent formatting edits | Reviewer applied no source fix |
| AST parsing/function sizes/import graph | Completed | Static imports, including delayed/type-only edges |
| Backend reachability from main/worker/scheduler/projectors/SDK | Completed | Importable is not invoked/delivered; offline cases need interpretation |
| Frontend literal-import reachability | Completed | Candidate signal; nonliteral/test/story consumers not certified |
| Exact AST body duplicate scan | Two meaningful duplicate pairs | Not all semantic duplication |
| Static migration graph | One head | No PostgreSQL upgrade/schema-drift execution |
| Isolated archive checksum reproduction | Different synthetic audit content yielded the same checksum | Extracted real functions; no live archive service |
| README local-link existence | Two missing paths | Not every documentation link |
| Local service probes | 5174 served Vite HTML; 3000/3001 unavailable; 8000 closed without healthy response | Environment availability, not production diagnosis |
| Browser-control attempt | No browser available | No screenshots, responsive, keyboard, screen-reader or live journey certification |

The checksum reproduction changed organization, principal, resource, and details while preserving event ID/action/time. It reported `different_audit_content=True`, `same_checksum=True`. It used no credentials/business data.

Restoration verification checks document existence, relative links, all 40 route entries, and stable finding/backlog IDs. The latest remediation tracker is kept separate rather than automatically treated as passed evidence.

## 5. Target-environment verification still needed

- Real OIDC/session and least-privilege journeys through the deployment proxy.
- Concurrent governance transitions against PostgreSQL.
- Archive write/retrieve/retention/hold receipts and full-envelope verification.
- SIEM/notification receipts, durable retries and deduplication during outages.
- Effective workspace enforcement across REST/MCP/exports/jobs.
- Connector capabilities against representative engines/versions.
- Correct approved results, masking, refusals, quality signals and live-model quality.
- Browser keyboard/screen-reader/contrast/responsive visual QA.
- Large-estate load/soak, tenant fairness and recovery measurements.
- Actual wheel/image contents, current dependency vulnerabilities and network controls.

These boundaries define what the review can establish while preserving the directly evidenced findings.
