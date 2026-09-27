# Session Addendum -- 2026-09-10 -- the steward agent (ADR-0029)

> **Purpose.** New tracker rows and evidence for the steward agent, built on
> `feature/agent-os-v2`. Staged here rather than merged into `03-tracker.md`
> for the same reason the 09..19 addenda were. Fold these rows in on the next
> tracker rebase.

## Headline

The roadmap's first role agent after the analyst and reviewer agents. It was
built so that its identity *enforces* something rather than naming something,
which is the test [ADR-0023](../10-architecture/adr/ADR-0023-deterministic-jobs-vs-generative-producers.md)
and the [2026-09-09 review](../10-architecture/15-agent-architecture-critical-review.md)
set. [ADR-0029](../10-architecture/adr/ADR-0029-steward-agent.md) records the decision.

| | Before | After |
|---|---|---|
| Runtime consumers of `AgentContract.autonomy_tier` | 0 | 1 (the steward agent: T0 observes, T1+ proposes, nothing above T1 grants more) |
| Contracted automation that writes stewardship proposals | none -- the only automated writer was the unregistered `auto-enqueue-drafter` | the steward agent (the side-car is unchanged; see "not done") |
| Where GL-8 matching and the AT-5 worklist gatherer live | private functions inside `stewardship_api` | `glossary_link_candidates`, `documentation_worklist_signals`, pinned by an import contract |

---

## Rows to add

### AG-12 -- the steward agent (P1)

Section: **F. Tools, model gateway, query gateway, governance**.

- `src/aida/steward_agent.py` -- one bounded run: resolve authority, check the
  kill switch, read the tier, select from the AT-5 worklist in its priority
  order and from GL-8's label matcher, propose each item as a
  `GovernanceReview` requested by `agent:steward`, and write one
  `AgentTask` per proposal.
- `src/aida/steward_agent_api.py` -- `GET /v1/organizations/{org}/steward-agent`
  (state, refusal reason, kill state, capabilities with their ADR-0027 tiers,
  outcomes) and `POST …/steward-agent/run` (409 with a stable reason code on
  refusal, and the run rolled back whole).
- Settings: `steward_agent_principal_id` (default `agent:steward`),
  `steward_agent_max_proposals_per_run` (25), `steward_agent_max_pending_proposals`
  (100). There is no enable flag; the agent does nothing until registered.
