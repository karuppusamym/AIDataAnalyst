# Tracker

> Status: **Living document.** Owner: Engineering lead. Update at every increment.
> This is the single place to answer "what is the state of everything." It consolidates the open-work IDs from every module spec, the security backlog, and the test gaps.

**Last reviewed:** 2026-08-28

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
| ST-01 | Target structure + module template | all | 0 | P0 | TODO | — | Generated module passes all contracts |
| ST-02 | Import-linter ratchet in CI | all | 0 | P0 | TODO | — | New violations fail CI |
| ST-03 | Tier 0 invariant suite (9 tests) | all | 0 | P0 | TODO | — | All nine exist, pass, unskippable |
| ST-04 | Extract `platform/` | platform | 0 | P0 | TODO | — | `platform-purity` passes |
| ST-05 | Split `models.py` into module schemas | all | 0 | P0 | TODO | — | No cross-schema FKs except `identity` |
| ST-06 | Split `schemas.py` → `schemas`/`contracts` | all | 0 | P0 | TODO | — | `module-privacy` passes |
| ST-07 | Split `api.py` into routers | all | 0 | P0 | TODO | — | OpenAPI spec byte-identical after split |
| ST-08 | Untangle `intelligence_api.py` | 06/07/09 | 0 | P1 | TODO | — | Each endpoint in its owning module |
| ST-09 | Remove all import-linter exemptions | all | 0 | P1 | TODO | — | Zero exemptions |
| ST-10 | Per-module standalone test jobs | all | 0 | P1 | TODO | — | Each module's tests run alone |

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
| CN-8 | Index and partition extraction | 02/04 | A | P1 | TODO | — | Normalized models populated |
| IN-1 | Bulk source onboarding | 03 | A | P0 | TODO | — | 200 sources onboarded in one operation |
| IN-2 | Batch pause/cancel/replay controls | 03 | A | P1 | TODO | — | Operator console actions with audit |
| IN-3 | Kafka intake + schema registry | 03 | B | P1 | TODO | — | Same envelope, same persistence path |
| IN-4 | Signed workload producer identity | 03 | B | P1 | TODO | — | Unsigned producer rejected |
| IN-5 | Envelope 1.1+ (BI, pipeline, topic, file, ML) | 03 | B | P1 | TODO | — | Backward-compatible; consumers unaffected |
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
| **LN-2** | **View and procedure lineage** | 09 | A | **P0** | TODO | — | Column-level where dialect permits |
| **LN-3** | **AI decision lineage as first-class edges** | 09/13 | E | **P0** | TODO | — | Includes rejections and refusals |
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
| **DQ-1** | **Notification and escalation routing** | 11 | A | **P0** | TODO | — | Owner routing + ITSM webhook |
| **DQ-2** | **Approved watermark contracts → freshness** | 11 | A | **P0** | TODO | — | Freshness activates per configured table |
| **DQ-3** | **Quality → runtime coupling** | 11/12/13/14 | E | **P1** | TODO | — | Demotion, warnings, tool gating all live |
| DQ-4 | Custom rule packs and scheduling | 11 | B | P1 | TODO | — | Rules run outside scans |
| DQ-5 | Data SLA/SLO definitions | 11 | A | P1 | TODO | — | Breach raises an incident |
| DQ-6 | Seasonality-aware thresholds | 11 | E | P2 | TODO | — | Reduced false positives, measured |
| DQ-7 | Bank-scale incident-volume certification | 11 | D | P1 | TODO | — | No alert fatigue at target volume |
| DQ-8 | Open quality framework for third-party detectors | 11 | E | P2 | TODO | — | Monte Carlo / Anomalo signals ingested |
| **RT-1** | **Vector projection and similarity retrieval** | 12 | A | **P0** | TODO | — | pgvector, rebuildable |
| **RT-2** | **Graph expansion from seed hits** | 12 | A | **P0** | TODO | — | Bounded, policy-filtered |
| **RT-3** | **Fusion ranking with inspectable factors** | 12 | A | **P0** | TODO | — | Every factor in the evidence |
| RT-4 | PostgreSQL full-text index | 12 | A | P0 | TODO | — | Lexical p95 within budget |
| RT-5 | Global search + command palette | 12/21 | A | P0 | TODO | — | Ctrl/Cmd+K, keyboard-navigable |
| RT-6 | Usage/popularity signal | 12 | B | P1 | TODO | — | Ranking factor from execution history |
| RT-7 | Quality trust factor in ranking | 12/11 | E | P1 | TODO | — | Part of DQ-3 |
| RT-8 | Large-catalog retrieval benchmarks | 12 | D | P0 | TODO | — | 1M objects, < 1 s first paint |
| RT-9 | Cross-source search | 12 | A | P0 | TODO | — | One query spans sources |
| **AG-1** | **Indirect-injection defence** | 13 | B | **P0** | TODO | — | Corpus of malicious descriptions: zero bypasses |
| **AG-2** | **Multilingual and obfuscation coverage** | 13 | B | **P0** | TODO | — | Corpus: zero bypasses |
| **AG-3** | **Bank model-risk evaluation corpus** | 13/15 | B | **P0** | TODO | — | Published accuracy and refusal results |
| AG-4 | Multi-step tool plans with budgets | 13/14 | E | P1 | TODO | — | Step/time/token/cost enforced |
| AG-5 | AI decision lineage emission | 13/09 | E | P0 | TODO | — | Same as LN-3 |
| AG-6 | Quality trust warnings on answers | 13/11 | E | P1 | TODO | — | Part of DQ-3 |
| AG-7 | Query memory similarity + safe adaptation | 13 | C | P1 | TODO | — | Version-aware; never bypasses validation |
| AG-8 | Retrieval and model benchmarks | 13 | D | P0 | TODO | — | Published |

