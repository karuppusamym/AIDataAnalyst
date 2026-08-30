# Tracker

> Status: **Living document.** Owner: Engineering lead. Update at every increment.
> The item-level open-work list: one row per work item, from every module spec, the security
> backlog, the test gaps and the 2026-08 review. For the summary answer to "where are we" — the
> capability matrix, invariant status, open gaps and the decisions waiting on a person — read
> `60-delivery/00-status.md` instead. This file is the detail behind it, not a second copy of it.

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

### ID prefixes

Two vocabularies meet in this file, and both are stable identifiers rather than sequence numbers.

| Prefix | Meaning | Defined by |
|---|---|---|
| `ST-`, `CN-`, `IN-`, `CA-`, `PR-`, `RI-`, `SE-`, `GL-`, `LN-`, `KG-`, `DQ-`, `RT-`, `AG-`, `TL-`, `MG-`, `QG-`, `PG-`, `SD-`, `CP-`, `UX-`, `OB-`, `PF-` | Module open-work, matching the owning spec's table in `20-modules/` | The owning module spec |
| `BD-` | A decision required from the bank | §J below |
| `K-`, `C-`, `N-`, `E-`, `D-` | The 2026-08 review's verdict on an existing or proposed capability: **K**eep, **C**orrect, **N**ew, **E**ngineering debt, **D**rop | Re-homed here 2026-08-30 from `review-2026-08/gap/02-gap-diff-and-plan.md`, which stays as the historical plan with the original engineer-week and risk estimates |

A review item and a module item can describe the same work from two directions — `N7` (build an
ABAC engine) and `PG-7` (module 17 needs a policy decision point) are the same thing. Where that
happens the row names both, rather than being listed twice.

---

