# Atlas application review — build, remove, simplify

> **Reconciled 2026-09-11 against `06b0b56`.** Use the [current execution queue](../60-delivery/03-tracker.md#p-current-execution-queue-reconciled-2026-09-11) for status and the [reconciliation](../60-delivery/23-review-reconciliation-2026-09-11.md) for earlier-review dispositions. Measurements below retain the original baseline. X7 removal is cancelled: current REST/MCP callers exist. Agent safety partials and deployment/accessibility gaps are explicitly carried forward. This review is evidence and rationale, not a separate work queue.

Review date: 11 September 2026. Code measured between `d716694` and `ab7ac42` on `feature/agent-os-v2` (other sessions committed during the review; line numbers can drift by a few lines).
Scope: `src/aida`, `src/atlas`, `sdk/`, `scripts/`, `ui-next/`, `tests/`, `migrations/`, `Docs/`, `compose*.yaml`, `infra/`, `.github/workflows/ci.yml`.
Method: seven independent reviews (backend dead code, UI, agents/AI, governance and infrastructure, core metadata, tests/docs/hygiene, product journey), each finding tied to `file:line` or a measurement. The findings that lead each section were re-checked in source for this document.
Predecessor: [review-2026-09-05](../review-2026-09-05/REVIEW.md). Its status is in §7.

**Confidence labels.** *Confirmed* — verified in source or by a command. *Likely* — strong signal, one check missing. *Candidate* — needs a product decision or usage data. Nothing here has been deleted or changed; this document is a plan.

---

## 1. The short version

Atlas has a genuinely strong core: one SQL execution choke point, models that only propose, maker-checker review through one decision service, a value-free control plane, fail-closed model activation, and honest readiness reporting. Every finding from the 5 September review now has code behind it.

The problem is proportion. In two weeks the repository reached ~415K lines — 141K of backend product code, 117K of tests, 60K of UI, 52K of docs — with **44 screens, 471 endpoints, ~200 tables, 243 settings and 19 compose services**. Against that:

- **The core journey still breaks at the payoff.** On a fresh install, Ask refuses any question that no parameter-free governed tool covers (model generation is off by default and the UI never sends tool parameters). Approved data-product access stays `PENDING` forever. There is no UI to re-scan a source or configure freshness.
- **Almost nothing outward-facing has met a real counterpart.** No live model has ever been called. Of 33 implemented capabilities in the register, 16 are unverified and 12 are verified only locally; the 5 fully verified rows are plumbing.
- **New surface keeps outrunning use.** 43% of the last five days' commits were agent work: three task agents, a scheduler, a roster, budgets — none of which is established here as verified against a real estate. **Reconciliation:** Ask as a registered agent now has REST and MCP callers; X7 removal is cancelled.
- **One central invariant has a hole.** Native policy sync opens its own driver connection to a source and runs DDL outside the query gateway (§2, D1).

**Recommendation:** freeze new surface until the journey works end to end, first without a model and then with one. Spend that time on the defects in §2, the build list in §3, and removing or parking roughly 25–35K lines, ~20 tables and 5–7 compose services that the default product does not use (§4, §5).

### By the numbers

| Measure | Value | Note |
|---|---:|---|
| Backend product code | 141K lines | `src/aida` 130K (284 flat modules) + `src/atlas` 11K |
| Backend tests | 117K lines | 291 files |
| UI (non-test) / UI tests | 56K / 15K lines | includes a 7.7K-line demo fixture file and 5.3K generated types |
| Docs | 52K lines | 209 Markdown files |
| Migrations | 16K lines | 150 revisions, 46 of them merges |
| Routes | 471 | 243 used by the UI, 62 by scripts/ops/SDK, 65 only by tests, **101 by nothing** |
| Screens | 44 | 40 at the last review, which asked for consolidation |
| ORM tables | ~200 | 135 in `models.py` |
| Settings | 243 | 76 documented in `.env.example`; 12 default-off flags nothing turns on |
| Compose services | 19 | 5–7 not needed by the default product |
| Event types | 118 produced | 8 consumed, by 2 consumers |
| Agent / AI subsystem | ~35K lines, 43 tables, ≥125 endpoints | four evaluation mechanisms, none measuring answer correctness |

---

## 2. Defects found in this review — fix first

These findings describe the original review baseline. During reconciliation, concurrent working-tree fixes appeared for D2/D3/D5/D6/D8; their current status is PARTIAL with remaining verification in tracker section P. Do not repeat those implementations. The other source findings have not all been reverified against ongoing edits.

| ID | Defect | Evidence | Severity | Fix |
|---|---|---|---|---|
| D1 | **Native policy sync runs DDL on a source outside the query gateway** — a second execution path, contrary to INV-2 / ADR-0004 | `policy_native_sync.py:767` (`asyncpg.connect`), `:842` (`pytds.connect`), statements executed at `:780-782`; credential resolved at `policy_native_sync_api.py:516`. It is the only module outside `connectors/` that opens a source connection. None of INV-2's three guards catches it: the import-linter contract protects `execution_access`, and the tier-0 test scans only for `execute_read_query`/`estimate_read_query`. No UI calls it; the sample SQL Server login is `db_datareader` only (`compose.yaml:227`), so apply either fails or needs an over-privileged credential. | High | Remove `apply` and keep `preview` (hand DDL to a DBA), or route it through an explicit, ADR-approved admin execution capability. Extend the tier-0 test to flag any driver `connect` outside `connectors/`. |
| D2 | **Raw audit HMAC key bypasses the signing provider** | `hmac` keyed with `settings.audit_hmac_key` at `agent_orchestrator.py:751`, `:1174`, `intelligence_api.py:553`, `tool_api.py:1042`. `signing.py:1-20` says the raw key never leaves the KMS; production validation still requires it (`config.py:1059`). | High | Route all four through `SigningProvider`; drop the production requirement for the raw key once nothing reads it. |
| D3 | **Compliance packs claim WORM archiving but only insert a row** | `compliance_packs.py:534-547` — docstring "WORM-archive a generated compliance pack", body is `session.add(record)`. The same class of claim F01 fixed for audit events. | Medium | Write through the archive provider with a version id and read-back check, or change the claim and the label. |
| D4 | **Withdrawing a table description can leave it looking described** *(Likely — write the test first)* | `atlas/modules/catalog/service.py:162-175` falls back from the documentation version to the annotation's `business_description`; withdrawal retires only the documentation version (`description_withdrawal.py:93-125`). The model docstring promises the asset reads as undescribed (`models.py:1829-1830`). | Medium | Do not fall back past a withdrawn readme; long term, one description table (S5). |
| D5 | **Catalog glossary chip links to a path that doesn't exist** | `ui-next/src/components/CatalogTable.tsx:72,76` set `window.location.href = "/business-meaning?..."`; the app routes by hash, so the click lands on Overview. `meaning` declares no `term` field (`routes.ts:142`). | Medium | Use `buildLink`; add `term` to the route's query schema. |
| D6 | **Ownership-expiry banner decides "mine" with the development principal** | `OwnershipExpiryBannerScreen.tsx:35-36,62` uses `VITE_DEV_PRINCIPAL_ID \|\| "local-ui-admin"`; wrong under OIDC. `MeRead.principal_id` is available. | Medium | Use the session principal. |
| D7 | **Eleven screens reintroduce the 500-item picker cap (F15)** | `fetchOrgDatasources` hard-caps `limit=500` (`ui-next/src/lib/api/identity.ts:94-104`) and 11 screens fetch their own list with it. | Medium at scale | One `useDatasource()` hook on the scope layer (S10). |
| D8 | **Graph reconciliation connects to Neo4j whatever the backend** | `graph_reconciliation.py:499-520` opens a driver for every due datasource with no `graph_store_backend` check; a Postgres-only deployment logs a failure per datasource every interval. | Low | Reconcile only organizations whose backend is Neo4j (or delete with S1). |
| D9 | **Operators cannot run the ENFORCE readiness check they are told to run** | `workspace_access.enforcement_readiness` (`workspace_access.py:269`) is called only by tests, while `config.py:684` tells operators to run it before SHADOW→ENFORCE. | Low–Medium | Expose it on the operations screen or readiness; part of B3. |
| D10 | **The SLO budget screen reads a table nothing writes** | `SloMeasurement` has no writer; read at `observability_audit/router.py:144`; the UI panel (`operations.ts:320-331`) is always empty. | Low | Build the writer or remove the panel and table. |
| D11 | **Housekeeping written but never scheduled** | `token_revocation.prune_expired_revocations` (`token_revocation.py:54`) — the revocation table grows without bound. `business_graph.rebuild_*` (`:487`, `:500`) are unreferenced, so `rollup()` always takes the slow fallback its docstring warns about. | Low | Add both to the scheduler (or S8's job table). |
| D12 | **Stale "not built yet" text on live paths** | `mcp_server.py:1351` tells MCP clients AT-11 is TODO; `sql_lineage_parser.py:696` says N3 has not shipped (it has); `asset_context.py:21`; `identity_tenancy/router.py:773`. | Low | Correct the text. |
| D13 | **Three CI gates are almost always red, so they gate nothing** | Last 20 non-cancelled runs: gitleaks failed 19, docs/shim-register 16, npm audit 15. | Medium (process) | Fix each or delete it; a gate that is always red trains people to ignore red. |

---

## 3. What to build — finish and verify before adding

Ordered. P0 items make the core journey pay off; P1 items close operational loops; P2 can wait for a customer.

### P0 — the journey has to work, first without a model and then with one

| ID | Build | Why (evidence) | Done when | Effort |
|---|---|---|---|---|
| B1 | **Ask works with no model.** A parameter form fed by `plan_evidence.required_parameters`; governed tools for the sample estate generated with the existing view/multi-table blueprint generators; a "Propose as tool" action on an Ask result that goes through review. | `AskScreen.tsx:487` sends only the question; the planner returns CLARIFICATION (409) for any tool with parameters (`agent_intelligence.py:254-264`); otherwise the model path refuses with `MODEL_ROUTE_NOT_CONFIGURED` (`agent_orchestrator.py:1469-1473`) because `model_generation_enabled=False` (`config.py:741`). "Promote to tool" (analyst job A4) has no UI. | With generation off, the seeded estate answers a fixed set of questions with `generation_source=GOVERNED_TOOL`; proposing, approving and asking again hits the new tool. | M |
| B2 | **The first live governed answer, measured.** Approve the pending route, run a fixed ~50-question corpus on the sample estate, publish execution-match accuracy and refusal correctness. Make this corpus the one evaluation gate (see S2). | No real model has ever been called; the `openai-bank-sql` route has been pending since 2026-09-11. The register marks answer quality "unmeasured"; the quality benchmark is "framework-only". | Accuracy and refusal tables published and dated in the register; the corpus runs in CI with a recorded model. | M |
| B3 | **Enforce workspace authorization in one workspace.** | Defaults are SHADOW / OBSERVING (`config.py:660`, `:689`); the production validator does not require ENFORCING; 17 surfaces fail under DENY (`Docs/60-delivery/00-status.md:117`). A bank will not accept a control that only observes. | Suite green under DENY; one workspace in ENFORCE; readiness reports ENFORCING; an OIDC viewer is denied a cross-workspace read. | M–L |
| B4 | **Data-product access that actually provisions.** | The default fulfilment provider `outbox` always returns `PENDING` (`entitlements.py:28-29`, `config.py:711`); the event it emits has no consumer; the webhook provider posts inline and bypasses the delivery ledger (`entitlements.py:54`). | Request → approve → PROVISIONED → consumer query succeeds → revoke → query denied, all audited. | M |
| B5 | **Verify the first customer's connector live, behind a real conformance suite.** | "Certification" scores six presence checks, none about discovery correctness (`ingestion.py:261-309`); only PostgreSQL runs live in CI; Oracle, BigQuery, Snowflake and Databricks have never met a live instance. | One contract suite parametrized over the connector registry (golden catalog results per dialect: counts, types, keys, views, routines, profile counts, row cap/timeout, streaming batches); the customer's connector passes against a live TLS instance with a least-privilege account. | M |
| B6 | **Corporate IdP and secret manager in a deployed topology.** | Browser OIDC is verified only against the mock issuer and a reload drops the session (F06); `env://` secrets are refused in production and no secret manager is configured. | SSO sign-in; session survives reload; group → role mapping; secrets via the Vault provider; credential rotation without an outage. | M |

### P1 — close the operational loop

| ID | Build | Why (evidence) | Effort |
|---|---|---|---|
| B7 | **Re-scan, schedule and retry from the UI.** | The only UI caller that starts a scan is `FirstSourceSetup.tsx:490`; `PUT /datasources/{id}/scan-policy` (`connectivity/router.py:394`) has no UI caller; README:123 still advertises schedules that left with the old UI. | S–M |
| B8 | **Freshness contracts that raise incidents.** | Watermark approval has no UI caller, so freshness is permanently `NOT_CONFIGURED`; `evaluate_freshness` runs only on request (`quality_api.py:750`) and nothing creates a freshness incident. Run it as a scheduled rule into the existing incident sink. | S |
| B9 | **Audit export and honest archiving.** | The audit ledger screen has no export; compliance packs are not archived (D3); the archive backend defaults to `none` (`config.py:880`). | M |
| B10 | **Notification delivery on, scheduler failures visible.** | Delivery is off by default (`config.py:933`); owner routing failed silently 10,584 times until `031d153`. A failing scheduler pass should show on Operations within one interval. | S–M |
| B11 | **A browser journey test in CI.** | Connect → scan → describe → review → ask → evidence through production nginx with least-privilege OIDC accounts, failing on each break in §1. | M |
| B12 | **Decide the half-built producers.** Each has a live review/apply half and a producing half that runs only in tests: query-history mining (`query_history_miner.py:471`), classification propagation (`classification_propagation.py:321`, `:382`, `:547`), principal-leaver events (`identity_events.py:63`, `:109`), exemplar promotion (`exemplar_store.py:368-460`). Wire each producer to a trigger, or delete both halves. | S each |
| B13 | **Lineage that reaches reports.** | Unified lineage omits BI and consumption edges (`unified_lineage_builder.py:70-77`) — yet "which reports does this column feed" is the core banking impact question. Falls out of the lineage consolidation in S5. | M |
| B14 | **A generated capability register.** | 32 of 36 rows are dated 2026-09-06 and at least 5 contradict the code (bulk decision, OIDC, MCP, single decision, Ask rows). Generate what can be generated; gate staleness. | S |

### P2 — when a customer is in view

- **B15 Capacity and restore evidence** at the first customer's estate size: p95 for catalog, search and review APIs, and a timed restore (register DR row is "No" in every column).
- **B16 Incremental discovery for non-Postgres connectors** — only PostgreSQL implements `discover_streaming` (`connectors/base.py:340-360`); do it for the customer's connector, not all five.

### Don't build yet

1. **More agents, autonomy or scheduled agent runs.** The three task agents have never run on a real estate; every acceptance rate is `None`. Keep unattended reviewer-agent approvals off: AR-03's adversarial evaluation now exists and the recorded result still approves seven false twins (R11-C3). Closing this requires independent truth evidence, not merely running a corpus.
2. **Ask as a registered agent and per-agent token budgets** — unless MCP agents need per-agent contracts soon, in which case wire it end to end before extending it (X7).
3. **Teradata/Db2, or certifying all four unverified connectors at once.** Certify the one the customer runs (B5).
4. **Ontology expansion, knowledge compilation (N10), federation/DuckDB (N13).** No persona job asks for them.
5. **GCS/Azure archive providers, SOC2/ISO packs, Iceberg export, Slack marketplace, further Excel add-in work.** Wait for a customer to name the destination.
6. **A 1M-object benchmark corpus as a gate.** Measure at the customer's scale instead (B15).

---

## 4. What to remove

"Remove" means verified unused or unreachable in the product as shipped. Items that need a migration say so. No production deployment exists, so unused endpoints have no external callers to break — but check before deleting anything a partner could have integrated with.

| ID | Remove | Evidence | Size | Risk |
|---|---|---|---|---|
| X1 | **Demo fixtures in the production bundle.** Step 1: make demo mode a build-time constant and import fixtures dynamically inside `demoOr`. Step 2: retire demo mode once tests use the seeded estate. | Demo mode is chosen at runtime (`ui-next/src/lib/appConfig.ts:121`), so the 7,711-line `fixtures.ts` ships to every user: 342 KB (97 KB gzip), **32% of all JS**, preloaded by both the app and the Excel add-in. `demoOr` (`api/transport.ts:98`) has 163 call sites in 15 files. | −97 KB gzip now; ~8K lines later | `npm run dev` defaults to fixtures; 3 tests rely on it |
| X2 | **Tables nothing reads or writes** — `search_index`, `vector_embedding`, `abac_policy`, `abac_decision` (`models.py:3459`, `:3481`, `:3513`, `:3541`); `slo_measurement` unless D10 builds a writer; possibly `isolation_boundary` and `business_assignment_rule` (`identity_tenancy/models.py:202`, `:512`, re-exported by the shim only). | Zero references outside their definitions and an ORM-drift migration. | 4–7 tables, ~155 lines | Drop migration; confirm tables are empty |
| X3 | **Code referenced nowhere** — 62 top-level symbols, 528 lines, e.g. `stewardship_worklist.compute_worklist` (113 lines, `:288`), `negative_knowledge.auto_lift_on_material_change` (`:250`), `agent_runtime.ModelGateway`/`DisabledModelGateway` (`:56`, `:67`), `reaper_service.py:673`, ~35 constants. **And test-only helpers in `src`** — the thin `run_steward_agent`/`run_lineage_agent`/`run_quality_agent` wrappers, `studio.py:117-150` in-memory ops and others (~350 lines): move to `tests/support` or point tests at the real API. | AST walk plus repo-wide word grep; top 48 hand-checked. | ~880 lines | Low |
| X4 | **23 empty `src/atlas` scaffolds** (events/repository/service/workers ×5, the unmounted `profiling/router.py`) and 10 modules reached only by tests under `src/atlas/modules/*/tests`, which pytest never collects (`testpaths=["tests"]`). Extend the reachability gate to `src/atlas` (`tests/test_reachability_gate.py:48` scans `src/aida` only). | Import graph. | 33 files, 268 lines | Low (`scripts/generate_module.py` regenerates scaffolds) |
| X5 | **Endpoints nobody calls** — 101 of 471 routes (~4.9K handler lines) have no client; 65 more (~3.5K) are called only by tests. Start with whole routers the UI never touches: `graph_perspectives_api` (5), `policy_native_sync_api` (4, plus the 921-line engine — see D1), `retrieval_ops_api` (4), `composite_key_api` (3), `search_api` (2) with `semantic_api`'s test-only global search, and the test-only routers `table_family_api`, `view_lineage_api`, `access_review_api`. For each cluster: delete, or build the UI if it belongs to the journey. | Route table cross-referenced with `ui-next/src`, `sdk/`, `scripts/`, `tests/` (conservative: a GET caller marks a same-path POST as called). **Keep** the machine-to-machine ones: OpenLineage ingest, `/mcp`, health and metrics, ingestion chunk upload, classification feed, detokenize/revoke, capability matrix. | Up to ~8.4K handler lines plus backing modules | Medium; decide per cluster |
| X6 | **Settings nothing reads** — `profiling_exception_default_retention_days` (`config.py:232`), `cross_source_candidate_max_datasource_pairs` (`:242`), `usage_boost_enabled_default` (`:249`), `vector_index_credential_reference` (`:618`), plus one at `:551`. | Zero references, tests included; no deploy file sets them. | 5 fields | Low |
| X7 | **CANCELLED 2026-09-11: retain the registered-agent branch and token budgets.** | No caller passes `agent_asset_version_id` — the only occurrence is the orchestrator forwarding its own argument (`agent_orchestrator.py:719`). Dead: `:792-826`, `:1770-1795`, the `AgentRun.ai_asset_version_id` fill-in, and the `agent_budget_window` table. Four commits went into budgets, the latest (`94babdf`) today. Keep the wall-clock cap task agents use (`task_agent.py:68`). | No removal | Active security controls |
| X8 | **Duplicates** — the flat procedure parser exposed as a second `/procedure-lineage/parse` route (`view_lineage_api.py:273-289`; `sql_lineage_parser.py:693-697` calls itself "the flat parser under another name"); two UI clients for one relationship-decision endpoint (`api/crossSource.ts:565`, `api/governance.ts:185`); `_optional_text` ×5, `_EnvelopeRows.reason` ×3, `_response_field` ×2; three copies of graph BFS (`knowledge_graph.py:25-123`, `unified_lineage.py:52-145`, `graph_retrieval.py:116`); UI dead code (`Swimlanes` in `NarratedLineageScreen.tsx:108-148`, `OrgPicker.tsx`, `revokeAssetCertification`, `getGlossaryTerm`, `fetchContextProductScope`, `fieldErrorMap`). | Each confirmed by reference search. | ~500 lines | Low |
| X9 | **Infrastructure the default product doesn't use.** Step 1: compose profiles so the default stack is postgres, temporal, migrate, api, ui-next, metadata-worker, fleet-scheduler and the sample sources. Step 2: delete Redis (move the MCP budget counter to a Postgres row). Step 3: once Neo4j is parked, let the description drafter poll the outbox table and drop Kafka. | **Redis**: backs `lineage_cache.py` (51 lines, off by default, `config.py:284`) and `mcp_budget.py:91` (off by default) — yet the API waits on it (`compose.yaml:248`). **Neo4j + graph-projector**: default backend is Postgres (`config.py:460`) and the Neo4j adapter is marked uncertified (`graph_store.py:22`). **Redpanda + console + outbox-publisher**: 118 event types produced, 8 consumed by 2 consumers (`graph_projector.py:78-83`, `newly_created_table_drafter.py:729-731`). **MinIO**: archive backend is `none` unless configured (`config.py:880`); image pinned to `:latest` (`compose.yaml:156`). **temporal-ui**: make opt-in. | 5–7 services; ~2.3K lines (Neo4j path + Redis) out of the default path; the `aiokafka` and `redis` dependencies | Low for profiles; medium for the drafter rewrite |
| X10 | **Deployment sketches presented as deployment.** `infra/k8s/base` deploys only the API and a migration job, with placeholder digests (`deployment.yaml:51`) and a stale README; `infra/airflow` is one smoke DAG run by hand. | File inspection. | 10+ files | Low — label as sketch/example, or remove |

*§6 adds the repository, test and documentation cuts.*

---

## 5. Where we over-engineered — simplify

Each item states what it costs today, what it buys today, and the simpler shape. Security controls that protect a real invariant are listed in §8 as things to keep, however thorough.

### S1. Five infrastructure services for jobs Postgres already does
- **Today:** Redis, Neo4j, Redpanda (+ console), MinIO and a graph projector start on every developer's machine and in every environment modelled on compose.
- **Buys today:** with default settings, nothing the product needs: Postgres is the certified graph backend, agent budgets already use an atomic Postgres window, the outbox is a Postgres table, and archives go nowhere by default.
- **Simpler:** X9. Later, and only after S8: Temporal runs two workflows and six activities; a Postgres job table with leases could replace it (three lease/claim implementations already exist). *Candidate — do last.*

### S2. Agent governance built around agents that have not run
- **Today:** ~35K lines, 43 tables, ≥125 endpoints and ~9.2K lines of UI screens. One Ask passes about fifteen controls; a task-agent proposal about ten. **Four evaluation mechanisms** — `agent_evals` (441), `agent_eval_gate` (548), `studio_eval` + harness (645, 3 tables), `tool_certification` (159, 2 tables) — plus `quality_benchmark` (939) and `tests/context_path_eval` (771); **none measures whether an answer is correct**. The eval gate requires at least one promoted Ask exemplar (`agent_eval_gate.py:146`), so no organization can approve even a deterministic steward agent before Ask has a confirmed answer. Roster, inbox and four consoles all report agent activity. The contract has two write paths (direct PUT and contract request). Decision lineage writes one row per *rejected* candidate after widening retrieval to 5,000 (`agent_intelligence.py:153`, `agent_orchestrator.py:198-213`), read only by one endpoint.
- **Buys today:** real invariants — models propose, deterministic services authorize, maker-checker, attributable evidence, kill switch — plus a large amount of recorded-but-unread state.
- **Simpler:** an agent is a principal plus a contract (tier, capabilities, limits, kill switch) approved through governance review; agents write only through the review queue and read only through the query gateway; evidence is `agent_run` / `agent_task` / audit with one summarized decision record (selected hits plus a rejected count); **one question corpus (B2) measures answer quality; separate reviewer-adversarial, injection, authorization, contract and concurrency gates remain**. Merge the roster into the inbox; make the direct contract PUT open a request; rename the DQ triage "agent", which is a pure function. *~2–3K lines, ~6 tables.*

### S3. Retrieval: seven modules for one path, two of them inert
- **Today:** `retrieval` 959 + `retrieval_stages` 1,068 + `fusion_ranking` 310 + `graph_retrieval` 203 + metrics ≈ 2,650 lines. The lexical channel is Python BM25 over name-filtered scans of up to 5,000 rows per type (`retrieval.py:277-302`), while Postgres full-text search (`full_text_index.py`) serves only `search_api`. The vector channel is skipped by default (`embedding_provider="unset"`, `config.py:638`) yet carries four backends and ~1.5K lines. A second, dead embedding table exists (X2).
- **Simpler:** Postgres FTS over tables, columns, tools and glossary; one hop over foreign keys; a tool boost; keep the brute-force vector store behind its flag and drop `external`/`pgvector` until a bank asks. *~1.0–1.5K lines.*

### S4. Too many kinds of "reusable governed artifact"
- **Today:** governed tools (3 tables, plus 3 blueprint generators, 1,146 lines); tool plans (3 tables, ~1,240 lines and a 657-line screen — Ask never uses them, and the step executor hard-codes `"tokens_used": 0` at `tool_plans_api.py:606`); context products (6 tables, ~2,200 lines); **Studio change sets** (7 tables, ~2,570 lines — a second authoring path for metrics, tools, terms and context products, all of which already have review-gated APIs); playbooks (bulk catalog actions run on every scheduler tick); and three hint stores (query memory, exemplars, negative knowledge).
- **Simpler:** two artifacts — governed tool and context product. Freeze Studio and fold it away once direct authoring covers its cases; park tool plans until Ask needs multi-step plans; one hint store. *~2.5–4K lines, ~10 tables.*

### S5. The same fact stored in many tables
| Family | Today | Target | Saves |
|---|---|---|---|
| Lineage edges | **8 edge tables** (view, procedure, deep procedure, dbt, OpenLineage table and column, BI report→metric, BI metric→column). `view_lineage_edge` and `procedure_lineage_edge` have the same 18 columns and natural key (`models.py:3600-3723`); `deep_procedure_lineage_edge` exists only to avoid editing `models.py` and calls itself "strictly more capable" (`procedure_lineage_models.py:14-28`). "Unified lineage" is a Python union of 7 tables (`unified_lineage_builder.py:45-63`). | One `lineage_edge` keyed by `source_kind`, keeping the provenance tables; unified lineage becomes one query and gains BI (B13). | −7 tables, ~1.0–1.5K lines |
| Consumption | 3 stores with the same shape: `context_product_consumption_edge`, `mcp_consumption_evidence`, `consumption_record` (`models.py:2948`, `:3029`, `:3884`). | One `consumption_event`. | −2 tables |
| Descriptions | A table's governed description lives in two version tables (`models.py:1712`, `:1565`), resolved by a four-step fallback (and D4). Asset and column pipelines are near-duplicates (reject 0.97, draft-read 0.88, edit 0.82 token similarity). | `description` + append-only `description_version` for TABLE and COLUMN; drafts become PENDING versions; keep withdrawal and model-import tables. | 6 tables → 2, ~0.8–1.2K lines |
| Business concept ↔ asset | Four models: `business_domain`/`business_entity`, the `business_node` taxonomy (ADR-0018), ontology concepts stored as JSON (new on 2026-09-10, in flat `aida` against the refactor plan), glossary term + `asset_term_link`. | Glossary term is the concept; one polymorphic term-link table; a `term_relation` table. *Candidate — product decision.* | −4 tables, +1 |
| Candidate reviews | Six candidate tables repeat the same lifecycle columns (relationship, group, table family, rename, composite key, cross-source). | One mixin and one decision adapter. | Code, not tables |
| Quality thresholds | Relative thresholds in `data_quality_policy`, absolute ones in `quality_rule`. | One rule table and evaluator. | −1 table |

Migrations must keep row UUIDs (AgentRun grounding digests and review `version_id` point at them), keep descriptions append-only with WITHDRAWN distinct from SUPERSEDED, and keep Postgres authoritative.

### S6. The bounded-context relocation costs more than it returns
- **Today:** after two weeks, `src/atlas` holds 7.8% of backend lines, 26% of tables and 14% of routes. It has gained **no tables since 6 September** while flat `aida` grew by 25 modules and ~10.5K lines. The 14 shims carry 810 shim-to-caller relationships; 10 of the 12 atlas service and repository files are seven-line stubs; the refactor plan still describes `models.py` as 1,274 lines (it is 5,503). The code is already doing package-by-feature informally — five "side files" hold tables to avoid `models.py` merge collisions.
- **Simpler:** consolidate first (S5), then move each consolidated family into `aida/<feature>/` (models, schemas, service, router) **in one commit that updates its callers — no shim** — with one import-linter contract per package. Order: lineage, descriptions/stewardship, semantics/glossary, quality, relationships. Drop the plan's schema-per-module and cross-schema foreign-key steps; they buy nothing in a single-database monolith. See [refactor plan](../40-engineering/06-refactor-plan.md) and [shim register](../40-engineering/09-compatibility-shim-register.md).

### S7. Compliance machinery built ahead of any destination
- **SIEM transports** — 1,380 lines + 1,419 test lines, webhook plus syslog UDP and TCP; the endpoint is `NOT_CONFIGURED` and the delivery worker off by default (`config.py:856-858`, `:933`). Keep the `delivery_intents` ledger; ship the webhook transport only.
- **Archive providers** — 2,470 lines and 3 tables, including a hand-rolled S3 client and SigV4 signer (890 lines) written to avoid a dependency; `gcs` and `azure_blob` are accepted values that only refuse. Keep the state machine and envelope; implement a provider with a maintained client when a bank names its store.
- **Access review** (533 lines, own table, no UI) duplicates the access-review section of compliance packs (`compliance_packs.py:255`, `:480`). Merge.
- **Cost showback** (a proxy metric by its own admission, no UI), **edition entitlements** (one process-wide edition defaulting to REGULATED, so everything is always allowed — `config.py:709`), **LOB concurrency** (in-process only, so the real bound is replicas × limit — `lob_concurrency.py:25-43`). Park behind one flag or remove.
- *~2.5K lines.*

### S8. Fifteen sweepers, five scheduling mechanisms
- **Today:** 15 passes per scheduler tick (`workflows/scheduler.py:614-671`), an archive loop in every API replica (`main.py:356`), and the drafter inside the Temporal worker. Eight passes keep "last run" in process memory, so a restart re-runs every daily pass and a second scheduler double-runs them. The reaper promises new retention rules are "one row in RULES" (`reaper_service.py:17-22`), yet certification and ownership expiry are separate services (561 lines).
- **Simpler:** one `scheduled_job` table with leases and last-run state; retention and expiry rules as rows. This also hosts D11 and B8.

### S9. A configuration surface no operator can hold in their head
- **Today:** 243 settings against 76 documented variables; 32 booleans; ~70 governance/security knobs; per-sweeper lease, backoff and interval settings. **Twelve default-off flags that no compose file, `.env.example` or manifest turns on** keep ~3K lines dark: the reviewer agent (`config.py:315`, 977 lines), governance notifications (`:420`, 603 lines), the delivery worker (`:933`), principal reconciliation (`:600`), agent query memory (`:738`), seasonal / month-end / certification-expiry / ITSM quality branches, and legal hold. `.env.example:104` enables SIEM while `:113` disables the delivery worker it presumably needs.
- **Simpler:** for each flag, enable it in `compose.dev.yaml` with an end-to-end test, or retire the code. X9 alone removes ~15 settings.

### S10. The UI has three ways to do most things
- **Routes: 44 → 19**, grouped along the journey (table below). Tabs lazy-load the existing screen components rather than merging files; old ids stay as aliases so bookmarks keep working. The last review proposed six merges; none has happened, and four routes were added since — three of them 33–42-line wrappers around `TaskAgentConsole`.
- **Eight agent/AI screens → two** (Agents; AI models & trust).
- **Decisions are spread over six screens and six endpoint families** (reviews, parsed edges, relationship candidates, resolution candidates, source bindings, negative knowledge); the shared `ReviewDetail` is used by two. One Reviews surface needs the backend queue to federate those items.
- **Three ways to pick a datasource** (scope layer directly in 4 screens, `useDatasourcePicker` in 8, own capped fetch in 11 — D7) → one hook.
- **Loading and errors hand-rolled per screen** — 134 `useState<string|null>(null)` error states in 57 files beside two half-adopted helpers (`screenState.tsx` in 8 files; `AsyncState` in 2). Adopt as screens are touched.
- **Six hand-written copies of generated types**, and a transport adapter that stringifies bodies only to parse them again (`transport.ts:110`).

| Journey stage | Target screen | Folds in |
|---|---|---|
| My work | Home | home, inbox |
| Connect / ingest | Sources | sources, transformations, data-dictionaries |
| | Administration | administration |
| Ownership / meaning | Catalog | catalog |
| | Stewardship | stewardship, worklist, description-drafts, playbooks, negative-knowledge |
| | Meaning & metrics | meaning, semantics |
| | Relationships & identity | relationships, cross-source |
| | Lineage | lineage, unified-lineage |
| | Data quality | quality |
| Review | Reviews | governance, parsed-lineage-review (+ relationship and cross-source decisions once federated) |
| | Agents | agent-roster, reviewer-agent, steward-agent, lineage-agent, quality-agent |
| Consume | Ask Atlas | analyst |
| | Tools | tools, tool-plans |
| | Data products | marketplace, portfolio-analytics |
| | Developer | context, studio, developer |
| Evidence & ops | Audit & evidence | audit, compliance, refusals |
| | Operations | operations, reliability |
| | Access | access-policies, workspace-access, delegations |
| | AI models & trust | agents, ai |

### S11. Four connectors carried at full cost without a source
- **Today:** Snowflake 1,146 + Oracle 1,100 + BigQuery 918 + Databricks 665 = 3,829 lines and 2,784 test lines run against fake drivers, plus ~74 MB of drivers in every image (botocore alone is 25.8 MB, pulled in by Snowflake). Duplication between connectors is small (~3%); the cost is weight, CVE surface and dependency churn.
- **Simpler:** keep the code; move the drivers to an optional extra; label the four EXPERIMENTAL; make Oracle's import lazy (`oracle.py:7`). Promote each one when B5 runs it live.

### S12. Smaller items
- `mcp_server.py` is 2,652 lines in one file — split by tools, resources and prompts when next touched.
- Governance review now handles 43 `object_type` values; S5 and S4 reduce that directly.

*§6 adds process weight: tests, CI, generated artifacts, docs and history-in-comments.*

---

## 6. Repository, tests and documentation

The recurring cost here is churn rather than code: **22% of all commits touch a generated artifact, 40% touch the tracker, and 24% are merges.** Tests are mostly genuine behaviour tests (85% of test lines); the waste is copy-pasted setup.

| ID | Item | Verdict | Evidence | Size | Risk |
|---|---|---|---|---|---|
| P1 | **Stop regenerating the OpenAPI baseline on every change.** | Simplify | The 98,774-line `openapi-baseline.json` is touched by 127 of 653 commits, yet `scripts/openapi_diff.py:448-450` passes any non-breaking drift. It embeds docstrings, including 81 tracker IDs, and is the main collision point between parallel sessions. | ~20% of commits | Re-baseline per release, or store a signature-only baseline without descriptions |
| P2 | **Generate `ui-next/src/lib/types.ts` in the build instead of committing it** *(Candidate)* | Simplify | 5,260 lines, 53 commits, a byte-exact CI gate (`ci.yml:299-300`), 71 docstrings copied in. | ~53 regeneration commits | A generate step before `tsc` |
| P3 | **Make the doc generators non-gating** — except the destination inventory, which has security value | Simplify | ~3K lines of scripts keep four docs byte-exact: shim register (991), architecture map (761), destination inventory (738), surface-control matrix (503, gated through `tests/test_surface_control_matrix.py`). | Fewer forced regenerations | Docs may drift; they are labelled generated |
| P4 | **A shared tests conftest with the common fixtures** | Simplify | There is no conftest. A `session` fixture is defined in 97 files, `db` in 31, and the SQLite audit-id workaround `_assign_audit_event_id` in 35; ~2.6K lines are identical helper bodies. | 2.5–4K test lines | Migrate file by file |
| P5 | **Squash migrations to one baseline while no production database exists** *(Likely)* | Simplify | 150 revisions, 16.4K lines, 46 merges (44 of them `pass`-only), one head; "no production deployment artifact exists" (`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md:129`). | 16K lines → one file | Dev databases need `alembic stamp` or a rebuild; merges return unless new revisions rebase onto head |
| P6 | **Move callers off the platform shims** `aida.db`, `aida.config`, `aida.context` | Simplify | 141 + 105 test imports and 101 + 86 + 67 source/script imports; only one test pins a shim. | Removes 3 shims; shrinks the shim register | Touches ~300 files — needs a window with no parallel sessions |
| P7 | **Archive delivery history; keep one status authority** | Merge | 61% of Docs lines are history: 17 session logs (2.8K), the accomplishment log (12.3K), review-2026-08 (8K), `_superseded` (6.4K). The tracker is 456 KB in 652 lines (86 rows over 2,000 characters). Five documents each claim to be the status authority (README.md:163; Docs/README.md:31, :158, :197; POINTS-TRACKER). | ~32K lines out of the working set | Update links so the link check stays green |
| P8 | **Fix docs that contradict code** | Build (fix) | ADR count (README says 16, Docs/README says 17; 29 ADR files exist and the register omits ADR-0021, 0024, 0025, 0026). Module count (Docs/README says 1 or 5 of 21; 6 exist). The doc-claims test's own docstring says there are no import-linter contracts (there are 12). `ci.yml` says the surface matrix is not wired into CI (it runs inside pytest) and cites "94+" migrations (150). | Docs can be trusted | None |
| P9 | **Close the doc-claims gate's blind spot** | Build (fix) | `tests/test_doc_claims.py` exempts every citation into `src/atlas` (line 115). | The gate does what it says | May surface stale citations |
| P10 | **Stop writing history into comments and docstrings** | Simplify | 1,442 tracker IDs in `src` (947 in docstrings, which flow into OpenAPI); ~2.5K lines (range 1.5–3K) are history rather than enduring "why". Comments are 229 of 587 lines in `pyproject.toml` and 275 of 878 in `ci.yml`; `atlas/platform/config.py` has more comment lines (634) than code (397). Example: `pyproject.toml:181-187`. | Readability; less OpenAPI churn | Rewrite on touch, not in one sweep |
| P11 | **Consolidate CI** | Simplify | 19 jobs; `tests` is the 22.5-minute critical path and every other job finishes in 77 s or less; 11 jobs repeat `uv sync --extra dev`; the reachability test runs twice (`ci.yml:402`, `:524`); `ui-types-diff` overlaps the UI `tsc` step. Plus the always-red gates in D13. | ~6 fewer environment builds per push | Low |
| P12 | **Delete tracked clutter and orphan scripts** | Remove | `scratch_map.md` (a committed `SyntaxError` traceback); `Docs/UI_Audit_Report_2026-09-03.html` (no references); two `.docx` files (8.1 MB per clone — move to a shared drive and leave a pointer); `scripts/verify-stewardship.ps1` (cited only in the accomplishment log); the procedure capability matrix snapshot and its generator (the live endpoint serves it); `requirements-locked.txt` (CI regenerates it before use). | ~10 files, 8 MB | Low |
| P13 | **Reclaim ~2.1 GB of local clutter and fill the ignore gaps** | Remove | Five Linux virtualenvs and `.uv-cache-linux` (1.64 GB) point at an interpreter from a sandbox that no longer exists; `scratch/` is 419 MB (repo tarballs, screenshot folders); 106.6 MiB of loose git objects and 311 leftover temp objects from interrupted concurrent writes. Add `.venv*/`, `.uv-cache*/`, `.import_linter_cache/` and `coverage.xml` to `.gitignore`; run `git gc` only when no session is writing. | ~2.1 GB | None — untracked (skim the proof-gap reports in `scratch/` first) |

**Target doc set.** Keep ~20K canonical lines: product, architecture and ADRs, modules, contracts, engineering, security, and from delivery only the status, capability register, tracker (one-line rows), roadmap, epic backlog and connector backlog. Archive ~32K lines (61%) outside the link gate. Delete the tracked clutter in P12.

**Keep:** the real-PostgreSQL race and concurrency tests (8.7K lines — they cover what SQLite cannot), the meta-gates (~6K lines, 5% of tests — proportionate), `scripts/check_docs_links.py` (3.5 s, green), the single-head migration check, the migration-drift gate, both reachability gates, and the breaking-change logic in `scripts/openapi_diff.py`.

---

## 7. Status of the 5 September review

All 22 findings now have code behind them. The table records what is still missing; "Partial" means fixed in code but not verified against a real counterpart.

| Status | Findings |
|---|---|
| Fixed | F02, F03, F05, F07, F08, F09, F10, F12, F13, F14, F15, F16, F17, F18, F19 (wired, off by default), F20 (fresh answers only; rows are not kept when a run is reopened), F22; D01 (except `OrgPicker`, X8), D02, D03, D04, D05 |
| Partial | **F01** — archive states and a real S3 Object Lock provider exist; the default backend is `none` and only MinIO has been exercised. **F04** — real syslog/webhook transports behind a delivery ledger; off by default and no collector has received anything. **F06** — PKCE client exists; verified only against the mock issuer; reload drops the session. **F11** — posture is validated, checked at startup and reported; defaults are still SHADOW/OBSERVING (B3). **F21** — route error boundary and a clean automated WCAG scan; no screen-reader pass. **D06** — the register has drifted again (B14). |
| Regressions of a fixed class | D3 (a false WORM claim, like F01), D5 (a hand-built link, like F08), D7 (a capped picker, like F15). |

---

## 8. Keep — heavy-looking, but earning its place

- **The execution choke point:** `query_gateway.execute`, the SQL guard and validation, and the INV-2 import-linter contract — once D1 closes the one bypass.
- **Deterministic planning and prompt-risk screening; fail-closed model activation; the model-gateway kill switch read on every call.**
- **One governance decision service** for single, bulk and automated review (F05's compare-and-set claim).
- **Organization enforcement and token revocation on every OIDC request; delegation** (wired into permission checks, has a UI).
- **The audit envelope v2 and archive state machine; the `delivery_intents` ledger shape; readiness and posture reporting; the production settings validators.**
- **Quality incidents as the single sink; classification kept as asserted vs derived** (an ABAC input); the lineage provenance tables; the description-withdrawal design.
- **In the UI:** `routes.ts` + the location store + `useUrlState` (the F08/F09 fix), the core of `scope.tsx` (org-tagged responses, the F10 fix), the PKCE client, the single HTTP transport, `primitives.tsx`, `VirtualList`, the generated `types.ts` with its CI diff, and the frontend reachability gate — the reason UI dead code is down to ~230 lines.
- **The public tool SDK** (reachable only from scripts and tests by design) and the offline injection corpus.
- **ADR-0008 (no agent framework in core)** still holds and should keep holding.

---

## 9. Suggested order of work

The order matters: deletions and small fixes first make every later step cheaper, and the journey work proves which of the remaining surface is actually needed.

1. **Freeze new surface** — no new agents, screens or routers — until B1–B4 pass.
2. **Fix D1–D3** (the invariant bypass, the signing bypass, the false WORM claim), then D5–D6 in the UI and D8/D11 in the scheduler.
3. **Slim the default stack** with compose profiles (X9 step 1), repair the three red CI gates while preserving their security/control coverage (D13), clean local clutter (P13), and stop regenerating the OpenAPI baseline on every change (P1).
4. **Delete the verified-dead set:** X2–X4, X6, X8, and X7 unless MCP agents are imminent. Make demo mode a build-time flag (X1 step 1).
5. **Make the journey pay off:** B1, B2, B7, B8, B4, then B11 to keep it working.
6. **Triage unused endpoints** cluster by cluster (X5). Migration squash is deferred pending current database inventory and upgrade/rollback evidence (P5). Reconcile history before physical archival (P7).
7. **Consolidate storage** (S5), moving each consolidated family into a feature package with no shim (S6).
8. **Simplify the surfaces:** navigation 44 → 19 (S10), agent governance (S2), artifacts (S4), retrieval (S3), scheduling (S8), compliance (S7).
9. **Before the first customer pilot:** complete B3 enforcement and B6 identity/secrets verification; collect their prerequisites now. B5/B15 certify the selected connector and estate; B16 follows measured need.

---

## Appendix — how this was measured

- **Import graph and symbol use:** an AST walk of `src/`, `sdk/`, `scripts/`, `tests/` and `migrations/` (860 modules, 24,637 import edges including 750 function-local ones). 339 `aida` modules are live; 4,541 top-level symbols, of which 481 are registered by decorator (routes, Temporal definitions) and treated as live.
- **Routes:** every decorator on an `APIRouter`, path-normalized and matched against `ui-next/src`, `sdk/`, `scripts/` and `tests/`, plus a per-segment grep of the UI for zero-caller clusters.
- **UI:** an import graph from `main.tsx` and the add-in entries, respecting `React.lazy`; bundle chunks attributed with source maps.
- **Settings:** every `Settings` field searched across the repo, then hand-checked for dynamic `getattr` reads (15 false positives removed).
- **Commit themes:** a keyword classifier over `git log --since=2026-08-28` (651 commits). Theme counts are approximate.
- **CI:** job durations and outcomes from recent GitHub Actions runs.
- Not done: no test suite run, no load test, no browser session, no live connector or model call.
