# End-to-end validation before the demo: 2026-09-21

A dated session log: evidence, not a queue (see `CLAUDE.md`, rule 2). Status lives in
[tracker section P](03-tracker.md#p-current-execution-queue-reconciled-2026-09-11); the new
work this pass found is queued there under *Validation pass: 2026-09-21*, and dated evidence is
in the [capability register](20-capability-register.md).

**Asked for:** validate the code and the configuration end to end, confirm the gaps and fixes
across the reviews already completed, record everything, and get the product ready to demo.
Reuse the review and tracker work already started.

**What was reused.** A validation run from earlier the same morning (08:44 to 09:35, author
not recorded in any session) had left twenty `validation-*` logs in the repository root and nine
uncommitted fixes (R11-AUD12, R11-AUD13 and the GraphQL explorer). Its full backend run
(14,858 passed, 184 skipped, 0 failed) and full UI run (1,271 passed) had started after the
last of those edits, so they held for that tree. Its lint, type and build runs predated the final
edits and were rerun. The logs are left in place, untracked, because they are not this session's.

**Baseline and end point.** Started at `cb69f42` plus those nine files; ended at the commit that
carries this log. The local stack was rebuilt once, from a clean worktree of `e534c1b`; later
commits change CI configuration, documents, one agent-facing message and one docstring.

## 1. Results

### Gates (the CI jobs, run locally)

| Gate | Result | Notes |
|---|---|---|
| `ruff check .` | pass | `ruff format` is not a gate; the two files it flags were unformatted at HEAD already |
| `mypy src sdk/aida_tool_sdk` (strict) | pass | |
| `lint-imports` | pass | |
| `alembic heads` | pass | one head, `53558182d9fb`; the dev database is on it |
| `check_proxy_contract.py` | pass | now also holds the three upload routes to 32 MiB |
| `check_image_packaging.py` | pass | |
| `openapi_diff.py`, `generate_ui_types.py` | pass | no API surface change this session |
| `check_docs_links.py`, `test_doc_claims.py` | pass | |
| `shim_register.py --check`, `generate_architecture_map.py --check` | failed, then pass | the AUD12 imports and a new lazy import moved both counts; regenerated in `5db4dbd` |
| `generate_destination_inventory.py --check`, `check_frontend_reachability.py --check` | pass | |
| Parity gate, source-side half (unreachable deployment) | pass | reports "not measured", as its CI contract requires |
| `test_reachability_gate.py` | pass | |
| `test_migration_orm_drift.py` against real PostgreSQL | pass | 1 passed, not skipped |
| `perf_baseline.py`, `quality_benchmark.py --no-report` | pass | |
| UI `tsc --noEmit` | pass | |
| UI `vitest run`, whole suite, alone | **120 files, 1,286 tests passed** | at `0eb0656`; UI code is unchanged after it |
| UI production build | pass | `vite build` inside the `ui-next` image build at `e534c1b` |
| Backend `pytest`, whole suite, bare | **14,889 passed, 184 skipped, 0 failed** | at `6c376d0`, from a clean worktree; 76 minutes under load (39 on the same machine earlier in the day) |
| Playwright journey suite (isolated stub stack) | **46 passed** | UI image and stub both from the `6c376d0` worktree; nothing of the main stack touched |
| Demo contract tests (`test_demo_maker_checker_loop.py`, `test_oidc_demo_bundles.py`) | 111 passed | after the demo-script edits |
| Not run here | gitleaks, the dependency scans, the Docker-based `ui-proxy` job | gitleaks is not installed (see R11-VAL01); the scans need the network; the `ui-proxy` checks were run against the deployed nginx instead (below) |

### The running stack, rebuilt from `e534c1b`

| Check | Result |
|---|---|
| `check_deployment_parity.py`, from the clean worktree and from the main checkout | **9 of 9 matched**: schema `53558182d9fb`, 440 paths, 579 schemas, 290 settings, source digest `a22a9bce5bf7`, built from `e534c1b` |
| `live_role_sweep.py` (every GET route as 12 roles) | 201 routes, 2,412 calls, **0 server errors** |
| `live_role_matrix.py`, each platform role | 5,179 probes, **0 failures**; the four known NARROWED draft-list rows |
| `live_role_matrix.py`, the seven non-admin demo bundles | 2,407 probes, **0 failures** |
| `live-a11y-audit.mjs` on `:3001` | 37 screens, **0 violations** in light and dark, no sideways reflow, 0 of 6 opened states with violations |
| `demo-rehearsal.mjs` (47 screens as the eight demo users) | **0 with an issue**, the same 5 known conditions |
| Upload limits through the deployed nginx (R11-AUD11), posted as a Viewer so the role guard refuses and nothing is written | 2 MiB and exactly 32 MiB reach the API (its 403); 32 MiB + 1 is nginx's 413 on the workbook and OKF routes; the batch `submit` keeps 1 MiB |
| Container logs after the rebuild, counted, never printed | 0 error markers and 0 key-shaped strings in the five backend containers; `scheduler_became_leader`, `newly_created_table_drafter_started` |
| Browser, as `omar.auditor` on `:5184` | Compliance offers no Generate or Download (a note instead); the Act 1 run link opens the run and its Evidence tab |

No migration was applied, no prompt was sent to a model provider, and nothing was written to the
estate: every probe that could write was sent as a role the route refuses.

### Continuous integration on GitHub

The last pushed commit, `cb69f42`, passed 18 jobs and failed three. **Tests** was cancelled at
its 30-minute limit, as on every run since 2026-09-19, so no CI run had finished the backend
suite since then. **Production proxy contract, live** failed on four of the last six runs (at `cb69f42` with a 502): it
waited for nginx's own `/health`, never for the stub behind it. Both are fixed in `0870d5c`
(a 75-minute limit and a wait through the proxy), which is not pushed. **Secret scan
(gitleaks)** reports 8 findings that the log does not name; see R11-VAL01.

## 2. What was fixed in this session

| Commit | Change | Tracker |
|---|---|---|
| `193457f` | The Kubernetes migration Job runs `alembic upgrade heads`, like compose; a test pins both | R11-AUD13 DONE |
| `dbe8078` | GraphQL explorer: asks before any document naming a mutation (a fragment written first used to skip it), clears the acknowledgement on edits, states the real introspection policy, and shows a refusal's code and detail | R11-GQL02 |
| `5db4dbd` | Access-policy and workspace-grant reviews show a structured diff on both review surfaces. The in-flight fix covered the batch queue only; the Review queue's change preview reads the single-review route, which still said "not yet diffable". Both now share one snapshot, organization-checked | R11-AUD12 DONE |
| `0dc70e7` | nginx admits exactly the API's 32 MiB on the workbook and OKF bundle imports; the gate, tests and the CI live job cover it. Corrects the row's 64 MiB OKF figure (that is the uncompressed bundle) | R11-AUD11 PARTIAL |
| `27dee78` | Asked through a context product, the SQL model saw tables a referenced routine reads or a pinned concept maps to even when the product did not name them; the gateway then refused the SQL. The model context is now held to the product's tables | R11-FP12 |
| `bfa79de` | Controls a role is refused are no longer offered: Compliance Generate and Download for the Auditor persona; the Catalog Knowledge section for Auditor, Reviewer, Viewer and DataAdmin. Ask explains `CONTEXT_PRODUCT_TOOL_DEPENDENCY_OUT_OF_SCOPE` instead of showing the token. Reliability no longer claims a WORM trail behind every audit event (the stack reports `NO_ARCHIVES`); the Review queue no longer says classification propagation does not exist | R11-D3 class, R11-B17, R11-OKF02 |
| `e534c1b` | The two Kubernetes READMEs and the topology page say `heads` | R11-AUD13 |
| `0870d5c` | CI: the proxy job waits for its stub; the Tests job may run 75 minutes | R11-VAL01 note |
| `0eb0656` | Demo script: Act 1 shows the run as Omar (an Analyst may open only her own runs), with the query before the `#`; Act 2 says bulk actions apply at once, and counts 3 pending and 7 approved relationships; Act 3 shows Dana's own-review banner between loop steps 3 and 4; Act 5 points at what the scope line really says and uses `curl.exe`; loop step 5 prints a table | |
| `6c376d0` | The MCP asset-context message told agents nothing triggers classification propagation; the scheduler does, when its interval is set. `08-workers-and-workflows.md` called AR-01 to AR-04 open | R11-B17 |
| this commit | This log; section P's validation rows and count; the register; the walkthrough index and readiness pages; `00-status.md`; the coding-standards gate table; three configuration statements (Vault token references, the scheduler ConfigMap comment, `.env.example` defaults) | R11-VAL03 PARTIAL |

## 3. The reviews, checked against the code

Seven read-only audits ran in parallel, one per review or area. Their findings were leads: every
deviation below was re-measured here before being written down, and three of their claims were
narrowed or dropped on re-measurement (named at the end).

| Review | Checked | Outcome |
|---|---|---|
| 2026-08 review, tracker sections L to O, the 2026-09-18 functional fixes | all open rows, 78 merged rows, 5 fixes | No regression. **Four older rows have no disposition anywhere** (R11-VAL02) |
| 2026-09-05 review (72 points) | points, roadmap, journeys | 48 verified, 20 open with a row. Contradicted: the R03 schema import cycle is still there (R11-VAL05). Lost: the workbook upload never reports to the connection badge (R05 residual, R11-VAL05) |
| 2026-09-11 review (68 rows) | D, X, B, P, S rows | 53 hold, 9 partly. X3 and X8 closed with named dead code still present (R11-VAL05); B10 dropped the "a failing scheduler pass shows on Operations" clause (R11-VAL04); D12's text went stale after B17 (fixed in `6c376d0`) |
| 2026-09-16 review (F01 to F09, sections 3 to 8) | 20 items | 7 resolved, 8 partly, 4 open. **F01/F02 follow-through defect** found and fixed (`27dee78`). The review's release gate is not met; see section 5 |
| 2026-09-20 audit rows and the newest rows (44) | FP, OKF, GQL, SQL, REV, S13, C14, C15, I1, AUD | 38 hold, 5 partly, none contradicted. Row corrections are listed in section 4 |
| Configuration | settings, compose, Kubernetes, nginx, images, CI | All 290 settings match the inventory; every compose combination validates; the manifests render with `kubectl kustomize`. Five mismatches (R11-VAL03) |
| Demo pack | every step and number | Four steps would fail or say something untrue on screen, and the numbers disagreed across pages; fixed in `0eb0656` and this commit |

## 4. Deviations found and not fixed here

Each has a section P row; this list is the evidence behind them.

- **Secret scan red (R11-VAL01).** 8 gitleaks findings at `cb69f42`, up from 1 on 2026-09-18.
  The names are only in the run's report artifact. A local pattern scan of HEAD found only known
  fixtures and false positives (`risk-tier`, AWS's published example key, the allowlisted synthetic
  private key). History was not scanned.
- **Four older rows with no successor (R11-VAL02):** C5 (data quality as its own module), the
  old C9 (lineage derivation method, not R11-C9), N7's residency attribute and N8's general document
  ingestion. `23-review-reconciliation-2026-09-11.md` says every open older row points to a successor.
- **Configuration statements the code contradicts (R11-VAL03):** `secret.example.yaml` calls the
  Vault token references optional, while `signing.py` and `tokenization.py` refuse an empty one under
  `vault_transit`; `configmap.yaml` says its intervals reach a fleet-scheduler Deployment, which does
  not exist; `AIDA_BASE_URL`, read by the seed scripts, is 0.867 similar to `AIDA_DATABASE_URL`, above
  the typo detector's 0.84 cutoff, so putting it in `.env` stops every app container; two
  `.env.example` values differ from the code defaults without saying so (86,400 against 259,200 and
  200 against 500). (a), (b) and (d) were corrected in the commit carrying this log; (c) needs a
  `config.py` change and is left while another session may change that file.
- **A failing scheduler pass ends the loop silently (R11-VAL04).** An exception from any pass ends
  `run_scheduler`'s loop; nothing shows it on Operations.
- **Leftovers of closed rows (R11-VAL05):** the R03 schema import cycle (`atlas.modules.*.schemas`
  import `ApiModel` from `aida.schemas`, which re-exports them); dead symbols X3 named
  (`auto_lift_on_material_change` and `DisabledModelGateway` have no references, `compute_worklist`
  only comment ones); the R05 upload badge residual.
- **Row corrections for their owners.** R11-AUD01's route counts are 29, 8 and 5 for
  DataProductOwner, ProjectAdmin and ComplianceOfficer on the live app (the row counted REST rows
  only; sent to the session that owns AUD01). R11-GQL02's "Agent Gateway explorer" remainder exists
  (`AgentGatewayConnect.tsx`), so only SDK examples remain. R11-OKF01's listed remainder appears to be
  in code and the row may be closable; R11-FP10 and FP11 show nothing left to do. Not re-verified
  here row by row, so left to the row owners.

Measured rather than assumed: the configuration audit flagged nginx's default 60 s read timeout on `/v1/` as a risk for a slow Ask through `:3001`. The 52 agent runs recorded on Northwind took at most 14.7 s from creation to their last update and none over 30 s, so on this evidence it is not a demo risk; it stays unmeasured for a very large synchronous ingestion push (R11-AUD11).

Narrowed or dropped on re-measurement: the FP16 row's stated defaults (0, opt-in) are right; the
local stack simply runs 15 and 30 minutes from `.env`. An "API reads the whole body" wording in
AUD11 was wrong: both upload handlers refuse a declared `Content-Length` first. The `ci.yml` stale
comments the configuration audit listed were not re-verified one by one.

## 5. Still pending, and whose call it is

**Yours (the owner's), before or at the demo**

1. Push the local commits (`193457f` to the log commit): CI's two fixes take effect only on a push.
2. Let the gitleaks report be read: download the `gitleaks-report` artifact of run `35601042250`
   (a few KB, from this repository's Actions), or allow installing gitleaks to scan history locally.
3. Rotate the Gemini key that reached the scheduler's log before R11-AUD14.
4. Whether live Ask is part of the demo: it sends the prompt to a real provider.
5. Unchanged decisions: ADR-0030 (**Landed after this validation, and not committed when this log was written.** The session that owns those rows (the agreed split) built R11-AUD01, AUD03, AUD04, AUD06, AUD07, AUD08, AUD10 and AUD11's API half, found and fixed a new defect (R11-AUD15: six routes that never committed their writes), and gave VAL02 and the rest of VAL03 their dispositions, all as uncommitted changes in the shared working tree on top of `6c376d0`. It reports its own verification on that combined tree (the full backend and UI suites, the gates, 16 roles checked live, 50 screens rehearsed) and rebuilt the local stack from it. **From that point the running stack is uncommitted code:** the parity and live results in section 1 describe the `e534c1b` build, and parity against any commit drifts until round 12 is committed and the stack is rebuilt from a clean worktree of it. That session also recorded owner-level decisions on its rows (the 16-role catalog with Auditor access to compliance packs, ADR-0030 accepted, a signing-key verification window while the identity provider is unreachable, three older rows deferred), each reversible by editing its row. This log's tracker rows, counts and pages were committed as HEAD plus this session's edits only, so its hunks stay in the working tree on top.

eens, AUD10, and the API-side half of AUD11. It will land them after this validation, rerun the
suite and the live checks, and redeploy only then; its batch will need the demo pack refreshed.

**The 2026-09-16 review's release gate, today:** not met, as expected for a demo build. Deployed code
matches HEAD now (parity 9 of 9). Not met: authorization enforcing (observing, three sources
unbound); notification delivery (worker disabled); the product loop proven on the deployed stack
with a model-generated path; the blocked customer-evidence rows (B5, B6, B15, C9, C10, C11, C13).

## 6. Evidence files

Raw logs of this session are in its scratchpad and were not committed: the static gate logs, the
seven audit reports, the parity JSON, the sweep and matrix JSON, the accessibility report and the
rehearsal screenshots. Re-measure with the commands in the tables above.