## A. Structural foundation

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| ST-01 | Target structure + module template | all | 0 | P0 | DONE | — | `scripts/generate_module.py` generates the full anatomy (§7); `identity_tenancy` scaffold generated from it; `platform-is-the-lowest-layer` import-linter contract (ST-02) passes against the generated tree — verified 2026-08-29 (`lint-imports`: 1 kept, 0 broken) |
| ST-02 | Import-linter ratchet in CI | all | 0 | P0 | DONE | — | `.github/workflows/ci.yml` added 2026-08-30 with five gates (ruff, mypy, lint-imports, single-Alembic-head, pytest). Three import-linter contracts pass, including the INV-2 gateway-exclusivity contract (QG-7). Recipe verified end-to-end in a clean checkout via `uv sync --frozen --extra dev`; re-verified 2026-08-30 after the authorization wiring: ruff clean, mypy clean on 120 files, 4 contracts kept, 1 Alembic head, 716 tests passing. Pre-existing ruff (6) and mypy (2) failures were fixed so the gate is green from its first run rather than red on arrival. Broader `aida` layering contracts still land with decomposition |
| ST-03 | Tier 0 invariant suite (9 tests) | all | 0 | P0 | DONE | — | All nine invariants executable in the default `pytest` run with no external service, no skips — verified 2026-08-30 (`ruff check tests` clean; full suite 716 passed, 1 xfailed). `tests/test_tier0_invariants.py` keeps INV-2/3/4/8 plus workspace-level INV-5; INV-1, INV-5 (API surface), INV-6, INV-7 and INV-9 land in `tests/test_inv{1,5,6,7,9}_*.py` on shared harnesses in `tests/support/`. Data-driven throughout: all 199 FastAPI routes enumerated (44 organization-scoped ones driven with a foreign tenant against a session that raises on first use), every connector in the registry, every mapped column, every Cypher statement in `src/aida`. 20 of 20 properties confirmed capable of failing by deliberate mutation. Two strict xfails record codebase gaps, not suite gaps: 13 endpoints in `ai_registry_api`/`product_marketplace_api` commit governed state with no audit row (INV-7), and capability flags are hand-declared rather than derived from certification (INV-9, needs E12). INV-1's live rebuild drill (E5) and INV-6's full ingestion sentinel sweep still need infrastructure; both are proven in-process instead and say so. Detail: `Docs/review-2026-08/gap/06-tier0-invariant-suite.md` One strict xfail remains and it records a codebase gap, not a suite gap: capability flags are hand-declared rather than derived from certification (INV-9, needs E12). The INV-7 xfail was closed 2026-08-30 under ST-17 and is now a passing test with no exemption list, and INV-5's `_TRANSITIVELY_SCOPED_WORKERS` exemption is likewise empty — `plan_profile_tasks` carries an explicit `organization_id` predicate. |
| ST-04 | Extract `platform/` | platform | 0 | P0 | IN PROGRESS | — | `db.py`, `config.py`, `logging.py`, `context.py` moved to `atlas.platform`, each with a re-export shim left at the old `aida.*` path so every existing caller (40+ import sites) is unchanged; `platform-is-the-lowest-layer` passes; full local suite green except 3 pre-existing failures in `test_operational_behaviors.py` unrelated to this change (concurrent WIP on `computed_usage_boost` scheduling, ADR-0017 §8). Not yet moved: `events.py`, `main.py` (still imports nearly every domain router — deferred to Phase 5, the `api.py` router split, rather than moved as-is), and the not-yet-built pagination/idempotency/error-taxonomy/telemetry scaffolding |
| ST-05 | Split `models.py` into module schemas | all | 0 | P0 | TODO | — | No cross-schema FKs except `identity` |
| ST-06 | Split `schemas.py` → `schemas`/`contracts` | all | 0 | P0 | TODO | — | `module-privacy` passes |
| ST-07 | Split `api.py` into routers | all | 0 | P0 | TODO | — | OpenAPI spec byte-identical after split |
| ST-08 | Untangle `intelligence_api.py` | 06/07/09 | 0 | P1 | TODO | — | Each endpoint in its owning module |
| ST-09 | Remove all import-linter exemptions | all | 0 | P1 | TODO | — | Zero exemptions |
| ST-10 | Per-module standalone test jobs | all | 0 | P1 | TODO | — | Each module's tests run alone |
| ST-11 | Resolve `16 query-gateway`'s layer placement and the `09`↔`16` cycle (`10-architecture/04-module-decomposition.md` §5.3) | 09,16 | 0 | P0 | DONE | — | Resolved 2026-08-30 by checking the code rather than redesigning: no cycle exists. The gateway imports no lineage module and no lineage module imports the gateway; `extract_column_lineage` is defined inside `query_gateway.py`. The mutual edge was an error in the module register, now corrected. Rule recorded: the gateway emits, intelligence modules consume |
| ST-12 | Documentation truth pass (`gap/02` C10) | all | 0 | P0 | DONE | — | Applied 2026-08-30. Every structural claim in `00-product/`, `10-architecture/`, `20-modules/`, `30-contracts/`, `40-engineering/`, `90-reference/` and `Docs/README.md` is either true of the code or carries a dated `Implementation status` callout naming the file that proves otherwise. 28 callouts added; `20-modules/00-module-index.md` gained code-sourced `Module dir?` and `Lives today in` columns for all 21 modules. Record and evidence: `Docs/review-2026-08/gap/04-documentation-truth-pass.md` |
| ST-13 | Refresh the review's baseline snapshot against the post-Phase-0 tree | all | 0 | P1 | **DONE** | — | Resolved 2026-08-30 by retirement rather than refresh. The snapshot (formerly `review-2026-08/gap/01-baseline-reality.md`) was a deliberate point-in-time measurement and almost every number in it had moved — CI, contract count, test count, the workspace model. Refreshing it would have produced a second status document; it is now `_superseded/27-review-baseline-reality.md`, and `60-delivery/00-status.md` holds current state |
| ST-14 | Reconcile emitted event names with `30-contracts/04-event-catalog.md` | all | 0 | P1 | TODO | — | Decide per event whether to rename the emitted `.v1` type or restate the catalog row, then land whichever. Exit: every `event_type=` argument in `src/` appears in the catalog and vice versa. Blocked on the U2 authorial question in `gap/04` §3 |
| ST-15 | `edge_kind` vocabulary matches the lineage contract | 09 | 0 | P1 | TODO | — | Code assigns only `SUGGESTED_RELATIONSHIP` (absent from the documented enum) and defaults to `ETL`; `QUERY`/`VIEW`/`PROCEDURE`/`DBT`/`BI`/`AI_DECISION` are never written. Exit: one agreed vocabulary, a DB-level constraint enforcing it, and `30-contracts/06` matching |
| ST-16 | `validate_sql` split and exposure (`gap/02` N14) | 16,19 | 0 | P0 | DONE | — | Landed 2026-08-30. `_run_validation` in `query_gateway.py` is the single pipeline; `validate()` is the no-execute entry point and `execute()` calls the same pipeline, so the two cannot drift. Exposed as MCP native tool `atlas__validate_sql` and `POST /v1/datasources/{id}/sql-validations`. INV-2 preserved (`execute_read_query` still has one call site; `lint-imports` 4 kept). 16 tests in `tests/test_sql_validation.py`. Detail: `Docs/review-2026-08/gap/05-validate-sql-handoff.md` |
| ST-17 | INV-7 audit closeout — 13 unaudited mutations | 17,20 | 0 | P0 | DONE | — | Landed 2026-08-30. All 13 endpoints in `ai_registry_api.py` (6) and `product_marketplace_api.py` (7) call `record_audit(...)` in the same transaction as the mutation they describe, carrying principal, resource, tenant boundary, correlation id and outcome; a 14th audits the remediation-independence denial as `DENIED`. `_KNOWN_UNAUDITED_MUTATIONS` is empty and the strict xfail is gone, so a 14th unaudited endpoint fails on arrival and excusing it fails in the same commit. Ratchet mutation-tested by deleting the `record_audit` in `revoke_marketplace_access`. Detail: `Docs/review-2026-08/gap/09-inv7-audit-closeout.md` |
| ST-18 | Ratify INV-7's meaning of "mutation" for lazily-created default rows | 17 | 0 | P1 | TODO | — | Architecture decides whether "mutation" means "stages a row" or "records an actor's decision", and `10-architecture/01-principles-and-invariants.md` says which. Recommendation and both sides of the trade-off in `review-2026-08/gap/09-inv7-audit-closeout.md` §4: recommend "records an actor's decision", because `ensure_default_domain` and `ensure_organization_integration_policy` build their row from constants plus the tenant id, so naming a creator would manufacture attribution rather than preserve it. Exit: the document says which reading binds; if "stages a row" wins, both helpers return a created/found flag, the 8 GET call sites audit on the creating branch only, and `_LAZY_DEFAULT_WRITE_ROUTES` is deleted |

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
| CT-1 | Bulk actions (tag, classify, own, certify) | 04 | A | P1 | IN PROGRESS | — | Delivered 2026-08-30. Filter-or-explicit-selection with per-item partial-success reporting for tag/classify/own/certify (`catalog_bulk_actions.py`, 4 endpoints under `/v1/organizations/{organization_id}/tables/bulk-*`), 500-item batch cap enforced with a 422 over the limit; own/certify reuse GL-2/GL-5's ownership/certification plumbing. Not yet verified: this repo has no live/fake-DB endpoint-level test harness at all (a pre-existing, systemic gap — confirmed absent for every comparable endpoint, not specific to this item), so the SQL selection/filter paths are verified by code review and mypy/ruff, not by an executed request |
| CT-2 | Million-object virtualization | 04/21 | A | P1 | TODO | — | 1M rows, no lockup |
| CT-3 | Index/partition normalized models | 04 | A | P1 | TODO | — | Populated by ≥2 adapters |
| CT-4 | Rename detection | 04 | C | P2 | TODO | — | Heuristic + steward confirmation |
| CT-5 | Asset certification lifecycle with expiry | 04/08 | A | P1 | DONE | — | `AssetCertification` now covers tables and columns (`asset_type` + nullable `column_id`, `21a56d48976e`); expiry enforced via a shared, tested predicate (`asset_certification.py::asset_certification_is_active`) rather than a trusted `status` column; exposed at module 04's own declared `POST`/`GET /v1/tables/{id}/certification`. Verified against a live local Postgres 16 (full Alembic chain up/down/up, then a real ASGI HTTP run: certify table, certify column independently, re-certify supersedes, and — the exit condition itself — a row reading `status=="ACTIVE"` with a past `expires_at` 404s) |
| CT-6 | Cross-source object resolution | 04 | A | P1 | TODO | — | Same logical asset across sources |
| PR-1 | Composite key inference | 05 | B | P1 | DONE | — | Delivered 2026-08-30. Bounded, evidence-backed candidate composite (and single) key detection from `TableProfile`/`ColumnProfile` statistics, conservatively scored and persisted as review-gated `CompositeKeyCandidate` rows with maker-checker decision (`composite_key_inference.py`, `composite_key_api.py`) |
| PR-2 | Policy-approved range/top-value profiling | 05 | C | P2 | TODO | — | Per-classification approval + retention contract |
| PR-3 | Authoritative classification feed integration | 05 | B | P1 | TODO | — | External feed overrides inference |
| PR-4 | Task-level retry/heartbeat drill-down API | 05 | A | P1 | TODO | — | Operator console shows per-task evidence |
| PR-5 | Continue-as-new at maximum scale | 05 | D | P0 | TODO | — | 1M-table run completes |
| RL-1 | Table family / temporal intelligence | 06 | B | P1 | DONE | — | Delivered 2026-08-30. Snapshot (date/period-suffix siblings), history/audit and delta suffix-pair, and SCD Type 2 (temporal-validity column pairs) detection over naming/column evidence, persisted as review-gated `TableFamilyCandidate` rows with maker-checker decision (`table_family_intelligence.py`, `table_family_api.py`) |
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
| SM-2 | Glossary term binding to semantic objects | 07/08 | A | P0 | DONE | — | Delivered 2026-08-30. `TermSemanticBinding` between `GlossaryTerm` and `SemanticMetric` (`models.py`/`schemas.py`), mirroring `CrossBoundaryGrant`'s maker-checker shape (PENDING_APPROVAL → ACTIVE/REJECTED via the shared governance review queue, self-approval blocked) rather than GL-8's evidence-inference shape, since a binding here is a direct steward assertion. Create/list (both directions)/delete endpoints in `semantic_api.py`. Wired into `retrieval.py::hybrid_retrieve`: an ACTIVE binding folds the bound term's definition/synonyms into the semantic metric's retrievable/rankable text, and a glossary-term hit surfaces its bound semantic objects; a binding that never activated does not participate — proven against a real hybrid_retrieve call over a real in-memory database, not just a DB row check |
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
| GL-6 | Unowned-asset backlog with routing | 08 | A | P1 | IN PROGRESS | — | Coverage returns and UI exposes a bounded backlog. Automated owner routing/escalation delivered 2026-08-30 (`glossary_owner_routing.py`), reusing DQ-1's `notification_routing` engine directly (same `route_notification`/`escalate`/ITSM dispatch, not a fork) against aged unowned entries. Not yet DONE: nothing schedules the routing run automatically — a cron/worker/UI action must call `POST .../unowned-backlog/route`; no default catch-all notification rule ships, so routing is inert until an org creates one; escalation is single-tier (ROUTED → ESCALATED) |
| GL-7 | Leaver reassignment | 08 | C | P2 | TODO | — | Whole portfolio in one action |
| GL-8 | Term linkage inference | 08 | B | P1 | DONE | — | Approved annotation exact-label evidence generates bounded proposals; independent approval creates provenance links |
| **LN-1** | **OpenLineage ingestion** | 09 | A | **P0** | IN PROGRESS | — | Parser, mounted ingest/list/get API (`POST /v1/lineage/openlineage`) and migration exist and produce table/column edges (`openlineage.py`, `openlineage_api.py`); 65 automated tests added 2026-08-30 (`tests/test_openlineage.py`, `tests/test_openlineage_api.py`), which also caught and fixed a `TypeError` that broke every ingest/list/get call; still missing: no Airflow-sourced event has been verified producing real edges |
| **LN-2** | **View and procedure lineage** | 09 | A | **P0** | DONE | — | Delivered 2026-08-30. Multi-dialect SQL lineage parser with CTE support and literal redaction (`sql_lineage_parser.py`, `view_lineage_api.py`); column-level where dialect permits |
| **LN-3** | **AI decision lineage as first-class edges** | 09/13 | E | **P0** | DONE | — | Delivered 2026-08-30. Retrieval, tool selection, rejection, and refusal edges as first-class lineage (`ai_decision_lineage.py`, `ai_decision_lineage_api.py`) |
| LN-4 | BI lineage (Tableau, Power BI, Looker) | 09 | A | P1 | IN PROGRESS | — | Delivered 2026-08-30 for Tableau only. `bi_lineage.py`/`bi_api.py` parse Tableau Metadata API (GraphQL) artifacts into workbook→sheet/dashboard→metric→column edges, resolved down to `MetadataColumn` against the real catalog where possible; formula content is never persisted, only a hash. Power BI and Looker are named and pluggable (`SUPPORTED_BI_TOOLS`) but rejected as not-yet-implemented — minimum-credible-investment per the roadmap, not silently missing. Same systemic no-DB-test-harness gap as CT-1/TL-1 |
| LN-5 | Column-level dbt manifest lineage | 09 | B | P1 | DONE | — | Delivered 2026-08-30. `COLUMN_DEPENDS_ON` edges extracted from each dbt resource's redacted compiled SQL via the existing LN-2 `sql_lineage_parser`, resolved against sibling resources' `relation_name` (`dbt_column_lineage.py`); unresolved/unqualified references dropped rather than guessed. Persisted on `DbtLineageEdge` (migration `25c51ca82a9b`) and surfaced through the existing `GET /dbt-artifact-imports/{id}/lineage` read path; the unified lineage graph query is unaffected (filtered to table-level edges only) |
| LN-6 | dbt `run_results.json` operational evidence | 09 | B | P1 | DONE | — | Parsed and ingested (`dbt_artifacts.py::parse_dbt_run_results`), test status/failures/execution time persisted per resource and reconciled into data-quality incidents (`dbt_quality_bridge.py`); unit-tested; full-endpoint integration test added 2026-08-30 (`tests/test_dbt_run_results_integration.py`) exercising `import_dbt_manifest` end-to-end including idempotency |
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
| **DQ-1** | **Notification and escalation routing** | 11 | A | **P0** | DONE | — | Delivered 2026-08-30. Rules, escalation, dedup, and ITSM routing (`notification_routing.py`, `notification_api.py`). Reused directly by GL-6. Its `notification_rule`/`notification_event` tables shipped as ORM models with no migration creating them (found while integrating GL-6, which depends on `notification_rule`); fixed 2026-08-30 (`f3a8c62d9e17_notification_rule_and_event_tables.py`) |
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
| TL-1 | Tool certification corpus and workflow | 14 | B | P0 | IN PROGRESS | — | Delivered 2026-08-30. Maker-checker certification (`tool_certification.py`, `tool_api.py`): a versioned corpus of deterministic cases runs against a governed tool version's real invocation path (`tool_rendering.render_tool_sql`); any case failure is terminal (`CERTIFICATION_FAILED`), a full pass requires an independent checker's approval before `CERTIFIED`; expiry is query-time filtering (mirrors `AssetCertification`); recertification is a new immutable run, full history preserved. Same systemic no-DB-test-harness gap as CT-1/LN-4 |
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
| **QG-1** | **Adversarial SQL corpus per dialect** | 16 | D | **P0** | DONE | — | Delivered 2026-08-30. 100 structured, versioned adversarial cases across postgres/tsql/oracle/snowflake/bigquery (`tests/fixtures/adversarial_sql_corpus/*.json`), each driven through the real `QueryExecutionGateway.validate()` → `_run_validation` pipeline (ST-16/N14) — no parallel checker. Found and fixed real bypasses in `sql_guard.py` rather than dropping cases: locking reads (`FOR UPDATE`/`FOR SHARE`, T-SQL `UPDLOCK`/`HOLDLOCK`/`XLOCK`/`TABLOCKX` hints) as a contention/timing side-channel; vacuous join conditions (`ON true`, `ON 1=1`) that satisfied "has an ON clause" without relating either side; table-valued sources (T-SQL `OPENQUERY`/`OPENROWSET`, Snowflake/BigQuery `TABLE(...)`) that structurally bypass the catalog allowlist; and a much larger per-dialect forbidden-function/package-prefix denylist (Postgres `dblink`*/`lo_`*/`pg_read_file` family, T-SQL `xp_cmdshell`/`sp_oa*`/linked-server functions, Oracle `DBMS_*`/`UTL_*` package prefixes wholesale rather than enumerated, Snowflake `SYSTEM$*`, BigQuery `EXTERNAL_QUERY`). "Zero bypasses" asserted, not hoped for: any case the pipeline accepts fails the test loudly by name, no skip/xfail path |
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
| **CX-2** | **Context products with maker-checker** | 19 | A | **P0** | DONE | — | Most of this was already built (`context_product_api.py`, `ContextProduct`/`ContextProductVersion`, the `CONTEXT_PRODUCT_VERSION` branch of `decide_governance_review`'s maker≠checker guard, the PUBLISHED-only MCP `resources/read` gate) — the module doc was ahead of this tracker row. Delivered 2026-08-30: the one real gap, explicit `owner_type` (`INDIVIDUAL`/`GROUP`, matching GL-2/GL-6's ownership shape) alongside `owner_principal`, plus direct test coverage the exit condition calls for — DRAFT-only editing creates a new immutable version via `based_on_version_id` without mutating the source version, a context-product-specific self-approval rejection test, and a SQL-predicate assertion that MCP resource resolution only ever matches `status = 'PUBLISHED'`. `info.version` bumped 0.1.0→0.2.0 as the new required field's sanctioned TS-4 acknowledgment |
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
| TS-11 | Event-catalog CI gate | 0 | P0 | DONE | — | Delivered 2026-08-30. `tests/test_event_catalog_gate.py` AST-scans `src/` for every `event_type=` and asserts each is documented in `30-contracts/04-event-catalog.md` or explicitly named in a `KNOWN_ST14_DRIFT` baseline citing the row it collides with (never a silent rename or a guessed duplicate); non-literal `event_type=` sites and catalog rows with no current emitter are reported, not hidden |
| TS-12 | Doc-claim regression test for named artefacts | 0 | P1 | DONE | — | Delivered 2026-08-30. `tests/test_doc_claims.py` checks every backtick-quoted test path/name, `src/aida` module path, and import-linter contract name cited across `Docs/` against the real repo; found and fixed 9 stale "planned, not written" invariant-test claims plus one dead file reference, and carries a self-checking `KNOWN_UNRESOLVED_CONTRACT_CITATIONS` baseline for the remaining aspirational/renamed contract-name citations |
| TS-3 | Sentinel value-leak scan | 0 | P0 | IN PROGRESS | — | Logs closed 2026-08-30 (OB-8: `tests/test_log_scrubbing.py`, live `structlog` redaction processor — this is distinct from and additional to the in-process control-plane scan below, since neither touches rendered log output). Tables and events (audit rows, outbox payloads, persisted SQL) closed in-process by `tests/test_inv6_value_freedom.py::test_no_source_values_in_control_plane` against a fake executor — narrower than the specced fixture in the way that test's own docstring states: it proves the query-execution path is value-free, not the ingestion/profiling pipelines, which need a live source. Traces are not yet applicable — no tracing is emitted until OB-1 (OpenTelemetry export, still TODO) exists |
| TS-4 | OpenAPI diff gate | 0 | P0 | DONE | — | Delivered 2026-08-30. `scripts/openapi_diff.py` classifies breaking vs. non-breaking OpenAPI changes (removed/renamed path, narrowed required/enum/type, removed response field or status code = breaking; additive changes = non-breaking) against a committed baseline (`Docs/90-reference/openapi-baseline.json`), wired as a new `openapi-diff` job in `.github/workflows/ci.yml` alongside the other five gates. A deliberate breaking change is acknowledged via an `info.version` bump plus `--accept-baseline` regeneration, so it is never silent. 24 tests (`tests/test_openapi_diff_gate.py`); baseline regenerated against the merged tip after concurrent API churn (LN-5, TS-11) and confirmed to report zero breaking changes. Not yet observed: a real GitHub Actions run of the new job (verified locally only, not yet exercised on a pushed PR) |
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

