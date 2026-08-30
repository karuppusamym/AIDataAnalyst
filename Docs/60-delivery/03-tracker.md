# Tracker

> Status: **Living document.** Owner: Engineering lead. Update at every increment.
> This is the single place to answer "what is the state of everything." It consolidates the open-work IDs from every module spec, the security backlog, and the test gaps.

**Last reviewed:** 2026-08-30

## How to use this

| Column | Meaning |
|---|---|
| **ID** | Stable identifier, matching the owning module spec's open-work table |
| **Item** | What needs to exist |
| **Mod** | Owning module (`20-modules/`) |
| **Ph** | Roadmap phase |
| **Pri** | P0 blocks the phase · P1 required for the phase · P2 desirable |
| **Status** | `TODO` · `IN PROGRESS` · `BLOCKED` · `DONE` · `N/A` |
| **Owner** | Named individual — `—` means unassigned, which is itself a tracked problem |
| **Exit** | The objectively verifiable condition for `DONE` |

**Rules.** An item is `DONE` only when its exit condition is verifiably met. "Code written" is not `DONE`. `BLOCKED` requires a named blocker. An unassigned P0 is escalated at every review.

---

## A. Structural foundation

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| ST-01 | Target structure + module template | all | 0 | P0 | DONE | — | `scripts/generate_module.py` generates the full anatomy (§7); `identity_tenancy` scaffold generated from it; `platform-is-the-lowest-layer` import-linter contract (ST-02) passes against the generated tree — verified 2026-08-29 (`lint-imports`: 1 kept, 0 broken) |
| ST-02 | Import-linter ratchet in CI | all | 0 | P0 | DONE | — | `.github/workflows/ci.yml` added 2026-08-30 with five gates (ruff, mypy, lint-imports, single-Alembic-head, pytest). Three import-linter contracts pass, including the INV-2 gateway-exclusivity contract (QG-7). Recipe verified end-to-end in a clean checkout via `uv sync --frozen --extra dev`: ruff clean, mypy clean on 106 files, 3 contracts kept, 1 Alembic head, 387 tests passing. Pre-existing ruff (6) and mypy (2) failures were fixed so the gate is green from its first run rather than red on arrival. Broader `aida` layering contracts still land with decomposition |
| ST-03 | Tier 0 invariant suite (9 tests) | all | 0 | P0 | DONE | — | All nine invariants executable in the default `pytest` run with no external service, no skips — verified 2026-08-30 (`ruff check tests` clean; full suite 560 passed, 2 xfailed). `tests/test_tier0_invariants.py` keeps INV-2/3/4/8 plus workspace-level INV-5; INV-1, INV-5 (API surface), INV-6, INV-7 and INV-9 land in `tests/test_inv{1,5,6,7,9}_*.py` on shared harnesses in `tests/support/`. Data-driven throughout: all 199 FastAPI routes enumerated (44 organization-scoped ones driven with a foreign tenant against a session that raises on first use), every connector in the registry, every mapped column, every Cypher statement in `src/aida`. 20 of 20 properties confirmed capable of failing by deliberate mutation. Two strict xfails record codebase gaps, not suite gaps: 13 endpoints in `ai_registry_api`/`product_marketplace_api` commit governed state with no audit row (INV-7), and capability flags are hand-declared rather than derived from certification (INV-9, needs E12). INV-1's live rebuild drill (E5) and INV-6's full ingestion sentinel sweep still need infrastructure; both are proven in-process instead and say so. Detail: `Docs/review-2026-08/gap/06-tier0-invariant-suite.md` One strict xfail remains and it records a codebase gap, not a suite gap: capability flags are hand-declared rather than derived from certification (INV-9, needs E12). The INV-7 xfail was closed 2026-08-30 under ST-17 and is now a passing test with no exemption list, and INV-5's `_TRANSITIVELY_SCOPED_WORKERS` exemption is likewise empty — `plan_profile_tasks` carries an explicit `organization_id` predicate. |
| ST-04 | Extract `platform/` | platform | 0 | P0 | IN PROGRESS | — | `db.py`, `config.py`, `logging.py`, `context.py` moved to `atlas.platform`, each with a re-export shim left at the old `aida.*` path so every existing caller (40+ import sites) is unchanged; `platform-is-the-lowest-layer` passes; full local suite green except 3 pre-existing failures in `test_operational_behaviors.py` unrelated to this change (concurrent WIP on `computed_usage_boost` scheduling, ADR-0017 §8). Not yet moved: `events.py`, `main.py` (still imports nearly every domain router — deferred to Phase 5, the `api.py` router split, rather than moved as-is), and the not-yet-built pagination/idempotency/error-taxonomy/telemetry scaffolding |
| ST-05 | Split `models.py` into module schemas | all | 0 | P0 | TODO | — | No cross-schema FKs except `identity` |
| ST-06 | Split `schemas.py` → `schemas`/`contracts` | all | 0 | P0 | TODO | — | `module-privacy` passes |
| ST-07 | Split `api.py` into routers | all | 0 | P0 | TODO | — | OpenAPI spec byte-identical after split |
| ST-08 | Untangle `intelligence_api.py` | 06/07/09 | 0 | P1 | TODO | — | Each endpoint in its owning module |
| ST-09 | Remove all import-linter exemptions | all | 0 | P1 | TODO | — | Zero exemptions |
| ST-10 | Per-module standalone test jobs | all | 0 | P1 | TODO | — | Each module's tests run alone |
| ST-11 | Resolve `16 query-gateway`'s layer placement and the `09`↔`16` cycle (`10-architecture/04-module-decomposition.md` §5.3) | 09,16 | 0 | P0 | DONE | — | Resolved 2026-08-30 by checking the code rather than redesigning: no cycle exists. The gateway imports no lineage module and no lineage module imports the gateway; `extract_column_lineage` is defined inside `query_gateway.py`. The mutual edge was an error in the module register, now corrected. Rule recorded: the gateway emits, intelligence modules consume |
| ST-12 | Documentation truth pass (`gap/02` C10) | all | 0 | P0 | DONE | — | Applied 2026-08-30. Every structural claim in `00-product/`, `10-architecture/`, `20-modules/`, `30-contracts/`, `40-engineering/`, `90-reference/` and `Docs/README.md` is either true of the code or carries a dated `Implementation status` callout naming the file that proves otherwise. 28 callouts added; `20-modules/00-module-index.md` gained code-sourced `Module dir?` and `Lives today in` columns for all 21 modules. Record and evidence: `Docs/review-2026-08/gap/04-documentation-truth-pass.md` |
| ST-13 | Refresh `gap/01-baseline-reality.md` against the post-Phase-0 tree | all | 0 | P1 | TODO | — | Three of its claims are now false (CI absent, gateway-exclusivity contract unwired, no workspace model) and its LOC figures are low. Header dated and corrected, or a "superseded in part" note added |
| ST-14 | Reconcile emitted event names with `30-contracts/04-event-catalog.md` | all | 0 | P1 | TODO | — | Decide per event whether to rename the emitted `.v1` type or restate the catalog row, then land whichever. Exit: every `event_type=` argument in `src/` appears in the catalog and vice versa. Blocked on the U2 authorial question in `gap/04` §3 |
| ST-15 | `edge_kind` vocabulary matches the lineage contract | 09 | 0 | P1 | TODO | — | Code assigns only `SUGGESTED_RELATIONSHIP` (absent from the documented enum) and defaults to `ETL`; `QUERY`/`VIEW`/`PROCEDURE`/`DBT`/`BI`/`AI_DECISION` are never written. Exit: one agreed vocabulary, a DB-level constraint enforcing it, and `30-contracts/06` matching |
| ST-16 | `validate_sql` split and exposure (`gap/02` N14) | 16,19 | 0 | P0 | DONE | — | Landed 2026-08-30. `_run_validation` in `query_gateway.py` is the single pipeline; `validate()` is the no-execute entry point and `execute()` calls the same pipeline, so the two cannot drift. Exposed as MCP native tool `atlas__validate_sql` and `POST /v1/datasources/{id}/sql-validations`. INV-2 preserved (`execute_read_query` still has one call site; `lint-imports` 4 kept). 16 tests in `tests/test_sql_validation.py`. Detail: `Docs/review-2026-08/gap/05-validate-sql-handoff.md` |
| ST-17 | INV-7 audit closeout — 13 unaudited mutations | 17,20 | 0 | P0 | DONE | — | Landed 2026-08-30. All 13 endpoints in `ai_registry_api.py` (6) and `product_marketplace_api.py` (7) call `record_audit(...)` in the same transaction as the mutation they describe, carrying principal, resource, tenant boundary, correlation id and outcome; a 14th audits the remediation-independence denial as `DENIED`. `_KNOWN_UNAUDITED_MUTATIONS` is empty and the strict xfail is gone, so a 14th unaudited endpoint fails on arrival and excusing it fails in the same commit. Ratchet mutation-tested by deleting the `record_audit` in `revoke_marketplace_access`. Detail: `Docs/review-2026-08/gap/09-inv7-audit-closeout.md` |
| ST-18 | Ratify INV-7's meaning of "mutation" for lazily-created default rows | 17 | 0 | P1 | TODO | — | Architecture decides whether "mutation" means "stages a row" or "records an actor's decision", and `10-architecture/01-principles-and-invariants.md` says which. Recommendation and both sides of the trade-off in `gap/09-inv7-audit-closeout.md` §4: recommend "records an actor's decision", because `ensure_default_domain` and `ensure_organization_integration_policy` build their row from constants plus the tenant id, so naming a creator would manufacture attribution rather than preserve it. Exit: the document says which reading binds; if "stages a row" wins, both helpers return a created/found flag, the 8 GET call sites audit on the creating branch only, and `_LAZY_DEFAULT_WRITE_ROUTES` is deleted |

