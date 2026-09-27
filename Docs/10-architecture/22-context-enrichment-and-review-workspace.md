# Context enrichment and review workspace: 22-item assessment

Date: 2026-09-17. Assessment of the current working tree; concurrent implementation is in progress. Existing code below means inspected implementation, not a new deployment certification. This document proposes API/UI acceptance criteria; it does not claim they have shipped.

Continue the [database footprint design](20-database-footprint-and-agent-context.md) and [GraphQL, OKF and workspace design](21-graphql-okf-and-workspace-design.md). The [delivery tracker, section P](../60-delivery/03-tracker.md) remains the single work queue. No additional database candidates or unrelated future features are added.

## Decision

Proceed with the existing product direction. Prioritize a shared SQL review workspace, usable profiling evidence, and review at estate scale. Most requests extend existing services. Avoid parallel metadata stores, approval engines, query executors or wiki truth sources.

The outcome is reusable, permission-scoped understanding: what an object means, how it relates to others, which evidence supports each statement, what remains unknown, and which approved tools can answer a live question. More generated prose alone does not improve context.

## All 22 requests mapped

| # | Request | Current foundation and proposed change | Delivery owner |
|---|---|---|---|
| 1 | Analyze distinct field values; ignore PK | Value-free distinct counts, ratios and uniqueness facets exist. Add key-aware profiling plans and explain skipped/unsupported work. Skip expensive value enumeration on PKs; retain constraint health checks. | R11-FP04 |
| 2 | Histograms | Fixed length-bucket counts exist. Add per-column distribution views with observation scope, freshness and unavailable reasons. Numeric boundaries and category labels need the value-bearing evidence policy. Table overview summarizes its columns. | R11-FP04 |
| 3 | Open Knowledge Format | Pure exporter, authorized snapshot assembly and download API exist. Finish durable snapshots, incremental refresh and approved consumption; concurrent storage files are not completion evidence. | R11-OKF01/02/03 |
| 4 | Basic column description | Column description generation and review exist. Surface missing/stale descriptions, evidence and approved text in the column panel. | R11-FP08 |
| 5 | Declared/observed/inferred | Existing relationship evidence reason codes and model proposal origins provide a foundation. Present provenance independently from approval and strength of evidence. | R11-FP06/08/09 |
| 6 | A to B to C lineage | Unified lineage and routine edges exist. Show typed paths, intermediate objects, transform evidence and unresolved segments. Dependency paths do not automatically establish joins. | R11-FP07/11 |
| 7 | Merge tabs | Navigation consolidation already reduced destinations. Continue task-based grouping, preserve deep links, and measure common journeys. | R11-S13 |
| 8 | OpenRouter selection | No OpenRouter integration found in the inspected source. It can select models from prompts, but Atlas must constrain eligible routes and audit the actual selection. Optional provider evaluation under the existing model-governance task. | R11-C11 |
| 9 | Human validates generated SQL | Datasource-aware validation and gateway execution exist. Add a generation-only draft stage, edit/validate/confirm workflow and server-enforced execution binding. | R11-SQL01, new |
| 10 | Paste SQL and analyze | Reuse the same workspace and validator; offer explain/validate without running, then an explicit Run action. The development-only candidate_sql override is not the user feature. | R11-SQL01 |
| 11 | BERT/encoder | Embedding configuration and hybrid retrieval already exist. Evaluate a sentence encoder or bounded reranker within those stages; no separate retrieval service or assumption that BERT generates SQL. | R11-FP11 |
| 12 | Small history, large result | Answer-first layout and collapsible history changes exist. Finish results/query/evidence workspace and responsive/browser acceptance. | R11-UX16 |
| 13 | GraphQL query/data | Design and tracker tasks exist; an enum or ingestion parser is not a served endpoint. Read facade first, controlled execution mutation second, both using existing services. | R11-GQL01/02 |
| 14 | Views/functions as tools; review procedures | Existing tool agent proposes drafts for eligible views and proven read-only extracted routine queries. Keep publication review. Native function/procedure invocation needs its own effect and signature contract. | R11-FP14; native invocation remains R11-FP18 |
| 15 | Infer joins from views | Reuse parsed lineage and relationship validation. Extract join predicates, direction, composite pairs and source versions; create evidence-backed candidates. | R11-FP06/07 |
| 16 | Enrich tables from routines/views | Reuse description and lineage services. Add derived usage explanations with exact source evidence; do not overwrite source declarations with inferred business meaning. | R11-FP08/16 |
| 17 | Generate OKF YAML | Deterministic exporter already serializes YAML frontmatter and Markdown from structured snapshots. Finish preview/download of the same immutable snapshot. | R11-OKF01/02 |
| 18 | Persona documentation | Context products/compiler are the foundation. Add analyst, engineer, steward and agent projections of the same approved knowledge, with permission filtering before rendering. | R11-FP12, R11-OKF02 |
| 19 | Separate Agent Gateway hub | Existing gateway UI and MCP integration provide a logical hub. Keep integration setup/discovery there; reuse platform policy, context and execution. A separate deployed service is not required now. | R11-S13, R11-GQL01 |
| 20 | Playbook/rules approval | Rules/playbooks, governance reviews and reversible bulk operations exist. Add explicit evidence-based decision requirements and simulations; an LLM score must not grant approval. | R11-REV01, new |
| 21 | Merge stewardship/playbooks | Group Overview, Review queue and Rules/Playbooks under Stewardship; retain distinct review and automation contracts. | R11-S13 |
| 22 | Review thousands of objects/columns | Bulk actions and batch review exist. Add bounded, filtered, change-focused review with server-side pagination, explicit batch membership and stale-evidence checks. | R11-REV01 |

