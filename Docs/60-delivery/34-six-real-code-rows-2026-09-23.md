
# Six of the eight real-code PARTIAL rows, advanced: 2026-09-23

A dated addendum to the [FP12 closure](33-fp12-closure-2026-09-23.md): evidence, not a queue
(`CLAUDE.md`, rule 2). Status lives in
[tracker section P](03-tracker.md#p-current-execution-queue-reconciled-2026-09-11).

**Asked for:** pick up the remaining PARTIAL rows with real code left (FP01, FP03, FP07, FP09, FP12,
OKF02, REV01) and get as far as possible, one at a time, each in its own isolated worktree, verifying
the row's own claims against the current code before building.

**FP12 excluded from this batch.** A sibling session ("End-to-end validation and gaps") was
independently building a broader FP12 implementation (a batched changes-since-published/pinned-meaning
count route, plus badges on four screens -- Context products, the agent gateway exposure list, Ask's
product picker and Rollout's version list -- more surfaces than this session's own FP12 pass covered)
at the same time, discovered by a cross-session message mid-integration. Since both implementations
touch `context_product_api.py`, `context_product_coverage.py`, `schemas.py` and
`ContextProductsScreen.tsx`, shipping both would collide; the sibling's is the more complete answer for
that row, so this session's own FP12 patch was dropped and never applied. FP12's closure at `0349e40`
is that session's own commit; this addendum is built on top of it.

## What changed, one row at a time

**R11-FP01** (still PARTIAL, shorter remaining list). SQL Server and Oracle triggers (own-bodied,
`action_routine IS NULL`) are now retrieval candidates, admitted the same fail-closed way R11-D23
established: `src/aida/retrieval.py` gained a BM25-scored candidate block over `MetadataTrigger`, with
the firing table resolved case-insensitively; `src/aida/agent_orchestrator.py`'s
`ContextProductScope.HIT_TYPE_RULES` gained a `TRIGGER` entry reusing the existing `_owning_table` rule
(a trigger fires on exactly one table, so no new reference-group field). 43 new/updated tests, 394
further regression tests, 2 mutation checks caught. Two judgement calls recorded on the row rather than
applied silently: the trigger-sweep cost (bounded, sequential per datasource, not parallelised) and
keeping the propagation gap's routing at EXPLAINED rather than HUMAN_REVIEW (no dedicated queue exists
for it). Still open: the TRIGGER candidate is lexical-only, no vector/graph reach or description boost.

**R11-FP03** (still PARTIAL, two of four remaining items closed). Re-measured the package-wide
hop-dedupe issue against R11-FP07's `a721712` fix as instructed -- the fix did not close it as a side
effect, only made the collision reachable -- and found the real root cause: `propagate_intermediate_hops`
deduped `(source, target)` across every member's edges with no per-member key, silently keeping only the
first member's fact when two collided. Fixed with `_propagate_hops_per_member` in
`src/aida/routine_call_descent.py`, restoring the per-member scoping `parse_procedure_lineage` already
uses. Also fixed: a callee reached via descent that is itself a package with unresolved sibling-call
ordering now reconciles at every recursion depth, not only the root. Two items verified stale/out of
scope and left alone: three-part names on non-Oracle dialects (no non-Oracle connector ever emits a
PACKAGE routine, so nothing to disambiguate), and expression calls to a non-special-cased builtin
function (a real gap, but already documented as an accepted trade-off, and closing it needs an
open-ended per-dialect builtin list judged disproportionate for this pass). 235 tests in the FP03/FP07
suites, 377 in a wider lineage sweep, both fixes mutation-checked.

**R11-FP07** (still PARTIAL, one bug fixed, one verified-but-left-open). Fixed a real alias-shadowing
bug in `src/aida/procedure_lineage.py`: `_collect_table_aliases_with_temp` walked an UPDATE/MERGE
statement's *entire* tree for aliases, so a same-named alias inside a nested `WHERE`/`EXISTS` subquery
silently overwrote the statement's own target alias, resolving the write (and any column read through
it) to the wrong table. Rescoped to the statement's own FROM-scope, mirroring the per-column-resolution
discipline `procedure_column_owners._Resolver` already uses. Confirmed fail-before/pass-after by hand
in both directions (checked out the old file, watched the new test fail; reapplied the fix, watched it
pass). 597 tests across the lineage/trigger/routine neighbourhood, no regressions. Separately verified
as real but left open: a semicolon-free T-SQL body with a second statement after
`SET @v = (SELECT ...)` collapses into one `UNPARSED` chunk -- reproduced, not fixed this pass.

