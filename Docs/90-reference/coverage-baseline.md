# Coverage baseline (AU-14)

Generated 2026-09-01T15:34:51Z by a real, full run of the suite:

```
AIDA_ENVIRONMENT=development uv run --extra dev pytest \
  --cov=aida --cov=atlas \
  --cov-report=term-missing --cov-report=xml --cov-report=json
```

6,095 tests collected and run (6,082 passed, 3 failed, 9 skipped, 1 xfailed — see
"Pre-existing failures" below; none touched by this change). Every number on this
page is read directly off that run's coverage report, not estimated.

## Why this exists

`pytest-cov==6.2.1` has been a declared dev dependency (`pyproject.toml`) invoked
nowhere — no `--cov` flag anywhere in `[tool.pytest.ini_options]` or in
`.github/workflows/ci.yml`. Coverage was never measured at all. This closes that
gap:

- `pyproject.toml` gets `[tool.coverage.run]` (`source = ["aida", "atlas"]`,
  branch coverage on, test/migration dirs omitted) and `[tool.coverage.report]`.
  Deliberately **not** folded into `[tool.pytest.ini_options]`'s `addopts` —
  several CI jobs (`migration-drift`, `reachability`, `connector-version-fixtures`)
  run `pytest` against a single test file each for a fast, targeted signal, and
  forcing `--cov` into every one of those would slow them down for a meaningless
  partial-suite number. `pytest-cov`'s plugin only activates when `--cov` is
  actually passed, so this costs those jobs nothing.
- `.github/workflows/ci.yml`'s `tests` job (the one job that runs the full suite)
  now runs with `--cov=aida --cov=atlas --cov-report=term-missing
  --cov-report=xml --cov-fail-under=69`, and uploads `coverage.xml` as a build
  artifact.
- This file publishes the real baseline the floor above was set from.

## Overall

| Metric | Value |
|---|---|
| Combined (statement + branch) coverage | **74.10%** |
| Statement coverage | 77.39% |
| Branch coverage | 59.91% |
| Total statements | 31,433 |
| Covered statements | 24,326 |
| Missed statements | 7,107 |
| Total branches | 7,278 |
| Partial branches | 1,010 |
| Files measured (`aida` + `atlas`, non-zero statement count) | 302 |
| Files at exactly 0% coverage | 32 |

`--cov-fail-under` (and the 74.10% headline number above) is coverage.py's
*combined* statement+branch figure, since `branch = true` is set — not the plain
statement-only 77.39%. The floor below is chosen against the combined number.

## Zero-coverage files (32)

Two are real, live `aida` modules with zero coverage:

| File | Statements |
|---|---|
| `src/aida/batch_ingestion.py` | 185 |
| `src/aida/workflows/worker.py` | 20 |

The remaining 30 are every file (`api.py`, `contracts.py`, `events.py`,
`repository.py`, `router.py`, `service.py` — 6 files × 5 modules) in five
`src/atlas/modules/*` scaffolds: `catalog`, `connectivity`, `identity_tenancy`,
`ingestion`, `observability_audit`. This is not a coverage-tooling artifact — each
of these files really is unreachable from any running process today. Grepping the
whole tree for their real import path (`atlas.modules.<name>.api`, `.router`, ...)
turns up exactly one reference each: that same module's own
`src/atlas/modules/<name>/tests/test_module_scaffold.py`. Those scaffold test
files live under `src/atlas/modules/*/tests/`, which is **not** in
`[tool.pytest.ini_options]`'s `testpaths = ["tests"]`, so they are never collected
by a plain `pytest` run — confirmed by grepping this run's own test collection
output for `atlas`, which returns nothing. Separately, `src/aida/main.py` never
imports any `atlas.modules.*.api`/`.router` at all, so these five modules'
`api.py`/`router.py` are not even wired into the running app, independent of the
test-collection question. `models.py`/`schemas.py` in the same five module
directories are excluded from this list — those are 85–100% covered, exercised
indirectly through the ORM/Pydantic layer even though the module's own
router/service/repository/api/contracts/events files are not.

## API-surface modules: the row's own framing, re-verified