## F. Tools, model gateway, query gateway, governance

| ID | Item | Mod | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| TL-1 | Tool certification corpus and workflow | 14 | B | P0 | TODO | — | Certification with expiry and recertification |
| TL-2 | Multi-tool plans | 14 | E | P1 | TODO | — | Same as AG-4 |
| TL-3 | Quality gating of tool invocation | 14/11 | E | P1 | TODO | — | Part of DQ-3 |
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
| QG-7 | Gateway-exclusivity import contract | 16 | 0 | P0 | TODO | — | INV-2 mechanically enforced |
| **PG-1** | **ABAC (classification, purpose, residency)** | 17 | B | **P0** | TODO | — | Decision p95 ≤ 50 ms |
| **PG-2** | **Agent-vs-human context attribute** | 17 | B | **P0** | TODO | — | Distinguishable in policy |
| PG-3 | Bulk decisions with per-item rationale | 17 | C | P0 | TODO | — | 10,000-item selection workable |
| PG-4 | Delegation and reassignment | 17 | C | P1 | TODO | — | Time-bounded, audited |
| PG-5 | Entitlement evaluation for editions | 17 | C | P1 | TODO | — | Edition gates capability |
| PG-6 | Full policy decision logging | 17 | B | P0 | TODO | — | Inputs and outcome, auditor-readable |
| PG-7 | External PDP (OPA / bank PDP) adapter | 17 | B | P1 | TODO | — | Certified against the bank bundle |
| PG-8 | Policy simulation | 17 | E | P2 | TODO | — | "Who could see this?" |

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
| ST-A1 | Studio change sets | 18 | C | P1 | TODO | — | Conflict detection vs. base version |
| ST-A2 | Studio test harness | 18 | C | P1 | TODO | — | Synthetic fixtures; gate on submission |
| ST-A3 | Semantic diff view | 18 | C | P1 | TODO | — | Same as SM-7 |
| ST-A4 | Parameter-contract designer | 18 | C | P1 | TODO | — | Typed, enum-bound |
| ST-A5 | Impact preview at submission | 18/09 | C | P1 | TODO | — | Blast radius shown |
| ST-A6 | Git binding (Atlas authoritative) | 18 | E | P2 | TODO | — | Merge cannot bypass maker-checker |
| ST-A7 | Context product builder | 18/19 | A | P1 | TODO | — | Authoring surface for module 19 |
| **CX-1** | **MCP server** | 19 | A | **P0** | IN PROGRESS | — | Real JSON-RPC 2.0 endpoint (`initialize`/`ping`/`tools`/`resources`) mounted at `POST /mcp`; `tools/call` routes through the full governed orchestrator/gateway stack — resource reads are not yet per-read policy-evaluated (see CX-3) |
| **CX-2** | **Context products with maker-checker** | 19 | A | **P0** | TODO | — | Versioned, owned, approved |
| **CX-3** | **Per-read policy evaluation** | 19 | A | **P0** | IN PROGRESS | — | `tools/call` is fully policy-evaluated (role-eligibility + governed gateway); `resources/read` still bypasses per-read evaluation |
| **CX-4** | **Consumption as lineage** | 19 | A | **P0** | TODO | — | Edges recorded |
| **CX-5** | **Eligible-tool exposure + governed invocation** | 19 | A | **P0** | DONE | — | `tools/list` filters to role-eligible tools; `tools/call` denies ineligible tools with the same not-found response used for absent tools (no existence leak), records audit/outbox evidence, and invokes only through the governed orchestrator/gateway |
| CX-6 | Per-consumer rate limits and budgets | 19 | A | P1 | TODO | — | Enforced |
| CX-7 | Workload identity for MCP consumers | 19/01 | A | P0 | TODO | — | Same as ID-3 |
| CX-8 | BI-surface context injection | 19 | B | P1 | TODO | — | Tableau/Power BI/Looker |
| **UX-1** | **Persona navigation from OIDC groups** | 21/01 | C | **P0** | TODO | — | Browser selection removed in production |
| UX-2 | Global search + command palette | 21/12 | A | P0 | TODO | — | Same as RT-5 |
| UX-3 | List virtualization | 21 | A | P1 | TODO | — | Same as CT-2 |
| UX-4 | Bulk selection + background execution | 21 | C | P1 | TODO | — | 10,000 items, progress, cancellable |
| UX-5 | Accessibility audit and remediation | 21 | C | P1 | IN PROGRESS | — | ARIA roles/labels, roving-tabindex keyboard nav, focus management/restoration, live regions, reduced-motion support and a verified contrast fix applied across ui/; no browser was available to run an interactive screen-reader/axe-core WCAG AA audit — that certification remains |
| UX-6 | Graph level-of-detail rendering | 21/10 | C | P1 | TODO | — | Same as KG-3 |
| UX-7 | Evidence permalinks and export | 21 | C | P1 | TODO | — | Shareable, permission-aware |
| UX-8 | Guided onboarding per persona | 21 | C | P2 | TODO | — | Setup wizards |
| UX-9 | Browser regression suite | 21 | D | P1 | TODO | — | Supported matrix green |
| **OB-1** | **OpenTelemetry export** | 20 | B | **P0** | TODO | — | Traces and metrics to the collector |
| **OB-2** | **SIEM routing** | 20 | B | **P0** | TODO | — | Security events reach the SOC |
| **OB-3** | **WORM archive + retention enforcement** | 20 | B | **P0** | TODO | — | Immutable; legal hold supported |
| **OB-4** | **SLO definitions with alerting** | 20 | B | **P0** | TODO | — | Error budgets tracked |
| OB-5 | Compliance pack generation | 20 | E | P1 | TODO | — | Reproducible; WORM-archived |
| OB-6 | Cost and showback aggregation | 20 | C | P1 | TODO | — | Per LOB |
| OB-7 | Access review reporting | 20 | B | P1 | TODO | — | Self-service entitlement report |
| OB-8 | Log-scrubbing verification | 20 | 0 | P0 | TODO | — | Sentinel scan passes |

## H. Testing, performance, and certification

| ID | Item | Ph | Pri | Status | Owner | Exit |
|---|---|:--:|:--:|:--:|:--:|---|
| TS-1 | Formalize Tier 0 invariant suite | 0 | P0 | TODO | — | Same as ST-03 |
| TS-2 | Reflection-generated tenant denial coverage | 0 | P0 | TODO | — | Every endpoint and worker |
| TS-3 | Sentinel value-leak scan | 0 | P0 | TODO | — | Tables, logs, events, traces |
| TS-4 | OpenAPI diff gate | 0 | P0 | TODO | — | Breaking change fails CI |
| TS-5 | Adversarial SQL corpus per dialect | D | P0 | TODO | — | Same as QG-1 |
| TS-6 | Prompt-injection corpus (incl. indirect) | B | P0 | TODO | — | Same as AG-1/AG-2 |
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
