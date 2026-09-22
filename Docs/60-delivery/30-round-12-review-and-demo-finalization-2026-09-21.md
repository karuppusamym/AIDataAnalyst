# Round-12 review, the reviews' last leftovers, and the final live run before the demo: 2026-09-21 (evening)

A dated session log: evidence, not a queue (see `CLAUDE.md`, rule 2). Status lives in
[tracker section P](03-tracker.md#p-current-execution-queue-reconciled-2026-09-11), dated evidence in
the [capability register](20-capability-register.md). It continues the
[morning's validation log](28-demo-readiness-validation-2026-09-21.md) in the same session.

**Asked for:** validate end to end, confirm the gaps and fixes across the reviews, report anything
pending or deviating, log everything, and get the product ready to demo; then "proceed and get
things fixed properly", including reading the secret-scan report (download approved) and pushing
once green.

**Baseline and end point.** Started at `1d11cca`: the morning's log commit `c652fc5`, then round
12 (`168ed19`, `1d11cca`), the session that owns the AUD rows landing its work, which this pass
reviewed. Ended at `9128241` for code
and the commit carrying this log for documents. The local stack was rebuilt twice from clean
worktrees, at `80ad325` and at `9128241`, and runs `9128241`.

**Two things about this branch that shaped the work.** Another tool commits the whole working tree
of the main checkout with `feat:` messages: `f0793e2` swept up this session's half-finished
scheduler, OIDC and body-limit edits, without their tests or regenerated artifacts, and HEAD did
not pass until `cbf2b23` finished them 10 minutes later. From then on every change was made in a
clean worktree of HEAD and the branch moved by a guarded `update-ref` (old SHA checked). And
`94f307d` (R05, a peer) landed during the pass; its row edit is kept as written.

## 1. The round-12 review

Four read-only reviews ran over `c652fc5..1d11cca` (round 12): identity, backend, UI and
documentation claims.
Every finding below was re-measured before it was fixed or written down.

| Finding | Fix | Commit |
|---|---|---|
| A 4xx, malformed or empty key-set answer from the identity provider was served stale like an outage, so a provider that *withdrew* every key kept old ones valid for the whole grace window | Only an outage (5xx, 429, transport) is served stale; a rejection (`JwksRejected`) fails closed past expiry. A request arriving while a refresh is in flight is answered from a servable stale set instead of queueing for the whole fetch timeout | `f0793e2`, `cbf2b23` |
| The drafter's up gauge stayed 1 when the broker dropped after the start: aiokafka retries internally and never raises into the loop, so the consumer-down alert could not fire | A metadata probe every 30 s (10 s timeout) sets the gauge to 0 while the broker does not answer, one log line per change | `cbf2b23` |
| A pass failing on every iteration was isolated and counted but invisible without the logs (R11-VAL04's open half) | Outcomes persisted per pass, `GET /v1/operations/scheduler-passes`, the Operations panel, `AtlasSchedulerPassFailing`, and a per-pass backoff | `f0793e2`, `cbf2b23`, `dc0496d` |
| `POST /mcp` had no API-side body cap, and any route with a body model read the whole body behind nginx's 1 MiB only (R11-AUD11) | `RequestBodyLimitMiddleware`: a declared length over the route's limit is refused before anything reads the body, a streamed one at the first chunk past it | `f0793e2`, `cbf2b23` |
| Two concurrent parses of one routine raced to a 500 on the unique constraint | 409 with "retry the parse" | `f0793e2` |
| The Studio screen submitted a change set with no confirmation; links offered screens a role is refused; a stale "Load more" reply could overwrite a newer list | ConfirmDialog, `useMayOpenReviewQueue`, `CATALOG_ROWS_ROLES` gating, a reply guard | `c483efa` |
| A comment said the compliance download withheld evidence from a Viewer; the download holds only aggregate counts a Viewer can already read | The comment says so, and what must change first | `c7d1774` |
| The browser journeys failed on round 12's Coverage tab: the stub synthesized a placeholder body the view cannot render | Realistic bodies pinned in the stub; the journey walks all four tabs | `80ad325` |
| 17 documentation claims no longer matched the code (counts, digests, the stack's build, a garbled item in log 28) | Corrected in the commit carrying this log (section 4) | this commit |

## 2. The reviews' last leftovers

| Row | What was left | Now | Commit |
|---|---|---|---|
| R11-VAL05 (2026-09-05 R03) | `atlas.modules.*.schemas` imported `ApiModel` from `aida.schemas`, which re-exports them | `ApiModel` lives in `atlas.platform.schemas`. The first form, an assignment, and then an alias, hid every model from mypy's pydantic plugin (3,001 errors); the import form keeps full `mypy src` clean | `59a1665`, `846f5df` |
| R11-VAL05 (2026-09-11 X3) | Three unreferenced symbols | `auto_lift_on_material_change` and `DisabledModelGateway` removed in `f0793e2`; `compute_worklist` and its two helpers in `cb4c767` | `f0793e2`, `cb4c767` |
| (none: found here) | `shim_register.py --check`, a CI gate, red since `846f5df` added two lines to `aida/schemas.py` | Regenerated | `cb4c767` |
| R11-AUD03 | A consumer killed by one message over and over, which the gauge can miss, with no alert | `aida_newly_created_table_drafter_starts_total` and `AtlasNewlyCreatedTableDrafterRestarting`; the monitoring README's placeholder table also gained `dc0496d`'s pass-failure alert, which it had missed | `fc53576` |
| R11-AUD08 | A column hit in search named neither its table nor its datasource, so none could open | The hit carries both and `table.column`; live, a `customer_id` hit opened the Catalog on its `account` table | `9128241` |
| R11-AUD08 | Coverage by domain or line of business, and a reviewed bulk operation created directly, have no screen | Queued as **R11-VAL06** (not on the demo path) | this commit |
| R11-VAL01 | 8 gitleaks findings the log did not name | The report (downloaded with your approval) names all 8: gitleaks' default `generic-api-key` rule on `key` and `subject_key` fields of test fixtures, none a credential, so nothing to rotate. The four values are allowlisted exactly, not the files, so a real secret pasted there still fails the job; the CI job passed on `f0793e2` (run 35678470343). **DONE** | `f0793e2` |

## 3. Results

### Gates

| Gate | Result |
|---|---|
| The 18 static CI gates, run locally at `cb4c767` (ruff, lint-imports, alembic heads, the proxy contract, image packaging, OpenAPI and UI-types diffs, the docs links, the shim register, the architecture map, the destination inventory, frontend reachability, the parity gate's source side, the reachability gate, migration drift against real PostgreSQL, `mypy --strict` on 430 files, the perf and quality baselines) | **18 of 18 pass**; the shim register failed at `80ad325` and was fixed in `cb4c767` |
| Backend `pytest`, whole suite, bare, from a clean worktree of `80ad325` | **15,177 passed, 184 skipped, 2 failed** (54 minutes, under the live batch's load). Both failures were in tests and are fixed: the configuration inventory, which `cbf2b23` left stale (the scheduler-pass code reads `scheduler_poll_seconds` twice more), in `e1c11b5`; and the F17 metric test, which assumed no earlier test had created the `__unmatched__` label. The body-cap tests now create it, because a request the cap refuses never reaches routing and is counted under that constant label, which is what F17 wants. Fixed in `b21eb24`. The six files CI failed, run together at `e1c11b5`: 193 passed. A confirming full run at `b21eb24` was started as this log was committed, and CI runs the suite on the push |
| Tests of the areas changed after `80ad325`, at `9128241` | worklist 56, monitoring and drafter 49, search 3 (two fail on the old handler), all passed; mutants caught for each new assertion |
| UI `vitest run`, whole suite, alone | **148 files, 2,147 tests passed** at `b21eb24`; `tsc` clean |
| Playwright journey suite (isolated stub stack) | **46 passed** at `80ad325`; the later UI changes are comments, one test title and a demo-fixture field, and were not re-journeyed |

### The running stack, rebuilt from a clean worktree of `9128241`

| Check | Result |
|---|---|
| `check_deployment_parity.py`, from the worktree and from the main checkout | **9 of 9**: schema `c4a7e2d9b815`, 441 paths, 581 schemas, 290 settings, source digest `e9d0b82ae652`, built from `9128241` |
| `live_role_sweep.py` (every GET route as 12 roles) | 208 routes, 2,496 calls, **0 server errors** |
| `live_role_matrix.py`, the sixteen platform roles | 5,547 probes, **0 failures**, the four known NARROWED draft-list rows |
| `live_role_matrix.py`, the seven non-admin demo bundles | 2,405 probes, **0 failures** |
| `live-a11y-audit.mjs` on `:3001` | 40 screens, **0 violations** in light and dark, no sideways reflow, 0 of 6 opened states with violations |
| `demo-rehearsal.mjs`, the eight demo users from empty browsers | **50 screens, 0 with an issue**, the same 5 known conditions |
| `GET /v1/operations/scheduler-passes` | 25 passes, 0 failing, 0 stale, 0 never run as Operations; 403 as a Viewer |
| API body cap on port 8000 | 2 MiB declared to `POST /v1/security/tokens/revoke` with no identity: 413 before authentication |
| Search | `customer_id` as COLUMN: every hit carries `table.column`, its datasource and `table_id`; clicking `account.customer_id` opened the Catalog on `account` |
| Container logs since the rebuild, counted and never printed | 0 error markers in four of five backend containers and one false positive in the scheduler (`reaper_pass_complete`), 0 key-shaped strings; `scheduler_became_leader` and `newly_created_table_drafter_started` once each |

The worker runs with its metrics port off on this stack (`AIDA_WORKER_METRICS_PORT=0`), so the
drafter's new counter was checked against the stub consumer only. The migration
`c4a7e2d9b815` (the scheduler-pass table) was applied to the development database by this pass's
first rebuild; no other migration was applied, no production configuration changed, nothing was
written to the estate (every write probe went as a refused role), and no question was put to a
model.

### Continuous integration

The last pushed commit before this pass pushed was `f0793e2`, pushed by the tool that made it. Its run (35678470343) passed 15 jobs, including **Secret scan (gitleaks), for the first time since 2026-09-18**, and failed 5: Tests (14 failures), Lint, types and architecture, the ui-next type generation gate, Documentation links (the shim register) and the browser journey. Each failure was re-run at `9128241` or later. Twelve of the test failures and the four other jobs came from `f0793e2`'s unfinished state (stale generated artifacts, the ruff import order, the Coverage stub) and were fixed in `cbf2b23`, `59a1665`, `846f5df`, `80ad325` and `cb4c767`; the other two in `e1c11b5` and `b21eb24`. This pass's commits are pushed with this log. Their CI run is reported in the session, not here, because a log cannot carry the result of its own push.

## 4. Documentation brought back to the code

In the commit carrying this log: section P (the 83/33 confirmation line no commit ever had, now
74/38 at `c652fc5`; a recount for tonight; VAL01, VAL04, VAL05 and AUD03 to AUD11 updated; OKF01's
closure sentence; stale line references in AUD03 and AUD04; AUD01's run-on "Not done"; seven
BLOCKED rows, not five, in the 2026-09-12 note; the new VAL06); the capability register's
sixteen-role, parity, drafter, search, OIDC, body-cap and scheduler rows; `00-status.md` (its date,
counts, priorities and the validation-log link `168ed19` dropped); and the walkthrough pack
(`readiness.html`, `index.html`, `demo-script.html`, `roles-and-users.html`: counts, the stack's
build, 25 alerts, 50 screens, alex holding all sixteen roles). Item 5 of log 28's section 5 had been
garbled by an edit collision when it was committed; its text was repaired with no change of claim.

## 5. Still pending, and whose call it is

**Yours (the owner's), before or at the demo**

1. **Rotate the Gemini key** that reached the scheduler's log before R11-AUD14: set the new one in
   `.env` and recreate the containers that read it in the same step, or the running API keeps the
   old one.
2. **Whether live Ask is part of the demo.** It sends the prompt to a real provider (Gemini, at a
   cost), and the approved route is labelled residency `development`.
3. **Decisions taken on your instruction to proceed**, each reversible by editing its row: the
   sixteen-role catalog and Auditor access to compliance packs (AUD01), ADR-0030 accepted (AUD06),
   the 15-minute window in which a withdrawn signing key still verifies while the identity provider
   is unreachable (AUD10), three older rows deferred (VAL02), the scheduler-pass panel's place on
   Operations (VAL04), deleting `auto_lift_on_material_change` (a rejected predicate that returns stays
   suppressed until lifted, VAL05), and allowlisting the eight fixture values by value rather than by
   path (VAL01).

**Still open in the queue:** 36 of the 126 active rows in section P. Among them: R11-AUD11 (the upload read timeout, unmeasured), R11-VAL06, C2, B9, B10 and I1, the FP01 to FP09 and FP12 to FP17 remainders, and the seven BLOCKED rows.

**The 2026-09-16 review's release gate, today:** not met, as expected for a demo build. Deployed
code matches HEAD's code (parity 9 of 9). Not met: authorization enforcing (observing); notification
delivery (worker disabled, nine notifications retrying since 2026-09-12); the product loop proven on
the deployed stack with a model-generated path; the blocked customer-evidence rows (B5, B6, B15,
C9, C10, C11, C13).

## 6. Evidence files

Raw logs are in this session's scratchpad and were not committed: the static gate logs
(`static-cb4`), the backend suite log (`final2`), the live batches on `80ad325` (`live2`) and
`9128241` (`live3`: parity JSON, sweep and matrix JSON, the accessibility report and the rehearsal
screenshots), and the four review reports (`review2`). Re-measure with the commands in the tables
above.