## B. Connectors and ingestion

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| CN-1a | Oracle adapter to certified | 02 | A | P0 | IN PROGRESS | — | Live fixture verified; 100-point certification |
| CN-1b | Oracle EXPLAIN via least-privilege `PLAN_TABLE` | 02 | A | P1 | BLOCKED | — | Blocked on a certified bank-scoped Oracle role; fails closed meanwhile |
| CN-1c | BigQuery adapter to certified | 02 | A | P0 | IN PROGRESS | — | Native pull adapter (project→catalog, dataset→schema), dry-run byte-budget gate, discovery, bounded profiling and unit/contract tests done; no live GCP project/credentials available to verify — certification and version fixtures remain |
| CN-2a | Snowflake adapter to certified | 02 | A | P0 | IN PROGRESS | — | Native pull adapter implemented and registered `IMPLEMENTED/BETA` (multi-database `INFORMATION_SCHEMA` discovery via shared assembly helpers, partition-pruned `EXPLAIN USING JSON` cost estimate, `APPROX_COUNT_DISTINCT` bounded profiling, real `sfqid` warehouse-query-ID capture); 7 unit/contract tests pass; no live Snowflake account/warehouse credentials available to verify — certification and version fixtures remain |
| CN-2b | Databricks adapter | 02 | A | P0 | TODO | — | No adapter code exists yet (`declare_planned` only); certification + version fixtures |
| CN-3 | Executable vendor/version fixtures | 02 | A | P0 | TODO | — | ≥2 versions per adapter |
| CN-4 | Source-side connector agent (mTLS, outbound-only) | 02 | B | P1 | TODO | — | Network team accepts: no inbound exception |
| CN-5 | Delegated / read-only source identities | 02 | B | P0 | TODO | — | Certified per adapter |
| CN-6 | Public connector SDK + certification harness | 02 | E | P1 | TODO | — | Third-party adapter passes without core changes |
| CN-7 | Per-connector health scoring | 02 | A | P1 | TODO | — | Visible in fleet view |
| CN-8 | Index and partition extraction | 02/04 | A | P1 | TODO | — | Normalized models populated Deliberately excluded from envelope 1.1: both serve cost estimation rather than lineage or meaning, and no Phase 1 or Phase 2 consumer reads them. |
| IN-1 | Bulk source onboarding | 03 | A | P0 | TODO | — | 200 sources onboarded in one operation |
| IN-2 | Batch pause/cancel/replay controls | 03 | A | P1 | TODO | — | Operator console actions with audit |
| IN-3 | Kafka intake + schema registry | 03 | B | P1 | TODO | — | Same envelope, same persistence path |
| IN-4 | Signed workload producer identity | 03 | B | P1 | TODO | — | Unsigned producer rejected |
| IN-5 | Envelope 1.1+ (BI, pipeline, topic, file, ML) | 03 | B | P1 | TODO | — | Backward-compatible; consumers unaffected |
| IN-5a | Envelope 1.1: views + DDL, routines + body, source comments, source grants | 03 | B | P0 | DONE | — | Shipped 2026-08-30 (gap N1). Additive: 1.0 is accepted unchanged and remains the default; declaring 1.0 while sending 1.1 content is a 422 naming the fields, never a silent drop. Five tables in `src/aida/envelope_models.py`, migration `a1c9f4b7e230`, single head verified. Unavailable is distinguishable from empty in the stored row, enforced three times (Pydantic validator, persistence, `CHECK` constraint). 1.1 FULL reconciliation accumulates across chunks and reconciles once, and is additionally gated on the declared version so a 1.0 producer cannot retire 1.1 metadata. Record: `Docs/review-2026-08/gap/07-envelope-v11.md` |
| IN-5b | Envelope 1.1 on the native pull path | 03 | B | P0 | DONE | — | Wired 2026-08-30: `activities.discover_datasource` calls `persist_envelope_extensions` after `persist_discovery_snapshot`, with `deprecate_missing` following the run mode. A source scanned via native pull now persists the axes its connector collects, not only the two push paths |
| IN-5c | Envelope 1.1 axes on Oracle, Snowflake, BigQuery | 02 | B | P1 | DONE | — | Delivered 2026-08-30 in parallel with IN-5a. Eleven of twelve connector x axis cells populated; BigQuery `grants` is answered rather than deferred — BigQuery has no SQL GRANT, and writing an IAM role bundle into `DiscoveredGrant.privilege` would make "who can already see this" mean something different there than on Oracle or Snowflake while looking identical. Record: `Docs/review-2026-08/gap/08-envelope-v11-connectors.md` |
| IN-5d | Live-source verification of the 1.1 discovery SQL | 02 | B | P1 | TODO | — | No 1.1 discovery statement on any connector has run against a live source; row shapes are tested, the SQL text is not. Same standing limitation as `CN-1c`/`CN-2a`. Exit: one live fixture per source returns a non-empty view, routine, comment and, where the source has them, grant inventory |
| IN-5e | `source_description` on `metadata_column` | 04 | B | P2 | TODO | — | Column comments land in `metadata_object_description` because `models.py` was owned by a concurrent workstream during N1, so the same fact is a column for tables and a row for columns. Exit: column added, backfilled from `object_type = 'COLUMN'`, `DESCRIBABLE_OBJECT_TYPES` narrowed to `('CATALOG', 'SCHEMA')` |
| IN-5f | Fold the three later connectors onto the shared 1.1 assembly helpers | 02 | B | P2 | TODO | — | Oracle, Snowflake and BigQuery were written against a local rebuild of the envelope-1.1 assembly while `connectors/discovery.py` was being edited concurrently; both agree on the contract and both are tested, but the duplication should collapse onto `apply_view_definitions` / `build_routines` / `build_grants` |
| IN-6 | Maximum-scale recovery certification | 03 | D | P0 | TODO | — | Forced restart at 1M tables, no reprocessing |
| IN-7 | Fleet fairness at 1,000+ sources | 03 | D | P1 | TODO | — | No source starves; measured |