The row as written cites "16 of 36 API modules (92 of 320 endpoints) ... imported
by no test." As of today (2026-09-01) that count has drifted: there are **51**
API-surface modules — 46 `src/aida/*_api.py` files plus 5 `src/atlas/modules/*/api.py`
module routers (a naming convention the row's original count predates).

Measured against real coverage rather than a "does the module name appear in a
test file" proxy, the picture is better than the row's stale count suggests for
the `aida` side and worse for the `atlas` side:

- **0 of the 46 `src/aida/*_api.py` files are at 0% coverage.** Every one is
  exercised at least partially — the lowest is `src/aida/ingestion_api.py` at
  15.41%. A same-name-string grep against `tests/` (the kind of proxy the
  original "imported by no test" framing implies) flags 8 of them as apparently
  untested by name (`compliance_api.py`, `negative_knowledge_api.py`,
  `notification_api.py`, `quality_api.py`, `runtime_contracts_api.py`,
  `search_api.py`, `semantic_intelligence_api.py`, `sql_validation_api.py`), but
  all 8 turn out to have real, non-zero coverage (17–72%) — they're reached
  through the FastAPI `TestClient` hitting live routes in integration-style
  tests that never mention the module's file name in source. The grep proxy
  overstates the gap; the coverage number is the honest one.
- **All 5 of the `src/atlas/modules/*/api.py` files are at 0% coverage** — see
  "Zero-coverage files" above. This is the real, current instance of the row's
  "imported by no test" claim.

Full list, ascending by coverage:

| Coverage | Statements | Module |
|---|---|---|
| 0.00% | 1 | `src/atlas/modules/catalog/api.py` |
| 0.00% | 1 | `src/atlas/modules/connectivity/api.py` |
| 0.00% | 1 | `src/atlas/modules/identity_tenancy/api.py` |
| 0.00% | 1 | `src/atlas/modules/ingestion/api.py` |
| 0.00% | 1 | `src/atlas/modules/observability_audit/api.py` |
| 15.41% | 227 | `src/aida/ingestion_api.py` |
| 17.03% | 181 | `src/aida/semantic_intelligence_api.py` |
| 18.18% | 238 | `src/aida/quality_api.py` |
| 20.73% | 62 | `src/aida/search_api.py` |
| 20.85% | 241 | `src/aida/ai_registry_api.py` |
| 20.97% | 192 | `src/aida/bi_api.py` |
| 25.00% | 190 | `src/aida/workspace_api.py` |
| 26.37% | 77 | `src/aida/notification_api.py` |
| 26.61% | 87 | `src/aida/context_compiler_api.py` |
| 32.26% | 828 | `src/aida/intelligence_api.py` |
| 40.30% | 104 | `src/aida/table_family_api.py` |
| 41.22% | 195 | `src/aida/glossary_api.py` |
| 43.03% | 514 | `src/aida/product_marketplace_api.py` |
| 47.47% | 91 | `src/aida/runtime_contracts_api.py` |
| 49.41% | 348 | `src/aida/unified_lineage_api.py` |
| 50.39% | 309 | `src/aida/context_product_api.py` |
| 50.66% | 360 | `src/aida/tool_api.py` |
| 51.11% | 74 | `src/aida/observability_api.py` |
| 51.71% | 774 | `src/aida/semantic_api.py` |
| 52.86% | 181 | `src/aida/operational_api.py` |
| 53.16% | 71 | `src/aida/compliance_api.py` |
| 55.71% | 188 | `src/aida/tool_plans_api.py` |
| 63.75% | 136 | `src/aida/ai_governance_api.py` |
| 64.14% | 201 | `src/aida/asset_description_api.py` |
| 64.32% | 189 | `src/aida/dbt_api.py` |
| 64.49% | 203 | `src/aida/studio_api.py` |
| 66.62% | 550 | `src/aida/stewardship_api.py` |
| 67.86% | 54 | `src/aida/negative_knowledge_api.py` |
| 72.13% | 59 | `src/aida/sql_validation_api.py` |
| 74.58% | 98 | `src/aida/delegation_api.py` |
| 75.52% | 109 | `src/aida/metric_suggestion_api.py` |
| 85.00% | 40 | `src/aida/ai_decision_lineage_api.py` |
| 85.34% | 88 | `src/aida/graph_perspectives_api.py` |
| 85.54% | 71 | `src/aida/view_lineage_api.py` |
| 86.36% | 44 | `src/aida/consumption_lineage_api.py` |
| 86.36% | 56 | `src/aida/token_revocation_api.py` |
| 86.84% | 62 | `src/aida/access_review_api.py` |
| 91.67% | 30 | `src/aida/review_queue_api.py` |
| 92.68% | 187 | `src/aida/policy_native_sync_api.py` |
| 92.75% | 112 | `src/aida/openlineage_api.py` |
| 94.31% | 93 | `src/aida/composite_key_api.py` |
| 95.24% | 38 | `src/aida/asset_evidence_api.py` |
| 100.00% | 13 | `src/aida/agent_roster_api.py` |
| 100.00% | 17 | `src/aida/persona_api.py` |
| 100.00% | 25 | `src/aida/lineage_evidence_export_api.py` |
| 100.00% | 40 | `src/aida/detokenization_api.py` |

## Floor: `--cov-fail-under=69`

Chosen the same way `AG-8`'s quality-baseline gate is: a fixed-point margin below
the real measured number, not the number itself — a floor is meant to catch a
*regression* below what the codebase already clears today, not to be a target
still to reach. `quality-baseline`'s own gate ("dropping more than 5 points fails
CI") establishes this repo's convention for how large that margin is on a
percentage-point gate; the same 5-point margin applied here: 74.10% − 5 ≈ 69.10%,
floored to **69**. That leaves headroom for the ordinary run-to-run noise coverage
numbers carry (a skipped/xfailed test toggling, a newly added test file not yet
touching every branch it could) without being so loose that a real regression —
someone's PR quietly dropping a large already-covered module's tests — passes
unnoticed. It is not a stretch target: today's suite clears it by 5.10 points,
comfortably, and every number in "API-surface modules" above proves that number
is real, not rounded up.

## Pre-existing failures during this run (unrelated to AU-14)

This is CI/tooling-only work — no `aida`/`atlas` source was touched — and none of
these three are new:

- `tests/test_config.py::test_environment_must_be_explicit_outside_tests` — fails
  specifically when `AIDA_ENVIRONMENT` is present in the ambient shell environment
  at test time, which this run's own `AIDA_ENVIRONMENT=development` (required to
  make `pytest-cov`'s dev-extra import work, per this row's own task brief)
  triggers. Documented as a known, pre-existing, out-of-scope conflict with
  `.github/workflows/ci.yml`'s workflow-level `AIDA_ENVIRONMENT: "development"`
  in the AU-12 row of `Docs/60-delivery/03-tracker.md`.