## 2026-08 review items

The review's own vocabulary, carried here so the open-work list is one list. Estimates and risk
ratings are the review's, and the reasoning behind each sits in
`review-2026-08/gap/02-gap-diff-and-plan.md` — that file is now the historical plan and is not
maintained as status.

### Correct — existing capability that needs reshaping

| ID | Item | Wks | Pri | Status | Exit |
|---|---|:--:|:--:|:--:|---|
| C1 | Tenancy model → three axes | — | P0 | **DONE** | ADR-0018 accepted; migration steps 1–4 built (`f1a2b3c4d5e6`). Step 5, retiring the old tenancy columns, is deliberately deferred until a repository base class exists (ST-05/06/07) |
| C2 | `legal_entity` | — | P1 | **DONE** | Withdrawn in ADR-0018 rather than deferred; module 01's domain model corrected |
| C3 | Agent runtime's last five states | 2 | P1 | TODO | Five independently-gated checkpoints that can each refuse, replacing one `for` loop applied after the gateway returned |
| C4 | Lineage ↔ gateway cycle (ST-11) | 1 | P0 | **DONE** | No cycle existed in code — it was an error in the module register. The one-directional rule is now an import-linter contract |
| C5 | Data quality as its own module | 2 | P2 | TODO | Baselines fold into profiling; gates become ABAC conditions — a policy rather than a subsystem |
| C6 | Module count: 21 → 16 | — | P2 | TODO | Ships with the decomposition (ST-05 onward). Target map in `review-2026-08/target/05-target-architecture.md` §3 |
| C7 | **Graph store as a configurable port** (was: remove Neo4j) | 2–3 | P1 | TODO | Decided 2026-08-30: keep Neo4j, selectable per organization. Three adapters — `postgres` (default, certified), `neo4j`, `disabled` — copying `vector_store.py`. Exit: (a) the port and the per-organization admin setting; (b) **a conformance suite asserting both backends return identical node sets, ordering, cap behaviour and truncation reasons** — not merely that both return something; (c) Neo4j running in CI, or the backend advertised as uncertified (INV-9); (d) the setting provably cannot reach the authorization path or the classification roll-up (INV-1). ADR-0020 amendment |
| C8 | Defer Kafka | 1 | P2 | TODO | Keep the outbox (the hard part) and the envelope; remove a broker from a bank deployment |
| C9 | Lineage confidence model | 1 | P1 | TODO | Store the derivation *method*, not a single number; policy decides what each method may do |
| C10 | Documentation truth | 2 | P0 | **DONE** | ST-12. 28 dated callouts applied; record in `review-2026-08/gap/04-documentation-truth-pass.md` |

