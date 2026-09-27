# Product, implementation and deployment review — 2026-09-16

> **Follow-up items 13–17:** see [GraphQL, OKF and workspace design](../10-architecture/21-graphql-okf-and-workspace-design.md) for the restarted review, current implementation distinctions, API/UI acceptance slices and [visual wireframe](items-13-17-layout.svg). The Ask layout fix is implemented; GraphQL/OKF contracts and further consolidation are designs, not delivered endpoints.

Reviewed source: `bf9b909fc1c1703098657cf7f70fe2e931317bbf`.

**Concurrent implementation note:** the full suite started on that revision. During the review the parallel implementation committed `3d553b5` (retrieval benchmark isolation) and then `723c487` (generated-SQL product scope). The combined benchmark/product-scope focused suites passed 26 tests after those changes. Findings below describe the audited baseline, with this later work called out explicitly. No full-suite pass on the final revision is claimed.

Related authorities: [footprint design](../10-architecture/20-database-footprint-and-agent-context.md), [delivery tracker](../60-delivery/03-tracker.md), [capability register](../60-delivery/20-capability-register.md). This report is a dated review, not a replacement status tracker.

## Verdict

**Continue with the existing vision and architecture. Prioritize completion and deployment verification; do not start a broader rewrite or add database candidates.** The product has substantial implemented functionality, shared execution/governance boundaries, and meaningful regression coverage. It is not accurate to call the entire planned database-context product complete, or to treat the currently running containers as the reviewed source.

The immediate work is to finish context-product scope enforcement and its Ask UI, align deployment and migrations, configure the existing maintenance loop, and prove a current-source user journey. Navigation consolidation is worthwhile after those correctness gaps. Native routine invocation and the larger infrastructure/storage consolidations should remain deferred.

This review changes documentation only. It does not migrate the configured database, rebuild running containers, enable workers, change authorization modes, or modify application code being developed in parallel.

## 1. Findings requiring action

### F01 — Product scope does not constrain SQL execution (high)

**Evidence:** `src/aida/agent_orchestrator.py`, `ContextProductScope` and retrieval at approximately lines 1084–1135, filters retrieval by a published product. `src/aida/query_gateway.py::allowed_tables` builds an allowlist from all ACTIVE tables in the datasource. `_run_validation` uses that list. The orchestrator's `_checkpoint_validated` independently uses the same datasource-wide list. The product scope is not passed to either check.

**Consequence:** an answer asked through a curated product can generate SQL referring to a table outside that product if the table is otherwise available within the datasource. A filtered prompt is insufficient to enforce the product's execution scope. This is a product-boundary gap; this review does not establish a cross-tenant authorization bypass.

**Fix:** carry the resolved published product version and its executable scope through planning and gateway validation. Intersect product scope with existing caller/datasource authorization before any connector execution. Define how an eligible governed tool's approved dependencies fit that scope; do not silently widen it. Preserve the version receipt through the answer.

**Acceptance:** with tables A and B otherwise authorized, a product containing A rejects generated or submitted SQL reading B before opening an execution session. Cover joins, qualified names, CTEs, tool selection, direct SQL and model-generated SQL. Product-free requests retain their existing behavior.

**Late implementation update — `723c487`:** the parallel worker added an orchestrator check before execution, carrying the product scope through `RetrievalOutcome` and refusing resolved table IDs outside it. Explicitly eligible governed tools are exempt under that patch's stated contract. The focused scope suite passed as part of the 26-test rerun. Mark the basic generated-SQL defect addressed in source, with broader path/alias/unresolved-reference acceptance and deployed verification still open; the gateway itself remains datasource-scoped. Do not reimplement the same check simply because the baseline finding remains documented here.

### F02 — Routine evidence escapes product selection (high)

**Evidence:** `ContextProductScope` contains `table_ids` and `tool_version_ids`, but no `routine_ids`. Its `admits()` returns true when an unhandled hit has no `table_id`. `src/aida/retrieval.py` emits ROUTINE hits containing `routine_id`, `reads_table_ids` and `writes_table_ids`, without a singular `table_id`.

A direct, read-only reproduction against current source constructed a product scope and unrelated hits:

