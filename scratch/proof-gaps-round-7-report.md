# Round 7: Tier 0 Invariant Suite Started (ST-03), 4 of 9 Invariants Now Enforced

You picked "start the Phase-0 refactor" over the two smaller alternatives. Given how much bigger and riskier that track is than the test-gap work — Phase 0 touches nearly every file in the codebase (splitting `models.py`/`schemas.py`/`api.py` by module, extracting `platform/`, Alembic schema-per-module migrations, removing FK constraints) — I started with the lowest-risk, highest-leverage piece the refactor plan itself calls out first: **ST-03, the Tier 0 invariant suite**. It's pure test-writing against the current code (no restructuring, no migrations, nothing to revert), and the refactor plan's own words are "the safety net that makes the rest safe" — it's the thing you want in place *before* moving files around, so a real structural change can't silently break one of the nine binding guarantees in `Docs/10-architecture/01-principles-and-invariants.md`.

## What "Tier 0" means here

Your own `Docs/40-engineering/04-testing-strategy.md` defines nine invariant tests (INV-1 through INV-9), each mapped to a named test function, and says plainly: "the invariant suite is not yet formalized as a distinct tier" (status: Partial) — that's tracker item ST-03 / TS-1, priority P0.

## What's closed this round: 4 of 9, honestly

I split the nine by whether they're provable with the no-live-infrastructure convention this whole test suite already follows, versus ones that genuinely need a running Neo4j/search/Postgres stack or a full-app endpoint sweep. I did not fake the second kind to get a green checkmark — that would be exactly the "route exists, not tested" problem this whole engagement has been about closing.

**Closed, in `tests/test_tier0_invariants.py` (22 new tests):**

- **INV-2 — one execution choke point.** `test_no_connector_execution_outside_gateway` statically scans every file under `src/aida` (via Python's `ast` module) for a call to the connectors' `execute_read_query` method and asserts `query_gateway.py` is the only caller. This is real static analysis, not a mock — it would fail today if anyone added a second call site.
- **INV-3 — model output is never authority.** `test_model_output_types_are_inert` checks both LLM-output types in the codebase (`SqlGenerationOutput`, `SemanticEnrichmentBatchOutput`): they're plain Pydantic models with no `execute`/`run`/`__call__` surface, and — the concrete proof — `QueryExecutionGateway.execute()`'s `sql` parameter is typed as a plain `str`, never one of these proposal objects, via `inspect.signature`. There's no conversion function because the type system doesn't admit one.
- **INV-4 — fail closed.** `test_production_config_fail_closed` is a 10-case parameterized test, one per rejection branch in `Settings.reject_insecure_production_configuration` — each starts from a secure production baseline and flips exactly one field. This consolidates and completes what `test_config.py` already partially covered (7 of the 10 branches existed as separate ad hoc tests); the 3 new cases are the OIDC issuer/audience requirement, the OIDC JWKS presence requirement, and the production JWKS-must-be-HTTPS requirement, which had no test before. A companion test asserts the baseline itself is valid, so a broken baseline can't silently make every rejection case meaningless.
- **INV-8 — maker ≠ checker.** `test_self_approval_denied` is parameterized across all nine governed object types `decide_governance_review` handles (semantic model version, governed tool version, model route, business-semantics proposal, glossary term version, asset documentation version, bulk stewardship operation, glossary conflict, glossary link proposal). I'd only proven this for one object type in round 5 (governed tool version); reading the function showed the self-approval check runs *before* it even looks at `object_type`, so one fake session proves it for every type without needing type-specific fixtures for each.

## What's still open, and why (not silently dropped)

- **INV-1** (`test_projection_rebuild`) and **INV-6** (`test_no_source_values_in_control_plane`) need a live Neo4j/search stack and a full ingestion pipeline to delete-and-replay or scan for sentinel leakage — Tier 3/4 integration infrastructure that doesn't exist in this sandbox.
- **INV-5** (`test_cross_tenant_denial`) and **INV-7** (`test_every_mutation_audits`) as specced require exercising *every* API endpoint and background worker with a foreign-tenant context — a running app or a much larger per-route fake-session harness, not a single test file. Real, scoped work, just a different size of task than the other seven.
- **INV-9** (`test_capability_matrix_matches_certification`) has nothing to test yet — there's no certification-result store in the codebase that capability flags could be checked against. This one may need a design decision before it can even be written, not just a test.

I'm flagging these clearly rather than either rushing shallow versions or quietly leaving the report silent on them.

## Verification (fresh clean-room build, current device state)

| Check | Result |
|---|---|
| `pytest` | **306 passed, 0 failed** (284 → 306, +22 new tests) |
| `ruff check .` | All checks passed |
| `mypy src` | Success: no issues found in 70 source files |

## Files touched this round

- `tests/test_tier0_invariants.py` (new) — 22 tests covering INV-2, INV-3, INV-4, INV-8

No production code changed — all four closed invariants were provable against the existing code as written.

## Where this leaves the Phase-0 refactor

ST-03 is now 4/9 there — a real, verifiable start, not a checkbox flip. The remaining ST items (ST-01 target structure + module template, ST-02 import-linter ratchet, ST-04 extract `platform/`, ST-05 split `models.py`, ST-06 split `schemas.py`, ST-07 split `api.py`, ST-08 untangle `intelligence_api.py`, ST-09 remove lint exemptions, ST-10 per-module test jobs) are all still `TODO` and each is a substantially bigger, higher-risk piece of work — several involve real file moves and, per the refactor plan itself, database schema migrations. I'd want to tackle those one at a time with the same verify-and-report cadence, rather than batching several into one pass, given how much more there is to break.

Nothing has been committed. Say when you want this committed, and let me know if you'd like me to keep going on the next Phase-0 item (I'd suggest ST-01, the target structure + module template, as the next lowest-risk step — it's additive scaffolding, not a move of existing code) or redirect elsewhere.