### New — no foundation existed

| ID | Item | Wks | Risk | Status | Note |
|---|---|:--:|:--:|:--:|---|
| N1 | Envelope v1.1 — views + DDL, routines + body, functions, comments, grants | 3 | Low | **DONE** | Shipped across PostgreSQL, Oracle, Snowflake, BigQuery. SQL stored redacted + fingerprinted, never raw (`d5f8b21c4a03`) |
| N2 | View DDL → column-level lineage | 3 | Low | TODO | **Unblocked by N1.** Largest single lineage coverage win; `sqlglot` already in the stack |
| N3 | Procedure body parsing (T-SQL, PL/SQL first) | 8–10 | **High** | TODO | Uncontested in the market and genuinely hard. Must degrade explicitly rather than silently under-report |
| N4 | Lineage proposal / review / negative-knowledge workflow | 5 | Medium | TODO | Diff-based, impact-ordered, bulk decisions |
| N5 | Hybrid retrieval (lexical ∪ vector ∪ graph) | 2 | Medium | IN PROGRESS | **Unblocked 2026-08-30.** Store built (ADR-0019) and the embedding provider chosen and implemented — OpenAI or Gemini, `src/aida/embedding_provider.py`, 12 tests. The fused path no longer feeds hash-derived noise into ranking as a `vector` signal; with no provider configured the stage is skipped and the reason logged. Remaining: embed the catalogue, and run the recall@10 evaluation *after policy filtering* described in `review-2026-08/decisions/02-embedding-model.md` |
| N6 | Workspace primitive + expiring source bindings | — | — | **DONE** | Expiry enforced and tested; binding approval is maker-checker separated |
| N7 | ABAC policy engine | — | — | **DONE, MEASURING** | Wired into execute, validate and five read surfaces via `authorization_gate.py`. Denies nothing — shadow mode. Residency attribute still open |
| N8 | Document ingestion — upload, parse, section, map, claims | 6 | Medium | TODO | Data-dictionary spreadsheets are the highest-value case; build that path first |
| N9 | Business graph | — | — | **DONE** | Recursive CTE traversal, effective-dated assignments, `as_of` history, materialised roll-up |
| N10 | Knowledge compilation — pages, blocks, provenance, staleness, diff proposals | 10–12 | Medium | TODO | The differentiator, the largest new build, and the one nobody else has |
| N11 | Tool generator B — view → tool | 3 | Low | TODO | Views are pre-curated queries; best quality per unit of effort |
| N12 | Tool generator C — procedure → tool | 4 | Medium | TODO | Depends on N3. Eligible only when a parse *proves* the procedure read-only |
| N13 | Federation planner + DuckDB join layer | 8 | **High** | TODO | Must preserve INV-2 — a leaf query per source, each through the gateway |
| N14 | `validate_sql` tool | 2 | Low | **DONE** | Shares `_run_validation` with `execute`, so validation and execution cannot drift |
| N15 | Agent registry + evaluation-gated publication | 5 | Medium | TODO | Makes "production-grade agent" evidenced rather than asserted |
| N16 | Negative knowledge as a context-product section | 2 | Low | TODO | Nearly free — the data is a by-product of review workflows already running |
| N17 | Exemplar store + benchmark suites | 4 | Low | TODO | Accuracy is a curation loop, not a model choice |
| N18 | Ingestion-time prompt-risk screening | 2 | Low | **DONE** | `src/aida/ingest_screening.py`. Screened once at write; flagged text quarantined, not deleted |
| N19 | UI rebuild on a real framework | 12+ | Medium | TODO | The one place "start from scratch" is the right call |

