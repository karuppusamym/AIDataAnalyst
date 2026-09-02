# Delivery Status

> Status: **Living document — the single answer to "where are we".** Owner: Engineering lead.
> Consolidated 2026-08-30 from `04-status-matrix.md` and `05-gap-register.md`, both now in
> `Docs/_superseded/`. If a status claim appears in two places, this one wins; every other
> document should carry a pointer here rather than its own summary.

**Verified:** 2026-09-02, against the working tree at commit `fd70428`. (This branch has been under
continuous concurrent push across many parallel sessions since 2026-08-30 — every number below is
"true as of" this commit, not a stable fact, and will drift again within hours. The §1 table facts
were re-derived directly from tooling — `pytest`, `mypy`, `ruff`, `alembic heads`, a file count of
`10-architecture/adr/` — not carried forward from the prior 2026-08-30 17:35 UTC snapshot, which
had gone stale on several of them: the test suite had nearly tripled, the Alembic head had moved,
and a real `mypy`/`ruff` regression from concurrent work — 46 "object not callable" errors from a
type-erasing `__getattr__` shim, plus 13 line-length/subprocess-safety errors — had crept in and
was found and fixed as part of this pass. See `06-accomplishment-log.md` for what changed between
checks, and `03-tracker.md`'s §K for the current item-level DONE/TODO/IN-PROGRESS/BLOCKED count
(267 tracked items, 177 DONE as of the same commit).

> **Scope of this pass.** This refresh corrects §1 ("At a glance") against live tooling output and
> fixes the two things that were unambiguously wrong (stale counts, a real static-analysis
> regression). It does **not** re-verify every row of §4's capability matrix, §3's invariant table,
> or §7's gap register against current code — those were largely kept current by the sessions that
> did the underlying work (most §4 rows already cite specific recent tracker IDs), but nobody has
> done a dedicated line-by-line audit of the whole document since 2026-08-30. Treat prose beyond §1
> as directionally reliable, not independently re-verified today.

## 1. At a glance

| | |
|---|---|
| Test suite | **6,497 passing, 11 skipped, 1 expected failure** (6,509 collected; the xfail is INV-9's known gap, §3), no unexpected failures, no external service required |
| Static quality | `mypy --strict` clean on **283** files · **8** import-linter contracts kept. `ruff` clean except **1** pre-existing, unrelated `E501` (an overlong test function name, out of scope to rename) |
| Migrations | **1** Alembic head, `ca56d6ce3f18` |
| Architecture decisions | **22** recorded, 1 superseded (ADR-0017 → ADR-0018) |
| Invariants | **9 of 9** have an automated test; 3 carry a named limit (see §3) |
| Authorization | Wired into the execution path and 5 read surfaces; **enforcing nothing** (shadow mode, §3 INV-4) — unverified this pass whether more read surfaces have been wired since 2026-08-30 |
| Open decisions | **4** — two were answered on 2026-08-30 (the embedding model, and Neo4j) (§6) — not re-audited this pass |

The single most important sentence on this page: **everything in the "Implemented" column below
has local end-to-end evidence and nothing has bank-scale evidence.** That distinction is held
consistently, and the day it stops being held this document stops being worth reading.

### 1a. Four defects the linters were hiding

The 184 reported `ruff` and `mypy` errors read as style noise. Four were not, and each would
have failed at runtime:

| Where | Defect |
|---|---|
| `observability.traced` | On any exception the wrapped function's error was swallowed by `except Exception: pass` and the function was then **called a second time** by the fallback path — a silent duplicate side effect, with the caller seeing only the second failure. Tracing must never change how many times the thing it observes runs |
| `studio_api` | `record_outbox` was called without `aggregate_type` or `aggregate_id`, both required — a `TypeError` on every change-set submit |
| `search_api` | `UUID(hit.datasource_id)` where the value is already a `UUID` — a `TypeError` for every search hit that had a datasource |
| `observability.MetricsConfig` | `configure_metrics` reads `config.insecure`, which the dataclass did not define — `AttributeError` the moment metrics were enabled |

Two more were narrowed rather than fixed: a bare `except Exception` inside the injection
detector, where a wide silent catch makes a decoder bug read exactly like "nothing found", and
`assert False` in a test, which `python -O` removes entirely.

The general point is worth keeping: **a lint backlog is where real defects hide**, because
nobody reads 168 warnings looking for the four that matter.

## 2. Status definitions

| Status | Meaning |
|---|---|
| **Implemented** | Code, migration, API/runtime path, automated checks, and local end-to-end evidence exist |
| **Partial** | A safe production-oriented vertical slice exists, but named breadth or certification remains |
| **Pending** | Planned capability, not implemented |
| **Retest required** | Implemented locally but requires bank-scale, security, recovery, or target-environment certification |
| **Bank decision** | Must remain fail closed until an enterprise standard is supplied |

## 3. Invariant status

Nine invariants are binding on design review. By this project's own standard *an invariant
without an automated test is a wish* — at the start of the 2026-08 review four were wishes. All
nine now have tests. **Having a test is not the same as the property holding**, so each limit is
named rather than rounded up to a tick.

| # | Invariant | Test | Limit that remains |
|:--:|---|---|---|
| INV-1 | Single authoritative store | `test_inv1_single_authoritative_store.py` (8) | Does **not** prove Neo4j ingests correctly — no Neo4j runs in the suite. The projection-rebuild drill has never been run (E5) |
| INV-2 | One execution choke point | Type system + import contract + AST scan | None. The statement was narrowed so it is literally true: discovery and profiling touch sources but cannot carry caller SQL |
| INV-3 | Model output is never authority | `test_tier0_invariants.py` | None |
| INV-4 | Fail closed | `test_tier0_invariants.py` + `test_inv4_authorization_wiring.py` (26) | The decision is now *reached* on the execution path and 5 read surfaces, but every workspace is in `SHADOW` and the unresolved-workspace posture defaults to `SHADOW` — **so nothing is denied**. See §6 decision 3 |
| INV-5 | Tenant isolation is total | `test_inv5_tenant_isolation.py` (8) + route-table scan | The intended structural mechanism — a repository base class with no unscoped query helper — **does not exist**. Scoping is per-query by convention; the test substitutes for the guarantee (ST-05/06/07) |
| INV-6 | Value-freedom of control-plane state | `test_inv6_value_freedom.py` (13) | None outstanding. A real leak was found and closed here: envelope v1.1 stored view and routine SQL raw, outside the scan's reach (migration `d5f8b21c4a03`) |
| INV-7 | Attributability of high-impact actions | `test_inv7_attributability.py` (11) | None outstanding. Found 13 endpoints committing governed state with no audit row; all 13 now audit and the exemption list is empty and asserted empty |
| INV-8 | Maker ≠ checker | `test_tier0_invariants.py`, per object type | None |
| INV-9 | Honest capability reporting | `test_inv9_capability_honesty.py` (11) | **One strict xfail remains**, and it records a codebase gap rather than a suite gap: capability flags are hand-declared rather than derived from the certification result (E12) |
| INV-10 | *Generated knowledge is never silently authoritative* | — | **Proposed, not accepted.** See §6 decision 4 |

## 4. Capability matrix

| Capability | Mod | Status | Implemented now | Remaining or next evidence |
|---|:--:|---|---|---|
| Organization / LOB / project tenancy | 01 | Implemented | Stable hierarchy, organization enforcement, paginated tenant inventory, guided onboarding, audit and outbox evidence | Bulk onboarding. (Legal entity is withdrawn, not pending — ADR-0018) |
| Workspace tenancy, source bindings, business graph, ABAC (ADR-0018) | 01, 17 | Implemented | Workspace + membership + expiring source bindings with maker-checker approval; effective-dated classification tree with recursive-CTE roll-up and `as_of` history; pure ABAC evaluator with DENY as a hard ceiling and agent-vs-human as a first-class attribute; one fail-closed authorization entry point; authorization-probe endpoint; INV-5 formalised in the Tier-0 suite; **wired into the execution path (the INV-2 choke point), the validation path and five read surfaces**, with subject-independent workspace resolution and a static scan (`test_inv4_authorization_wiring.py`) that keeps it wired | **Decides nothing in production yet by design: every workspace is in `SHADOW` and the unresolved-workspace posture defaults to `SHADOW`, so the system measures and does not deny.** Completing the rollout is flipping `unresolved_workspace_posture` to `DENY` per environment once clients pass `workspace_id` — 17 tests fail under that setting today, and they name the surfaces still to migrate; reads outside the five wired surfaces (query lineage, glossary, stewardship, semantic, marketplace) are ungated; residency attribute; decision latency measured only on a synthetic estate (0.8 ms auth hot path), not the bank's catalogue |
| Enterprise authentication | 01 | Partial | Signed OIDC/JWKS verification — issuer, audience, time, algorithm; JWKS cache/refresh; pinned keys; configurable claim paths and role mapping; local identity is development-only | Bank issuer/claims/groups certification; workload identity; revocation/replay policy; break-glass |
| Enterprise secrets and source identity | 01 | Partial | Inline secrets rejected; one configured provider scheme; provider-neutral adapter contract; strict reference parsing; bounded cache with rotation invalidation; production rejects `env://` | Register and certify the bank adapter; workload identity; rotation/outage drills; delegated read-only source identities |
| Source registry and ingestion | 02, 03 | Partial | Registration, secret references, capability negotiation, source operations; envelope `1.0` with atomic synchronous ingestion; persisted idempotent manifests/chunks; Temporal retries/heartbeats; cross-chunk FK resolution; deferred FULL reconciliation; payload cleanup; full Atlas workbench | Bulk onboarding; signed producers; Kafka/schema registry; pause/cancel; admission quotas; explicit replay tooling; maximum-scale recovery certification |
| PostgreSQL connector | 02 | Implemented for the current contract | Discovery, read-only execution, EXPLAIN cost, timeouts, bounded profiling, capability/version definition, deterministic per-source conformance evidence | Version fixtures; load/cancellation/recovery certification; delegated identity |
| Microsoft SQL Server connector | 02 | Partial (`BETA`) | Real Docker fixture passing connectivity, exact 4/22/7 discovery, bounded profiling, database-scoped SHOWPLAN cost, governed query/masking, 100-point certification | Multi-version, TLS/private-network, delegated-identity, load, cancellation, recovery certification |
| Oracle connector | 02 | Partial (`BETA`, unverified live) | Native async adapter (`python-oracledb` thin mode), canonical DSN parsing, `ALL_TAB_COLUMNS`/`ALL_CONSTRAINTS` discovery reusing shared helpers, real session-ID capture, LOB-aware bounded profiling, compose fixture | Live container verification; certified least-privilege `PLAN_TABLE` path before enabling EXPLAIN (currently fails closed with `QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR`) |
| BigQuery connector | 02 | Partial (`BETA`, unverified live) | Native pull adapter with a single structured service-account/workload-identity credential shape (no fake DSN), GCP project→catalog and dataset→schema mapping, `INFORMATION_SCHEMA`-based discovery with honest foreign-key omission, dry-run byte estimate feeding a new connector-agnostic byte-budget gate in the query gateway, bounded profiling with explicit byte/row/timeout limits | Live GCP project verification; certification; version fixtures |
| Snowflake connector | 02 | Partial (`BETA`, unverified live) | Native pull adapter with JSON-or-URI credential parsing, multi-database `INFORMATION_SCHEMA` discovery via shared assembly helpers, partition-pruned `EXPLAIN USING JSON` cost estimation, `APPROX_COUNT_DISTINCT` bounded profiling, real warehouse query-ID (`sfqid`) capture | Live account/warehouse verification; certification; version fixtures |
| Databricks connector | 02 | Partial (`BETA`, unverified live) | Native pull adapter (`databricks-sql-connector`) with JSON-or-URI (PAT) credential parsing, Unity Catalog per-catalog `INFORMATION_SCHEMA` discovery via shared assembly helpers (PK/UNIQUE constraints, best-effort FOREIGN KEY, table/column/schema/catalog comments), `EXPLAIN COST`-based cost estimation, `APPROX_COUNT_DISTINCT` bounded profiling, warehouse query-ID capture | Live workspace verification; certification; version fixtures; view/routine/grant discovery axes; delegated/OAuth identity |
| Other connectors | 02 | Pending | Teradata, Db2 are visibly `PLANNED` (canonical push ingestion only; no native pull adapter). Databricks moved from `PLANNED` to a real pull adapter (CN-2b) | Implement and certify Teradata/Db2 in bank priority order |
| Connector certification | 02 | Implemented for control-plane conformance | Immutable certification runs scoring implementation, secret reference, connection, hierarchy capability, inventory, canonical push evidence; capability/maturity/transport matrix with drill-down | Executable vendor/version fixtures; source-side agents; load, retry, cancellation, least-privilege, recovery packs |
| Catalog inventory | 04 | Implemented | Catalog/schema/table/view/column/constraint with stable identity, fingerprints, drift counts, tombstones, reactivation; bulk stewardship actions (tag/classify/own/certify) with explicit-or-filter selection, a 500-item cap with `truncated` reporting, per-item SAVEPOINT isolation and partial-success reporting, mirroring PG-3's pattern (`catalog_bulk_actions.py`, `POST /v1/organizations/{organization_id}/tables/bulk-{tag,classify,own,certify}`) — verified against a real in-memory-sqlite database at full-batch (500-item) scale drawn from thousands-row candidate pools, including a real `IntegrityError` proven contained to one item's SAVEPOINT (`tests/test_catalog_bulk_actions_endpoints.py`); this also caught and fixed a pre-existing defect where `CatalogBulkActionRun`'s ORM model was missing the `requested_by` column its own migration and persistence code required, which would have crashed every real call; `GET /v1/organizations/{org}/catalog/rows` (UX-12) composes description/proposal-state, owner, certification, quality, glossary terms and row estimate per row in a fixed, page-size-independent number of queries — the read model the `ui-next` catalog screen needs, replacing five per-row calls | Index/partition models; virtualization; rename detection |
| Safe profiling | 05 | Implemented | Table estimates and value-free null/distinct/length statistics with sampling and hard bounds | Policy-approved ranges/top-values by classification; freshness-relevant profiling |
| Classification | 05 | Implemented | Deterministic rules with evidence and algorithm version | Authoritative external classification feed |
| Durable analysis workflow | 05 | Implemented | Temporal discovery plus independently retryable table-task DAG with heartbeats, cancellation, resume | Continue-as-new at maximum source scale |
| Fleet scheduling | 03 | Implemented | HA-safe policy polling, priority, maintenance windows, organization quotas, per-source admission, backpressure | Fairness and capacity testing across thousands of sources |
| Schema drift / reconciliation | 04 | Implemented | Created/changed/deprecated evidence, soft deprecation, reactivation, graph lag summary | Enterprise notification and retention policy |
| Declared PK/FK graph | 06, 10 | Implemented | Constraint inventory and Neo4j FK relationships | Projection performance at millions of nodes |
| Inferred relationships | 06 | Partial | Bounded metadata-only candidates, enriched named edges, confidence/evidence, durable review, negative knowledge; Graph Explorer V2 with server-side search, 1–4 hop directional traversal, policy caps, truncation reasons, evidence inspection | Composite candidates; statistical evidence policy; projection of approvals to Neo4j; cross-source traversal; million-node rendering certification |
| Temporal / table-family intelligence | 06 | Partial | Snapshot/history/delta/SCD Type 2 detection over naming/column evidence, maker-checker-reviewed `TableFamilyCandidate` rows (RL-1); canonical-table resolution with steward-override precedence over the algorithm's own pick (RL-2); bounded multi-column composite relationship candidates (RL-3); approved/suggested candidates now actually projected to Neo4j after a wiring-bug fix (RL-4); cross-source column matching survives real dialect differences via canonical-name and physical-type-family buckets (RL-5); bulk relationship review at up to 500 candidates per decision (RL-6) | Confidence calibration against a labelled banking corpus (RL-7) — bucket-vs-observed-approval-rate machinery exists, but no corpus exists in this environment so no calibration curve is published |
| SQL / query lineage | 09 | Partial | Durable referenced tables/columns, value-free SELECT output-to-source mappings, direct/derived classification, transformation names, governed-tool dependencies, API and Atlas visibility; OpenLineage run-event ingestion producing table- and column-level edges via a mounted API (`openlineage.py`/`openlineage_api.py`), untested and unverified against a real Airflow-sourced event | View/procedure lineage; OpenLineage test coverage and Airflow-sourced e2e evidence; warehouse history adapters |
| dbt transformation intelligence | 09 | Partial | Governed project registration, immutable manifest v12-compatible ingestion, model/source/test inventory, catalog matching, SQL hash plus literal-redacted SQL, dependency DAG, impact, agent retrieval, Atlas workbench; `run_results.json` ingestion driving test-status/failure evidence and automatic data-quality-incident open/resolve reconciliation (`dbt_quality_bridge.py`) | CI/dbt Cloud auth; column-level lineage; retention; large-DAG virtualization |
| Impact analysis | 09 | Implemented | Physical table to semantic metrics, governed tools, approved relationship evidence (direct, single-hop); separately, bounded transitive cross-kind traversal (`GET /v1/datasources/{id}/unified-lineage/impact/{node_id}`, MCP `atlas__get_lineage_impact`) across declared FK, suggested/approved relationships, dbt manifest dependencies, OpenLineage ETL edges, and (2026-08-31, LN-7) SQL-parsed view/procedure definition edges, with per-node hop depth and contributing edge-kind evidence, node/depth bounds, and org/RBAC policy scoping | BI report/metric node kinds folded into the unified graph (LN-11, depends on LN-4's Looker support); AI-decision edges (LN-3); authoritative column-to-column mapping (LN-10) |
| Business-semantic inference | 07 | Implemented for the metadata-structure slice | Bounded deterministic plus optional approved-LLM proposals for domains, entities, descriptions, table roles, grain, synonyms, questions, safe tool blueprints; metadata-only evidence; strict schema validation; maker-checker; authoritative annotations; cross-domain FK map | Inferred glossary binding; ambiguity/conflict resolution; confidence calibration; bank-domain evaluation corpus |
| Semantic models and metrics | 07 | Partial | Versioned metrics with grain/time/physical mappings, maker-checker publication, supersession, clone rollback; **2026-09-02 (Group A worktree): glossary-term-to-semantic-object binding (SM-2) — `TermSemanticBinding`, maker-checker activation, and a bound term's definition/synonyms actually participating in `retrieval.hybrid_retrieve`'s scoring in both directions, re-verified reachable through `GovernedAgentOrchestrator.run()`; this row was stale, SM-2 had already landed** | Governed dimensions (SM-1, blocked — see `03-tracker.md`); conflicts; metric suggestions from annotations |
| Glossary and stewardship | 08 | Implemented for the governed table-stewardship slice | Categories; immutable term definitions/synonyms; maker-checker publication, supersession and deprecation; manual/bulk/inferred links with provenance; individual/group and rule ownership; retained conflict resolution; expiring certification; six-dimension organization/source/domain/LOB coverage with snapshots; bounded unowned backlog; responsive control-center UI | Category administration; inheritance/leaver workflow; automatic certification-expiry events; fuzzy inference calibration; scheduled routing/trends; additional asset types; bank-scale and interactive accessibility certification |
| Hybrid retrieval | 12 | Partial | **2026-09-02 (Group A worktree), reconciled against `03-tracker.md` RT-1/RT-2/RT-3/RT-4/RT-9/RT-5, this row was stale — those had already landed and were re-verified reachable (`06-accomplishment-log.md`/AU-2), just not reflected here.** Organization/source-scoped lexical ranking across active tables, columns, approved annotations, published metrics, published tools, latest dbt artifacts; bounded evidence and selection reasons; policy filtering before ranking; PostgreSQL GIN full-text index (`full_text_index.py`, RT-4); live per-query embedding vector similarity (RT-1 — a *persisted*, rebuildable pgvector index, `vector_store.py`, is still not wired to any live path, tracked below); bounded graph expansion from seed hits over FK, dbt `depends_on`, and governed-tool `referenced_tables` edges (RT-2); RRF/weighted-linear fusion ranking with every factor inspectable in evidence (RT-3); two cross-source search surfaces — lexical-only `/v1/search` (RT-5/RT-9, pre-existing) and the fuller lexical+vector+graph+fusion `GET /v1/organizations/{id}/global-search` (RT-5/RT-9, new) | Persisted/rebuildable pgvector index wired to a live path (RT-1's own named gap); large-catalog retrieval benchmarks (RT-8) |
| Agent orchestration | 13 | Partial | Framework-neutral typed states including a pre-retrieval `SCREENED` gate; versioned deterministic prompt-risk classifier blocking instruction override, prompt/credential extraction, policy/masking bypass, privilege escalation, unbounded extraction; governed retrieval; approved-tool-first execution; bounded grounding; OpenAI/Gemini structured output; semantic/policy pinning; value-free trace and plan evidence | Multi-step tool plans; indirect-injection defences; multilingual/obfuscation coverage; retrieval/model benchmarks; bank model-risk corpus |
| Query execution gateway | 16 | Implemented | SQLGlot AST validation, allowlists, EXPLAIN/cost, read-only timeout, masking, redaction, HMAC evidence; QG-6 tokenization for opted-in columns (certified local/dev `TokenizationProvider`, gated/audited detokenize endpoint, Vault Transform adapter shape not yet exercised against a live Vault); QG-2 source-native row/column policy sync for Postgres RLS (row-level) and SQL Server DDM (column-level) — dry-run preview + maker-checker gated apply, with durable evidence (apply verified only against a mocked connection, not a live source) | Cancel propagation certification; per-LOB quotas; SQL Server native RLS and Postgres native column masking (documented future work); Vault Transform and apply certified against live services |
| Query memory | 13 | Partial | Value-free, semantic-version-aware evidence and feedback suppression | Similarity retrieval; safe adaptation; usage scoring; benchmark |
| Governed tools | 14 | Implemented | Versioning, strict parameter schemas, AST literal binding, RBAC, maker-checker publish/deprecate, audited execution, retrieval ranking, tool-first agent binding | Formal certification corpus; multi-tool plans; quality gating |
| Model route and AI governance | 15 | Partial | Immutable route versions with residency/retention/capability/budget contracts; credential-reference redaction; maker-checker lifecycle; honest activation states; OpenAI Responses and Gemini adapters with structured output, bounded retries/timeouts, non-content evidence; durable evaluations | Rotated credentials; bank-approved route selection; private routing/workload identity; retention certification; model-risk corpus; monitoring; **kill-switch drill** |
| Data-quality observability | 11 | Implemented for profile-baseline controls | Source/table policies; deterministic volume, null-rate, schema-fingerprint comparison; immutable value-free observations; fingerprinted incident lifecycle with auto-recovery; audited acknowledge/resolve; metadata scan-age posture; automatic Temporal integration; Atlas investigation workspace | Approved watermark contracts for freshness; rule scheduling beyond scans; notification routing; SLOs; bank-scale incident certification; **runtime coupling** |
| Policy and governance | 17 | Partial | RBAC role gates, organization enforcement, unified cross-object review queue with filters, rationale capture, independent decisions, platform-enforced maker≠checker | ABAC; purpose-based access; agent-vs-human context; bulk decisions; delegation; entitlements; full decision logging |
| Audit and event delivery | 20 | Implemented | Attributable audit ledger, transactional outbox, idempotent publication, retry/backoff, dead-letter, authorized requeue | WORM archive; retention; SIEM/SOC routing; OpenTelemetry; access review |
| Context products and MCP | 19 | Partial | JSON-RPC 2.0 MCP endpoint (`POST /mcp`); immutable Context Products with maker-checker publication/deprecation; policy-gated REST/MCP reads; role-filtered tools; Redis budgets; marketplace request/approve/provision-pending lifecycle; deterministic compiler; AI registry/trust; and tenant-scoped portfolio analytics summary/trend APIs, all with local end-to-end evidence | Million-node lineage/load certification; authoritative BI/procedure lineage; privacy operations; workflow templates; workload identity; external provider certification; browser/accessibility and bank-scale security certification |
| Studio | 18 | Partial | Change-set lifecycle (DRAFT→TESTING→SUBMITTED→MERGED/REJECTED) with base-version conflict detection; synthetic-fixture test harness gating submission; field-level semantic diff view; impact preview; typed, enum-bound parameter-contract designer reusing the module-14 tool-registry contract and SQL renderer, wired into the test gate and exposed standalone at `POST /v1/studio/parameter-contracts/validate` (`studio.py`, `studio_api.py`, `studio_test_harness.py`) | Git binding (Atlas authoritative); usage-derived eval suite is a separate change-set gate (ST-A8, delivered); systemic no-DB-test-harness gap shared with CT-1/TL-1/LN-4 |
| Agentic product portal | 21 | Implemented for current API scope | Role-oriented product at port 3000: asset-first shell and explorer; tabbed asset workspace; glossary term authoring/review; versioned aliases/README ownership and approved term linking; analyst workflow, catalog/impact, dbt transformations, business meaning, semantics, tools, graph, model routes, sources, quality, operations, governance and audit; ARIA roles/labels, roving-tabindex tab/command-palette keyboard navigation, focus management and restoration, live-region status/error announcements, `prefers-reduced-motion` support and a verified body-text contrast fix | Persona bound to OIDC groups; interactive screen-reader/axe-core WCAG AA accessibility certification; million-node visual certification |
| ui-next shell rebuild (strangle migration) | 21 | Partial | React 18 + TypeScript strict shell (UX-10); virtualized Catalog reference screen (UX-11); its one backend gap closed — `CatalogRowRead` read-model endpoint (UX-12, `GET /v1/organizations/{org}/catalog/rows`), CT-2 keyset shape, permission-filtered, no writes; `ui-next/src/lib/api.ts`'s `VITE_USE_FIXTURES` flag left at its default (fixtures on) because it also gates the not-yet-built UX-13 evidence endpoint — flipping it now would 404 that call, not just switch data sources; see `03-tracker.md` UX-12 | Set `VITE_USE_FIXTURES=0` once UX-13 lands too, or split the flag per-endpoint sooner; UX-13 evidence endpoint; migrate remaining screens (UX-15); generated API types (UX-14); retire legacy `ui/` (UX-16) |
| Production platform / network | — | **Bank decision** | Reproducible local Docker engineering topology | Kubernetes/managed services, regions, private endpoints, mTLS, egress, residency |
| DR and continuity | — | **Bank decision** | Durable local service volumes | Approved RPO/RTO, backup/restore, failover, regional recovery exercises |
| Performance / security / recovery certification | — | **Retest required** | Unit, strict type, migration drift, and local end-to-end suites pass | Load, soak, chaos, penetration, SAST/DAST, restore, connector certification |
### Reading the matrix

Three patterns are worth naming, because each is a consequence of a choice rather than an accident:

1. **The control plane is strong; the operational evidence is absent.** Everything in the
   "Implemented" column has local end-to-end evidence and nothing has bank-scale evidence. That
   gap is Phase D, and it is the honest answer to "is this production-grade?"
2. **One module is entirely pending** — studio (18). Glossary (08) implements the governed
   table-stewardship slice; context products and MCP (19) covers Context Products, marketplace
   access, the compiler, AI registry/trust and portfolio analytics locally.
3. **Several "Partial" rows are partial in the same way**: the safe slice exists, the breadth
   does not. Connectors, retrieval, lineage and policy all follow this shape, which is the
   deliberate consequence of building vertically rather than broadly.

## 5. Deliberate simplifications

Each of these is a decision, not an omission.

| Area | Simplification | Reason |
|---|---|---|
| Agent framework | Typed state machine and model-gateway contract; no LangGraph/ADK in the core | Keeps policy, evidence, and workflow history portable (ADR-0008) |
| Workflow | Temporal owns durable workflows; Kafka owns event distribution | Avoids using a broker or an agent graph as a workflow database (ADR-0007) |
| System of record | PostgreSQL authoritative; Neo4j/search/vector are projections | Enables reconciliation and deterministic rebuild (ADR-0003) |
| Query execution | One gateway for validation, authorization, cost, execution, masking, lineage | Removes bypass paths (ADR-0004) |
| Metadata processing | Discovery and profiling deterministic; LLM enrichment optional and reviewable | Prevents model output becoming unverified truth (ADR-0001) |
| Multi-agent behaviour | Specialized capabilities share one explicit state and permission envelope | Avoids autonomous agents with hidden permissions or unbounded loops |
| Service decomposition | Modular monolith with four deployment units and a planned extraction path | Distributed cost before boundaries are proven (ADR-0011) |
| Delivery | Production vertical slices, not a throwaway POC | Exercises controls and operability from the first release |
## 6. Decisions waiting on a person

These are not blocked on work. Each is blocked on a judgement that belongs to an owner, and each
is stated with a recommendation so that "no decision" is visibly a choice rather than a default.

| # | Decision | Recommendation | What it blocks |
|:--:|---|---|---|
| 1 | **Commit the working tree, or keep reviewing.** 24 files changed or new on `feature/snowflake-dbt-lineage-mcp`, uncommitted at the owner's instruction | Owner's call — no technical reason to wait | Nothing technically; everything is verified green |
| 2 | ~~Which embedding model~~ — **DECIDED 2026-08-30: OpenAI or Gemini**, selected by `embedding_provider` | Built: `src/aida/embedding_provider.py`, reusing the generation path's credential resolution rather than adding a second one. Resolution **fails closed** — with no provider configured the vector stage is skipped and the reason logged, never backfilled with the hash double, which had been feeding ranking a signal derived from a SHA-256 digest | Unblocks N5. Still open: nothing embeds the catalogue yet, and the recall@10 evaluation has not been run. ADR-0019 amendment |
| 3 | **When to switch authorization from measuring to enforcing** (`unresolved_workspace_posture` → `DENY`) | Leave in shadow; let divergence and unresolved-workspace counts accumulate on the real estate for a week; flip one workspace and read the evidence before the rest. Flipping today denies 17 known surfaces and an unknown number of real callers | Nothing — shadow mode is a safe steady state. ADR-0018 rollout status |
| 4 | **Accept INV-10, or leave it a principle.** "Generated knowledge is never silently authoritative" | Accept before the wiki work starts, not after — it constrains provenance and review state on every model-derived name, description and inferred edge | N10 knowledge compilation, N4 lineage review |
| 5 | ~~Remove Neo4j, or keep it~~ — **DECIDED 2026-08-30: keep it, and make it a per-organization setting** (PostgreSQL or Neo4j for the graph read path) | Build it as a port with three adapters, copying `vector_store.py` exactly. The switch itself is cheap — 3 modules read Neo4j and a gating boolean already exists. The work is the conformance suite proving both backends answer *identically*, and putting Neo4j in CI so the second backend is not untested. INV-1 confines the setting to lineage and exploration reads — never authorization, never the classification roll-up | Makes E5, the projection rebuild drill, a prerequisite rather than a deferred item. ADR-0020 amendment; tracker C7 |
| 6 | **Repo hygiene.** 19 tracked files and 7.9 MB under `scratch/`, including 9 tarballs; `.git` is 22 MB | Untrack rather than rewrite history — the branch is shared. Rescue the nine `proof-gaps-*` reports into `Docs/` first | Nothing. E13. Brief: `review-2026-08/decisions/01-repo-hygiene.md` |

## 7. Open enterprise gaps

A gap with a documented safe default is a managed risk; a gap without one is an incident waiting.

| Pri | Gap | Current safe default | Production closure evidence |
|:--:|---|---|---|
| **P0** | Enterprise identity and authorization | Signed OIDC/JWKS boundary implemented and required in production; local headers are development-only | Bank issuer/claim/group activation; centralized ABAC/RBAC tests; revocation/replay policy; workload identity; break-glass |
| **P0** | Secrets and source identity | One configured provider, strict references, registered adapter boundary, bounded cache with rotation invalidation; production rejects `env://` | Register/certify the bank adapter; workload identity; rotation/outage tests; read-only delegated identities; access review |
| **P0** | Network and connector placement | Single local network | Zone topology; egress allowlists; private endpoints; connector-agent mTLS; firewall evidence |
| **P0** | Data entitlements and masking | Catalog allowlist plus conservative column masking with alias and derived-expression propagation | Source-aligned row/column policy; purpose and consent rules; dynamic masking test suite |
| **P0** | Production platform | Single-node Docker | Kubernetes/managed topology; multi-AZ; capacity model; IaC; image provenance |
| **P0** | DR and continuity | Durable local volumes only | Approved RPO/RTO; backup/restore drills; regional failover; Temporal/Kafka/PostgreSQL recovery |
| **P0** | Model route and AI governance | Provider-neutral structured gateway; bounded metadata grounding; OpenAI and Gemini adapters; bounded retry/timeout/token contracts; durable control evaluations; pre-retrieval deterministic prompt-risk gate; versioned maker-checker routes with opaque credential references. Generation stays fail-closed until an approved route is selected and its credential resolves | Rotate development keys; approve provider/model/route selection; replace environment credentials with workload identity and private routing; certify retention/residency; pass multilingual, obfuscated, and **indirect** injection plus bank-domain evaluations; connect monitoring; **exercise the kill switch** |
| **P0** | Glossary and stewardship | Governed table-stewardship slice: categories and immutable term synonyms/definitions; reviewed publication/deprecation; manual/bulk/exact-inferred links; individual/group/rule ownership; retained conflicts; expiring table certification; scoped coverage snapshots and bounded unowned backlog; audit/outbox and responsive control-center UI | Dedicated leaver/vacate and inherited ownership; automatic expiry and escalation workers; category administration; fuzzy/model inference calibration; broader asset types; bank-scale and interactive accessibility certification |
| **P0** | Context products and MCP | JSON-RPC 2.0 MCP resources/tools/prompts; immutable maker-checker Context Products; role/quality/purpose gates; atomic Redis budgets; fuzzy bounded lineage and transformation tools; guarded marketplace access writes; deterministic compiler; product/contract marketplace; AI registry/trust; tenant-scoped portfolio analytics summary/trends; optional Neo4j unified projection; audit/outbox evidence and UI control plane | Workload identity; broader MCP stewardship writes; live scale/security certification; authoritative BI/procedure lineage; privacy operations; workflow templates; external provider certification; browser/accessibility QA; CP-9..CP-14 expansion in `20-modules/19-context-products-and-mcp.md` §15.2 |
| **P0** | Retrieval breadth | Lexical ranking with policy filtering before ranking; bounded evidence and selection reasons; **stale as of 2026-09-02 (Group A worktree, see the Hybrid retrieval row in §4) — full-text index, vector similarity, graph expansion, fusion ranking, and cross-source search are all implemented and live-wired** | Persisted/rebuildable pgvector index wired to a live path; large-catalog retrieval benchmarks |
| P1 | Connector fleet | PostgreSQL and SQL Server native pull plus canonical envelope `1.0`, atomic ingestion, resumable Temporal manifests/chunks; Oracle, BigQuery, Snowflake and Databricks adapters present but each unverified against a live source; Teradata, Db2 visibly `PLANNED` | Build Teradata, Db2 adapters; live-verify Oracle/BigQuery/Snowflake/Databricks; signed producers; Kafka/schema-registry intake; quotas/pause/cancel; version fixtures; maximum-scale recovery evidence; delegated source identities |
| P1 | Fleet scheduling | HA-safe polling, quotas, maintenance windows, backpressure, priorities, cancellation reconciliation, table-task concurrency | Prove fairness and capacity at bank scale; integrate enterprise maintenance calendars |
| P1 | Schema deletion and change handling | Tombstones, reactivation, drift counts, stable identity, impact APIs | Approve retention policy; add source-specific drift notification routing |
| P1 | Data-quality observability | Deterministic value-free baselines, source/table policies, immutable observations, deduplicated durable incidents, recovery reconciliation, scan age, audited operator transitions, Atlas workbench; source-row freshness fails closed as `NOT_CONFIGURED` | Approve connector watermark columns and classification/retention rules; alert/SLA routing; ownership escalation; custom rule packs; seasonality; incident-volume/load tests; induced anomaly and recovery certification; **runtime coupling** |
| P1 | Semantic governance | Versioned metrics plus governed metadata-only inference for domains, entities, descriptions, roles, grain, synonyms, questions; independent approval creates authoritative annotations and a cross-domain FK map; approved glossary terms can be linked to physical assets; **binding terms to semantic objects (SM-2) is implemented — stale here as of 2026-09-02, see the Semantic models and metrics row in §4** | Ambiguity and conflict workflows; confidence calibration; bank stewardship operating model |
| P1 | Relationship and lineage evidence | Source constraints, durable value-free query column lineage, tool dependencies, bounded candidates, durable review, confidence, **unified graph merging FK + suggested + dbt + OpenLineage edges, transitive bounded impact, and optional generation-stamped Neo4j projection/read fallback (EA.14, delivered 2026-08-29)**, server-side graph search, policy-bounded 1–4 hop exploration | View/procedure and certified ETL/OpenLineage adapters; cross-source and time-aware traversal; million-node projection/virtualization certification; authoritative column-level mapping (LN-10); graph export (LN-12) |
| P1 | dbt transformation intelligence | Immutable manifest ingestion, bounded inventory, deterministic catalog matching, dependency lineage, raw-artifact exclusion, SQL redaction and fingerprints, impact integration, agent retrieval, Atlas workbench | Authenticated CI artifact push; `run_results.json` health/SLA evidence; dbt Cloud/Core job adapters; column-level manifest lineage; snapshot retention; very-large-DAG virtualization |
| P1 | Operations and compliance | Structured logs, metrics, audit/outbox, fleet evidence, retry/backoff, dead-letter visibility, requeue control | OpenTelemetry export; SIEM/SOC integration; SLO alerts; WORM audit retention; compliance packs |
| P1 | Software supply chain | Pinned dependencies, non-root image | SBOM; signing; vulnerability policy; SAST/DAST; admission controls; patch SLAs |
| P1 | Studio | Change sets with conflict detection, test harness, diff view, impact preview, typed/enum-bound parameter-contract designer | Git binding |
| P2 | User experience | Atlas covers implemented workflows with accessible command palette, table virtualization, and responsive stewardship control center; **persona navigation is bound to the bank OIDC group contract (UX-1, delivered 2026-08-31)** — `GET /v1/me` derives persona server-side from the verified groups claim via the same configurable claim-path mechanism used for role mapping, and the manual persona switcher is genuinely absent from the rendered ui-next shell whenever `identity_provider=oidc`, surviving only under the development identity provider it is explicitly labelled for | Complete interactive WCAG/usability, very-large bulk-selection, and million-node visual certification |
| P2 | Chargeback and quotas | Per-source query limits | LOB budgets; tenant quotas; showback; anomalous-spend controls |
### Still-open gaps identified during the 2026-08 review

| Pri | Gap | Why it matters |
|:--:|---|---|
| P0 | **Aggregate exfiltration detection** | Per-query bounds do not stop a thousand compliant queries extracting what one non-compliant query would not (threat T20) |
| P0 | **Indirect prompt injection through retrieved metadata** | **Partly closed 2026-08-30** — `src/aida/ingest_screening.py` screens model-reachable text once at write and quarantines rather than deletes. What remains is the multilingual and obfuscated coverage, and the same treatment for document ingestion when N8 lands (threat T7 residual) |
| P1 | **MCP consumer threat surface** | Needs workload identity, per-read policy, budgets and consumption lineage (threat T18) |
| P1 | **Privileged-operator misuse** | Operators are audited but not monitored; no access review or separation-of-duties enforcement (threat T19) |
| P1 | **Legal hold** | No mechanism to suspend retention for a matter under investigation |
| P1 | **MCP write operations** (`MCP-2`) | An agent can request data-product access through the governance model but cannot approve or grant it; catalog edits, glossary proposals and classification changes remain |
| P1 | **Fuzzy entity resolution beyond lineage** (`MCP-3`) | Closed for lineage tools via `resolve_entity`; governed-SQL and catalog tools still require exact UUIDs |
| P2 | **Data contracts** | Foundation closed; external ODCS round-trip certification, entitlement fulfilment and bank-scale portfolio proof remain |

Gaps closed during the 2026-08 review are recorded in `06-accomplishment-log.md` rather than
carried here as closed rows — a status document listing what is already done stops being scannable.

## 8. Retest register

| Test area | Local result | Required next run |
|---|---|---|
| Static quality | Strict mypy clean (164 files), 4 import contracts kept, 1 Alembic head (`12aa5b4dd87d`) — reverified 2026-08-30 17:35 UTC. Ruff regressed to 14 errors (all auto-fixable) between the 17:20 and 17:35 checks, from concurrent work merging in; not yet run. Test count: **2,381 passing, 5 skipped, 1 xfailed** (2,387 collected), up from 1,199 at 12:25 and 1,391 at 17:20 — three different true counts inside one day, from other sessions pushing to this branch throughout | CI on every change — **wired** (`.github/workflows/ci.yml`, ST-02). Not yet observed running on a remote |
| Unit / contract suite | 2,381 passing + 5 skipped + 1 xfailed (2,387 collected) as of 17:35 UTC — SQL Server and canonical/chunk contract validation, stable checksums, sequence/duplicate rejection, scope counting, quality, prompt-risk, graph, business inference, model, dbt, OIDC, secret, lineage, tool-first, evaluation controls, adversarial SQL corpus, catalog certification, doc-claim regression gate (`test_doc_claims.py`, new) | Run `ruff check --fix` to clear the 14 new errors; add database concurrency/race tests, forced mid-batch restart, incident concurrency, JWKS outage, indirect prompt attacks, bank-domain benchmarks |
| Database migrations | Single head, applied locally. The ADR-0018 chain (`f1a2b3c4d5e6` → `a7c3e91d4f28` → `b4e2f70a9c15` → `c9d1a83e6b47`) and the value-freedom fix (`d5f8b21c4a03`) were **rehearsed on PostgreSQL 16 with populated data** — 14/14 backfill assertions, and a real sentinel seeded pre-migration confirmed absent from the database afterwards, with a clean downgrade/re-upgrade round trip | Rehearsal at the bank's catalogue scale; PITR restore |
| End-to-end banking fixture | R20/R21/R22 real API/Temporal/PostgreSQL runs proved manifest/chunk replay, conflicting-content denial, cross-chunk FK resolution, exact scope counts, physical payload cleanup, live SQL Server discovery/profiling/SHOWPLAN/masking | FULL retirement/recovery and concurrent forced-restart fixtures; Kafka intake; maximum-scale load; remaining connectors; approved-provider certification; interactive visual/accessibility certification |
| Security | Fail-closed controls and cross-tenant denial pass locally | Threat-led penetration testing; ABAC/row-policy certification |
| Recovery | Component retries exercised | Full PostgreSQL/Temporal/Kafka/Neo4j restore and regional failover drill |
| Performance | **Not measured** | Load, soak, spike, projection rebuild timing, PITR restore |
## 9. Decisions the bank will eventually supply

These change adapters and deployment policy, **not the core architecture**: approved cloud/on-prem regions; identity provider and claims; policy engine; vault; source priority list; residency classes; retention; RPO/RTO; LOB isolation tiers; model providers and routes; SIEM; ITSM; Kubernetes and managed-service standards.

Until supplied, production mode remains fail closed for identity, model generation, and development overrides.
## 10. Where everything else lives

Status used to be tracked in twelve places, four of which disagreed. It is now tracked here.
Everything below is a *different question* rather than a second copy of this one.

| Question | Document |
|---|---|
| Where are we? | **This file** |
| What is the state of one specific work item? | `60-delivery/03-tracker.md` — the item-level open-work list, one row per ID |
| What did we build, when, and what did it cost us to learn? | `60-delivery/06-accomplishment-log.md` — append-only; corrections are new entries, never edits |
| What is planned, in what order, and why that order? | `60-delivery/01-roadmap.md` (phases) · `60-delivery/02-epic-backlog.md` (epics with acceptance criteria) |
| What still has to happen per connector? | `60-delivery/07-connector-implementation-backlog.md` |
| Why is it built this way? | `10-architecture/adr/` — 20 records; `10-architecture/adr/README.md` is the register |
| What must always be true? | `10-architecture/01-principles-and-invariants.md` |
| What does the API/event/envelope contract say? | `30-contracts/` |
| What does each module own? | `20-modules/` |
| What did the 2026-08 review find, and what did it propose? | `review-2026-08/` — `00-README.md` is the index |
| What decision is waiting on me? | `review-2026-08/decisions/` — one brief per open decision |
| What did a competitor do, and how do we know? | `review-2026-08/research/` — all vendor research, primary-source and dated |

### Rules that keep it consolidated

1. **One status claim, one home.** A document that needs to state status links here instead of
   restating it. A dated `Implementation status` callout in a design document is the exception,
   and it must name the file that proves it.
2. **The accomplishment log is history, not status.** It is append-only. Something being in it is
   not evidence that it is still true.
3. **Completed work-item write-ups keep their design rationale and lose their status section.**
   The rationale is why the code looks the way it does and is worth keeping; the status was true
   on the day it was written and is now this file's job.
4. **A superseded document moves to `Docs/_superseded/` with a header saying what replaced it.**
   It is never edited into agreement and never silently deleted.

## Related documents

- Tracker: `60-delivery/03-tracker.md`
- Accomplishment log: `60-delivery/06-accomplishment-log.md`
- Threat model: `50-security/02-threat-model.md`
- Compliance and evidence: `50-security/04-compliance-and-evidence.md`
- ADR register: `10-architecture/adr/README.md`