- Guards, each asserted in `tests/test_steward_agent.py` (41 tests):
  - fail-closed authority (no contract, unapproved version, two approved
    contracts, another organization's contract, a principal equal to the
    reviewer agent's);
  - every kill scope refuses a run (the agent's own, its tier's, the
    organization's, and the model gateway's);
  - a switch engaged mid-run is seen at the next item. This is proved with a
    Core `UPDATE` that bypasses the identity map, which is the trap
    `populate_existing` avoids;
  - lowering the tier to T0 mid-run withdraws the licence to write, and a run
    whose licence was withdrawn keeps nothing;
  - T0 observes, and T1, T2 and T3 propose identically;
  - maker != checker: the supervising steward can approve the agent's draft,
    and the agent is refused;
  - the agent leaves alone any table with an open draft, text a reviewer
    already rejected, and drafts below the GL-9 evidence bar;
  - the run limit is clamped by configuration; the review-backlog bound and
    the wall-clock cap end a run and keep what it did;
  - the datasource scope holds; a rejected link is never raised again;
  - the ledger is value-free, and the acceptance rate is `None`, not 0, until
    a reviewer decides something;
  - static: every object type the agent can open is at or below
    `HARD_MAX_AGENT_TIER`, and the module cannot reach a decision or publish
    path.

### ST-12 -- two stewardship rules moved out of their router (P2)

- `documentation_worklist_signals.py` -- the AT-5 gatherer, moved unchanged
  apart from the entry point's name (`gather_documentation_worklist_signals`).
- `glossary_link_candidates.py` -- GL-8's matcher. The endpoint's output is
  identical; its existing-link, existing-proposal and table reads are now
  bounded to the annotated tables rather than the whole organization.
- Import contract **"ADR-0029 the steward agent and the rules it shares never
  import a router"** -- 12 contracts kept, 0 broken.

### UX-22 -- steward agent console (P1)

Screen `steward-agent` in the Steward work area. It shows:

- registered or not, with the refusal and how to register;
- the tier and what it permits;
- the kill-switch state;
- a Preview that opens nothing, and a Run that lists every item with its
  action and reason, linking each proposal to the review queue;
- per-type acceptance, shown as "—" until something is decided.

Eight vitest tests. The screen was also checked in the browser in fixture mode.

---

## Baselines regenerated

Each was checked semantically, not by line count:

- **OpenAPI:** +2 paths and +6 schemas. The `documentation-worklist`
  operation's description also changed, because its docstring names the moved
  function. Nothing was removed.
- **UI types:** +6 `StewardAgent*` interfaces.
- **Surface control matrix:** +2 steward surfaces.

**One inconsistency to resolve at commit time.** This branch's working tree is
shared with another session. That session's uncommitted column-worksheet work
(in `model_import_api.py`) appeared in the tree while these files were being
regenerated. None of it exists at HEAD. As a result:

- the regenerated UI types also contain `WorksheetColumnEdit` and
  `WorksheetSave`;
- the regenerated surface control matrix also contains
  `POST /v1/tables/{table_id}/column-worksheet`;
- the OpenAPI baseline, generated seconds earlier, does not.

Whoever commits should regenerate all three from the exact tree being
committed:

- `scripts/openapi_diff.py --accept-baseline`
- `scripts/generate_ui_types.py --accept-baseline`
- `scripts/generate_surface_control_matrix.py`

Run them with `AIDA_ENVIRONMENT=development`.

---

## An operational consequence worth stating

The reviewer agent (ADR-0027) is off by default. If an operator enables it,
it may decide the steward agent's proposals, because they come from a
different identity:

- **Glossary links:** GL-8 scores every link 1.0 or 0.92, above
  `reviewer_agent_approve_confidence` (0.8). They can therefore go from
  proposal to applied with no human, except the 5% audit sample. That is
  ADR-0027 working as designed.
- **Description drafts:** these typically score about 0.4-0.6 and will be
  abstained on, so a human decides them.

Enable the reviewer agent knowing this.

---

## What is still not done

Stated plainly, as the previous addenda did.

1. **No scheduler.** A person starts every run.
2. **The ingest side-car is unchanged.** `auto-enqueue-drafter` still writes
   drafts with no contract. Bringing it under this one would stop
   auto-drafting in every organization that has not registered the agent, so
   it needs its own decision.
3. **Column descriptions are not a capability.** They were in flight on this
   branch from another session.
4. **A proposal cannot be edited before review.** A steward rejects it, which
   records negative knowledge, and writes their own.
5. **The inbox and the roster undercount the agent.** Their run counters
   (`runs_recent`, `success_rate`, the roster's method summary) count
   `AgentRun` rows, and the steward agent writes none. They will report zero
   runs for it. Its work shows in the inbox's task lists and on its own
   console. A run itself is recorded as a `steward_agent.run` audit event
   plus its tasks; there is no run table.
6. **GL-9 lineage names can cross datasources.** Table drafts name upstream
   and downstream tables without the ADR-0017 same-source filter the column
   drafter applies. This predates the agent, but the agent now reaches it
   without a human in the loop. Filed as a follow-up task.
7. **Nothing bank-scale.** The mid-run stop is proven on SQLite in one process
   (with the aiosqlite BEGIN recipe, so a savepoint cannot silently commit),
   not across PostgreSQL workers. No run has been made against a real estate,
   so every acceptance rate is `None`.

## Verification

```
pytest tests/test_steward_agent.py                  -> 41 passed
pytest tests/ (full suite, run as 12 file shards)   -> 7 shards clean; 8 failures in 5 shards:
  test_doc_claims.py (3)            this change's wording -- fixed, re-run clean
  test_perf_baseline_gate.py (1)    wall-clock bound under 12-way CPU contention -- passes alone
  test_reachability_gate.py (1)     aida.ontology_api is unwired            \
  test_migration_orm_drift.py (1)   models.py +4 lines with no migration     | another session's
  test_reviewer_agent.py (1)        ONTOLOGY_VERSION not in the tier table   | uncommitted work in
  test_openapi_diff_gate.py (1)     column-worksheet route not in baseline  /  the shared tree
mypy (touched modules)                              -> Success: no issues found
mypy src sdk/aida_tool_sdk                          -> 4 errors, all in src/aida/ontology_api.py
                                                       (another session's uncommitted file)
ruff check (touched files)                          -> All checks passed
ruff check .                                        -> 30 errors, all in ontology_api.py /
                                                       ontology_models.py (same)
lint-imports                                        -> Contracts: 12 kept, 0 broken
openapi_diff / generate_ui_types / surface matrix   -> regenerated; --check clean
check_docs_links                                    -> OK
ui-next: npm run typecheck                          -> clean
ui-next: npm run test                               -> 79 files, 599 passed
```