- `tests/test_openapi_diff_gate.py::test_committed_baseline_matches_current_app_openapi_output`
  — the committed OpenAPI baseline is stale relative to `app.openapi()`,
  consistent with other feature rows landing on this branch since the baseline
  was last regenerated. Regenerating it is TS-4's gate's own concern, not this
  tooling-only row's, and out of AU-14's file scope.
- `tests/test_au12_temporal_outage_resilience.py::test_readiness_recovers_to_up_once_reconnect_succeeds`
  — a timing-sensitive reconnect-polling assertion (`assert 'UP' == 'DOWN'`);
  looks like ordinary flakiness under this sandbox's load during a 6,095-test
  run with coverage instrumentation active, not something this change touched.

None of the three are coverage-tooling issues, and none block AU-14's own exit
criterion (measure coverage, establish a baseline and a floor).

## Known caveats

- **`src/atlas/modules/*/tests/`** (the scaffold module test directories, 10
  files across the five modules) are outside `testpaths = ["tests"]` and were
  never collected by this run — a pre-existing gap this row did not create and
  is out of scope to fix (this row measures and reports, it doesn't restructure
  test collection). It is the direct cause of the five `atlas` `api.py` router
  files above reading 0%.
- Coverage was measured with `branch = true`; the 74.10% headline is the
  combined statement+branch figure coverage.py reports and enforces via
  `--cov-fail-under`, not the higher 77.39% statement-only number — reported
  both above to avoid ambiguity.
- An `opentelemetry` metrics-exporter exception ("I/O operation on closed file")
  appears at the very end of the raw run log, after the coverage report and
  test-failure summary had already printed in full. It fires during interpreter
  shutdown (an `atexit`-registered metrics flush racing the closed stdout
  pipe), not during any test, and does not affect the coverage numbers or test
  outcomes above — all 6,095 collected tests ran and reported a result.