```text
TABLE outside product: admitted=False
ROUTINE outside product: admitted=True
ONTOLOGY_CONCEPT outside product: admitted=True
```

**Fix:** constrain routine hits to the product's routine references, including lexical/vector paths and graph-derived evidence. Apply consistent dependency disclosure rules to the evidence returned to the agent. Add explicit tests for a product with no routines and one selecting only a single routine.

**Related design decision:** semantic candidates are intentionally left unrestricted by the current `admits()` implementation. Specify whether an answer must use the product's pinned ontology, glossary and semantic-model versions or may use additional authorized current meaning. The compiler's pinning and Ask's answer semantics should agree. Treat this as an explicit contract decision, not as an inferred tenant leak.

### F03 — Running deployment is behind source and schema (high for release)

Read-only inspection found:

| Surface | Reviewed source | Running/configured environment |
|---|---|---|
| OpenAPI paths | 405 | 404 |
| Footprint gap details | `/v1/datasources/{datasource_id}/footprint-gaps/{kind}` present | Absent from live OpenAPI |
| Ask request | `context_product_key` present | Absent from live `AgentAnalysisRequest` |
| Alembic revision | Head `f4a8c1d7e236` | Current `b7e2d9c4f158` |
| API/UI code delivery | Workspace source | Images created September 15, without source bind mounts |

The missing migration records which called routine a lineage edge was read through. Both live health endpoints return 200, but liveness/readiness do not prove revision parity.

**Action:** package the reviewed commit, apply the documented migration procedure, deploy matching API/workers/scheduler/UI, and verify revision/API parity. Perform backup/rollback preparation appropriate to the target environment. This review intentionally did not perform deployment.

**Acceptance:** source and deployed API contracts match; Alembic current equals head; the new gap detail and product-scoped Ask UI work against the real API, not only fixtures.

### F04 — Automatic context maintenance is implemented but disabled here (high for the product promise)

`src/aida/workflows/scheduler.py::run_context_rebuild_pass` is called from the scheduler iteration and drafts affected artifacts for review. It is not missing wiring. Inspection of the running fleet-scheduler settings returned:

```text
context_rebuild_interval_minutes = 0
change_signal_processing_interval_minutes = 0
lineage_agent_interval_minutes = 0
```

**Consequence:** this deployment does not continuously run those passes. Discovery/rescans must also occur before a new source definition can be observed. Execution holds compare against captured metadata; they do not inspect the external database's current definition on every call.

**Action:** configure discovery cadence, change-signal processing, lineage investigation and context rebuild in the intended environment. Set queue-age/freshness thresholds and connect the existing footprint metrics to monitoring. Keep changed artifacts in review; enabling scheduling must not imply automatic publication.

**Acceptance:** change a sample view; a scheduled scan captures the change; affected artifacts are held or flagged; one replacement draft is proposed; approval releases the appropriate hold; unrelated artifacts remain unchanged; backlog and last-success age are visible.

### F05 — Development posture must not be described as production enforcement/delivery (high for release)

Live `/health/ready` reports:

- Workspace authorization `OBSERVING`; one workspace observing, zero enforcing; unresolved scope `PROCEEDS_UNDECIDED`.
- Notification delivery worker disabled; nine queued notifications; oldest age approximately 3.7 days at inspection.
- PostgreSQL and Temporal UP; zero pending outbox entries.

These are distinct controls. Observation mode is explicitly supported by `authorization_posture.py`; it records workspace decisions without enforcing denials. This does not mean all authentication and authorization are absent. A healthy dependency endpoint also does not demonstrate delivered notifications.

**Action:** follow the existing workspace migration procedure before claiming enforcement, verify positive and negative access cases, and enable/test the chosen delivery destination and worker before claiming notifications work. No configuration was changed during this review.

### F06 — Important scope remains partial (medium; completion work)

