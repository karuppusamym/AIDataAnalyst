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

Studio is **entirely unbuilt**. Current authoring is form-based inside the Atlas portal: metric composer, tool authoring, and business-meaning review exist as individual screens with no change sets, no tests, no diffs, and no version control.

| Capability | Now | Target |
|---|---|---|
| Metric composer | Implemented (form) | Move into Studio with diff and test |
| Tool authoring | Implemented (form) | Parameter-contract designer with test harness |
| Change sets | Not implemented | Core Studio primitive |
| Test harness | Not implemented | Required before submission |
| Diff view | Not implemented | Required for reviewers |
| Impact preview | Partial (module 09) | Integrated into submission |
| Git binding | Not implemented | Optional, Phase C |

## 13. Open work

| ID | Item | Priority |
|---|---|---|
| ST-1 | Change set primitive with conflict detection | P1 |
| ST-2 | Test harness with synthetic fixtures | P1 |
| ST-3 | Diff view for semantic objects | P1 |
| ST-4 | Parameter-contract designer | P1 |
| ST-5 | Impact preview at submission | P1 |
| ST-6 | Git binding with Atlas-authoritative sync | P2 |
| ST-7 | Context product builder | P1 |
