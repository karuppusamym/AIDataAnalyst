# Review reconciliation — 2026-09-16

Baseline: `81c75d5` at the start of this cycle. [Tracker section P](03-tracker.md#p-current-execution-queue-reconciled-2026-09-11)
remains the only work-status authority; this document records how the
[2026-09-16 review](../review-2026-09-16/REVIEW.md) was folded into it and what was
deliberately not done. It does not open a second queue — two live queues is the failure
section P exists to prevent.

The review was measured against `bf9b909`. This cycle's work was measured against
`81c75d5`, which is five commits later, so the first job was separating findings that were
still true from findings the tree had already outgrown.

## Findings that were already closed before this cycle began

Three, each verified rather than assumed:

| Finding | How it was checked | Result |
|---|---|---|
| **F02** — routine evidence escapes product selection | The review's own reproduction was re-run against current source: a `ContextProductScope` with unrelated hits | `TABLE outside product: admitted=False`, **`ROUTINE outside product: admitted=False`**, and a pinned ontology narrows while an unpinned kind does not. The defect the review reproduced is closed; `cdab7a8` closed it |
| **F09** — full-suite retrieval calibration failure | `tests/test_quality_benchmark_gate.py` re-run | 17 passed. `3d553b5` closed it |
| **F08**, request-wiring half | Read `AskScreen.tsx` | `context_product_key` was already sent on the initial ask, the clarification resubmission and the retry, and the resolved version already rendered. The *seams* around it were not done — see below |

The review said so itself for F01 and F09 ("do not reimplement the same check simply
because the baseline finding remains documented here"). This is that instruction applied to
F02 and F08 as well.

## What each remaining finding became

| Finding | Row it lives on | Disposition |
|---|---|---|
| F01 — product scope does not constrain SQL execution | R11-FP12 | **Closed.** Scope moved into the query gateway, at the choke point every execution path shares |
| F02 — routine evidence escapes product selection | R11-FP12 | Already closed; the pinned-meaning contract it asked to specify is stated in `ContextProductScope.admits`'s own docstring and tested |
| F03 — running deployment behind source and schema | **R11-D17 (new)** | **Closed.** Detector built, then the deploy performed and parity verified — 8 matched, 0 drifted |
| F04 — automatic context maintenance disabled here | R11-D17's runbook | **Configuration templates only, by the user's decision.** See below |
| F05 — development posture described as production | R11-D17's runbook, R11-B10 | **Procedures only.** The readiness endpoint's own answer is that the estate is not ready; see below |
| F06.1 — routine descriptions | R11-FP08 | **Closed.** The description family extended, not duplicated |
| F06.2 — profiles and samples | R11-FP04 | **Split BLOCKED → PARTIAL.** Value-free half shipped; sample rows stay blocked on their ADR addendum |
| F06.3 — discovery coverage | R11-FP01, R11-FP02 | **Reporting closed, engine gaps named.** `PERMISSION_DENIED` now exists in code |
| F06.4 — code understanding | R11-FP07 | **Closed for bounded coverage reporting.** Parse coverage persisted; dbt macros/hooks reported as a limitation; source mapping recorded unsupported |
| F06.5 — answer evaluation | R11-FP13 | **Harness built, not run.** Thresholds are the domain owner's |
| F06.6 — operations | R11-FP17 | **Closed for what is measurable.** Scraping, rules, cost metrics, quotas and a burst harness |
| F07 — completion documents drifted | This document, and section P's row convention | **Closed for the rows touched**, normalised on touch thereafter |
| F08 — product-scoped Ask has no UI request wiring | R11-FP12 | **Closed**, including four seams the review had not separated |
| F09 — retrieval calibration | — | Already closed |
| §3 — screens worth consolidating | R11-S13 | **Five of six merges landed**, 41 destinations → 38; the relationship/cross-source merge declined on the review's own constraint |
| §5 — database coverage | R11-FP01 | **Closed.** One generated engine × six-facet matrix |

## Defects found this cycle that the review had not

The review's own standard — that it "is not an exhaustive certification of every line" —
held. Six gaps were found while closing F01, three of them more serious than the finding
that led to them:

1. **MCP `tools/call` resolved a context product and never forwarded it.** It used the
   resolved product only to filter tool eligibility, then constructed the orchestrator
   without the key, so `context_product_scope` was `None` and **neither** enforcement point
   ran on that surface. Proven against `81c75d5` in a throwaway worktree: a governed tool
   reading `retail.secret_ledger` ran to COMPLETED through a product naming only
   `retail.orders`, recording no product version on the run.
2. **`POST /v1/datasources/{id}/query-executions`** took arbitrary caller SQL with no
   product parameter at all, so a principal scoped through Ask could submit the same
   statement unscoped. F01's acceptance names "direct SQL" explicitly.
3. **Nothing verified that an eligible governed tool's tables were inside the product's.**
   Absent at all four lifecycle points — product version create/update, product version
   approval, tool version approval and tool draft creation. The exemption rested on "the
   product declared that version eligible", which selects a tool; it does not widen a table
   scope.
4. **An unresolved table reference was admitted**, because `any()` over a lossy resolution
   is vacuously false. Only the all-unresolved shape failed closed, and only on execute.
5. **A leaf-name collision across schemas produced a false refusal** — a product naming
   `retail.orders` refused its own table when a `staging.orders` also existed, and the
   post-execution check named a table the statement never read.
6. **A nested CTE shadowed a physical table out of `referenced_tables` entirely.**
   `cte_aliases` was one flat, scope-blind set over the whole statement, so a CTE declared
   inside a subquery deleted an identically-named physical table from the parsed reference
   list — escaping the catalog allowlist, the ABAC axes **and** the product boundary at
   once.

Two more, outside F01:

7. **BigQuery reported a bounded profile as `FULL`** (it set `row_count_estimate` from
   `sampled_row_count`) and **Snowflake reported a full scan as `SAMPLE`**. Both corrupted
   the observation scope that relationship approval consumes and the Relationships screen
   renders.
8. **`EvidencePane` answered one 403 with three `role="alert"`s** — it rendered its own
   denial and still mounted the subordinate panels, each of which failed and alerted again.

## Two rows were unreadable, in the way F07 describes

R11-FP05 and R11-FP06 each carried an unescaped `|` after "Original scope:", which split
the row into a sixth cell and pushed the original scope text outside the table — the same
defect [the 2026-09-11 reconciliation](23-review-reconciliation-2026-09-11.md) records
repairing on B10. Both are escaped now, and all 103 section P rows render as five cells.
This is a small thing that matters for the reason F07 gives: a status document nobody can
read in full is a status document that drifts without anyone noticing.

## Not done, and why

Each of these is a decision, not an omission.

**The deploy and the migration (F03) — done, after this document first said otherwise.** Docker Desktop
was down when the cycle's work was committed; it was started afterwards and both were performed. The
order mattered: the four parallel migrations were chained to one head, then
`tests/test_migration_orm_drift.py` was run against real PostgreSQL **before** the configured database
was touched — a failed `migrate` service holds the API down, and that gate skips silently without a
reachable PostgreSQL, so a green local run had proven nothing about them. It passed. The rebuild
repeated the `--profile full` flags so no profiled container was left on the old image, and the migrate
service applied `b7e2d9c4f158` through `c9b3e7f15a48` in order. Parity then reported **8 matched, 0
drifted**. The three surfaces the review named are all live: `context_product_key` on
`AgentAnalysisRequest`, `/v1/datasources/{datasource_id}/footprint-gaps/{kind}`, and a deployed
`Settings` carrying all 270 settings — so `run_footprint_metrics_pass`, absent from the old image
entirely, now runs at its 300s default.

**Enabling the maintenance loop (F04).** Configuration templates only, by the user's
decision. Two facts made that the right default: change-signal processing opens CRITICAL
incidents and an open CRITICAL incident fails governed tools closed, which setting the
interval back to `0` does not undo; and `metadata_change_signal` is empty, so the pass would
consume nothing until a rescan observed a change. The intervals ship documented and off,
which the configuration decisions register already records as deliberate.

**Posture and delivery (F05).** Procedures only. `GET /.../enforcement-readiness` was run
read-only — step 1 of the documented four-step migration — and **answers `ready: false`**
with three blockers: two datasources have no live source binding, so queries against them
would be refused once unresolved scope is `DENY`; unresolved-workspace requests still
proceed undecided; and the one ACTIVE workspace is still in SHADOW. The workspace itself
reports `would_be_denials: 0`, so step 2 alone would be safe, but completing the flip would
start refusing real queries. Binding those two datasources is the actual prerequisite.
On delivery: the nine queued notifications point at `http://127.0.0.1:9/` — the discard
port — with `attempt_count=5` against a `delivery_max_attempts` of 6. They are test residue
aimed at a deliberately dead endpoint, not pending business notifications, and no Slack,
Teams or webhook destination is configured at all. Enabling the worker would spend the last
attempt and dead-letter them, proving nothing; F05 asks for a *verified destination*, which
is the work that remains.

**The live answer evaluation (F06.5).** Declined by the user for this cycle. It is blocked
on two things, not one: thresholds nobody has signed off, and an enriched live datasource —
the sample source seeds three tables with no routines, lineage or ontology, so the harness
refuses rather than score the wrong estate.

**Navigation consolidation (§3, R11-S13).** Not started. It was sequenced behind F08
because both own `ui-next/src/lib/routes.ts`, and F08 was the correctness fix. The review
agrees with that order: consolidation is "worthwhile after those correctness gaps". The
recon that would drive it stands: `RETIRED_SCREEN_ALIASES` is the proven mechanism, six
merges are scoped, and `OrgPicker` is confirmed dead with its reachability allow-list entry
to be deleted in the same commit. One latent bug should be fixed with or before the lineage
merge rather than blamed on it: `UnifiedLineageScreen` reads and writes `depth` and
`direction`, neither declared in `SCREEN_QUERY_FIELDS`, so a pasted non-canonical link
silently drops both.

**The quality-benchmark baseline was deliberately not accepted.** `retrieval_recall_within_bound_rate`
now measures 0.7647 against a committed 0.7059 — an improvement from this cycle's retrieval
work, and the gate is green. Locking it in was rejected: the run reports
`path=LIVE_EMBED`, so the figure depends on an embedding provider being configured, and
ratcheting an environment-dependent number into the committed baseline would recreate
precisely the F09 defect this review had just had fixed.

**No engine gaps were closed.** Triggers and sequences remain undiscovered on all six
adapters; Databricks still declares neither views nor routines; PostgreSQL aggregate and
window functions are still not discovered; schema pushdown is still postgres and SQL Server
only; the push-ingestion path still applies no discovery selection. §5 asked for an honest
matrix, not for these to be closed, and the published matrix names every one of them as
unsupported with its reason — which is what makes the deferral honest rather than silent.

## Deferred and cancelled rows: unchanged

Every disposition in the review's §4 table was accepted as written. No deferred row was
restarted and no cancelled row resurrected. In particular: S6 and X7 stay CANCELLED, P5
stays deferred (migration history was chained, not squashed or reset), B16 stays deferred,
and C12 stays parked — no database candidate was added.

## Evidence run in this reconciliation

| Check | Result |
|---|---|
| `ruff check src tests scripts` | Clean (three errors found and fixed, one a missing import) |
| `mypy src sdk/aida_tool_sdk`, strict | Success, **387** source files (371 at the review) |
| `alembic heads` | **One** head, `c9b3e7f15a48`, after chaining four parallel revisions |
| Backend, targeted across every touched subsystem | 5,185 passed, 0 failed, incl. the doc-claims, capability-register and reachability gates |
| New tests for this cycle's work | 373 passed |
| Frontend `npm run test` | 96 files, **859** passed (843 at the review) |
| Frontend typecheck and production build | Pass |
| `scripts/openapi_diff.py` | Seven new paths and three new response fields; **no breaking changes** |
| `scripts/generate_ui_types.py` | Regenerated, 516 schemas, matches |
| `scripts/quality_benchmark.py` | Green; no regression beyond 5.0 points |
| `GET /.../enforcement-readiness` | `ready: false`, three blockers — read-only |
| `tests/test_migration_orm_drift.py` | **Passed** against real PostgreSQL once Docker was started — all five migrations apply to an empty database and the result matches `Base.metadata` |
| Deploy, migration and `scripts/check_deployment_parity.py` | **Performed** — parity 8 matched / 0 drifted / 0 not measured; 17 services healthy |
| Playwright browser journey | **14 passed** against the real production nginx image and the journey stub, including the three new product-scoped Ask steps |
| Change-burst latency harness | **Not executed** — authored, and no measurement is claimed |

Passing tests do not establish deployment parity, answer quality, or any of the
customer-specific release evidence the review's §7 gate names. The full backend suite was
still running when this document was written; its result is recorded separately rather than
predicted here.

## Follow-on cycle — 2026-09-17

The review's own findings were closed on 2026-09-16. This cycle worked **section P's
remainders**, which is the tracker's job rather than this document's, so what is recorded here
is only what a future reader would otherwise have to reconstruct.

### Two features were half-built, and nothing was failing

The most useful thing this cycle produced is not a feature. Two workstreams ended with a
feature that looked complete and was not:

- **Trigger and sequence discovery** built the models, the migration, the per-engine queries
  and 66 tests — and nothing persisted a row. A scan discovered triggers, built them, and
  dropped them on the floor.
- **The per-facet refusal mechanism** was complete, wired into the receipt, the gap register
  and the reconciliation — and no adapter called it. A real source's refusal still failed the
  whole run.

Neither gap failed a test, and neither could: a feature that discovers and discards is green
all the way down, and a mechanism nobody calls is green too. They were found by reading the
handover notes, because both agents wrote plainly that they had stopped at a file boundary
someone else owned. That is the only reason both were closed the same day, and it is the
argument for those notes being part of the work rather than a courtesy.

Three **reverse-guards** — tests deliberately written to assert a gap exists and to fail when
it closes — did exactly their job and were flipped to positive assertions.

### Deliberate decisions, so they are not re-litigated

- **Sample rows (R11-FP04)** — an ADR-0014 addendum is now drafted and marked **NOT ADOPTED**,
  so the block has a stated exit instead of none. It is honest that whole rows of arbitrary
  columns are a categorically larger decision than the one-temporal-value freshness addendum it
  follows, and that the value-free half already shipping means the product does not need it to
  work. Adoption is Architecture and Data Governance's, not this cycle's.
- **FP13 thresholds** — proposed with reasoning rather than copied from a measurement: 1.0 for
  the three safety-shaped metrics (a refusal for the wrong reason is not a correct refusal), 0.9
  for evidence support because it measures retrieval, which this repository already treats as
  tolerance-bearing. Every `minimum` is left null pending sign-off, and the three metrics that
  need the paid live run are left unproposed — proposing a number for a metric nothing has
  measured is the failure that file warns about.
- **`AIDA_WORKER_METRICS_PORT` was not set.** 12 of the 24 alert rules have no data until it is,
  because the registry is per-process. Compose's own comment says opening a port is the
  operator's decision, and this is one developer's machine rather than a monitored estate.
- **The quality-benchmark baseline was again not accepted** despite the metrics sitting above
  it, for the same reason as last cycle: the run reports `path=LIVE_EMBED`, so ratcheting in an
  environment-dependent figure would recreate the F09 defect.
- **The relationship/cross-source merge stays declined** on the review's own constraint.

### A defect in the shipped bootstrap (R11-D18)

Two sessions hit it independently, an hour apart, each attributing it to the other. `.env.example`
ships a `credential_reference` target that is deliberately not a `Settings` field; the env source
drops such a key before validation but the **dotenv** source hands it to the model, where
`extra="forbid"` refuses it. So `cp .env.example .env` produced a config that raised for every
host-side script. It shipped because the existing test loaded the template into the *environment*
and passed `_env_file=None` — exercising the source that always worked, never the one that was
broken. Worth recording as a shape: a test that covers the mechanism rather than the documented
path can be green while the documented path is broken.

### Evidence

| Check | Result |
|---|---|
| Backend suite | **11,653 passed, 0 failed**, 169 skipped, 1 xfailed |
| Frontend | **921 passed across 100 files**; typecheck and build clean |
| mypy strict | 388 source files |
| Import contracts | 12 kept, 0 broken |
| Alembic | one head, `d4a8c1e6b520`; migration/ORM drift green against **real PostgreSQL**, run before the configured database was touched |
| Live-engine evidence | triggers and sequences created, scanned, dropped and rescanned on PostgreSQL and SQL Server; a genuinely refused facet on PostgreSQL with the run completing |
| Deployment | redeployed; parity **8 matched / 0 drifted**, 17 services healthy, the new tables present and 12 native object kinds answering on the live capability matrix |
| Change-burst latency | measured, and the answer is a split — see R11-FP17. It proves the shape of the degradation and no magnitude at any target scale, so **R11-B15 is untouched** |
| Playwright journey, paid answer evaluation | not run this cycle, and not claimed |

One frontend a11y case failed in the first full run and passed alone and in its own file; it was
CPU contention with the concurrently running backend suite, which has a per-case time budget. The
921 above is from a clean run.