1. **Routine descriptions (FP08):** tables/views have the description lifecycle, but routines lack an Atlas-authored, versioned description store/review path. Resolve the small architecture decision to extend the existing description family; do not create a parallel governance engine. This is core to the requested product.
2. **Profiles and samples (FP04):** governed row access awaits policy. Separate value-free aggregate profiling from row samples so the whole task is not unnecessarily blocked. Preserve redaction and access boundaries.
3. **Discovery coverage (FP01–03):** six adapters share selection handling, but source-side filtering and definition facets vary. Packages, native routine varieties, permission-limited bodies, history browsing and push-ingestion selection are not uniformly complete.
4. **Code understanding (FP05–07):** temp-table/statement analysis and called-routine lineage exist; dynamic SQL, unresolved references, dbt macros/hooks and precise source mapping still need bounded coverage reporting. Never label every path understood merely because an object was inventoried.
5. **Answer evaluation (FP13):** current retrieval improvements and governed-tool journeys do not establish correctness of model-generated answers using the enriched footprint. Run that specific evaluation with expected answers, evidence, refusals and token/cost results.
6. **Operations (FP17):** metrics exist; deployed scraping/alerts, parser/model cost metrics, tenant/source quotas and change-burst latency evidence remain incomplete.

### F07 — Completion documents have drifted (medium)

The tracker accumulates dated implementation paragraphs alongside older remaining-work statements. Examples include FP01's old PACKAGE-selection remainder, FP09's old lack of product-scoped retrieval, FP11's old table-only product claim and FP16's statement that nothing schedules passes. Current code has moved beyond those statements, although deployment can still have the corresponding feature disabled.

The capability register's September 12 reviewer row still describes C3 as PARTIAL. The current tracker records the September 13 product decision: unattended review stays disabled in production, and the production configuration rejects enabling it. **Do not reopen or claim completion by enabling unattended approval.**

**Action:** normalize each row into implemented / wired / configured / verified / remaining, with one current remainder and links to historical evidence. Use the existing capability register rather than inventing another competing status authority. FP task numbers describe broad acceptance criteria; small successful subfeatures do not close the whole task.

### F08 — Product-scoped Ask has no UI request wiring (high for the intended journey)

Repository-wide search finds `context_product_key` in the generated frontend request type only. `ui-next/src/screens/AskScreen.tsx` sends the question and, during clarification, tool parameters/preferred tool version; it does not send a context product. The current Context Products screen also has no Ask-through-this-product action.

**Consequence:** adding the API field did not complete the UI journey. Deploying the latest source alone will not allow a normal UI user to ask through a selected product.

**Fix:** provide a published-product selection or an Ask entry from the product, preserve the product across clarification/retry/navigation, send its key, and show the resolved product version with the answer evidence. Scope the available products to the caller and datasource/project. Handle unavailable/forbidden product errors explicitly.

**Acceptance:** a UI test verifies the selected product key in the outgoing request and clarification resubmission; a real-backend journey verifies the resolved version and out-of-product rejection. A generated TypeScript field alone is not completion evidence.

### F09 — Full-suite retrieval calibration failure; correction landed during review (medium)

The full suite failed `tests/test_quality_benchmark_gate.py::test_retrieval_benchmark_resolves_named_cases_as_calibrated`: the `customer-lookup-tool-outranks` case returned rank 3 while the test asserted rank 2. Captured evidence showed the live embedding path running. The benchmark read local settings, making an exact rank depend on whether an embedding provider was configured.

The parallel implementation committed `3d553b5`: the test fixture disables the embedding provider, clears cached settings and asserts the intended bounded ordering instead of an exact seat. The single failing test then passed in 3.02 seconds. This resolves the reproduced calibration failure in that focused rerun; it does not retroactively turn the original full run green. Keep a separate explicit live-vector quality evaluation rather than silently treating deterministic lexical coverage as vector-quality evidence.

## 2. What is built correctly, and what remains to prove