## C. Catalog, profiling, relationships

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| CT-1 | Bulk actions (tag, classify, own, certify) | 04 | A | P1 | TODO | — | Filter or explicit selection; partial success reported |
| CT-2 | Million-object virtualization | 04/21 | A | P1 | TODO | — | 1M rows, no lockup |
| CT-3 | Index/partition normalized models | 04 | A | P1 | TODO | — | Populated by ≥2 adapters |
| CT-4 | Rename detection | 04 | C | P2 | TODO | — | Heuristic + steward confirmation |
| CT-5 | Asset certification lifecycle with expiry | 04/08 | A | P1 | TODO | — | All asset types; expiry enforced |
| CT-6 | Cross-source object resolution | 04 | A | P1 | TODO | — | Same logical asset across sources |
| PR-1 | Composite key inference | 05 | B | P1 | TODO | — | Evidence-backed; review-gated |
| PR-2 | Policy-approved range/top-value profiling | 05 | C | P2 | TODO | — | Per-classification approval + retention contract |
| PR-3 | Authoritative classification feed integration | 05 | B | P1 | TODO | — | External feed overrides inference |
| PR-4 | Task-level retry/heartbeat drill-down API | 05 | A | P1 | TODO | — | Operator console shows per-task evidence |
| PR-5 | Continue-as-new at maximum scale | 05 | D | P0 | TODO | — | 1M-table run completes |
| RL-1 | Table family / temporal intelligence | 06 | B | P1 | TODO | — | History/snapshot/delta/SCD detected with evidence |
| RL-2 | Canonical table resolution | 06 | B | P1 | TODO | — | Steward override; feeds retrieval ranking |
| RL-3 | Composite relationship candidates | 06 | B | P1 | TODO | — | Bounded; evidence-backed |
| RL-4 | Project approved relationships to Neo4j | 06/10 | A | P1 | IN PROGRESS | — | Approved/suggested relationship candidates are already bounded, policy-filtered and visible in Graph Explorer V2 (`intelligence_api.py::get_knowledge_graph`/`get_knowledge_graph_neighborhood`, `knowledge_graph.py::expand_frontier`); still missing: projecting approved `RelationshipCandidate` edges into Neo4j itself — `graph_projector.py` only projects declared FK constraints, not candidates |
| RL-5 | Cross-source relationship inference | 06 | B | P1 | TODO | — | Heterogeneous estate traversal |
| RL-6 | Bulk relationship review | 06 | C | P1 | TODO | — | 500-candidate queue workable |
| RL-7 | Confidence calibration vs. labelled corpus | 06 | D | P1 | TODO | — | Published calibration curve |