### Engineering debt that blocks "production-grade"

None of this is architecture. A bank's third-party risk process stops at several of these rows.

| ID | Item | Wks | Status | Note |
|---|---|:--:|:--:|---|
| E1 | CI pipeline | 1 | **DONE** | ST-02. Five gates: ruff, mypy, lint-imports, single-head, pytest |
| E2 | Import-linter gateway exclusivity (QG-7) | 0.5 | **DONE** | Converted INV-2 from convention to proof |
| E3 | Import-linter module boundary contracts | 2 | TODO | Ships with the restructure (ST-05 onward) |
| E4 | Tier-0 invariant suite | 4 | **DONE** | All nine have tests. Three carry named limits — `00-status.md` §3 |
| E5 | Projection rebuild drill | 1 | TODO | Never run. INV-1's remaining limit. **Promoted to a prerequisite of C7** — a projection never proven rebuildable should not be offered as a selectable backend |
| E6 | PITR restore drill | 1 | TODO | Never run |
| E7 | Temporal failover drill | 1 | TODO | Never run |
| E8 | Credential rotation drill | 1 | TODO | Never run |
| E9 | Kill-switch drill | 0.5 | TODO | Never run — and the AI-safety argument depends on it |
| E10 | Load / soak at 1M objects | 3 | TODO | p95 targets are published and unmeasured |
| E11 | Penetration test | ext. | TODO | Not run |
| E12 | Connector + lineage-parser certification corpus | 3 | TODO | INV-9's one remaining strict xfail is unverifiable without it |
| E13 | Repo hygiene — `scratch/` in git history | 0.5 | **READY** | Measured: 19 tracked files, 7.9 MB, 9 tarballs, 22 MB `.git`. Touches shared history, so it needs a decision — `00-status.md` §6 decision 6 |

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
- Delivery status (the summary this file details): `60-delivery/00-status.md`
- Accomplishment log: `60-delivery/06-accomplishment-log.md`
- The 2026-08 review's original plan and estimates: `review-2026-08/gap/02-gap-diff-and-plan.md`