| Product stage | Current implementation evidence | Remaining boundary |
|---|---|---|
| Discover the footprint | Native adapters, inventory envelope, discovery selection and preview, source UI, explicit availability states | Per-engine certification and complete facet/filter parity |
| Understand code | View definitions, routine identities/parameters, ordered procedural analysis, temp datasets, called-routine lineage and review | Runtime-generated SQL, macros/external code, source maps and permission gaps remain explicit unknowns |
| Explain the data model | Table/view descriptions, relationship proposals and validation, glossary/ontology mappings | Routine descriptions and governed profiling/sampling are unfinished |
| Form ontology | Versioned ontology lifecycle and typed asset mappings; concepts participate in retrieval | Domain concepts evolve through proposals/review; inference is not automatically published truth |
| Publish reusable context | Versioned context products, routine references, compiled artifacts, REST/MCP reads and freshness reporting | Consistent Ask scope, pinned-meaning semantics and UI request wiring: F01/F02/F08 |
| Expose tools | Governed view/query blueprints, routine-derived query candidates, gateway checks and approval lifecycle | Extracted SELECT is not native procedure/function invocation; FP18 remains deferred |
| Maintain context | Change signals, source bindings, holds, targeted draft rebuilds, product re-pinning and scheduler hooks | Enable and observe the deployed loop; rescan cadence determines source freshness |
| Operate the product | Operations gaps, queue visibility, capability reporting, metrics, audit and model-route checks | Actual delivery, monitoring integration, target-scale and customer-environment evidence |

The ontology is **a stable technical vocabulary with evolving, versioned domain content**. Object kinds and allowed relationship/mapping forms give agents a consistent structure; business concepts, definitions and asset bindings can change through the governed lifecycle. It should not become a free-form, self-publishing LLM memory. Each usable claim needs source evidence, review state, version, scope and freshness.

This improves context in four concrete ways: agents can find logic hidden in views/routines; relationships and meaning narrow ambiguous questions; published versions provide a reproducible basis for answers; source changes make stale context visible and produce targeted replacement drafts. F01/F02 and deployed scheduling are necessary to make those guarantees consistent end to end.

### Previously reported defects that have been addressed in source

- Source-bound tools compare captured definition fingerprints at approval and execution (`tool_source_binding.py`), rather than treating approval time as evidence of definition freshness. New versions cannot silently remove/repoint the binding.
- Relationship validation checks each side's metadata authorization and applicable cross-boundary grant (`relationship_validation_api.py`).
- SQL defenses reject unknown routine calls, declared source routine names and sequence advancement. These supplement the existing gateway boundary.
- View descriptions account for captured/truncated/withheld/quarantined definitions and refuse approval after the definition moves.

These repairs should not be reopened as unchanged historical bugs. Current deployment parity and relevant regression evidence still matter.

## 3. Screens and features worth consolidating

Consolidate user journeys and reuse existing components; do not merge different permission or publication contracts.

| Current surfaces | Recommendation | Preserve |
|---|---|---|
| Lineage and Unified lineage | One Lineage destination with Explain, Graph and Impact views | Narration, graph exploration, provenance, impact traversal and old deep links |
| Relationships and Cross-source | One relationship workspace with local/cross-source views | Separate cross-boundary grant decisions and per-side authorization |
| Documentation worklist, Description drafts, Data dictionaries | One documentation workspace with Priorities, Drafts and Imports tabs | Imported claims, authored drafts and priority rankings are different operations |
| OntologyManager currently opened from Unified lineage | Make Business Meaning the primary authoring entry; retain a contextual graph shortcut | Ontology versioning, publish/review permissions and graph context |
| Datasource registration in Administration; source management in Sources | Reuse the registration form from Sources to complete that journey | Organization/project administration stays in Administration |
| OrgPicker versus active ScopePicker | Remove the documented dead OrgPicker after confirming no external use | ScopePicker and its selection behavior |

`CrossBoundaryGrants` is already a shared component reused in two screens, not duplicate backend functionality. Contextual entry points can remain. Task-agent consoles and parsed-lineage review have already been consolidated; preserve that work.

Do **not** collapse semantic metrics, business ontology, context products and governed tools into one feature. They respectively define calculations, meaning, a consumable approved bundle and an executable operation. Connect their navigation and provenance instead.

Acceptance for any navigation merge: existing URLs redirect or retain view state; caller scope survives navigation; keyboard/accessibility behavior remains usable; all prior actions remain reachable; role-specific permissions and regression tests remain intact. No broad module relocation is necessary.

## 4. Planned completion and parked work