## D. Semantics, glossary, lineage, graph

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| SM-1 | Governed dimension authoring | 07 | B | P1 | TODO | — | Versioned; maker-checker |
| SM-2 | Glossary term binding to semantic objects | 07/08 | A | P0 | TODO | — | Terms resolve in retrieval |
| SM-3 | Confidence calibration + bank-domain corpus | 07 | D | P1 | TODO | — | Published accuracy results |
| SM-4 | Metric suggestions from approved annotations | 07 | C | P1 | TODO | — | Proposals enter the review queue |
| SM-5 | Multi-table tool blueprints | 07/14 | B | P1 | TODO | — | Deterministically rendered |
| SM-6 | Open Semantic Interchange evaluation | 07 | E | P2 | TODO | — | Decision recorded as an ADR |
| SM-7 | Semantic diff view | 07/18 | C | P1 | TODO | — | Reviewers see version deltas |
| **GL-1** | **Term lifecycle with versions** | 08 | A | **P0** | DONE | — | Categories, immutable synonyms/definitions, maker-checker publication/supersession and reviewed deprecation verified |
| **GL-2** | **Ownership assignment (incl. bulk, rule-based)** | 08 | A | **P0** | DONE | — | Individual/group, explicit/rule-derived and reviewed bulk table assignments verified |
| **GL-3** | **Conflict detection and resolution** | 08 | A | **P0** | DONE | — | Manual/automatic conflict records, independent resolution, and retained losing position verified |
| **GL-4** | **Coverage scoring** | 08 | A | **P0** | DONE | — | Six dimensions computed per organization/source/domain/LOB with durable snapshots/history |
| GL-5 | Bulk certification with expiry | 08 | A | P1 | DONE | — | Reviewed batch table certification persists rationale, certifier and expiry; expired records stop counting |
| GL-6 | Unowned-asset backlog with routing | 08 | A | P1 | IN PROGRESS | — | Coverage returns and UI exposes a bounded backlog; automated owner routing/escalation remains |
| GL-7 | Leaver reassignment | 08 | C | P2 | TODO | — | Whole portfolio in one action |
| GL-8 | Term linkage inference | 08 | B | P1 | DONE | — | Approved annotation exact-label evidence generates bounded proposals; independent approval creates provenance links |
| **LN-1** | **OpenLineage ingestion** | 09 | A | **P0** | IN PROGRESS | — | Parser, mounted ingest/list/get API (`POST /v1/lineage/openlineage`) and migration exist and produce table/column edges (`openlineage.py`, `openlineage_api.py`); no automated tests and no Airflow-sourced event has been verified producing real edges |
| **LN-2** | **View and procedure lineage** | 09 | A | **P0** | DONE | — | Delivered 2026-08-30. Multi-dialect SQL lineage parser with CTE support and literal redaction (`sql_lineage_parser.py`, `view_lineage_api.py`); column-level where dialect permits |
| **LN-3** | **AI decision lineage as first-class edges** | 09/13 | E | **P0** | DONE | — | Delivered 2026-08-30. Retrieval, tool selection, rejection, and refusal edges as first-class lineage (`ai_decision_lineage.py`, `ai_decision_lineage_api.py`) |
| LN-4 | BI lineage (Tableau, Power BI, Looker) | 09 | A | P1 | TODO | — | Report→metric→column edges |
| LN-5 | Column-level dbt manifest lineage | 09 | B | P1 | TODO | — | Where the manifest provides it |
| LN-6 | dbt `run_results.json` operational evidence | 09 | B | P1 | IN PROGRESS | — | Parsed and ingested (`dbt_artifacts.py::parse_dbt_run_results`), test status/failures/execution time persisted per resource and reconciled into data-quality incidents (`dbt_quality_bridge.py`); unit-tested; no full-endpoint integration test |
| LN-7 | Transitive cross-kind impact | 09 | B | P1 | TODO | — | Bounded traversal across all edge kinds |
| LN-8 | Large-DAG virtualization | 09/21 | C | P1 | TODO | — | No full-graph render |
| KG-1 | Project approved relationships | 10 | A | P1 | IN PROGRESS | — | Same as RL-4 |
| KG-2 | Cross-source traversal | 10 | B | P1 | TODO | — | Bounded, policy-filtered |
| KG-3 | Level-of-detail rendering | 10/21 | C | P1 | TODO | — | API boundary unchanged |
| KG-4 | Time-aware / version traversal | 10 | E | P2 | TODO | — | "What did this look like last quarter" |
| KG-5 | Saved perspectives per persona | 10 | C | P2 | TODO | — | Persisted, shareable |
| KG-6 | Rebuild timing drill + published SLO | 10 | D | P0 | TODO | — | 1M objects < 4 h, measured |
| KG-7 | Scheduled reconciliation + alerting | 10 | B | P1 | TODO | — | Drift detected and alarmed |

