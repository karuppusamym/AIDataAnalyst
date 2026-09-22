# Module 18 — Studio

> Layer L5 · Schema `studio` · Owner: Product Engineering

## 1. Purpose

The authoring environment for semantics, tools, and context products — with drafts, tests, diffs, and version control. Studio is where a steward or analytics engineer *builds* governed objects, as opposed to the steward workbench where they *curate* proposals.

**This is a parity requirement.** Snowflake ships Semantic Studio (AI-assisted IDE with Git integration); Atlan ships Context Engineering Studio. Atlas differentiates not by having a Studio but by what its objects do: Atlas semantic objects **carry policy and compile into executable governed tools**.

## 2. Jobs served

S1 (author and curate), A4 (build a reusable tool), and the authoring half of R1.

## 3. Responsibilities

- Semantic model editor: entities, annotations, metrics, dimensions, grain, join rules.
- Tool authoring with the parameter-contract designer.
- Context product builder.
- **Change sets** — group related edits into one reviewable unit.
- Test harness: dry-run against fixtures before submission.
- Diff view: what this change does to the published state.
- Git-backed change sets for teams that manage semantics as code.
- Impact preview before submission.

> **Implementation status (2026-09-20).** Change sets, conflict detection, the test harness, the diff, the impact preview, the parameter-contract validator, context-product items and the usage-derived eval gate are built on the API (`src/aida/studio_api.py`; tracker ST-A1 to ST-A5, ST-A7 and ST-A8). The UI is narrower: `ui-next/src/screens/StudioChangeSetsScreen.tsx`, over the calls in `ui-next/src/lib/api/studio.ts`, lists change sets, shows their items, diff, impact and latest eval run, and submits them. As of 2026-09-21 (tracker R11-AUD08) it also lets the four roles that may write Studio (DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin) create a change set, add an item and remove one (DRAFT only), run the tests, detect conflicts against a published state the author supplies, and mine eval questions, and lets any role that may read Studio run the two definition checks (parameter contract, context product) on an item. That is tested with mocked transport and in the demo build; it has not been exercised against the running API. The API side did not keep its writes until 2026-09-21: create, add, remove, run-tests and detect-conflicts flushed and never committed, so a change set that answered 201 was gone on the next request. Fixed the same day and pinned by `tests/test_studio_writes_persist.py`, which sends each call as its own request. The test run returns totals, not the reason an item failed. Tracker row R11-X5 owns studio authoring with a date. Git-backed change sets are not built: there is no Git binding model, route or worker in `src/` (ST-A6, deferred with R11-C12 at P2). The sample estate holds no change sets as of 2026-09-20.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Owning semantic objects | 07 semantic-layer |
| Owning tools | 14 tool-registry |
| Approval | 17 policy-governance |
| Execution | 16 query-gateway |

Studio is an **authoring surface over other modules' objects**. It owns change sets and drafts; it does not own the published artifacts.

## 5. Domain model

```text
change_set (author, state, target_objects, created_at)
draft (object_type, object_ref, content, base_version)
test_fixture, test_run, test_result
git_binding (repo, branch, path_mapping, sync_state)
```

## 6. Change sets

The unit that makes Studio useful rather than a form.

| Property | Behaviour |
|---|---|
| Grouping | Related edits across metrics, annotations, and tools reviewed together |
| Base version | Each draft records the published version it was based on |
| Conflict detection | If the base version was superseded, the change set flags the conflict |
| Test gate | A change set cannot be submitted until its tests run |
| Impact preview | Shows downstream tools, metrics, and dashboards affected |
| Single proposal | Submits as **one** proposal to the review queue |

Reviewing eight related edits as one coherent change is materially different from reviewing eight unrelated queue items. The change set is what makes semantic work reviewable at team scale.

## 7. Test harness

Governed semantic objects should be testable before they are approved.

| Test kind | What it checks |
|---|---|
| Compilation | The metric compiles to a valid logical plan |
| Validation | The rendered SQL passes gateway validation (without executing) |
| Fixture execution | Against a synthetic fixture dataset, results match expectations |
| Grain consistency | The metric's grain matches its physical mapping |
| Join validity | Join paths exist and are approved |
| Regression | Previously passing tests still pass |

**Fixture datasets are synthetic**, never production data (ADR-0014).

## 8. Git integration

Optional; for teams treating semantics as code.

| Capability | Behaviour |
|---|---|
| Export | Change sets serialize to a versioned file format |
| Import | Repository changes create draft change sets |
| Sync state | Divergence between Git and Atlas is visible, not silently resolved |
| Authority | **Atlas remains authoritative** — Git is a projection of approved state, not a bypass of approval |

The last row is the important one. A Git merge must not be able to publish a semantic version without passing maker-checker.

> **Implementation status (2026-09-20).** Nothing in this section is built. There is no Git binding model, route or worker in `src/`; the row is ST-A6, deferred with R11-C12 at P2.

## 9. Public interface

```python
# studio/api.py
def create_change_set(scope, name) -> ChangeSetDTO
def add_draft(scope, change_set_id, object_type, content, base_version) -> DraftDTO
def run_tests(scope, change_set_id) -> TestRunDTO
def preview_impact(scope, change_set_id) -> ImpactReportDTO
def diff(scope, change_set_id) -> DiffDTO
def submit(scope, change_set_id) -> ProposalDTO       # via module 17
def sync_git(scope, binding_id) -> SyncResultDTO
```

## 10. Events

Emits `studio.changeset_created|submitted|abandoned`, `studio.tests_run`, `studio.git_synced`.

## 11. Dependencies

07 semantic-layer, 14 tool-registry, 19 context-products-mcp.