The current execution queue has **102 unique R11 rows: 49 DONE, 20 PARTIAL, 8 BLOCKED, 23 DEFERRED and 2 CANCELLED**. These are administrative task counts, not a product-completion percentage. The footprint program itself has 16 PARTIAL rows, FP04 BLOCKED and FP18 DEFERRED. Thus “everything planned is complete” is false even though many constituent features are implemented.

### Deferred/cancelled disposition — all current rows

| Tasks | Disposition |
|---|---|
| B16 incremental discovery | Keep deferred until a selected connector and measured full-scan cost justify it. Incremental context rebuilding is separate and should proceed now. |
| S1 infrastructure, S5 storage, S8 scheduler consolidation | Keep deferred. Configure/test the existing services before replacing them. |
| S2 agent governance consolidation | Limit to coherent navigation and shared controls; preserve registered-agent contracts and budgets. |
| S3 retrieval simplification | Keep architectural replacement deferred; fix scope first and measure retrieval quality before removing channels. |
| S4 governed-artifact consolidation | Keep broad rewrite deferred; make the narrow decision needed for routine descriptions. |
| S7 compliance integrations | Wait for the selected customer/provider integration. |
| S13 remaining scope/review surfaces | Proceed narrowly with the navigation plan above after correctness fixes. |
| S11 optional connector drivers | Keep deferred unless packaging cost or a selected deployment requires it. |
| S12 large modules, P4 repeated fixtures, P6 shim callers, P10 historical comments | Improve when touching the relevant code. P6 should not depend on the cancelled relocation program. |
| P1 OpenAPI baseline churn, P2 build-time type generation, P3 documentation gates | Keep deferred; current compatibility/type/documentation gates are useful. |
| P5 migration squash | Keep deferred; do not reset migration history to solve deployment drift. |
| P11 CI setup/duplicate runs | Low-priority measurable efficiency work; preserve independent coverage. |
| P12 tracked artifacts, P13 caches/scratch data | Keep deferred; no bulk cleanup is needed for product completion. |
| C12 customer-driven expansion | Keep parked. No additional database candidates in this review. |
| FP18 native routine invocation | Keep deferred pending explicit adoption of an invocation/effects/result-set contract. Query extraction remains useful without it. |
| S6 relocate feature packages; X7 remove registered-agent branch/budgets | Keep CANCELLED. Do not resurrect either as cleanup. |

### Blocked rows — distinguish engineering from external acceptance

- **B5:** selected customer connector/version/TLS/account certification.
- **B6:** corporate identity issuer and secrets manager verification.
- **B15:** target capacity, topology and restore/RPO/RTO evidence.
- **C9:** release security and break-glass evidence in the target environment.
- **C10:** relationship-confidence calibration with labeled customer-relevant evidence.
- **C11:** model residency, usage and monitoring decisions/certification.
- **C13:** selected licensed dynamic-masking provider.
- **FP04:** governed sample-access policy; proceed independently with permitted aggregate-profile work.

Additional PARTIAL rows are B9 (real archive/export evidence), B10 (delivery and scheduler health), I1 (real Teams/Slack tenant delivery), and C2 (manual accessibility and visual acceptance). These remain acceptance work, not reasons to rewrite their implemented subsystems. B2 is DONE for its recorded governed-answer benchmark; FP13 still needs the distinct enriched-context model-answer evaluation.

## 5. Database coverage

The architecture can represent multiple dialects, but that is not equivalent to verified support for every database/object kind. Current native scope is **PostgreSQL, SQL Server, Oracle, Snowflake, BigQuery and Databricks**. Existing Teradata/Db2 planning is not a delivery claim. No additional candidates are proposed.

For each supported engine publish the existing capability matrix at the level of inventory, definition retrieval, parsing/lineage, profile access, candidate generation and execution. Distinguish unsupported, not applicable, permission denied, unavailable, truncated and unresolved. An Oracle package, PostgreSQL materialized view and SQL Server indexed view must retain their native identity even where they share a common graph category.

PostgreSQL/SQL Server fixtures are useful evidence, not universal certification. The checked-in `live-results.json` is dated September 15 and explicitly says native SQL checks only, without Atlas ingestion. The newer `test_footprint_journey.py` goes further: real source engines, in-process Atlas with SQLite control-plane state, a governed-tool answer, change/rebuild/review and another answer. It still is not a deployed browser journey or a model-generated answer test.

