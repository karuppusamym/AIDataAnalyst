# Accomplishment Log

> Status: **Append-only ledger.** Owner: Engineering lead.
> Records material implementation outcomes, decisions, verification evidence, and known limitations, in date order. Entries are never edited or removed — a correction is a new entry.
>
> Migrated unchanged from the retired flat `10-accomplishment-log.md` on 2026-08-28. Forward-looking status lives in `60-delivery/04-status-matrix.md`; open work lives in `60-delivery/03-tracker.md`.

## Entry conventions

- One section per date; sub-sections per release or workstream.
- Record what was **verified**, not what was intended — including the identifiers of runs, batches, and executions that prove it.
- Record known limitations in the same entry as the achievement. An entry that claims completion without naming what remains open is incomplete.

---

## 2026-08-30

### Phase 0 — "make the invariants true" (independent architecture review, `Docs/review-2026-08/`)

#### Completed

**Continuous integration now exists** (tracker ST-02, closed). `.github/workflows/ci.yml`
adds five gates across three jobs: `ruff`, `mypy` (strict), `lint-imports`, an
exactly-one-Alembic-head guard, and `pytest`. `UV_FROZEN=1` makes a stale `uv.lock` itself a
failure. Before this date there was no pipeline at all, while `40-engineering/03-coding-standards.md`
and `30-contracts/01-contract-strategy.md` both stated that checks "fail CI."

*Verified*, in a clean checkout outside the working tree, using the exact CI recipe
(`uv sync --frozen --extra dev`): ruff clean; mypy clean across 106 source files; 3 import
contracts kept, 0 broken; 1 Alembic head (`e6d5b8c6bcef`); **387 tests passing**.