## Profiling and evidence contract

Inspect `src/atlas/modules/profiling/facets.py` and `models.py`: ordinary profile facets are value-free; approved value-bearing artifacts have a separate exception/retention lifecycle. Preserve that separation.

- Distinct count is different from listing distinct values. Counts often suffice for uniqueness, categorical hints and join assessment. Do not send all distinct values to an LLM.
- A composite PK is unique as a tuple; its individual columns may repeat. Record whether a declared constraint is enabled, validated and current. A 98% uniqueness heuristic is not a primary-key declaration.
- Record full scan versus sample/estimate, row count, null count, algorithm, timestamp, cost bound and unsupported reasons. No zeros invented for missing evidence.
- Show categorical top-k/other, numeric histograms or date distributions only when that evidence class is permitted. Bound cardinality and suppress small sensitive groups according to policy. Histogram edges and labels can reveal values even if rows are absent.
- Use incremental profiling based on data freshness as well as schema changes: unchanged DDL does not imply unchanged distributions.

API additions should extend the existing profile response with typed distribution availability and policy reasons. UI should use a table summary plus a searchable, paginated column list and an on-demand column panel. Acceptance includes composite keys, sampled estimates, high-cardinality text, denied values, empty/all-null columns and source timeout behavior.

## Provenance, ontology and code understanding

Keep a stable core vocabulary (database, schema, object, column, routine, tool, concept and typed edges). Business concepts and mappings evolve through versioned proposals and review. Ontology is therefore a stable structure with dynamic governed content.

Each claim needs a subject, predicate/value, origin, source object/version, evidence location, observation time and review state. Suggested UI origin labels: Declared, Observed, Rule-derived, Model-proposed. Map existing records to these labels; do not silently rewrite enums. Review state (draft/approved/rejected/stale) is a separate dimension. A reviewer approving a claim does not turn inferred evidence into a database declaration.

Parse code by dialect into statements and scopes. Track CTEs, temporary tables, writes, reads and calls through sequential statements. Retain uncertainty for dynamic SQL, truncated bodies, unavailable definitions and unsupported constructs. Chunk long code by semantic boundaries, summarize with source spans/digests, then compose object summaries within a token budget. Never claim complete lineage from an incomplete parse.

A view joining Customers to Orders supports a candidate join with exact column pairs and join/filter semantics. It does not prove a declared FK, referential completeness or one-to-many cardinality. Validate those separately. An A-to-B-to-C path must retain each edge's type and evidence; transitive dependency is not permission to join A directly to C.

Derive table-level usage notes from approved routine/view evidence. Source changes stale the affected claims, relationships, tool candidates and context documents through existing change processing. Rejections should remain suppressed until relevant evidence changes.

## Shared SQL workspace: API and UI

Existing entry points:

- `POST /v1/query/validate`: guard-only check.
- `POST /v1/datasources/{datasource_id}/sql-validations`: wired in `main.py`; calls `QueryExecutionGateway.validate`, returning findings, references, lineage and estimates. It may contact the source for a dry-run estimate; it returns no data rows.
- Existing datasource query execution uses the gateway. Validation access is not execution authorization.

Build one workspace for generated and pasted SQL. Reuse it inside Ask rather than adding another top-level destination. Display source/dialect, selected context product, SQL editor, parameters, explanation, validation findings, estimated cost, and prominent Results/Query/Evidence views. History remains collapsible and narrow.

Proposed lifecycle: `DRAFT -> VALIDATED -> CONFIRMED -> EXECUTING -> SUCCEEDED/FAILED`; editing invalidates validation and confirmation. Draft generation and analysis must never execute the query. Explicitly distinguish user execution confirmation from governance publication approval of reusable tools.

Persist a bounded draft and validation receipt tied to SQL/parameter digest, caller, datasource, workspace, context-product version, policy/catalog versions, row/cost limits and expiry. Define new request contracts in the implementation; do not imply these fields already exist on the validation endpoint. Carry product scope through validation and execution. At Run, verify the receipt and reauthorize/revalidate through the gateway. Changed grants, source definitions or policy cannot be bypassed by an earlier confirmation. Retry/idempotency handling must prevent an accidental duplicate execution.

Acceptance: generated SQL causes zero executions before Run; pasted SQL uses the same path; edited SQL requires revalidation; expired/cross-user receipts fail; product scope and revoked access fail closed; forbidden SQL and unauthorized functions cannot execute; cancellation and failed estimates are visible. Exercise PostgreSQL and SQL Server fixtures plus the browser journey. A passing validator does not certify business correctness.