## 6. Validation performed in this review

| Check | Result |
|---|---|
| Frontend suite (`npm test`) | 96 files, 843 tests passed |
| Frontend production build | Passed |
| Ruff (`src tests scripts`) | Passed |
| Strict mypy | Passed, 371 source files |
| Import architecture contracts | 12 kept, 0 broken |
| Frontend reachability | Passed; 212 reachable application modules and three documented exceptions |
| OpenAPI compatibility | No breaking changes against baseline |
| Generated UI types | Match current OpenAPI, 503 schemas |
| Backend complete suite | **10,910 passed, 168 skipped, 1 xfailed, 1 failed**, 58 warnings; 25m 35s; F09 describes the failure |
| Failed backend case after concurrent benchmark correction | Passed, 3.02 seconds; new commit, not an unchanged-tree rerun |
| Combined quality-benchmark and product-scoped Ask suites after parallel changes | 26 passed, 18.25 seconds |
| Real-source footprint journeys (explicit `-rs` rerun to establish neither engine skipped) | PostgreSQL and SQL Server: 2 passed, 11.43 seconds; six SQL Server driver deprecation warnings |
| Product routine-scope reproduction | Unexpected routine admitted; F02 confirmed |
| Live API/UI | UI 200; `/health/live` and `/health/ready` 200; revision/configuration gaps above |
| Source versus deployed OpenAPI and migration | Mismatches confirmed; F03 |
| Documentation links | All relative links resolve across 238 Markdown files |
| New review's source-reference assertions | 10 passed after final evidence updates |

The backend command was `.venv/Scripts/python.exe -m pytest -o addopts= -q --tb=short`; coverage instrumentation was not enabled. The generated-type check used `scripts/generate_ui_types.py` with its default comparison behavior.

Interactive browser access was unavailable in this session. The existing Playwright journey targets the real built nginx/UI with a stub API; it is valuable proxy/navigation evidence but not a real-backend end-to-end certification, and was not rerun here. No new live model benchmark, customer connector certification, load/restore exercise or manual accessibility audit is claimed. Passing tests do not establish those outcomes.

## 7. Recommended execution order and release gate

1. **Finish F01/F02/F08:** retain and validate the newly committed F01 guard, fix routine retrieval scope and the Ask UI request, document pinned-meaning semantics, and extend boundary regressions.
2. **Align and configure the environment:** matching images/migration, intended authorization posture, maintenance passes, discovery cadence and actual notification delivery.
3. **Complete the core context gaps:** routine description lifecycle, permitted profile evidence, explicit per-engine coverage and source-history drilldown.
4. **Prove the product loop:** SQL Server and PostgreSQL, current deployed API/UI, governed-tool and model-generation paths, a denied out-of-product request, a source change and incremental rebuild. Record expected/actual answer and provenance before and after.
5. **Consolidate navigation and refresh status documents:** keep one current remainder per task and attach dated evidence.

**Proceed with focused delivery. Do not declare broad production completion until the applicable gates above and customer-specific release evidence are met.** No new infrastructure, storage rewrite, unrestricted automatic publication, or extra database expansion is required to realize the current vision.

## 8. Comparison with previous reviews — follow-up reconciliation

**Coverage answer:** this report covers the current product direction, footprint implementation, major API/UI gaps, observed deployment, completion queue and deferred-work decisions. It is not an exhaustive certification of every line, route, screen, database engine or external integration. The initial September 16 pass used the current tracker and capability register; this follow-up adds an explicit comparison with the older review packages. Historical evidence is retained, not represented as freshly rerun.

### Review lineage and authority