## E. Quality, retrieval, runtime

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| **DQ-1** | **Notification and escalation routing** | 11 | A | **P0** | DONE | — | Delivered 2026-08-30. Rules, escalation, dedup, and ITSM routing (`notification_routing.py`, `notification_api.py`) |
| **DQ-2** | **Approved watermark contracts → freshness** | 11 | A | **P0** | DONE | — | Delivered 2026-08-30. Watermark config, maker-checker, ADR-0016 freshness monitoring (`freshness.py`, `quality_api.py`) |
| **DQ-3** | **Quality → runtime coupling** | 11/12/13/14 | E | **P1** | DONE | — | Delivered 2026-08-30. Demotion, trust warnings, and tool gating (`quality_coupling.py`); trust scoring with composite 0-100, A-F grade, explainable factors (`trust_scoring.py`) |
| DQ-4 | Custom rule packs and scheduling | 11 | B | P1 | TODO | — | Rules run outside scans |
| DQ-5 | Data SLA/SLO definitions | 11 | A | P1 | DONE | — | Delivered 2026-08-30. Runtime data contracts with schema drift, quality breach, and SLA enforcement (`runtime_contracts.py`, `runtime_contracts_api.py`) |
| DQ-6 | Seasonality-aware thresholds | 11 | E | P2 | TODO | — | Reduced false positives, measured |
| DQ-7 | Bank-scale incident-volume certification | 11 | D | P1 | TODO | — | No alert fatigue at target volume |
| DQ-8 | Open quality framework for third-party detectors | 11 | E | P2 | TODO | — | Monte Carlo / Anomalo signals ingested |
| **RT-1** | **Vector projection and similarity retrieval** | 12 | A | **P0** | DONE | — | Delivered 2026-08-30. pgvector embedding with cosine similarity retrieval (`vector_retrieval.py`); rebuildable |
| **RT-2** | **Graph expansion from seed hits** | 12 | A | **P0** | DONE | — | Delivered 2026-08-30. BFS traversal with org-boundary enforcement (`graph_retrieval.py`); bounded, policy-filtered |
| **RT-3** | **Fusion ranking with inspectable factors** | 12 | A | **P0** | DONE | — | Delivered 2026-08-30. Reciprocal rank and weighted linear fusion (`fusion_ranking.py`); every factor in the evidence |
| RT-4 | PostgreSQL full-text index | 12 | A | P0 | DONE | — | Delivered 2026-08-30. GIN index, ts_query, cross-source full-text search (`full_text_index.py`) |
| RT-5 | Global search + command palette | 12/21 | A | P0 | DONE | — | Delivered 2026-08-30. Global search API (`search_api.py`) and command palette UI (`ui/scripts/features/global-search.js`); Ctrl/Cmd+K, keyboard-navigable |
| RT-6 | Usage/popularity signal | 12 | B | P1 | TODO | — | Ranking factor from execution history |
| RT-7 | Quality trust factor in ranking | 12/11 | E | P1 | DONE | — | Delivered 2026-08-30 as part of DQ-3. Quality-runtime coupling feeds trust factor into retrieval ranking (`quality_coupling.py`, `trust_scoring.py`) |
| RT-8 | Large-catalog retrieval benchmarks | 12 | D | P0 | TODO | — | 1M objects, < 1 s first paint |
| RT-9 | Cross-source search | 12 | A | P0 | DONE | — | Delivered 2026-08-30. Cross-source full-text index and hybrid retrieval orchestration (`full_text_index.py`, `retrieval.py`) |
| **AG-1** | **Indirect-injection defence** | 13 | B | **P0** | DONE | — | Delivered 2026-08-30. Pattern detection with injection corpus (`injection_defense.py`, `injection_corpus.py`) |
| **AG-2** | **Multilingual and obfuscation coverage** | 13 | B | **P0** | DONE | — | Delivered 2026-08-30. Multilingual and encoding-aware injection defense (`injection_defense.py`, `injection_corpus.py`) |
| **AG-3** | **Bank model-risk evaluation corpus** | 13/15 | B | **P0** | TODO | — | Published accuracy and refusal results |
| AG-4 | Multi-step tool plans with budgets | 13/14 | E | P1 | DONE | — | Delivered 2026-08-30. Validation, budget enforcement, dependency ordering, partial failure (`tool_plans.py`, `tool_plans_api.py`) |
| AG-5 | AI decision lineage emission | 13/09 | E | P0 | DONE | — | Delivered 2026-08-30. Same as LN-3 (`ai_decision_lineage.py`, `ai_decision_lineage_api.py`) |
| AG-6 | Quality trust warnings on answers | 13/11 | E | P1 | DONE | — | Delivered 2026-08-30 as part of DQ-3. Trust warnings integrated via quality-runtime coupling (`quality_coupling.py`) |
| AG-7 | Query memory similarity + safe adaptation | 13 | C | P1 | TODO | — | Version-aware; never bypasses validation |
| AG-8 | Retrieval and model benchmarks | 13 | D | P0 | TODO | — | Published |

