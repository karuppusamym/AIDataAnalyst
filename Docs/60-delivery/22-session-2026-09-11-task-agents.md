# Session Addendum -- 2026-09-11 -- one task-agent runtime, the lineage and quality agents (ADR-0029)

> **Purpose.** Tracker rows and evidence for the second and third task agents
> and the runtime they share, built on `feature/agent-os-v2`. Staged here, like
> [the steward agent's addendum](21-session-2026-09-10-steward-agent.md), for
> the next tracker rebase. The decision is recorded as an amendment to
> [ADR-0029](../10-architecture/adr/ADR-0029-steward-agent.md).

## Headline

| | Before | After |
|---|---|---|
| Where the governing rules of a task agent live | inside `steward_agent.py` | `task_agent.py`, shared by three agents |
| View definitions captured at ingestion that anything parsed | none | every eligible one the lineage agent reaches; each edge PROPOSED for a person |
| Quality rules anything suggested | none | row-count floors and null-rate ceilings from profile history, each a T2 review a person decides |
| How high an agent may *propose* / *decide* | T1 / T1 | T1 by default, T2 by declaration, never T3 / T1, unchanged |

---

## Rows to add

### ST-13 -- the task-agent runtime (P1)

- `src/aida/task_agent.py` -- the governing rules, moved out of the steward
  agent unchanged in behaviour:
  - authority from exactly one contract naming the agent's principal, on an
    approved `AGENT`-kind version;
  - every other agent's principal reserved, read from the settings model;
  - the kill switch re-read before every item;
  - the autonomy tier as a ceiling;
  - per-run, backlog and wall-clock bounds;
  - the ledger and the acceptance rate.
- `src/aida/task_agent_api.py` -- the state and run response shapes, and the
  rolled-back 409 on refusal, shared by every agent's router.
- Reason codes are agent-neutral (`agent_version_not_approved`,
  `agent_principal_reserved`, `agent_autonomy_withdrawn`,
  `agent_review_backlog_full`).
- **Two write paths, both narrow.** `open_review` writes into the shared
  queue. `proposed_in_queue` writes only into a queue on the human-only list;
  `PARSED_LINEAGE_REVIEW` is its only entry.
- **Proposing and deciding are separate ceilings.** A spec proposes at most
  `HARD_MAX_AGENT_TIER` (T1) unless it declares up to `HARD_MAX_PROPOSAL_TIER`
  (T2). A spec above T2 cannot be constructed, and the tier is checked again
  when a review is opened. No agent decides above T1.

### AG-13 -- the lineage agent (P1)

- `src/aida/lineage_agent.py` parses the view definitions ingestion captured
  and nothing had parsed. A definition must be eligible: active, available,
  literal-redacted and screened clean, for a view with no parsed lineage in
  any review state.
- The resolved edges are written `PROPOSED`, authored by `agent:lineage`,
  whatever the auto-activation settings say. They are decided in ADR-0026's
  per-edge queue, which refuses the agent as reviewer of its own edge.
- A definition it cannot use is recorded once. It is not examined again
  until it changes.
- `lineage_table_resolution.py` resolves table names the way a person's parse
  does; it was moved out of `view_lineage_api`.
- `GET`/`POST /v1/organizations/{org}/lineage-agent[/run]`, with three
  settings.
- Tests: `tests/test_lineage_agent.py`.

### AG-14 -- the quality agent (P1)

- `src/aida/quality_rule_proposals.py` derives DQ-4 rules from each table's
  last five completed profiles. It needs at least three. It proposes:
  - `TABLE_ROW_COUNT_MIN` at half the smallest recent row count;
  - `COLUMN_NULL_RATE_MAX` at the worst recent null rate plus 0.02, for a
    column never more than 5% null.

  It uses only counts the profiler already stores (INV-6). A rule key already
  covered by any rule or any proposal is excluded in SQL.
- `QUALITY_RULE_PROPOSAL`: a new governed object and table (migration
  `e3b8f14c6a92`), classified T2 in the ADR-0027 tier table. Its decision
  adapter is registered by `semantic_api`. Approval creates an enabled rule in
  the datasource's "Agent-proposed rules" pack, with the approver as its
  creator. The review queue shows the proposed rule first.
- `src/aida/quality_agent.py` is the only spec that raises its proposal
  ceiling, to T2. No agent can decide its proposals at any configured ceiling.
- `GET`/`POST /v1/organizations/{org}/quality-agent[/run]`, with three
  settings. Operators are the roles that may create a rule by hand.
- Tests: `tests/test_quality_agent.py`.

### AG-15 -- scheduled task-agent runs, and counting them honestly (P2)

- `task_agent_registry.py` lists the three agents for every surface that
  treats them as a class. A test fails if an agent principal setting exists
  without an entry.
- `task_agent_schedule.py`, called from the scheduler: a positive
  `<key>_agent_interval_minutes` starts that agent once per interval, in every
  organization with a contract for it on an approved version. The default is
  0, so nothing changes until an operator sets one.
- A scheduled run is the governed run, triggered by `fleet-scheduler` instead
  of a person. A refusal is rolled back and recorded, and the pass goes on to
  the next organization.
- A refusal that came after authority resolved now records the version it
  stopped. The agent inbox counts a task agent's runs, completed and refused,
  from those audit rows. The roster says where a task agent's runs are counted
  instead of reading as an agent that never ran. The console shows the
  schedule.
- Tests: `tests/test_task_agent_schedule.py`.

### AG-16 -- the steward agent drafts column descriptions (P1)

- A third steward capability, COLUMN_DESCRIPTION. It works the columns of the
  same worklist tables, in the same order, that have no approved description,
  no deliberately retired one and no open draft.
- Each draft comes from the column drafter's evidence path, never its model
  path, and is submitted as the agent's own `COLUMN_DESCRIPTION_DRAFT` request
  (T0). A column too thin for the review bar is passed over without an item,
  and text a reviewer rejected is never raised again.
- Tests: `tests/test_steward_column_descriptions.py`.

### AG-17 -- the ingest side-car under the steward agent's contract (P2)

- Where an organization registered the steward agent,
  `newly_created_table_drafter` drafts as that agent. Its kill switch or a T0
  contract stops it, a reviewable draft is submitted as the agent's request,
  and every draft gets a ledger row.
- An organization that never registered the agent sees the side-car behave
  exactly as before.
- Tests: `tests/test_side_car_steward_contract.py`.

### Fixed on the way

- **GL-9 cross-source lineage names (defect).** Table drafts named upstream
  and downstream tables from other datasources, past the per-read grant check
  (ADR-0017), and cited edges a reviewer had rejected. Both are gone. Tests:
  `tests/test_gl9_lineage_same_source.py`.
- **The roster** lists each task agent's completed runs from its audit rows.
- **Governed ontology v1** (bc6ab78) is wired in (`56f0476`):
  - the router is mounted;
  - migration `7c2d94e1b8a3` creates its tables;
  - `ONTOLOGY_VERSION` is T2;
  - a decision adapter lets the governance queue decide it.

  The module's own ruff and mypy errors are cleared, and the three gates it
  had left red -- reachability, the tier table and ORM drift -- pass. The
  session that owns the module was finishing it at the same time, with its
  own tests and a screen. That session dropped its duplicate migration, so
  the branch has one head. Its in-flight edits to the same files refine this
  wiring when it commits. Tests: `tests/test_ontology_api.py`.

### UX-23 -- one console for every task agent (P1)

- `TaskAgentConsole` renders any task agent. The steward, lineage and quality
  screens supply only names and reason labels.
- A proposal decided in a dedicated queue links there: lineage edges link to
  the parsed-lineage review.
- A capability with no ADR-0027 tier reads "human review".

---

## What is still not done

1. **Scheduled runs are off.** Every `<key>_agent_interval_minutes` is 0 by
   default, so a person starts every run until an operator sets one.
2. **The roster lists completed task-agent runs, not refused ones.** The inbox
   counts those.
3. **The lineage agent parses views only, by decision.** The routine-aware
   procedure edge table has no review state, and the reviewable one has no
   routine identity. An agent must not write lineage no person reviews.
   Procedures wait for a review state on the routine-aware table; dbt models
   keep their own path.
4. **The quality agent proposes two rule types.** Profiles store no values,
   so there is nothing to derive a range or distribution rule from.
5. **Nothing measured on a real estate.** Every acceptance rate is `None`.

## Verification

Run in a clean worktree at `ac137db` plus the quality agent, so another
session's uncommitted files could not affect it:

```
pytest tests/ (whole suite, 10 file shards)  -> 9,297 passed, 23 skipped, 1 xfailed, 3 failed
  test_reachability_gate (1)     aida.ontology_api is unwired            \
  test_reviewer_agent (1)        ONTOLOGY_VERSION not in the tier table   | bc6ab78's ontology
  test_migration_orm_drift (1)   ontology_head / ontology_version have    | work, not this change
                                 no migration                            /
  The drift gate applied e3b8f14c6a92 on PostgreSQL; quality_rule_proposal matched the ORM.
ruff / mypy (touched modules)                  -> clean
lint-imports                                   -> Contracts: 12 kept, 0 broken
openapi_diff / generate_ui_types / surface matrix
                                               -> regenerated: +2 quality-agent paths,
                                                  +1 interface, +2 surfaces
check_docs_links / test_doc_claims             -> OK
ui-next: npm run typecheck                     -> clean
ui-next: npm run test                          -> 81 files, 607 passed
```

AG-16, AG-17 and the fixes above were each verified in a clean worktree
before they were committed; each commit message records its runs.

AG-15 (scheduling and counting) was verified separately, in a clean worktree
at `b0eaccd`. Its tests and the steward, lineage and quality agent tests
passed, 83 in all. Only the same two ontology failures appeared, in the
reachability and tier-table tests. Its commit message records the runs.
