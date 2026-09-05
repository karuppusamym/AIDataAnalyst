# Workflow revalidation — 2026-09-04

Scope: current working tree, React portal (`ui-next`), relevant backend services, and read-only requests to the running API through the portal at localhost:3001. Existing uncommitted application changes were preserved. This is a review, not an implementation or deployment.

## Confirmed findings, ordered by priority

### 1. High: Tool Plans reports completion without executing tools

`src/aida/tool_plans_api.py:414` calls `execute_plan` without a `step_executor`. In `src/aida/tool_plans.py:306`, the missing-executor branch records each step as `COMPLETED` with `{"dry_run": true}`. The API persists these results and exposes them as a completed execution.

The validation endpoint calls `validate_plan(plan)` without resolving available published tools (`tool_plans_api.py:322`). Availability checking in `tool_plans.py:192` only runs if `available_tools` is supplied. Consequently, a structurally valid plan referencing a nonexistent tool can validate and complete in the default execution path.

Reproduced locally without database writes: a plan referencing `nonexistent-review-tool`, version `999`, returned `valid=True`, `status=COMPLETED`, and `step_evidence={"dry_run": True}`. Inspection confirms that the HTTP execution endpoint invokes that same default path. No plan was executed against the running deployment.

Required correction: resolve versioned, published tools in the caller's organization, validate parameters and permissions, and execute through the existing governed execution gateway. Missing runtime wiring must fail explicitly. If simulation remains available, expose it as a separate operation and status. Step timeouts and token limits also need actual enforcement; the current executor does not enforce the per-step timeout, and token consumption remains zero.

### 2. High: Catalog Export JSON bypasses authentication headers

`ui-next/src/components/EvidencePane.tsx:151` uses an ordinary anchor to `/v1/metadata/tables/{id}/evidence/export`. Navigation bypasses `identityHeaders()` in `ui-next/src/lib/api.ts:158`. The endpoint correctly requires an authenticated context (`src/aida/asset_evidence_api.py:116`).

Live reproduction through the React portal proxy, using the same existing table and export URL:

- Without identity headers: HTTP 401, `{"detail":"X-Principal-Id is required in development mode"}`.
- With the normal development principal, roles and organization headers: HTTP 200, `application/json`, 381 bytes.

Required correction: fetch through the shared authenticated client, then download the returned blob with a filename; expose loading and error states. Preserve backend authentication. This finding is confirmed for Catalog Export JSON, not every export: Compliance already uses an authenticated API helper.

### 3. High: automatic business-semantic generation stops at a readiness marker

`src/aida/newly_created_table_drafter.py:332` creates a description draft. Once analysis is complete, the same handler only records `semantic_inference_ready=True`; its comment explicitly leaves proposal generation to the operator-triggered inference endpoint (around lines 412–419).

The implementation therefore does not fulfill the module header's claim that ingestion automatically creates a semantic-inference proposal. The default `auto_enqueue_on_ingest=True` setting does not close this gap.

Required correction: enqueue a durable, idempotent semantic-inference job after a successful scan and persist the actual proposals. Surface Pending scan / Generating / Proposed / Needs review / Approved / Failed states in the asset UI. Existing automatic description drafting should be retained and tested separately.

### 4. Medium: semantic authoring and recommendation APIs are disconnected from the main screens

`ui-next/src/screens/SemanticsScreen.tsx` lists models, metrics and consumers. It exposes no model/metric creation, suggestion generation or submission actions. Business Meaning browses approved annotations and supports glossary work, but does not invoke semantic inference or tool-blueprint promotion.

Backend capabilities exist:

- Manual model creation, cloning, metric creation and review submission: `src/aida/semantic_api.py`.
- Rules-based inference with an optional governed model: `POST /v1/datasources/{id}/semantic-inference-runs`, in `src/aida/semantic_intelligence_api.py:132`.
- Candidate metrics from approved business annotations: `POST /v1/organizations/{id}/metric-suggestions/generate`, in `src/aida/metric_suggestion_api.py:108`.

Required correction: connect these capabilities through clear Create manually / Generate suggestions / Review suggestions actions. Keep business annotations (domain, entity, grain, meaning) distinct from executable semantic models and metrics (measures, aggregations and definitions). Generating an annotation is not equivalent to publishing a semantic model.

### 5. Medium: description generation is mislabeled, and human editing is missing from the React workflow

Catalog and Description Drafts use “model-drafted” wording. The description endpoint actually calls `compose_draft_text(evidence)` in `src/aida/asset_description_api.py:257`. `src/aida/asset_description_service.py:169` composes text deterministically from catalog evidence, including available dbt and approved business descriptions. Upstream evidence may have other origins; this endpoint itself is not an LLM generation call.

Description Drafts allows submission and review navigation, but has no editor. Its API has generation/list/submit operations, with no draft text update endpoint.

Human-authored documentation is possible through a separate backend workflow: `POST /v1/metadata/tables/{id}/documentation-versions` and `POST /v1/asset-documentation-versions/{id}/submit` in `src/aida/glossary_api.py:431` and `:494`. Those calls are not connected to a React description editor.

Required correction: offer Generate from metadata and Write manually; allow editing a generated draft before submitting. For an approved description, create a new revision and show a diff before review. Preserve origin, source evidence, author and reviewer; do not present edited text as untouched machine output.

### 6. Medium: prompt-to-reusable-tool is not an integrated user journey