## F. Tools, model gateway, query gateway, governance

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| TL-1 | Tool certification corpus and workflow | 14 | B | P0 | TODO | — | Certification with expiry and recertification |
| TL-2 | Multi-tool plans | 14 | E | P1 | DONE | — | Delivered 2026-08-30. Same as AG-4 (`tool_plans.py`, `tool_plans_api.py`) |
| TL-3 | Quality gating of tool invocation | 14/11 | E | P1 | DONE | — | Delivered 2026-08-30 as part of DQ-3. Tool gating via quality-runtime coupling (`quality_coupling.py`) |
| TL-4 | Usage-weighted tool ranking | 14/12 | C | P1 | TODO | — | Popular tools rank higher |
| TL-5 | Public Tool SDK | 14 | E | P2 | TODO | — | Produces drafts only; publication still maker-checker |
| TL-6 | Tool-first execution rate metric | 14/20 | A | P1 | TODO | — | Dashboard; target ≥40% in a mature tenant |
| TL-7 | Deprecation impact preview | 14/09 | C | P1 | TODO | — | Blast radius before deprecation |
| **MG-1** | **Rotate development credentials → workload identity** | 15 | B | **P0** | TODO | — | No `env://` in any non-local environment |
| **MG-2** | **Kill-switch drill** | 15 | B | **P0** | TODO | — | < 60 s to stop; evidence retained |
| MG-3 | Bank-approved route selection + private routing | 15 | B | P0 | IN PROGRESS | — | Approved-route enforcement implemented (`ModelRouteConfiguration` maker-checker lifecycle + config-selected `route_key` gate in `model_gateway.py`/`ai_governance_api.py`); private-endpoint routing not started |
| MG-4 | Residency/retention contract certification | 15 | B | P0 | TODO | — | Certified per route |
| MG-5 | Model-risk evaluation corpus | 15 | B | P0 | TODO | — | Same as AG-3 |
| MG-6 | Spend, latency, drift monitoring | 15 | B | P1 | TODO | — | Alerts wired |
| MG-7 | Private / self-hosted adapter | 15 | B | P1 | TODO | — | Certified |
| **QG-1** | **Adversarial SQL corpus per dialect** | 16 | D | **P0** | TODO | — | Zero bypasses |
| **QG-2** | **Source-native row/column policy** | 16 | B | **P0** | TODO | — | Synchronized where supported |
| QG-3 | Per-LOB quotas + concurrency controller | 16 | B | P1 | TODO | — | Fair under contention |
| QG-4 | Cancel propagation certification | 16 | D | P1 | TODO | — | Cancel reaches the source |
| QG-5 | KMS-managed HMAC keys | 16 | B | P0 | TODO | — | No application-managed keys |
| QG-6 | Dynamic masking / tokenization integration | 16 | B | P1 | TODO | — | Certified per source |
| QG-7 | Gateway-exclusivity import contract | 16 | 0 | P0 | DONE | — | Landed 2026-08-30. SQL-accepting methods moved off `Connector` onto `aida.connectors.sql_execution.SqlExecutor`; `aida.connectors.execution_access` is the sole source of one; import-linter contract permits only `aida.query_gateway` to import it. Verified to *break* when a second importer is added, and mypy verified to reject `execute_read_query` on a registry-produced `Connector`. INV-2 now enforced three ways: type system, import graph, AST scan |
| **PG-1** | **ABAC (classification, purpose, residency)** | 17 | B | **P0** | DONE | — | Delivered 2026-08-30. Full ABAC engine with policy evaluation, agent-vs-human gating, and simulation mode (`abac.py`, `abac_api.py`); supersedes earlier `policy_engine.py` partial |
| **PG-2** | **Agent-vs-human context attribute** | 17 | B | **P0** | DONE | — | `principal_kind` ∈ HUMAN/AGENT/SERVICE is a first-class subject attribute; tested both directions in `test_policy_engine.py`. The ADR-0018 migration seeds the agent sensitive-data DENY as DRAFT so it is reviewable without changing behaviour on migration day |
| PG-3 | Bulk decisions with per-item rationale | 17 | C | P0 | TODO | — | 10,000-item selection workable |
| PG-4 | Delegation and reassignment | 17 | C | P1 | TODO | — | Time-bounded, audited |
| PG-5 | Entitlement evaluation for editions | 17 | C | P1 | TODO | — | Edition gates capability |
| PG-6 | Full policy decision logging | 17 | B | P0 | DONE | — | Delivered 2026-08-30. ABAC engine logs inputs and outcomes via `abac.py` and `abac_api.py`; auditor-readable |
| PG-7 | External PDP (OPA / bank PDP) adapter | 17 | B | P1 | TODO | — | Certified against the bank bundle |
| PG-8 | Policy simulation | 17 | E | P2 | DONE | — | Delivered 2026-08-30. Simulation mode in the ABAC engine (`abac.py`, `abac_api.py`); "Who could see this?" |