| Previous document/package | How it relates to this review |
|---|---|
| [August architecture review index](../review-2026-08/00-README.md) and [gap plan](../review-2026-08/gap/02-gap-diff-and-plan.md) | Preserve the execution boundary, evidence-backed metadata, typed graph and governed context/tool direction. Older structural proposals are subject to accepted ADRs and subsequent decisions; they are not a reason to restart module relocation, remove infrastructure or add new scope. Competitor research was not refreshed. |
| [September 5 review](../review-2026-09-05/REVIEW.md), [points tracker](../review-2026-09-05/POINTS-TRACKER.md), [roadmap](../review-2026-09-05/ROADMAP.md) and [coverage record](../review-2026-09-05/COVERAGE.md) | The original F01–F22, structural/hygiene findings and T-series roadmap have successor dispositions in the existing reconciliation. Preserve completed fixes and carry forward external acceptance; do not treat old defect wording as current source evidence. |
| [September 5 UX and seven journeys](../review-2026-09-05/UX-AND-JOURNEYS.md) | Section 3 here retains the consolidation direction, but does not re-audit every old route suggestion. Journey acceptance below explicitly retains the operator, analyst, steward, reviewer, developer and auditor flows. |
| [September 11 review](../review-2026-09-11/REVIEW.md) and [September 11/12 reconciliation](../60-delivery/23-review-reconciliation-2026-09-11.md) | These map the earlier findings into R11. The current delivery tracker is the status authority. Old numerical snapshots and claims such as no live model call, missing freshness writer or blocked ontology migration are not current status. |
| [Agent architecture review](../10-architecture/15-agent-architecture-critical-review.md) | Keep independent contract, budget, injection, suspension, authorization and correction tests. Answer accuracy cannot replace these controls. Use current C-series dispositions instead of reopening historical AR partials automatically. |
| [Graph/query review](../10-architecture/16-graph-query-review.md) | Keep bounded authorized traversal, ambiguity refusal, graph provenance and PostgreSQL authority. Presentation filtering is not authorization; optional graph adapters require their own conformance evidence. Guided language is not unrestricted graph-query execution or formal ontology reasoning. |
| [Accessibility acceptance](../60-delivery/24-accessibility-acceptance-2026-09-12.md) and [acceptance testing guide](../40-engineering/14-acceptance-testing-guide.md) | Retain the actual procedures and dated evidence. Automated UI results in section 6 do not replace manual screen-reader/visual testing or corporate-identity deployment acceptance. |