**R11-FP09 -- closed, PARTIAL to DONE.** Its own remaining text, "package-member mappings, which wait
on FP03," was stale in exactly the way R11-FP08 turned out to be: R11-FP03's earlier work already makes
every package member a plain `MetadataRoutine` row with `package_name` set, and none of the four places
an ontology mapping travels through (`ontology_api.py`'s create/submit/list validation,
`context_product_api.py`'s reference validation and routine picker, and
`context_product_coverage.py`'s two meaning-loading paths for the compile and MCP doors) filters on
`package_name` or `routine_type`. Proved, not assumed: `tests/test_r11_fp09_package_member_ontology_mapping.py`
(2 tests) maps a package-member routine and a standalone one to the same concept and confirms identical
behaviour through validation, compile and MCP; a mutation check (temporarily filtering `package_name`)
made both fail as expected, then was reverted. 44 further regression tests, 0 failures.

**R11-OKF02** (still PARTIAL, one question answered, one measured, one blocked). Answered, with proof:
whether a scan touching only `metadata_table.updated_at` forces a no-op re-freeze. It does not --
`_get_or_create_table`'s unconditional field reassignment is idempotent at the SQLAlchemy flush-history
level (no UPDATE is issued on an identical second call, confirmed via a SQL-capturing test), and the
digest-equality guard in `_serve` independently confirms a no-op even if `updated_at` moved on its own.
Two new tests in `tests/test_okf_source_bundles.py`, both mutation-checked; 109/109 in the OKF suites,
no regressions. Measured, not guessed: the per-schema gate's cost, ~11-18 ms/schema, roughly linear
(11 schemas: 143-193 ms; 501 schemas: 5.63-5.70 s), on in-memory SQLite in one process -- a real
Postgres-over-network estate could differ, stated as a caveat. Attempted and honestly reported as
blocked: walking a binding revocation through the browser against the shared dev stack, stopped when the
API container restarted mid-attempt under a concurrent peer redeploy -- not fabricated, not retried
against a moving target.

**R11-REV01** (still PARTIAL, two of seven remaining items closed). Closed: stored playbook dry runs
had no retention -- `src/aida/reaper_service.py` gained a `stale_playbook_dry_runs` rule (90 days,
reusing the existing generic sweep, no migration). Mitigated, not fully closed, and said so plainly: "a
member that fails the same way every time blocks each resume at that member." The transactional design
treats any exception as a crash-safe interruption by nature, which is right for a real crash and wrong
for a deterministic bug that will just fail the same way on every resume; a durable, cross-process fix
would need a schema migration and doesn't fit the module's SQLite/StaticPool test harness. Implemented
an honest, bounded version instead: `decide_review_batch` now remembers, in-process only, which member
last failed unhandled, and a second consecutive identical failure raises `REVIEW_BATCH_STUCK_AT_MEMBER`
(409) rather than repeating the same raw crash -- a genuine fix still resumes and clears the marker. 85
tests across the reaper and review-batch suites, both new behaviours mutation-checked, the sanctioned
real-PostgreSQL resume test re-run and still green.

## A shared infrastructure defect, found and worked around

Several of this batch's worktrees were created from a stale, diverged commit (`cb2a47a`, "Merge pull
request #36") instead of the real branch tip -- not a few commits behind, but ~250-390 commits behind
on a different fork entirely, missing whole features (FP03's own work, the GraphQL layer, R11-FP12).
One agent (assigned FP12, before it was dropped from this batch) caught it and stopped rather than build
on a broken base; the others caught it themselves and either `git reset --hard`ed their own worktree's
branch to the real tip (verified no work was at risk first) or created a fresh local branch there. None
of this touched the shared main checkout or any peer's ref. Flagged here because it will recur for any
future isolated-worktree agent on this repository until the underlying cache is fixed.

## Checks

| Check | Result |
|---|---|
| Static gates on the merged batch (ruff, mypy --strict, lint-imports) | clean after two integration-time fixes: a variable-name collision `retrieval.py`'s new code introduced with an unrelated existing variable in the same function (renamed to `firing_table_rows`), and four cosmetic ruff findings across three new test files (line length x2, import order, one S608 false positive on fixture-only SQL text, restructured to a `.format()` template) |
| Generated artifacts | OpenAPI baseline and `ui-next/src/lib/types.ts` needed no further regeneration on top of FP12's own (none of these six rows touch a response schema); the shim register and the architecture map were stale from FP01's new code paths and were regenerated once, on top of FP12's committed state |
| `tsc --noEmit` | clean (no UI files in this batch) |
| `scripts/check_docs_links.py` | clean |
| `tests/test_doc_claims.py`, `tests/test_capability_register_claims.py` | clean after one fix: the OKF02 row's note named two docker container ids in backticks (aida-platform-api-1 / aida-platform-ui-next-1), which the doc-claims gate's import-linter-contract scanner reads as a citation whenever a backticked hyphenated name shares a physical line with the word "contract" (the row has one elsewhere) -- reworded to plain text rather than loosening the gate |
| Full backend suite, bare, on the merged tree | run separately; see the tracker recount line |
| Full UI suite, alone | not re-run for this batch (no UI files changed) |

## The counts, after this

98 DONE, 21 PARTIAL, 7 BLOCKED, 0 TODO, 22 DEFERRED, 2 CANCELLED over 150 rows; 28 of the 126 active
rows are open. Since the last recount: R11-FP09 closed; R11-FP01, FP03, FP07, OKF02 and REV01 each
advanced with real, verified code and stay PARTIAL with a shorter, honest remaining list.
