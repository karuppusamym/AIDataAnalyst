# Session Addendum -- 2026-09-04 -- Agent contract (AG-10) and suite repair

> **Purpose.** New tracker rows and evidence for the 2026-09-04 session on
> branch `feature/agent-os-v2`. Staged here rather than merged into
> `03-tracker.md` directly, for the same reason the 09..16 addenda were:
> that file has extensive concurrent edits. Fold these rows into
> `03-tracker.md` on the next tracker rebase.

## Headline

The branch was carrying **177 failing tests** before this session and now
carries **0**, across 7,421 collected tests. `mypy --strict` is clean on 304
files (was 19 errors). Import-linter is 8 contracts kept, 0 broken (was 2
broken). None of the 177 failures were caused by the code under test: they
were fixtures authored in cloud sessions against a different checkout, plus
four genuine production defects the fixtures happened to expose.

---

## Rows to add / update

### AG-10 -- Agent contract, AgentRun attribution and the agent task ledger (P1)

Section: **F. Tools, model gateway, query gateway, governance** (extends the
AT-/TL- series). Design: `Docs/00-product/08-market-deep-dive-and-target-architecture-2026-09.md` §4.2.

**Problem.** `aida.agent_roster` documents, in its own module docstring, that
there is no persisted way to attribute an `AgentRun` to a registered
`AiAsset`: the runtime produces runs from exactly one code path scoped only
by organization and datasource, while the AI registry can hold any number of
`AGENT`-kind entries. More broadly, the platform had every primitive an agent
workforce needs -- drafters, playbooks with an auto-apply threshold, an eval
gate on agent versions, an exemplar store, a governance threshold, a kill
switch -- and no object binding them into a named, budgeted, tiered agent
with its own identity.

**Fix.**

- `AgentContract` (`src/aida/models.py`, appended): one governed row per
  `AGENT`-kind `AiAssetVersion`, carrying `agent_principal_id` (a distinct
  non-human workload identity), `capability_envelope`
  (`tool_slugs` / `context_product_ids` / `write_lanes`), `autonomy_tier`
  (T0-T3), the three budget caps, `eval_gate_threshold`,
  `supervisor_persona`, `kill_scope` (AGENT / TIER / ALL), `kill_engaged`
  and `sampling_rate`. Every enumeration is closed by a CHECK constraint and
  the ADR-0027 5% sampling floor is a constraint, not a convention.
- `AgentRun.ai_asset_version_id`: nullable FK, `SET NULL` on delete. Every
  existing run stays valid and unlinked, and the roster keeps reporting those
  organization-wide.
- `AgentTask` plus `src/aida/agent_tasks.py`: the work-unit ledger. The
  sampled-for-audit decision is a pure function of the task's
  `inputs_fingerprint` and the contract's rate -- no RNG, no clock -- so it
  replays.
- `src/aida/agent_contracts.py`: the authority half. Validation refuses an
  `agent_principal_id` equal to the human writing the contract or lacking the
  `agent:` workload-identity prefix; `agent_kill_blocking_reason` composes the
  contract's own switch, its tier's, the organization's, and the existing
  model-gateway kill switch; `envelope_violation` treats an unparseable
  envelope as empty (fail closed), never as unrestricted.
- `GovernedAgentOrchestrator.run` gained an optional `agent_asset_version_id`.
  When present it enforces, in order: a named version with **no** contract is
  refused (`agent_contract_missing`) rather than run unconstrained; an engaged
  kill switch stops the run before any retrieval or generation; and the
  capability envelope is checked against the governed tool the planner
  actually selected, not against what the caller asked for. All three write
  DENIED audit rows through the existing `_persist_rejection` funnel, which
  also closes the open task -- looked up by run rather than passed down, so no
  caller can forget to.

**Migration.** `a91c4d7e2b58`, `down_revision = d5b2e4f7a9c1`. Additive with
no backfill: with no contract written the platform behaves exactly as before.

**Tests.** Existing orchestrator, roster, registry and Tier-0 suites all pass
unchanged. *Known gap:* dedicated AG-10 unit tests (contract CRUD, envelope
violation, kill scope, sampling floor) are **not** written yet -- the
enforcement paths are exercised only indirectly. See "What is not done".

---

### FIX-1 -- Production defects found while greening the suite (P0/P1)

Four were real, and each would have failed silently in production.

| Defect | Impact |
|---|---|
| `atlas.modules.identity_tenancy.router` used `OrganizationIntegrationPolicy` in `create_organization` without importing it | `NameError` on **every organization creation**. Introduced by the ST-07 router split; caught by `ruff --select F821`, not by any test |
| `certification_evidence.backfill_certification_evidence_v1` filtered on `evidence.is_(None)` | `evidence` is a JSON column and SQLAlchemy serialises Python `None` to the JSON literal `null`, not SQL NULL, so the filter matched nothing. The backfill reported success and populated **zero rows on every run**, in production as well as in tests |
| `ownership_expiry_warning` subtracted a column timestamp from an aware `now` | PostgreSQL reads `timestamptz` back aware and SQLite reads it back naive, so the sweep raised `TypeError` on SQLite and was effectively untestable. Now normalised through `aida.timeutil.as_utc`, the convention `business_graph` already uses |
| `query_gateway.validate()` raised `AuthorizationRejected` for unresolvable table references | See QG-FIX below -- it masked every validation finding |
| Eight `scripts/*` report writers used the platform default encoding | On a Windows cp1252 host they emitted bytes their own readers could not decode. All `read_text`/`write_text` calls now pin UTF-8 |
| `GET /v1/datasources/{id}/graph-summary` was deleted, not moved, by the ST-07 split (688e571) | The endpoint disappeared from the API. It was still in the committed OpenAPI baseline, which corroborates that its removal was accidental. Restored verbatim from the pre-split revision |