## 12. Current state → target

Studio is **partly built**. It has change sets, conflict detection, the test harness, diff, impact preview and the eval gate on the API (tracker ST-A1 to ST-A5, ST-A7 and ST-A8), and a screen that reads, authors and submits them (R11-AUD08). Git binding is deferred (P2, R11-C12) and has no code. The metric composer, tool authoring and business-meaning review are still individual form-based screens that write their own objects; none of them writes into a change set yet.

| Capability | Now | Target |
|---|---|---|
| Metric composer | Implemented (form) | Move into Studio with diff and test |
| Tool authoring | Implemented (form) | Parameter-contract designer with test harness |
| Change sets | Implemented on the API (ST-A1); the UI lists, inspects, creates and submits them, and adds or removes items while DRAFT (R11-AUD08) | Core Studio primitive, authored in the UI |
| Test harness | Implemented on the API (ST-A2); run from the change-set screen (R11-AUD08). Running it moves a DRAFT to TESTING, which locks its items; the response gives totals, not per-item failure reasons | Required before submission |
| Diff view | Implemented on the API (ST-A3); shown on the change-set screen | Required for reviewers |
| Impact preview | Implemented on the API (ST-A5); shown on the change-set screen | Integrated into submission |
| Git binding | Not implemented; deferred (ST-A6, P2, R11-C12) | Optional |
| Usage-derived eval suite | Implemented (ST-A8) | Mined from consumption + BI lineage edges; gates change-set submission |

## 13. Open work

*Status lives in [tracker section P](../60-delivery/03-tracker.md); this table is the original scope. ST-1 to ST-5, ST-7 and ST-8 are the tracker's ST-A1 to ST-A5, ST-A7 and ST-A8, all DONE on the API as of 2026-09-20, and ST-6 is ST-A6, deferred with R11-C12. What is still missing for the rest is the authoring UI (section 3).*

| ID | Item | Priority |
|---|---|---|
| ST-1 | Change set primitive with conflict detection | P1 |
| ST-2 | Test harness with synthetic fixtures | P1 |
| ST-3 | Diff view for semantic objects | P1 |
| ST-4 | Parameter-contract designer | P1 |
| ST-5 | Impact preview at submission | P1 |
| ST-6 | Git binding with Atlas-authoritative sync | P2 |
| ST-7 | Context product builder | P1 |
| ST-8 | Usage-derived answer eval suite: mine BI dashboards and query logs for real question patterns, turn them into a regression suite that must pass before a change set can publish | P1 |

**ST-8, added 2026-08-30.** Atlan's Context Engineering Studio auto-generates "hundreds of questions your AI agent needs to answer correctly" from existing BI dashboards and SQL queries, and gates deployment on them passing ([atlan.com/context-engineering-studio](https://atlan.com/context-engineering-studio/) — atlan.com is blocked by this environment's egress proxy; read via search-result summaries, not fetched directly, so verify the exact mechanics before scoping). Studio's test harness (§7) already validates that a semantic object *compiles and executes correctly*; it does not yet validate that an *agent's answer* stays correct as usage evolves — that's the model-risk evaluation-corpus gap already flagged in `00-product/04-competitive-feature-matrix.md` §6 (`Atlas: ◐ control evals only`). ST-8 is the concrete mechanism to close it: derive the eval corpus from real usage (query logs, saved BI questions) rather than hand-authoring it, and re-run it as a regression gate on every change set — the same shape as TS-12's doc-claim regression gate, applied to answer correctness instead of documentation claims.

**ST-8, delivered 2026-08-30 (tracker ID ST-A8).** Built as an extension of the existing change-set/test-harness machinery, not a parallel system — no free-text LLM grading, matching the module's deterministic, value-free shape (ADR-0014). Three new tables (`studio_eval_question`, `studio_eval_run`, `studio_eval_result`; migration `d3f8a1c56e90`) and a mining module (`studio_eval.py`):

- **Mining** (`POST /v1/studio/eval/mine`, org-scoped, idempotent): scans recent `ConsumptionRecord` rows (`resource_type="governed_tool_version"`) into TOOL questions, and BI `BiReportMetricEdge` rows into METRIC questions by resolving the BI field's already-matched physical column (`BiMetricColumnEdge.matched_table_id`/`matched_column_id`, populated by `bi_api.py` at import time) against a `PUBLISHED` `SemanticMetricVersion` defined on the same table+column. One question per distinct object; each stores only an evidence *edge id* and a label built from governed object names (never raw query text or result values).
- **The gate**: `run_tests` re-validates every mined question a change set's touched items cover, reusing `_validate_metric_item`/`_validate_tool_item` from §7 unchanged, and persists `StudioEvalRun`/`StudioEvalResult`. `submit_change_set` blocks (409, naming the specific regressed question ids) if the latest eval run failed — on top of, not a substitute for, the existing per-item test-status gate.
- **Proof**: `tests/test_studio_eval.py::test_mined_eval_question_blocks_regressing_change_set` mines a question from a seeded BI edge, submits a change set that sets the metric's aggregation to an invalid value, and asserts the rejection is attributable to the specific mined question (via `StudioEvalResult` evidence and the submit detail), not merely a generic test failure.
- **Honest gaps**: mining is an explicit API call, not a scheduled sweep. Regression re-checking is the existing structural/shape validator (§7's "Compilation"/"Validation" rows) applied to a usage-backed snapshot — it is not the deeper "Fixture execution"/"Join validity" kind of check, and a DELETE of an object with a mined question is not itself flagged as a regression (deletes pass unconditionally today, same as any other item). This makes §7's "Regression" row real for the first time — previously nothing re-checked a *previously working* object against anything but its own edit.
