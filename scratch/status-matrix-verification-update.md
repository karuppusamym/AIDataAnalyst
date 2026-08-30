# Verification Update — In-Progress Fixes Reviewed and Confirmed

> Follow-up to `status-matrix-verification.md`. This reviews the uncommitted work already sitting
> in the working tree (`git status`) against the gaps that audit identified.

## What I checked

Bundled the current **uncommitted** working tree (not the last commit) into a clean
`python3.13` venv and ran `pytest`, `ruff check .`, `mypy src` from scratch.

| Check | Result |
|---|---|
| `pytest` | **237 passed, 0 failed** (226 → 237: +11 new tests, all pass) |
| `ruff check .` | All checks passed |
| `mypy src` | Success: no issues found in 70 source files |

## The two new test files, verified line by line against the source they exercise

`tests/test_high_stakes_behaviors.py` and `tests/test_operational_behaviors.py` are real,
targeted fixes for six of the specific proof gaps from the original audit — not more
route-existence tests. I traced each one back to the actual source change:

| Gap from the original audit | Fix found | What it actually verifies |
|---|---|---|
| GL-4 "coverage scoring" — only test was a route-existence assertion | **New** `build_stewardship_coverage()` extracted from `stewardship_api.py`'s `_coverage` handler into `stewardship_service.py` as a pure function | `test_stewardship_coverage_scores_actual_evidence_and_reports_unowned_tables` feeds it real evidence sets and checks exact numbers (`overall_score == 58.33`, `unowned_table_ids == [second]`) — genuinely computes and checks the score, not just that the route exists |
| Catalog tombstoning — zero references to "tombstone" anywhere in the codebase | `activities.py`'s deprecation logic refactored: new `missing_snapshot_scope()` pure diff function, `_deprecate_missing` now builds its UPDATE statements from that diff instead of inline conditional subqueries | Two tests: one checks the set-diff logic directly, one (`test_catalog_tombstoning_executes_updates_for_each_missing_object_level`) actually **compiles the generated SQLAlchemy UPDATE statements** and inspects their bound parameters to confirm only the missing IDs are targeted, never the observed ones |
| Temporal durability (heartbeats/resume) — "temporal" appeared exactly once in the whole suite, disabling it | No source change here — `DatasourceDiscoveryWorkflow` itself was already written, just never exercised | `test_discovery_workflow_heartbeats_retryable_stages_and_aggregates_profiles` monkeypatches `workflow.execute_activity` and runs the **real workflow class**, confirming it calls `profile_table_task` once per planned table, aggregates the results correctly, and sets `heartbeat_timeout`/`retry_policy` on every stage. `test_resume_creates_a_linked_run_and_emits_audit_and_outbox` exercises the resume-a-failed-run API path and checks the audit/outbox evidence it emits. |
| Fleet scheduling — only "maintenance windows" had tests; priority/quotas/backpressure/admission were untested | No source change — `scheduler.process_scan_policy` existed, untested | Two tests cover the commit-before-dispatch ordering, audit/outbox content, and — importantly — the **admission-rejection path** (`RunAdmissionRejected` → no dispatch, retry backoff applied, no evidence emitted) |
| Org tenancy — "organization enforcement" had zero dedicated test | No source change — `enforce_organization` existed, untested | `test_tenant_boundary_denies_cross_organization_access` / `test_platform_admin_can_operate_across_organizations` directly test the enforcement function, including the `PlatformAdmin` cross-org exemption |
| Neo4j FK graph projection — "neo4j" appeared nowhere in tests | No source change — `graph_projector.project_discovery` existed, untested | `test_discovery_projection_builds_inventory_hierarchy_and_references` captures the actual Cypher `MERGE` calls issued and checks the `HAS_SCHEMA`/`HAS_TABLE`/`HAS_COLUMN`/`HAS_CONSTRAINT`/`REFERENCES` edges are built correctly from a projection payload |

**Assessment: this is real work, not test-theater.** Every one of the six closes a specific,
named gap from the audit with a test that exercises actual logic — computed scores, compiled SQL
statements, workflow control flow, or captured Cypher queries — not a
`assert "..." in paths` check. I found no regressions, no weakened assertions, and no case where
a test was written to match whatever the code happened to do rather than what it should do (the
tombstoning refactor in particular is a genuine improvement: the old inline-conditional version
was correct but unreadable and untestable; the new version separates the pure diff from the SQL
construction, which is why it could be tested at all).

## One loose end, not yet addressed

The original gap note for catalog inventory paired "tombstones" with **reactivation** — a table
that disappears from a scan and later reappears should un-deprecate. Tombstoning is now real and
tested; I found no matching reactivation logic or test anywhere in the current tree. Worth a
follow-up if that half of the claim still needs to be true.

## Also present but out of scope for this pass

The working tree also has an unrelated, substantial UI refactor: `ui/app.js` (1508 lines,
down from ~1508+564 originally — a features/ split has begun) and `ui/styles.css` (now 6 lines,
`@import`-ing five files under a new `ui/styles/`) have been decomposed into
`ui/scripts/{core,api,virtual-table}.js` and `ui/scripts/features/{integration-policy,
transformation-workbench}.js`. I checked the wiring only: `index.html`'s `<script>`/`<link>` tags
and `ui/Dockerfile`'s `COPY` lines both correctly reference the new files, so it isn't obviously
broken — but I did not render it in a browser, so I can't confirm it actually works end to end.
Separate task if you want it verified.

## Bottom line

Nothing to fix — the in-progress changes are correct, tested, and clean (ruff/mypy both pass).
This is safe to commit as-is.