The registry supports manual SQL/parameter authoring, new versions, review submission and execution of published tools. The analyst orchestrator can select governed tools or generate SQL for an analysis (`src/aida/agent_orchestrator.py:817` onward), but the Ask screen does not offer a Save as draft tool action.

A narrower generated-tool path already exists: approved business-semantic blueprints can be promoted to draft governed tools via `POST /v1/metadata-enrichment-proposals/{id}/promote-tool` (`src/aida/semantic_intelligence_api.py:665`). Multi-table and view blueprint APIs also exist in `src/aida/tool_api.py`; the registry screen does not surface them.

Required correction: connect approved blueprint promotion first. Add Save as draft tool to eligible successful analyses, with parameter extraction, metadata references, validation and independent publication review. Saving a generated query must not automatically publish it.

### 7. Medium: Tool Plans authoring does not expose its stated multi-step purpose

`ui-next/src/screens/ToolPlansScreen.tsx:164` explicitly limits the editor to one step. It accepts manually typed tool IDs, versions and parameter JSON. There is no published-tool picker, add-step/dependency editor, prompt generation action, or plan inventory; an existing plan is retrieved by ID.

A Tool Plan is a sequence of tool invocations with dependencies and budgets. It is a separate persisted object from the analyst's internal planning result and from a registry tool version. The presence of agent planning does not mean the Tool Plans page supports prompt-generated plans.

Required correction: repair execution first, then add a published-tool picker with typed parameter controls and multi-step editing. A future prompt flow should propose a visible plan that a human can edit before execution. Save reusable multi-step workflows as versioned plan templates; registering a composite plan as one tool needs an explicit composite-tool contract that is not supplied by the current SQL-tool form.

### 8. Medium: lineage topology obscures direction and does not fit the available viewport

`ui-next/src/screens/UnifiedLineageScreen.tsx:137` places nodes in columns by node kind, independent of dependencies. Connected TABLE nodes therefore share an x-coordinate. Edges are plain center-to-center SVG lines (`:505` onward), so links between tables overlap vertically and pass through intervening nodes; they have no arrowheads.

The SVG uses fixed dimensions determined by node counts. The topology displays only the first 90 nodes, has no fit/zoom controls, and competes with a fixed 360px impact panel (`UnifiedLineageScreen.css:13`). These are code-confirmed limitations. No enabled Chrome or in-app browser was available, so no visual screenshot or viewport-specific rendering has been verified.

Required correction: default to a selected asset's upstream/downstream neighborhood, lay out nodes by dependency direction, add arrows and fit/zoom/full-screen controls, and make the detail panel collapsible. Keep provenance and review status visible. Distinguish declared relationships from transformation/data-flow evidence. Retain honest server and display-limit indicators and the table fallback.

### 9. Medium: creating a new tool version drops existing parameter constraints

`ui-next/src/screens/ToolRegistryScreen.tsx:680` copies parameter name, type, required/sensitive flags and allowed values into its editable form, but omits `default`, `minimum`, `maximum` and `max_length`. The creation payload around `:717` also omits them. Allowed values are converted through strings.

Consequently, choosing New version and submitting can change the parameter contract even when the user intended only a description or SQL edit. This is a code-confirmed loss of constraints, not a live mutation test.

Required correction: preserve and expose the complete parameter schema, retain value types, and show contract differences before submission.

## How these features should work together

| Feature | Practical purpose | Creation and human control |
|---|---|---|
| Description | Explain what an asset contains and how to use it | Metadata draft or manual writing → edit → review → approved revision |
| Business semantics | Explain entities, domains, grain and terminology | Post-scan suggestions or manual input → edit/review → approved annotation |
| Semantic metric/model | Make analytical definitions consistent | Recommendation or manual definition → validation → review → publication |
| Governed tool | Reuse an approved parameterized analytical operation | Manual authoring, approved blueprint, or saved analysis → draft → review → published tool |
| Tool plan | Coordinate several tools for one task | Manual or future prompt-generated steps → inspect/edit → validate → execute; save a reusable template separately |
| Lineage | Explain origin, transformations, consumers and change impact | Collect evidence from supported metadata/SQL/dbt/OpenLineage/runtime sources; review inferred edges where configured |

Example intended journey: ingest a payments source → review its generated description and business meaning → define an approved payment-total metric → save a validated monthly-payments operation as a governed tool → use it from Ask or a multi-step plan → inspect lineage before changing its source columns. This is a target journey; the disconnected and non-executing paths above prevent claiming it is complete today.

## Verification and implementation order

- Live export comparison: missing identity headers gives 401; normal identity headers gives 200.
- Local Tool Plans probe: nonexistent tool validates and records dry-run completion.
- Backend tests: `tests/test_tool_plans.py`, `tests/test_asset_description.py`, `tests/test_semantic_inference.py`, `tests/test_lineage_evidence_export.py` — 41 passed.
- React tests: Semantics, Description Drafts, Tool Plans, Tool Registry and Unified Lineage — 28 passed across five files.
- Passing tests cover existing contracts; they do not prove real plan execution, authoring completeness, export navigation authentication or visual layout quality.
- Browser-based visual verification unavailable; existing app changes were not modified, committed or deployed.

Implementation order: (1) truthful governed plan execution and authenticated exports; (2) preserve tool contracts and connect actual semantic jobs; (3) description/semantic editing and review; (4) generated-tool reuse and multi-step plan authoring; (5) lineage layout and navigation. Add regression coverage for the reproduced failures and meaningful end-to-end journeys as each change lands.