---

### QG-FIX -- AU-11/AU-15 fail-closed check masked validation findings (P0)

**Problem.** The unresolvable-table check had two defects that together broke
103 tests, including the entire adversarial SQL corpus for every dialect whose
statements the guard accepts.

1. It ignored guard validity. Its own comment says it applies when "the guard
   accepted a statement", but the condition never tested that, so a statement
   already rejected on its merits (stacked DDL, a mutation, an unbounded join)
   came back as a generic authorization error instead of its real violation.
2. It raised from `validate()`, which opens no connector and returns no row.
   The pipeline already refuses every unresolvable reference with
   `UNKNOWN_OR_UNAUTHORIZED_TABLE`, naming each offending table, so raising
   preempted the catalog allowlist check outright and denied the caller the
   one thing validation exists to give them.

**Fix.** Gated on `guard_result.valid`; `validate()` now reports while
`execute()` still fails closed. Leaving the ABAC axes empty on the validate
path is safe there and only there: the axes describe catalog objects, every
referenced table is absent from the catalog, so there is no classification,
certification or quality state to bypass and no metadata about a real asset to
disclose. The attempt stays attributable -- the reason rides in the single
audit row `validate` already writes, so one validation remains one audit row
and one commit.

`test_unresolvable_table_reference_fails_closed_on_validate` is amended to
assert the property AU-15 actually wanted (denied, and attributable); the
execute-path test is unchanged.

---

### TIER0-FIX -- Three Tier-0 gates had stopped checking what they claim (P0)

| Gate | What had happened |
|---|---|
| **INV-7** attributability | The call-graph walker in `tests/support/app_surface.py` was hardcoded to `src/aida`. When ST-07 moved handlers into `src/atlas/modules/*/router.py` it could no longer read their source and reported **33 genuinely-audited endpoints as unaudited** -- the invariant had silently stopped covering a third of the application's mutations. The walker now reads every package in `WALKED_PACKAGES` |
| **INV-4** authorization wiring | Asserted `reaches_call("aida.api", "list_catalog_rows", ...)` for a handler that had moved, so it was asserting against a module that no longer defines it. Now parametrized per `(module, handler)` so the next move fails loudly |
| **INV-6** value freedom | `CatalogSession` defaulted `referenced_table_ids` to empty while declaring the catalog contains those tables -- two contradictory statements about one catalog. Harmless until AU-11 made empty resolution a hard denial, after which the value-freedom tests died on an authorization error before reaching the masking and lineage behaviour they exist to check |

---

### DOC-FIX -- Event catalog and doc-truth gate (P1)

Nine event types were published via `record_outbox` in `src/` but absent from
`Docs/30-contracts/04-event-catalog.md`, against that document's own rule
("before publishing any new event"): the two certification lifecycle events,
two identity principal-lifecycle events, and five ownership assignment
events. All nine are now documented with trigger and payload.

ADR-0025 (Proposed) cited the auto-approve sweep module as a backticked
src path; that module is the thing the ADR proposes and does not exist. Reworded
so the citation is not a claim about current code.

---

### REACH-1 -- Identity-lifecycle modules allowlisted, with the reason (P2)

`aida.identity_events` and `aida.ownership_principal_lifecycle` are reachable
from none of the five entry points. They are the OW-5 handler and its emission
half; their caller is an identity-provider integration (webhook or directory
sync) that has not been built. Both are exercised end to end by
`tests/test_ownership_expiry_and_leaver.py` -- the gap is the trigger, not the
behaviour -- so they are on the reachability `ALLOWLIST` citing OW-5, which is
what that gate's docstring permits for a tracked backlog item. **Remove both
entries when the IdP integration lands.**

---

## What is not done

Stated plainly so the next session does not have to rediscover it.

1. **AG-10 has no dedicated tests.** The contract, envelope enforcement, kill
   scope and sampling floor are exercised only indirectly. This is the largest
   gap in this session's work.
2. **No API or UI for the agent contract.** The design in §4.2 calls for
   contract CRUD endpoints, a kill/release endpoint, an agent-task listing and
   an agent inbox read model, plus the `ui-next` agent inbox screen. None of
   that is built; the ORM, the enforcement and the migration are.
3. **ADR-0027 is not written.** The 5% sampling floor is enforced as a CHECK
   constraint and referenced in code comments, but the ADR that decides
   risk-tiered agent checking does not exist, and no reviewer agent is built.
4. **The other four planned workstreams did not land**: reviewer agent and
   risk tiers, persisted vector index / exemplar few-shot / stewardship
   worklist, the `ui-next` persona workbenches and agent inbox, and
   Slack/Teams governance notifications.
5. **38 E402 lint errors remain**, a structural consequence of the ST-07
   extraction assembling files from moved code. Pre-existing, unrelated to
   this session, and left alone deliberately.
6. **Nothing here has bank-scale evidence.** Every claim above is local
   end-to-end only, which is the same caveat `00-status.md` §4 applies to the
   whole platform.

## Verification

```
pytest tests/          -> 0 failed (7,421 collected)
mypy --strict src      -> Success: no issues found in 304 source files
ruff check src tests scripts -> 38 errors, all pre-existing E402
lint-imports           -> Contracts: 8 kept, 0 broken
alembic heads          -> a91c4d7e2b58 (single head)
```