**INV-2 gateway exclusivity is now enforced rather than asserted** (tracker QG-7, closed;
ADR-0004's named mechanism, outstanding since that ADR was accepted). The SQL-accepting pair
`estimate_read_query` / `execute_read_query` was moved off the `Connector` ABC onto a new
`aida.connectors.sql_execution.SqlExecutor`. `ConnectorRegistry.create` still returns
`Connector`, which now has no SQL-accepting member. `aida.connectors.execution_access` is the
sole source of a `SqlExecutor`, and the import-linter contract *"INV-2 connector SQL execution
is reachable only from the query gateway"* permits exactly one importer.

*Verified by making it fail, not only by making it pass:*

- Adding `from aida.connectors.execution_access import ...` to `aida.api` breaks the contract:
  `Illegal imports of protected package aida.connectors.execution_access: aida.api -> ... (l.22)`.
- Calling `connector.execute_read_query(...)` on a registry-produced connector is rejected by
  mypy: `"Connector" has no attribute "execute_read_query" [attr-defined]`.
- Both probes were reverted; the tree is clean.

The Tier-0 AST scan (`test_no_connector_execution_outside_gateway`) was widened to cover
`estimate_read_query` as well — it takes a caller-supplied statement exactly as
`execute_read_query` does — and a new test,
`test_the_connector_handed_to_the_platform_has_no_sql_surface`, fails if the methods are ever
moved back onto `Connector`, a change that would leave the import contract and the AST scan
passing while the type-level guarantee silently disappeared.

**The `09` ↔ `16` import cycle does not exist** (tracker ST-11, closed). Checked against the
code before redesigning anything: `query_gateway.py` imports no lineage module, no lineage
module imports the gateway, and `extract_column_lineage` is defined inside `query_gateway.py`
and called only there. The mutual edge was an error in the module register in
`10-architecture/04-module-decomposition.md` §3/§4, not a property of the import graph. Rule
recorded: **the gateway emits, intelligence modules consume.** No layer diagram redraw needed.

**Pre-existing gate failures fixed so CI is green on its first run** rather than red on
arrival: 6 ruff errors (4 × E501, 2 × unsorted imports) and 2 mypy errors.

#### Found while doing the above

- **`PyYAML` was an undeclared dependency.** `src/aida/context_compiler.py` imports `yaml`;
  nothing in `pyproject.toml` declared it, and it resolved only transitively. Now declared
  (`PyYAML==6.0.3`) with `types-PyYAML` in the dev extra. `uv.lock` regenerated — it had also
  been missing the dev extras entirely, so `import-linter` was not in the lockfile.
- **`domain_service.resolve_domain` returned `DataDomain | None` against a `DataDomain`
  annotation.** An unresolvable `data_domain_id` returned `None` for callers to dereference.
  Now raises (INV-4, fail closed) rather than returning a value the type says cannot occur.

#### Known limitations — explicitly still open

- `bandit`/SAST and `pip-audit` are named as CI gates in `03-coding-standards.md` and are
  **not wired**; the tools are not in the `dev` extras. Marked as such in that table.
- The import-linter contracts cover three narrow, real invariants. There is still **no layering
  or independence contract over the flat `aida` package** — that lands with decomposition
  (ST-05/06/07), all of which remain TODO.
- CI has never actually run: this workflow file has not yet been pushed to a remote. The recipe
  was verified locally in a clean checkout, which is not the same as a green run on GitHub.
- Five of the nine Tier-0 invariant tests remain unformalised (INV-1, 5, 6, 7, 9), for the
  reasons the test module's own docstring gives. Unchanged by this work.
- Every operational drill remains **never run**. Unchanged by this work.

### Decisions

**ADR-0018 accepted; ADR-0017 superseded before acceptance.** Access, classification and
technical hierarchies are modelled as three independent axes, and only access grants. Tenancy
becomes `organization → workspace`; `line_of_business` and `data_domain` become effective-dated
`business_node` classification records with many-to-many assignments; policy becomes
attribute-based and keys on classification. `legal_entity` is withdrawn rather than deferred —
it has never existed in the schema. ADR-0017's `cross_boundary_grant` mechanism is retained.

The triggering argument is ADR-0017's own recorded reversal condition — *"domain taxonomy turns
out not to nest cleanly (a table genuinely needs two sibling domains)"* — which is structurally
met in a bank estate rather than being a future risk. **No migration code has been written; the
schema is unchanged.**

---

## 2026-08-24

### Completed

- Reviewed the original architecture, metadata engine, runtime, data model, security, operations, and backlog documents.
- Reframed delivery for a large regulated bank with multiple LOBs and thousands of databases.
- Selected a hybrid deterministic/LLM architecture.
- Selected Temporal for durable enterprise workflows and Kafka for replayable integration/projection events.
- Kept the analytical agent runtime framework-neutral and established a typed state-machine boundary.
- Established PostgreSQL as authoritative and Neo4j/vector/search as rebuildable projections.
- Established the Query Execution Gateway as the mandatory choke point for generated queries, approved tools, and platform workloads.
- Confirmed local Docker Desktop, Docker Compose, Python, and Node prerequisites.

### In progress

- Production-oriented repository and local platform scaffold.

### Known limitations

- Formal bank infrastructure, identity, regulatory, residency, source inventory, RPO/RTO, and model-hosting requirements are not yet available. Working assumptions are recorded in `08-enterprise-assumptions-decisions.md`.

## 2026-08-25

### Completed

- Created the Python 3.13/FastAPI repository, pinned dependencies, Alembic migrations, non-root image, tests, linting, and strict type checking.
- Started a persistent Docker platform with PostgreSQL/pgvector, Temporal and UI, Redpanda and console, Neo4j, Redis, MinIO, API, migration job, metadata worker, transactional outbox publisher, graph projector, and sample banking source.
- Implemented organization, LOB, project, and datasource tenancy with organization enforcement on resource access.
- Implemented explicit development identity, production fail-closed configuration, role gates, credential references, structured audit records, correlation IDs, health probes, and Prometheus metrics.
- Implemented the connector SDK and PostgreSQL adapter for connection testing, discovery, EXPLAIN, read-only query execution, and bounded safe profiling.
- Implemented retryable Temporal discovery/profiling with heartbeats, fingerprints, idempotent persistence, deterministic sensitive-column classification, immutable run-scoped profiles, and no persisted source values.
- Implemented PostgreSQL transactional outbox publication to Kafka and idempotent Kafka-to-Neo4j projection.
- Implemented SQLGlot AST controls for one read-only query, mutation/admin/function/join/wildcard denial, enforced limits, catalog authorization, source EXPLAIN cost policy, read-only transaction timeout, conservative masking, and query lineage.
- Implemented a framework-neutral governed agent orchestration envelope with explicit state transitions, semantic/policy pinning, a fail-closed model route, development-only generated-SQL injection, deterministic gates, question hashing, and execution evidence.
- Added metadata inventory, safe profile, graph summary, SQL validation, governed execution, and agent-analysis APIs.
- Added the local operations runbook, automated end-to-end verifier, and prioritized enterprise gap register.

### Verification evidence

- Static checks: Ruff clean; strict mypy clean across 27 source files; 23 automated tests passing; Alembic reports no model drift at revision `3df18be7a420`.
- Durable metadata run `0ddf4a63-6e4e-4dc2-9802-197bb12a365f` completed through Temporal: 1 catalog, 2 schemas, 4 tables, 22 columns, 4 table profiles, and 22 column profiles.
- Agent run `c36df4f3-4334-48f1-9441-319296a24575` completed with semantic snapshot pinning and a 10-state trace.
- Query execution `c20e04a4-d782-4988-b9f5-77cdacc6d9ea` passed AST, catalog, EXPLAIN, cost, and execution gates; `customer_name` and `email_address` were masked.
- Wildcard, mutation, and uncatalogued-table test queries were denied with HTTP 422 before source execution.
- A missing approved model route was denied with HTTP 503; the runtime did not silently substitute an unapproved provider.
- Readiness confirmed PostgreSQL and Temporal; Neo4j contained 1 catalog, 2 schemas, 4 tables, and 22 columns for the initial tenant.
- Final isolated verifier run `d0ee311b-f3a6-4319-82d6-6c8965d61f3f` discovered 7 source constraints; the reconciled graph reported 7 constraint nodes, 3 foreign-key relationships, and zero object-count lag.
- Verified sensitive-expression lineage masking for renamed and derived outputs; persisted query evidence redacts literals and user/query fingerprints use a keyed HMAC.
- Verified a non-admin principal from another organization receives HTTP 403 when probing a foreign datasource.

### Current limitations

- The model gateway is intentionally disabled until a bank-approved route and AI governance controls are supplied.
- PostgreSQL is the first certified connector; other engines require adapters and certification fixtures.
- Local Docker services are single-node engineering infrastructure, not the target HA deployment.
- OIDC, vault, production ABAC, source-delegated identity, production topology, and DR evidence remain production gates in `12-enterprise-gap-register.md`.

### Enterprise functionality iteration

- Added active/deprecated lifecycle state and tombstone timestamps across catalogs, schemas, tables, columns, and constraints. Re-scans now report created, changed, and deprecated object counts and reactivate stable identities when an object returns.
- Added analysis-run cancellation, terminal-state reconciliation against Temporal, resume-as-new-history, manual/scheduled/resume trigger evidence, priorities, and organization-wide run inventory.
- Replaced monolithic source profiling with a Temporal table-task DAG. Table profiles retry independently, remain idempotent, and execute in batches bounded by each source's configured concurrency.
- Added source disable/enable administration and fail-closed enforcement across scans, direct queries, agent analyses, and governed tool executions.
- Added HA-safe scan-policy scheduling with database row locks, organization quotas, one active scan per source, priority ordering, maintenance windows, backpressure deferral, and a dedicated scheduler service.
- Added governed semantic model and metric versions, immutable physical mappings, draft/review/publish/supersede/reject states, maker-checker separation, and clone-based rollback.
- Added governed reusable tool versions with AST-validated SQL templates, exact parameter contracts, AST literal binding, role intersection, semantic pinning, approval, version supersession, HMAC parameter fingerprints, dependency capture, and mandatory query-gateway execution.
- Added value-free query-memory evidence and owner feedback. Raw questions and comments are not persisted; keyed hashes are used, and negative feedback suppresses evidence.
- Added bounded metadata-only relationship candidate generation, durable negative knowledge, inspectable evidence, maker-checker decisions, and no automatic promotion to source truth.
- Added physical-table impact analysis across semantic metrics, governed tools, and approved inferred relationships.
- Added fleet summaries, organization run inventory, filtered audit evidence, exponential outbox retry state, dead-letter visibility, and an authorized requeue path.

### Verification evidence — enterprise iteration

- Static checks: Ruff clean; strict mypy clean across 34 source files; 39 automated tests passing; Alembic reports no model drift at revision `f16bd8c935a4`.
- All migrations from `3df18be7a420` through the new schema-drift, semantic, tool, scheduling, intelligence, and outbox revisions applied transactionally to the running PostgreSQL service.
- End-to-end verifier run `644d943d-39e4-47b0-a134-fb73e79cf8da` passed the healthy Atlas portal, organization/LOB/project/source creation, credential-safe portal inventory, connection validation, the table-task Temporal workflow, sensitive masking, mutation denial, feedback and memory, semantic maker-checker publication, governed tool publication/execution/deprecation, impact analysis, relationship review, scheduling, audit/fleet evidence, source disablement, and graph reconciliation.
- Manual analysis run `3f189221-c96c-402f-96a1-82953accc62a` and scheduler-admitted run `7a002e55-be12-4f07-b42d-5c0897d42b50` both completed with 4 tables, 22 columns, 7 constraints, 4 table profiles, and 22 column profiles.
- Governed tool execution `fd8e3e89-5ddd-4e79-895a-9a3d6afef36a` completed through the same AST, catalog, EXPLAIN, cost, timeout, masking, and evidence gateway used by agent-generated SQL; its approved deprecation then prevented further execution with HTTP 409.

### Portal and status transparency

- Added the Atlas operational portal as a Docker service at `http://localhost:3000` with live organization selection, fleet overview, registered sources, scan actions, source enable/disable controls, run histories, pending governance reviews, audit evidence, implementation status and architecture decisions.
- Added tenant-safe list APIs for organizations, LOBs, projects and credential-reference-free datasource summaries so the portal can navigate the hierarchy without bypassing organization controls.
- Added `14-implementation-status-matrix.md` as the single implemented/partial/pending/retest/bank-decision status source and recorded the LangGraph/ADK, hybrid execution, Temporal, Kafka, PostgreSQL, query-gateway and data-minimization decisions.
- Built and started the portal container successfully. Both API and portal report healthy; the live portal proxy returned 9 organizations and the selected fixture reported 1 LOB, 2 completed runs, 36 audit events, and zero dead-letter events. Datasource summaries were verified not to expose credential references.
- Extended `scripts/verify-local.ps1` so future end-to-end runs require a healthy Atlas UI, validate its product title, traverse the portal's API proxy, and assert credential references are absent from datasource inventory responses.

### Agentic product portal iteration

- Promoted Atlas from an operational status slice to an agentic product portal. The default workspace now accepts a business question and controlled candidate SQL, executes the existing hybrid agent runtime, renders masked results, and shows the complete stage/control trace and durable run history.
- Added live workbenches for metadata/table profiles and downstream impact, semantic-model drafts and metric inspection, governed-tool catalog and parameter execution, inferred-relationship discovery and checker decisions, scan-policy scheduling, model/runtime governance, source fleet operations, maker-checker review, and audit evidence.
- Added credential-safe agent-run list/detail APIs. Raw question text and its HMAC digest are not exposed by the history contract.
- Added a live AI runtime posture API that reports the framework-neutral typed state machine, hybrid orchestration, nine deterministic gates, optional LangGraph/Google ADK adapter posture, development-route configuration, and the intentionally unconfigured production model route.
- Kept the model boundary fail closed. The UI clearly labels the SQL candidate path as a controlled development route and does not imply that an LLM is active.
- Extended the local verifier to require the agentic product title and execution-trace surface, verify the live `HYBRID/NOT_CONFIGURED` runtime posture, and confirm agent history through the portal proxy.
- End-to-end verifier run `3faf4656-dfd6-40b1-a301-1f62dc54b50d` passed with UI/API health, 4 tables, 22 columns, 7 constraints, 4/22 table/column profiles, masked `customer_name` and `email_address`, mutation and disabled-source denial, semantic and tool governance, relationship review, scheduling, audit, and current graph projection. Agent run `b0566df0-2d4f-4e17-9c72-05be6e158081` and governed query execution `e1255fdc-3bd8-4457-8223-654f61c3d295` completed.
- Static verification is clean: JavaScript syntax, Ruff, and strict mypy pass; the automated Python suite now has 41 passing tests. Interactive browser visual QA could not run because no browser surface was connected in this session; live HTTP product/proxy checks passed instead.

### R7 governed agent intelligence

- Added organization/source-scoped, value-free lexical retrieval across active technical metadata, published semantic metrics, and published governed tools. Results are bounded, ranked, reason-coded, and never include source-row values.
- Added deterministic approved-tool-first planning with confidence thresholds, role intersection, explicit tool selection, required-parameter clarification, and a safe fallback to the development SQL or approved-model boundary.
- Bound agent execution directly to published tool versions. Parameters continue through strict schema validation and AST literal rendering; only an HMAC parameter fingerprint is persisted, and execution still uses the mandatory query gateway.
- Added durable retrieval evidence, plan evidence, selected-tool reference, and trace details to agent runs. Agent history and analysis responses expose the bounded evidence but not raw questions or their keyed digests.
- Added a provider-neutral structured model gateway with explicit route registration, input/output budgets, timeout enforcement, Pydantic output validation, and non-content fingerprints. No external adapter is registered, so the live runtime remains fail closed.
- Added a durable agent-control evaluation ledger and UI. The initial eight-scenario suite verifies safe reads, mutation/multi-statement/wildcard denial, approved-tool-first planning, role binding, model-route fail closure, and prompt data minimization.
- Enhanced the AI Analyst with plan preview, ranked evidence, plan confidence/reasons, detailed execution-stage evidence, tool-first run history, and the Agents & Models screen with runnable evaluation history.
- Added migration `a7c4e2d91b60`; Alembic reports it as head with no model drift. Ruff, strict mypy, JavaScript syntax, and all 50 automated tests pass.
- Final end-to-end verifier run `4c1320e9-b272-4580-90ac-e04f6de5d357` passed the complete banking fixture. Tool-first agent run `1e634f41-071a-4596-a4c7-362cb01e0b97` selected strategy `GOVERNED_TOOL` with 9 retrieval evidence records, and evaluation run `f7c9ef2a-bbe8-42d4-add7-d90f10598e41` passed at 100%.

### R8 enterprise identity, secret boundaries, and column lineage

- Implemented production OIDC bearer-token verification with signed JWT validation, issuer and audience enforcement, algorithm allowlisting, expiration/issued-at checks, bounded clock skew, cached JWKS retrieval, unknown-key refresh, pinned-JWKS support, configurable claim paths, external-to-platform role mapping, organization claim validation, and generic authentication failures that do not disclose verification internals.
- Production configuration now requires an OIDC issuer, audience, and HTTPS JWKS source or pinned JWKS. It rejects development identity, development SQL override, weak audit keys, and the local environment credential provider.
- Replaced the local-only secret resolver with a provider-neutral, explicitly registered adapter contract supporting provider/version metadata, strict reference parsing, one deployment-approved scheme, bounded in-memory TTL caching, and invalidation for rotation. Inline credentials, unknown providers, traversal-like references, empty values, and provider mismatch fail closed; secret values are never persisted or logged.
- Added durable, value-free query column lineage. Governed SELECT executions now retain referenced columns plus output-to-source mappings, direct/derived classification, and transformation names without expressions or literal values. The same evidence is returned by query/tool/agent responses and the tenant-safe `GET /v1/query-executions/{execution_id}/lineage` API.
- Updated Atlas to release posture R8. Verified results display referenced-column counts and column lineage, while Agents & Models shows the live identity verification mode, selected credential provider/adapter status, and honest enterprise-security activation state.
- Added migration `c8e5f3a20d71`. Ruff, strict mypy across 39 source files, JavaScript syntax, Alembic single-head/no-drift validation, and all 58 automated tests pass.
- Final end-to-end verifier organization `f18ab71f-6ac5-4532-84f3-d44a9922a8c6` passed UI/API health, discovery, profiling, masking, mutation and disabled-source denial, durable column lineage through the UI proxy, feedback/memory, semantic and tool maker-checker governance, tool-first agent execution, evaluation, impact, relationships, scheduling, audit, and graph reconciliation.
- Query execution `60c60d23-6ca9-4e24-bc3c-ca14bec69557` retained five referenced columns and four output lineage records while masking `customer_name` and `email_address`. Tool-first agent `396f7509-c3c3-43e9-a270-a5b29d009d88` used nine retrieval evidence records; evaluation `61373ac5-fa66-4193-8f93-61c4856fc8f7` passed at 100%.

### R8 remaining production gates

- The bank must provide its issuer/claim contract, external group mappings, centralized ABAC decision point, break-glass process, and workload-identity standard before production identity can be activated.
- The bank-selected Vault, CyberArk, AWS Secrets Manager, Azure Key Vault, or GCP Secret Manager adapter must be registered at the deployment composition root and certified for workload identity, rotation, outage behavior, access review, and no-secret telemetry.
- View definition, stored procedure, ETL/OpenLineage, and warehouse-history lineage adapters remain; current column lineage is the governed SELECT execution slice.

## 2026-08-26

### R9 knowledge graph and governed model-route workbenches

- Audited implemented APIs against Atlas and added `15-ui-capability-coverage.md` so product UI, API-only administration, and deployment-only security controls are tracked separately.
- Added a bounded, tenant-safe authoritative knowledge-graph API. It returns named table nodes, declared foreign-key edges, enriched source/target column suggestions, confidence, review status, and value-free evidence with total counts and truncation state.
- Rebuilt the relationship screen as a knowledge-graph workbench with topology cards, declared-versus-suggested visual treatment, edge filters, source/target names, confidence, evidence boundaries, discovery and independent approve/reject actions.
- Added immutable organization-scoped model-route versions covering route key, provider type, model/deployment alias, endpoint alias, credential-reference presence, residency, retention, capabilities, token ceilings and timeout. Credential references are excluded from every read contract.
- Added model-route maker-checker submission and approval. Approval supersedes an older approved version but cannot select a runtime route, enable generation, resolve credentials, or register a private adapter. Atlas exposes `APPROVED_NOT_SELECTED`, `GENERATION_DISABLED`, and `ADAPTER_REGISTRATION_REQUIRED` rather than implying readiness.
- Added model-route registry and authoring UI, effective activation chain, source connection testing, deterministic SQL validation, analysis-run cancel/resume controls and agent helpful/incorrect feedback.
- Added migration `d9f6a4b31e82`. Ruff, strict mypy across 40 source files, JavaScript syntax, Alembic single-head/no-drift validation, and all 61 automated tests pass.
- Final end-to-end verifier organization `51afcf7e-24b7-4295-95f8-d0594ce18108` passed the complete local banking fixture. The graph exposed four nodes, three declared FK edges and two suggestions. Model route `8937c683-5210-4ac1-af22-78215a3cac76` passed maker-checker approval and remained safely `APPROVED_NOT_SELECTED` with no adapter.
- Agent run `d10f69ff-58a7-40b3-9213-6c8e86d7e998` and query execution `26c26be8-4b05-42c4-867c-2764c3e5c8eb` completed with five referenced columns, four lineage outputs and masking of `customer_name` and `email_address`. Tool-first agent `dd0f374b-00e9-4f50-a338-dc0c62350a74` used nine retrieval evidence records; evaluation `2d938ff3-40dd-40ee-8400-7ee708da4403` passed at 100%.

### R10 product completion and enterprise workflow rebuild

- Replaced the dark, flat 12-screen proof-of-concept presentation with a restrained banking product system: role-grouped navigation, operating brief, attention queue, consistent workbenches, contextual detail, accessible dialogs, responsive layouts, explicit control states and a light information-dense visual language.
- Closed the former UI-only gaps end to end: guided organization/LOB/project/source onboarding with immediate connection verification; semantic metric composition and model clone/rollback; governed tool authoring/versioning/publish/execute/deprecation; query-memory inspection; event-delivery exception inventory and requeue; filtered audit and run evidence.
- Replaced per-project portal fleet traversal with bounded tenant-level project and datasource inventory APIs. This removes the hierarchy N+1 request pattern for organizations with many LOBs, projects and database registrations.
- Added a real data-driven canvas topology map with table nodes and distinct declared/suggested edges while retaining the evidence table and independent relationship decision workflow.
- Added a tenant-scoped event inventory that deliberately excludes event payloads. Operators can inspect status, aggregate, attempts and bounded errors and can requeue only dead-letter events through the audited control.
- JavaScript syntax, Ruff and strict mypy are clean; all 63 automated tests pass. The final Docker verifier passed UI/API health, tenant inventory, four tables, 22 columns, seven constraints, masking, column lineage, semantic/tool/model governance, tool-first agents, 100% control evaluation, query memory, graph suggestions, scheduling, audit/outbox evidence and projection reconciliation.
- Final verifier organization `20ba44a2-471e-4895-9c14-614357efdc17` produced analyst run `e4a2cf55-e72b-4cae-b77c-fabf4cc8e275`, query execution `cefc0d6b-8c06-4f2e-8805-c25c7d75ba4f`, tool-first run `c42a69cb-c10b-4d4d-8217-2603f025c90d`, evaluation `bda2c3f0-9018-4c98-8f5c-d2cf03854189`, three declared graph edges and two governed suggestions.

### R11 governed model providers and grounded generation

- Implemented concrete OpenAI Responses and Google Gemini GenerateContent adapters behind the existing provider-neutral model gateway. Both require strict structured JSON output, enforce organization-approved route/model/capability selection, use bounded token and timeout budgets, and retry only transient provider failures.
- Added credential resolution for `env://OPENAI_API_KEY` and `env://GEMINI_API_KEY` in local development without persisting or returning secret material. Provider telemetry retains only route/model/endpoint aliases and non-content fingerprints.
- Replaced the real credentials that had been placed in `.env.example` with placeholders and created the ignored local `.env`. Both exposed credentials must be rotated before any model traffic is enabled.
- Grounded model requests with bounded, organization/source-scoped retrieval evidence plus active qualified tables, column types/classifications and constraints. Raw source values are never added to model context, and generated SQL still passes every deterministic authorization, AST, catalog, cost, timeout, masking and lineage gate.
- Updated Atlas runtime posture and model-route authoring for `OPENAI` and `GOOGLE_GEMINI`; approval still does not activate generation. Local generation remains deliberately disabled until rotated credentials and an approved route are selected.
- JavaScript syntax, Ruff, strict mypy across 40 source files, Alembic no-drift validation, and all 66 automated tests pass. Adapter tests mock provider traffic; no compromised key was used for a live request.
- Final Docker verifier organization `2e2103ee-354f-473c-ac3d-312040f79bc7` passed the complete banking fixture. Analysis run `75350aa7-2ad7-44c4-9338-0f1f85c51f6b`, analyst run `6594561c-14fd-4cc4-957c-702b39d83eac`, query execution `ed6231f1-f359-4828-8eab-390dac34e049`, tool-first run `995ebd40-6cef-4d13-bf28-f5fbb62156f7`, and evaluation `35eaa5be-d5f0-4804-885f-c24014212c25` all completed; the live runtime advertised both model adapters while remaining `HYBRID/NOT_CONFIGURED` and fail closed.

## 2026-08-27

### R12 dbt transformation intelligence and friendly workbench

- Clarified and enforced the operating boundary: dbt compiles and executes transformations in its target warehouse; Atlas imports transformation evidence and never treats dbt artifacts as an execution bypass or a source-extraction engine.
- Added organization/project/source-scoped dbt project registrations and immutable manifest imports. The raw manifest is not retained; bounded resources, dependencies, catalog mappings, metadata, fingerprints and normalized evidence are persisted instead.
- Added support for models, sources, tests, seeds, snapshots, analyses, exposures, metrics, semantic models and saved queries with 32 MiB/25,000-resource/100,000-edge bounds and idempotent manifest fingerprints.
- Compiled SQL is hashed, parsed with the datasource dialect, stripped of comments and rewritten with every literal replaced by a placeholder. Unparseable or oversized SQL is not stored.
- Matched dbt relations deterministically to active catalog tables and added dbt resource IDs to downstream impact. Latest dbt artifacts now participate in value-safe agent retrieval and hydrate the same bounded physical table context used by model generation.
- Added the Atlas **Transformations** workbench: project registration, `manifest.json` upload, immutable history, coverage metrics, resource filters, build/test lineage, catalog status and a literal-redacted SQL evidence viewer. The screen explains what dbt, Atlas and source ingestion each own.
- Added migration `e4b7c2a91d35`. JavaScript syntax, Ruff, strict mypy across 42 source files, Alembic no-drift validation and all 71 automated tests pass.
- Final Docker verifier organization `2433a2b4-74c5-49ba-ab36-22d09f48d2ab` passed the complete banking fixture. dbt project `de45be7a-c14e-4d91-b66f-b429cceef0c6` imported artifact `5bb70996-8db7-4ab0-97eb-41a67a4a63d1` with three resources, two edges and one catalog match; the raw marker literal was absent from every response and `DBT_MODEL` appeared in governed agent retrieval.

### R13 governed business-semantic inference and cross-domain workbench

- Added a metadata-only semantic inference pipeline that goes beyond technical collection. Deterministic rules and an optional organization-approved `CLASSIFICATION` model route create bounded proposals for business names/descriptions, domains, entities, table roles, row grain, synonyms, analytical questions, tags and safe tool blueprints.
- Enforced the AI trust boundary in code: source rows are never loaded into inference context; identifiers are treated as untrusted data; strict Pydantic output contracts reject extra fields; returned table IDs and tool columns are allowlisted; sensitive classifications cannot enter tool blueprints; invalid or unavailable model batches fall back to deterministic proposals; and no LLM-authored SQL is accepted.
- Integrated proposals with the common governance queue and maker-checker separation. Approval creates or updates authoritative organization domains, domain-owned entities and versioned table annotations; rejection is durable negative evidence. All inference, decisions and promotions emit audit/outbox evidence.
- Added a tenant-scoped business map with domain, entity and table nodes plus cross-domain edges derived only from approved annotations and authoritative foreign keys. Approved annotations now participate in governed agent retrieval as `BUSINESS_ENTITY` evidence.
- Added safe tool promotion. Only an approved proposal can be promoted; Atlas rechecks current active/non-sensitive columns, renders SQL deterministically with the source dialect, passes it through the existing SQL/catalog validator, and creates a `DRAFT` tool that still requires the standard publication review.
- Added the Atlas **Business meaning** workbench with source selection, inference execution, rule/model engine posture, proposal queue, common review actions, approved annotation inventory, domain/entity/table map, cross-domain relationships and draft-tool promotion. The Data Catalog table detail now surfaces approved business meaning, and model route configuration exposes `CLASSIFICATION` as a separate metadata-inference capability.
- Added migration `f2c8d5a93e71`. JavaScript syntax, Ruff, strict mypy across 44 source files, Alembic single-head/no-drift validation and all 76 automated tests pass.
- Final Docker verifier organization `1da6e47a-b624-456c-adea-46ce93fc2750` passed the complete banking fixture. Inference run `f41aef7f-12bc-4e36-8027-77411f3cb88f` produced four `RULES_ONLY` proposals; independent approval created two business domains and two entities with one cross-domain FK edge; approved annotation `ec3c3977-50cf-49b2-aba6-edc91cc4d06e` appeared in agent retrieval; and proposal promotion created draft tool version `7d51abf4-7b82-4152-8a6d-1acc45bfe54e` without model-authored SQL.
- The Docker-hosted UI and proxy passed live health/content/API assertions at `http://localhost:3000`. An interactive browser was not connected in this session, so visual click-through and accessibility remain explicitly listed for retest rather than reported as passed.

### R14 bank-safe Graph Explorer V2

- Replaced the fixed 40-node topology slice with a server-backed exploration contract. Authorized users can search active table, schema and catalog names, select a focus table, and expand deterministic `REFERENCES`, `REFERENCED_BY` or bidirectional neighborhoods from one to four hops.
- Added deployment policy ceilings for traversal depth, returned nodes and returned edges. Every neighborhood reports the requested scope, returned counts and explicit truncation reasons; excessive depth/size requests fail closed instead of shifting unbounded work into PostgreSQL or the browser.
- Added deterministic frontier expansion with stable link ordering and unit coverage for direction, depth, node-budget truncation and invalid budgets. Search escapes wildcard characters and rejects whitespace-only terms.
- Upgraded Atlas with server-side search results, focus history, estate reset, hop/direction/edge filters, radial focused layouts, overview layouts, arrow direction, selected-edge highlighting, zoom controls, responsive behavior and accessible native table-node buttons.
- Added a governed node inspector that combines active columns, classifications, safe aggregate profile counts, approved business meaning, visible relationship evidence and downstream object counts. The UI explicitly states that raw customer, account and transaction values are never rendered; declared and suggested edges carry `source_values_inspected=false` evidence.
- Extended `scripts/verify-local.ps1` to require Graph Explorer V2 content and verify search, a two-hop bounded neighborhood, depth metadata, node/edge ceilings, value-free edge evidence and denial above the configured depth policy.
- JavaScript syntax, Ruff and strict mypy across 45 source files are clean; all 81 automated tests pass. No schema migration was required because this increment extends API/view contracts over existing authoritative metadata.
- Final Docker verifier organization `968a6ef1-bf20-4d57-b9bf-4fff32311d4a` passed the complete local banking fixture. Graph search returned two matches; focus expansion returned four nodes and five edges; a depth-five request was denied. Analysis run `0507c798-de9a-4184-9fc0-e383ef8680cf`, tool-first agent `2f414e2e-1a4e-4c23-bbe1-6641ce76d960`, evaluation `0849a661-5d46-463c-bd66-7fde296a4652`, dbt artifact `a7ecb745-2794-496e-a71d-d36acf91bbc8` and semantic inference run `b1d201f0-7def-4b5d-888d-96455e98dec1` all completed successfully.
- The Docker-hosted UI and API are healthy at `http://localhost:3000` and `http://localhost:8000`. The browser runtime reported no available browser connection, so interactive visual/accessibility certification remains a truthful retest item; live HTML, proxy, API, responsive CSS structure and JavaScript syntax checks passed.

### R15 deterministic prompt-risk screening and agent control suite v2

- Added a versioned `deterministic-prompt-risk-v1` classifier for direct user-prompt attacks. It detects instruction override, system-prompt extraction, credential extraction, authorization/guardrail bypass, masking/redaction bypass, privilege escalation and unbounded regulated-data extraction signals.
- Inserted a mandatory `SCREENED` state after authorization and before metadata retrieval. A blocked request creates value-free plan/trace/audit evidence and a rejected agent-run record, then returns HTTP 422 without retrieving metadata, constructing model context, choosing a tool or executing SQL.
- Kept raw prompt content out of new evidence. Only the existing keyed question HMAC plus classifier version, decision, numeric score, signal count and stable reason codes are retained or returned. Matched text fragments are deliberately excluded.
- Extended the planner with an explicit `BLOCKED` strategy that wins over candidate SQL, approved-tool and model paths. Preview uses the same classifier and returns zero retrieval records for a blocked prompt, allowing users to understand the denial before execution.
- Upgraded Atlas plan and run-history views with prompt decision, risk score, classifier version and reason codes. The AI runtime now reports `prompt_risk_classification` as an enforced deterministic control and runtime state-machine version `v2`.
- Upgraded the durable control evaluation to `governed-agent-controls-v2`, adding benign-prompt allowance plus instruction-override, credential-extraction, security-bypass, masking-bypass and privilege-escalation denials.
- JavaScript syntax, Ruff and strict mypy across 46 source files are clean; all 90 automated tests pass. No schema migration was required because prompt-risk evidence uses the existing versioned plan/trace JSON contracts.
- Final Docker verifier organization `58a70205-635f-4d80-9c2a-051149da84b9` passed the complete local fixture. Risk preview returned `BLOCKED` with no retrieval evidence; risky execution was denied before SQL; benign agent run `5819bd07-5084-4c16-a27d-5e5e32745379` recorded `SCREENED/ALLOW`; tool-first run `c96dec35-9664-4b25-8c1b-3a49606796b7` completed; evaluation `373d945b-ee45-4d69-a794-6b58f9bf2762` passed at 100%. Graph Explorer V2, dbt, governed semantics, masking, lineage, scheduling, audit/outbox and projection checks remained green.
- The direct classifier is defense in depth, not a claim of universal injection prevention. Multilingual, obfuscated and indirect injections through retrieved metadata/tool descriptions remain explicitly tracked for bank model-risk evaluation. The browser runtime still had no available connection, so interactive visual/accessibility certification remains open.

### R16 durable value-free data-quality observability

- Added source-default and table-override quality policy records for volume movement, null-rate movement, schema fingerprint change and maximum metadata scan age. Policies are tenant enforced, bounded, auditable and emitted through the transactional outbox.
- Extended immutable table profiles with the scan-time schema fingerprint. Every completed Temporal table-task workflow now invokes an idempotent quality reconciliation before committing completion evidence. Evaluation row-locks the run against concurrent replay and batch-loads historical baselines, policies, column statistics and incident state instead of issuing per-table lookup chains.
- Added a deterministic `quality-v1` evaluator. A first profile becomes `NO_BASELINE`; later profiles compare estimated/bounded sampled row count, percentage-point null-rate movement across stable column IDs and schema fingerprints. Only counts, rates, IDs and hashes are retained—never sampled source values.
- Added immutable quality observations and fingerprinted incidents with `OPEN`, `ACKNOWLEDGED` and `RESOLVED` states. Repeated failures update the same incident and occurrence count, regressions reopen it, and healthy comparisons automatically resolve recovered controls. Manual lifecycle changes require a rationale and emit audit/outbox evidence.
- Added paginated policies, observations and incidents; replay-safe completed-run evaluation; tenant-safe quality summaries using the latest observation per table; exact active/critical incident counts; average scores; and metadata scan-age posture. Source-row freshness deliberately returns `NOT_CONFIGURED` until a connector receives an approved watermark contract.
- Added the Atlas **Data quality** workspace with source selection, coverage/score/incident/scan-age metrics, policy editing, explicit measurement boundaries, incident filters/evidence, friendly acknowledge/resolve dialog and immutable observation history. The navigation badge reports active incident count.
- Added migration `1b7e4c9a62d0`, six deterministic evaluator/API contract tests and quality assertions to the full local verifier. Ruff is clean, strict mypy passes across 49 source files, and all 96 automated tests pass.
- Final Docker verifier organization `4a134037-926d-4aee-907c-f89c17a2cc8c` passed the complete local banking fixture. Policy `5d9647c1-82f8-40e4-aed3-5cf5f504ce2d` governed four tables; two completed scans automatically produced eight observations with average score 100, `CURRENT` metadata scan posture and explicitly `NOT_CONFIGURED` source freshness. The existing prompt-risk, agent, dbt, semantic, graph, lineage, masking, audit/outbox and projection checks remained green.
- Docker UI/API are healthy at `http://localhost:3000` and `http://localhost:8000`. No interactive browser instance was available, so visual click-through and accessibility certification remain open; live HTML/proxy/API assertions and responsive structure were verified.

## 2026-08-28

### R23 governed glossary and asset documentation

- Added organization-scoped glossary terms with stable keys, immutable versions, owner principals, and draft/review/approved/rejected/superseded maker-checker lifecycle.
- Added versioned table aliases, README content, ownership, and approved glossary-term links, all enforced by tenant and role policy with audit and transactional outbox evidence.
- Added the glossary authoring workbench and asset Intelligence controls for documentation, review submission, term linking, and unlinking; business-meaning pagination now reads every bounded page instead of silently stopping at the API page limit.
- Added migration `ab31d7e4c920`, contract/schema tests, and the implemented event contracts. Ruff, strict mypy across all source files, the complete Python suite, JavaScript syntax, and Alembic head checks pass.
- Live public-API verification on the rebuilt Docker stack created, independently approved, read back, and linked a glossary term and asset-documentation version against a discovered SQL Server table. Interactive browser visual and accessibility certification remains open because no in-app browser session was available.

### R18 enterprise metadata ingestion and connector certification

- Added canonical metadata envelope `1.0` for push and stream-shaped producers. Nested catalogs, schemas, tables, columns and constraints are strictly validated for names, duplicate identities, ordinals, local/foreign-key consistency, type/size limits and a 100-catalog/50,000-table/250,000-column synchronous safety boundary.
- Enforced a value-free attribute contract: scalar bounded attributes only, with sample/row-value/password/secret/token/credential keys rejected before persistence. The payload itself is not stored; jobs retain a canonical SHA-256 fingerprint, counts and operational evidence.
- Added datasource-scoped idempotency and row locking. An identical key/payload retry returns the original job, conflicting key reuse returns HTTP 409, unrelated sources remain independent, and the inventory/job/event changes commit atomically.
- Implemented explicit snapshot behavior. `INCREMENTAL` upserts present objects without retiring omissions. `FULL` treats the envelope as authoritative and soft-deprecates missing metadata through the existing tombstone path.
- Reused the authoritative discovery persistence path for pull and push so stable IDs, fingerprints, classifications, constraints, drift evidence, audit, outbox and Neo4j projection remain consistent. Push jobs also create normal completed analysis-run evidence.
- Added immutable connector certification records and suite `connector-contract-v1`, scoring implementation registration, opaque secret reference, prior connection evidence, hierarchy capabilities, active inventory and canonical push support. Added an honest registry matrix for PostgreSQL (`IMPLEMENTED/BETA`) and Oracle, SQL Server, Snowflake, Databricks, Teradata and Db2 (`PLANNED`).
- Added the Atlas **Enterprise ingestion control plane** with fleet inventory, capability/maturity/transport matrix, source certification and check evidence, a guided canonical JSON delivery form, incremental safe default, full-snapshot confirmation, privacy boundary, and ingestion change/history drill-down. Corrected datasource onboarding so its PostgreSQL selector maps to backend connector type `postgres`.
- Added migration `7d2f9a41c6e3`, five ingestion/certification contract tests, and full-verifier assertions for matrix honesty, 100-point certification, identical replay, conflicting-key denial and no incremental retirement. JavaScript syntax and Ruff are clean, strict mypy passes across 51 source files, all 101 Python tests pass, and Alembic reports no model drift.
- Final Docker verifier organization `4a178080-d084-4a01-b93f-17d6c6e27ee7` passed. Source `1f1b1050-3f6e-40ac-ae31-e7980bc3b2ec` earned certification `85e96308-f166-49ae-a3c1-95e8b58d1c96` at 100. Ingestion `35013701-7b1a-4659-8bf6-ca87ab2b4875` replayed to the same ID, conflicting reuse was denied, and all existing agent, prompt-risk, dbt, business semantics, tools, lineage, masking, graph, quality, scheduling, audit/outbox and projection checks remained green.
- The rebuilt API and Atlas UI are healthy at `http://localhost:8000` and `http://localhost:3000`. The in-app browser runtime exposed no browser surface, so interactive visual/accessibility certification remains open; live HTML, proxy, JavaScript, API and complete Docker workflow checks passed.

### R20 OpenAI structured-output schema fix and model-route activation walkthrough

- Fixed a real bug in `src/aida/model_gateway.py`'s `OpenAIResponsesProvider`: it forwarded pydantic's `model_json_schema()` output to OpenAI's Responses API unmodified, but OpenAI's strict structured-output mode requires every object node to set `additionalProperties: false` and list every property (including ones with defaults) in `required`. Live end-to-end testing against the user's actual configured OpenAI key reproduced the exact failure — HTTP 400, `"'additionalProperties' is required to be supplied and to be false"` — confirming this was a genuine defect, not a configuration problem. Added `_openai_strict_schema()`, a recursive schema rewriter that walks `properties`, `items`, `$defs`/`definitions`, and `anyOf`/`oneOf`/`allOf`/`prefixItems` branches, so it correctly handles both the flat `SqlGenerationOutput` schema and the nested `SemanticEnrichmentBatchOutput` schema (which references a child model through `$defs`) used elsewhere in the codebase. Verified against both schemas directly (structural assertions that every object node is compliant) and against the full test suite.
- Fixed a second, independently-discovered bug: the Atlas UI's "Validate SQL controls" button (`ui/app.js` `validateSql()`) hardcoded `dialect: "postgres"` on every call to `/v1/query/validate`, so validating candidate SQL against a non-Postgres datasource (like the new SQL Server connector) silently checked it against the wrong dialect's grammar. It now looks up the selected datasource's actual `dialect` field.
- Walked the user through the full local model-route activation path live against their running stack, which doubled as an end-to-end audit of that governance flow: real `OPENAI_API_KEY`/`GEMINI_API_KEY` in `.env`, `AIDA_MODEL_GENERATION_ENABLED` flipped from its safe-by-default `false`, a `ModelRouteConfiguration` created with `route_key=openai-bank-sql`/`provider_type=OPENAI`/`capabilities=[SQL_GENERATION, EXPLANATION]`, submitted and approved under proper maker-checker separation (a different principal decided the review than the one who submitted it, matching the platform's own enforcement), and confirmed `activation_status: READY` for the correct organization — which took real diagnostic work, since an org-scoped mismatch (the route was initially approved in a different organization than the one owning the SQL Server datasource under test) and an unpaginated organization list (more than 100 test organizations had accumulated locally) both had to be found and corrected before the right organization was identified.
- After the schema fix and route activation, confirmed live in the user's own environment that `Run governed analysis` with no candidate SQL supplied now reaches the `MODEL_GATEWAY` generation strategy and calls OpenAI successfully end to end, rather than requiring the `DEVELOPMENT_OVERRIDE` (hand-typed SQL) path.
- Fixed a third bug found during the same live walkthrough: the "Candidate SQL" textarea in `ui/index.html` held its example text (`SELECT customer_id, customer_name, email_address, state FROM public.customers`) as literal starting *value*, not a placeholder hint. Leaving the field visually untouched meant that text was still submitted as `candidate_sql`, silently routing the request down the `DEVELOPMENT_OVERRIDE` path instead of the intended `MODEL_GATEWAY` (LLM) path — and since `public.customers` doesn't exist on a SQL Server source, governance correctly rejected it with `UNKNOWN_OR_UNAUTHORIZED_TABLES`, which looked like a model-generation failure but wasn't one. Converted the field to a real HTML `placeholder` attribute so example text is never part of the submitted value; the field now starts genuinely empty.
- Noted but not yet fixed: `_post_with_retry` in `model_gateway.py` discards the model provider's actual error response body on failure, surfacing only the HTTP status code (`"model provider request failed with HTTP {status}"`). This made the OpenAI 400 in this session much harder to diagnose than necessary — the real cause only surfaced by reproducing the exact request directly against OpenAI's API outside the platform. Worth including the provider's error `message`/`code` (not the full body, to avoid ever logging potentially sensitive echoed input) in the raised exception in a follow-up pass.
- All three fixes confirmed live in the user's own environment after the final rebuild: with the "Candidate SQL" field left genuinely empty, `Run governed analysis` reached the `MODEL_GATEWAY` strategy at 45% plan confidence, generated SQL correctly grounded in the real SQL Server catalog (joining `retail.account`, `retail.customer`, and `risk.customer_risk_snapshot` — no hallucinated tables), passed prompt-safety and governance validation, executed successfully (2 rows, 38ms), and automatically masked the sensitive output columns (`customer_name`, `email_address`). This is the first fully model-generated, live, end-to-end governed analysis run against the SQL Server connector in this project's history.

### R19 Microsoft SQL Server connector

- Added `SqlServerConnector` (`src/aida/connectors/sqlserver.py`) implementing the same `Connector` contract PostgreSQL uses: `test_connection`, `discover`, `explain_read_query`, `execute_read_query`, `profile_table`. Uses `python-tds`, a pure-Python TDS driver, specifically so the connector needs no system ODBC driver install — matching the project's zero-extra-system-dependency posture for connectors. All synchronous driver calls are wrapped in `asyncio.to_thread` so the connector's public interface stays fully async like every other connector.
- Connection references follow the existing "resolved secret is a driver-ready DSN string" convention, using a `mssql://user:password@host:port/database` URL form parsed and validated by `_parse_dsn`; malformed or incomplete references are rejected before any connection attempt.
- Discovery uses ANSI `INFORMATION_SCHEMA` views (`COLUMNS`, `TABLES`, `TABLE_CONSTRAINTS`, `KEY_COLUMN_USAGE`, `REFERENTIAL_CONSTRAINTS`) for portability, with `sys.partitions`/`sys.tables`/`sys.schemas` used only for approximate row-count statistics where no ANSI equivalent exists. Primary key, unique and foreign key constraints are grouped and ordinal-ordered into the same catalog/schema/table/column/constraint shape the governed catalog already expects.
- Query cost estimation uses `SET SHOWPLAN_XML ON` (SQL Server's no-execution plan mechanism) and parses the returned plan with `defusedxml` (not stdlib `ElementTree`, to close the XXE finding Ruff's `S314` check raises) into the same `{"Plan": {"Total Cost": ...}}` shape the connector-agnostic query gateway already reads, so `src/aida/query_gateway.py` needed no changes.
- Registered the connector in `ConnectorRegistry` as `sqlserver` / dialect `tsql` / maturity `BETA`, removed it from the `declare_planned` placeholder list, and wired its capabilities into `default_capabilities()` in `src/aida/ingestion.py` alongside PostgreSQL's.
- Added a Docker Compose sample SQL Server source (`sample-mssql-source`, image `mcr.microsoft.com/mssql/server:2022-latest`, port `14330`) with an init sidecar (`sample-mssql-source-init`) that creates the database, a read-only `source` login, and seed data via `infra/sample-mssql-source/init.sql` — a T-SQL equivalent of the existing PostgreSQL sample source's `retail`/`risk` schemas and rows. Added the matching `AIDA_SAMPLE_MSSQL_SOURCE_DSN` to `.env.example` and `compose.yaml`'s shared environment block.
- Added SQL Server connector options to the Atlas UI's data source onboarding form (`ui/index.html`): "Microsoft SQL Server" in the connector selector and "T-SQL (SQL Server)" in the dialect selector, alongside the existing PostgreSQL/Postgres options.
- Added 17 unit tests (`tests/test_connectors_sqlserver.py`) covering DSN parsing (valid, missing-port-defaults-to-1433, and five invalid-reference cases), SHOWPLAN_XML extraction (valid plan, malformed XML, missing statement node, missing cost attribute), identifier quoting/escaping, catalog assembly (column ordering and nullability, primary-key grouping, foreign-key ordinal ordering), the connector's declared capabilities, and registry registration/maturity.
- Verified in this session: `ruff check .` is clean across the full repository; `mypy src` (strict mode) passes with no issues on the three touched/new files (`sqlserver.py`, `registry.py`, `ingestion.py`) — a full-repository strict mypy run could not complete within this sandbox's per-command time limit, so only the touched files were directly type-checked, though the full test suite passing is strong indirect evidence nothing else broke; the full `pytest` suite passes at 118/118 (101 pre-existing plus the 17 new SQL Server tests), confirming no regression to any existing connector, gateway, or ingestion behavior.
- Corrected `compose.yaml` after the user's first `docker compose up --build` attempt failed to resolve `mcr.microsoft.com/mssql-tools18:latest` (no such published image exists; `mssql-tools18` is an apt package, not a standalone container image). `sample-mssql-source-init` now reuses the same `mcr.microsoft.com/mssql/server:2022-latest` image as the server itself, which bundles `sqlcmd` at a path that varies by platform/architecture (`/opt/mssql-tools18/bin/sqlcmd` on some builds, `/opt/mssql-tools/bin/sqlcmd` on others); both the init script and the server healthcheck now probe for whichever path exists rather than assuming one.
- Diagnosed a `migrate` service failure the user hit on their second `docker compose up` attempt by reproducing the full 19-revision Alembic chain (including `9e4c7a12b5f8`, an unrelated pre-existing chunked-ingestion migration) against a real, throwaway local Postgres 17 instance — it applied cleanly both fresh and as a idempotent no-op replay, which isolated the cause to the user's Postgres volume having been reused (not recreated) from the earlier failed attempt rather than any migration defect. A full `docker compose down -v` followed by `docker compose up --build -d` resolved it: all 17 containers came up healthy, `migrate` exited 0, and `sample-mssql-source-init` exited 0.
- Live-verified the full connector path end to end against the user's actual running stack via direct API calls (`POST /v1/organizations` through `POST /v1/datasources/{id}/analysis-runs`): registered a `sqlserver`/`tsql` datasource with credential reference `env://AIDA_SAMPLE_MSSQL_SOURCE_DSN`, `POST /datasources/{id}/test` returned `CONNECTION_VERIFIED`, and a `FULL` analysis run reached `COMPLETED` and discovered all four seeded tables (`retail.account`, `retail.customer`, `retail.transaction_fact`, `risk.customer_risk_snapshot`). This closes the live-verification gap noted below — the connector is now proven against a real SQL Server instance, not just unit-tested.

### R20 resumable large-estate metadata ingestion

- Added persisted datasource-scoped batch manifests and checksum-addressed chunks with independent batch keys, chunk numbers and chunk keys. Exact retries return the original record; conflicting reuse returns HTTP 409. Tenant/role enforcement, audit attribution, analysis runs and transactional outbox evidence cover creation, receipt, submission and completion.
- Added `MetadataBatchIngestionWorkflow` and a heartbeat-enabled Temporal activity with bounded retry/backoff. Exact `1..expected_chunks` finalization is mandatory, processed chunks resume idempotently after failure, contract failures are non-retryable, and a retry creates a replacement analysis run linked to its predecessor.
- Reused the authoritative fingerprint/tombstone persistence path while accumulating stable identities across chunks. A second metadata-only pass resolves foreign keys whose target arrived later. `FULL` omission reconciliation runs only after every chunk succeeds, so partial delivery cannot retire metadata; `INCREMENTAL` never retires omissions.
- Added configurable batch ceilings of 1,000 chunks, 1,000,000 tables and 5,000,000 columns while retaining per-chunk envelope/value-free validation. Successful completion retains only checksums, counts, statuses and timestamps and physically clears chunk JSON with SQL `NULL`; failed work retains its bounded payload for an authorized retry.
- Added the Atlas durable-batch workbench to **Source fleet**: safe manifest creation, numbered JSON upload, checksum replay evidence, open-batch selection, received/processed progress, guarded full finalization, Temporal polling and failure/completion evidence. Incremental is the default in both synchronous and batch forms.
- Added migration `9e4c7a12b5f8`. The first real PostgreSQL migration run exposed colliding auto-generated composite-unique names; the transaction rolled back and explicit constraint names fixed it. The first real worker run then exposed pre-flush column identity counting and JSON-null cleanup semantics; both were fixed and reverified against PostgreSQL.
- Ruff is clean, strict mypy passes across all 54 source files, JavaScript syntax is valid, and all 121 Python tests pass. A dedicated API/Temporal run `98e95dd3-e323-48c1-acd5-603e4571d604` proved cross-chunk FK resolution, exact 1 catalog/2 schemas/2 tables/4 columns/3 constraints scope, 12 creations, two processed chunks and two physically SQL-NULL payloads.
- The expanded complete Docker verifier passed for organization `449cf64e-116a-41bf-916b-a37a7c68db93`. Batch `cf811ccb-0cea-4fc1-82b2-249620f9f706` and analysis run `7dd5742f-08db-409c-83b3-62ad75d2551f` completed with identical manifest/chunk replay and conflicting chunk denial. The existing four-table/two-scan quality score remained 100 and all agent, prompt-risk, dbt, business semantics, tool, graph, scheduling, lineage, masking, audit/outbox and projection checks remained green.

### R21 live Microsoft SQL Server fixture certification

- Started the real SQL Server 2022 Docker fixture and found that its init sidecar returned exit zero despite three SQL errors: the proposed read-only password failed SQL Server complexity policy, login/user creation consequently failed, and no usable source identity existed. Replaced it with a compliant fixture-only credential and added `sqlcmd -b` so any future T-SQL error fails the container instead of reporting false success.
- The first governed query exposed the second least-privilege gap: `db_datareader` can execute SELECT but cannot request a no-execution plan. The init contract now grants database-scoped `SHOWPLAN` to the source principal, which is the required permission for the connector's cost gate and does not grant data mutation.
- Registered the live `sqlserver`/`tsql` datasource through the public API using only `env://AIDA_SAMPLE_MSSQL_SOURCE_DSN`. Connection verification passed; Temporal discovery/profile run `3057bced-568b-476d-bea0-e3e010d2da7d` completed with 4 tables, 22 columns, 7 constraints and all 4 tables/22 columns profiled; deterministic certification scored 100.
- Governed query execution `b83f408d-ea64-48ea-a846-4c5a220cd307` captured SQL Server SHOWPLAN cost `0.0032844`, returned two bounded rows through `python-tds`, recorded `sqlserver-spid:76`, and masked both `customer_name` and `email_address` through the common policy gateway.
- Extended the permanent full Docker verifier to require the implemented SQL Server matrix entry, real connectivity, exact discovery/profile counts, 100-point certification, SHOWPLAN-backed query completion and PII masking. This closes the prior R19 local-live gap; vendor-version, scale, cancellation, recovery, TLS/private networking and bank delegated-identity certification remain release gates.
- Final combined verifier organization `e9706966-1ce9-46c1-8fbd-afe15b75f6e7` passed. SQL Server source `5fb19209-a140-4e48-ba7b-763a90caf579`, run `8ab167a7-14e8-4499-a4ca-c612e61eabef`, certification `d6e51bd0-8c7d-4c55-bb95-81a807d0b291` and query `382b8c8a-1eb6-433b-b22f-610dc2b1b6a8` all completed; durable batch `3e2f3ac7-ff2f-4c46-bcb8-032887f8ddbb` also completed in the same run, and every existing platform control stayed green.
- After moving cumulative table/column admission to chunk-upload time and setting the Atlas proxy boundary to 40 MiB, the final rebuilt-stack verifier also passed for organization `b61022b6-d0ce-48ab-aa6a-140c92726a92`. SQL Server run `1eec8e88-4fa9-4047-a58b-989dc147f06e`, query `16963d2b-c554-45dd-94fb-32cbd6b06ae0` and durable batch `628ce398-d766-402b-9937-5baa46eab769` completed with all 68 audit events and prior controls green.

### R17 persona-based navigation, global command search, and large-estate table virtualization

- Added a client-side workspace-persona switcher (Analyst, Steward, Platform operator, Auditor, or All capabilities) in the sidebar that filters the 15 primary navigation destinations to the subset relevant to each role; the choice persists per browser in local storage. Home stays visible under every persona so a filtered user is never stranded, matching the persona-based navigation requirement in `16-market-comparison-and-product-strategy.md` Phase C and the remaining item recorded in `15-ui-capability-coverage.md`.
- Added a global command palette (topbar search control, or Ctrl/Cmd+K from anywhere) that indexes every navigation destination plus the currently loaded tables, sources, governed tools, semantic model versions, and dbt projects, with arrow-key navigation, Enter-to-jump, and direct click-through to the matching record.
- Replaced the flat, fully-rendered table helper for large result sets with a windowed virtualization layer (`renderTable` / `mountVirtualTable` / `paintVirtualTable`): lists at or below 150 rows render exactly as before with no behavior change; lists above that threshold mount only the visible row window plus overscan inside a scroll-synced viewport, leaving row markup, existing click delegation, and existing CSS untouched. Applied to all 19 existing table render call sites (catalog, audit, sources, governance, quality, dbt, operations, agents, model routes, business meaning, semantics, relationships) and to the governed-analysis and tool-execution result grids, which previously rendered every returned row — up to the analyst's 100,000-row maximum — directly into the DOM.
- This is a UI-only increment: no API, schema, or migration changes. `node --check app.js` confirms the script still parses cleanly; the 19 table-render call-site replacements and every markup insertion were applied by an idempotent, assertion-guarded patch script and diffed against the prior files before being kept, rather than hand-edited.
- Not yet done, and explicitly still open rather than claimed complete: the Docker stack was not exercised end-to-end in this session (no running containers or connected interactive browser were available here), so live click-through, binding persona navigation to the bank's approved OIDC group contract, virtualization behavior at bank-scale row counts, and accessibility validation remain open — consistent with the existing UX entries in `12-enterprise-gap-register.md` and the Phase C exit criteria in `16-market-comparison-and-product-strategy.md`.

### R22 Oracle connector

- Added `src/aida/connectors/oracle.py`: a native pull adapter using `python-oracledb` 4.0.2's genuine async API (`connect_async`, `AsyncCursor`) in thin mode, so no Oracle Client library install is required, matching the project's no-sudo local-setup constraint. Connection parameters are parsed from one canonical `oracle://user:password@host:port/service_name` resolved-secret shape, rejecting partial or ambiguous forms before any network access.
- Discovery queries `ALL_TAB_COLUMNS`/`ALL_OBJECTS` for columns, and `ALL_CONSTRAINTS`/`ALL_CONS_COLUMNS` for primary/unique/foreign keys, scoped by `OWNER` excluding the standard Oracle-supplied system schemas. Raw uppercase-folded Oracle column names are normalized to the lowercase shape the shared `aida.connectors.discovery` helpers expect before assembly, reusing the same `build_table_map_from_column_rows`/`append_grouped_key_rows`/`append_grouped_foreign_key_rows`/`assemble_catalog` helpers SQL Server uses rather than a third one-off implementation.
- Governed read execution runs on the same cursor used to look up a real Oracle session identifier (`SYS_CONTEXT('USERENV', 'SID')`), recorded as `warehouse_query_id=oracle-sid:<sid>`, matching the SQL Server (`sqlserver-spid:<spid>`) and PostgreSQL (backend pid) convention of a real backend-scoped identifier rather than a synthetic UUID.
- Bounded profiling looks up each requested column's data type from `ALL_TAB_COLUMNS` first, then builds per-column aggregate expressions through a dedicated `_profile_expressions()` helper: standard scalar types get exact null/non-null counts, an approximate distinct count, and `TO_CHAR`-based length bounds; LOB-like types (`BLOB`, `CLOB`, `NCLOB`, `LONG`, `LONG RAW`, `BFILE`, `XMLTYPE`) — which reject `COUNT(DISTINCT ...)` and `TO_CHAR(...)` outright — fall back to honest static placeholders instead of failing the batch or fabricating a value.
- `estimate_read_query()` implements a real `EXPLAIN PLAN SET STATEMENT_ID ... FOR <sql>` / `plan_table` cost lookup with cleanup, but the connector ships with `capabilities.explain=False`: per the design decision recorded in `18-oracle-bigquery-implementation-backlog.md`, a least-privilege `PLAN_TABLE` write path has not been certified against a real bank-scoped Oracle role yet, so the deterministic query-cost gate in `query_gateway.py` currently fails closed with `QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR` for Oracle rather than advertise unproven support.
- `connector_registry` already carried an `oracle`/`oracle` `IMPLEMENTED`/`BETA` registration and `tests/test_connectors_oracle.py` (14 tests: credential parsing, identifier quoting, capability declaration, discovery assembly, LOB-aware profiling expressions) from earlier scaffolding in this build; `ingestion.py`'s `default_capabilities()` is fully connector-agnostic and needed no Oracle-specific branch.
- Added a `gvenzl/oracle-free:23-slim` sample source to `compose.yaml` (`sample-oracle-source`, host port `15210`, built-in `healthcheck.sh`) with `infra/sample-oracle-source/init.sql` creating least-privilege `retail`/`risk` schema-owner users plus a read-only `source` user, mirroring the `retail.customer`/`retail.account`/`retail.transaction_fact`/`risk.customer_risk_snapshot` fixture schema (including the cross-schema foreign key from `risk.customer_risk_snapshot` to `retail.customer`, requiring an explicit `GRANT REFERENCES`) that PostgreSQL and SQL Server already use, so the same manual API walkthrough applies unchanged. `AIDA_SAMPLE_ORACLE_SOURCE_DSN` was already present in `.env.example`; added the matching `compose.yaml` environment entry and data volume. This has not yet been exercised against a real running container in this session — Docker itself is unavailable in this sandbox, so `docker compose up` and live connection/discovery/profiling verification against the fixture remain an open step for the next session with access to the user's Docker host, exactly as the first SQL Server compose attempt needed a live iteration to fix an unavailable base image.
- Ruff, strict mypy, and the full pytest suite are clean against a real editable `uv`-installed verification environment (all Oracle unit tests plus the full existing suite pass with no regressions); `compose.yaml` was validated by parsing it with `pyyaml` to confirm the new service, environment entry, and volume are structurally well-formed, which is not a substitute for a real `docker compose up`.
- Live-attempted in a follow-up session against the user's real Docker host and found a genuine `gvenzl/oracle-free:23-slim` gotcha: the image ships `FREEPDB1` pre-baked into its compressed seed data, so the container's own `CREATE PLUGGABLE DATABASE FREEPDB1` step on first boot always raises `ORA-65012: Pluggable database FREEPDB1 already exists`; the entrypoint recovers by restarting and treating the database as "already initialized," but that recovery path skips `/container-entrypoint-initdb.d/` entirely, so `init.sql` (the retail/risk schema and the `source` reader user) never ran. The database itself came up and stayed healthy for hours with no other errors — this is purely an init-script-skip, not a broken image or a broken connector. The documented recovery (`docker exec ... resetPassword`, then `docker cp init.sql` + `sqlplus ... as sysdba @init.sql` inside the container) was handed to the user but not completed in that session — the user does not have `sqlplus` on the host, and it must be run *inside* the container via `docker exec`, which was not carried out before the session moved on. **Oracle live verification (connection test, discovery, profiling against real rows) remains genuinely open.** Rather than ask the user for more manual `docker exec` steps, fixed `compose.yaml` at the root cause: replaced the `/container-entrypoint-initdb.d/00-init.sql` mount on `sample-oracle-source` (which the quirk above makes dead weight) with a dedicated `sample-oracle-source-init` sidecar — same pattern as `sample-mssql-source-init` — that polls `sqlplus -s sys/...@//sample-oracle-source:1521/FREEPDB1 as sysdba` in a retry loop (up to 60 attempts, 5s apart) until the listener genuinely accepts a query, then applies `init.sql` unconditionally, independent of the main container's flaky `healthcheck.sh` status. `init.sql`'s internal `CONNECT retail/...`/`CONNECT risk/...` statements were also fixed from `@//localhost:1521/...` to `@//sample-oracle-source:1521/...`, since they now run from the separate sidecar container rather than from inside `sample-oracle-source` itself. This is a real fix for the diagnosed root cause, but it has **not been run against a live Docker host** — no Docker access exists in this sandbox — so `docker compose up --build -d` and a full connection-test/discovery/profiling pass against real rows remain the concrete next step. The `sample-oracle-source` healthcheck itself was also observed reporting `unhealthy` for hours despite the database being genuinely up; its exact failure mode (`healthcheck.sh` invocation, PATH, or something else) was never captured, and is now decoupled from schema bootstrap but still worth fixing for accurate container status.

### R24 governed glossary and stewardship control center

- Completed the table-stewardship vertical slice with organization-scoped glossary categories, immutable term definitions and synonyms, reviewed deprecation, individual/group ownership, reusable schema/table pattern rules, and maker-checker bulk assign/link/certify/deprecate operations capped at 500 subjects.
- Added durable manual and detected conflicts with retained competing positions and independently reviewed resolution. Added value-free exact-label link inference from approved business annotations, bounded scans, proposal review, and authoritative links retaining confidence and source-annotation provenance.
- Added reviewed table certification with rationale and expiry plus six-dimension coverage for documented, owned, classified, certified, quality-monitored, and semantically mapped state. Coverage supports organization, data-source, domain, and line-of-business scope, returns a bounded unowned backlog, and persists scoped snapshots/history.
- Rebuilt Business Meaning as a responsive Stewardship Control Center with coverage, category, ownership-rule, inferred-link, conflict, bulk-operation, certification, and asset-accountability workflows. Added structural dialog naming, explicit command-palette close behavior, live regions, focus boundaries, reduced motion, and mobile layouts.
- Added migrations `7fbc5568a81f` and `9284d3ee7c0e`, then merge revision `d81e6c0f2a14` to reconcile the concurrent organization-integration-policy branch. Alembic has one head and reports no model drift.
- Repository-wide Ruff and strict mypy pass; JavaScript syntax is valid; all 188 Python tests pass. The rebuilt API/UI are healthy and the permanent `scripts/verify-stewardship.ps1` workflow passed against the public API, including two ownership assignments, an `INFERRED` provenance link, auto-detected/resolved conflict `17b0e127-360d-4f92-b1f1-0467193c621d`, coverage snapshot, and reviewed term deprecation.
- Interactive browser visual/WCAG certification remains open because the in-app browser runtime exposed no browser session. Static accessibility contracts and deployed HTML/JavaScript markers pass; bank-scale selection, scheduled expiry/escalation, dedicated leaver reassignment, broader asset types, and fuzzy inference calibration remain explicit follow-up work.

### R25 BigQuery connector

- Added `src/aida/connectors/bigquery.py`, a native pull adapter implementing the same `Connector` contract as Oracle/SQL Server (`test_connection`, `discover`, `explain_read_query`, `execute_read_query`, `profile_table`) via `google-cloud-bigquery==3.44.0`. Credentials are one canonical structured payload (`project_id`, `location`, `auth_method` of `service_account` or `workload_identity`, with `service_account_info` required only for the former) — deliberately not a fake DSN string, matching the design decision in `18-oracle-bigquery-implementation-backlog.md` Workstream C. GCP project maps to catalog, dataset to schema.
- Discovery uses region-qualified `INFORMATION_SCHEMA.COLUMNS`/`TABLES`/`TABLE_CONSTRAINTS`/`KEY_COLUMN_USAGE`, reusing the shared `aida.connectors.discovery` assembly helpers. Foreign-key metadata and `column_default` are honestly omitted rather than guessed at, since their `INFORMATION_SCHEMA` shapes could not be verified live.
- Estimation uses a `dry_run=True` job for `total_bytes_processed` (no row estimate — BigQuery dry runs don't provide one). Extracted the query-gateway cost gate into a new pure function `gate_query_estimate(estimate, settings)` in `src/aida/query_gateway.py` that branches structurally on `estimate.estimated_bytes is not None`, adding a deterministic byte-budget check (`max_bigquery_dry_run_bytes`, default 10 GB, new `config.py` setting) without changing PostgreSQL/SQL Server/Oracle's existing cost-plan gating path. Governed execution records `warehouse_query_id="bigquery-job:<job_id>"`, matching the `oracle-sid:`/`sqlserver-spid:` convention. Bounded profiling caps every query with an explicit `LIMIT`, `maximum_bytes_billed` and timeout; `REPEATED`/`RECORD`/`STRUCT`/`BYTES`/`GEOGRAPHY`/`JSON` columns fall back to static placeholders rather than issuing aggregates BigQuery rejects on those types.
- Registered `bigquery` in `connector_registry` (`BETA`, transports `PULL`+`PUSH`, dialect `bigquery`) and removed it from the `declare_planned` list.
- Added `tests/test_connectors_bigquery.py` (28 tests): credential parsing (valid plus 11 invalid/ambiguous forms), capability declaration, identifier/region quoting, discovery assembly including the FK omission, profiling-expression fallback for complex types, and `gate_query_estimate` (byte-budget allow/reject, cost-based fallback, non-finite rejection).
- Full local suite: `ruff check .` and `mypy src` (strict) are clean on every file this increment touched; `pytest` passes at 170/170 (up from a 141-test baseline, +28 new plus +1 in `tests/test_ingestion.py` distinguishing BigQuery-implemented from still-planned connectors).
- Not done, and explicitly left open: no live GCP project or credentials were available in this session, so `test_connection`, discovery, dry-run estimation, execution and profiling are unit-tested against mocked shapes only, never against a real BigQuery project — this mirrors exactly how Oracle's live-fixture verification was left open in its own increment. Certification and multi-version fixtures are unstarted.

### R26 UI accessibility and usability remediation

- Reviewed the R17 accomplishment entry and `20-modules/21-experience-shell.md` (UX-5, "accessibility audit and remediation") before changing anything, then applied targeted ARIA/keyboard/focus/contrast fixes across `ui/index.html`, `ui/app.js` and `ui/styles.css` via assertion-guarded patch scripts (each replacement asserted its expected match count before writing, and the live files were re-read after each stage to confirm), matching the idempotent-patch discipline R17 used.
- `ui/index.html`: `aria-label`s on all 12 icon-only dialog-close buttons; `aria-expanded`/`aria-controls` on the sidebar toggle; `tabindex="-1"` on `#page-title` so navigation can move focus to it; `#graph-canvas` marked `aria-hidden` (its real content lives in sibling button nodes); the operations tabs and the five asset-detail tabs converted to a real ARIA tabs pattern (`role="tab"`/`aria-selected`/`aria-controls`, `role="tabpanel"`/`aria-labelledby`); the command palette input exposes `role="combobox"`/`aria-expanded`/`aria-controls`/`aria-activedescendant` with a `role="listbox"` results container; `#alert-region` and the analysis-status badge are live regions.
- `ui/app.js`: `notify()` now switches between `role="status"` (success) and `role="alert"` (errors) instead of announcing both at the same urgency; `showView()` sets `aria-current="page"`, moves focus to `#page-title` after navigation, and respects `prefers-reduced-motion`; added `bindTabKeyboardNav()` for roving-tabindex Left/Right/Home/End navigation on both tab groups; the virtualized-table row-range indicator is a polite live region so scrolling a large table announces "Showing X–Y of Z rows" without re-announcing the whole grid; added `window.confirm()` guards on three previously-silent destructive actions (disabling a source, unlinking a glossary term, cancelling an in-flight analysis run).
- `ui/styles.css`: a `prefers-reduced-motion: reduce` block; a global `:focus-visible` outline (most custom controls had no explicit focus style before); fixed the command-palette search input, which set `outline: none` with no replacement; changed `--muted` from `#6e7890` (4.42:1 on white, just under WCAG AA's 4.5:1 for normal text, computed by hand since no browser was available) to `#5b6680` (5.74:1) in both `:root` blocks in the file.
- Verified: `node --check ui/app.js` passes before and after every patch stage; HTML tag and CSS brace counts balanced (`<dialog>` 13/13, `<div>` 309/309, `<button>` 104/104, CSS braces 649/649); confirmed via mtimes that only the three `ui/` files were touched.
- Not done, and explicitly left open: no browser, screen reader, or axe-core run was available in this session, so none of the above was interactively verified — the same constraint R17 hit. The contrast fix is the only color pair checked against the WCAG formula; the rest of the stylesheet's palette is unaudited. Interactive click-through, real keyboard-flow verification, and a full WCAG AA certification remain open, consistent with the `UX-5` tracker entry and the portal's status-matrix row.

### 2026-08-28 consolidation note

- This session ran three independent workstreams in parallel against a live, actively-changing checkout (BigQuery connector, glossary term lifecycle, UI accessibility) and closed with a repo-wide verification pass. At verification time `git status` showed 21 additional changed files from *other, unrelated concurrent work* on this same checkout (a Snowflake connector, a dbt/quality bridge, and OpenLineage ingestion changes) that this session did not author and left untouched, plus a stale `.git/index.lock`. `ruff check .` at that point showed 45 errors, all confined to those other files (none in anything this session touched); `uv run alembic heads` showed one clean head; `pytest` passed 214/214; nothing was committed. Flagging this for whoever picks up next: the working tree had more than one active author in the same window and was not committed by this session.

### R27 repo-wide lint/type cleanup and dependency fix

- Fixed the 45 `ruff check .` errors and 2 `mypy --strict` errors that were sitting in files from other concurrent work on this checkout (`migrations/versions/8a7f3c1d4b22_openlineage_run_events.py`, `migrations/versions/04003a3d6945_dbt_resource_test_and_extra_metadata.py`, `src/aida/data_contracts.py`, `src/aida/openlineage.py`, `src/aida/openlineage_api.py`, `src/aida/connectors/snowflake.py`) — none in this session's own BigQuery/glossary/UI work, which was already clean.
- `uv run ruff check --fix .` cleared unused imports and import ordering (11 auto-fixed); `uv run ruff format` on the five affected files reflowed most remaining over-100-column lines (mechanical, quote-style/wrapping only, no logic change); the 6 lines it couldn't safely reflow (long f-string `message=` assignments in `data_contracts.py` and one `raise OpenLineageError(...)` in `openlineage.py`) were manually wrapped into parenthesized implicit string concatenation, preserving the exact original message text (verified via diff).
- Added the missing `snowflake-connector-python==3.15.0` dependency to `pyproject.toml` — `snowflake.py` was importing it at runtime (`_get_connection()`) without it being declared, which both broke `mypy` (`import-not-found`) and meant a fresh `uv sync` would silently produce a connector that fails at first use. `uv sync` resolved cleanly (also correctly downgrading `cryptography` from `50.0.1` to `45.0.7` to satisfy the new dependency's constraint).
- Final state: `ruff check .` → All checks passed. `mypy src` (strict) → Success, no issues found in 70 source files. `pytest` → 214/214 passed, no regressions.

### R28 MCP tool-exposure role-binding enforcement

- Audited `src/aida/mcp_server.py` (the real JSON-RPC 2.0 MCP endpoint at `POST /mcp`, mounted in `main.py`) against the CX-1/CX-3/CX-5 exit criteria and found the docs describing module 19 as entirely `Pending` were stale — the endpoint already existed and routed `tools/call` through the full governed orchestrator/query-gateway stack — but found a real, unflagged gap: `_handle_tools_list` returned every published tool regardless of the caller's role, and `_handle_tools_call` never checked `GovernedToolVersion.allowed_roles` before invoking the orchestrator, unlike the identical role-binding check already enforced in the native REST path (`tool_api.py::execute_tool`) and the native agent planner (`agent_intelligence.py::GovernedPlanner.plan`). A caller with no eligible role could see an ineligible tool listed and, on calling it, fall through to open-ended `MODEL_GENERATION` SQL generation instead of a denial — the endpoint's own `_ERR_ACCESS_DENIED` code was declared but never raised.
- Added `_tool_role_eligible(roles, allowed_roles)`, mirroring `tool_api.py`'s check exactly. `tools/list` now filters to role-eligible tools and surfaces `allowed_roles` in `_atlas_meta`. `tools/call` on an ineligible tool now returns the identical "not found or not published" response used for a genuinely absent tool — deliberately not a distinguishable "access denied," so a caller can't enumerate tool existence by role-probing — while recording an `AuditEvent` (`mcp.tool_call.role_binding_denied`) and outbox event (`mcp.tool_invocation_denied.v1`) so operators can see the denial.
- Added `tests/test_mcp_server.py` (12 tests), taking `mcp_server.py` from zero test references to full coverage of the new decision logic, following the codebase's existing DB-free unit-test convention for this kind of routing/decision code.
- Corrected `Docs/20-modules/19-context-products-and-mcp.md` §13 from "entirely unbuilt" to an accurate implemented/partial/missing breakdown. Also flagged for future audits: `src/aida/context.py` is an unrelated 7-line correlation-ID helper, not a "context products" implementation — the name overlap with CX-2 is coincidental.
- `ruff check .` clean, `mypy src` (strict) clean across 70 files, `pytest` 226/226 passed (214 + 12 new).
- Explicitly still open, not attempted this pass: CX-2 (context products with maker-checker — no model exists at all), CX-4 (`resources/read` records no consumption-lineage edges), CX-6 (per-consumer rate limits/budgets), MCP `prompts/*` handlers (advertised in `initialize` capabilities but unimplemented), and module 12's RT-1/2/3/4/6/7/8/9 (retrieval is a real single-source lexical scorer only — no vector projection, graph expansion, true multi-factor fusion, Postgres full-text index, or cross-source search; `retrieval.py` and `agent_orchestrator.py` both remain completely untested, a real gap worth its own increment).

### R29 documentation audit — Snowflake connector, OpenLineage ingestion, dbt quality bridge (backfilled records)

- This entry does not describe new code. It backfills the accomplishment-log record for three capabilities that were already sitting in the working tree from unattributed concurrent sessions (the Snowflake connector, OpenLineage ingestion, and the dbt-quality bridge — all previously visible only as the "2026-08-28 consolidation note" and the R27 lint/type fixup, neither of which described what they actually do) and corrects every tracker/status-matrix/module-doc row that had gone stale as a result. Done at the user's explicit request ("go through all the files and update the track again") after other concurrent sessions on this checkout stopped, so this record is now the authoritative baseline going forward.
- **Snowflake connector** (`src/aida/connectors/snowflake.py`, 517 lines) is a native pull adapter registered `IMPLEMENTED`/`BETA` in `connector_registry` — not `PLANNED` as every doc still claimed. It parses either a `snowflake://` URI or a structured JSON credential payload (`_parse_dsn`), discovers columns and primary/unique/foreign-key constraints across every database in the account via `INFORMATION_SCHEMA`, reusing the same `aida.connectors.discovery` assembly helpers as every other connector, estimates query cost via `EXPLAIN USING JSON` with a partition-pruning-ratio evidence field (`_extract_snowflake_explain_estimate`, so `capabilities.explain=True`), profiles tables with `APPROX_COUNT_DISTINCT`, and captures the real Snowflake query ID (`cur.sfqid`) as `warehouse_query_id="snowflake-query:<sfqid>"` — the same real-backend-identifier convention Oracle/SQL Server/BigQuery use. `tests/test_connectors_snowflake.py` (7 tests: identifier quoting, both DSN formats, EXPLAIN-JSON extraction, registry definition, discovery assembly, query execution) passes cleanly, verified directly in this session (`pytest tests/test_connectors_snowflake.py` → 7 passed). No live Snowflake account exists in any session, so connection/discovery/profiling against a real warehouse remain unverified — the same "implemented, unverified live" position as Oracle and BigQuery. Corrected: tracker `CN-2` (split into `CN-2a` Snowflake/`CN-2b` Databricks), `04-status-matrix.md`'s "Other connectors" row (added a dedicated Snowflake row), `05-gap-register.md`'s connector-fleet row, `07-connector-implementation-backlog.md` (added a full "Workstream E" record matching the Oracle/BigQuery format), and `20-modules/02-connectivity.md`'s adapter table and open-work list (which additionally had Oracle itself mis-stated as `PLANNED` maturity — it has been `BETA` since R19).
- **OpenLineage ingestion** (`src/aida/openlineage.py`, 272 lines; `src/aida/openlineage_api.py`, 433 lines; migration `8a7f3c1d4b22_openlineage_run_events`) is a real, mounted capability, not the `TODO`/"module unbuilt" the tracker and gap register claimed. `parse_openlineage_run_event` validates a bounded, value-free OpenLineage RunEvent payload (job/run/namespace/dataset model, facet-shape validation) and extracts column-lineage edges from the `columnLineage` facet. `POST /v1/lineage/openlineage` (mounted in `main.py`) is idempotent by SHA-256 event fingerprint, resolves input/output datasets against the existing catalog (exact, schema+table, and table-only matching tiers), and persists `OpenLineageRunEvent`/`OpenLineageDataset`/table/column edge rows with audit and outbox evidence; `GET /v1/datasources/{id}/openlineage-events` and `GET /v1/openlineage-events/{id}` expose them. What remains genuinely missing, confirmed by direct check in this session: **zero test files** reference OpenLineage anywhere in `tests/` (`ls tests/ | grep -i openlineage` → no matches), and no Airflow-sourced event has ever been posted to the endpoint — only the parser's own internal logic has been read, never exercised end to end. Corrected: tracker `LN-1` (`TODO` → `IN PROGRESS`), `04-status-matrix.md`'s "SQL / query lineage" row, and `05-gap-register.md`'s "Context products and MCP" row (which was unrelated to OpenLineage but was found to be separately and severely stale — see below).
- **dbt quality bridge** (`src/aida/dbt_quality_bridge.py`, 223 lines) couples dbt test outcomes into the existing `DataQualityIncident` lifecycle — a real, narrow instance of "quality signals driving other behavior," though not the broader DQ-3/RT-7/AG-6/TL-3 "quality → runtime coupling" (retrieval ranking, answer warnings, tool gating) that tracker row still correctly lists as `TODO` (`DataQualityIncident` has zero references in `retrieval.py`, `agent_orchestrator.py`, or `tool_api.py`, confirmed by grep). `infer_dbt_test_anomaly_type` classifies a failing dbt test (`not_null`/`unique`/`relationship`/`accepted_values`/`freshness` naming conventions) into the platform's existing anomaly taxonomy, and `reconcile_dbt_test_quality` opens, reopens, or resolves a deterministically fingerprinted `DataQualityIncident` per failing/passing test, wired into the manifest-import endpoint (`dbt_api.py`) via `parse_dbt_run_results`, which was already parsing `run_results.json` and persisting `test_status`/`test_failures`/`test_execution_time` per `DbtResource` with no consumer. `tests/test_dbt_quality_bridge.py` and `tests/test_dbt_artifacts.py` pass cleanly, verified directly in this session. No integration test exercises the full `POST .../artifact-imports` → incident-reconciliation path together. Corrected: tracker `LN-6` (`TODO` → `IN PROGRESS`) and `04-status-matrix.md`'s "dbt transformation intelligence" row.
- **Also corrected while auditing, found independently stale and outside the three items above:** tracker `KG-1`/`RL-4` (`TODO` → `IN PROGRESS` — Graph Explorer V2's bounded, policy-filtered relationship-candidate visibility already satisfies most of the exit bar; only projecting *approved* candidates into Neo4j itself, as opposed to declared FK constraints, remains) and tracker `MG-3` (`TODO` → `IN PROGRESS` — approved-route selection via `ModelRouteConfiguration`'s maker-checker lifecycle plus config-selected `route_key` gating has been real since R9/R11; only private-endpoint routing is unbuilt). `05-gap-register.md`'s "Context products and MCP" row separately claimed **"None — module unbuilt,"** flatly contradicting the tracker's own `CX-1`/`CX-3`/`CX-5` rows (which R28 had already correctly updated to `IN PROGRESS`/`DONE`) and the status matrix's "Context products and MCP" row (`Partial`) — corrected to match.
- Four parallel research agents did the actual code-vs-doc comparison (connectors; lineage/dbt-quality; semantics/glossary/graph/retrieval/agent; tools/gateway/governance/identity/UX) against `03-tracker.md`, `04-status-matrix.md`, and the relevant module docs; every other row they checked — including a full re-verification of `GL-1` through `GL-8`, `KG-2` through `KG-7`, all `RT-*`, `AG-*`, `SM-*`, and the entire tools/gateway/governance/identity/observability section — matched the code exactly and needed no change. `src/aida/data_contracts.py` (a `DataContractSpec`/SLA-evaluation module) was found to be genuine dead code: not imported anywhere outside itself, no route, no table, no test — it does not count as evidence toward `DQ-2` and was left alone rather than either wired in or removed, since the user did not ask for either.
- Not run in this pass: a full repo-wide re-verification of every one of the tracker's 171 rows — the four agents' scope was targeted at the sections most likely to have drifted (connectors, lineage/quality, semantics/glossary/graph/retrieval/agent, tools/gateway/governance/identity/UX), on the working assumption (confirmed correct in every section checked) that rows already updated by R14/R24/R26/R28 were current and that Section A (structural foundation — `platform/` extraction, import-linter, etc.) and Section H/I/J (testing/performance/certification, drills, bank decisions) describe target-architecture and operational work with no code correlate to check.


### R29 addendum — competitor-comparison and module-doc sweep (same audit pass)

- Extended the R29 audit beyond the tracker/status-matrix/module docs it directly targeted, grepping `Snowflake`/`BigQuery`/`Oracle`/`OpenLineage`/`declare_planned`/`MCP` across every remaining `Docs/` subtree (`00-product`, `10-architecture`, `20-modules`, `30-contracts`, `40-engineering`, `50-security`, `90-reference`, `competitors`) to catch stale claims outside the four agents' original briefs. `00-product`, `10-architecture`, `30-contracts`, `40-engineering`, `50-security`, and `90-reference` all describe target architecture, competitor offerings, or wire contracts rather than Atlas's own current implementation state — nothing there needed correction.
- `Docs/competitors/05-codebase-gap-analysis-and-improvements.md`: "Connector Coverage" row corrected from "PostgreSQL & SQL Server (Beta)" / "**BEHIND**: Missing Snowflake, BigQuery, Databricks, and Oracle adapters" to reflect Oracle/BigQuery/Snowflake all being implemented (`BETA`, unverified live) and only Databricks/Teradata/Db2 still missing. "Context API / MCP Server" row corrected from "Internal REST API only" / "**BEHIND**: No standard...MCP server" to describe the real, tested, role-eligible `mcp_server.py` endpoint, noting it has not yet been exercised by a live external MCP client.
- `Docs/competitors/06-codebase-architecture-reference.md`: the connectors table was missing `bigquery.py`/`snowflake.py` rows entirely and still described `registry.py` as using `declare_planned()` "for BigQuery / Snowflake / Databricks" — added the two missing rows and corrected the registry row and the "Planned but not yet implemented" line to list only `databricks`/`teradata`/`db2`. Gap-list row #3 ("No BigQuery / Snowflake pull adapters", self-contradictorily citing the very files that disprove the claim) reworded to the real remaining gap (Databricks/Teradata/Db2). Gap-list row #1 ("No MCP Server", citing `mcp_server.py` as a file *to create*) reworded to reflect that the 652-line, tested MCP server already exists and the real gap is external-client verification.
- `Docs/20-modules/09-lineage.md` §12: "ETL / OpenLineage" row corrected from "**Not implemented**" to "Partial" with the same real-implementation/zero-test-coverage caveat as the tracker; "DBT" row's target column had `run_results.json` removed since `dbt_quality_bridge.py` now consumes it.
- `Docs/20-modules/15-model-gateway.md` §14/§15: "Route versions" row and the `MG-3` open-work line both still described "bank-approved route selection" as entirely un-implemented target work; corrected to note the config-selected `route_key` gating is real (since R9/R11) and only private-endpoint routing remains open, matching the tracker's `MG-3` correction.
- Checked and found already accurate, no change needed: `Docs/20-modules/10-knowledge-graph.md` (Graph Explorer V2 already marked Implemented), `Docs/20-modules/06-relationship-intelligence.md` (RL-4/"projection of approvals to Neo4j" already correctly listed as outstanding), `Docs/20-modules/19-context-products-and-mcp.md` (already carries the detailed, accurate MCP partial-build breakdown from R28).
- This closes out the "go through all the files" audit request. Standing open item, unrelated to documentation accuracy: no live Docker verification has been performed against a real Oracle/BigQuery/Snowflake backend in this session (blocked on this session's network egress allowlist blocking the user's local Docker host); the user has taken over verification themselves via `/tmp/verify_oracle.ps1` for Oracle and asked this session to move on from requesting manual debugging steps.


## 2026-08-29 — Unified Lineage Explorer (EA.14) and Collibra platform gap wiring

User shared the Collibra Data Lineage and Collibra Platform product pages and asked for the
findings to be captured as feature requirements with references, and for the highest-value
gap to actually be built rather than only documented.

**Built:**
- `src/aida/unified_lineage.py` — pure, database-free graph module: `UnifiedLink`,
  `expand_frontier`, and `traverse`, generalizing `aida/knowledge_graph.py`'s BFS to string
  node ids (needed because dbt resources and OpenLineage datasets without a matched catalog
  table get a synthetic id, e.g. `dbt:<uuid>`, `openlineage:<namespace>:<name>`, instead of
  disappearing from the graph).
- `src/aida/unified_lineage_api.py` — `GET /v1/datasources/{id}/unified-lineage/graph` (merges
  `MetadataConstraint` FKs, `RelationshipCandidate` suggestions, `DbtLineageEdge` dependencies
  from each project's latest imported manifest, and `OpenLineageTableEdge` ETL edges into one
  node/edge set, bounded and truncation-flagged like the existing knowledge-graph endpoints)
  and `GET /v1/datasources/{id}/unified-lineage/impact/{node_id}` (bounded transitive
  upstream/downstream traversal — replaces `/v1/metadata/tables/{id}/impact`'s direct-reference
  count for the nodes reachable in the unified graph; that endpoint is left in place since it
  also covers metrics/tools not part of the lineage graph).
- New schemas in `schemas.py`: `UnifiedLineageNodeRead`, `UnifiedLineageEdgeRead`,
  `UnifiedLineageGraphRead`, `UnifiedLineageImpactNodeRead`, `UnifiedLineageImpactRead`.
- Wired into `src/aida/main.py`.
- `tests/test_unified_lineage.py` — 8 tests: pure BFS/traversal behavior (direction semantics,
  bounding, transitive multi-hop depth across mixed edge sources) with no database, plus
  OpenAPI-contract and schema-serialization tests mirroring `tests/test_knowledge_graph.py`'s
  style. Full suite (165 tests before and after) plus `ruff check`, `ruff format`, and
  `mypy --cache-dir=/tmp/mypy_cache` all pass. Verified by installing dependencies into an
  ephemeral `uv` environment at `/tmp/aida-venv` (`UV_PROJECT_ENVIRONMENT=/tmp/aida-venv uv
  sync --frozen --extra dev`) since the checked-in `.venv` is a Windows venv unusable from the
  Linux device-bridge shell, and its directory can't be overwritten from that shell
  (`Operation not permitted` on `.venv/.gitignore`).

**Known limitation, documented rather than silently accepted:** column-level edges are still
name-matched (dbt UI) or absent (unified graph is table-level only); view/procedure and BI
nodes are not yet in the unified graph; there is no export. These are exactly LN-10, LN-11,
LN-12, tracked as open work below.

**Documentation:**
- New `Docs/competitors/08-collibra-lineage-and-platform-analysis-2026-08.md` — the
  screenshot-driven capability comparison for both pages, with source URLs, and the resulting
  gap list.
- `Docs/90-reference/03-sources.md` — added the Collibra Data Lineage URL.
- `Docs/20-modules/09-lineage.md` — Impact row and HTTP surface updated for the delivered
  endpoints; LN-7 marked delivered; LN-9 (delivered) through LN-12 (open) added to open work.
- `Docs/60-delivery/02-epic-backlog.md` — added `EA.14` (delivered, full acceptance detail) and
  `EE.8`–`EE.11`, wiring the CP-2/CP-3/CP-5/CP-6/CP-7/CP-8 platform requirements that
  `Docs/20-modules/19-context-products-and-mcp.md` §15.2 had already specified in detail (from
  an earlier pass over the same Collibra platform material) but that had not yet been turned
  into epic-backlog or gap-register entries.
- `Docs/60-delivery/05-gap-register.md` — updated the "Relationship and lineage evidence" and
  "Context products and MCP" rows, and added four new rows to "Newly identified gaps" for the
  lineage-MCP, context-compiler, product/contract-registry, and AI-registry/trust gaps.
- `Docs/20-modules/19-context-products-and-mcp.md` — cross-referenced the new competitors doc
  and the CP-* -> EE.* epic mapping.


## 2026-08-29 (continued) — Lineage MCP tools (EE.10, partial) and 5-page Collibra review

User pasted five more Collibra product page URLs (Data Marketplace, Data Catalog,
Integrations & APIs, MCP Server, Data Governance) and asked for a further review and for the
platform to keep being built out meaningfully.

**Reviewed:** all five pages via WebFetch. Most of what they show was already anticipated by
the CP-1..CP-14 requirements added to `Docs/20-modules/19-context-products-and-mcp.md` §15.2 in
an earlier pass. Two genuinely new, concrete gaps came out of the MCP Server page specifically
(it lists 25+ tools, both read and write, plus "fuzzy name matching and concept mapping"):
`MCP-2` (no MCP write path to catalog stewardship) and `MCP-3` (no fuzzy entity resolution --
every tool we expose needs an exact UUID). Full findings:
`Docs/competitors/09-collibra-marketplace-catalog-integrations-mcp-governance-2026-08.md`.

**Built — EE.10 (partial):**
- Refactored `unified_lineage_api.py`'s two route bodies into reusable payload builders
  (`build_unified_lineage_graph_payload`, `build_unified_lineage_impact_payload`) that take an
  already-loaded, already-authorized `DataSource` rather than doing their own `Depends`-based
  lookup, so the exact same merge/traversal logic can be called from a second transport. Added
  `LineageNodeNotFoundError` (plain `ValueError` subclass, not `HTTPException`) so the REST
  route and the new MCP tool can each translate a missing node into their own transport's error
  shape from one raise site.
- `mcp_server.py`: two new native MCP tools, `atlas__get_lineage_graph` and
  `atlas__get_lineage_impact`, dispatched in `_handle_tools_call` before the
  `GovernedToolVersion` lookup (native tools are not backed by a published tool row). Listed in
  `tools/list` only for callers whose roles intersect `UNIFIED_LINEAGE_READER_ROLES` --
  eligible-tool exposure applied the same way it already is for governed SQL tools, including
  the anti-enumeration property (an ineligible call gets the identical "not found or not
  published" text as a genuinely unknown tool name).
- 7 new tests in `tests/test_mcp_server.py` (role denial, invalid UUID, cross-org datasource,
  missing `node_id`, and two success-path tests that monkeypatch the payload builders --
  consistent with this test file's existing no-database convention) plus 1 in
  `tests/test_unified_lineage.py`'s neighborhood confirming the refactor didn't change route
  behavior.
- Full suite, `ruff check`, `ruff format`, `mypy` all clean for every file this session touched.
  **Noted, not fixed** (out of scope -- belongs to the separate, already-uncommitted
  `context_product_api.py`/`context_product_policy.py` work): `tests/test_context_products.py`
  is flaky, failing a different test on about 1 in 3 runs with
  `AttributeError: '_Result' object has no attribute 'all'` in `context_product_policy.py`,
  independent of anything touched this session (confirmed by running it in isolation, repeatedly).
- Also corrected stale text in `Docs/20-modules/19-context-products-and-mcp.md` §13: it still
  said "no `ContextProduct` concept anywhere in the codebase," which predates the (uncommitted)
  `context_product_api.py` work discovered while wiring these tools in -- `ContextProduct` /
  `ContextProductVersion` models and their MCP resource-read path already exist.

**Not built, tracked as open work:** MCP-2 (write operations), MCP-3 (fuzzy resolution),
transformation-detail-as-a-tool, consumption-lineage recording for the new tools (same
pre-existing `CX-4` gap `resources/read` already has), and a dedicated cross-tenant leak test
for the two new tools.


## 2026-08-29 (continued) — Code review of a separately AI-generated build-out; router-wiring and type-safety fixes

User ran a different AI model against this same repository in parallel with this session and
asked for the result to be reviewed, the docs corrected to match reality, and any real bugs
fixed. This entry supersedes several "not met" / "not built" notes from the two entries above
it, which the other model's work closed.

**Reviewed** (via `git diff`/`git show` against commits `2fa7667` "Harden context products and
unified lineage" and `99cc556`, plus the working tree, which was still being actively written to
during this review — see caveat below): `context_product_policy.py`, `lineage_cache.py`, the
`unified_lineage_api.py` and `mcp_server.py` hardening diff, the `9a6d4f21c8b7` and
`b4e8f2a71c90` migrations, `src/aida/models.py`'s new ORM classes, `platform_schemas.py`,
`context_compiler.py` / `context_compiler_api.py`, and `product_marketplace_api.py`.

**Findings — fixed:**
- `src/aida/main.py` did not register the `product_marketplace_api` or `context_compiler_api`
  routers. Both files were fully implemented (contract/product lifecycle, marketplace search,
  access requests, context compilation, drift detection) but every one of their ~16 endpoints
  was unreachable — confirmed by generating `app.openapi()` before and after. Fixed by adding
  both imports and `include_router` calls in the correct alphabetical position.
- `product_marketplace_api.py::_validate_product_references` assigned `session.get(...)` results
  of three different ORM types to the same `asset` variable across an if/elif/else chain without
  an explicit annotation; `mypy` narrowed it to the first branch's type and flagged the other two
  as `arg-type` errors. Fixed with an explicit
  `asset: MetadataTable | SemanticModelVersion | ContextProductVersion | None` annotation.
  Runtime behavior was already correct — this was a type-checker-only defect, but a real one
  (would fail a `mypy` CI gate).
- My own `tests/test_mcp_server.py::test_native_lineage_tool_slugs_match_declared_definitions`
  (written last session) hard-coded the expected native-tool slug set to only
  `{"get_lineage_graph", "get_lineage_impact"}`. The other model legitimately added
  `resolve_entity` and `get_transformation_detail` as real, fully-wired native tools (not
  stubs — traced both handlers), so my test was failing against correct new behavior. Updated
  the assertion to the current four-tool set.

**Findings — verified as non-issues:**
- The context-compiler's `YAML` target sets `content_type: application/yaml` but the body is
  canonical JSON. Not a defect: JSON is a valid subset of YAML 1.2, so the content is valid
  YAML, just not idiomatically formatted (no `pyyaml` dependency exists in the project to do
  better yet). Documented as a simplification in `02-epic-backlog.md` (EE.9) rather than fixed.
- The previously-noted flaky `tests/test_context_products.py` (`AttributeError: '_Result' object
  has no attribute 'all'`, intermittent) did not reproduce across 6 consecutive runs (1 full run
  + 5 targeted re-runs) after the other model's changes. Whatever caused it earlier appears to
  already be resolved; no fix was needed or applied.
- Org-scoping, role-gating, and maker-checker patterns across `product_marketplace_api.py` and
  `context_compiler_api.py` consistently follow the codebase's existing conventions
  (`enforce_organization` called before any read/write on every scoped lookup; role-based
  discoverability filtered at the SQL level, not post-filtered in Python; cache/audit keys
  scoped by organization before the caller's authorization is checked). No authorization gaps
  found.
- `context_product_policy.py`, `lineage_cache.py`, and the `unified_lineage_api.py`/
  `mcp_server.py` hardening diff (bounded `register_node`/`register_link` helpers replacing my
  original unbounded `nodes.setdefault(...)` calls, org+datasource-scoped Redis cache keys,
  quality-gated context-product access, `ContextProductConsumptionEdge` tracking) are genuine
  improvements over what this session shipped last time, not regressions.

**Findings — flagged, not fixed (need a decision, not just an edit):**
- `ai_asset` / `ai_asset_version` / `ai_assessment` have models, a migration, and Pydantic
  schemas (including an `AiTrustScoreRead` contract) but no API/service layer and no trust-score
  computation function anywhere in the codebase — the schema has no producer. `EE.11` downgraded
  from "open" to "partial — data layer only" rather than claimed delivered.
- `scratch/repo_bundle{3..8}.tar.gz` / `repo_live.tar.gz` (~5.4 MB of binary tarballs) and
  `proof-gaps-round-*-report.md` files are committed to git history and `scratch/` is not in
  `.gitignore`. Left alone: removing tracked history is a decision for the user, not something
  to do unilaterally mid-review.
- No dedicated unit tests exist yet for `resolve_entity`, `get_transformation_detail`,
  `product_marketplace_api.py`, or `context_compiler_api.py` (only the slug-set test, now
  fixed, indirectly touches the first two). No leak/cross-org test for the two newest MCP tools.

**Verification:** built a fresh Linux `uv` venv (`UV_PROJECT_ENVIRONMENT=/tmp/aida-venv`), ran
`ruff check` (clean on every file this pass touched or fixed; pre-existing `E501` line-length
warnings in the other model's new files were left alone as cosmetic), `mypy --cache-dir=/tmp/mypy_cache`
(clean on every file reviewed, after the one fix above), and the full `pytest` suite (all green,
including 6 consecutive clean runs of the previously-flaky file).

**Caveat this session flagged to the user directly:** the repository was being actively written
to during this review — new untracked files (`context_compiler.py`, `product_marketplace_api.py`,
the `b4e8f2a71c90` migration, `platform_schemas.py`) appeared with modification timestamps only
seconds to minutes old partway through, and a `.git/index.lock` was present for over ten minutes
without the index itself changing. No git write operations (commit, `rm --cached`, etc.) were
performed this pass to avoid racing whatever process holds or held that lock; all fixes above are
uncommitted working-tree edits only.

**Not built, tracked as open work:** trust-score computation and AI-registry API layer (`EE.11`
remainder), dedicated tests for the four new modules above, a leak test for `resolve_entity` /
`get_transformation_detail`, idiomatic YAML compilation target, contract breaking-change
approved-exception override, and the `scratch/` repo-hygiene cleanup.

### R34 agentic data platform foundation completion

- Completed the data product and contract control plane: immutable versions, typed ports,
  normalized producer/consumer roles, structural compatibility checks, independent
  breaking-change exceptions, publication/supersession/retirement, and audited access
  request/approve/reject/expire/revoke lifecycle.
- Added policy-filtered marketplace REST/UI surfaces and a deliberately bounded MCP write:
  agents may request product access but cannot grant it or bypass maker-checker review.
- Added the deterministic Context Compiler with stable hashes, structural drift evidence,
  quality/lifecycle gates, and MCP, REST, YAML, OSI, ODCS, Snowflake Semantic View, and
  Databricks Metric View targets.
- Added a tenant-scoped AI asset registry, immutable versions, independent assessments,
  maker-checker publication, and deterministic explainable trust scoring. Seven inspectable
  factors total exactly 100 points; prohibited risk, critical runtime incidents, missing or
  failed assessments, and weak high-risk evaluations are explicit blockers.
- Completed MCP prompts, deterministic fuzzy entity resolution, redacted dbt transformation
  detail, atomic Redis consumer budgets, and governed marketplace access requests. Budget keys
  hash principals and production fails closed if an enabled budget store is unavailable.
- Extended the Kafka/Neo4j projector with generation-stamped unified FK, approved-relationship,
  dbt, and OpenLineage nodes/edges. Optional Neo4j impact reads are bounded and fail open to the
  authoritative PostgreSQL graph; Redis remains an optional response cache.
- Added marketplace, authoring, compiler, AI registry, assessment, and trust-factor UI surfaces;
  added migration `b4e8f2a71c90`; and consolidated pure behavior/OpenAPI coverage in
  `tests/test_agentic_platform.py`.
- Remaining scale expansion is explicit rather than hidden: purpose ABAC/workload identity,
  entitlement-provider fulfillment, managed compliance templates/remediation, provider sync,
  idiomatic YAML/downloads/external validators, million-node projection certification,
  broader MCP stewardship writes, privacy operations, adoption analytics, and CP-S8 ecosystem
  integrations.


## 2026-08-29 (continued) — Second review pass: AI registry / MCP budget, and a correction

User repeated the "review the code / update the document / fix if needed" request. By this
point the repo had settled (the other model's process finished; `.git/index.lock` was gone) and
everything from the prior entry had been committed in `434e98d "Build agentic data marketplace
and AI trust platform"`, including this session's router-wiring and `mypy` fixes.

**Correction to the entry above:** it claimed "no dedicated tests exist yet" for
`product_marketplace_api.py`, `context_compiler_api.py`, `ai_registry_api.py`, and
`mcp_budget.py`. That was wrong — `tests/test_agentic_platform.py` (282 lines, 10 tests)
already covered contract compatibility, product-port validation, marketplace access-expiry,
context-compiler determinism and drift, trust-score explainability with an incident blocker,
assessment scoring, raw-evidence rejection, fuzzy-entity scoring, disabled-budget behavior, and
an OpenAPI route-publication smoke test — the search that missed it only grepped for
`ai_registry|mcp_budget|marketplace|compiler` in filenames, which `test_agentic_platform.py`
doesn't match. `02-epic-backlog.md` and `05-gap-register.md` have since been corrected (by the
same process that built this code) to credit that file; no further doc fix was needed there.

**Reviewed this pass:** `ai_registry.py` (`compute_ai_trust_score`, `score_assessment_controls`)
and `ai_registry_api.py` (full AI-asset lifecycle: create/version/submit/assess/trust, wired
into `semantic_api.py`'s maker-checker dispatcher under `AI_ASSET_VERSION`, including the
one-approved-per-asset supersede-on-approve logic), and `mcp_budget.py` (Redis
`INCR`+`EXPIRE` Lua-script token counter, wired into `mcp_endpoint` for `REQUEST_MINUTE` /
`TOOL_DAY` / `CONTEXT_DAY` buckets, fail-closed in staging/production, fail-open in
development). No bugs found — `ruff` and `mypy` clean, and the maker-checker approval path
correctly supersedes the prior approved version.

**Added:** `tests/test_ai_registry.py`, 11 tests giving `compute_ai_trust_score` and
`score_assessment_controls` edge-case coverage `test_agentic_platform.py` didn't have:
`PROHIBITED` risk tier, `HIGH` risk below the evaluation threshold, a missing assessment alone
(vs. bundled with an incident), a failed assessment alone, and `score_assessment_controls` with
empty and `NOT_APPLICABLE`-only control lists. Full suite green (was already green; this only
added coverage, changed no behavior).

**Open at that review point:** idiomatic YAML compilation, file-export delivery,
entitlement-provider fulfillment, managed compliance templates/remediation/retirement APIs,
provider sync, score history, dependency-graph visualization, and repo hygiene. The production
features in this list were subsequently closed by R35 below; shared-history cleanup remains.

### R35 production acceptance and control-plane hardening

- Applied migrations `b4e8f2a71c90` and `c8a4d3e91f02` to live PostgreSQL and verified the
  expected evidence tables. Redis and Neo4j live probes passed; the rebuilt API reported ready.
- Enforced OIDC-backed MCP workload principal types outside development, propagated bounded
  business-purpose claims, added exact purpose ABAC to Context Product REST/compiler/MCP reads,
  and persisted generic immutable MCP consumption evidence without prompts, SQL, or values.
- Added idempotent entitlement fulfillment state and outbox/webhook adapters. Governance remains
  authoritative when providers fail; provisioning and revocation are independently retryable.
- Added managed EU AI Act, NIST AI RMF, and AI-UC assessment templates; durable remediation and
  independent risk acceptance; maker-checker retirement; immutable trust history; value-free
  provider evidence sync; and dependency graph APIs.
- Added idiomatic deterministic YAML, validated attachment downloads, and structural conformance
  checks for MCP, REST, YAML, OSI, ODCS, Snowflake, and Databricks compiler targets.
- Enabled Redis lineage caching, MCP budgets, and Neo4j lineage reads in the local integration
  stack while retaining production fail-closed/fallback behavior defined in code.


## 2026-08-29 (continued) — Local portfolio analytics completion and verifier hardening

### Completed

- Added tenant-scoped portfolio analytics summary and trend APIs in `product_marketplace_api.py`
  over existing product, contract, context-read, MCP, tool, query, quality, and agent evidence.
- Extended `scripts/verify-local.ps1` to create and publish a Context Product, publish a linked
  Data Product and Data Contract, request and approve marketplace access, provision the
  entitlement through the outbox-backed local path, and verify the new portfolio analytics
  endpoints end to end.
- Fixed three real local defects uncovered by that verifier pass: marketplace search used
  `DISTINCT` across JSON-backed version rows and failed on PostgreSQL; marketplace access
  requests could flush before their governance-review row existed and misreport the resulting
  foreign-key failure as "already pending"; and governance approval/outbox plus marketplace
  access-request listing both returned non-JSON-safe payloads.
- Added regression coverage in `tests/test_agentic_platform.py` for portfolio trend bucketing,
  marketplace access-request flush ordering, and governance outbox expiry serialization.

### Verification evidence

- Repo-wide static and test gates passed on Saturday, August 29, 2026: `379` tests passed, Ruff
  clean, and strict mypy clean.
- Final local verifier run passed on Saturday, August 29, 2026 with organization
  `abe5877e-e12e-4095-88a4-411562a763f6`, datasource `d623616a-9df9-48d4-bbc5-3b4e51d20208`,
  analysis run `0545b916-bd88-4e46-b322-a0bfde07bfcb`, Context Product version
  `cf6ebf7b-2d69-48a4-b001-015f0ecbb13d`, Data Product version
  `3e18b674-dba8-4f46-9f45-6c24764ea8fb`, Data Contract version
  `cd5308d0-44ca-4756-82d3-8094c951ebf6`, marketplace access request
  `dff2bda5-f0ed-4e2c-ab18-3817efb7a885`, and tool-first agent run
  `eacc8511-28c8-4d41-8c92-c5ae3179f3e6`.
- The same verifier proved `portfolio_access_requests = 1`, `portfolio_context_reads = 1`,
  `portfolio_agent_runs = 3`, `portfolio_top_product_key = customer_portfolio_1788039914`, and
  an outbox-backed entitlement state of `PENDING`, which is the correct local fail-safe posture
  without an external fulfillment provider.

### Current limitations

- The remaining open items are the dedicated-environment gates rather than local code-path gaps:
  million-node lineage/load certification, authoritative BI/procedure lineage, privacy
  operations, workflow templates, external provider certification, and browser/accessibility QA.

## 2026-08-29 (continued) — MCP lineage-tool coverage completion

### Completed

- Added dedicated unit coverage in `tests/test_mcp_server.py` for the two newest native
  lineage MCP tools, `resolve_entity` and `get_transformation_detail`.
- The new tests cover input validation, anti-enumeration denial symmetry for ineligible callers,
  successful value-free JSON payload rendering, and the not-found branch for transformation
  detail reads.
- This closes the local code-review gap that previously noted the tools existed in production
  code but only had slug-level coverage in the test suite.

### Verification evidence

- Focused MCP verification passed on Saturday, August 29, 2026:
  `python -m pytest tests/test_mcp_server.py -q` (`29` passed),
  `python -m ruff check tests/test_mcp_server.py src/aida/mcp_server.py`, and
  `python -m mypy src/aida/mcp_server.py`.

### Current limitations

- The remaining open items are still dedicated-environment gates rather than local code-path
  gaps: million-node lineage/load certification, authoritative BI/procedure lineage, privacy
  operations, workflow templates, external provider certification, and browser/accessibility QA.

## 2026-08-29 (continued) — AI registry dependency graph UI completion

### Completed

- Extended the `ai-registry` portal view to render governed AI dependency topology using the
  shared graph engine already used by Knowledge Graph and Unified Lineage.
- Added operator actions for dependency inspection and retirement requests directly from the AI
  asset portfolio table, reusing the existing `/ai-asset-versions/{version_id}/dependencies`
  and `/ai-assets/{asset_id}/retire` API paths.
- Added a value-free side panel that shows the selected asset or dependency node's status,
  owner, provider, dependency counts, and approved references without exposing prompts or source
  values.

### Verification evidence

- Repo-wide gates remained green on Saturday, August 29, 2026 after the UI change:
  `python -m pytest -q`, `python -m ruff check .`, and `python -m mypy src`.
- The full local verifier passed again on Saturday, August 29, 2026 with `status = PASS`,
  `ui_status = HEALTHY`, and `ui_url = http://localhost:3000`, preserving the same end-to-end
  workflow evidence for Context Products, marketplace access, AI registry/trust, and portfolio
  analytics.

### Current limitations

- The remaining open items are still dedicated-environment gates rather than local code-path
  gaps: million-node lineage/load certification, authoritative BI/procedure lineage, privacy
  operations, workflow templates, external provider certification, and browser/accessibility QA.

## 2026-08-29 (continued) — Refactor Phase 0: import-linter ratchet + `platform/` extraction (ST-01–ST-04)

### Completed

- Added `[tool.importlinter]` to `pyproject.toml` with `root_packages = ["atlas"]` and a
  `platform-is-the-lowest-layer` layers contract (`atlas.modules` → `atlas.platform`, never the
  reverse). Scoped as a permissive baseline: only the target `atlas` package is checked, matching
  Phase 0's ratchet design — `aida`, the pre-existing flat package, is intentionally out of scope
  until the strangler migration reaches each module.
- Extracted `db.py`, `config.py`, `logging.py`, and `context.py` from `aida` into
  `atlas.platform`, adapting `db.py`'s internal `config` import to the new location. Left a
  backward-compatible re-export shim at each old `aida.*` path so the 40+ existing import sites
  across `src/aida/*` and `tests/*` needed no changes.
- Added `src/atlas` to the `hatchling` wheel package list so the extracted modules are included
  in production builds, not just the editable dev install.
- Confirmed `scripts/generate_module.py` (tracker ST-01) and `tests/test_tier0_invariants.py`
  (tracker ST-03, 4 of 9 invariants) already existed from prior work; the tracker had gone stale
  and still listed both as `TODO` — corrected to reflect actual repo state.
- Deliberately did **not** touch `models.py`, `schemas.py`, `api.py`, or any Phase 2+ work — a
  concurrent session was actively editing those same files for ADR-0017 (domain-complete tenancy)
  while this work was in progress, and the refactor plan itself calls Phase 2 (the models/schemas
  split) the one phase needing a migration freeze.

### Verification evidence

- Built an isolated Python 3.13 verification environment (`uv venv` + `uv pip install -e ".[dev]"`)
  outside the repo, since the checked-in `.venv` is a Windows venv not runnable from this session.
- `python -m pytest -q`: baseline before any change was fully green (no failures). After the
  change, all tests pass except 3 in `tests/test_operational_behaviors.py`
  (`test_scheduler_commits_run_and_evidence_before_workflow_dispatch`,
  `test_scheduler_defers_rejected_admission_without_dispatch`,
  `test_due_scan_policies_statement_orders_by_priority_then_next_run_at`) — confirmed via
  `git diff` to belong to the concurrent session's in-progress `computed_usage_boost` scheduling
  feature (ADR-0017 §8), not this change: `scheduler.py` and `models.py` were mid-edit for that
  feature throughout this verification, unrelated to `db`/`config`/`logging`/`context`.
- `pytest -q src/atlas/modules/identity_tenancy` (standalone module execution) passes.
- `lint-imports`: `platform-is-the-lowest-layer KEPT` — `Contracts: 1 kept, 0 broken`.
- `ruff check` and `mypy` (strict) clean on every new and changed file.

### Current limitations

- ST-02's exit criterion ("new violations fail CI") is not fully met: this repo has no CI
  pipeline at all yet (no `.github/workflows`), so the contract passes locally but isn't enforced
  automatically. Setting up CI is a separate, larger gap.
- ST-03 remains 4 of 9 invariants; the other 5 (INV-1, INV-5, INV-6, INV-7, INV-9) need
  infrastructure that does not exist locally yet (live Neo4j/search replay, an all-endpoints fake
  session harness, a certification-result store) — see the docstring in
  `tests/test_tier0_invariants.py` for the reasoning per invariant.
- ST-04 covers 4 of the ~10 files/areas Phase 1 names. `main.py` was deliberately left where it
  is: it currently imports nearly every domain router (violating `platform-purity` as-is), and the
  refactor plan's own sequencing defers untangling that to Phase 5 (the `api.py` router split)
  rather than moving it in its current shape. `events.py` and the pagination/idempotency/
  error-taxonomy/telemetry scaffolding remain unbuilt.
- Phases 2 and onward (splitting `models.py`/`schemas.py`, extracting leaf/runtime modules) are
  untouched — see `Docs/60-delivery/03-tracker.md` ST-05 onward.

## 2026-08-29 (continued) — Refactor doc corrections found during ST-04 verification

### Completed

- Checked whether Phase 2 (models/schemas split) had unblocked since the last entry: it hadn't —
  `models.py`, `schemas.py`, and `api.py` were still uncommitted and actively changing under the
  concurrent session (a new file, `context_product_api.py`, picked up a modification between
  checks). Left Phase 2+ untouched again; did documentation-only work instead that needed no code
  freeze.
- Corrected `40-engineering/06-refactor-plan.md` Phase 1: it listed `events.py` (outbox
  mechanics) as moving to `platform/`. Read the file — it directly constructs and writes
  `AuditEvent`/`OutboxEvent` (`aida.models`), module 20's owned tables per
  `10-architecture/04-module-decomposition.md` §4 and §9, not domain-free infrastructure. Moving
  it to `platform/` as written would have failed the `platform-purity` contract (ST-02) on day
  one. `04-module-decomposition.md` §9 already had the correct target (module 20, Phase 3/4);
  fixed the refactor plan to match, and fixed the same incorrect claim in
  `src/atlas/platform/__init__.py`'s docstring (written in the previous entry).
- Flagged a real, previously undocumented architectural tension in
  `10-architecture/04-module-decomposition.md` (new §5.3): three L2 modules (`05` profiling, `09`
  lineage, `11` data-quality) depend on `16 query-gateway`, an L3 module, contradicting the
  document's own layering rule; separately, `09` and `16` list each other as callable, which is a
  cycle contradicting the `no-cycles` contract the same document says CI will enforce. Added
  tracker `ST-11` (P0, unassigned) so this is resolved before Phase 4 extracts those modules,
  rather than being discovered mid-extraction.

### Verification evidence

- `ruff check` clean and `ast.parse` valid on the one `.py` docstring touched
  (`src/atlas/platform/__init__.py`); `pytest -q src/atlas/modules/identity_tenancy` still passes.
  No other code changed in this entry — documentation only.

### Current limitations

- ST-11 is flagged, not resolved — it needs an architecture-owner decision (redraw the layer
  diagram to move `16` down, or narrow what `05`/`09`/`11` actually need from it), not something
  to decide unilaterally.
- Phase 2 (models/schemas split) and the leaf-module extraction it unblocks remain untouched;
  still gated on the concurrent session's ADR-0017 work landing.