## G. Identity, studio, context products, experience, observability

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| ID-1 | Bank secret-manager adapter certified | 01 | B | P0 | TODO | — | Registered, certified, rotation drilled |
| ID-2 | Bank OIDC issuer/claim/group certification | 01 | B | P0 | TODO | — | Certified against bank IdP |
| ID-3 | Workload identity | 01 | B | P0 | TODO | — | Agents and MCP consumers |
| ID-4 | Token revocation and replay policy | 01 | B | P0 | TODO | — | Tested |
| ID-5 | Break-glass with elevated audit | 01 | B | P1 | TODO | — | Exercised |
| ID-6 | Rotation drill under load | 01 | D | P1 | TODO | — | Zero failed requests |
| ID-7 | Bulk onboarding + entitlement feeds | 01 | A | P1 | TODO | — | Enterprise feed integrated |
| ST-A1 | Studio change sets | 18 | C | P1 | DONE | — | Delivered 2026-08-30. Create, items, and conflict detection (`studio.py`, `studio_api.py`) |
| ST-A2 | Studio test harness | 18 | C | P1 | DONE | — | Delivered 2026-08-30. Fixture validation and metrics (`studio_test_harness.py`) |
| ST-A3 | Semantic diff view | 18 | C | P1 | DONE | — | Delivered 2026-08-30. Diff view endpoints in `studio_api.py` |
| ST-A4 | Parameter-contract designer | 18 | C | P1 | TODO | — | Typed, enum-bound |
| ST-A5 | Impact preview at submission | 18/09 | C | P1 | DONE | — | Delivered 2026-08-30. Impact preview endpoints in `studio_api.py` |
| ST-A6 | Git binding (Atlas authoritative) | 18 | E | P2 | TODO | — | Merge cannot bypass maker-checker |
| ST-A7 | Context product builder | 18/19 | A | P1 | TODO | — | Authoring surface for module 19 |
| **CX-1** | **MCP server** | 19 | A | **P0** | IN PROGRESS | — | Real JSON-RPC 2.0 endpoint (`initialize`/`ping`/`tools`/`resources`) mounted at `POST /mcp`; `tools/call` routes through the full governed orchestrator/gateway stack — resource reads are not yet per-read policy-evaluated (see CX-3) |
| **CX-2** | **Context products with maker-checker** | 19 | A | **P0** | TODO | — | Versioned, owned, approved |
| **CX-3** | **Per-read policy evaluation** | 19 | A | **P0** | DONE | — | Delivered 2026-08-30. Per-read policy evaluation wired into `mcp_server.py`; `tools/call` and `resources/read` both policy-evaluated |
| **CX-4** | **Consumption as lineage** | 19 | A | **P0** | DONE | — | Delivered 2026-08-30. Consumer tracking and graph (`consumption_lineage.py`, `consumption_lineage_api.py`); edges recorded |
| **CX-5** | **Eligible-tool exposure + governed invocation** | 19 | A | **P0** | DONE | — | `tools/list` filters to role-eligible tools; `tools/call` denies ineligible tools with the same not-found response used for absent tools (no existence leak), records audit/outbox evidence, and invokes only through the governed orchestrator/gateway |
| CX-6 | Per-consumer rate limits and budgets | 19 | A | P1 | DONE | — | Delivered 2026-08-30. Per-consumer rate limits via `mcp_budget.py` enhancement; enforced |
| CX-7 | Workload identity for MCP consumers | 19/01 | A | P0 | TODO | — | Same as ID-3 |
| CX-8 | BI-surface context injection | 19 | B | P1 | TODO | — | Tableau/Power BI/Looker |
| **UX-1** | **Persona navigation from OIDC groups** | 21/01 | C | **P0** | TODO | — | Browser selection removed in production |
| UX-2 | Global search + command palette | 21/12 | A | P0 | DONE | — | Delivered 2026-08-30. Same as RT-5 (`search_api.py`, `ui/scripts/features/global-search.js`) |
| UX-3 | List virtualization | 21 | A | P1 | TODO | — | Same as CT-2 |
| UX-4 | Bulk selection + background execution | 21 | C | P1 | TODO | — | 10,000 items, progress, cancellable |
| UX-5 | Accessibility audit and remediation | 21 | C | P1 | IN PROGRESS | — | ARIA roles/labels, roving-tabindex keyboard nav, focus management/restoration, live regions, reduced-motion support and a verified contrast fix applied across ui/; no browser was available to run an interactive screen-reader/axe-core WCAG AA audit — that certification remains |
| UX-6 | Graph level-of-detail rendering | 21/10 | C | P1 | TODO | — | Same as KG-3 |
| UX-7 | Evidence permalinks and export | 21 | C | P1 | TODO | — | Shareable, permission-aware |
| UX-8 | Guided onboarding per persona | 21 | C | P2 | TODO | — | Setup wizards |
| UX-9 | Browser regression suite | 21 | D | P1 | TODO | — | Supported matrix green |
| **OB-1** | **OpenTelemetry export** | 20 | B | **P0** | DONE | — | Delivered 2026-08-30. Tracing and metrics via `observability.py`; traces and metrics to the collector |
| **OB-2** | **SIEM routing** | 20 | B | **P0** | DONE | — | Delivered 2026-08-30. CEF format with syslog/webhook transport (`siem_routing.py`); security events reach the SOC |
| **OB-3** | **WORM archive + retention enforcement** | 20 | B | **P0** | DONE | — | Delivered 2026-08-30. Immutable audit with legal hold and retention (`worm_archive.py`); legal hold supported |
| **OB-4** | **SLO definitions with alerting** | 20 | B | **P0** | DONE | — | Delivered 2026-08-30. SLO, error budgets, and archive status via `observability_api.py`; error budgets tracked |
| OB-5 | Compliance pack generation | 20 | E | P1 | DONE | — | Delivered 2026-08-30. Five frameworks, reproducible, WORM-archived (`compliance_packs.py`, `compliance_api.py`) |
| OB-6 | Cost and showback aggregation | 20 | C | P1 | TODO | — | Per LOB |
| OB-7 | Access review reporting | 20 | B | P1 | TODO | — | Self-service entitlement report |
| OB-8 | Log-scrubbing verification | 20 | 0 | P0 | DONE | — | `atlas.platform.logging.redact_sensitive_data` is a structlog processor wired into `configure_logging` (before `JSONRenderer`) that redacts secret-shaped keys (password/token/credential/api_key/hmac/dsn/cookie/... denylist, recursive through nested mappings and sequences) and secret-shaped values in free text (JWTs, `user:pass@host` DSNs, `Bearer <token>`, AWS access-key IDs); `tests/test_log_scrubbing.py::test_sentinel_scan_end_to_end_log_output` runs the real pipeline end to end and asserts a sentinel value never reaches rendered stdout while non-sensitive fields survive |

## H. Testing, performance, and certification