The [existing reconciliation crosswalk](../60-delivery/23-review-reconciliation-2026-09-11.md#september-5-crosswalk) remains the detailed earlier-ID mapping. This report adds current evidence and gaps; it does not create a second backlog or silently close inherited obligations.

### Earlier findings carried forward explicitly

The following statuses are read from the current tracker during this follow-up; DONE means closed at that row's documented scope, not universally certified in every deployment.

| Earlier concern | Current disposition | September 16 treatment |
|---|---|---|
| Archive integrity, progress and real WORM storage (September 5 F01–F03/T01–T03) | Preserve integrity/progress fixes; B9 remains PARTIAL for real-service evidence | F06/section 4 retain it. Require real destination read-back, retention/hold and applicable provider behavior; do not equate local/MinIO proof with AWS certification. |
| SIEM and notification delivery (F04/F12/T04/T06) | B10 and I1 PARTIAL | F05 measures the disabled worker/backlog. External collector, Slack and Teams receipts remain separate. Teams trigger HTTP 202 does not prove the downstream card posted; retain run-history verification. |
| Corporate OIDC, identity and session lifecycle (F06/T07, September 11 D6/B6) | D6 DONE; B6 BLOCKED on the selected issuer/secrets environment | Keep implemented session/principal repairs. Corporate login, reload/renewal/revocation, role mapping and secret rotation still need the actual topology. |
| Workspace enforcement and direct-path access (F11/T09, B3/D9) | B3 DONE for its enforcement evidence | F05 separately reports this deployment as OBSERVING. A done implementation task does not mean this workspace was switched to ENFORCE. Preserve REST/MCP/direct-ID/export/job boundary cases. |
| Atomic review decisions and agent suspension (F05/T05, AR-04/C4) | Completed decision/concurrency evidence; C4 DONE within its isolation contract | Preserve PostgreSQL concurrency and suspension tests. A UI consolidation must not bypass the single decision path or broaden the supported isolation claim. |
| Agent contracts, budgets, MCP output and correction trails (AR series) | C6/C7/C8 DONE with documented decisions/limits; X7 CANCELLED | Preserve agent identity and budget controls. C7 explicitly accepts the existing execution invariant for result rows rather than adding per-row screening. C8's recorded correction limits are not new unapproved expansion. Provider-reported usage and estimates must remain distinguishable; C11 carries target-environment monitoring/residency acceptance. |
| Unsafe unattended approvals (AR-03/C3) | C3 DONE by the decision to prohibit them in production | F07 retains this decision. A closed safety task does not claim the historical semantic judge became accurate. |
| Access provisioning and revocation (B4) | DONE at the documented local authority/gateway scope | Preserve the request → approval → provision → consume → revoke → deny journey. This was too implicit in the initial September 16 summary; it remains a regression obligation. |
| Freshness observations, scheduled producers and model health (B8/B12/B18) | DONE at recorded scope | Preserve their writers/triggers and visibility. F04's disabled footprint passes do not mean all schedulers or freshness producers are missing. Model existence/reachability does not prove generation quota or successful answers. |
| BI impact and graph/query behavior (B13 and graph review) | B13 DONE at its stated graph contract | Preserve report impact and provenance. Do not add consumption/principal edges to an exportable impact graph merely because an older review requested all consumption lineage. Optional graph-backend parity, lag and outage behavior require specific evidence. |
| Ontology publication and migrations (C1) | DONE with its recorded live journey | Keep that evidence. F03 concerns a newer migration/deployment mismatch, not the obsolete ontology-table blocker. Formal ontology import/reasoning is not implied by the current governed ontology lifecycle. |
| Browser journeys, route/state fixes, scope pickers and accessibility | B11/D6/D13 DONE at recorded scope; C2 PARTIAL; S13 DEFERRED | Preserve completed routing, proxy, identity and CI work. Section 3 proposes narrow consolidation; manual acceptance remains open. Do not resurrect the obsolete target screen count. |
| SDK packaging, dependency boundaries, shims and source execution/signing | Earlier completed repairs and current architecture gates retained | The full test/static checks are broad regression evidence, not a fresh release-container SDK packaging audit or vulnerability scan. Do not delete compatibility shims while consumers remain. |
| Capacity, contention, tenant fairness, pool sizing, restore and release security | B15/C9 BLOCKED on target evidence | Carry these into deployment acceptance. Neither small sample journeys nor the backend pass count proves target-scale behavior or restore objectives. |
| Optional UI breadth, workbook integration, product telemetry and broad retention | Existing C12/S7/deferred decisions apply | Manual workbook import/export does not promise automatic Excel save-back. Do not start that integration, formal reasoning, new telemetry or new retention capabilities as a result of this review. |

### Coverage completion at the user-journey level

To claim the complete product works, retain all seven earlier journeys, not just the database-footprint path:

1. **Operator:** register a source, verify scope/credentials, scan, inspect omissions, configure schedules and recover failure.
2. **Analyst:** select permitted context, ask, clarify, read results/evidence, and receive correct refusals. Include F02/F08 and the new F01 guard.
3. **Steward:** inspect/profile evidence, draft descriptions/meaning/relationships, respond to source changes and submit for review.
4. **Reviewer:** inspect actual differences and provenance, approve/reject independently, enforce authority and trace corrections.
5. **Developer/agent consumer:** publish a reviewed context product/tool, discover and consume it through REST/MCP, and enforce permissions, versions, budgets and revocation.
6. **Operator recovery:** see failures and stale queues, retry safely, observe actual destination delivery and recover the intended state.
7. **Auditor:** reconstruct an outcome from trace/version/evidence, export within authority and verify the applicable archive/retention controls.

The September 16 review supplies automated and source-inspection evidence for portions of these journeys. It does **not** certify all seven through the deployed browser, real corporate identity and external destinations. These are retained acceptance obligations, not newly proposed features.

**Result of comparison:** the new report is aligned with the earlier reviews and current decisions. It is now explicit about inherited obligations that were previously summarized too briefly. No additional database candidates or unrelated future features are added. Read this report for the current assessment, the delivery tracker for work status, and the older reviews/acceptance guides for detailed rationale and procedures.