## Review at estate scale and playbook decisions

Use one review queue, filtered by source/schema, object kind, owner, missing/stale evidence, change reason and decision type. Group related changes and show diffs; virtualize the UI and paginate on the server. Review detail loads on demand. Never load a thousand-column object into a single generation prompt or browser panel.

Batch decisions must bind explicit IDs/versions or a frozen selection snapshot, not an unbounded live filter. Preview count, exclusions and representative evidence; let reviewers inspect every member. Group approval requires each member's checks. Sampling is useful for quality measurement, not proof that uninspected critical changes are safe. Record per-item success/refusal, concurrent edits, resumable progress and compensating actions.

For each decision type define required evidence and the permitted actor:

| Decision | Required review evidence |
|---|---|
| Description | Source references, unsupported-claim checks, conflicts and changed-source status |
| Relationship | Exact column pairs, predicate origin, uniqueness/cardinality/null evidence and observation scope |
| Tool publication | Read/effect contract, parameters, grants, scope, result schema, limits and validation fixtures |
| Classification/certification | Policy rule/version, affected objects, expiry and responsible reviewer |

Existing playbooks have an `auto_apply_max_items` mechanism, so do not describe all automation as currently human-only. Audit each action's real path when extending rules. Default new semantic/publication decisions to review, preserve maker-checker and production restrictions on unattended LLM reviewer approval. A match-count threshold or model confidence does not establish truth. Add rule dry-run, versioned match evidence, conflict handling and before/after previews to the existing playbook service.

Acceptance includes a synthetic 1,000-table estate and a 1,000-column table, bounded API responses, selection across pages, changed/deleted members, unauthorized objects, partial failures and correction replay. Set and record performance budgets before implementation benchmarking; no unmeasured latency promise.

## OKF, persona documents and agent consumption

OKF is Markdown with YAML frontmatter, not a single YAML dump. Follow the pinned [Atlas export profile](../90-reference/okf-export-profile.md) and its conformance limits. Current code uses structured serialization; the LLM proposes evidence-backed content, not paths or authority fields.

Generation: authorized approved snapshot -> object/concept documents -> source/product indexes and permitted links -> validation and hashes -> immutable bundle -> preview/download/retrieval. Use object identity plus routine signature/package where relevant. Wiki is a view of the same documents. Imported edits return through proposals, not direct publication.

Illustrative frontmatter shape only, not a conformance fixture:

```yaml
---
type: concept
---
```

The Markdown body carries the object description, supported relationships, evidence references and limitations. Exact supported fields and generated paths belong to the exporter/profile; never invent a human verification attestation for model text.

Persona projections emphasize different fields: analysts get meaning/grain/join cautions; engineers get definitions/dependencies/change impact; stewards get evidence/conflicts/decisions; agents get structured IDs, versions, scope and callable approved tool contracts. Filter authorization before projection. Personas do not change facts or grant access.

Incremental refresh recomputes affected documents and dependent indexes, preserves no-op hashes, and marks stale evidence before reuse. Answers cite the retrieved object/document versions. Current numeric questions execute approved queries/tools against the source; a context document is not live data. Enforce permissions on each online retrieval; downloaded copies cannot be remotely revoked.

## Model routing and encoders

OpenRouter's `openrouter/auto` selects a model based on the prompt/task and supports routing restrictions. Selection can change between requests. If adopted, Atlas must enforce the eligible provider/model set, data handling, budgets and fallback behavior and record the actual model/provider used. Start with explicit approved routes and evaluate automatic selection against SQL/description tasks. Prompt classification never supplies data authorization. See [official Auto Router documentation](https://openrouter.ai/docs/guides/routing/routers/auto-router).

Sentence encoders can improve semantic matching of names, descriptions and business terms; a cross-encoder can rerank a small retrieved candidate set. Integrate only within existing retrieval stages and measure retrieval recall, correctness, latency and cost against the current baseline. Version embeddings and rebuild affected indexes on model changes. BERT similarity is not an approval score. See [Sentence Transformers retrieve and rerank](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html).

## Delivery order and verification boundary

1. R11-SQL01: reviewed generated/pasted SQL workspace, integrated with UX16.
2. Existing FP04/06/08/09: actionable profiling, visible provenance, relationships and descriptions.
3. R11-REV01 with S13: scalable review and unified stewardship navigation.
4. Existing OKF/GQL tasks: complete approved context export/consumption and agent access.

OpenRouter and encoder changes are optional evaluations within existing work, not prerequisites. Native routine execution remains explicitly deferred under FP18; automatic safe tool drafts are already in scope. The six database families in the footprint design retain capability-specific support; this assessment does not claim every engine supports every object or profiler.

Source inspection for this assessment included profiling facets/models, SQL validation API/router wiring and gateway, description/model schemas, relationship validation, playbooks API/service, OKF exporter and current tracker reconciliation. No new runtime behavior was delivered by this document. Existing tests and historical live proofs remain attached to their owning tracker rows; each new journey requires its own acceptance evidence before being marked complete.