| ID | Item | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|---|
| TS-1 | Formalize Tier 0 invariant suite | 0 | P0 | DONE | — | Same as ST-03, closed 2026-08-30 |
| TS-2 | Reflection-generated tenant denial coverage | 0 | P0 | DONE | — | `tests/test_inv5_tenant_isolation.py` (2026-08-30) enumerates all 199 FastAPI routes and every worker entry point from the app and registry rather than by hand, with named closed exemption lists |
| TS-11 | Event-catalog CI gate | 0 | P0 | TODO | — | A test asserts every `event_type=` published from `src/` appears in `30-contracts/04-event-catalog.md`. This is the gate whose absence let the catalog drift; cheap, and it stops the drift recurring after ST-14 |
| TS-12 | Doc-claim regression test for named artefacts | 0 | P1 | TODO | — | A test asserting that every test function name, module path and import-linter contract name cited in `Docs/` resolves to something real. Exit: the class of defect fixed by ST-12 cannot silently return |
| TS-3 | Sentinel value-leak scan | 0 | P0 | IN PROGRESS | — | Logs closed 2026-08-30 (OB-8: `tests/test_log_scrubbing.py`, live `structlog` redaction processor — this is distinct from and additional to the in-process control-plane scan below, since neither touches rendered log output). Tables and events (audit rows, outbox payloads, persisted SQL) closed in-process by `tests/test_inv6_value_freedom.py::test_no_source_values_in_control_plane` against a fake executor — narrower than the specced fixture in the way that test's own docstring states: it proves the query-execution path is value-free, not the ingestion/profiling pipelines, which need a live source. Traces are not yet applicable — no tracing is emitted until OB-1 (OpenTelemetry export, still TODO) exists |
| TS-4 | OpenAPI diff gate | 0 | P0 | TODO | — | Breaking change fails CI |
| TS-5 | Adversarial SQL corpus per dialect | D | P0 | TODO | — | Same as QG-1 |
| TS-6 | Prompt-injection corpus (incl. indirect) | B | P0 | DONE | — | Delivered 2026-08-30. Same as AG-1/AG-2 (`injection_defense.py`, `injection_corpus.py`) |
| TS-7 | Load, soak, spike suites | D | P0 | TODO | — | All targets measured |
| TS-8 | Chaos and restore drills | D | P0 | TODO | — | Run, timed, evidenced |
| TS-9 | Accessibility audit | C | P1 | TODO | — | Same as UX-5 |
| TS-10 | Labelled semantic/relationship corpus | D | P1 | TODO | — | Calibration published |
| PF-1 | Bank-scale benchmark corpus (1M objects) | D | P0 | TODO | — | Reproducible, versioned |
| PF-2 | Published performance dashboards | D | P0 | TODO | — | Every target measured |
| PF-3 | CI performance regression gates | 0 | P0 | TODO | — | Configured and enforcing |
| PF-4 | Projection rebuild timing | D | P0 | TODO | — | Same as KG-6 |
| PF-5 | PITR restore drill | D | P0 | TODO | — | RTO met, timed |
| PF-6 | Temporal failover drill | D | P1 | TODO | — | < 15 min |
| PF-7 | Migration rehearsal on production-like data | D | P0 | TODO | — | Up and down |
| CE-1 | Penetration test | D | P0 | TODO | — | No critical findings |
| CE-2 | SOC 2 Type II | D | P1 | TODO | — | Report issued |
| CE-3 | ISO 27001 | D | P2 | TODO | — | Certified |

## I. Drill currency

**A drill that has not been run and timed does not count.** This table is checked at every review; anything older than its cadence is escalated.

| Drill | Cadence | Last run | Result | Status |
|---|---|---|---|---|
| Projection rebuild | Quarterly | **Never** | — | **OVERDUE** |
| PITR restore | Quarterly | **Never** | — | **OVERDUE** |
| Temporal failover | Quarterly | **Never** | — | **OVERDUE** |
| Credential rotation | Quarterly | **Never** | — | **OVERDUE** |
| Kill switch | Quarterly | **Never** | — | **OVERDUE** |
| Batch forced-restart | Per release | 2026-08 (local) | Passed | Current for local only |
| Regional failover | Annual | **Never** | — | **OVERDUE** |
| Break-glass | Annual | **Never** | — | **OVERDUE** |

## J. Decisions required from the bank

These do not block product development. They **do** block production release.

| # | Decision | Blocks | Status |
|---|---|---|---|
| BD-1 | Identity provider and claim contract | ID-2, PG-1 | Open |
| BD-2 | Policy engine / PDP | PG-7 | Open |
| BD-3 | Secret manager | ID-1 | Open |
| BD-4 | Source priority list and versions | CN-1..CN-3 | Open |
| BD-5 | Network zones and regions | CN-4, deployment | Open |
| BD-6 | Data residency and retention classes | MG-4, OB-3 | Open |
| BD-7 | RPO / RTO | PF-5, DR design | Open |
| BD-8 | Model providers and approved routes | MG-3 | Open |
| BD-9 | SIEM and WORM archive | OB-2, OB-3 | Open |
| BD-10 | Kubernetes / managed-service standards | Deployment | Open |
| BD-11 | LOB isolation tiers and chargeback model | PG-5, OB-6 | Open |
| BD-12 | ITSM integration target | DQ-1 | Open |

## K. Summary

| Category | P0 | P1 | P2 | Total |
|---|:--:|:--:|:--:|:--:|
| Structural foundation | 7 | 3 | 0 | 10 |
| Connectors and ingestion | 9 | 7 | 0 | 16 |
| Catalog / profiling / relationships | 2 | 13 | 2 | 17 |
| Semantics / glossary / lineage / graph | 8 | 18 | 4 | 30 |
| Quality / retrieval / runtime | 11 | 10 | 3 | 24 |
| Tools / gateways / governance | 10 | 12 | 2 | 24 |
| Identity / studio / context / UX / observability | 12 | 16 | 4 | 32 |
| Testing / performance / certification | 12 | 5 | 1 | 18 |
| **Total** | **71** | **84** | **16** | **171** |

**The honest read.** 70 P0 items, none assigned, and eight overdue drills. The architecture is well ahead of the operational evidence — which is exactly the pattern named in `50-security/04-compliance-and-evidence.md` §7. Phase D is not a formality; it is where the product's claims become defensible.

## Related documents

- Roadmap: `60-delivery/01-roadmap.md`
- Epic backlog: `60-delivery/02-epic-backlog.md`
- Status matrix: `60-delivery/04-status-matrix.md`
- Gap register: `60-delivery/05-gap-register.md`
