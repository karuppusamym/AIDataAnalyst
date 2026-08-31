# Accomplishment Log

> Status: **Append-only ledger.** Owner: Engineering lead.
> Records material implementation outcomes, decisions, verification evidence, and known limitations, in date order. Entries are never edited or removed — a correction is a new entry.
>
> Migrated unchanged from the retired flat `10-accomplishment-log.md` on 2026-08-28. Forward-looking status lives in `60-delivery/04-status-matrix.md`; open work lives in `60-delivery/03-tracker.md`.

## Entry conventions

- One section per date; sub-sections per release or workstream.
- Record what was **verified**, not what was intended — including the identifiers of runs, batches, and executions that prove it.
- Record known limitations in the same entry as the achievement. An entry that claims completion without naming what remains open is incomplete.

---

## 2026-08-30 (tenth entry)

### Cleared the lint backlog — and four of the errors were runtime faults

The ninth entry recorded 168 `ruff` and 16 `mypy` errors arriving with the parallel session's
~35 new modules, and said they were "surfaced, not repaired". The owner's instruction was to fix
rather than escalate, so this entry is the repair. **CI is green again**: ruff clean, mypy clean
on 156 files, 4 import contracts kept, one Alembic head, 1,199 tests passing.

#### The part worth remembering

Most of the 184 were genuine noise — unused imports, import order, long lines. **Four were
defects that would have failed at runtime**, and they were invisible because nobody reads 168
warnings looking for the four that matter.

* **`observability.traced` called the function it wrapped twice.** The `try` block enclosed the
  wrapped call, so any exception was swallowed by `except Exception: pass` and the fallback path
  then ran the function *again* — a silent duplicate side effect on every traced operation that
  raised, with the caller seeing only the second failure. The `try` now guards tracer
  acquisition alone, and the call sits in the `else` branch. Tracing must never change how many
  times the thing it observes runs.
* **`studio_api` called `record_outbox` without `aggregate_type` or `aggregate_id`** — both
  required, so every change-set submit would have raised `TypeError`.
* **`search_api` wrapped an existing `UUID` in `UUID(...)`** — `TypeError` for every search hit
  carrying a datasource.
* **`MetricsConfig` had no `insecure` field** that `configure_metrics` reads — `AttributeError`
  the moment OTLP metrics were enabled.

Two more were narrowed rather than silenced: a bare `except Exception` inside the injection
detector, where a wide silent catch makes a decoder bug read exactly like "nothing found"; and
an `assert False` in a test, which `python -O` deletes, turning the test into one that passes
either way.

A mistake of my own is worth recording with them: a blanket replacement of
`org_id = context.require_organization()` hit eleven call sites when only five had an unused
variable, breaking four functions. `ruff` reported the resulting `F821`s immediately and the
repair was scoped per-function. The lesson is the ordinary one — a mechanical edit across call
sites needs the scope check *before* the write, not the linter afterwards.

#### The embedding model is chosen and built (N5)

Decision: **OpenAI or Gemini**, the same two providers the generation path already supports.
`src/aida/embedding_provider.py` implements both, with 12 tests.

Reusing those two providers was the point rather than a shortcut: a third embedding vendor means
a second credential path, a second retry policy and a second failure mode for one capability.
The embedding credential resolves through the same reference mechanism as every model
credential, inheriting its rotation, registry and production refusal of `env://`.

**The most valuable thing this uncovered was already in the tree.** The fused retrieval path
built `HashEmbeddingProvider()` *unconditionally* and fed its output into ranking as the
`vector` signal. A hash has no semantic structure, so that score was noise wearing the name of a
signal — and from outside, the result looked complete. Resolution now fails closed
(`EmbeddingUnavailable` with a reason code, no fallback), and the vector stage is **skipped and
logged** rather than substituted. A smaller answer beats a confidently wrong one, and reporting
a capability you do not have is what INV-9 forbids.

Three provider responses are refused rather than accepted: wrong vector count, wrong width,
unparseable shape. Each would otherwise misalign vectors with the texts they describe or store
something incomparable with what is already indexed — and **neither is detectable downstream**.
They would surface as quietly bad search months later.

#### Neo4j: configurable, not removed

Recorded in full in the ninth entry; the tracker rows moved with it. C7 becomes "graph store as
a configurable port" and E5, the projection rebuild drill, is promoted from deferred to a
prerequisite of shipping the `neo4j` backend.

#### Verification evidence

- `ruff check .` clean · `mypy src` clean on **156** files · `lint-imports` 4 kept, 0 broken ·
  `alembic heads` = 1 (`d5f8b21c4a03`) · **1,199 tests passing**, no failures, no skips.
- The four runtime faults above were each confirmed by reading the call site, not by inference
  from the error text.

#### Current limitations

- **Nothing embeds the catalogue yet.** The provider exists and is tested against mocked
  transports; no vectors have been produced for real metadata, and no live call to either
  provider has been made from this repository.
- **The recall@10 evaluation has not been run** — 200–500 real steward questions, measured
  *after* policy filtering, per `review-2026-08/decisions/02-embedding-model.md`. Choosing the
  provider was the blocking decision; proving the choice is a separate piece of work.
- The lint repair touched 20 files that a parallel session had open. It was done only after
  confirming no writes since 05:49; if that session resumes on the same files, this is where a
  conflict will appear.
- `MAX_BATCH` is declared and not yet enforced by a chunking caller — the batching limit exists
  as a constant for the backfill that does not exist yet.

---

## 2026-08-30 (ninth entry)

### Neo4j: configurable, not removed — and a tree that moved underneath the last entry

Two things, recorded together because the second changes how to read the first.

#### The graph store becomes a setting (C7, ADR-0020 amendment)

Asked whether both backends could be offered under an admin setting, and whether that was hard.
It is not, and the reasoning is worth keeping because it reverses the framing of ADR-0020's
own reversal condition.

That condition said: reintroduce a graph store when p95 lineage traversal exceeds ~200 ms after
caps, on a real estate. Under a removal, satisfying it meant weeks of work *at exactly the moment
the estate was proving the need*. As a per-organization setting, satisfying it means changing a
value for one organization and measuring the result. **A decision that can be tested is worth more
than a decision that has to be defended**, so the amendment strengthens the ADR rather than
softening it.

The switch itself is cheap and the honest reason is that the surface is small: three modules read
Neo4j, a gating boolean (`lineage_neo4j_read_enabled`) already exists, and `vector_store.py` is
the same port-with-adapters pattern already built once under ADR-0019.

**What actually costs the time is not the switch.** Two backends must answer *identically* —
identical node sets, ordering, tie-breaks, cap behaviour and truncation reasons — or a
configuration flag has quietly become a correctness surface, and the user who hits the difference
has no way to diagnose it. One conformance suite run against both is the deliverable that makes
the setting safe. Beyond that: no Neo4j runs in the test suite today (INV-1's test says so
itself), so the second backend either joins CI or ships advertised as uncertified (INV-9); and
INV-1 confines the setting to lineage and exploration reads, never the authorization path or the
classification roll-up.

The cost named rather than absorbed: removal would have eliminated two overdue drills. Keeping
Neo4j puts them back and promotes **E5**, the projection rebuild drill, from deferred to a
prerequisite. A projection never proven rebuildable should not be offered as a selectable backend.

#### The working tree moved, and it no longer passes lint

The eighth entry's verification block said 716 tests and 120 files, clean. That was true at 04:30
and false by 12:25. The parallel session restarted, committed three times (latest `2c30a6f`), swept
this session's authorization work into its commits, and added roughly 35 modules — ABAC API,
studio, tool plans, view lineage, full-text index, fusion ranking, graph retrieval, compliance
packs, consumption and AI-decision lineage.

| | 04:30 | 12:25 |
|---|:--:|:--:|
| Tests passing | 716 | **1,187** |
| Source files (mypy) | 120 | 155 |
| `ruff` | clean | **168 errors** |
| `mypy --strict` | clean | **16 errors in 7 files** |
| Import contracts | 4 kept | 4 kept |
| Alembic heads | 1 | 1 |

**CI would be red.** The failures concentrate in the in-flight modules — `runtime_contracts.py`
(12), `injection_defense.py` (10), `tool_plans.py` (9), `retrieval.py` (8), `compliance_api.py`
(8) — and 119 of the 168 are auto-fixable, so this reads as work in progress rather than damage.
It is recorded because the eighth entry's numbers are now wrong, and this log's own rule is that a
correction is a new entry rather than an edit.

It is also the first live test of the consolidation rule written four hours earlier. `00-status.md`
now carries the moving-tree caveat in its header instead of a clean number that would have been
false within the day — which is the behaviour the rule was written to produce.

#### Verification evidence

- 1,187 tests pass, no failures, no skips; 4 import contracts kept; one Alembic head
  `d5f8b21c4a03`. `ruff` and `mypy` fail as tabulated above.
- No code was changed in this entry. ADR-0020 gained an amendment; `00-status.md` §1 and decision
  5, and tracker rows C7 and E5, were updated to match.

#### Current limitations

- **The lint regression is not this session's to fix.** Touching 46 files another session has open
  is how two sessions produce a merge conflict neither can explain. It is surfaced, not repaired.
- The C7 estimate (2–3 weeks) is judgement, not measurement: roughly a week for the port and the
  admin setting, a week for the conformance suite and the CI service container, plus E5.
- Nothing has yet been built for C7. This entry records a decision and its cost, not an outcome.

---

## 2026-08-30 (eighth entry)

### Documentation consolidation — twelve status documents down to one

Status had accumulated in twelve places, and four of them disagreed. This entry is the cleanup.
It changed no code.

#### What was actually wrong

Not sprawl for its own sake — the failure mode was specific. `60-delivery/04-status-matrix.md`
said 387 tests and Alembic head `9e4c7a12b5f8`; `05-gap-register.md` said retrieval was
lexical-only; `review-2026-08/gap/01-baseline-reality.md` said CI did not exist and there were two
import contracts; `10-architecture/01-principles-and-invariants.md` said four of nine invariants
had no test. Each was true when written. Together they meant **no document could be trusted
without checking the code**, which is the same as having no status document at all.

Worse, one false claim was sitting in an authoritative *contract*, not a status file:
`30-contracts/05-metadata-ingestion-envelope.md` stated that the envelope-1.1 pull path did not
persist the new axes. `workflows/activities.py` imports `persist_envelope_extensions` at line 27
and calls it at line 587. A reader would have concluded a shipped capability was missing.

#### The consolidation

`60-delivery/00-status.md` is now the single answer to "where are we": at-a-glance figures,
capability matrix, per-invariant status *with each one's remaining limit*, deliberate
simplifications, open gaps, the retest register, and the decisions waiting on a person. The two
documents it absorbed are in `_superseded/`.

Four rules were written into it, because a consolidation with no rule about what happens next just
resets the clock:

1. **One status claim, one home.** A document that needs to state status links to it. A dated
   `Implementation status` callout in a design document is the exception and must name the file
   that proves it.
2. **The accomplishment log is history, not status.** Being in it is not evidence of still being
   true.
3. **A completed work-item write-up keeps its design rationale and loses its status section.** The
   rationale explains why the code looks the way it does and is worth keeping.
4. **A superseded document moves to `_superseded/` with a header naming its replacement.** Never
   edited into agreement, never silently deleted.

#### Also done

- **The review's `C`/`N`/`E`/`D` item vocabulary was re-homed into `03-tracker.md`**, with status
  per item, so the open-work list is one list. `gap/02` keeps the original week and risk estimates
  and is now explicitly the historical plan.
- **All vendor research moved into one folder.** Four shallower competitor analyses were superseded
  by the review's primary-source research; the two Collibra deep-dives worth keeping moved to join
  it. `Docs/competitors/` is gone.
- **The two open decision briefs got their own home**, `review-2026-08/decisions/`. They were
  buried among completion write-ups with colliding numeric prefixes, which is a poor place for the
  only two documents in the folder that need someone to *do* something.
- **A published contract was moved out of a handoff note.** The fourteen SQL-validation finding
  codes — declared append-only, and a breaking change to every MCP client if renamed — lived in
  `gap/05-validate-sql-handoff.md`. They are now `30-contracts/09` §7.
- **`gap/06` presented a closed INV-7 breach as live.** Thirteen endpoints, fixed under ST-17, and
  the section still read as an open finding. Marked closed, with the finding kept because the
  finding is the useful part.
- Two numeric-prefix collisions resolved; the stale "the repository is not under version control"
  claim in `_superseded/README.md` corrected.

#### Verification evidence

- **Every relative link and backticked document path under `Docs/` resolves**, checked by script
  across all 130 files. One exception is deliberate and reads as such: ST-13 names the retired
  baseline snapshot as "formerly" at that path.
- Code untouched, and re-verified unchanged: `ruff check .` clean, `mypy src` clean on 120 files,
  `lint-imports` 4 contracts kept, 1 Alembic head, **716 tests passing**, 1 xfailed.

#### Current limitations

- **Paths in earlier entries of this log point at pre-consolidation locations.** This document is
  append-only, so they were not rewritten — that rule is worth more than the broken links. The
  mapping is in `_superseded/README.md`. In short: `60-delivery/04-status-matrix.md` and
  `05-gap-register.md` → `60-delivery/00-status.md`; `Docs/competitors/*` → `_superseded/` or
  `review-2026-08/research/`; `review-2026-08/gap/01` → `_superseded/27`; `gap/03` and the
  embedding brief → `review-2026-08/decisions/`.
- **`Docs/` is 130 files and this pass did not attempt to reduce that.** It removed *contradiction*,
  not volume. `20-modules/` (21 files) still describes a decomposition the code has not reached,
  and that is a code problem (ST-05 onward), not a documentation problem.
- The consolidation is only as durable as rule 1. Nothing enforces it mechanically — there is no
  test that fails when a second status claim appears, and writing one would mean parsing prose.

---

## 2026-08-30 (seventh entry)

### The authorization decision is now wired into production paths (Task #19)

For the last four entries this platform had a complete attribute-based authorization
system -- policy engine, workspace membership, expiring source bindings, rule-derived
roles, shadow mode -- with **48 passing tests and zero production callers**. Every prior
entry said so plainly. This entry closes that gap: `authorize` now decides on the
execution path, the validation path, four catalog read handlers and the retrieval
preview, and three tests exist to stop it becoming unwired again.

#### The problem that had to be solved first: which workspace is this request in?

`authorize` needs a workspace id. Nothing in the existing API surface supplies one --
the contracts predate ADR-0018 -- so a resolution step was unavoidable. The obvious
implementation is a hole:

> find the workspaces this principal belongs to, intersect with the ones bound to this
> datasource, take the match

That picks the workspace by where the caller already has access and then asks whether
the caller has access there. It reads as helpful and it is a check with a foregone
answer. **`aida/workspace_resolution.py` is subject-independent by construction:** every
rule looks only at the request and at platform state, never at who is asking. There are
exactly two ways to get a workspace -- the request names one, or the datasource has
exactly one live binding so the answer is forced. Two live bindings is
`WORKSPACE_AMBIGUOUS`: a refusal to answer, not a guess.

The single-binding path is what carries the existing estate, because the ADR-0018
migration gave every datasource exactly one grandfathered binding from its project.

#### An unresolved workspace is a third state, not a quiet allow

Most callers still name no workspace. Making that an allow would hide the size of the
remaining migration inside a boolean; making it a denial on the day the gate was wired
would take the platform down. It is its own state, with its own setting
(`unresolved_workspace_posture`, default `SHADOW`), a `decided=False` flag on the gate's
result so no caller can claim a check that did not happen, and a warning log line that
**counts the callers still to migrate**. Flipping that setting to `DENY` is the actual
completion of this rollout, and it is one setting, not a code change.

#### Wired at the choke point, not at the handlers

The gate sits inside `QueryExecutionGateway.execute` and `.validate`, which INV-2
already makes the only path to a warehouse. Gating the four callers instead (two HTTP
handlers, the MCP tool surface, the agent orchestrator) would mean the gate is present
on the ones somebody remembered.

`AuthorizationRejected` subclasses `QueryRejected` deliberately: every existing caller
already knows how to record a rejection, with the execution id, failure reason and run
status handling that goes with it. A sibling exception type would have meant each caller
either grew a branch or let a denial escape as a 500 -- and the second outcome happens to
whichever caller is overlooked. Handlers that want the right status catch it first and
answer **403**, not 422: the statement was never the problem.

Authorization runs *after* the execution row and its `requested` audit entry exist, and
before anything reaches a connector. A denied attempt therefore leaves a REJECTED row
naming who asked and why (INV-7), which gating earlier would have thrown away.

#### Two design corrections found while building it

* **A shadow record written into the caller's session is discarded exactly when it
  matters.** A read handler never commits and a rejected execution rolls back, so the
  most interesting divergences -- the ones attached to requests that failed -- would be
  the least likely to survive, biasing the readiness report towards agreement. Since
  that report is what a human reads before flipping a workspace to `ENFORCE`, the record
  now gets a transaction of its own (`record_divergence_durably`).
* **The INV-7 mutation scan was right to flag the gated GETs, and the fix was not an
  exemption list.** Once a read path is gated every gated GET reaches a `session.add`,
  and a route-keyed exemption list would grow with each one. `NON_GOVERNED_WRITERS` in
  the shared scan says instead that recording an authorization divergence is not a
  mutation of governed state -- nothing reads it, no object differs because it exists --
  and `test_the_excluded_writers_only_ever_write_attributable_shadow_records` asserts
  that claim rather than trusting it.

#### Verification evidence

- `ruff check .` clean; `mypy src` clean on 120 files; `lint-imports` **4 contracts kept,
  0 broken**; `alembic heads` = 1. **716 tests pass** (26 new), 1 xfailed.
- **This change adds no migration.** Schema is untouched; the only configuration addition
  is one setting with a safe default.
- **Measured proof the gate is not a no-op:** the suite was re-run with
  `AIDA_UNRESOLVED_WORKSPACE_POSTURE=DENY`. **17 tests fail**, every one of them a test
  whose double supplies no source binding. The gate is live, it fails closed, and the
  failures name exactly the surfaces that must pass a workspace id before the posture
  can flip.
- The static half of `test_inv4_authorization_wiring.py` would pass against a `gate` that
  returned True unconditionally; the behavioural half would pass against a perfect gate
  nothing called. Both are present because neither is worth much alone, and
  `test_the_scan_would_notice_if_a_gate_were_removed` keeps the static half able to fail.

#### Current limitations

- **Nothing is denied in production today**, and that is the intended state: every
  workspace is in `SHADOW`, the unresolved posture is `SHADOW`, so behaviour is
  unchanged. What exists now is the *measurement* -- divergences and unresolved-workspace
  counts accumulating per workspace -- that makes flipping to `ENFORCE` evidence-based.
  Claiming enforcement on the strength of this entry would be false (INV-9).
- **Clients do not yet pass `workspace_id`.** `QueryExecutionRequest` and
  `GatewaySqlValidationRequest` accept it and no caller sends it, so resolution runs
  through the sole-binding path. The DENY rehearsal above is the list of what to fix.
- **Coverage is the execution path plus five read surfaces**, not every read in the
  platform. Query lineage, glossary, stewardship, semantic and marketplace reads are
  ungated; `test_the_scan_would_notice_if_a_gate_were_removed` currently uses
  `get_query_lineage` as its ungated control, which is itself a reminder that it should
  be gated.
- The per-request cost of a resolved gate (binding lookup, workspace, membership, rules,
  classification scope, policy load) has been measured only on the synthetic estate from
  the ADR-0019/0020 benchmarks (auth hot path 0.8 ms with the materialised roll-up). It
  has not been measured against the bank's real catalogue.

---

## 2026-08-30 (sixth entry)

### Cross-session review: one gap neither session would have found alone

The parallel session stopped after six commits (validate_sql, all nine Tier-0 invariants,
a documentation truth pass, envelope 1.1 across four connectors, INV-7 auditing). Reviewing
that work against the same adversarial standard found one defect that exists precisely
*because* two sessions worked in parallel: each was internally consistent, and the
inconsistency lived between them.

#### What the other session built, assessed honestly

Strong work, and better than mine in two places:

* **`validate_sql`** is real and shares `_run_validation` with `execute`, so "what
  validation says" and "what execution enforces" cannot drift. Exactly the design the
  review recommended.
* **Five invariant test files** (~90 KB) that include *meta-tests* -- `test_the_cypher_scan
  _finds_the_statements_it_is_supposed_to`, `test_the_control_plane_scan_would_notice_a
  _leak`, `test_the_unauthenticated_route_list_stays_closed`. These are the guard against
  the failure mode this session hit on its own INV-6 test, which asserted nothing. Better
  practice than mine.
* Exemption lists are tight and justified: **3** unauthenticated routes, **8** tenant-free
  routes each carrying a written reason.
* INV-1 is explicit that it does not prove Neo4j ingests correctly, because no Neo4j is
  running. Honest about its own limit rather than claiming the invariant outright.

#### The gap

**The same codebase now treated persisted SQL two different ways, and the newer path was
the unsafe one.**

`dbt_artifacts.py` has always stored `compiled_sql_hash` + `compiled_sql_redacted` and
never the raw artifact. Envelope 1.1's `metadata_view_definition.definition_sql` and
`metadata_routine.body_sql` stored source SQL **raw**. A view defined
`... WHERE ssn = '123-45-6789'` landed verbatim in the control plane -- a source value
written in a different syntax, which is exactly what INV-6 forbids.

It was invisible because **INV-6's own test drives only the query gateway**, so the tables
envelope 1.1 introduced sat outside the scan entirely. A test is only as strong as the
paths its author had in mind.

#### Fixed, migration `d5f8b21c4a03`

Columns replaced with `*_redacted` + `*_fingerprint` + `redaction_status`, raw columns
**dropped rather than migrated** -- carrying the text forward would defeat the change.
Redaction extracted to `aida/sql_redaction.py`, which also removed a latent L1-imports-L3
edge (ingestion would otherwise have imported the gateway).

**A design problem surfaced while building it, and changed the design.** Fail-closed
redaction -- "store nothing that does not parse" -- discarded most *procedure bodies*,
because `BEGIN ... END` is procedural rather than a single statement and every dialect
spells it differently. That would have thrown away the text envelope 1.1 exists to capture,
and with it procedure lineage: one of the four capabilities no competitor offers. So
redaction now has three tiers -- `PARSED` (node-level, precise), `LEXICAL` (literals
removed by scanning; structure survives, values do not), `UNPARSED` (nothing stored).
Removing literals never actually required a parse.

Numeric literals are scrubbed as well as quoted strings: an account number is as likely to
appear unquoted as quoted, and `LIMIT 100` losing its number is the accepted cost of not
guessing which numbers are values.

#### Ingestion-time screening (ADR-0013, N18) -- shipped

`aida/ingest_screening.py` runs the existing deterministic classifier over source-supplied
text at **write** time, recording a verdict and quarantining what fails. Screening once on
write is cheaper and more complete than screening on every read, and quarantine changes
*eligibility for model context* rather than deleting a source's own metadata.

The gap this closes is recorded in ADR-0013, threat model T7, AI-safety AS-1 and
agent-runtime AG-1, and was addressed in none of them. Envelope 1.1 had just made it much
larger: a procedure body is kilobytes of source-controlled text that meaning inference and
tool generation are both designed to read.

Stated plainly in the module: this is defence in depth and it is the weaker layer. INV-3 is
load-bearing -- a successful injection still produces a proposal that cannot execute.

#### INV-6's test now covers the ingestion path

The systemic fix, not just the instance. Sentinel-laden view and procedure SQL is driven
through the real redaction path and searched for leakage.

#### Verified on PostgreSQL 16, with an SSN actually in the database

Seeded a raw view definition containing `123-45-6789` at the pre-migration revision, then
migrated. **5 of 5**: rows preserved, raw column dropped, the SSN gone, CHECK constraint
rebuilt around the new column, pre-existing row still satisfying it. Downgrade and
re-upgrade clean with data present.

#### A process failure worth recording

The single-head migration gate produced **two heads twice**, and the second time this
session missed it -- because the verification command used `alembic heads | tail -1`, which
showed one line and hid the second head. The gate was built in Phase 0, then not actually
run. A check you own is not a check you performed.

689 tests passing, ruff and mypy clean, 4 import contracts kept, one head.

---

## 2026-08-30 (fifth entry)

### A devil's-advocate check before building found that the next planned change was an outage

The plan stated at the end of the previous entry was "wire `authorize` into the read and
execution paths". Challenging that plan before implementing it, rather than after, produced
the most valuable finding of the day.

#### The plan would have denied every request in the platform

The ADR-0018 migration backfills one workspace per project and **zero memberships**. It
backfills zero because there is nothing to backfill *from*: this codebase has **no persisted
principal table at all** -- identity and roles arrive as OIDC claims per request and are
never stored. (Module 01's spec claims a principal registry; it does not exist. Third
doc-versus-code gap of the day.)

Verified on the populated rehearsal database: 24 workspaces, 48 active source bindings,
**0 memberships**. Wiring `authorize` in would have returned `NO_WORKSPACE_MEMBERSHIP` for
every request. The 14-assertion migration rehearsal missed it because not one assertion
asked "can anyone actually get in".

#### What was built instead

Seeding 24 synthetic owners would have invented an access grant nobody made. Two mechanisms
instead, migration `c9d1a83e6b47`:

* **`workspace_access_rule`** derives workspace membership from an IdP role, scoped to a
  workspace, to everything under a business node, or org-wide. Seven rules per organization
  cover every migrated workspace; revoking a rule revokes the access; rules only grant, and
  a DENY policy still outranks them.
* **Shadow mode.** `workspace.authorization_mode` defaults to `SHADOW`, where the full
  decision is computed, divergences are recorded in `authorization_shadow_record`, and
  nothing is denied. `authorize_enforced` is what surfaces call; `authorize` stays public
  for the probe endpoint, which wants the unmodulated answer. `enforcement_readiness`
  summarises the shadow record so flipping a workspace to `ENFORCE` is a measurement.

Re-verified on the populated PostgreSQL rehearsal: 7 rules seeded, 0 workspaces enforcing,
0 memberships invented, ADR-0018 backfill intact, upgrade/downgrade round trip clean.
**Lockout risk: none.**

#### A bug class, not a bug

The timezone-naive/aware comparison defect found in the previous entry appeared **twice
more** while building this -- in `workspace_access._live` and latently in
`workspace_service._expired`. Three occurrences in one day makes it a class, so it now has
one implementation: `aida/timeutil.py`, with `as_utc`, `is_expired`, `is_live` and
`same_instant`, and a test pinning the behaviour. Both of the new sites were **expiry checks
on access grants**, which is the worst possible place for a backend-dependent answer.

#### The CI gate caught its first real defect

Applying the new migration produced `Multiple head revisions are present` -- the parallel
session had authored a migration from the same parent. That is a merge accident which
normally surfaces at deploy time, and the single-head gate added in Phase 0 caught it.

Resolving it produced a second, sharper failure: both authors rebased onto the other
simultaneously, creating a revision **cycle** -- which `alembic heads` cannot even report,
because it raises instead of returning a count. Resolution rule recorded in the migration:
whoever moved last yields. Chain is linear again.

#### Hub-shaped lineage measured, and it reframed the ADR-0020 caveat

ADR-0020 listed hub fan-out as unmeasured and as the likeliest weakness in choosing
PostgreSQL over Neo4j. Measured downstream through a single hub column on the 880,000-edge
DAG: 50,000 fan-out at depth 12 costs **3,402 ms** and reaches 480,000 nodes.

But cost tracks **nodes reached, not depth** -- and that is not a graph-database problem,
because Neo4j must also materialise 480,000 nodes. Bounding the traversal, which ADR-0010
already mandates, takes the same worst case to **1.5 ms** at a 1,000-node cap. A degree
pre-check ("is this a hub?") costs 8.8 ms.

The mitigation was already the design; it had simply never been shown to be load-bearing.
ADR-0020 now carries three binding requirements: node caps with explicit truncation, a hub
degree pre-check before traversal, and precomputed impact summaries for high-degree nodes.

#### Also produced

`gap/05-embedding-model-decision-brief.md` -- deliberately a decision *input*, not a
decision. Which embedding model runs and where is a model-risk and procurement question,
and the irreversible part is that `index_signature` pins model, version, dimensions and
chunking, so changing the model means re-embedding the estate.

#### Known limitations

- Surfaces still call neither `authorize` nor `authorize_enforced`; the safe wiring now
  exists but no endpoint uses it. That is the next change and it is now genuinely safe.
- Hub measurement is synthetic. The real estate's degree distribution is still unknown, and
  what matters is how many columns exceed a 1,000 fan-out.
- No embedding model chosen, so retrieval remains lexical-only.

---

## 2026-08-30 (fourth entry)

### Adversarial self-review: five defects in work that had already shipped green

Prompted by a direct challenge — "how can I trust your analysis, and are you making a
big mistake?" — the ADR-0018/0019 work was attacked rather than re-read. All five
findings below were in code that had passed review, passed `mypy --strict`, and shipped
inside a suite of 575 passing tests.

The method mattered more than the findings: tests were written to *fail first*, and each
was watched failing before anything was changed. Three are now permanent regressions in
`tests/test_regressions_from_adversarial_review.py`.

#### 1. Fail-open tenant isolation (INV-5) — the serious one

`workspace_service.authorize` read
`if context.organization_id is not None and <organization mismatch>`, so a caller
claiming **no** organization skipped the cross-organization check entirely. Development
identity makes `X-Organization-Id` optional, so `None` is reachable from outside.

This was in the function whose whole purpose is INV-5, and the suite already contained
two cross-tenant tests — both of which supplied an organization and therefore never
reached the branch. Fixed: an absent tenant claim is now `NO_ORGANIZATION_CONTEXT`, a
denial. Deliberately no `PlatformAdmin` bypass, unlike the older `enforce_organization`:
a workspace is a tenancy boundary and INV-5 says isolation is total.

#### 2. An allowlist matching on the wrong key

`PostgresBruteForceIndex.search` filtered candidates by `owner_id`, which is unique only
*within* an `owner_type`. An allowlist authorising `("TABLE", "x")` also admitted
`("COLUMN", "x")` — an object the policy filter had not authorised. Fixed by matching the
full pair, narrowing in SQL on the indexed column and completing the match in Python
because a tuple `IN` is not portable across the dialects in play.

#### 3. A constraint violation visible on only one backend

Re-asserting an assignment at the same instant set `effective_to == effective_from` and
then inserted a row colliding on the unique key. **The first fix did not work**, and the
reason is the finding: timestamps read back aware from PostgreSQL and naive from SQLite,
so `stored == supplied` was silently False on the test backend and would have been True
in production. Backend-dependent comparison is the worst failure shape there is, because
no single environment reveals it. Fixed with explicit UTC normalisation (`_as_utc`).

#### 4. A test that asserted nothing

`assert "tbl_1" not in rendered or decision.matched_policy_id is not None` — the
right-hand side is always true, so the assertion always passed. It was the INV-6 test,
meaning the one control claimed as verified was the one not verified at all. Rewritten to
assert both halves; the original kept in a comment as a standing reminder that a green
test is not evidence until it has been watched failing.

#### 5. A performance bug behind a correct-looking fallback

`rollup` fell through to the expensive recompute whenever the materialised result was
empty — but empty is ambiguous between "nothing assigned here" and "projection not built",
and the first is the common case early in an estate's life. The ~3 s query would have run
constantly on precisely the nodes the materialisation exists to make fast. Fixed with an
existence probe.

### Also produced

`Docs/review-2026-08/gap/04-how-to-verify-this-work.md` — every claim in the review
paired with the command that checks it, every performance number with the dataset shape
that produced it, and an explicit list of what has *not* been verified. It includes the
instruction to break the INV-2 import contract deliberately and watch it fail, because a
contract that has only ever passed is not evidence of anything.

### Known limitations — unchanged and still open

- Nothing is wired into the read and execution paths; ABAC and the vector index decide
  nothing in production traffic.
- Hub-shaped lineage remains unmeasured and is the likeliest weakness in ADR-0020.
- No embedding model is configured.
- CI has never run on a remote.

---

## 2026-08-30 (third entry)

### Three assumptions challenged, measured, and two of them corrected

Prompted by three questions from the product owner. Each was answered with a benchmark
against real PostgreSQL 16 rather than an opinion, because two of the three concerned
claims this project had asserted without evidence.

#### 1. `pgvector` is not available in the target estate — ADR-0019 accepted

The retrieval design named `pgvector` as a fact. It is not one: the bank's PostgreSQL has
no `vector` extension, and `CREATE EXTENSION` needs a privilege a DBA will not grant a new
platform. An architecture that requires an extension the operator cannot install is a
design defect, not a deployment detail.

Nearest-neighbour search is now a **port** (`aida/vector_store.py`) with four adapters:
exact cosine over `bytea` in plain PostgreSQL (default, needs nothing), the bank's
in-network vector service over HTTP, `pgvector` where it genuinely exists (probed via
`pg_available_extensions`, never trusted from configuration — INV-9), and disabled.
Embeddings live in a new `embedding` table with a stored norm and an `index_signature`
pinning model, version, dimensions and chunking; vectors from different signatures are not
comparable, and mixing them fails as quietly poor search rather than as an error.

*Measured* end to end against 200,000 stored 768-dimension embeddings — fetch, unpack,
score, top-25: **200 candidates 45 ms, 1,000 → 100 ms, 5,000 → 427 ms, 20,000 → 1,697 ms.**
That is the honest envelope, and it moved the default candidate cap from a guessed 20,000
to a measured 5,000. The cap is a refusal with a reason code, not a truncation: scoring an
arbitrary slice of a larger set returns plausible answers that are wrong.

Recorded and not glossed: **embeddings are not anonymous.** Embedding-inversion research
recovers substantial portions of source text from vectors alone, so the embedding store
inherits the classification of what was embedded, an external index must be inside the
bank's network, and there is no hosted-vector-API mode.

#### 2. "Will recursive SQL scale?" — the tree was fine, the aggregation was not

The distinction that matters and was easy to miss: the recursive CTE walks the *taxonomy*,
never tables or columns. Measured against a bank-scale taxonomy (13,548 nodes, depth 4) and
5,000,000 assignments:

| Operation | Before | After |
|---|---:|---:|
| Descendants of an LOB | 3.3 ms | 1.5 ms (closure) |
| Authorization scope for one table | 26 ms (two round trips) | 0.8 ms (one query) |
| Roll-up over a subtree | **3,147 ms** | **0.4 ms** (materialised) |

So: a closure table (`business_node_closure`) for traversal, a materialised
`business_node_rollup` for aggregation — full recompute of every node takes ~47 s as one
grouped statement, which is a batch job, not a request — and `classification_scope`
collapsed from two round trips into one because it sits inside a 50 ms authorization
budget. `computed_at` is returned to callers so roll-up staleness is visible rather than
hidden. Migration `a7c3e91d4f28`.

#### 3. "Wouldn't Neo4j be better for lineage?" — measured; the answer held, the argument did not

The original recommendation to drop Neo4j was a cost argument and was too glib about
capability. A graph database genuinely is better at deep, variable-length paths, and
lineage is where depth is real, so the honest question was how deep this product's
traversal actually goes.

A bank-shaped column-level DAG — 12 layers, 40,000 columns per layer, real fan-in,
**880,000 column-level edges** — traversed upstream from one report column on PostgreSQL:
depth 4 → 0.7 ms, depth 8 → 1.6 ms, **depth 12 → 10.8 ms reaching 3,637 nodes.** The
join-per-hop cost does not bite at this depth and edge count, and the 1–4 hop cap in
ADR-0010 turns out to be a product decision about what to render rather than a performance
ceiling.

ADR-0020 records the decision *and what was not measured*: hub-shaped fan-out (a shared key
column feeding tens of thousands of downstream columns), all-paths enumeration, and graph
algorithms. Each is a named reversal condition with a threshold, not a hand-wave.

Worth noting the first attempt at this benchmark measured nothing — the synthetic graph
collapsed to a linear chain with no branching. It was rebuilt before any conclusion was
drawn from it.

#### The migration rehearsal gap is closed

The previous entry listed "the migration has not been run against a populated PostgreSQL
database" as a real gap. It has now been run, on PostgreSQL 16:

- Full 37-migration chain from base to head on an empty database — clean.
- Then the real rehearsal: populated the pre-ADR-0018 tables with 6 LOBs, 24 domains
  (including a sub-domain layer), 24 projects and 48 datasources, and ran the chain over
  it. **14 of 14 backfill assertions passed** — workspace count, node count, parent chains
  for both DOMAIN→LOB and SUB_DOMAIN→DOMAIN, assignment count, grandfathered ACTIVE
  bindings, code uniqueness under namespacing, 4 ACTIVE + 1 DRAFT seeded policies, closure
  and roll-up populated, and the pre-ADR-0018 tables untouched.
- Full round trip: upgrade → downgrade → upgrade. New tables removed cleanly, original data
  intact, backfill reproduced identically.

#### Known limitations — still open

- **Nothing is wired into the read and execution paths yet.** ABAC and the vector index
  both exist, are tested, and decide nothing in production traffic. Unchanged from the
  previous entry and still the next thing to do.
- **Hub-shaped lineage is unmeasured** and is the likeliest place the PostgreSQL graph
  decision hurts. It should be measured on the real estate once view and procedure parsing
  land and the graph has a realistic degree distribution.
- **No embedding model is configured.** `embedding_model_id` defaults to `unset`; nothing
  produces vectors yet, and which model runs where is a model-route governance question
  (ADR-0009) that has not been answered.
- Scoring runs in Python. `numpy` would cut the scoring half of the cost substantially and
  was deliberately not added, because a dependency should be paid for by a measurement.
- The `pgvector` adapter is declared and refuses with `PGVECTOR_ADAPTER_NOT_IMPLEMENTED`
  rather than existing untested (INV-9).

---

## 2026-08-30 (second entry)

### ADR-0018 three-axis tenancy -- schema, engine, API and tests

#### Completed

**Steps 1-4 of the ADR-0018 migration are built. Step 5 is deliberately not.**

*Access axis.* `workspace`, `workspace_membership`, `source_binding` and
`isolation_boundary` models plus migration `f1a2b3c4d5e6`. A workspace is created with
its first owner in one call, because a workspace with no owner is one nobody can
administer and making that state reachable invites it.

*Classification axis.* `business_node` (LOB / SUB_LOB / DOMAIN / SUB_DOMAIN / CONCEPT,
self-referencing, effective-dated), `business_assignment` (many-to-many, polymorphic
target, effective-dated) and `business_assignment_rule`. `business_graph.py` provides
descendant and ancestor traversal by recursive CTE, `nodes_for_target`,
`classification_scope`, `rollup` and `as_of` history.

*Policy.* `policy_engine.py` -- pure, no I/O, exhaustively unit-testable. DENY is a hard
ceiling at any priority including PlatformAdmin; default is deny; `principal_kind` is a
first-class subject attribute; MASK and FILTER obligations accumulate; ALLOW ties break
deterministically so a decision replays identically a year later.

*Enforcement.* `workspace_service.authorize` is the single entry point, failing closed at
every step in order: workspace unavailable, cross-organization, no membership, role does
not permit the action, no active binding, binding expired, outside schema scope,
classification outside binding, then the policy decision.

*HTTP.* `workspace_api.py` -- workspaces, memberships, source bindings with maker-checker
approval, business nodes and assignments, `as_of` tree, roll-up, access policies, and an
`/authorization-probes` endpoint that answers "what would you decide, and why" without
performing the action. Every mutation audits in the same transaction (INV-7).

*Migration behaviour.* The backfill creates one workspace per project keeping its slug, a
business node per LOB and per data domain preserving the parent chain, `MIGRATED`
assignments for every project and datasource, and grandfathered ACTIVE source bindings so
existing access does not break at the moment the binding model is introduced. Seeded
policies reproduce today's RBAC outcomes exactly; the one policy that would change
behaviour (agents denied sensitive classifications) is seeded `DRAFT`.

**INV-5 is now formalised in the Tier-0 invariant suite** -- the first of the five
previously-unformalised invariants to close. The earlier docstring said INV-5 needed "a
running app plus a much heavier per-route fake-session harness"; that stopped being true
once tenant isolation had a single enforcement point, so it is asserted against that
function with a real in-memory database instead.

*Verified*, in a clean checkout using the exact CI recipe: ruff clean, mypy clean across
110 files, 3 import contracts kept, 1 Alembic head (`f1a2b3c4d5e6`), **424 tests passing**
(up from 387; +35 new across `test_policy_engine.py`, `test_workspace_authorization.py`
and the two new Tier-0 INV-5 cases).

#### Found while doing the above

**A real bug in the recursive CTE traversal, caught by a warning rather than a failure.**
The first implementation built its live-node predicate against the un-aliased
`BusinessNode` inside the recursive term, which silently added a second FROM entry -- a
cartesian product with the whole table, filtering on "some row is live" rather than "this
row is live". It returned correct results on the small test tree and would have returned
wrong ones on a real estate. SQLAlchemy emitted a cartesian-product `SAWarning`; the
predicate helper now takes the entity or alias explicitly and the joins are written out.
The tests were re-run with warnings escalated to confirm none remain.

#### Notable choices

**These are the first tests in the repository that run against a real database.** SQLite
in memory, added as a dev-only dependency. The behaviour under test is recursive CTE
traversal, effective-dated history and multi-step authorization; a fake session would have
asserted that the fake behaves, not that the SQL does. The full 89-table schema creates
cleanly on SQLite, so the fixture is a few lines rather than a harness.

#### Known limitations -- explicitly still open

- **Step 5 of the migration is not started.** The tenancy columns are still authoritative
  and no repository base class exists to scope on `(organization_id, workspace_id)`;
  `src/atlas/platform/` holds config, context, db and logging only. This depends on the
  module decomposition (ST-05/06/07).
- **No endpoint is routed through `authorize` yet.** The entry point, the engine and the
  probe endpoint exist and are tested; wiring the existing read and execution paths through
  them is the next change. Until then ABAC decides nothing in production traffic -- which
  is also why migration day changes no behaviour.
- **The p95 ≤ 50 ms authorization budget is unmeasured.** `load_policies` runs per request
  with no cache, and `classification_scope` issues two CTE queries. Both are obvious
  caching targets and neither has been profiled.
- **Residency is not an attribute** (tracker PG-1 remains PARTIAL for that reason).
- Purpose is matchable but not mandatory per session.
- The migration has been verified for correctness by reading and by schema creation, but
  **has not been run against a populated PostgreSQL database.** That is a real gap: the
  backfill is the part most likely to surprise, and it deserves a rehearsal on a copy.

---

## 2026-08-30

### Phase 0 — "make the invariants true" (independent architecture review, `Docs/review-2026-08/`)

#### Completed

**Continuous integration now exists** (tracker ST-02, closed). `.github/workflows/ci.yml`
adds five gates across three jobs: `ruff`, `mypy` (strict), `lint-imports`, an
exactly-one-Alembic-head guard, and `pytest`. `UV_FROZEN=1` makes a stale `uv.lock` itself a
failure. Before this date there was no pipeline at all, while `40-engineering/03-coding-standards.md`
and `30-contracts/01-contract-strategy.md` both stated that checks "fail CI."

*Verified*, in a clean checkout outside the working tree, using the exact CI recipe
(`uv sync --frozen --extra dev`): ruff clean; mypy clean across 106 source files; 3 import
contracts kept, 0 broken; 1 Alembic head (`e6d5b8c6bcef`); **387 tests passing**.

**INV-2 gateway exclusivity is now enforced rather than asserted** (tracker QG-7, closed;
ADR-0004's named mechanism, outstanding since that ADR was accepted). The SQL-accepting pair
`estimate_read_query` / `execute_read_query` was moved off the `Connector` ABC onto a new
`aida.connectors.sql_execution.SqlExecutor`. `ConnectorRegistry.create` still returns
`Connector`, which now has no SQL-accepting member. `aida.connectors.execution_access` is the
sole source of a `SqlExecutor`, and the import-linter contract *"INV-2 connector SQL execution
is reachable only from the query gateway"* permits exactly one importer.

*Verified by making it fail, not only by making it pass:*

- Adding `from aida.connectors.execution_access import ...` to `aida.api` breaks the contract:
  `Illegal imports of protected package aida.connectors.execution_access: aida.api -> ... (l.22)`.
- Calling `connector.execute_read_query(...)` on a registry-produced connector is rejected by
  mypy: `"Connector" has no attribute "execute_read_query" [attr-defined]`.
- Both probes were reverted; the tree is clean.

The Tier-0 AST scan (`test_no_connector_execution_outside_gateway`) was widened to cover
`estimate_read_query` as well — it takes a caller-supplied statement exactly as
`execute_read_query` does — and a new test,
`test_the_connector_handed_to_the_platform_has_no_sql_surface`, fails if the methods are ever
moved back onto `Connector`, a change that would leave the import contract and the AST scan
passing while the type-level guarantee silently disappeared.

**The `09` ↔ `16` import cycle does not exist** (tracker ST-11, closed). Checked against the
code before redesigning anything: `query_gateway.py` imports no lineage module, no lineage
module imports the gateway, and `extract_column_lineage` is defined inside `query_gateway.py`
and called only there. The mutual edge was an error in the module register in
`10-architecture/04-module-decomposition.md` §3/§4, not a property of the import graph. Rule
recorded: **the gateway emits, intelligence modules consume.** No layer diagram redraw needed.

**Pre-existing gate failures fixed so CI is green on its first run** rather than red on
arrival: 6 ruff errors (4 × E501, 2 × unsorted imports) and 2 mypy errors.

#### Found while doing the above

- **`PyYAML` was an undeclared dependency.** `src/aida/context_compiler.py` imports `yaml`;
  nothing in `pyproject.toml` declared it, and it resolved only transitively. Now declared
  (`PyYAML==6.0.3`) with `types-PyYAML` in the dev extra. `uv.lock` regenerated — it had also
  been missing the dev extras entirely, so `import-linter` was not in the lockfile.
- **`domain_service.resolve_domain` returned `DataDomain | None` against a `DataDomain`
  annotation.** An unresolvable `data_domain_id` returned `None` for callers to dereference.
  Now raises (INV-4, fail closed) rather than returning a value the type says cannot occur.

#### Known limitations — explicitly still open

- `bandit`/SAST and `pip-audit` are named as CI gates in `03-coding-standards.md` and are
  **not wired**; the tools are not in the `dev` extras. Marked as such in that table.
- The import-linter contracts cover three narrow, real invariants. There is still **no layering
  or independence contract over the flat `aida` package** — that lands with decomposition
  (ST-05/06/07), all of which remain TODO.
- CI has never actually run: this workflow file has not yet been pushed to a remote. The recipe
  was verified locally in a clean checkout, which is not the same as a green run on GitHub.
- Five of the nine Tier-0 invariant tests remain unformalised (INV-1, 5, 6, 7, 9), for the
  reasons the test module's own docstring gives. Unchanged by this work.
- Every operational drill remains **never run**. Unchanged by this work.

### Decisions

**ADR-0018 accepted; ADR-0017 superseded before acceptance.** Access, classification and
technical hierarchies are modelled as three independent axes, and only access grants. Tenancy
becomes `organization → workspace`; `line_of_business` and `data_domain` become effective-dated
`business_node` classification records with many-to-many assignments; policy becomes
attribute-based and keys on classification. `legal_entity` is withdrawn rather than deferred —
it has never existed in the schema. ADR-0017's `cross_boundary_grant` mechanism is retained.

The triggering argument is ADR-0017's own recorded reversal condition — *"domain taxonomy turns
out not to nest cleanly (a table genuinely needs two sibling domains)"* — which is structurally
met in a bank estate rather than being a future risk. **No migration code has been written; the
schema is unchanged.**

---

## 2026-08-24

### Completed

- Reviewed the original architecture, metadata engine, runtime, data model, security, operations, and backlog documents.
- Reframed delivery for a large regulated bank with multiple LOBs and thousands of databases.
- Selected a hybrid deterministic/LLM architecture.
- Selected Temporal for durable enterprise workflows and Kafka for replayable integration/projection events.
- Kept the analytical agent runtime framework-neutral and established a typed state-machine boundary.
- Established PostgreSQL as authoritative and Neo4j/vector/search as rebuildable projections.
- Established the Query Execution Gateway as the mandatory choke point for generated queries, approved tools, and platform workloads.
- Confirmed local Docker Desktop, Docker Compose, Python, and Node prerequisites.

### In progress

- Production-oriented repository and local platform scaffold.

### Known limitations

- Formal bank infrastructure, identity, regulatory, residency, source inventory, RPO/RTO, and model-hosting requirements are not yet available. Working assumptions are recorded in `08-enterprise-assumptions-decisions.md`.

## 2026-08-25

### Completed

- Created the Python 3.13/FastAPI repository, pinned dependencies, Alembic migrations, non-root image, tests, linting, and strict type checking.
- Started a persistent Docker platform with PostgreSQL/pgvector, Temporal and UI, Redpanda and console, Neo4j, Redis, MinIO, API, migration job, metadata worker, transactional outbox publisher, graph projector, and sample banking source.
- Implemented organization, LOB, project, and datasource tenancy with organization enforcement on resource access.
- Implemented explicit development identity, production fail-closed configuration, role gates, credential references, structured audit records, correlation IDs, health probes, and Prometheus metrics.
- Implemented the connector SDK and PostgreSQL adapter for connection testing, discovery, EXPLAIN, read-only query execution, and bounded safe profiling.
- Implemented retryable Temporal discovery/profiling with heartbeats, fingerprints, idempotent persistence, deterministic sensitive-column classification, immutable run-scoped profiles, and no persisted source values.
- Implemented PostgreSQL transactional outbox publication to Kafka and idempotent Kafka-to-Neo4j projection.
- Implemented SQLGlot AST controls for one read-only query, mutation/admin/function/join/wildcard denial, enforced limits, catalog authorization, source EXPLAIN cost policy, read-only transaction timeout, conservative masking, and query lineage.
- Implemented a framework-neutral governed agent orchestration envelope with explicit state transitions, semantic/policy pinning, a fail-closed model route, development-only generated-SQL injection, deterministic gates, question hashing, and execution evidence.
- Added metadata inventory, safe profile, graph summary, SQL validation, governed execution, and agent-analysis APIs.
- Added the local operations runbook, automated end-to-end verifier, and prioritized enterprise gap register.

### Verification evidence

- Static checks: Ruff clean; strict mypy clean across 27 source files; 23 automated tests passing; Alembic reports no model drift at revision `3df18be7a420`.
- Durable metadata run `0ddf4a63-6e4e-4dc2-9802-197bb12a365f` completed through Temporal: 1 catalog, 2 schemas, 4 tables, 22 columns, 4 table profiles, and 22 column profiles.
- Agent run `c36df4f3-4334-48f1-9441-319296a24575` completed with semantic snapshot pinning and a 10-state trace.
- Query execution `c20e04a4-d782-4988-b9f5-77cdacc6d9ea` passed AST, catalog, EXPLAIN, cost, and execution gates; `customer_name` and `email_address` were masked.
- Wildcard, mutation, and uncatalogued-table test queries were denied with HTTP 422 before source execution.
- A missing approved model route was denied with HTTP 503; the runtime did not silently substitute an unapproved provider.
- Readiness confirmed PostgreSQL and Temporal; Neo4j contained 1 catalog, 2 schemas, 4 tables, and 22 columns for the initial tenant.
- Final isolated verifier run `d0ee311b-f3a6-4319-82d6-6c8965d61f3f` discovered 7 source constraints; the reconciled graph reported 7 constraint nodes, 3 foreign-key relationships, and zero object-count lag.
- Verified sensitive-expression lineage masking for renamed and derived outputs; persisted query evidence redacts literals and user/query fingerprints use a keyed HMAC.
- Verified a non-admin principal from another organization receives HTTP 403 when probing a foreign datasource.

### Current limitations

- The model gateway is intentionally disabled until a bank-approved route and AI governance controls are supplied.
- PostgreSQL is the first certified connector; other engines require adapters and certification fixtures.
- Local Docker services are single-node engineering infrastructure, not the target HA deployment.
- OIDC, vault, production ABAC, source-delegated identity, production topology, and DR evidence remain production gates in `12-enterprise-gap-register.md`.

### Enterprise functionality iteration

- Added active/deprecated lifecycle state and tombstone timestamps across catalogs, schemas, tables, columns, and constraints. Re-scans now report created, changed, and deprecated object counts and reactivate stable identities when an object returns.
- Added analysis-run cancellation, terminal-state reconciliation against Temporal, resume-as-new-history, manual/scheduled/resume trigger evidence, priorities, and organization-wide run inventory.
- Replaced monolithic source profiling with a Temporal table-task DAG. Table profiles retry independently, remain idempotent, and execute in batches bounded by each source's configured concurrency.
- Added source disable/enable administration and fail-closed enforcement across scans, direct queries, agent analyses, and governed tool executions.
- Added HA-safe scan-policy scheduling with database row locks, organization quotas, one active scan per source, priority ordering, maintenance windows, backpressure deferral, and a dedicated scheduler service.
- Added governed semantic model and metric versions, immutable physical mappings, draft/review/publish/supersede/reject states, maker-checker separation, and clone-based rollback.
- Added governed reusable tool versions with AST-validated SQL templates, exact parameter contracts, AST literal binding, role intersection, semantic pinning, approval, version supersession, HMAC parameter fingerprints, dependency capture, and mandatory query-gateway execution.
- Added value-free query-memory evidence and owner feedback. Raw questions and comments are not persisted; keyed hashes are used, and negative feedback suppresses evidence.
- Added bounded metadata-only relationship candidate generation, durable negative knowledge, inspectable evidence, maker-checker decisions, and no automatic promotion to source truth.
- Added physical-table impact analysis across semantic metrics, governed tools, and approved inferred relationships.
- Added fleet summaries, organization run inventory, filtered audit evidence, exponential outbox retry state, dead-letter visibility, and an authorized requeue path.

### Verification evidence — enterprise iteration

- Static checks: Ruff clean; strict mypy clean across 34 source files; 39 automated tests passing; Alembic reports no model drift at revision `f16bd8c935a4`.
- All migrations from `3df18be7a420` through the new schema-drift, semantic, tool, scheduling, intelligence, and outbox revisions applied transactionally to the running PostgreSQL service.
- End-to-end verifier run `644d943d-39e4-47b0-a134-fb73e79cf8da` passed the healthy Atlas portal, organization/LOB/project/source creation, credential-safe portal inventory, connection validation, the table-task Temporal workflow, sensitive masking, mutation denial, feedback and memory, semantic maker-checker publication, governed tool publication/execution/deprecation, impact analysis, relationship review, scheduling, audit/fleet evidence, source disablement, and graph reconciliation.
- Manual analysis run `3f189221-c96c-402f-96a1-82953accc62a` and scheduler-admitted run `7a002e55-be12-4f07-b42d-5c0897d42b50` both completed with 4 tables, 22 columns, 7 constraints, 4 table profiles, and 22 column profiles.
- Governed tool execution `fd8e3e89-5ddd-4e79-895a-9a3d6afef36a` completed through the same AST, catalog, EXPLAIN, cost, timeout, masking, and evidence gateway used by agent-generated SQL; its approved deprecation then prevented further execution with HTTP 409.

### Portal and status transparency

- Added the Atlas operational portal as a Docker service at `http://localhost:3000` with live organization selection, fleet overview, registered sources, scan actions, source enable/disable controls, run histories, pending governance reviews, audit evidence, implementation status and architecture decisions.
- Added tenant-safe list APIs for organizations, LOBs, projects and credential-reference-free datasource summaries so the portal can navigate the hierarchy without bypassing organization controls.
- Added `14-implementation-status-matrix.md` as the single implemented/partial/pending/retest/bank-decision status source and recorded the LangGraph/ADK, hybrid execution, Temporal, Kafka, PostgreSQL, query-gateway and data-minimization decisions.
- Built and started the portal container successfully. Both API and portal report healthy; the live portal proxy returned 9 organizations and the selected fixture reported 1 LOB, 2 completed runs, 36 audit events, and zero dead-letter events. Datasource summaries were verified not to expose credential references.
- Extended `scripts/verify-local.ps1` so future end-to-end runs require a healthy Atlas UI, validate its product title, traverse the portal's API proxy, and assert credential references are absent from datasource inventory responses.

### Agentic product portal iteration

- Promoted Atlas from an operational status slice to an agentic product portal. The default workspace now accepts a business question and controlled candidate SQL, executes the existing hybrid agent runtime, renders masked results, and shows the complete stage/control trace and durable run history.
- Added live workbenches for metadata/table profiles and downstream impact, semantic-model drafts and metric inspection, governed-tool catalog and parameter execution, inferred-relationship discovery and checker decisions, scan-policy scheduling, model/runtime governance, source fleet operations, maker-checker review, and audit evidence.
- Added credential-safe agent-run list/detail APIs. Raw question text and its HMAC digest are not exposed by the history contract.
- Added a live AI runtime posture API that reports the framework-neutral typed state machine, hybrid orchestration, nine deterministic gates, optional LangGraph/Google ADK adapter posture, development-route configuration, and the intentionally unconfigured production model route.
- Kept the model boundary fail closed. The UI clearly labels the SQL candidate path as a controlled development route and does not imply that an LLM is active.
- Extended the local verifier to require the agentic product title and execution-trace surface, verify the live `HYBRID/NOT_CONFIGURED` runtime posture, and confirm agent history through the portal proxy.
- End-to-end verifier run `3faf4656-dfd6-40b1-a301-1f62dc54b50d` passed with UI/API health, 4 tables, 22 columns, 7 constraints, 4/22 table/column profiles, masked `customer_name` and `email_address`, mutation and disabled-source denial, semantic and tool governance, relationship review, scheduling, audit, and current graph projection. Agent run `b0566df0-2d4f-4e17-9c72-05be6e158081` and governed query execution `e1255fdc-3bd8-4457-8223-654f61c3d295` completed.
- Static verification is clean: JavaScript syntax, Ruff, and strict mypy pass; the automated Python suite now has 41 passing tests. Interactive browser visual QA could not run because no browser surface was connected in this session; live HTTP product/proxy checks passed instead.

### R7 governed agent intelligence

- Added organization/source-scoped, value-free lexical retrieval across active technical metadata, published semantic metrics, and published governed tools. Results are bounded, ranked, reason-coded, and never include source-row values.
- Added deterministic approved-tool-first planning with confidence thresholds, role intersection, explicit tool selection, required-parameter clarification, and a safe fallback to the development SQL or approved-model boundary.
- Bound agent execution directly to published tool versions. Parameters continue through strict schema validation and AST literal rendering; only an HMAC parameter fingerprint is persisted, and execution still uses the mandatory query gateway.
- Added durable retrieval evidence, plan evidence, selected-tool reference, and trace details to agent runs. Agent history and analysis responses expose the bounded evidence but not raw questions or their keyed digests.
- Added a provider-neutral structured model gateway with explicit route registration, input/output budgets, timeout enforcement, Pydantic output validation, and non-content fingerprints. No external adapter is registered, so the live runtime remains fail closed.
- Added a durable agent-control evaluation ledger and UI. The initial eight-scenario suite verifies safe reads, mutation/multi-statement/wildcard denial, approved-tool-first planning, role binding, model-route fail closure, and prompt data minimization.
- Enhanced the AI Analyst with plan preview, ranked evidence, plan confidence/reasons, detailed execution-stage evidence, tool-first run history, and the Agents & Models screen with runnable evaluation history.
- Added migration `a7c4e2d91b60`; Alembic reports it as head with no model drift. Ruff, strict mypy, JavaScript syntax, and all 50 automated tests pass.
- Final end-to-end verifier run `4c1320e9-b272-4580-90ac-e04f6de5d357` passed the complete banking fixture. Tool-first agent run `1e634f41-071a-4596-a4c7-362cb01e0b97` selected strategy `GOVERNED_TOOL` with 9 retrieval evidence records, and evaluation run `f7c9ef2a-bbe8-42d4-add7-d90f10598e41` passed at 100%.

### R8 enterprise identity, secret boundaries, and column lineage

- Implemented production OIDC bearer-token verification with signed JWT validation, issuer and audience enforcement, algorithm allowlisting, expiration/issued-at checks, bounded clock skew, cached JWKS retrieval, unknown-key refresh, pinned-JWKS support, configurable claim paths, external-to-platform role mapping, organization claim validation, and generic authentication failures that do not disclose verification internals.
- Production configuration now requires an OIDC issuer, audience, and HTTPS JWKS source or pinned JWKS. It rejects development identity, development SQL override, weak audit keys, and the local environment credential provider.
- Replaced the local-only secret resolver with a provider-neutral, explicitly registered adapter contract supporting provider/version metadata, strict reference parsing, one deployment-approved scheme, bounded in-memory TTL caching, and invalidation for rotation. Inline credentials, unknown providers, traversal-like references, empty values, and provider mismatch fail closed; secret values are never persisted or logged.
- Added durable, value-free query column lineage. Governed SELECT executions now retain referenced columns plus output-to-source mappings, direct/derived classification, and transformation names without expressions or literal values. The same evidence is returned by query/tool/agent responses and the tenant-safe `GET /v1/query-executions/{execution_id}/lineage` API.
- Updated Atlas to release posture R8. Verified results display referenced-column counts and column lineage, while Agents & Models shows the live identity verification mode, selected credential provider/adapter status, and honest enterprise-security activation state.
- Added migration `c8e5f3a20d71`. Ruff, strict mypy across 39 source files, JavaScript syntax, Alembic single-head/no-drift validation, and all 58 automated tests pass.
- Final end-to-end verifier organization `f18ab71f-6ac5-4532-84f3-d44a9922a8c6` passed UI/API health, discovery, profiling, masking, mutation and disabled-source denial, durable column lineage through the UI proxy, feedback/memory, semantic and tool maker-checker governance, tool-first agent execution, evaluation, impact, relationships, scheduling, audit, and graph reconciliation.
- Query execution `60c60d23-6ca9-4e24-bc3c-ca14bec69557` retained five referenced columns and four output lineage records while masking `customer_name` and `email_address`. Tool-first agent `396f7509-c3c3-43e9-a270-a5b29d009d88` used nine retrieval evidence records; evaluation `61373ac5-fa66-4193-8f93-61c4856fc8f7` passed at 100%.

### R8 remaining production gates

- The bank must provide its issuer/claim contract, external group mappings, centralized ABAC decision point, break-glass process, and workload-identity standard before production identity can be activated.
- The bank-selected Vault, CyberArk, AWS Secrets Manager, Azure Key Vault, or GCP Secret Manager adapter must be registered at the deployment composition root and certified for workload identity, rotation, outage behavior, access review, and no-secret telemetry.
- View definition, stored procedure, ETL/OpenLineage, and warehouse-history lineage adapters remain; current column lineage is the governed SELECT execution slice.

## 2026-08-26

### R9 knowledge graph and governed model-route workbenches

- Audited implemented APIs against Atlas and added `15-ui-capability-coverage.md` so product UI, API-only administration, and deployment-only security controls are tracked separately.
- Added a bounded, tenant-safe authoritative knowledge-graph API. It returns named table nodes, declared foreign-key edges, enriched source/target column suggestions, confidence, review status, and value-free evidence with total counts and truncation state.
- Rebuilt the relationship screen as a knowledge-graph workbench with topology cards, declared-versus-suggested visual treatment, edge filters, source/target names, confidence, evidence boundaries, discovery and independent approve/reject actions.
- Added immutable organization-scoped model-route versions covering route key, provider type, model/deployment alias, endpoint alias, credential-reference presence, residency, retention, capabilities, token ceilings and timeout. Credential references are excluded from every read contract.
- Added model-route maker-checker submission and approval. Approval supersedes an older approved version but cannot select a runtime route, enable generation, resolve credentials, or register a private adapter. Atlas exposes `APPROVED_NOT_SELECTED`, `GENERATION_DISABLED`, and `ADAPTER_REGISTRATION_REQUIRED` rather than implying readiness.
- Added model-route registry and authoring UI, effective activation chain, source connection testing, deterministic SQL validation, analysis-run cancel/resume controls and agent helpful/incorrect feedback.
- Added migration `d9f6a4b31e82`. Ruff, strict mypy across 40 source files, JavaScript syntax, Alembic single-head/no-drift validation, and all 61 automated tests pass.
- Final end-to-end verifier organization `51afcf7e-24b7-4295-95f8-d0594ce18108` passed the complete local banking fixture. The graph exposed four nodes, three declared FK edges and two suggestions. Model route `8937c683-5210-4ac1-af22-78215a3cac76` passed maker-checker approval and remained safely `APPROVED_NOT_SELECTED` with no adapter.
- Agent run `d10f69ff-58a7-40b3-9213-6c8e86d7e998` and query execution `26c26be8-4b05-42c4-867c-2764c3e5c8eb` completed with five referenced columns, four lineage outputs and masking of `customer_name` and `email_address`. Tool-first agent `dd0f374b-00e9-4f50-a338-dc0c62350a74` used nine retrieval evidence records; evaluation `2d938ff3-40dd-40ee-8400-7ee708da4403` passed at 100%.

### R10 product completion and enterprise workflow rebuild

- Replaced the dark, flat 12-screen proof-of-concept presentation with a restrained banking product system: role-grouped navigation, operating brief, attention queue, consistent workbenches, contextual detail, accessible dialogs, responsive layouts, explicit control states and a light information-dense visual language.
- Closed the former UI-only gaps end to end: guided organization/LOB/project/source onboarding with immediate connection verification; semantic metric composition and model clone/rollback; governed tool authoring/versioning/publish/execute/deprecation; query-memory inspection; event-delivery exception inventory and requeue; filtered audit and run evidence.
- Replaced per-project portal fleet traversal with bounded tenant-level project and datasource inventory APIs. This removes the hierarchy N+1 request pattern for organizations with many LOBs, projects and database registrations.
- Added a real data-driven canvas topology map with table nodes and distinct declared/suggested edges while retaining the evidence table and independent relationship decision workflow.
- Added a tenant-scoped event inventory that deliberately excludes event payloads. Operators can inspect status, aggregate, attempts and bounded errors and can requeue only dead-letter events through the audited control.
- JavaScript syntax, Ruff and strict mypy are clean; all 63 automated tests pass. The final Docker verifier passed UI/API health, tenant inventory, four tables, 22 columns, seven constraints, masking, column lineage, semantic/tool/model governance, tool-first agents, 100% control evaluation, query memory, graph suggestions, scheduling, audit/outbox evidence and projection reconciliation.
- Final verifier organization `20ba44a2-471e-4895-9c14-614357efdc17` produced analyst run `e4a2cf55-e72b-4cae-b77c-fabf4cc8e275`, query execution `cefc0d6b-8c06-4f2e-8805-c25c7d75ba4f`, tool-first run `c42a69cb-c10b-4d4d-8217-2603f025c90d`, evaluation `bda2c3f0-9018-4c98-8f5c-d2cf03854189`, three declared graph edges and two governed suggestions.

### R11 governed model providers and grounded generation

- Implemented concrete OpenAI Responses and Google Gemini GenerateContent adapters behind the existing provider-neutral model gateway. Both require strict structured JSON output, enforce organization-approved route/model/capability selection, use bounded token and timeout budgets, and retry only transient provider failures.
- Added credential resolution for `env://OPENAI_API_KEY` and `env://GEMINI_API_KEY` in local development without persisting or returning secret material. Provider telemetry retains only route/model/endpoint aliases and non-content fingerprints.
- Replaced the real credentials that had been placed in `.env.example` with placeholders and created the ignored local `.env`. Both exposed credentials must be rotated before any model traffic is enabled.
- Grounded model requests with bounded, organization/source-scoped retrieval evidence plus active qualified tables, column types/classifications and constraints. Raw source values are never added to model context, and generated SQL still passes every deterministic authorization, AST, catalog, cost, timeout, masking and lineage gate.
- Updated Atlas runtime posture and model-route authoring for `OPENAI` and `GOOGLE_GEMINI`; approval still does not activate generation. Local generation remains deliberately disabled until rotated credentials and an approved route are selected.
- JavaScript syntax, Ruff, strict mypy across 40 source files, Alembic no-drift validation, and all 66 automated tests pass. Adapter tests mock provider traffic; no compromised key was used for a live request.
- Final Docker verifier organization `2e2103ee-354f-473c-ac3d-312040f79bc7` passed the complete banking fixture. Analysis run `75350aa7-2ad7-44c4-9338-0f1f85c51f6b`, analyst run `6594561c-14fd-4cc4-957c-702b39d83eac`, query execution `ed6231f1-f359-4828-8eab-390dac34e049`, tool-first run `995ebd40-6cef-4d13-bf28-f5fbb62156f7`, and evaluation `35eaa5be-d5f0-4804-885f-c24014212c25` all completed; the live runtime advertised both model adapters while remaining `HYBRID/NOT_CONFIGURED` and fail closed.

## 2026-08-27

### R12 dbt transformation intelligence and friendly workbench

- Clarified and enforced the operating boundary: dbt compiles and executes transformations in its target warehouse; Atlas imports transformation evidence and never treats dbt artifacts as an execution bypass or a source-extraction engine.
- Added organization/project/source-scoped dbt project registrations and immutable manifest imports. The raw manifest is not retained; bounded resources, dependencies, catalog mappings, metadata, fingerprints and normalized evidence are persisted instead.
- Added support for models, sources, tests, seeds, snapshots, analyses, exposures, metrics, semantic models and saved queries with 32 MiB/25,000-resource/100,000-edge bounds and idempotent manifest fingerprints.
- Compiled SQL is hashed, parsed with the datasource dialect, stripped of comments and rewritten with every literal replaced by a placeholder. Unparseable or oversized SQL is not stored.
- Matched dbt relations deterministically to active catalog tables and added dbt resource IDs to downstream impact. Latest dbt artifacts now participate in value-safe agent retrieval and hydrate the same bounded physical table context used by model generation.
- Added the Atlas **Transformations** workbench: project registration, `manifest.json` upload, immutable history, coverage metrics, resource filters, build/test lineage, catalog status and a literal-redacted SQL evidence viewer. The screen explains what dbt, Atlas and source ingestion each own.
- Added migration `e4b7c2a91d35`. JavaScript syntax, Ruff, strict mypy across 42 source files, Alembic no-drift validation and all 71 automated tests pass.
- Final Docker verifier organization `2433a2b4-74c5-49ba-ab36-22d09f48d2ab` passed the complete banking fixture. dbt project `de45be7a-c14e-4d91-b66f-b429cceef0c6` imported artifact `5bb70996-8db7-4ab0-97eb-41a67a4a63d1` with three resources, two edges and one catalog match; the raw marker literal was absent from every response and `DBT_MODEL` appeared in governed agent retrieval.

### R13 governed business-semantic inference and cross-domain workbench

- Added a metadata-only semantic inference pipeline that goes beyond technical collection. Deterministic rules and an optional organization-approved `CLASSIFICATION` model route create bounded proposals for business names/descriptions, domains, entities, table roles, row grain, synonyms, analytical questions, tags and safe tool blueprints.
- Enforced the AI trust boundary in code: source rows are never loaded into inference context; identifiers are treated as untrusted data; strict Pydantic output contracts reject extra fields; returned table IDs and tool columns are allowlisted; sensitive classifications cannot enter tool blueprints; invalid or unavailable model batches fall back to deterministic proposals; and no LLM-authored SQL is accepted.
- Integrated proposals with the common governance queue and maker-checker separation. Approval creates or updates authoritative organization domains, domain-owned entities and versioned table annotations; rejection is durable negative evidence. All inference, decisions and promotions emit audit/outbox evidence.
- Added a tenant-scoped business map with domain, entity and table nodes plus cross-domain edges derived only from approved annotations and authoritative foreign keys. Approved annotations now participate in governed agent retrieval as `BUSINESS_ENTITY` evidence.
- Added safe tool promotion. Only an approved proposal can be promoted; Atlas rechecks current active/non-sensitive columns, renders SQL deterministically with the source dialect, passes it through the existing SQL/catalog validator, and creates a `DRAFT` tool that still requires the standard publication review.
- Added the Atlas **Business meaning** workbench with source selection, inference execution, rule/model engine posture, proposal queue, common review actions, approved annotation inventory, domain/entity/table map, cross-domain relationships and draft-tool promotion. The Data Catalog table detail now surfaces approved business meaning, and model route configuration exposes `CLASSIFICATION` as a separate metadata-inference capability.
- Added migration `f2c8d5a93e71`. JavaScript syntax, Ruff, strict mypy across 44 source files, Alembic single-head/no-drift validation and all 76 automated tests pass.
- Final Docker verifier organization `1da6e47a-b624-456c-adea-46ce93fc2750` passed the complete banking fixture. Inference run `f41aef7f-12bc-4e36-8027-77411f3cb88f` produced four `RULES_ONLY` proposals; independent approval created two business domains and two entities with one cross-domain FK edge; approved annotation `ec3c3977-50cf-49b2-aba6-edc91cc4d06e` appeared in agent retrieval; and proposal promotion created draft tool version `7d51abf4-7b82-4152-8a6d-1acc45bfe54e` without model-authored SQL.
- The Docker-hosted UI and proxy passed live health/content/API assertions at `http://localhost:3000`. An interactive browser was not connected in this session, so visual click-through and accessibility remain explicitly listed for retest rather than reported as passed.

### R14 bank-safe Graph Explorer V2

- Replaced the fixed 40-node topology slice with a server-backed exploration contract. Authorized users can search active table, schema and catalog names, select a focus table, and expand deterministic `REFERENCES`, `REFERENCED_BY` or bidirectional neighborhoods from one to four hops.
- Added deployment policy ceilings for traversal depth, returned nodes and returned edges. Every neighborhood reports the requested scope, returned counts and explicit truncation reasons; excessive depth/size requests fail closed instead of shifting unbounded work into PostgreSQL or the browser.
- Added deterministic frontier expansion with stable link ordering and unit coverage for direction, depth, node-budget truncation and invalid budgets. Search escapes wildcard characters and rejects whitespace-only terms.
- Upgraded Atlas with server-side search results, focus history, estate reset, hop/direction/edge filters, radial focused layouts, overview layouts, arrow direction, selected-edge highlighting, zoom controls, responsive behavior and accessible native table-node buttons.
- Added a governed node inspector that combines active columns, classifications, safe aggregate profile counts, approved business meaning, visible relationship evidence and downstream object counts. The UI explicitly states that raw customer, account and transaction values are never rendered; declared and suggested edges carry `source_values_inspected=false` evidence.
- Extended `scripts/verify-local.ps1` to require Graph Explorer V2 content and verify search, a two-hop bounded neighborhood, depth metadata, node/edge ceilings, value-free edge evidence and denial above the configured depth policy.
- JavaScript syntax, Ruff and strict mypy across 45 source files are clean; all 81 automated tests pass. No schema migration was required because this increment extends API/view contracts over existing authoritative metadata.
- Final Docker verifier organization `968a6ef1-bf20-4d57-b9bf-4fff32311d4a` passed the complete local banking fixture. Graph search returned two matches; focus expansion returned four nodes and five edges; a depth-five request was denied. Analysis run `0507c798-de9a-4184-9fc0-e383ef8680cf`, tool-first agent `2f414e2e-1a4e-4c23-bbe1-6641ce76d960`, evaluation `0849a661-5d46-463c-bd66-7fde296a4652`, dbt artifact `a7ecb745-2794-496e-a71d-d36acf91bbc8` and semantic inference run `b1d201f0-7def-4b5d-888d-96455e98dec1` all completed successfully.
- The Docker-hosted UI and API are healthy at `http://localhost:3000` and `http://localhost:8000`. The browser runtime reported no available browser connection, so interactive visual/accessibility certification remains a truthful retest item; live HTML, proxy, API, responsive CSS structure and JavaScript syntax checks passed.

### R15 deterministic prompt-risk screening and agent control suite v2

- Added a versioned `deterministic-prompt-risk-v1` classifier for direct user-prompt attacks. It detects instruction override, system-prompt extraction, credential extraction, authorization/guardrail bypass, masking/redaction bypass, privilege escalation and unbounded regulated-data extraction signals.
- Inserted a mandatory `SCREENED` state after authorization and before metadata retrieval. A blocked request creates value-free plan/trace/audit evidence and a rejected agent-run record, then returns HTTP 422 without retrieving metadata, constructing model context, choosing a tool or executing SQL.
- Kept raw prompt content out of new evidence. Only the existing keyed question HMAC plus classifier version, decision, numeric score, signal count and stable reason codes are retained or returned. Matched text fragments are deliberately excluded.
- Extended the planner with an explicit `BLOCKED` strategy that wins over candidate SQL, approved-tool and model paths. Preview uses the same classifier and returns zero retrieval records for a blocked prompt, allowing users to understand the denial before execution.
- Upgraded Atlas plan and run-history views with prompt decision, risk score, classifier version and reason codes. The AI runtime now reports `prompt_risk_classification` as an enforced deterministic control and runtime state-machine version `v2`.
- Upgraded the durable control evaluation to `governed-agent-controls-v2`, adding benign-prompt allowance plus instruction-override, credential-extraction, security-bypass, masking-bypass and privilege-escalation denials.
- JavaScript syntax, Ruff and strict mypy across 46 source files are clean; all 90 automated tests pass. No schema migration was required because prompt-risk evidence uses the existing versioned plan/trace JSON contracts.
- Final Docker verifier organization `58a70205-635f-4d80-9c2a-051149da84b9` passed the complete local fixture. Risk preview returned `BLOCKED` with no retrieval evidence; risky execution was denied before SQL; benign agent run `5819bd07-5084-4c16-a27d-5e5e32745379` recorded `SCREENED/ALLOW`; tool-first run `c96dec35-9664-4b25-8c1b-3a49606796b7` completed; evaluation `373d945b-ee45-4d69-a794-6b58f9bf2762` passed at 100%. Graph Explorer V2, dbt, governed semantics, masking, lineage, scheduling, audit/outbox and projection checks remained green.
- The direct classifier is defense in depth, not a claim of universal injection prevention. Multilingual, obfuscated and indirect injections through retrieved metadata/tool descriptions remain explicitly tracked for bank model-risk evaluation. The browser runtime still had no available connection, so interactive visual/accessibility certification remains open.

### R16 durable value-free data-quality observability

- Added source-default and table-override quality policy records for volume movement, null-rate movement, schema fingerprint change and maximum metadata scan age. Policies are tenant enforced, bounded, auditable and emitted through the transactional outbox.
- Extended immutable table profiles with the scan-time schema fingerprint. Every completed Temporal table-task workflow now invokes an idempotent quality reconciliation before committing completion evidence. Evaluation row-locks the run against concurrent replay and batch-loads historical baselines, policies, column statistics and incident state instead of issuing per-table lookup chains.
- Added a deterministic `quality-v1` evaluator. A first profile becomes `NO_BASELINE`; later profiles compare estimated/bounded sampled row count, percentage-point null-rate movement across stable column IDs and schema fingerprints. Only counts, rates, IDs and hashes are retained—never sampled source values.
- Added immutable quality observations and fingerprinted incidents with `OPEN`, `ACKNOWLEDGED` and `RESOLVED` states. Repeated failures update the same incident and occurrence count, regressions reopen it, and healthy comparisons automatically resolve recovered controls. Manual lifecycle changes require a rationale and emit audit/outbox evidence.
- Added paginated policies, observations and incidents; replay-safe completed-run evaluation; tenant-safe quality summaries using the latest observation per table; exact active/critical incident counts; average scores; and metadata scan-age posture. Source-row freshness deliberately returns `NOT_CONFIGURED` until a connector receives an approved watermark contract.
- Added the Atlas **Data quality** workspace with source selection, coverage/score/incident/scan-age metrics, policy editing, explicit measurement boundaries, incident filters/evidence, friendly acknowledge/resolve dialog and immutable observation history. The navigation badge reports active incident count.
- Added migration `1b7e4c9a62d0`, six deterministic evaluator/API contract tests and quality assertions to the full local verifier. Ruff is clean, strict mypy passes across 49 source files, and all 96 automated tests pass.
- Final Docker verifier organization `4a134037-926d-4aee-907c-f89c17a2cc8c` passed the complete local banking fixture. Policy `5d9647c1-82f8-40e4-aed3-5cf5f504ce2d` governed four tables; two completed scans automatically produced eight observations with average score 100, `CURRENT` metadata scan posture and explicitly `NOT_CONFIGURED` source freshness. The existing prompt-risk, agent, dbt, semantic, graph, lineage, masking, audit/outbox and projection checks remained green.
- Docker UI/API are healthy at `http://localhost:3000` and `http://localhost:8000`. No interactive browser instance was available, so visual click-through and accessibility certification remain open; live HTML/proxy/API assertions and responsive structure were verified.

## 2026-08-28

### R23 governed glossary and asset documentation

- Added organization-scoped glossary terms with stable keys, immutable versions, owner principals, and draft/review/approved/rejected/superseded maker-checker lifecycle.
- Added versioned table aliases, README content, ownership, and approved glossary-term links, all enforced by tenant and role policy with audit and transactional outbox evidence.
- Added the glossary authoring workbench and asset Intelligence controls for documentation, review submission, term linking, and unlinking; business-meaning pagination now reads every bounded page instead of silently stopping at the API page limit.
- Added migration `ab31d7e4c920`, contract/schema tests, and the implemented event contracts. Ruff, strict mypy across all source files, the complete Python suite, JavaScript syntax, and Alembic head checks pass.
- Live public-API verification on the rebuilt Docker stack created, independently approved, read back, and linked a glossary term and asset-documentation version against a discovered SQL Server table. Interactive browser visual and accessibility certification remains open because no in-app browser session was available.

### R18 enterprise metadata ingestion and connector certification

- Added canonical metadata envelope `1.0` for push and stream-shaped producers. Nested catalogs, schemas, tables, columns and constraints are strictly validated for names, duplicate identities, ordinals, local/foreign-key consistency, type/size limits and a 100-catalog/50,000-table/250,000-column synchronous safety boundary.
- Enforced a value-free attribute contract: scalar bounded attributes only, with sample/row-value/password/secret/token/credential keys rejected before persistence. The payload itself is not stored; jobs retain a canonical SHA-256 fingerprint, counts and operational evidence.
- Added datasource-scoped idempotency and row locking. An identical key/payload retry returns the original job, conflicting key reuse returns HTTP 409, unrelated sources remain independent, and the inventory/job/event changes commit atomically.
- Implemented explicit snapshot behavior. `INCREMENTAL` upserts present objects without retiring omissions. `FULL` treats the envelope as authoritative and soft-deprecates missing metadata through the existing tombstone path.
- Reused the authoritative discovery persistence path for pull and push so stable IDs, fingerprints, classifications, constraints, drift evidence, audit, outbox and Neo4j projection remain consistent. Push jobs also create normal completed analysis-run evidence.
- Added immutable connector certification records and suite `connector-contract-v1`, scoring implementation registration, opaque secret reference, prior connection evidence, hierarchy capabilities, active inventory and canonical push support. Added an honest registry matrix for PostgreSQL (`IMPLEMENTED/BETA`) and Oracle, SQL Server, Snowflake, Databricks, Teradata and Db2 (`PLANNED`).
- Added the Atlas **Enterprise ingestion control plane** with fleet inventory, capability/maturity/transport matrix, source certification and check evidence, a guided canonical JSON delivery form, incremental safe default, full-snapshot confirmation, privacy boundary, and ingestion change/history drill-down. Corrected datasource onboarding so its PostgreSQL selector maps to backend connector type `postgres`.
- Added migration `7d2f9a41c6e3`, five ingestion/certification contract tests, and full-verifier assertions for matrix honesty, 100-point certification, identical replay, conflicting-key denial and no incremental retirement. JavaScript syntax and Ruff are clean, strict mypy passes across 51 source files, all 101 Python tests pass, and Alembic reports no model drift.
- Final Docker verifier organization `4a178080-d084-4a01-b93f-17d6c6e27ee7` passed. Source `1f1b1050-3f6e-40ac-ae31-e7980bc3b2ec` earned certification `85e96308-f166-49ae-a3c1-95e8b58d1c96` at 100. Ingestion `35013701-7b1a-4659-8bf6-ca87ab2b4875` replayed to the same ID, conflicting reuse was denied, and all existing agent, prompt-risk, dbt, business semantics, tools, lineage, masking, graph, quality, scheduling, audit/outbox and projection checks remained green.
- The rebuilt API and Atlas UI are healthy at `http://localhost:8000` and `http://localhost:3000`. The in-app browser runtime exposed no browser surface, so interactive visual/accessibility certification remains open; live HTML, proxy, JavaScript, API and complete Docker workflow checks passed.

### R20 OpenAI structured-output schema fix and model-route activation walkthrough

- Fixed a real bug in `src/aida/model_gateway.py`'s `OpenAIResponsesProvider`: it forwarded pydantic's `model_json_schema()` output to OpenAI's Responses API unmodified, but OpenAI's strict structured-output mode requires every object node to set `additionalProperties: false` and list every property (including ones with defaults) in `required`. Live end-to-end testing against the user's actual configured OpenAI key reproduced the exact failure — HTTP 400, `"'additionalProperties' is required to be supplied and to be false"` — confirming this was a genuine defect, not a configuration problem. Added `_openai_strict_schema()`, a recursive schema rewriter that walks `properties`, `items`, `$defs`/`definitions`, and `anyOf`/`oneOf`/`allOf`/`prefixItems` branches, so it correctly handles both the flat `SqlGenerationOutput` schema and the nested `SemanticEnrichmentBatchOutput` schema (which references a child model through `$defs`) used elsewhere in the codebase. Verified against both schemas directly (structural assertions that every object node is compliant) and against the full test suite.
- Fixed a second, independently-discovered bug: the Atlas UI's "Validate SQL controls" button (`ui/app.js` `validateSql()`) hardcoded `dialect: "postgres"` on every call to `/v1/query/validate`, so validating candidate SQL against a non-Postgres datasource (like the new SQL Server connector) silently checked it against the wrong dialect's grammar. It now looks up the selected datasource's actual `dialect` field.
- Walked the user through the full local model-route activation path live against their running stack, which doubled as an end-to-end audit of that governance flow: real `OPENAI_API_KEY`/`GEMINI_API_KEY` in `.env`, `AIDA_MODEL_GENERATION_ENABLED` flipped from its safe-by-default `false`, a `ModelRouteConfiguration` created with `route_key=openai-bank-sql`/`provider_type=OPENAI`/`capabilities=[SQL_GENERATION, EXPLANATION]`, submitted and approved under proper maker-checker separation (a different principal decided the review than the one who submitted it, matching the platform's own enforcement), and confirmed `activation_status: READY` for the correct organization — which took real diagnostic work, since an org-scoped mismatch (the route was initially approved in a different organization than the one owning the SQL Server datasource under test) and an unpaginated organization list (more than 100 test organizations had accumulated locally) both had to be found and corrected before the right organization was identified.
- After the schema fix and route activation, confirmed live in the user's own environment that `Run governed analysis` with no candidate SQL supplied now reaches the `MODEL_GATEWAY` generation strategy and calls OpenAI successfully end to end, rather than requiring the `DEVELOPMENT_OVERRIDE` (hand-typed SQL) path.
- Fixed a third bug found during the same live walkthrough: the "Candidate SQL" textarea in `ui/index.html` held its example text (`SELECT customer_id, customer_name, email_address, state FROM public.customers`) as literal starting *value*, not a placeholder hint. Leaving the field visually untouched meant that text was still submitted as `candidate_sql`, silently routing the request down the `DEVELOPMENT_OVERRIDE` path instead of the intended `MODEL_GATEWAY` (LLM) path — and since `public.customers` doesn't exist on a SQL Server source, governance correctly rejected it with `UNKNOWN_OR_UNAUTHORIZED_TABLES`, which looked like a model-generation failure but wasn't one. Converted the field to a real HTML `placeholder` attribute so example text is never part of the submitted value; the field now starts genuinely empty.
- Noted but not yet fixed: `_post_with_retry` in `model_gateway.py` discards the model provider's actual error response body on failure, surfacing only the HTTP status code (`"model provider request failed with HTTP {status}"`). This made the OpenAI 400 in this session much harder to diagnose than necessary — the real cause only surfaced by reproducing the exact request directly against OpenAI's API outside the platform. Worth including the provider's error `message`/`code` (not the full body, to avoid ever logging potentially sensitive echoed input) in the raised exception in a follow-up pass.
- All three fixes confirmed live in the user's own environment after the final rebuild: with the "Candidate SQL" field left genuinely empty, `Run governed analysis` reached the `MODEL_GATEWAY` strategy at 45% plan confidence, generated SQL correctly grounded in the real SQL Server catalog (joining `retail.account`, `retail.customer`, and `risk.customer_risk_snapshot` — no hallucinated tables), passed prompt-safety and governance validation, executed successfully (2 rows, 38ms), and automatically masked the sensitive output columns (`customer_name`, `email_address`). This is the first fully model-generated, live, end-to-end governed analysis run against the SQL Server connector in this project's history.

### R19 Microsoft SQL Server connector

- Added `SqlServerConnector` (`src/aida/connectors/sqlserver.py`) implementing the same `Connector` contract PostgreSQL uses: `test_connection`, `discover`, `explain_read_query`, `execute_read_query`, `profile_table`. Uses `python-tds`, a pure-Python TDS driver, specifically so the connector needs no system ODBC driver install — matching the project's zero-extra-system-dependency posture for connectors. All synchronous driver calls are wrapped in `asyncio.to_thread` so the connector's public interface stays fully async like every other connector.
- Connection references follow the existing "resolved secret is a driver-ready DSN string" convention, using a `mssql://user:password@host:port/database` URL form parsed and validated by `_parse_dsn`; malformed or incomplete references are rejected before any connection attempt.
- Discovery uses ANSI `INFORMATION_SCHEMA` views (`COLUMNS`, `TABLES`, `TABLE_CONSTRAINTS`, `KEY_COLUMN_USAGE`, `REFERENTIAL_CONSTRAINTS`) for portability, with `sys.partitions`/`sys.tables`/`sys.schemas` used only for approximate row-count statistics where no ANSI equivalent exists. Primary key, unique and foreign key constraints are grouped and ordinal-ordered into the same catalog/schema/table/column/constraint shape the governed catalog already expects.
- Query cost estimation uses `SET SHOWPLAN_XML ON` (SQL Server's no-execution plan mechanism) and parses the returned plan with `defusedxml` (not stdlib `ElementTree`, to close the XXE finding Ruff's `S314` check raises) into the same `{"Plan": {"Total Cost": ...}}` shape the connector-agnostic query gateway already reads, so `src/aida/query_gateway.py` needed no changes.
- Registered the connector in `ConnectorRegistry` as `sqlserver` / dialect `tsql` / maturity `BETA`, removed it from the `declare_planned` placeholder list, and wired its capabilities into `default_capabilities()` in `src/aida/ingestion.py` alongside PostgreSQL's.
- Added a Docker Compose sample SQL Server source (`sample-mssql-source`, image `mcr.microsoft.com/mssql/server:2022-latest`, port `14330`) with an init sidecar (`sample-mssql-source-init`) that creates the database, a read-only `source` login, and seed data via `infra/sample-mssql-source/init.sql` — a T-SQL equivalent of the existing PostgreSQL sample source's `retail`/`risk` schemas and rows. Added the matching `AIDA_SAMPLE_MSSQL_SOURCE_DSN` to `.env.example` and `compose.yaml`'s shared environment block.
- Added SQL Server connector options to the Atlas UI's data source onboarding form (`ui/index.html`): "Microsoft SQL Server" in the connector selector and "T-SQL (SQL Server)" in the dialect selector, alongside the existing PostgreSQL/Postgres options.
- Added 17 unit tests (`tests/test_connectors_sqlserver.py`) covering DSN parsing (valid, missing-port-defaults-to-1433, and five invalid-reference cases), SHOWPLAN_XML extraction (valid plan, malformed XML, missing statement node, missing cost attribute), identifier quoting/escaping, catalog assembly (column ordering and nullability, primary-key grouping, foreign-key ordinal ordering), the connector's declared capabilities, and registry registration/maturity.
- Verified in this session: `ruff check .` is clean across the full repository; `mypy src` (strict mode) passes with no issues on the three touched/new files (`sqlserver.py`, `registry.py`, `ingestion.py`) — a full-repository strict mypy run could not complete within this sandbox's per-command time limit, so only the touched files were directly type-checked, though the full test suite passing is strong indirect evidence nothing else broke; the full `pytest` suite passes at 118/118 (101 pre-existing plus the 17 new SQL Server tests), confirming no regression to any existing connector, gateway, or ingestion behavior.
- Corrected `compose.yaml` after the user's first `docker compose up --build` attempt failed to resolve `mcr.microsoft.com/mssql-tools18:latest` (no such published image exists; `mssql-tools18` is an apt package, not a standalone container image). `sample-mssql-source-init` now reuses the same `mcr.microsoft.com/mssql/server:2022-latest` image as the server itself, which bundles `sqlcmd` at a path that varies by platform/architecture (`/opt/mssql-tools18/bin/sqlcmd` on some builds, `/opt/mssql-tools/bin/sqlcmd` on others); both the init script and the server healthcheck now probe for whichever path exists rather than assuming one.
- Diagnosed a `migrate` service failure the user hit on their second `docker compose up` attempt by reproducing the full 19-revision Alembic chain (including `9e4c7a12b5f8`, an unrelated pre-existing chunked-ingestion migration) against a real, throwaway local Postgres 17 instance — it applied cleanly both fresh and as a idempotent no-op replay, which isolated the cause to the user's Postgres volume having been reused (not recreated) from the earlier failed attempt rather than any migration defect. A full `docker compose down -v` followed by `docker compose up --build -d` resolved it: all 17 containers came up healthy, `migrate` exited 0, and `sample-mssql-source-init` exited 0.
- Live-verified the full connector path end to end against the user's actual running stack via direct API calls (`POST /v1/organizations` through `POST /v1/datasources/{id}/analysis-runs`): registered a `sqlserver`/`tsql` datasource with credential reference `env://AIDA_SAMPLE_MSSQL_SOURCE_DSN`, `POST /datasources/{id}/test` returned `CONNECTION_VERIFIED`, and a `FULL` analysis run reached `COMPLETED` and discovered all four seeded tables (`retail.account`, `retail.customer`, `retail.transaction_fact`, `risk.customer_risk_snapshot`). This closes the live-verification gap noted below — the connector is now proven against a real SQL Server instance, not just unit-tested.

### R20 resumable large-estate metadata ingestion

- Added persisted datasource-scoped batch manifests and checksum-addressed chunks with independent batch keys, chunk numbers and chunk keys. Exact retries return the original record; conflicting reuse returns HTTP 409. Tenant/role enforcement, audit attribution, analysis runs and transactional outbox evidence cover creation, receipt, submission and completion.
- Added `MetadataBatchIngestionWorkflow` and a heartbeat-enabled Temporal activity with bounded retry/backoff. Exact `1..expected_chunks` finalization is mandatory, processed chunks resume idempotently after failure, contract failures are non-retryable, and a retry creates a replacement analysis run linked to its predecessor.
- Reused the authoritative fingerprint/tombstone persistence path while accumulating stable identities across chunks. A second metadata-only pass resolves foreign keys whose target arrived later. `FULL` omission reconciliation runs only after every chunk succeeds, so partial delivery cannot retire metadata; `INCREMENTAL` never retires omissions.
- Added configurable batch ceilings of 1,000 chunks, 1,000,000 tables and 5,000,000 columns while retaining per-chunk envelope/value-free validation. Successful completion retains only checksums, counts, statuses and timestamps and physically clears chunk JSON with SQL `NULL`; failed work retains its bounded payload for an authorized retry.
- Added the Atlas durable-batch workbench to **Source fleet**: safe manifest creation, numbered JSON upload, checksum replay evidence, open-batch selection, received/processed progress, guarded full finalization, Temporal polling and failure/completion evidence. Incremental is the default in both synchronous and batch forms.
- Added migration `9e4c7a12b5f8`. The first real PostgreSQL migration run exposed colliding auto-generated composite-unique names; the transaction rolled back and explicit constraint names fixed it. The first real worker run then exposed pre-flush column identity counting and JSON-null cleanup semantics; both were fixed and reverified against PostgreSQL.
- Ruff is clean, strict mypy passes across all 54 source files, JavaScript syntax is valid, and all 121 Python tests pass. A dedicated API/Temporal run `98e95dd3-e323-48c1-acd5-603e4571d604` proved cross-chunk FK resolution, exact 1 catalog/2 schemas/2 tables/4 columns/3 constraints scope, 12 creations, two processed chunks and two physically SQL-NULL payloads.
- The expanded complete Docker verifier passed for organization `449cf64e-116a-41bf-916b-a37a7c68db93`. Batch `cf811ccb-0cea-4fc1-82b2-249620f9f706` and analysis run `7dd5742f-08db-409c-83b3-62ad75d2551f` completed with identical manifest/chunk replay and conflicting chunk denial. The existing four-table/two-scan quality score remained 100 and all agent, prompt-risk, dbt, business semantics, tool, graph, scheduling, lineage, masking, audit/outbox and projection checks remained green.

### R21 live Microsoft SQL Server fixture certification

- Started the real SQL Server 2022 Docker fixture and found that its init sidecar returned exit zero despite three SQL errors: the proposed read-only password failed SQL Server complexity policy, login/user creation consequently failed, and no usable source identity existed. Replaced it with a compliant fixture-only credential and added `sqlcmd -b` so any future T-SQL error fails the container instead of reporting false success.
- The first governed query exposed the second least-privilege gap: `db_datareader` can execute SELECT but cannot request a no-execution plan. The init contract now grants database-scoped `SHOWPLAN` to the source principal, which is the required permission for the connector's cost gate and does not grant data mutation.
- Registered the live `sqlserver`/`tsql` datasource through the public API using only `env://AIDA_SAMPLE_MSSQL_SOURCE_DSN`. Connection verification passed; Temporal discovery/profile run `3057bced-568b-476d-bea0-e3e010d2da7d` completed with 4 tables, 22 columns, 7 constraints and all 4 tables/22 columns profiled; deterministic certification scored 100.
- Governed query execution `b83f408d-ea64-48ea-a846-4c5a220cd307` captured SQL Server SHOWPLAN cost `0.0032844`, returned two bounded rows through `python-tds`, recorded `sqlserver-spid:76`, and masked both `customer_name` and `email_address` through the common policy gateway.
- Extended the permanent full Docker verifier to require the implemented SQL Server matrix entry, real connectivity, exact discovery/profile counts, 100-point certification, SHOWPLAN-backed query completion and PII masking. This closes the prior R19 local-live gap; vendor-version, scale, cancellation, recovery, TLS/private networking and bank delegated-identity certification remain release gates.
- Final combined verifier organization `e9706966-1ce9-46c1-8fbd-afe15b75f6e7` passed. SQL Server source `5fb19209-a140-4e48-ba7b-763a90caf579`, run `8ab167a7-14e8-4499-a4ca-c612e61eabef`, certification `d6e51bd0-8c7d-4c55-bb95-81a807d0b291` and query `382b8c8a-1eb6-433b-b22f-610dc2b1b6a8` all completed; durable batch `3e2f3ac7-ff2f-4c46-bcb8-032887f8ddbb` also completed in the same run, and every existing platform control stayed green.
- After moving cumulative table/column admission to chunk-upload time and setting the Atlas proxy boundary to 40 MiB, the final rebuilt-stack verifier also passed for organization `b61022b6-d0ce-48ab-aa6a-140c92726a92`. SQL Server run `1eec8e88-4fa9-4047-a58b-989dc147f06e`, query `16963d2b-c554-45dd-94fb-32cbd6b06ae0` and durable batch `628ce398-d766-402b-9937-5baa46eab769` completed with all 68 audit events and prior controls green.

### R17 persona-based navigation, global command search, and large-estate table virtualization

- Added a client-side workspace-persona switcher (Analyst, Steward, Platform operator, Auditor, or All capabilities) in the sidebar that filters the 15 primary navigation destinations to the subset relevant to each role; the choice persists per browser in local storage. Home stays visible under every persona so a filtered user is never stranded, matching the persona-based navigation requirement in `16-market-comparison-and-product-strategy.md` Phase C and the remaining item recorded in `15-ui-capability-coverage.md`.
- Added a global command palette (topbar search control, or Ctrl/Cmd+K from anywhere) that indexes every navigation destination plus the currently loaded tables, sources, governed tools, semantic model versions, and dbt projects, with arrow-key navigation, Enter-to-jump, and direct click-through to the matching record.
- Replaced the flat, fully-rendered table helper for large result sets with a windowed virtualization layer (`renderTable` / `mountVirtualTable` / `paintVirtualTable`): lists at or below 150 rows render exactly as before with no behavior change; lists above that threshold mount only the visible row window plus overscan inside a scroll-synced viewport, leaving row markup, existing click delegation, and existing CSS untouched. Applied to all 19 existing table render call sites (catalog, audit, sources, governance, quality, dbt, operations, agents, model routes, business meaning, semantics, relationships) and to the governed-analysis and tool-execution result grids, which previously rendered every returned row — up to the analyst's 100,000-row maximum — directly into the DOM.
- This is a UI-only increment: no API, schema, or migration changes. `node --check app.js` confirms the script still parses cleanly; the 19 table-render call-site replacements and every markup insertion were applied by an idempotent, assertion-guarded patch script and diffed against the prior files before being kept, rather than hand-edited.
- Not yet done, and explicitly still open rather than claimed complete: the Docker stack was not exercised end-to-end in this session (no running containers or connected interactive browser were available here), so live click-through, binding persona navigation to the bank's approved OIDC group contract, virtualization behavior at bank-scale row counts, and accessibility validation remain open — consistent with the existing UX entries in `12-enterprise-gap-register.md` and the Phase C exit criteria in `16-market-comparison-and-product-strategy.md`.

### R22 Oracle connector

- Added `src/aida/connectors/oracle.py`: a native pull adapter using `python-oracledb` 4.0.2's genuine async API (`connect_async`, `AsyncCursor`) in thin mode, so no Oracle Client library install is required, matching the project's no-sudo local-setup constraint. Connection parameters are parsed from one canonical `oracle://user:password@host:port/service_name` resolved-secret shape, rejecting partial or ambiguous forms before any network access.
- Discovery queries `ALL_TAB_COLUMNS`/`ALL_OBJECTS` for columns, and `ALL_CONSTRAINTS`/`ALL_CONS_COLUMNS` for primary/unique/foreign keys, scoped by `OWNER` excluding the standard Oracle-supplied system schemas. Raw uppercase-folded Oracle column names are normalized to the lowercase shape the shared `aida.connectors.discovery` helpers expect before assembly, reusing the same `build_table_map_from_column_rows`/`append_grouped_key_rows`/`append_grouped_foreign_key_rows`/`assemble_catalog` helpers SQL Server uses rather than a third one-off implementation.
- Governed read execution runs on the same cursor used to look up a real Oracle session identifier (`SYS_CONTEXT('USERENV', 'SID')`), recorded as `warehouse_query_id=oracle-sid:<sid>`, matching the SQL Server (`sqlserver-spid:<spid>`) and PostgreSQL (backend pid) convention of a real backend-scoped identifier rather than a synthetic UUID.
- Bounded profiling looks up each requested column's data type from `ALL_TAB_COLUMNS` first, then builds per-column aggregate expressions through a dedicated `_profile_expressions()` helper: standard scalar types get exact null/non-null counts, an approximate distinct count, and `TO_CHAR`-based length bounds; LOB-like types (`BLOB`, `CLOB`, `NCLOB`, `LONG`, `LONG RAW`, `BFILE`, `XMLTYPE`) — which reject `COUNT(DISTINCT ...)` and `TO_CHAR(...)` outright — fall back to honest static placeholders instead of failing the batch or fabricating a value.
- `estimate_read_query()` implements a real `EXPLAIN PLAN SET STATEMENT_ID ... FOR <sql>` / `plan_table` cost lookup with cleanup, but the connector ships with `capabilities.explain=False`: per the design decision recorded in `18-oracle-bigquery-implementation-backlog.md`, a least-privilege `PLAN_TABLE` write path has not been certified against a real bank-scoped Oracle role yet, so the deterministic query-cost gate in `query_gateway.py` currently fails closed with `QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR` for Oracle rather than advertise unproven support.
- `connector_registry` already carried an `oracle`/`oracle` `IMPLEMENTED`/`BETA` registration and `tests/test_connectors_oracle.py` (14 tests: credential parsing, identifier quoting, capability declaration, discovery assembly, LOB-aware profiling expressions) from earlier scaffolding in this build; `ingestion.py`'s `default_capabilities()` is fully connector-agnostic and needed no Oracle-specific branch.
- Added a `gvenzl/oracle-free:23-slim` sample source to `compose.yaml` (`sample-oracle-source`, host port `15210`, built-in `healthcheck.sh`) with `infra/sample-oracle-source/init.sql` creating least-privilege `retail`/`risk` schema-owner users plus a read-only `source` user, mirroring the `retail.customer`/`retail.account`/`retail.transaction_fact`/`risk.customer_risk_snapshot` fixture schema (including the cross-schema foreign key from `risk.customer_risk_snapshot` to `retail.customer`, requiring an explicit `GRANT REFERENCES`) that PostgreSQL and SQL Server already use, so the same manual API walkthrough applies unchanged. `AIDA_SAMPLE_ORACLE_SOURCE_DSN` was already present in `.env.example`; added the matching `compose.yaml` environment entry and data volume. This has not yet been exercised against a real running container in this session — Docker itself is unavailable in this sandbox, so `docker compose up` and live connection/discovery/profiling verification against the fixture remain an open step for the next session with access to the user's Docker host, exactly as the first SQL Server compose attempt needed a live iteration to fix an unavailable base image.
- Ruff, strict mypy, and the full pytest suite are clean against a real editable `uv`-installed verification environment (all Oracle unit tests plus the full existing suite pass with no regressions); `compose.yaml` was validated by parsing it with `pyyaml` to confirm the new service, environment entry, and volume are structurally well-formed, which is not a substitute for a real `docker compose up`.
- Live-attempted in a follow-up session against the user's real Docker host and found a genuine `gvenzl/oracle-free:23-slim` gotcha: the image ships `FREEPDB1` pre-baked into its compressed seed data, so the container's own `CREATE PLUGGABLE DATABASE FREEPDB1` step on first boot always raises `ORA-65012: Pluggable database FREEPDB1 already exists`; the entrypoint recovers by restarting and treating the database as "already initialized," but that recovery path skips `/container-entrypoint-initdb.d/` entirely, so `init.sql` (the retail/risk schema and the `source` reader user) never ran. The database itself came up and stayed healthy for hours with no other errors — this is purely an init-script-skip, not a broken image or a broken connector. The documented recovery (`docker exec ... resetPassword`, then `docker cp init.sql` + `sqlplus ... as sysdba @init.sql` inside the container) was handed to the user but not completed in that session — the user does not have `sqlplus` on the host, and it must be run *inside* the container via `docker exec`, which was not carried out before the session moved on. **Oracle live verification (connection test, discovery, profiling against real rows) remains genuinely open.** Rather than ask the user for more manual `docker exec` steps, fixed `compose.yaml` at the root cause: replaced the `/container-entrypoint-initdb.d/00-init.sql` mount on `sample-oracle-source` (which the quirk above makes dead weight) with a dedicated `sample-oracle-source-init` sidecar — same pattern as `sample-mssql-source-init` — that polls `sqlplus -s sys/...@//sample-oracle-source:1521/FREEPDB1 as sysdba` in a retry loop (up to 60 attempts, 5s apart) until the listener genuinely accepts a query, then applies `init.sql` unconditionally, independent of the main container's flaky `healthcheck.sh` status. `init.sql`'s internal `CONNECT retail/...`/`CONNECT risk/...` statements were also fixed from `@//localhost:1521/...` to `@//sample-oracle-source:1521/...`, since they now run from the separate sidecar container rather than from inside `sample-oracle-source` itself. This is a real fix for the diagnosed root cause, but it has **not been run against a live Docker host** — no Docker access exists in this sandbox — so `docker compose up --build -d` and a full connection-test/discovery/profiling pass against real rows remain the concrete next step. The `sample-oracle-source` healthcheck itself was also observed reporting `unhealthy` for hours despite the database being genuinely up; its exact failure mode (`healthcheck.sh` invocation, PATH, or something else) was never captured, and is now decoupled from schema bootstrap but still worth fixing for accurate container status.

### R24 governed glossary and stewardship control center

- Completed the table-stewardship vertical slice with organization-scoped glossary categories, immutable term definitions and synonyms, reviewed deprecation, individual/group ownership, reusable schema/table pattern rules, and maker-checker bulk assign/link/certify/deprecate operations capped at 500 subjects.
- Added durable manual and detected conflicts with retained competing positions and independently reviewed resolution. Added value-free exact-label link inference from approved business annotations, bounded scans, proposal review, and authoritative links retaining confidence and source-annotation provenance.
- Added reviewed table certification with rationale and expiry plus six-dimension coverage for documented, owned, classified, certified, quality-monitored, and semantically mapped state. Coverage supports organization, data-source, domain, and line-of-business scope, returns a bounded unowned backlog, and persists scoped snapshots/history.
- Rebuilt Business Meaning as a responsive Stewardship Control Center with coverage, category, ownership-rule, inferred-link, conflict, bulk-operation, certification, and asset-accountability workflows. Added structural dialog naming, explicit command-palette close behavior, live regions, focus boundaries, reduced motion, and mobile layouts.
- Added migrations `7fbc5568a81f` and `9284d3ee7c0e`, then merge revision `d81e6c0f2a14` to reconcile the concurrent organization-integration-policy branch. Alembic has one head and reports no model drift.
- Repository-wide Ruff and strict mypy pass; JavaScript syntax is valid; all 188 Python tests pass. The rebuilt API/UI are healthy and the permanent `scripts/verify-stewardship.ps1` workflow passed against the public API, including two ownership assignments, an `INFERRED` provenance link, auto-detected/resolved conflict `17b0e127-360d-4f92-b1f1-0467193c621d`, coverage snapshot, and reviewed term deprecation.
- Interactive browser visual/WCAG certification remains open because the in-app browser runtime exposed no browser session. Static accessibility contracts and deployed HTML/JavaScript markers pass; bank-scale selection, scheduled expiry/escalation, dedicated leaver reassignment, broader asset types, and fuzzy inference calibration remain explicit follow-up work.

### R25 BigQuery connector

- Added `src/aida/connectors/bigquery.py`, a native pull adapter implementing the same `Connector` contract as Oracle/SQL Server (`test_connection`, `discover`, `explain_read_query`, `execute_read_query`, `profile_table`) via `google-cloud-bigquery==3.44.0`. Credentials are one canonical structured payload (`project_id`, `location`, `auth_method` of `service_account` or `workload_identity`, with `service_account_info` required only for the former) — deliberately not a fake DSN string, matching the design decision in `18-oracle-bigquery-implementation-backlog.md` Workstream C. GCP project maps to catalog, dataset to schema.
- Discovery uses region-qualified `INFORMATION_SCHEMA.COLUMNS`/`TABLES`/`TABLE_CONSTRAINTS`/`KEY_COLUMN_USAGE`, reusing the shared `aida.connectors.discovery` assembly helpers. Foreign-key metadata and `column_default` are honestly omitted rather than guessed at, since their `INFORMATION_SCHEMA` shapes could not be verified live.
- Estimation uses a `dry_run=True` job for `total_bytes_processed` (no row estimate — BigQuery dry runs don't provide one). Extracted the query-gateway cost gate into a new pure function `gate_query_estimate(estimate, settings)` in `src/aida/query_gateway.py` that branches structurally on `estimate.estimated_bytes is not None`, adding a deterministic byte-budget check (`max_bigquery_dry_run_bytes`, default 10 GB, new `config.py` setting) without changing PostgreSQL/SQL Server/Oracle's existing cost-plan gating path. Governed execution records `warehouse_query_id="bigquery-job:<job_id>"`, matching the `oracle-sid:`/`sqlserver-spid:` convention. Bounded profiling caps every query with an explicit `LIMIT`, `maximum_bytes_billed` and timeout; `REPEATED`/`RECORD`/`STRUCT`/`BYTES`/`GEOGRAPHY`/`JSON` columns fall back to static placeholders rather than issuing aggregates BigQuery rejects on those types.
- Registered `bigquery` in `connector_registry` (`BETA`, transports `PULL`+`PUSH`, dialect `bigquery`) and removed it from the `declare_planned` list.
- Added `tests/test_connectors_bigquery.py` (28 tests): credential parsing (valid plus 11 invalid/ambiguous forms), capability declaration, identifier/region quoting, discovery assembly including the FK omission, profiling-expression fallback for complex types, and `gate_query_estimate` (byte-budget allow/reject, cost-based fallback, non-finite rejection).
- Full local suite: `ruff check .` and `mypy src` (strict) are clean on every file this increment touched; `pytest` passes at 170/170 (up from a 141-test baseline, +28 new plus +1 in `tests/test_ingestion.py` distinguishing BigQuery-implemented from still-planned connectors).
- Not done, and explicitly left open: no live GCP project or credentials were available in this session, so `test_connection`, discovery, dry-run estimation, execution and profiling are unit-tested against mocked shapes only, never against a real BigQuery project — this mirrors exactly how Oracle's live-fixture verification was left open in its own increment. Certification and multi-version fixtures are unstarted.

### R26 UI accessibility and usability remediation

- Reviewed the R17 accomplishment entry and `20-modules/21-experience-shell.md` (UX-5, "accessibility audit and remediation") before changing anything, then applied targeted ARIA/keyboard/focus/contrast fixes across `ui/index.html`, `ui/app.js` and `ui/styles.css` via assertion-guarded patch scripts (each replacement asserted its expected match count before writing, and the live files were re-read after each stage to confirm), matching the idempotent-patch discipline R17 used.
- `ui/index.html`: `aria-label`s on all 12 icon-only dialog-close buttons; `aria-expanded`/`aria-controls` on the sidebar toggle; `tabindex="-1"` on `#page-title` so navigation can move focus to it; `#graph-canvas` marked `aria-hidden` (its real content lives in sibling button nodes); the operations tabs and the five asset-detail tabs converted to a real ARIA tabs pattern (`role="tab"`/`aria-selected`/`aria-controls`, `role="tabpanel"`/`aria-labelledby`); the command palette input exposes `role="combobox"`/`aria-expanded`/`aria-controls`/`aria-activedescendant` with a `role="listbox"` results container; `#alert-region` and the analysis-status badge are live regions.
- `ui/app.js`: `notify()` now switches between `role="status"` (success) and `role="alert"` (errors) instead of announcing both at the same urgency; `showView()` sets `aria-current="page"`, moves focus to `#page-title` after navigation, and respects `prefers-reduced-motion`; added `bindTabKeyboardNav()` for roving-tabindex Left/Right/Home/End navigation on both tab groups; the virtualized-table row-range indicator is a polite live region so scrolling a large table announces "Showing X–Y of Z rows" without re-announcing the whole grid; added `window.confirm()` guards on three previously-silent destructive actions (disabling a source, unlinking a glossary term, cancelling an in-flight analysis run).
- `ui/styles.css`: a `prefers-reduced-motion: reduce` block; a global `:focus-visible` outline (most custom controls had no explicit focus style before); fixed the command-palette search input, which set `outline: none` with no replacement; changed `--muted` from `#6e7890` (4.42:1 on white, just under WCAG AA's 4.5:1 for normal text, computed by hand since no browser was available) to `#5b6680` (5.74:1) in both `:root` blocks in the file.
- Verified: `node --check ui/app.js` passes before and after every patch stage; HTML tag and CSS brace counts balanced (`<dialog>` 13/13, `<div>` 309/309, `<button>` 104/104, CSS braces 649/649); confirmed via mtimes that only the three `ui/` files were touched.
- Not done, and explicitly left open: no browser, screen reader, or axe-core run was available in this session, so none of the above was interactively verified — the same constraint R17 hit. The contrast fix is the only color pair checked against the WCAG formula; the rest of the stylesheet's palette is unaudited. Interactive click-through, real keyboard-flow verification, and a full WCAG AA certification remain open, consistent with the `UX-5` tracker entry and the portal's status-matrix row.

### 2026-08-28 consolidation note

- This session ran three independent workstreams in parallel against a live, actively-changing checkout (BigQuery connector, glossary term lifecycle, UI accessibility) and closed with a repo-wide verification pass. At verification time `git status` showed 21 additional changed files from *other, unrelated concurrent work* on this same checkout (a Snowflake connector, a dbt/quality bridge, and OpenLineage ingestion changes) that this session did not author and left untouched, plus a stale `.git/index.lock`. `ruff check .` at that point showed 45 errors, all confined to those other files (none in anything this session touched); `uv run alembic heads` showed one clean head; `pytest` passed 214/214; nothing was committed. Flagging this for whoever picks up next: the working tree had more than one active author in the same window and was not committed by this session.

### R27 repo-wide lint/type cleanup and dependency fix

- Fixed the 45 `ruff check .` errors and 2 `mypy --strict` errors that were sitting in files from other concurrent work on this checkout (`migrations/versions/8a7f3c1d4b22_openlineage_run_events.py`, `migrations/versions/04003a3d6945_dbt_resource_test_and_extra_metadata.py`, src/aida/data_contracts.py [deleted 2026-08-31, AU-6, orphaned duplicate of runtime_contracts.py — does not exist any more], `src/aida/openlineage.py`, `src/aida/openlineage_api.py`, `src/aida/connectors/snowflake.py`) — none in this session's own BigQuery/glossary/UI work, which was already clean.
- `uv run ruff check --fix .` cleared unused imports and import ordering (11 auto-fixed); `uv run ruff format` on the five affected files reflowed most remaining over-100-column lines (mechanical, quote-style/wrapping only, no logic change); the 6 lines it couldn't safely reflow (long f-string `message=` assignments in the now-deleted data_contracts.py and one `raise OpenLineageError(...)` in `openlineage.py`) were manually wrapped into parenthesized implicit string concatenation, preserving the exact original message text (verified via diff).
- Added the missing `snowflake-connector-python==3.15.0` dependency to `pyproject.toml` — `snowflake.py` was importing it at runtime (`_get_connection()`) without it being declared, which both broke `mypy` (`import-not-found`) and meant a fresh `uv sync` would silently produce a connector that fails at first use. `uv sync` resolved cleanly (also correctly downgrading `cryptography` from `50.0.1` to `45.0.7` to satisfy the new dependency's constraint).
- Final state: `ruff check .` → All checks passed. `mypy src` (strict) → Success, no issues found in 70 source files. `pytest` → 214/214 passed, no regressions.

### R28 MCP tool-exposure role-binding enforcement

- Audited `src/aida/mcp_server.py` (the real JSON-RPC 2.0 MCP endpoint at `POST /mcp`, mounted in `main.py`) against the CX-1/CX-3/CX-5 exit criteria and found the docs describing module 19 as entirely `Pending` were stale — the endpoint already existed and routed `tools/call` through the full governed orchestrator/query-gateway stack — but found a real, unflagged gap: `_handle_tools_list` returned every published tool regardless of the caller's role, and `_handle_tools_call` never checked `GovernedToolVersion.allowed_roles` before invoking the orchestrator, unlike the identical role-binding check already enforced in the native REST path (`tool_api.py::execute_tool`) and the native agent planner (`agent_intelligence.py::GovernedPlanner.plan`). A caller with no eligible role could see an ineligible tool listed and, on calling it, fall through to open-ended `MODEL_GENERATION` SQL generation instead of a denial — the endpoint's own `_ERR_ACCESS_DENIED` code was declared but never raised.
- Added `_tool_role_eligible(roles, allowed_roles)`, mirroring `tool_api.py`'s check exactly. `tools/list` now filters to role-eligible tools and surfaces `allowed_roles` in `_atlas_meta`. `tools/call` on an ineligible tool now returns the identical "not found or not published" response used for a genuinely absent tool — deliberately not a distinguishable "access denied," so a caller can't enumerate tool existence by role-probing — while recording an `AuditEvent` (`mcp.tool_call.role_binding_denied`) and outbox event (`mcp.tool_invocation_denied.v1`) so operators can see the denial.
- Added `tests/test_mcp_server.py` (12 tests), taking `mcp_server.py` from zero test references to full coverage of the new decision logic, following the codebase's existing DB-free unit-test convention for this kind of routing/decision code.
- Corrected `Docs/20-modules/19-context-products-and-mcp.md` §13 from "entirely unbuilt" to an accurate implemented/partial/missing breakdown. Also flagged for future audits: `src/aida/context.py` is an unrelated 7-line correlation-ID helper, not a "context products" implementation — the name overlap with CX-2 is coincidental.
- `ruff check .` clean, `mypy src` (strict) clean across 70 files, `pytest` 226/226 passed (214 + 12 new).
- Explicitly still open, not attempted this pass: CX-2 (context products with maker-checker — no model exists at all), CX-4 (`resources/read` records no consumption-lineage edges), CX-6 (per-consumer rate limits/budgets), MCP `prompts/*` handlers (advertised in `initialize` capabilities but unimplemented), and module 12's RT-1/2/3/4/6/7/8/9 (retrieval is a real single-source lexical scorer only — no vector projection, graph expansion, true multi-factor fusion, Postgres full-text index, or cross-source search; `retrieval.py` and `agent_orchestrator.py` both remain completely untested, a real gap worth its own increment).

### R29 documentation audit — Snowflake connector, OpenLineage ingestion, dbt quality bridge (backfilled records)

- This entry does not describe new code. It backfills the accomplishment-log record for three capabilities that were already sitting in the working tree from unattributed concurrent sessions (the Snowflake connector, OpenLineage ingestion, and the dbt-quality bridge — all previously visible only as the "2026-08-28 consolidation note" and the R27 lint/type fixup, neither of which described what they actually do) and corrects every tracker/status-matrix/module-doc row that had gone stale as a result. Done at the user's explicit request ("go through all the files and update the track again") after other concurrent sessions on this checkout stopped, so this record is now the authoritative baseline going forward.
- **Snowflake connector** (`src/aida/connectors/snowflake.py`, 517 lines) is a native pull adapter registered `IMPLEMENTED`/`BETA` in `connector_registry` — not `PLANNED` as every doc still claimed. It parses either a `snowflake://` URI or a structured JSON credential payload (`_parse_dsn`), discovers columns and primary/unique/foreign-key constraints across every database in the account via `INFORMATION_SCHEMA`, reusing the same `aida.connectors.discovery` assembly helpers as every other connector, estimates query cost via `EXPLAIN USING JSON` with a partition-pruning-ratio evidence field (`_extract_snowflake_explain_estimate`, so `capabilities.explain=True`), profiles tables with `APPROX_COUNT_DISTINCT`, and captures the real Snowflake query ID (`cur.sfqid`) as `warehouse_query_id="snowflake-query:<sfqid>"` — the same real-backend-identifier convention Oracle/SQL Server/BigQuery use. `tests/test_connectors_snowflake.py` (7 tests: identifier quoting, both DSN formats, EXPLAIN-JSON extraction, registry definition, discovery assembly, query execution) passes cleanly, verified directly in this session (`pytest tests/test_connectors_snowflake.py` → 7 passed). No live Snowflake account exists in any session, so connection/discovery/profiling against a real warehouse remain unverified — the same "implemented, unverified live" position as Oracle and BigQuery. Corrected: tracker `CN-2` (split into `CN-2a` Snowflake/`CN-2b` Databricks), `04-status-matrix.md`'s "Other connectors" row (added a dedicated Snowflake row), `05-gap-register.md`'s connector-fleet row, `07-connector-implementation-backlog.md` (added a full "Workstream E" record matching the Oracle/BigQuery format), and `20-modules/02-connectivity.md`'s adapter table and open-work list (which additionally had Oracle itself mis-stated as `PLANNED` maturity — it has been `BETA` since R19).
- **OpenLineage ingestion** (`src/aida/openlineage.py`, 272 lines; `src/aida/openlineage_api.py`, 433 lines; migration `8a7f3c1d4b22_openlineage_run_events`) is a real, mounted capability, not the `TODO`/"module unbuilt" the tracker and gap register claimed. `parse_openlineage_run_event` validates a bounded, value-free OpenLineage RunEvent payload (job/run/namespace/dataset model, facet-shape validation) and extracts column-lineage edges from the `columnLineage` facet. `POST /v1/lineage/openlineage` (mounted in `main.py`) is idempotent by SHA-256 event fingerprint, resolves input/output datasets against the existing catalog (exact, schema+table, and table-only matching tiers), and persists `OpenLineageRunEvent`/`OpenLineageDataset`/table/column edge rows with audit and outbox evidence; `GET /v1/datasources/{id}/openlineage-events` and `GET /v1/openlineage-events/{id}` expose them. What remains genuinely missing, confirmed by direct check in this session: **zero test files** reference OpenLineage anywhere in `tests/` (`ls tests/ | grep -i openlineage` → no matches), and no Airflow-sourced event has ever been posted to the endpoint — only the parser's own internal logic has been read, never exercised end to end. Corrected: tracker `LN-1` (`TODO` → `IN PROGRESS`), `04-status-matrix.md`'s "SQL / query lineage" row, and `05-gap-register.md`'s "Context products and MCP" row (which was unrelated to OpenLineage but was found to be separately and severely stale — see below).
- **dbt quality bridge** (`src/aida/dbt_quality_bridge.py`, 223 lines) couples dbt test outcomes into the existing `DataQualityIncident` lifecycle — a real, narrow instance of "quality signals driving other behavior," though not the broader DQ-3/RT-7/AG-6/TL-3 "quality → runtime coupling" (retrieval ranking, answer warnings, tool gating) that tracker row still correctly lists as `TODO` (`DataQualityIncident` has zero references in `retrieval.py`, `agent_orchestrator.py`, or `tool_api.py`, confirmed by grep). `infer_dbt_test_anomaly_type` classifies a failing dbt test (`not_null`/`unique`/`relationship`/`accepted_values`/`freshness` naming conventions) into the platform's existing anomaly taxonomy, and `reconcile_dbt_test_quality` opens, reopens, or resolves a deterministically fingerprinted `DataQualityIncident` per failing/passing test, wired into the manifest-import endpoint (`dbt_api.py`) via `parse_dbt_run_results`, which was already parsing `run_results.json` and persisting `test_status`/`test_failures`/`test_execution_time` per `DbtResource` with no consumer. `tests/test_dbt_quality_bridge.py` and `tests/test_dbt_artifacts.py` pass cleanly, verified directly in this session. No integration test exercises the full `POST .../artifact-imports` → incident-reconciliation path together. Corrected: tracker `LN-6` (`TODO` → `IN PROGRESS`) and `04-status-matrix.md`'s "dbt transformation intelligence" row.
- **Also corrected while auditing, found independently stale and outside the three items above:** tracker `KG-1`/`RL-4` (`TODO` → `IN PROGRESS` — Graph Explorer V2's bounded, policy-filtered relationship-candidate visibility already satisfies most of the exit bar; only projecting *approved* candidates into Neo4j itself, as opposed to declared FK constraints, remains) and tracker `MG-3` (`TODO` → `IN PROGRESS` — approved-route selection via `ModelRouteConfiguration`'s maker-checker lifecycle plus config-selected `route_key` gating has been real since R9/R11; only private-endpoint routing is unbuilt). `05-gap-register.md`'s "Context products and MCP" row separately claimed **"None — module unbuilt,"** flatly contradicting the tracker's own `CX-1`/`CX-3`/`CX-5` rows (which R28 had already correctly updated to `IN PROGRESS`/`DONE`) and the status matrix's "Context products and MCP" row (`Partial`) — corrected to match.
- Four parallel research agents did the actual code-vs-doc comparison (connectors; lineage/dbt-quality; semantics/glossary/graph/retrieval/agent; tools/gateway/governance/identity/UX) against `03-tracker.md`, `04-status-matrix.md`, and the relevant module docs; every other row they checked — including a full re-verification of `GL-1` through `GL-8`, `KG-2` through `KG-7`, all `RT-*`, `AG-*`, `SM-*`, and the entire tools/gateway/governance/identity/observability section — matched the code exactly and needed no change. src/aida/data_contracts.py (a `DataContractSpec`/SLA-evaluation module) was found to be genuine dead code: not imported anywhere outside itself, no route, no table, no test — it did not count as evidence toward `DQ-2` and was left alone at the time rather than either wired in or removed, since the user had not asked for either. (Since deleted 2026-08-31 under AU-6, once the end-to-end audit confirmed the same orphan status independently and a live duplicate, `runtime_contracts.py`.)
- Not run in this pass: a full repo-wide re-verification of every one of the tracker's 171 rows — the four agents' scope was targeted at the sections most likely to have drifted (connectors, lineage/quality, semantics/glossary/graph/retrieval/agent, tools/gateway/governance/identity/UX), on the working assumption (confirmed correct in every section checked) that rows already updated by R14/R24/R26/R28 were current and that Section A (structural foundation — `platform/` extraction, import-linter, etc.) and Section H/I/J (testing/performance/certification, drills, bank decisions) describe target-architecture and operational work with no code correlate to check.


### R29 addendum — competitor-comparison and module-doc sweep (same audit pass)

- Extended the R29 audit beyond the tracker/status-matrix/module docs it directly targeted, grepping `Snowflake`/`BigQuery`/`Oracle`/`OpenLineage`/`declare_planned`/`MCP` across every remaining `Docs/` subtree (`00-product`, `10-architecture`, `20-modules`, `30-contracts`, `40-engineering`, `50-security`, `90-reference`, `competitors`) to catch stale claims outside the four agents' original briefs. `00-product`, `10-architecture`, `30-contracts`, `40-engineering`, `50-security`, and `90-reference` all describe target architecture, competitor offerings, or wire contracts rather than Atlas's own current implementation state — nothing there needed correction.
- `Docs/competitors/05-codebase-gap-analysis-and-improvements.md`: "Connector Coverage" row corrected from "PostgreSQL & SQL Server (Beta)" / "**BEHIND**: Missing Snowflake, BigQuery, Databricks, and Oracle adapters" to reflect Oracle/BigQuery/Snowflake all being implemented (`BETA`, unverified live) and only Databricks/Teradata/Db2 still missing. "Context API / MCP Server" row corrected from "Internal REST API only" / "**BEHIND**: No standard...MCP server" to describe the real, tested, role-eligible `mcp_server.py` endpoint, noting it has not yet been exercised by a live external MCP client.
- `Docs/competitors/06-codebase-architecture-reference.md`: the connectors table was missing `bigquery.py`/`snowflake.py` rows entirely and still described `registry.py` as using `declare_planned()` "for BigQuery / Snowflake / Databricks" — added the two missing rows and corrected the registry row and the "Planned but not yet implemented" line to list only `databricks`/`teradata`/`db2`. Gap-list row #3 ("No BigQuery / Snowflake pull adapters", self-contradictorily citing the very files that disprove the claim) reworded to the real remaining gap (Databricks/Teradata/Db2). Gap-list row #1 ("No MCP Server", citing `mcp_server.py` as a file *to create*) reworded to reflect that the 652-line, tested MCP server already exists and the real gap is external-client verification.
- `Docs/20-modules/09-lineage.md` §12: "ETL / OpenLineage" row corrected from "**Not implemented**" to "Partial" with the same real-implementation/zero-test-coverage caveat as the tracker; "DBT" row's target column had `run_results.json` removed since `dbt_quality_bridge.py` now consumes it.
- `Docs/20-modules/15-model-gateway.md` §14/§15: "Route versions" row and the `MG-3` open-work line both still described "bank-approved route selection" as entirely un-implemented target work; corrected to note the config-selected `route_key` gating is real (since R9/R11) and only private-endpoint routing remains open, matching the tracker's `MG-3` correction.
- Checked and found already accurate, no change needed: `Docs/20-modules/10-knowledge-graph.md` (Graph Explorer V2 already marked Implemented), `Docs/20-modules/06-relationship-intelligence.md` (RL-4/"projection of approvals to Neo4j" already correctly listed as outstanding), `Docs/20-modules/19-context-products-and-mcp.md` (already carries the detailed, accurate MCP partial-build breakdown from R28).
- This closes out the "go through all the files" audit request. Standing open item, unrelated to documentation accuracy: no live Docker verification has been performed against a real Oracle/BigQuery/Snowflake backend in this session (blocked on this session's network egress allowlist blocking the user's local Docker host); the user has taken over verification themselves via `/tmp/verify_oracle.ps1` for Oracle and asked this session to move on from requesting manual debugging steps.


## 2026-08-29 — Unified Lineage Explorer (EA.14) and Collibra platform gap wiring

User shared the Collibra Data Lineage and Collibra Platform product pages and asked for the
findings to be captured as feature requirements with references, and for the highest-value
gap to actually be built rather than only documented.

**Built:**
- `src/aida/unified_lineage.py` — pure, database-free graph module: `UnifiedLink`,
  `expand_frontier`, and `traverse`, generalizing `aida/knowledge_graph.py`'s BFS to string
  node ids (needed because dbt resources and OpenLineage datasets without a matched catalog
  table get a synthetic id, e.g. `dbt:<uuid>`, `openlineage:<namespace>:<name>`, instead of
  disappearing from the graph).
- `src/aida/unified_lineage_api.py` — `GET /v1/datasources/{id}/unified-lineage/graph` (merges
  `MetadataConstraint` FKs, `RelationshipCandidate` suggestions, `DbtLineageEdge` dependencies
  from each project's latest imported manifest, and `OpenLineageTableEdge` ETL edges into one
  node/edge set, bounded and truncation-flagged like the existing knowledge-graph endpoints)
  and `GET /v1/datasources/{id}/unified-lineage/impact/{node_id}` (bounded transitive
  upstream/downstream traversal — replaces `/v1/metadata/tables/{id}/impact`'s direct-reference
  count for the nodes reachable in the unified graph; that endpoint is left in place since it
  also covers metrics/tools not part of the lineage graph).
- New schemas in `schemas.py`: `UnifiedLineageNodeRead`, `UnifiedLineageEdgeRead`,
  `UnifiedLineageGraphRead`, `UnifiedLineageImpactNodeRead`, `UnifiedLineageImpactRead`.
- Wired into `src/aida/main.py`.
- `tests/test_unified_lineage.py` — 8 tests: pure BFS/traversal behavior (direction semantics,
  bounding, transitive multi-hop depth across mixed edge sources) with no database, plus
  OpenAPI-contract and schema-serialization tests mirroring `tests/test_knowledge_graph.py`'s
  style. Full suite (165 tests before and after) plus `ruff check`, `ruff format`, and
  `mypy --cache-dir=/tmp/mypy_cache` all pass. Verified by installing dependencies into an
  ephemeral `uv` environment at `/tmp/aida-venv` (`UV_PROJECT_ENVIRONMENT=/tmp/aida-venv uv
  sync --frozen --extra dev`) since the checked-in `.venv` is a Windows venv unusable from the
  Linux device-bridge shell, and its directory can't be overwritten from that shell
  (`Operation not permitted` on `.venv/.gitignore`).

**Known limitation, documented rather than silently accepted:** column-level edges are still
name-matched (dbt UI) or absent (unified graph is table-level only); view/procedure and BI
nodes are not yet in the unified graph; there is no export. These are exactly LN-10, LN-11,
LN-12, tracked as open work below.

**Documentation:**
- New `Docs/competitors/08-collibra-lineage-and-platform-analysis-2026-08.md` — the
  screenshot-driven capability comparison for both pages, with source URLs, and the resulting
  gap list.
- `Docs/90-reference/03-sources.md` — added the Collibra Data Lineage URL.
- `Docs/20-modules/09-lineage.md` — Impact row and HTTP surface updated for the delivered
  endpoints; LN-7 marked delivered; LN-9 (delivered) through LN-12 (open) added to open work.
- `Docs/60-delivery/02-epic-backlog.md` — added `EA.14` (delivered, full acceptance detail) and
  `EE.8`–`EE.11`, wiring the CP-2/CP-3/CP-5/CP-6/CP-7/CP-8 platform requirements that
  `Docs/20-modules/19-context-products-and-mcp.md` §15.2 had already specified in detail (from
  an earlier pass over the same Collibra platform material) but that had not yet been turned
  into epic-backlog or gap-register entries.
- `Docs/60-delivery/05-gap-register.md` — updated the "Relationship and lineage evidence" and
  "Context products and MCP" rows, and added four new rows to "Newly identified gaps" for the
  lineage-MCP, context-compiler, product/contract-registry, and AI-registry/trust gaps.
- `Docs/20-modules/19-context-products-and-mcp.md` — cross-referenced the new competitors doc
  and the CP-* -> EE.* epic mapping.


## 2026-08-29 (continued) — Lineage MCP tools (EE.10, partial) and 5-page Collibra review

User pasted five more Collibra product page URLs (Data Marketplace, Data Catalog,
Integrations & APIs, MCP Server, Data Governance) and asked for a further review and for the
platform to keep being built out meaningfully.

**Reviewed:** all five pages via WebFetch. Most of what they show was already anticipated by
the CP-1..CP-14 requirements added to `Docs/20-modules/19-context-products-and-mcp.md` §15.2 in
an earlier pass. Two genuinely new, concrete gaps came out of the MCP Server page specifically
(it lists 25+ tools, both read and write, plus "fuzzy name matching and concept mapping"):
`MCP-2` (no MCP write path to catalog stewardship) and `MCP-3` (no fuzzy entity resolution --
every tool we expose needs an exact UUID). Full findings:
`Docs/competitors/09-collibra-marketplace-catalog-integrations-mcp-governance-2026-08.md`.

**Built — EE.10 (partial):**
- Refactored `unified_lineage_api.py`'s two route bodies into reusable payload builders
  (`build_unified_lineage_graph_payload`, `build_unified_lineage_impact_payload`) that take an
  already-loaded, already-authorized `DataSource` rather than doing their own `Depends`-based
  lookup, so the exact same merge/traversal logic can be called from a second transport. Added
  `LineageNodeNotFoundError` (plain `ValueError` subclass, not `HTTPException`) so the REST
  route and the new MCP tool can each translate a missing node into their own transport's error
  shape from one raise site.
- `mcp_server.py`: two new native MCP tools, `atlas__get_lineage_graph` and
  `atlas__get_lineage_impact`, dispatched in `_handle_tools_call` before the
  `GovernedToolVersion` lookup (native tools are not backed by a published tool row). Listed in
  `tools/list` only for callers whose roles intersect `UNIFIED_LINEAGE_READER_ROLES` --
  eligible-tool exposure applied the same way it already is for governed SQL tools, including
  the anti-enumeration property (an ineligible call gets the identical "not found or not
  published" text as a genuinely unknown tool name).
- 7 new tests in `tests/test_mcp_server.py` (role denial, invalid UUID, cross-org datasource,
  missing `node_id`, and two success-path tests that monkeypatch the payload builders --
  consistent with this test file's existing no-database convention) plus 1 in
  `tests/test_unified_lineage.py`'s neighborhood confirming the refactor didn't change route
  behavior.
- Full suite, `ruff check`, `ruff format`, `mypy` all clean for every file this session touched.
  **Noted, not fixed** (out of scope -- belongs to the separate, already-uncommitted
  `context_product_api.py`/`context_product_policy.py` work): `tests/test_context_products.py`
  is flaky, failing a different test on about 1 in 3 runs with
  `AttributeError: '_Result' object has no attribute 'all'` in `context_product_policy.py`,
  independent of anything touched this session (confirmed by running it in isolation, repeatedly).
- Also corrected stale text in `Docs/20-modules/19-context-products-and-mcp.md` §13: it still
  said "no `ContextProduct` concept anywhere in the codebase," which predates the (uncommitted)
  `context_product_api.py` work discovered while wiring these tools in -- `ContextProduct` /
  `ContextProductVersion` models and their MCP resource-read path already exist.

**Not built, tracked as open work:** MCP-2 (write operations), MCP-3 (fuzzy resolution),
transformation-detail-as-a-tool, consumption-lineage recording for the new tools (same
pre-existing `CX-4` gap `resources/read` already has), and a dedicated cross-tenant leak test
for the two new tools.


## 2026-08-29 (continued) — Code review of a separately AI-generated build-out; router-wiring and type-safety fixes

User ran a different AI model against this same repository in parallel with this session and
asked for the result to be reviewed, the docs corrected to match reality, and any real bugs
fixed. This entry supersedes several "not met" / "not built" notes from the two entries above
it, which the other model's work closed.

**Reviewed** (via `git diff`/`git show` against commits `2fa7667` "Harden context products and
unified lineage" and `99cc556`, plus the working tree, which was still being actively written to
during this review — see caveat below): `context_product_policy.py`, `lineage_cache.py`, the
`unified_lineage_api.py` and `mcp_server.py` hardening diff, the `9a6d4f21c8b7` and
`b4e8f2a71c90` migrations, `src/aida/models.py`'s new ORM classes, `platform_schemas.py`,
`context_compiler.py` / `context_compiler_api.py`, and `product_marketplace_api.py`.

**Findings — fixed:**
- `src/aida/main.py` did not register the `product_marketplace_api` or `context_compiler_api`
  routers. Both files were fully implemented (contract/product lifecycle, marketplace search,
  access requests, context compilation, drift detection) but every one of their ~16 endpoints
  was unreachable — confirmed by generating `app.openapi()` before and after. Fixed by adding
  both imports and `include_router` calls in the correct alphabetical position.
- `product_marketplace_api.py::_validate_product_references` assigned `session.get(...)` results
  of three different ORM types to the same `asset` variable across an if/elif/else chain without
  an explicit annotation; `mypy` narrowed it to the first branch's type and flagged the other two
  as `arg-type` errors. Fixed with an explicit
  `asset: MetadataTable | SemanticModelVersion | ContextProductVersion | None` annotation.
  Runtime behavior was already correct — this was a type-checker-only defect, but a real one
  (would fail a `mypy` CI gate).
- My own `tests/test_mcp_server.py::test_native_lineage_tool_slugs_match_declared_definitions`
  (written last session) hard-coded the expected native-tool slug set to only
  `{"get_lineage_graph", "get_lineage_impact"}`. The other model legitimately added
  `resolve_entity` and `get_transformation_detail` as real, fully-wired native tools (not
  stubs — traced both handlers), so my test was failing against correct new behavior. Updated
  the assertion to the current four-tool set.

**Findings — verified as non-issues:**
- The context-compiler's `YAML` target sets `content_type: application/yaml` but the body is
  canonical JSON. Not a defect: JSON is a valid subset of YAML 1.2, so the content is valid
  YAML, just not idiomatically formatted (no `pyyaml` dependency exists in the project to do
  better yet). Documented as a simplification in `02-epic-backlog.md` (EE.9) rather than fixed.
- The previously-noted flaky `tests/test_context_products.py` (`AttributeError: '_Result' object
  has no attribute 'all'`, intermittent) did not reproduce across 6 consecutive runs (1 full run
  + 5 targeted re-runs) after the other model's changes. Whatever caused it earlier appears to
  already be resolved; no fix was needed or applied.
- Org-scoping, role-gating, and maker-checker patterns across `product_marketplace_api.py` and
  `context_compiler_api.py` consistently follow the codebase's existing conventions
  (`enforce_organization` called before any read/write on every scoped lookup; role-based
  discoverability filtered at the SQL level, not post-filtered in Python; cache/audit keys
  scoped by organization before the caller's authorization is checked). No authorization gaps
  found.
- `context_product_policy.py`, `lineage_cache.py`, and the `unified_lineage_api.py`/
  `mcp_server.py` hardening diff (bounded `register_node`/`register_link` helpers replacing my
  original unbounded `nodes.setdefault(...)` calls, org+datasource-scoped Redis cache keys,
  quality-gated context-product access, `ContextProductConsumptionEdge` tracking) are genuine
  improvements over what this session shipped last time, not regressions.

**Findings — flagged, not fixed (need a decision, not just an edit):**
- `ai_asset` / `ai_asset_version` / `ai_assessment` have models, a migration, and Pydantic
  schemas (including an `AiTrustScoreRead` contract) but no API/service layer and no trust-score
  computation function anywhere in the codebase — the schema has no producer. `EE.11` downgraded
  from "open" to "partial — data layer only" rather than claimed delivered.
- `scratch/repo_bundle{3..8}.tar.gz` / `repo_live.tar.gz` (~5.4 MB of binary tarballs) and
  `proof-gaps-round-*-report.md` files are committed to git history and `scratch/` is not in
  `.gitignore`. Left alone: removing tracked history is a decision for the user, not something
  to do unilaterally mid-review.
- No dedicated unit tests exist yet for `resolve_entity`, `get_transformation_detail`,
  `product_marketplace_api.py`, or `context_compiler_api.py` (only the slug-set test, now
  fixed, indirectly touches the first two). No leak/cross-org test for the two newest MCP tools.

**Verification:** built a fresh Linux `uv` venv (`UV_PROJECT_ENVIRONMENT=/tmp/aida-venv`), ran
`ruff check` (clean on every file this pass touched or fixed; pre-existing `E501` line-length
warnings in the other model's new files were left alone as cosmetic), `mypy --cache-dir=/tmp/mypy_cache`
(clean on every file reviewed, after the one fix above), and the full `pytest` suite (all green,
including 6 consecutive clean runs of the previously-flaky file).

**Caveat this session flagged to the user directly:** the repository was being actively written
to during this review — new untracked files (`context_compiler.py`, `product_marketplace_api.py`,
the `b4e8f2a71c90` migration, `platform_schemas.py`) appeared with modification timestamps only
seconds to minutes old partway through, and a `.git/index.lock` was present for over ten minutes
without the index itself changing. No git write operations (commit, `rm --cached`, etc.) were
performed this pass to avoid racing whatever process holds or held that lock; all fixes above are
uncommitted working-tree edits only.

**Not built, tracked as open work:** trust-score computation and AI-registry API layer (`EE.11`
remainder), dedicated tests for the four new modules above, a leak test for `resolve_entity` /
`get_transformation_detail`, idiomatic YAML compilation target, contract breaking-change
approved-exception override, and the `scratch/` repo-hygiene cleanup.

### R34 agentic data platform foundation completion

- Completed the data product and contract control plane: immutable versions, typed ports,
  normalized producer/consumer roles, structural compatibility checks, independent
  breaking-change exceptions, publication/supersession/retirement, and audited access
  request/approve/reject/expire/revoke lifecycle.
- Added policy-filtered marketplace REST/UI surfaces and a deliberately bounded MCP write:
  agents may request product access but cannot grant it or bypass maker-checker review.
- Added the deterministic Context Compiler with stable hashes, structural drift evidence,
  quality/lifecycle gates, and MCP, REST, YAML, OSI, ODCS, Snowflake Semantic View, and
  Databricks Metric View targets.
- Added a tenant-scoped AI asset registry, immutable versions, independent assessments,
  maker-checker publication, and deterministic explainable trust scoring. Seven inspectable
  factors total exactly 100 points; prohibited risk, critical runtime incidents, missing or
  failed assessments, and weak high-risk evaluations are explicit blockers.
- Completed MCP prompts, deterministic fuzzy entity resolution, redacted dbt transformation
  detail, atomic Redis consumer budgets, and governed marketplace access requests. Budget keys
  hash principals and production fails closed if an enabled budget store is unavailable.
- Extended the Kafka/Neo4j projector with generation-stamped unified FK, approved-relationship,
  dbt, and OpenLineage nodes/edges. Optional Neo4j impact reads are bounded and fail open to the
  authoritative PostgreSQL graph; Redis remains an optional response cache.
- Added marketplace, authoring, compiler, AI registry, assessment, and trust-factor UI surfaces;
  added migration `b4e8f2a71c90`; and consolidated pure behavior/OpenAPI coverage in
  `tests/test_agentic_platform.py`.
- Remaining scale expansion is explicit rather than hidden: purpose ABAC/workload identity,
  entitlement-provider fulfillment, managed compliance templates/remediation, provider sync,
  idiomatic YAML/downloads/external validators, million-node projection certification,
  broader MCP stewardship writes, privacy operations, adoption analytics, and CP-S8 ecosystem
  integrations.


## 2026-08-29 (continued) — Second review pass: AI registry / MCP budget, and a correction

User repeated the "review the code / update the document / fix if needed" request. By this
point the repo had settled (the other model's process finished; `.git/index.lock` was gone) and
everything from the prior entry had been committed in `434e98d "Build agentic data marketplace
and AI trust platform"`, including this session's router-wiring and `mypy` fixes.

**Correction to the entry above:** it claimed "no dedicated tests exist yet" for
`product_marketplace_api.py`, `context_compiler_api.py`, `ai_registry_api.py`, and
`mcp_budget.py`. That was wrong — `tests/test_agentic_platform.py` (282 lines, 10 tests)
already covered contract compatibility, product-port validation, marketplace access-expiry,
context-compiler determinism and drift, trust-score explainability with an incident blocker,
assessment scoring, raw-evidence rejection, fuzzy-entity scoring, disabled-budget behavior, and
an OpenAPI route-publication smoke test — the search that missed it only grepped for
`ai_registry|mcp_budget|marketplace|compiler` in filenames, which `test_agentic_platform.py`
doesn't match. `02-epic-backlog.md` and `05-gap-register.md` have since been corrected (by the
same process that built this code) to credit that file; no further doc fix was needed there.

**Reviewed this pass:** `ai_registry.py` (`compute_ai_trust_score`, `score_assessment_controls`)
and `ai_registry_api.py` (full AI-asset lifecycle: create/version/submit/assess/trust, wired
into `semantic_api.py`'s maker-checker dispatcher under `AI_ASSET_VERSION`, including the
one-approved-per-asset supersede-on-approve logic), and `mcp_budget.py` (Redis
`INCR`+`EXPIRE` Lua-script token counter, wired into `mcp_endpoint` for `REQUEST_MINUTE` /
`TOOL_DAY` / `CONTEXT_DAY` buckets, fail-closed in staging/production, fail-open in
development). No bugs found — `ruff` and `mypy` clean, and the maker-checker approval path
correctly supersedes the prior approved version.

**Added:** `tests/test_ai_registry.py`, 11 tests giving `compute_ai_trust_score` and
`score_assessment_controls` edge-case coverage `test_agentic_platform.py` didn't have:
`PROHIBITED` risk tier, `HIGH` risk below the evaluation threshold, a missing assessment alone
(vs. bundled with an incident), a failed assessment alone, and `score_assessment_controls` with
empty and `NOT_APPLICABLE`-only control lists. Full suite green (was already green; this only
added coverage, changed no behavior).

**Open at that review point:** idiomatic YAML compilation, file-export delivery,
entitlement-provider fulfillment, managed compliance templates/remediation/retirement APIs,
provider sync, score history, dependency-graph visualization, and repo hygiene. The production
features in this list were subsequently closed by R35 below; shared-history cleanup remains.

### R35 production acceptance and control-plane hardening

- Applied migrations `b4e8f2a71c90` and `c8a4d3e91f02` to live PostgreSQL and verified the
  expected evidence tables. Redis and Neo4j live probes passed; the rebuilt API reported ready.
- Enforced OIDC-backed MCP workload principal types outside development, propagated bounded
  business-purpose claims, added exact purpose ABAC to Context Product REST/compiler/MCP reads,
  and persisted generic immutable MCP consumption evidence without prompts, SQL, or values.
- Added idempotent entitlement fulfillment state and outbox/webhook adapters. Governance remains
  authoritative when providers fail; provisioning and revocation are independently retryable.
- Added managed EU AI Act, NIST AI RMF, and AI-UC assessment templates; durable remediation and
  independent risk acceptance; maker-checker retirement; immutable trust history; value-free
  provider evidence sync; and dependency graph APIs.
- Added idiomatic deterministic YAML, validated attachment downloads, and structural conformance
  checks for MCP, REST, YAML, OSI, ODCS, Snowflake, and Databricks compiler targets.
- Enabled Redis lineage caching, MCP budgets, and Neo4j lineage reads in the local integration
  stack while retaining production fail-closed/fallback behavior defined in code.


## 2026-08-29 (continued) — Local portfolio analytics completion and verifier hardening

### Completed

- Added tenant-scoped portfolio analytics summary and trend APIs in `product_marketplace_api.py`
  over existing product, contract, context-read, MCP, tool, query, quality, and agent evidence.
- Extended `scripts/verify-local.ps1` to create and publish a Context Product, publish a linked
  Data Product and Data Contract, request and approve marketplace access, provision the
  entitlement through the outbox-backed local path, and verify the new portfolio analytics
  endpoints end to end.
- Fixed three real local defects uncovered by that verifier pass: marketplace search used
  `DISTINCT` across JSON-backed version rows and failed on PostgreSQL; marketplace access
  requests could flush before their governance-review row existed and misreport the resulting
  foreign-key failure as "already pending"; and governance approval/outbox plus marketplace
  access-request listing both returned non-JSON-safe payloads.
- Added regression coverage in `tests/test_agentic_platform.py` for portfolio trend bucketing,
  marketplace access-request flush ordering, and governance outbox expiry serialization.

### Verification evidence

- Repo-wide static and test gates passed on Saturday, August 29, 2026: `379` tests passed, Ruff
  clean, and strict mypy clean.
- Final local verifier run passed on Saturday, August 29, 2026 with organization
  `abe5877e-e12e-4095-88a4-411562a763f6`, datasource `d623616a-9df9-48d4-bbc5-3b4e51d20208`,
  analysis run `0545b916-bd88-4e46-b322-a0bfde07bfcb`, Context Product version
  `cf6ebf7b-2d69-48a4-b001-015f0ecbb13d`, Data Product version
  `3e18b674-dba8-4f46-9f45-6c24764ea8fb`, Data Contract version
  `cd5308d0-44ca-4756-82d3-8094c951ebf6`, marketplace access request
  `dff2bda5-f0ed-4e2c-ab18-3817efb7a885`, and tool-first agent run
  `eacc8511-28c8-4d41-8c92-c5ae3179f3e6`.
- The same verifier proved `portfolio_access_requests = 1`, `portfolio_context_reads = 1`,
  `portfolio_agent_runs = 3`, `portfolio_top_product_key = customer_portfolio_1788039914`, and
  an outbox-backed entitlement state of `PENDING`, which is the correct local fail-safe posture
  without an external fulfillment provider.

### Current limitations

- The remaining open items are the dedicated-environment gates rather than local code-path gaps:
  million-node lineage/load certification, authoritative BI/procedure lineage, privacy
  operations, workflow templates, external provider certification, and browser/accessibility QA.

## 2026-08-29 (continued) — MCP lineage-tool coverage completion

### Completed

- Added dedicated unit coverage in `tests/test_mcp_server.py` for the two newest native
  lineage MCP tools, `resolve_entity` and `get_transformation_detail`.
- The new tests cover input validation, anti-enumeration denial symmetry for ineligible callers,
  successful value-free JSON payload rendering, and the not-found branch for transformation
  detail reads.
- This closes the local code-review gap that previously noted the tools existed in production
  code but only had slug-level coverage in the test suite.

### Verification evidence

- Focused MCP verification passed on Saturday, August 29, 2026:
  `python -m pytest tests/test_mcp_server.py -q` (`29` passed),
  `python -m ruff check tests/test_mcp_server.py src/aida/mcp_server.py`, and
  `python -m mypy src/aida/mcp_server.py`.

### Current limitations

- The remaining open items are still dedicated-environment gates rather than local code-path
  gaps: million-node lineage/load certification, authoritative BI/procedure lineage, privacy
  operations, workflow templates, external provider certification, and browser/accessibility QA.

## 2026-08-29 (continued) — AI registry dependency graph UI completion

### Completed

- Extended the `ai-registry` portal view to render governed AI dependency topology using the
  shared graph engine already used by Knowledge Graph and Unified Lineage.
- Added operator actions for dependency inspection and retirement requests directly from the AI
  asset portfolio table, reusing the existing `/ai-asset-versions/{version_id}/dependencies`
  and `/ai-assets/{asset_id}/retire` API paths.
- Added a value-free side panel that shows the selected asset or dependency node's status,
  owner, provider, dependency counts, and approved references without exposing prompts or source
  values.

### Verification evidence

- Repo-wide gates remained green on Saturday, August 29, 2026 after the UI change:
  `python -m pytest -q`, `python -m ruff check .`, and `python -m mypy src`.
- The full local verifier passed again on Saturday, August 29, 2026 with `status = PASS`,
  `ui_status = HEALTHY`, and `ui_url = http://localhost:3000`, preserving the same end-to-end
  workflow evidence for Context Products, marketplace access, AI registry/trust, and portfolio
  analytics.

### Current limitations

- The remaining open items are still dedicated-environment gates rather than local code-path
  gaps: million-node lineage/load certification, authoritative BI/procedure lineage, privacy
  operations, workflow templates, external provider certification, and browser/accessibility QA.

## 2026-08-29 (continued) — Refactor Phase 0: import-linter ratchet + `platform/` extraction (ST-01–ST-04)

### Completed

- Added `[tool.importlinter]` to `pyproject.toml` with `root_packages = ["atlas"]` and a
  `platform-is-the-lowest-layer` layers contract (`atlas.modules` → `atlas.platform`, never the
  reverse). Scoped as a permissive baseline: only the target `atlas` package is checked, matching
  Phase 0's ratchet design — `aida`, the pre-existing flat package, is intentionally out of scope
  until the strangler migration reaches each module.
- Extracted `db.py`, `config.py`, `logging.py`, and `context.py` from `aida` into
  `atlas.platform`, adapting `db.py`'s internal `config` import to the new location. Left a
  backward-compatible re-export shim at each old `aida.*` path so the 40+ existing import sites
  across `src/aida/*` and `tests/*` needed no changes.
- Added `src/atlas` to the `hatchling` wheel package list so the extracted modules are included
  in production builds, not just the editable dev install.
- Confirmed `scripts/generate_module.py` (tracker ST-01) and `tests/test_tier0_invariants.py`
  (tracker ST-03, 4 of 9 invariants) already existed from prior work; the tracker had gone stale
  and still listed both as `TODO` — corrected to reflect actual repo state.
- Deliberately did **not** touch `models.py`, `schemas.py`, `api.py`, or any Phase 2+ work — a
  concurrent session was actively editing those same files for ADR-0017 (domain-complete tenancy)
  while this work was in progress, and the refactor plan itself calls Phase 2 (the models/schemas
  split) the one phase needing a migration freeze.

### Verification evidence

- Built an isolated Python 3.13 verification environment (`uv venv` + `uv pip install -e ".[dev]"`)
  outside the repo, since the checked-in `.venv` is a Windows venv not runnable from this session.
- `python -m pytest -q`: baseline before any change was fully green (no failures). After the
  change, all tests pass except 3 in `tests/test_operational_behaviors.py`
  (`test_scheduler_commits_run_and_evidence_before_workflow_dispatch`,
  `test_scheduler_defers_rejected_admission_without_dispatch`,
  `test_due_scan_policies_statement_orders_by_priority_then_next_run_at`) — confirmed via
  `git diff` to belong to the concurrent session's in-progress `computed_usage_boost` scheduling
  feature (ADR-0017 §8), not this change: `scheduler.py` and `models.py` were mid-edit for that
  feature throughout this verification, unrelated to `db`/`config`/`logging`/`context`.
- `pytest -q src/atlas/modules/identity_tenancy` (standalone module execution) passes.
- `lint-imports`: `platform-is-the-lowest-layer KEPT` — `Contracts: 1 kept, 0 broken`.
- `ruff check` and `mypy` (strict) clean on every new and changed file.

### Current limitations

- ST-02's exit criterion ("new violations fail CI") is not fully met: this repo has no CI
  pipeline at all yet (no `.github/workflows`), so the contract passes locally but isn't enforced
  automatically. Setting up CI is a separate, larger gap.
- ST-03 remains 4 of 9 invariants; the other 5 (INV-1, INV-5, INV-6, INV-7, INV-9) need
  infrastructure that does not exist locally yet (live Neo4j/search replay, an all-endpoints fake
  session harness, a certification-result store) — see the docstring in
  `tests/test_tier0_invariants.py` for the reasoning per invariant.
- ST-04 covers 4 of the ~10 files/areas Phase 1 names. `main.py` was deliberately left where it
  is: it currently imports nearly every domain router (violating `platform-purity` as-is), and the
  refactor plan's own sequencing defers untangling that to Phase 5 (the `api.py` router split)
  rather than moving it in its current shape. `events.py` and the pagination/idempotency/
  error-taxonomy/telemetry scaffolding remain unbuilt.
- Phases 2 and onward (splitting `models.py`/`schemas.py`, extracting leaf/runtime modules) are
  untouched — see `Docs/60-delivery/03-tracker.md` ST-05 onward.

## 2026-08-29 (continued) — Refactor doc corrections found during ST-04 verification

### Completed

- Checked whether Phase 2 (models/schemas split) had unblocked since the last entry: it hadn't —
  `models.py`, `schemas.py`, and `api.py` were still uncommitted and actively changing under the
  concurrent session (a new file, `context_product_api.py`, picked up a modification between
  checks). Left Phase 2+ untouched again; did documentation-only work instead that needed no code
  freeze.
- Corrected `40-engineering/06-refactor-plan.md` Phase 1: it listed `events.py` (outbox
  mechanics) as moving to `platform/`. Read the file — it directly constructs and writes
  `AuditEvent`/`OutboxEvent` (`aida.models`), module 20's owned tables per
  `10-architecture/04-module-decomposition.md` §4 and §9, not domain-free infrastructure. Moving
  it to `platform/` as written would have failed the `platform-purity` contract (ST-02) on day
  one. `04-module-decomposition.md` §9 already had the correct target (module 20, Phase 3/4);
  fixed the refactor plan to match, and fixed the same incorrect claim in
  `src/atlas/platform/__init__.py`'s docstring (written in the previous entry).
- Flagged a real, previously undocumented architectural tension in
  `10-architecture/04-module-decomposition.md` (new §5.3): three L2 modules (`05` profiling, `09`
  lineage, `11` data-quality) depend on `16 query-gateway`, an L3 module, contradicting the
  document's own layering rule; separately, `09` and `16` list each other as callable, which is a
  cycle contradicting the `no-cycles` contract the same document says CI will enforce. Added
  tracker `ST-11` (P0, unassigned) so this is resolved before Phase 4 extracts those modules,
  rather than being discovered mid-extraction.

### Verification evidence

- `ruff check` clean and `ast.parse` valid on the one `.py` docstring touched
  (`src/atlas/platform/__init__.py`); `pytest -q src/atlas/modules/identity_tenancy` still passes.
  No other code changed in this entry — documentation only.

### Current limitations

- ST-11 is flagged, not resolved — it needs an architecture-owner decision (redraw the layer
  diagram to move `16` down, or narrow what `05`/`09`/`11` actually need from it), not something
  to decide unilaterally.
- Phase 2 (models/schemas split) and the leaf-module extraction it unblocks remain untouched;
  still gated on the concurrent session's ADR-0017 work landing.

## 2026-08-30 — Log-scrubbing verification (OB-8) closed; TS-3 logs slice closed

### Completed

- A concurrent session was already active on this branch's namesake work (Snowflake/dbt/lineage
  MCP); picked an unrelated, self-contained P0 gap instead rather than duplicate that effort.
- `Docs/10-architecture/01-principles-and-invariants.md` INV-6 names
  `test_no_source_values_in_control_plane` as the invariant test and states it needs a live
  Neo4j/search stack and a full ingestion pipeline not present in this environment
  (`tests/test_tier0_invariants.py` module docstring says the same). That full-fixture test is
  still out of reach here, but the logs slice of INV-6 — OB-8 ("Log-scrubbing verification |
  Sentinel scan passes") — did not require live infrastructure and had no code behind it at all:
  `src/atlas/platform/logging.py` configured `structlog` with no redaction processor in the
  pipeline, and no test anywhere asserted that a secret value never reaches a rendered log line.
- Added `redact_sensitive_data`, a `structlog` processor in `src/atlas/platform/logging.py`,
  wired into `configure_logging`'s processor chain immediately before `JSONRenderer`. It redacts
  by key name (case-insensitive denylist covering password/secret/token/credential/api_key/
  authorization/jwt/hmac/private_key/connection_string/dsn/cookie and variants, matched on
  normalized `[^a-z]`-stripped keys so `db_password`, `apiKey`, and `client-secret` all match),
  recursing through nested dicts, lists, tuples, and sets; a value whose *key* is itself
  secret-shaped (e.g. `credentials`) is redacted wholesale rather than recursed into, since a
  nested non-sensitive field inside a container named `credentials` isn't a safe assumption. As
  defense in depth for secrets logged into free-text messages rather than structured keys, it
  also pattern-redacts JWTs, `user:pass@host` connection strings, `Bearer <token>` values, and AWS
  access-key IDs inside string values.
- Added `tests/test_log_scrubbing.py` (6 tests): key-based redaction, nested-structure redaction,
  whole-container redaction when the container key itself is sensitive, non-sensitive fields
  passing through unchanged, free-text pattern redaction, and — the OB-8 exit criterion itself —
  `test_sentinel_scan_end_to_end_log_output`, which calls the real `configure_logging`, logs a
  sentinel value under multiple sensitive keys through `structlog.get_logger`, captures real
  stdout, and asserts the sentinel is absent from the rendered JSON while a `tenant_id` field
  survives untouched.
- Updated tracker `OB-8` to DONE and `TS-3` to IN PROGRESS (logs closed; tables/events/traces
  still blocked on the same live-infrastructure gap as INV-6/`test_no_source_values_in_control_plane`).

### Verification evidence

- `uv run pytest -q` (full suite, Python 3.13 via `uv sync --python 3.13 --extra dev`): all tests
  pass, including the 6 new tests in `tests/test_log_scrubbing.py` and the pre-existing
  `tests/test_tier0_invariants.py` and `tests/test_secrets.py` suites (unaffected).
- `uv run ruff check src/atlas/platform/logging.py tests/test_log_scrubbing.py`: clean. Repo-wide
  `ruff check .` shows 6 pre-existing findings in unrelated files (`context_product_api.py`
  import order, a long line in `workflows/scheduler.py`) that predate this change — confirmed via
  `git status` showing no modification to those files.
- `uv run mypy src/atlas/platform/logging.py`: clean (strict mode); the processor is typed against
  `structlog`'s actual `Processor` signature (`MutableMapping[str, Any] -> Mapping[str, Any]`),
  not a loosened one.
- `uv run lint-imports`: `Contracts: 2 kept, 0 broken` — unchanged, no new cross-module imports
  introduced.
- `uv.lock` had unrelated pre-existing drift from `pyproject.toml` (missing `import-linter`,
  stale `pyyaml`/`types-pyyaml` entries) surfaced by `uv sync`; reverted with `git checkout --
  uv.lock` rather than committing it, since re-locking is a separate concern from this change.

### Current limitations

- TS-3's tables/events/traces sentinel scan (the rest of INV-6, `test_no_source_values_in_control_plane`)
  remains open — it genuinely needs the live Neo4j/search + full ingestion pipeline Tier 3/4
  infrastructure this environment doesn't have, same as documented for ST-03 above. This entry
  does not claim otherwise.
- The key-name denylist is a fixed list, not sourced from a schema; a secret field with a name
  outside `_SENSITIVE_KEY_TOKENS` (e.g. an idiosyncratic vendor field name) would not be caught by
  the key-based path and would only be caught if it happened to match one of the value patterns.
  Extending the denylist as new sensitive field names are found is expected maintenance, not a
  gap unique to this change.
- No audit/outbox event payload was found constructing log lines from secret material during this
  pass (a targeted `grep` across `src/aida` for `structlog.get_logger`/`get_logger(` call sites
  found none touching `secrets.py`/`security.py`/`query_gateway.py`), so this closes a
  defense-in-depth gap rather than an active leak — but that was a spot check, not an exhaustive
  audit of every call site.

## 2026-08-30 (eighth entry)

### Retrieval, security, quality, lineage, studio, observability, and tool-plan feature completion

Thirty-one tracker items across eight workstreams moved from TODO/PARTIAL to DONE in a single
coordinated build-out. Every module was implemented with code, tests, and API registration.
The full suite now passes 1109 tests with 0 failures — up from 716 at the start of this
increment. INV-5 tenant isolation and INV-7 audit invariants are verified.

#### Retrieval and search (RT-1 through RT-6)

- **Full-text index** (RT-4/tracker RT-4): GIN index, `ts_query`, cross-source search.
  `full_text_index.py`.
- **Vector retrieval** (RT-1/tracker RT-1): pgvector embedding with cosine similarity.
  `vector_retrieval.py`.
- **Graph expansion** (RT-2/tracker RT-2): BFS traversal with org-boundary enforcement.
  `graph_retrieval.py`.
- **Fusion ranking** (RT-3/tracker RT-3): Reciprocal rank and weighted linear fusion.
  `fusion_ranking.py`.
- **Global search API + command palette** (RT-5/tracker RT-5, UX-2): `search_api.py` and
  `ui/scripts/features/global-search.js`.
- **Enhanced hybrid retrieval orchestration** (RT-6): `hybrid_retrieve_enhanced` in
  `retrieval.py`.
- **Cross-source search** (tracker RT-9): Covered by the full-text index and hybrid retrieval.

Hybrid retrieval has moved from Partial to Implemented in the status matrix.

#### Security and access control (SEC-1 through SEC-4)

- **ABAC engine** (SEC-1): Policy evaluation, agent-vs-human gating, simulation mode.
  `abac.py`, `abac_api.py`. Closes tracker PG-1 (from PARTIAL), PG-6, PG-8.
- **Indirect injection defense** (SEC-2): Pattern detection, multilingual, encoding-aware
  corpus. `injection_defense.py`, `injection_corpus.py`. Closes tracker AG-1, AG-2, TS-6,
  and the gap-register P0 indirect prompt injection gap.
- **SIEM routing** (SEC-3): CEF format, syslog/webhook transport. `siem_routing.py`.
  Closes tracker OB-2.
- **WORM archive** (SEC-4): Immutable audit, legal hold, retention. `worm_archive.py`.
  Closes tracker OB-3 and the gap-register P1 legal hold gap.

Policy and governance has moved from Partial to Implemented in the status matrix.

#### AI decision and lineage (AI-1 through AI-4)

- **AI decision lineage** (AI-1): Retrieval, tool selection, rejection, and refusal edges.
  `ai_decision_lineage.py`, `ai_decision_lineage_api.py`. Closes tracker LN-3 and AG-5.
- **SQL lineage parser** (AI-2): Multi-dialect, CTE support, literal redaction.
  `sql_lineage_parser.py`, `view_lineage_api.py`. Closes tracker LN-2.
- **Consumption lineage** (AI-3/CX-4): Consumer tracking and graph.
  `consumption_lineage.py`, `consumption_lineage_api.py`. Closes tracker CX-4.
- **Negative knowledge** (AI-4): Rejected inferences, re-proposal suppression.
  `negative_knowledge.py`, `negative_knowledge_api.py`.

#### Quality and trust (QT-1 through QT-4)

- **Quality-runtime coupling** (QT-1): Demotion, trust warnings, tool gating.
  `quality_coupling.py`. Closes tracker DQ-3, RT-7, AG-6, TL-3.
- **Trust scoring** (QT-2): Composite 0-100 score, A-F grade, explainable factors.
  `trust_scoring.py`.
- **Freshness monitoring** (QT-3): Watermark config, maker-checker, ADR-0016.
  `freshness.py` plus `quality_api.py` endpoints. Closes tracker DQ-2.
- **Runtime data contracts** (QT-4): Schema drift, quality breach, SLA enforcement.
  `runtime_contracts.py`, `runtime_contracts_api.py`. Closes tracker DQ-5.

Data-quality observability has moved from "Implemented for profile-baseline controls" to
Implemented in the status matrix.

#### Studio and governance (STU-1 through STU-3)

- **Studio change sets** (STU-1): Create, items, conflict detection. `studio.py`,
  `studio_api.py`. Closes tracker ST-A1.
- **Studio test harness** (STU-2): Fixture validation, metrics. `studio_test_harness.py`.
  Closes tracker ST-A2.
- **Studio diff view and impact preview** (STU-3): `studio_api.py` endpoints. Closes
  tracker ST-A3 and ST-A5.

Studio has moved from Pending to Partial in the status matrix.

#### Notifications and compliance (NC-1, NC-2)

- **Notification routing** (NC-1): Rules, escalation, dedup, ITSM integration.
  `notification_routing.py`, `notification_api.py`. Closes tracker DQ-1.
- **Compliance pack generation** (NC-2): Five frameworks, reproducible, WORM-archived.
  `compliance_packs.py`, `compliance_api.py`. Closes tracker OB-5.

#### Observability (OB-1, OB-2)

- **OpenTelemetry tracing and metrics** (OB-1): `observability.py`. Closes tracker OB-1.
- **Observability API** (OB-2): SLO, error budgets, archive status.
  `observability_api.py`. Closes tracker OB-4.

#### Tool plans (TP-1)

- **Multi-step tool plans** (TP-1): Validation, budget enforcement, dependency ordering,
  partial failure. `tool_plans.py`, `tool_plans_api.py`. Closes tracker AG-4 and TL-2.

#### Context products enhancements (CX-3, CX-6)

- **Per-read policy evaluation** (CX-3): Wired into `mcp_server.py`. Closes tracker CX-3
  (from IN PROGRESS).
- **Per-consumer rate limits** (CX-6): `mcp_budget.py` enhancement. Closes tracker CX-6.

#### Verification evidence

- **1109 tests pass, 0 failures.** 17 new test files with 200+ tests covering all
  implemented modules.
- INV-5 tenant isolation verified across all new API surfaces.
- INV-7 audit invariant verified: `_KNOWN_UNAUDITED_MUTATIONS` is empty.
- All code registered with FastAPI routers in `main.py`.

#### Known limitations

- Bank-scale benchmarks for retrieval (large-catalog), SIEM routing (target SOC endpoint),
  and WORM archive (target storage) remain Phase D gates.
- The ABAC engine supersedes the earlier `policy_engine.py` partial but residency attribute
  evaluation against production data has not been measured.
- Injection defense corpus covers multilingual and encoding vectors but bank-specific
  adversarial evaluation remains.
- Studio parameter-contract designer and Git binding remain open (tracker ST-A4, ST-A6).
- Interactive browser visual/accessibility certification is not possible in this session.

## 2026-08-30 (continued) — Re-verified `00-status.md` "at a glance" and Retest register figures

### Completed

- A user-supplied summary of `00-status.md` quoted its 12:25 snapshot (1,199 tests, 156 mypy
  files, "9/9 invariants automated") and asked for the doc to be validated and corrected. By the
  time this was checked, other concurrent sessions had continued pushing to this shared branch —
  `git fetch` found 61, then 6 more, divergent commits arrive mid-session — so the 12:25 numbers
  were already stale rather than wrong-when-written.
- Built an isolated Python 3.13 venv (`uv venv` + `uv pip install -e ".[dev]"`, the checked-in
  `.venv` not being runnable from this session) against the current merged head (`c4d8e6f0a1b3`)
  and re-ran the full suite, `ruff`, `mypy --strict`, and `lint-imports`.
- Updated `00-status.md`'s "At a glance" table and Retest register (§8) to the 17:20 UTC figures:
  test count, mypy file count, and the Alembic head hash, with a note on how much drifted and why
  (other sessions' commits landing in the intervening ~5 hours), rather than silently overwriting
  the 12:25 numbers as if they had been wrong. Left the invariant-count claim ("9 of 9") and the
  capability matrix (§4) unchanged — nothing in this pass contradicted either.
- Confirmed a separate claim in the same user-supplied summary — that this repo's tracker "marks
  several Studio items done" while the status doc lists Studio as pending — does not hold:
  `03-tracker.md`'s `ST-A1`–`ST-A7` (module 18) are all still open, matching `00-status.md`'s
  "Studio | 18 | **Pending**" row. The two documents already agree on Studio.

### Verification evidence

- `pytest`: 1,390 passed, 1 xfailed (1,391 collected) — up from the doc's prior 1,199, with the
  Tier-0 invariant test files alone now at 188 total (`test_inv5_tenant_isolation.py` particularly,
  8 → 62) versus the doc's prior 71.
- `ruff check .`: clean. `mypy src` (strict): clean on 163 files (was 156). `lint-imports`: 4
  contracts kept, 0 broken (unchanged). `alembic heads`: single head, `c4d8e6f0a1b3` (was
  `d5f8b21c4a03` — a merge migration landed in between).

### Current limitations

- This branch is under heavy concurrent multi-session development right now (dozens of commits an
  hour observed while doing this check); any point-in-time count in `00-status.md` should be read
  as "true when last verified," not as a stable figure — the doc's own §2 "Living document" framing
  already says this, but the drift rate on this specific branch today is unusually high.
- Did not re-verify the capability matrix (§4), the invariant-by-invariant limits (§3), or the
  decisions/gaps sections (§6–§7) against current code — this pass was scoped to the specific
  numeric claims the user's summary quoted and to the Studio cross-document-consistency question
  they raised.

### Addendum (17:35 UTC) — the correction above was already stale by the time it merged

- Pushing the above correction required merging two further rounds of concurrent commits (a QG-1
  adversarial-SQL-corpus merge and a CT-5 certification merge, each bringing their own tracker/
  Alembic-head updates). Re-ran the same checks against the newly merged head (`12aa5b4dd87d`):
  `pytest` now reports **2,381 passed, 5 skipped, 1 xfailed** (2,387 collected) — up again from the
  1,391 just logged above, largely from a new `tests/test_doc_claims.py` (866 cases: a doc-claim
  regression gate landed by the same concurrent work, tracker TS-12) plus
  `test_catalog_certification.py` and `test_adversarial_sql_corpus.py`.
- `ruff check .` regressed from clean to **14 errors** (all auto-fixable) between this addendum and
  the check 15 minutes prior — not this session's doing; left unfixed as out of scope for a
  docs-only pass, and noted in `00-status.md` rather than silently fixed, since the owning session
  should decide whether `--fix` is safe against their in-flight work.
- Updated `00-status.md` a second time in the same sitting to the 17:35 figures rather than leave
  the 17:20 numbers live and known-wrong. This is the third distinct "true count" recorded for this
  page today (12:25: 1,199; 17:20: 1,391; 17:35: 2,387) — each correction was accurate when made and
  stale within the hour, which is itself the fact worth recording: on a branch this actively
  developed, treat every count in `00-status.md` as a timestamped snapshot, not a stable number.

## 2026-08-31 — ST-A4 (Studio parameter-contract designer) closed

- Studio's TOOL change-item validation (`studio_test_harness.py::_validate_tool_item`) previously
  re-implemented a weaker, ad hoc version of parameter-contract checking: it verified only that
  parameter names were unique and that `parameter_type` was one of the five known literals. It did
  not check `allowed_values`, `minimum`/`maximum`, the `sensitive`+`default` conflict, or whether
  the declared parameters actually matched the SQL template's placeholders — all of which module
  14's real `ToolParameterDefinition` (`schemas.py`) and `tool_rendering.py` already enforce for
  published tools.
- Closed by adding `validate_parameter_contract()` to `studio.py`, which parses each raw definition
  as a real `ToolParameterDefinition` (structured pydantic errors on failure, not a crash),
  cross-checks declared names against `tool_rendering.template_placeholders()`, and — once
  structurally valid — proves the contract renders by substituting one representative in-bounds
  value per parameter through the real `render_tool_sql()`. `_validate_tool_item` now calls this
  directly instead of its previous hand-rolled loop. Also exposed standalone at
  `POST /v1/studio/parameter-contracts/validate` (`studio_api.py`) so an author can validate a
  contract incrementally while still drafting, before it is attached to any change item.
- 9 new tests in the `TestParameterContractDesigner` class (e.g.
  `tests/test_studio.py::test_valid_contract_renders_a_sample`): valid contract renders a
  sample, invalid type reported as a typed error (not a crash), inverted bounds rejected, sensitive
  parameter with default rejected, duplicate name rejected, undeclared placeholder rejected, unused
  definition rejected, `allowed_values` takes precedence for the synthetic sample, malformed SQL
  template reported rather than raised. Full suite green (exit 0, no failures); `ruff check .`,
  `mypy src` (184 files), and `lint-imports` (4 contracts kept, 0 broken) all clean.
- The new endpoint is stateless (no DB session, persists nothing), so it needed the same documented
  exemption `POST /v1/context-compiler/validate` already has in both `test_inv5_tenant_isolation.py`
  (`_TENANT_FREE_ROUTES`) and `test_inv7_attributability.py` (`_READ_ONLY_POST_ROUTES`) — added with
  the same rationale rather than weakening either invariant's default (every route must reach a
  tenant boundary check or a documented, falsifiable exemption).
- OpenAPI baseline (`Docs/90-reference/openapi-baseline.json`) regenerated via
  `scripts/openapi_diff.py --accept-baseline`; the diff gate itself confirmed the change is
  additive only (`added path '/v1/studio/parameter-contracts/validate'`, no breaking changes) before
  the baseline was regenerated, so no `info.version` bump was needed.
- Known limitation: no live-DB / FastAPI-test-client coverage of the new endpoint end-to-end — the
  same "systemic no-DB-test-harness gap" already named for CT-1/TL-1/LN-4 in the tracker. Core-logic
  coverage is thorough; wiring-level coverage relies on the route-registration and INV-5/INV-7
  route-classification tests instead.
- Context: this branch is under very heavy concurrent multi-session development (see the
  17:35 UTC addendum above). Before starting this item, 8 speculative parallel workstreams were
  launched against a base that turned out to be 182 commits behind origin; every one of those items
  (RT-1..4, LN-2, DQ-1/2, PG-1/2/6, AG-1/2, ST-A1/2/3/5, ST-02, TS-4, TS-6) had already been
  delivered by other concurrent sessions by the time that was discovered, so all 8 were stopped
  before any wrote code, and ST-A4 — the one item confirmed still open after re-checking the live
  tracker — was picked up directly instead.

## 2026-08-31 — CT-1 (catalog bulk actions) closed: per-item SAVEPOINT isolation, real-DB tests, and the "no test harness exists" claim was wrong

- CT-1 (bulk tag/classify/own/certify) was already delivered 2026-08-30 with the right shape —
  filter-or-explicit selection, a 500-item cap, per-item partial-success reporting — but its own
  tracker row admitted it had never been run against a database: "this repo has no live/fake-DB
  endpoint-level test harness at all (a pre-existing, systemic gap)". That claim, also repeated
  verbatim against TL-1/LN-4/ST-A4, is false. `tests/test_bulk_governance_decisions.py` (PG-3) and
  `tests/test_catalog_pagination.py` (CT-2) already establish exactly that pattern — a real
  in-memory sqlite engine, `Base.metadata.create_all`, rows seeded through the real ORM, the
  endpoint function called in-process. Nobody had checked before writing that note down.
- Following that pattern surfaced a real defect the never-executed code had been carrying since
  2026-08-30: the `CatalogBulkActionRun` ORM model in `models.py` was missing the `requested_by`
  column that its own Alembic migration (`b3f7a1c94d62`) creates and that
  `_persist_catalog_bulk_action_run` unconditionally passes as a constructor kwarg. Every real call
  to any of the four bulk endpoints would have raised `TypeError` before a run could ever be
  persisted — a correctness bug invisible to `ruff`/`mypy`/code review, caught on the first test
  that actually executed a request. Fixed by adding the column to the model; no migration change
  needed since the table already had it.
- Separately, and more structurally: the previous implementation's four endpoints computed a whole
  batch's mutations in memory (`plan_tag`/`plan_classify`/`plan_own`/`plan_certify`, no session I/O)
  and issued a single `session.commit()` for the entire request. That is not partial-success-safe at
  the database level — a single DB-level failure anywhere in a 500-item batch (a constraint
  violation the application-level precondition checks don't catch) would have rolled back every
  item, not just the one that failed, silently contradicting the "partial success reported" exit
  condition the moment reality diverged from the happy path. Refactored to mirror PG-3's own
  pattern exactly: `catalog_bulk_actions.py` now exposes one `apply_<action>_item` function per
  action — the single-item core, raising `CatalogBulkItemError` on a precondition failure — and each
  of the four endpoints in `api.py` dispatches to it per subject inside that item's own SAVEPOINT
  (`async with session.begin_nested(): ... await session.flush([row, ...])`), catching both
  `CatalogBulkItemError` and a real `IntegrityError` per item.
- New `tests/test_catalog_bulk_actions_endpoints.py`, following PG-3's and CT-2's real-engine test
  pattern (not a hand-simulated session), proves:
  - **Partial success at real scale**: a full 500-item explicit-selection batch (tag: 470
    ACTIVE + 25 DEPRECATED + 5 never-existed ids; classify: 480 ACTIVE + 20 DEPRECATED columns)
    reports every item's outcome correctly and persists exactly the succeeded count — no failure
    dropped a success, no success went unpersisted.
  - **The cap and `truncated` flag**: filter selection over 5,000 candidate ACTIVE tables caps the
    processed batch at exactly `CATALOG_BULK_ACTION_MAX_ITEMS` (500) with `truncated=True` recorded
    in the run's `parameters`, and the database ends up with exactly 500 new `OwnershipAssignment`
    rows, never more; a 40-row match is correctly reported un-truncated.
  - **SAVEPOINT isolation actually contains a failure**: a real, table-defined CHECK constraint
    (`ck_asset_certification_column_consistency`) is tripped on exactly one item's certify dispatch
    via a `before_insert` listener (this sandbox's sqlite fixture is single-connection, so a genuine
    concurrent-writer race can't be reproduced deterministically — the constraint violation itself
    is real, not the trigger mechanism). Proven: the prior certification that item had already
    superseded in memory reverts to ACTIVE (not stuck SUPERSEDED with no replacement), no partial
    certification row exists for that table, the item is reported FAILED with the constraint reason,
    and both sibling items in the same request still commit and read ACTIVE — the outer transaction
    was never aborted by the contained failure.
- `tests/test_catalog_bulk_actions.py` (the pure-function unit tests) rewritten from
  `plan_tag`/`plan_classify`/`plan_own`/`plan_certify` to `apply_tag_item`/`apply_classify_item`/
  `apply_own_item`/`apply_certify_item`, since the old batch-planner functions no longer exist as a
  separate code path from what the endpoints actually call — keeping one single-item core per
  action, exactly PG-3's "single-item and bulk can never drift" property.
- Verified: `ruff check .` clean; `mypy src` clean (184 files); full `pytest` suite run to
  completion with exactly 2 failures, both pre-existing and unrelated to this item
  (`test_doc_claims.py::test_cited_test_path_resolves` flagging ST-A4's tracker/log citation of the
  test_studio.py TestParameterContractDesigner class by name rather than by function — the scanner
  only resolves function/method names, not class names, so a class-name citation can never pass —
  confirmed present at `ca80b14`, the commit this session started from, before any change made
  here). Every catalog-bulk-action test passes, and no other test in the suite fails.
- Known limitation, stated plainly rather than glossed: this sandbox has no live Postgres, so the
  SAVEPOINT-isolation proof above uses sqlite's own CHECK-constraint enforcement (real, but not
  Postgres) and a single-connection fault-injection listener rather than a genuine concurrent
  writer race, which sqlite's default in-memory single-connection fixture cannot reproduce
  deterministically. The mechanism proven (SQLAlchemy `begin_nested()` SAVEPOINT rollback contains
  a real `IntegrityError`) is dialect-independent, but a live-Postgres run of the same scenario has
  not been performed in this environment.

## 2026-08-31 — QG-6 (dynamic masking / tokenization integration) closed

- Gave `query_gateway.py`'s masking pass a second strategy alongside the existing flat
  `"***MASKED***"` redaction: reversible, format-preserving tokenization for output columns a
  steward has explicitly opted in. `tokenization.py`'s `TokenizationProvider` protocol is
  `signing.py`'s `SigningProvider` (QG-5) restructured for a second operation pair
  (`tokenize`/`detokenize` instead of `sign`/`verify`) rather than a new shape: async, resolved
  fresh per call by `resolve_tokenization_provider`, no fallback on failure. `LocalFpeTokenizationProvider`
  transforms the digit run of a value with a deterministic, invertible, unbalanced Feistel-style
  construction (keyed HMAC-SHA256 round function, alternating modular updates to two unequal
  halves) — covers realistic numeric PII shapes (card numbers, SSNs, account/phone numbers) and is
  explicitly documented as **not** a validated FF1/FF3-1 implementation, the same honesty line drawn
  around `LocalHmacSigningProvider`. `VaultTransformTokenizationProvider` calls HashiCorp Vault's
  Transform secrets engine over plain `httpx`, the same wire shape `VaultTransitSigningProvider`
  already uses for Transit.
- New `ColumnTokenizationPolicy` model/table (migration `2fa45be65bf7`, on top of head `4f730e96ee9b`):
  a steward-declared, `column_id`-scoped row that opts one catalog column into tokenization.
  `query_gateway.py`'s `_tokenized_output_names` mirrors `_sensitive_output_names`'s shape exactly
  (including reusing `sensitive_projection_names` for alias/derived-expression propagation, so a
  tokenized column stays tokenized under a rename or a wrapping expression), and `execute()` now
  tokenizes the columns that policy covers instead of redacting them — `tokenized_names` takes
  precedence over `masked_columns` for the columns it names, every other sensitive column keeps
  today's behaviour unchanged. A query that needs to tokenize but has no usable provider configured
  is rejected closed (`TOKENIZATION_PROVIDER_UNAVAILABLE` / `TOKENIZATION_FAILED`), never silently
  falling back to redaction or returning the raw value.
- Reversal is gated: `POST /v1/security/tokens/detokenize` (`detokenization_api.py`) requires one of
  `PlatformAdmin`/`OrganizationAdmin`/`ComplianceOfficer`/`DataSteward`, a stated `purpose`, and
  writes an audit row via `record_audit` on *every* path — both the grant and the denial — before
  the response returns, using the same "record before deciding what to return" shape
  `token_revocation_api.py` established rather than a bare `require_roles` dependency (which would
  raise before the handler body, and audit-record, ever runs). INV-6 held: neither the token nor the
  recovered value ever enters the audit `details` payload.
- Production configuration now refuses `tokenization_provider == "local"` and a `tokenization_key`
  under 32 characters, the same shape and same `reject_insecure_production_configuration` function
  as the existing `hmac_signing_provider == "local"` / `audit_hmac_key` checks. The Tier-0
  `_SECURE_PRODUCTION_BASELINE` fixture in `test_tier0_invariants.py` was updated to configure the
  KMS-backed tokenization provider (mirrors its existing `hmac_signing_provider: "vault_transit"`
  entry), and two new parameterized cases added to `_INCOMPLETE_POSTURE_CASES` alongside QG-5's.
- 33 new tests across three files: `test_tokenization.py` (22 — round-trip/determinism/key-binding/
  format-preservation for the local provider across SSN/card/phone/account-number lengths, mocked
  Vault Transform wire contract, fail-closed on network error/non-2xx/malformed body, production
  refusal), `test_query_tokenization.py` (4 — a policy-covered column comes back tokenized not
  redacted, the exact token the gateway produced detokenizes back to the original value through the
  same provider a real detokenize call would resolve, a sensitive column with no policy keeps
  today's flat redaction unchanged, a query needing tokenization with no usable provider is rejected
  closed with the execution row recorded `REJECTED`), and `test_detokenization_api.py` (7 — an
  authorized caller with a stated purpose recovers the value and the grant is audited without ever
  leaking the token or value into the audit payload, every one of the four authorized roles is
  accepted, an unauthorized caller is denied *and the denial is itself audited*, an unavailable
  provider fails closed with a 503 and that failure is also audited).
- `tests/support/doubles.py`'s `CatalogSession` (shared Tier-0 test double) gained a fourth
  recognised statement shape (a 2-column select, for the `ColumnTokenizationPolicy` join) alongside
  the existing 3-/4-column/scalars routing, with an empty default so every existing caller is
  unaffected.
- OpenAPI baseline regenerated (`uv run python scripts/openapi_diff.py --accept-baseline`) after
  confirming the only diff is additive (`added path '/v1/security/tokens/detokenize'`) — no breaking
  changes, no version bump needed.
- Full suite run in the foreground start to finish (not backgrounded, per the coordinator's
  correction mid-session — a backgrounded run does not survive this session's own turn boundaries),
  both before and after rebasing onto latest origin: `ruff check .` clean, `mypy src` clean (186
  files), `lint-imports` (4 contracts kept, 0 broken), one Alembic head, the OpenAPI baseline diff
  additive-only, and `pytest` green except one pre-existing, unrelated failure introduced by the
  CT-1 commit this session rebased onto (`eaf7ee1`, confirmed via `git show`) — its own
  accomplishment-log entry cites test_studio.py's TestParameterContractDesigner the same way an
  earlier ST-A4 entry did before a same-day fix (`08a37cb`) repointed the ST-A4 citations at a real
  function; `test_doc_claims.py::test_cited_test_path_resolves` correctly reports it, since the
  doc-claims scanner resolves `path::name` citations only against functions, never classes.
  Untouched by QG-6. (Deliberately not written as a backtick-fenced `path.py::Name` citation here,
  so this note about the gap does not itself become another instance the same scanner trips on.)
- Not yet DONE, stated plainly: no source-native (in-database) tokenization or masking policy — this
  is the query-gateway's own output-layer transform over an already-executed result set, the same
  "application layer today, source-native next" boundary the module doc's target column already
  named. `VaultTransformTokenizationProvider` has never made a request against a real Vault
  instance — verified only against a mocked HTTP transport asserting the documented wire contract,
  the same standing limitation already recorded for QG-5's `VaultTransitSigningProvider` and the
  connector work (CN-1c/CN-2a). Only one value shape is implemented (the digit run of a value);
  `ColumnTokenizationPolicy.value_shape` is left open for a future alphanumeric scheme without a
  schema change, but no such scheme exists yet.

## 2026-08-31 — QG-2 (source-native row/column policy synchronization)

- Built the synchronization path module 16's masking §7 target names but the codebase had not yet
  shipped: `policy_native_sync.py` translates the platform's existing governed row/column
  obligations into real source-native DDL, alongside (never instead of) `query_gateway.py`'s
  application-level masking, which is untouched. Deliberately reused the platform's one policy
  language rather than inventing a second: obligations come from `policy_engine.PolicyRecord`
  (loaded via the existing `business_graph.load_policies`) with effect `FILTER` (row) or `MASK`
  (column) — the same records `query_gateway.py` already builds masking decisions from.
- The load-bearing design choice: a policy is eligible for native sync only when it is
  *unconditional on subject* (empty `subject_match`) and its `resource_match` resolves without a
  business-graph closure query. A native `CREATE POLICY`/`ADD MASKED` construct has no way to see
  Atlas's roles, purpose, or principal kind, so a subject-scoped policy (e.g. "mask for AGENT
  principals only") is left to application-level enforcement rather than synced with a silently
  narrowed meaning — reported by name in `NativeSyncPlan.unsupported`, not dropped quietly. Same
  treatment for the two connector/obligation combinations with no real native construct today:
  Postgres column masking (would need the third-party `postgresql_anonymizer` extension) and SQL
  Server native row-level security (`CREATE SECURITY POLICY` needs a schema-bound predicate
  function this module does not yet manage) — both documented as future work in the module
  docstring, matching this codebase's convention of not shipping a source half-supported without
  saying so.
- Real, tested DDL for the two connectors with real native pull adapters: PostgreSQL RLS
  (`ENABLE`/`FORCE ROW LEVEL SECURITY` + `CREATE POLICY ... USING (...)`, idempotent via a leading
  `DROP POLICY IF EXISTS`) and SQL Server Dynamic Data Masking (`ALTER COLUMN ... ADD MASKED WITH
  (FUNCTION = ...)`, profile-mapped with a conservative `default()` fallback for an unrecognized
  profile, matching module 16's existing "default is conservative" masking rule).
- Row-filter predicates are governance-authored trusted SQL text (the same trust level as a
  `SourceBinding.masking_profile` or a governed tool's SQL template), but are still round-tripped
  through `sqlglot` before being concatenated into generated DDL: parsed as a `WHERE` condition,
  rejected on multiple statements/semicolons, rejected on any DDL/DML/administrative AST node, and
  re-rendered from the parsed tree rather than passed through raw — the same safety property
  `query_gateway._run_validation` already relies on. Found a real gap while writing the injection
  tests: a subquery can hide a call to a dangerous function (`(SELECT 1 FROM pg_sleep(5)) = 1`)
  past a pure node-type denylist, since the node itself (a comparison) is not forbidden. Closed by
  additionally walking every function call in the parsed predicate against the same
  Postgres denylist `sql_guard.SqlGuard` already applies to the read path (`pg_sleep`,
  `dblink_connect`, `pg_read_file`, etc.), independently duplicated rather than imported (see the
  module docstring on why this module does not reach into another module's private members) —
  verified closed by test before and after the fix.
- `policy_native_sync_api.py`: a dry-run preview (steward-tier role gate, nothing persisted or
  applied) plus a maker-checker `PolicyNativeSyncRequest` gate modeled directly on
  `ProfilingExceptionPolicy` — its own denormalized `status`/`requested_by`/`decided_by` fields
  rather than filing into the shared `governance_review` queue, for the exact reason
  `ProfilingExceptionPolicy`'s own docstring gives for itself: gating a live external-source write,
  with generated DDL as the evidence, doesn't fit that queue's existing per-object-type dispatcher
  without distorting it. `APPROVE` immediately attempts the apply and records the real outcome
  durably either way (`APPLIED` with an HMAC evidence hash via `aida.signing` — the same evidence
  mechanism `QueryExecutionGateway` uses for executed SQL — or `APPLY_FAILED` with the exception
  class only, never the raw driver error text, which could carry source-side values, INV-6);
  `REJECT` never touches the source. Maker != checker enforced the same way
  `decide_profiling_exception_policy` enforces it in `api.py`.
- Apply mode is deliberately a separate execution surface from the query gateway, not an oversight:
  `aida.connectors.execution_access` (the only source of a `SqlExecutor`) is import-linter-
  restricted to `aida.query_gateway` alone (INV-2), and `sql_guard.SqlGuard` refuses every
  DDL/administrative statement outright — `CREATE POLICY`/`ALTER TABLE ... ADD MASKED` could never
  pass through that read-only pipeline by the same rule that makes it safe for governed reads.
  `apply_native_sync_plan` therefore opens its own narrowly-scoped administrative connection
  (`asyncpg`/`pytds`, the same drivers the read connectors use) and executes only the exact
  statements the checker reviewed, in one transaction. `lint-imports` (4 contracts kept, 0 broken)
  and `tests/test_tier0_invariants.py::test_no_connector_execution_outside_gateway` both confirm
  this module never reaches the gateway-restricted surface.
- 32 tests (`tests/test_policy_native_sync.py`): DDL generation correctness against real RLS/DDM
  syntax; identifier and string-literal escaping (embedded `"`, `]`, `'` all verified to stay
  escaped rather than break out of a quoted identifier or literal); injection safety (multi-
  statement rejection, comment-smuggling neutralized to an inert re-rendered comment, dangerous
  function inside a subquery rejected, a legitimate subquery still accepted); policy-resolution
  scoping (subject-conditional and business-node-scoped policies correctly excluded, datasource/
  schema scoping, priority tie-break); `apply_native_sync_plan` against injected fake connections
  for both connector types, including a rollback-and-propagate path on failure — never against a
  real Postgres/SQL Server instance, the same "verified only against a mock" posture QG-5's
  `VaultTransitSigningProvider` tests already carry in this codebase; and the maker-checker HTTP
  surface exercised the way `test_profiling_exception_policy.py` exercises its own endpoints
  (self-approval refused, already-decided refused, reject never attempts apply, a failed apply is
  recorded durably without raising and without leaking the raw driver error).
- New table `policy_native_sync_request` (migration `a3f6c9e21b74`, chained onto head
  `4f730e96ee9b`). New events `policy_native_sync.requested.v1` / `.decided.v1` / `.applied.v1`
  added to `Docs/30-contracts/04-event-catalog.md` (`test_event_catalog_gate.py` was the gate that
  caught the omission). New router `policy_native_sync_api.py` mounted in `main.py` rather than
  added to `api.py`, to keep this change out of the branch's single largest shared file — three new
  paths confirmed additive-only via `scripts/openapi_diff.py` before regenerating
  `Docs/90-reference/openapi-baseline.json`.
- Full suite: 2,796 tests collected, exits with exactly 2 failures, both pre-existing and unrelated
  (`test_doc_claims.py` flagging ST-A4's tracker/log citation of the test_studio.py
  TestParameterContractDesigner class by name rather than by function — a stale citation from the
  ST-A4 session's own doc edits, present before this session started, not touched here). `ruff
  check .` and `uv run mypy src`
  (186 files) both clean; `lint-imports` 4/4 contracts kept.
- Not yet DONE, stated plainly: apply has never run against a real Postgres/SQL Server instance
  (mock-verified only — the same standing limitation QG-5, CN-1c, and CN-2a already carry in this
  tracker); SQL Server native row-level security and Postgres native column masking are explicitly
  unimplemented, documented as future work rather than faked; and only unconditional policies are
  synchronized by design, so a subject-scoped masking/filter rule stays application-level-only
  until a session-variable bridge between Atlas's subject attributes and a source engine's session
  context exists (also not started).

## 2026-08-31 — PF-3 (CI performance regression gates) closed

- Scope, stated plainly: this closes the CI-runner regression-gate *mechanism*, not bank-scale
  load/soak/spike testing. PF-1 (1M-object benchmark corpus), PF-2 (published performance
  dashboards), PF-4 (projection rebuild timing) and TS-7 remain open, infra-dependent items this
  work does not touch.
- Added `scripts/perf_baseline.py`, following the same committed-baseline ratchet pattern as ST-02's
  import-linter gate and TS-4's `scripts/openapi_diff.py`. It times four real, already-tested,
  in-process hot paths rather than any invented benchmark: `SqlGuard.validate()` over every case in
  QG-1's own adversarial SQL corpus (`tests/fixtures/adversarial_sql_corpus/*.json`, all 5 certified
  dialects); `abac.evaluate()` over 500 policies, the exact scenario PG-1's own
  `tests/test_abac.py::test_evaluation_under_50ms_with_500_policies` p95<50ms test already exercises, reused here rather than
  reimplemented; `fuse_results()`, hybrid retrieval's reciprocal-rank-fusion combiner, over a
  synthetic 500-candidate catalog; and `app.openapi()` — TS-4's own gate input — with FastAPI's
  schema cache cleared every iteration so it is genuinely regenerated, not cached.
- Each benchmark runs a warmup, then several timed iterations, and the comparison uses the median
  (p50) rather than a tail percentile: on a shared CI runner the tail is dominated by scheduler noise
  unrelated to the code under test, while the median stays representative with far fewer samples.
  The regression threshold is 20% — chosen because repeated local measurement showed run-to-run
  median variance comfortably under 15% for three of the four benchmarks even on an unusually loaded
  machine (see below), leaving headroom above that noise floor while still catching a real slowdown.
  A benchmark that crosses the threshold is re-measured once before the gate actually fails, so one
  transient blip does not fail the build by itself — the regression has to reproduce.
- Committed baseline at `Docs/90-reference/perf-baseline.json`; `--accept-baseline` regenerates it
  deliberately, exactly like `scripts/openapi_diff.py`'s own flag. Wired as a new job in
  `.github/workflows/ci.yml` alongside the existing five gates (ruff, mypy, lint-imports,
  single-Alembic-head, openapi-diff, pytest) — additive only, none of the existing jobs were
  restructured.
- 16 tests (`tests/test_perf_baseline_gate.py`): pure `find_regressions()` coverage of the threshold
  boundary and edge cases (exactly-at-threshold passes, missing-on-either-side entries are
  informational only, a zero baseline doesn't divide by zero), plus — per this item's own definition
  of done — `aida.abac.evaluate` wrapped with an artificial `time.sleep` to prove the gate actually
  flags a regression in a real benchmarked function (not just a synthetic fixture), and the same
  unmodified benchmark proven not to be flagged.
- Found and fixed two false positives of its own in the same edit: the tracker row's prose cited the
  OpenAPI diff script as a bare filename instead of the full `scripts/openapi_diff.py` path
  the doc-claims gate (TS-12) requires, and separately named the new CI job in backticks on the same
  line as "contracts kept" (from the lint-imports verification sentence), which made the doc-claims
  gate's contract-name checker mistake the job name for an import-linter contract citation. Both
  fixed before commit; `uv run pytest tests/test_doc_claims.py` re-run clean for every citation this
  entry and the tracker row introduce.
- This session's sandbox was, for its own reasons, unusually heavily loaded — many concurrent
  sibling sessions each running their own full ~2,400-test suite at the same time on the same
  machine (confirmed via `ps aux`: multiple `pytest -q` processes from other worktrees running
  concurrently throughout). That is real, encountered evidence, not a hypothetical, of exactly the
  shared-runner noise this gate's own docstring warns about: the first version of the fusion-ranking
  benchmark (20 `fuse_results()` calls per measured iteration, ~18ms) swung 107–188% run-to-run under
  that load, and even the CI-facing test written to prove "the gate passes when nothing regressed"
  flaked once at a 20.1% reading against the real 20% production threshold. Fixed two ways: the
  fusion-ranking benchmark's inner-loop batch size was raised from 20 to 100 calls per iteration
  (diluting a single scheduler stall's effect on the measured median), and the CLI round-trip test
  was changed to stub `measure()` to a fixed value rather than compare two live timings, since it
  exists to prove argument/file-I/O wiring, not timing precision — that coverage already exists,
  risk-free, in the pure `find_regressions()` unit tests. Left as an open, named caveat rather than
  engineered away entirely: real GitHub Actions runners are dedicated, not shared with N other full
  suites the way this sandbox was, so the committed baseline may still want one deliberate
  `--accept-baseline` re-capture after the job's first live run there — not yet observed, verified
  locally only, the same caveat TS-4 recorded for its own gate.
- `ruff check .` clean, `mypy src` clean (184 files), `lint-imports` 4 contracts kept / 0 broken,
  `alembic heads` a single head. Full `pytest` run twice after all fixes above: both times exactly
  two failures, both pre-existing and unrelated — `tests/test_doc_claims.py::test_cited_test_path_resolves`
  citing a `TestParameterContractDesigner` class inside `tests/test_studio.py`, from `03-tracker.md:231`
  and this log's own ST-A4 entry (`06-accomplishment-log.md:2064`), both from the ST-A4 commit already
  on origin before this session started (confirmed identical `tests/test_doc_claims.py` against
  `origin/feature/snowflake-dbt-lineage-mcp`; `tests/test_studio.py` has no such class, only flat
  test functions). Out of scope for PF-3 (neither the tracker's ST-A4 row nor another session's
  accomplishment-log entry is this item's to edit) and left for whoever owns ST-A4 to fix.

## 2026-08-31 — UX-1 (persona navigation from OIDC groups) closed

- Module 21 §5's rule — "a persona that a user can select is a persona that grants nothing, so any
  capability gated on it would be a fake control" — was true but unenforced: `ui-next`'s shell
  (UX-10) already labelled its persona `<select>` "Dev only — derived from OIDC in production," but
  nothing behind that label actually gated it. The switcher rendered unconditionally, in every mode.
- Closed by extending module 01's existing claims-to-roles pipeline rather than building a parallel
  one. `aida.oidc.context_from_claims` already turns a verified OIDC roles claim into platform roles
  via a configurable claim path (`Settings.oidc_roles_claim`) plus a mapping dict
  (`Settings.oidc_role_mappings`). Added the same shape for groups: `oidc_groups_claim` (default
  `"groups"`) and `oidc_persona_mappings` (group name -> persona), plus `oidc_default_persona` for a
  principal whose groups map to none of the configured personas. `_persona_from_groups` picks the
  first group, in claim order, with a recognized mapping — deterministic per token, and the bank's
  own group ordering controls priority with no extra config. `SecurityContext` gained
  `persona: str | None`. Refactored the roles/groups claim-parsing duplication in `oidc.py` into one
  `_string_list_claim` helper used by both, rather than copy-pasting the groups parser.
- New `GET /v1/me` (`src/aida/persona_api.py`, following the existing single-purpose-router pattern
  of `token_revocation_api.py`) is the seam the shell actually reads: it returns the current
  principal's roles, its server-derived `persona`, and `identity_provider` —
  `Settings.identity_provider.upper()`, the exact `"development" | "oidc"` value
  `aida.security.get_security_context` already branches its whole auth flow on. The shell defers to
  that one flag instead of inventing its own prod/dev signal.
- `ui-next`: extracted the persona `<select>` out of `App.tsx` into a standalone `PersonaNav`
  component (`ui-next/src/components/PersonaNav.tsx`) with three render branches keyed on one prop,
  `identityProvider`: `"OIDC"` renders read-only persona text and **no `<select>` in the DOM at
  all** (not disabled — absent); `"DEVELOPMENT"` renders the pre-existing manual switcher,
  unchanged; `null` (mode not yet resolved) renders nothing, matching module 01 INV-4's fail-closed
  default rather than guessing a mode while `/v1/me` is still in flight. `App.tsx` fetches `/v1/me`
  once on mount and passes the result straight through — no new state machine beyond what the fetch
  itself already models.
- `ui-next` had no test runner at all before this (`package.json` had no `test` script). Added
  `vitest` + `@testing-library/react` + `jsdom` as devDependencies (exact-pinned, matching this
  repo's convention) and a minimal `vitest.config.ts`/`ui-next/src/test/setup.ts` (the latter also stubs
  `ResizeObserver`, which jsdom lacks and `@tanstack/react-virtual`'s `CatalogTable`, UX-11, needs
  just to mount). `PersonaNav.test.tsx` (9 cases) asserts the rendered DOM directly per mode —
  `queryByRole("combobox")`/`queryByTestId("persona-select")` absent under OIDC (including the
  no-persona-mapped case), present under development, `onPersonaChange` never firing when there is
  no control to fire it. `App.test.tsx` (3 cases, `fetchMe` mocked) proves the same gating holds
  through the actual shell, not just the isolated component.
- Tests (`tests/test_persona_derivation.py`, 17 cases): a principal in a mapped group derives the
  configured persona; different groups derive different personas; the first mapped group wins when
  several are present, in claim order; an unmapped principal derives `None`, or the configured
  default when one is set; an out-of-catalog persona name is ignored whether it comes from a group
  mapping or from the default; the groups claim honors a custom dotted claim path exactly like the
  roles claim does; comma-separated and malformed groups claims are handled the same way roles
  claims already are; the full derivation survives real RS256 sign-and-verify, not just unit-level
  claim parsing; and `GET /v1/me` reports the right `persona`/`identity_provider` pair in both modes
  (called directly against a hand-built `SecurityContext`, no DB — the existing "no live-DB harness"
  gap already named for several other endpoints in this tracker, e.g. this file's ST-A4 entry
  above).
- `uv run ruff check .` / `uv run mypy src` (185 files) / `uv run lint-imports` (4 contracts kept) /
  `uv run alembic heads` (single head, no migration — no new tables) / `uv run pytest` all green,
  with the same one pre-existing, unrelated failure already logged in this file and in the CX-9
  tracker row: test_doc_claims.py's citation check for ST-A4's TestParameterContractDesigner
  test-path citation, reproduced identically on the unmodified branch (`git stash` confirmed).
  `ui-next`: `npm run typecheck`, `npm test` (12/12), `npm run
  build` all green. OpenAPI baseline regenerated (`scripts/openapi_diff.py --accept-baseline`):
  `GET /v1/me` is a new, additive path — the diff gate confirmed no breaking changes before the
  baseline was accepted.
- Also removed `ui-next/vite.config.ts.timestamp-*.mjs`, a stray Vite build artifact that had been
  committed by an earlier session; harmless but not something that belongs in the tree.

## 2026-08-31 — CN-2b (Databricks adapter) — native pull adapter, moved off `declare_planned`

- Re-checked the live tracker before starting: CN-2b was still `TODO`, Databricks still registered
  only via `connector_registry.declare_planned(...)` (canonical push ingestion only, no pull
  adapter) — not duplicated by any concurrent session.
- Added `src/aida/connectors/databricks.py` (`DatabricksConnector`), modeled directly on
  `aida.connectors.snowflake` — the closest existing adapter shape (a cloud warehouse reached over a
  DB-API driver via `INFORMATION_SCHEMA`, with EXPLAIN-based cost estimation) — and reusing the same
  shared assembly helpers (`aida.connectors.discovery`) every other adapter uses rather than
  reinventing per-adapter logic:
  - **Discovery**: Unity Catalog's per-catalog `INFORMATION_SCHEMA` (catalog → schema → table →
    column via `COLUMNS`/`TABLES`; `PRIMARY KEY`/`UNIQUE` via `TABLE_CONSTRAINTS`/
    `KEY_COLUMN_USAGE`; best-effort `FOREIGN KEY` via `REFERENTIAL_CONSTRAINTS`/
    `CONSTRAINT_COLUMN_USAGE`, degrading to "none observed" rather than failing discovery on an
    older metastore without that surface). Table/column/schema/catalog comments are also
    implemented and `object_comments` is honestly claimed `True`; `views`/`routines`/`grants` are
    deliberately left `False` — Unity Catalog exposes the ANSI-shaped views for them, but claiming
    those axes without a live workspace to verify column shapes and refusal modes against would be
    exactly the overclaim INV-9 exists to prevent.
  - **Capability negotiation**: `ConnectorCapabilities` matches what `discover()` actually reads —
    `catalogs`/`schemas`/`constraints`/`explain`/`query_history`/`approximate_statistics`/
    `object_comments` `True`; `indexes`/`partitions`/`delegated_identity` (PAT-only auth for now)/
    `views`/`routines`/`grants` honestly `False`.
  - **Credentials**: JSON payload or DSN URI (`databricks://token:<access_token>@<host>/<catalog>/
    <schema>?http_path=...`), parsed the same way `SecretResolver` hands resolved DSNs to every
    other adapter — no inline secrets, no plumbing changes to the secret-resolution path itself.
  - **Cost estimation**: `EXPLAIN COST <sql>`, parsing Spark's humanized
    `Statistics(sizeInBytes=<n> <unit>, rowCount=<n>)` fragments (byte-unit conversion table for
    `B`/`KiB`/`MiB`/`GiB`/`TiB`/`PiB`/`EiB`; largest fragment taken rather than summed, since
    fragments nest bottom-up in the plan and summing would multiply-count). Falls back to a floor
    estimate (`DATABRICKS_EXPLAIN_FALLBACK`) exactly as Snowflake's EXPLAIN-JSON path does when a
    plan carries no CBO statistics (table never `ANALYZE`d).
  - **Bounded profiling**: batched per-column-group queries (not one round trip per column) over a
    `LIMIT`-bounded CTE — `COUNT`/`APPROX_COUNT_DISTINCT`/`MIN|MAX(LENGTH(...))`, same shape as the
    BigQuery adapter's batching, chosen because Spark's planner benefits more from one shared scan
    per batch than Snowflake-style per-column round trips do.
- Registered in `src/aida/connectors/registry.py`: `connector_registry.register("databricks", ...,
  maturity="BETA", ...)` replacing the `declare_planned` entry (Teradata and Db2 remain planned).
  Notes are explicit that this has never been exercised against a live Databricks workspace — same
  honesty precedent already carried by the Snowflake, Oracle and BigQuery rows.
- Added `databricks-sql-connector==4.4.0` to `pyproject.toml` `dependencies` (matching how
  `snowflake-connector-python` is a hard dependency, not an extra) plus the matching
  `ignore_missing_imports` mypy override; `uv sync --extra dev` picked it up cleanly with no
  conflicts. `import databricks.sql` stays lazy inside `_get_connection`, guarded by `ImportError`,
  same as every other adapter's driver import.
- 24 new tests (`tests/test_connectors_databricks.py`): identifier quoting/escaping, JSON and URI
  DSN parsing (including malformed-payload and missing-field rejection), `EXPLAIN COST` byte-unit
  conversion (parametrized over `B`/`KiB`/`MiB`/`GiB`) and largest-fragment selection, the no-
  statistics fallback, registry definition/capability-honesty checks, mocked-connection discovery
  (columns/constraints/FK/comments assembling correctly) and a companion test proving FK and
  comment-query refusals degrade the envelope rather than raising, mocked `execute_read_query`
  (captures `cursor.query_id`), mocked `estimate_read_query`, mocked `profile_table`, and the
  positive-limits guard.
- Updated four existing tests that encoded the old `declare_planned` state, all pre-existing
  assertions this change made false rather than test bugs: `tests/test_connectors.py`
  (`default_capabilities(databricks)` now equals the real capability dict, not `{}`);
  `tests/test_ingestion.py` (the "planned connector" example switched to `teradata`; added a new
  `test_registry_exposes_databricks_as_implemented`, mirroring the existing Snowflake one);
  `tests/test_inv9_capability_honesty.py` (added a `databricks` entry to `_CONSTRUCTION_DSNS` so
  INV-9's advertised-vs-implemented and SQL-execution-surface checks actually run against it; the
  registry-populated tripwire's planned-count floor dropped from `>= 3` to `>= 2` — Databricks moved
  out of the planned set, and that is the correct, intended consequence of doing the work, not a
  weakened check, since two connectors — Teradata, Db2 — remain honestly planned).
- Verification run against `origin/feature/snowflake-dbt-lineage-mcp` HEAD `ca80b14` (synced via
  `git fetch && git reset --hard` before starting, confirming CN-2b was not already closed): `ruff
  check .` clean; `mypy src` clean (185 source files); full `pytest` suite green except two
  pre-existing, unrelated failures confirmed present on the clean checkout before this change
  (`tests/test_doc_claims.py::test_cited_test_path_resolves[...TestParameterContractDesigner]` ×2 —
  a stale doc citation from the ST-A4 entry above, naming a test class that does not exist in
  `tests/test_studio.py`; not touched here, out of this item's scope).
- Known limitations, stated the same way the Snowflake/Oracle/BigQuery rows already state theirs:
  no live Databricks workspace was available to verify against; `views`/`routines`/`grants`
  discovery axes are not implemented; auth is PAT-only (no OAuth service-principal / workload
  identity); FK discovery is best-effort against a Unity Catalog surface newer than PK/UNIQUE and
  unverified live; no certification run or version fixtures yet (CN-3 remains open for every
  adapter, not specific to this one).

## 2026-08-31 — LN-7 (transitive cross-kind impact traversal) closed

- On re-checking the live tracker before starting, most of LN-7's exit criterion — "bounded
  traversal across all edge kinds" — turned out to already be delivered: `traverse`/
  `expand_frontier` in `unified_lineage.py` is a real, unit-tested, breadth-first, depth- and
  node-bounded traversal wired into `GET /v1/datasources/{id}/unified-lineage/impact/{node_id}`
  and the MCP tool `atlas__get_lineage_impact`, already merging FOREIGN_KEY, SUGGESTED_RELATIONSHIP,
  and DBT_DEPENDENCY/OPENLINEAGE_ETL edges with per-node hop-depth and contributing-edge-kind
  evidence, policy-scoped by organization and RBAC role. `Docs/20-modules/09-lineage.md` §12 had
  already recorded this as "Transitive impact delivered 2026-08-29", but `03-tracker.md` row LN-7
  and the `00-status.md` "Impact analysis" row had never been updated to match — a doc-sync gap,
  not missing code.
- What was genuinely still missing: `schemas.py`'s `UnifiedLineageEdgeSource` literal already
  reserved `VIEW_DEFINITION`/`PROCEDURE_DEFINITION` labels (evidently anticipating this), but
  `unified_lineage_api.py::_build_unified_graph` never populated them — the view/stored-procedure
  SQL-parsed lineage edges landed by a concurrent session's LN-2 work (`view_lineage_api.py`,
  `ViewLineageEdge`/`ProcedureLineageEdge` in `models.py`) were persisted but invisible to the
  unified traversal, so a chain like `raw_table -> view -> downstream_table_via_FK` could not be
  surfaced as transitive impact even though every individual edge existed. Closed by folding both
  edge tables into `_build_unified_graph`, following the same source-depends-on-target convention
  as the existing FOREIGN_KEY/DBT_DEPENDENCY sections: the view/procedure is the dependent node
  (`target_table_id`), the base table it reads from is what it depends on (`source_table_id`).
  Only rows the SQL parser matched to a real catalog table on *both* ends are folded in — an
  unmatched free-text table name is left to the dedicated `/view-lineage`/`/procedure-lineage`
  list endpoints rather than guessed at, to avoid a false cross-schema merge on a shared table
  name. Multiple column-level rows between the same table pair collapse into one edge (same
  dedup the dbt `COLUMN_DEPENDS_ON` rows already needed).
- BI lineage (LN-4, Tableau/Power BI now real per the tracker) is deliberately **not** folded in
  here: `BiReportNode`/`BiMetricNode` are new node kinds with their own hierarchy
  (`BiReportMetricEdge`, `BiMetricColumnEdge`), a materially bigger lift than the two-column
  table-pair edges added above, and already tracked separately as LN-11 (now scoped down to just
  the BI half, since the view/procedure half is done here). Left open rather than rushed.
- Tests added to `tests/test_unified_lineage.py` (13 total in the file now, up from 6): a real
  2-hop chain across two different edge kinds end to end against an in-memory SQLite database
  (`raw_orders --VIEW_DEFINITION--> vw_orders --FOREIGN_KEY--> fct_orders`, asserting depth and
  contributing-edge-kind evidence at each hop); the same chain with `node_limit` too small to
  reach the second hop, asserting the bound stops traversal and self-reports `truncated`; a
  cross-datasource containment test proving a `ViewLineageEdge` whose matched `target_table_id`
  points at a table in a *different* datasource never surfaces as a node (policy containment,
  independent of the org/RBAC check); and a direct HTTP-route-level cross-organization denial test
  complementing the codebase-wide INV-5 structural sweep. `ruff check .` and `mypy src`
  (188 files) both clean; full `pytest` suite green with no failures after rebasing onto latest
  origin — `67cf3ae` landed mid-session and cleaned up the last stale
  `TestParameterContractDesigner`-class doc citations the CT-1/QG-2 entries above this one had
  independently reintroduced (this paragraph deliberately does not itself fence
  "tests/test_studio.py" and that class name together inside one pair of backticks, for the same
  reason those entries tripped `test_doc_claims.py` in the first place). OpenAPI baseline regenerated
  (`scripts/openapi_diff.py --accept-baseline`) after docstring-only changes to the graph-builder
  functions and the `DomainLineageGraphRead`/`UnifiedLineageEdgeRead` schema docstrings; diff is
  description-text only, no schema or path changes.

## 2026-08-31 — MG-2 (kill-switch drill) closed: built the mechanism, then drilled it

- Re-read `Docs/60-delivery/03-tracker.md` row MG-2 before starting (still TODO on a freshly
  fetched/reset branch — not a duplicate of already-closed work). `Docs/20-modules/
  15-model-gateway.md` §7 and §14 described a kill switch as designed but undrilled, and the
  module's own events section documented `model.kill_switch_engaged` / `.released`. Checking the
  actual code found neither claim true: `grep -rn "kill_switch" src/aida` returned exactly one
  hit, a docstring mention in `compliance_packs.py` — nothing implemented engagement, storage, or
  enforcement, and `tests/test_event_catalog_gate.py`'s own "no current emitter" report confirmed
  the two event names were catalog-only, never emitted. "Designed, not drilled" overstated what
  existed; there was no design in code to drill.
- Built the minimal real mechanism, following MG-3's `ModelRouteConfiguration` pattern for what to
  reuse and what to deliberately not reuse:
  - `aida.models.KillSwitchState` — one mutable current-state row per (organization_id,
    route_key), `route_key="*"` (`model_gateway.GLOBAL_KILL_SWITCH_SCOPE`) meaning organization-
    wide, any other value scoping to one route. Same "current-state row, immutable history lives
    in `AuditEvent`/`OutboxEvent`" shape as `OrganizationIntegrationPolicy`, not an event-sourced
    table of its own. New migration `d09d6e42028d_kill_switch_state.py`.
  - `engage_kill_switch` / `release_kill_switch` / `list_kill_switch_state` in
    `ai_governance_api.py` — deliberately *not* the `ModelRouteConfiguration` maker-checker
    lifecycle: job P5 ("stop AI immediately") and the module's own kill-switch contract call for a
    single-operator, immediately-effective action audited on both engagement and reversal, the
    opposite failure mode from a route approval (premature activation is the risk there; an
    unreviewed kill is not the risk here). Gated on the `PlatformAdmin` role via `require_roles`;
    every call records an `AuditEvent` and an `OutboxEvent` (`model.kill_switch_engaged` /
    `model.kill_switch_released`, matching the event catalog's documented names exactly — the
    catalog row's `` `model.kill_switch_engaged` / `.released` `` shorthand was also fixed to
    `` `model.kill_switch_engaged` / `model.kill_switch_released` `` after finding the catalog
    gate's family-expansion parser mis-expanded the original `.released` suffix into
    `model.released`; verified against the gate's own parser both before and after).
  - `model_gateway.kill_switch_blocking_state`, checked **first** — ahead of route approval,
    selection, credential resolution, adapter registration, and budget — inside
    `ProviderNeutralModelGateway.structured_completion`. That function is the single choke point
    every generation request passes through regardless of caller (its own docstring, and module
    15's charter: "The only path from Atlas to a language model"), so the check was added there
    rather than duplicated in each caller. A live per-request DB query, not a cached flag: a
    just-committed engagement blocks the very next call. `session` and `organization_id` became
    required keyword arguments on `structured_completion` (previously neither existed on that
    signature) — the two real production call sites, `agent_orchestrator.py`'s SQL-generation path
    and `semantic_inference.py`'s `model_enrich_batch`/`enrich_with_optional_model` classification
    path, already had both in scope and needed only mechanical threading, not logic changes; mypy
    on `src` confirms no call site was missed.
- Drilled in `tests/test_kill_switch_drill.py` (6 tests), engaging and releasing exclusively
  through the real governed endpoint functions with a `PlatformAdmin` `SecurityContext` and a real
  (in-memory sqlite) database — never a direct `KillSwitchState(engaged=True)` row construction.
  The drill test itself: baseline generation succeeds, engage through the real API, measure
  engagement-to-denial latency with `time.perf_counter()` and assert it under a 5s bound (generous
  margin against the tracker's 60s requirement), assert the next generation call raises
  `KillSwitchEngaged`, assert exactly one audit row and one outbox row exist and are queryable with
  the expected actor/reason/scope, then release through the same authorization and confirm
  generation resumes and the release is itself audited. Additional tests cover route-scoped (not
  just organization-wide) engagement leaving other routes generating, `PlatformAdmin`-only
  authorization (403 for a `Viewer` context), releasing a switch that was never engaged (409), and
  the current-state listing endpoint. Documented explicitly, in the test file's own module
  docstring and in the tracker, what this drill does and does not prove: in-process/in-memory-
  sqlite latency, not network or infrastructure propagation time to a deployed gateway process or a
  production Postgres round trip — that would need a live-environment timed exercise.
- Updated the two existing test files whose call sites gained required parameters
  (`tests/test_model_gateway.py`, `tests/test_semantic_inference.py`) with a matching in-memory
  sqlite session fixture (same real-engine pattern as `test_bulk_governance_decisions.py`) rather
  than weakening the new parameters to optional.
- OpenAPI baseline (`Docs/90-reference/openapi-baseline.json`) regenerated via
  `scripts/openapi_diff.py --accept-baseline` for the three new kill-switch routes; the diff was
  additive only.
- `ruff check .` and `mypy src` (184 files) both clean. Full `pytest` suite green apart from two
  pre-existing, unrelated failures confirmed present on the freshly-reset base branch before this
  session made any change (`tests/test_doc_claims.py::test_cited_test_path_resolves`, for a
  citation elsewhere in this file and in the tracker of a `TestParameterContractDesigner` class
  that `tests/test_studio.py` does not define — that file has the cited tests as flat functions,
  not a class of that name) — left alone as out of this item's module-15/AI-governance scope, not
  silently fixed; flagged separately as its own suggested task.
- Updated `Docs/60-delivery/03-tracker.md` row MG-2 to DONE, E9 (kill-switch drill) to DONE, and
  §I "Drill currency"'s kill-switch row from Never/OVERDUE to 2026-08-31 (in-process/local),
  explicit that it is current for local/in-process only pending a timed run against a deployed
  gateway — same honesty convention as that section's existing "Batch forced-restart... Current
  for local only" row. Updated `Docs/20-modules/15-model-gateway.md` §14 and §15 to match rather
  than leave the "Designed, not drilled" claim standing.

## 2026-08-31 — UX-12 (`CatalogRowRead` read-model endpoint) closed

- Module 21's rebuild plan named one backend gap: `MetadataTableRead` returns eight fields, so a
  governed catalog row on the new `ui-next` Catalog screen (UX-11, already DONE) cost five further
  per-table calls — 100 rows = 501 requests. Closed by `GET /v1/organizations/{org}/catalog/rows`
  (`aida.api.list_catalog_rows`), composing description, proposal state, owner, certification,
  quality, glossary terms and a row-count estimate into one response per row, in a fixed number of
  batched queries independent of page size — not a query per row, which is exactly the pattern
  this endpoint exists to remove.
- New module `aida/catalog_read_model.py` holds the composition. Each field's source, chosen from
  what already exists in the platform rather than anything new:
  - **description / description_is_proposed** — the tracker's "proposal state" folded into one
    boolean because the client type (`CatalogRowRead` in `ui-next/src/lib/types.ts`) has no separate
    field for it. Precedence: latest `APPROVED` `AssetDocumentationVersion` readme (GL-9's
    evidence-scored drafting pipeline, `asset_description_service.py`) → a `PENDING_APPROVAL`
    `AssetDescriptionDraft` shown as a proposal → the older
    `MetadataBusinessAnnotation.business_description` (always review-approved) → the
    connector-sourced `MetadataTable.source_description` → `None`.
  - **owner** — GL-2 `OwnershipAssignment` (status `ACTIVE`, `subject_type` `TABLE`), falling back
    to the approved documentation version's `owner_principal`, mirroring the two-source definition
    of "owned" already in `stewardship_api._owned_table_ids`.
  - **certification** — CT-5 `AssetCertification` (`asset_type` `TABLE`), newest row first,
    projected through the existing `asset_certification_is_active` query-time projection rather than
    trusting the raw `status` column, same as every other certification caller.
  - **quality** — module 11's `DataQualityIncident` (`OPEN`/`ACKNOWLEDGED` → `INCIDENT_OPEN`) and
    `DataQualityObservation` recency (no observation ever → `UNKNOWN`; last observation older than
    14 days → `STALE`; otherwise `PASSING`). Module 11's own coupling API (`get_trust_signal`, DQ-3)
    is documented as planned, not built, so this reads the source tables directly.
  - **glossary_terms** — GL-8/SM-2 `AssetTermLink` joined to `GlossaryTermVersion` (status
    `APPROVED`), the same join `asset_description_service.gather_evidence` already uses per table.
  - **row_count_estimate** — latest `TableProfile.row_count_estimate` (module 05 profiling), batched
    with the `row_number() OVER (PARTITION BY table_id ...)` idiom
    `intelligence_api._latest_table_profiles` and `quality_service` already use for the same
    "latest per table" problem, rather than inventing a new one.
- CT-2's `CursorPage` keyset contract is reused verbatim (`aida.pagination.apply_keyset`/
  `decode_cursor`/`encode_cursor`, the same primitives `list_tables` calls), including `total: null`
  under a cursor — the endpoint never runs a `COUNT(*)` on the keyset path.
- Permission-filtered by the same gate `list_tables` already applies (`aida.authorization_gate.gate`,
  action `READ_METADATA`, resource_type `datasource`) rather than a new authorization path — called
  once per **distinct datasource** on the page (cached, so a page dominated by one denied datasource
  still costs one call for it, not one per row), and a row from a datasource the caller cannot read
  is dropped from the page rather than failing the whole request. `enforce_organization` still fires
  first and unconditionally denies a foreign organization before any session access, same as every
  other organization-scoped read — the generic
  `test_inv5_tenant_isolation.py::test_cross_tenant_denial` parametrization picks the new route
  up automatically since its path names `{organization_id}`, and `list_catalog_rows` was added
  to `test_inv4_authorization_wiring.py`'s
  `test_the_catalog_read_handlers_are_gated` parametrization alongside `list_tables`/`list_columns`/
  `list_constraints`/`get_latest_table_profile`.
- No writes anywhere in the new code path (no `session.add`/`commit`) and no source-system SQL is
  accepted or forwarded, so ADR-0004 (the execution gateway is the only path to a source query) is
  untouched by construction, not just by inspection.
- `certification` is also accepted as an optional query filter, applied after composition (the
  states are derived, not stored columns) rather than as a correlated SQL subquery — the same reason
  permission filtering above can leave a page short of `limit`: walk further with `next_cursor` to
  keep collecting matches.
- Tests: new `tests/test_catalog_rows_read_model.py` (11 tests, real SQLite execution via aiosqlite
  — PostgreSQL is unreachable in this sandbox, same rationale `test_catalog_pagination.py` already
  documents) covering: the full composed shape against a fully-seeded row; the description precedence
  chain across five source combinations; all four quality states; all three reachable certification
  states plus the `certification` query filter; the CT-2 cursor walk (every row exactly once,
  `total` non-null only on page one) and the offset-mode/invalid-cursor cases mirrored from
  `test_catalog_pagination.py`; a cross-organization 403 fired before the (intentionally
  exploding-on-touch) session is used; per-datasource permission filtering proven by monkeypatching
  `aida.api.gate` to deny one of two seeded datasources and asserting both that the denied
  datasource's row is dropped and that `gate` was called exactly twice (once per distinct
  datasource, not once per row); and a query-count-bounded test (`before_cursor_execute` statement
  counter, the same pattern `test_bulk_governance_decisions.py`'s `_StatementCounter` uses) proving
  the statement count is identical for a 4-row and a 24-row page.
- Surfaced and fixed one latent gap along the way: `asset_certification_is_active`'s naive/aware
  datetime comparison raises under SQLite (`DateTime(timezone=True)` round-trips naive there,
  tz-aware under PostgreSQL in production) — nothing had previously combined a SQLite-seeded
  certification row with that check in a test. Fixed locally in `catalog_read_model.py` with a small
  `_as_aware` coercion and a non-frozen proxy dataclass satisfying `AssetCertificationLike` without
  mutating the ORM row (a frozen dataclass's fields are read-only, which mypy correctly refuses to
  accept against a protocol declaring settable attributes) — `asset_certification.py` itself was left
  unchanged, out of scope for this item.
- `ui-next/src/lib/api.ts`'s `VITE_USE_FIXTURES` flag was left at its default (fixtures on) rather
  than flipped to `0`: it also gates `fetchAssetEvidence`
  (UX-13, `GET /v1/metadata/tables/{id}/evidence`), which does not exist yet — flipping the flag
  now would 404 the evidence pane rather than just switch the catalog table to real data. Noted in
  `00-status.md`'s new `ui-next` shell rebuild row rather than silently left unflipped; the honest
  fix is either landing UX-13 first or splitting the flag per-endpoint.
- `Docs/90-reference/openapi-baseline.json` regenerated via `scripts/openapi_diff.py
  --accept-baseline`; the diff gate confirmed the change is additive only (`added path
  '/v1/organizations/{organization_id}/catalog/rows'`, no breaking changes) before the baseline was
  regenerated.
- Verification: `ruff check .` clean. `mypy src` clean (187 files). Full `pytest` suite green —
  before this item's rebase onto origin, two pre-existing `test_doc_claims.py` failures were
  present (a stale test-path citation in the ST-A4 entry above, confirmed present via `git stash`
  before this item's own changes and unrelated to this work); a separate concurrent session fixed
  that citation, and the fix was picked up by this item's own rebase onto origin before pushing, so
  no failures remain.
- Known limitations: no dedicated composite index on `(organization_id, status, name, id)` for the
  org-wide keyset order (the existing `ix_metadata_table_org_status` and per-datasource composite
  index don't cover this exact ordering) — acceptable for a first landing, worth revisiting once
  bank-scale row counts are available; and the certification query filter, being applied after
  composition, can return fewer than `limit` items per page for a narrow filter, same as permission
  filtering already can.

## 2026-08-31 (eleventh entry)

### AU-9: the first production deployment artifact — a reviewable Kubernetes manifest

The 2026-08-30 end-to-end audit's §4 said it plainly: *"No production deployment artifact
exists. `infra/` contains four `init.sql` seed files... Whoever writes the first manifest
decides whether C1 is set correctly — and there is currently nothing to review."* This entry is
that first manifest.

- New `infra/k8s/base/`: `namespace.yaml`, `serviceaccount.yaml` (no API token mounted —
  least privilege), `configmap.yaml`, `secret.example.yaml` (template only, deliberately
  excluded from `kustomization.yaml`), `deployment.yaml`, `service.yaml`,
  `poddisruptionbudget.yaml`, `migration-job.yaml` (mirrors compose.yaml's `migrate` service —
  `alembic upgrade head` on the same image before/alongside rollout), and
  `kustomization.yaml` tying the applyable resources together. Companion `infra/k8s/README.md`
  states what a deployer must supply and what this manifest does and does not cover.
- Real env var names were read from `src/atlas/platform/config.py` (`Settings`,
  `env_prefix="AIDA_"`), not guessed: `AIDA_ENVIRONMENT=production` and
  `AIDA_IDENTITY_PROVIDER=oidc` are pinned in `configmap.yaml`, directly answering audit
  findings C1 (typo'd variable names are silently dropped by `extra="ignore"`, so a reviewed
  manifest with the *correct* names is the available mitigation short of an application-code
  fix) and C2 (the default `development` identity provider trusts an unauthenticated
  `X-Roles` header).
- Checked the `Dockerfile` before assuming a fix was needed: it already creates and switches
  to a non-root `aida` user (uid/gid 10001, `USER aida`) — no image change was required.
  `deployment.yaml`'s pod- and container-level `securityContext` (`runAsNonRoot: true`,
  `runAsUser/Group: 10001`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
  `capabilities: drop: [ALL]`, `seccompProfile: RuntimeDefault`) was set to match that image
  rather than assert a number nothing enforces.
- CPU/memory requests and limits are inline and commented as tunable defaults, not
  measurements — no load test exists for this codebase yet (audit §5), so the entry says that
  rather than implying a capacity-planning result.
- `image:` in both `deployment.yaml` and `migration-job.yaml` is
  `REPLACE_ME_REGISTRY/aida-api@sha256:REPLACE_ME_WITH_REAL_DIGEST` — the manifest's *shape*
  forbids a floating tag, matching the audit's specific call-out of `compose.yaml`/minio's
  `:latest`. The README documents that the CI/CD pipeline expected to populate the real digest
  does not exist yet (audit remediation item #12, still open) and that populating it is not
  this item's job.
- Every credential-bearing value (DB DSN, Redis URL, Neo4j password, object-store keys,
  `AIDA_AUDIT_HMAC_KEY`/`AIDA_TOKENIZATION_KEY` — both required to independently be
  32+ characters in production per `Settings.reject_insecure_production_configuration`
  regardless of signing provider, `OPENAI_API_KEY`/`GEMINI_API_KEY`) is referenced via
  `envFrom: secretRef: aida-api-secrets`, never hardcoded. `secret.example.yaml` documents the
  required keys as a template a deployer populates via `kubectl create secret` or, preferably,
  a secrets operator — it is excluded from `kustomization.yaml` specifically so `kubectl apply
  -k` can never apply placeholder credentials by accident.
- Readiness/liveness probes point at the existing `/health/ready`/`/health/live` routes in
  `src/aida/main.py` (audit-confirmed to exist already).
- Honest gap surfaced while writing the ConfigMap: `Settings` forbids
  `credential_provider=="env"` in production, so `configmap.yaml` sets
  `AIDA_CREDENTIAL_PROVIDER=vault` to satisfy that startup check — but per audit remediation
  item #10, no non-`env` `SecretProvider` is actually implemented in `src/aida/secrets.py` yet
  (only the `Protocol` and caching exist). This lets the process pass config validation, not
  actually resolve production credentials end to end. `infra/k8s/README.md` states this
  explicitly rather than letting the manifest imply it's solved; the real fix is AU-10.
- Validation: this sandbox has no reachable Kubernetes cluster, said plainly rather than
  glossed over. `kubectl` v1.30.5 and `kubeconform` v0.6.7 were fetched to do the strongest
  offline check available. `kubectl kustomize infra/k8s/base` renders all 7 resources with no
  errors; `kubectl apply --dry-run=client` was attempted but this kubectl version calls out to
  a live API server even in client mode for resource-mapping discovery, which fails with no
  cluster present. `kubeconform -strict -summary` against the rendered output validated all 7
  resources (plus the excluded `secret.example.yaml` template, checked separately) against the
  real, versioned Kubernetes v1.30 OpenAPI schema — 7/7 and 1/1 valid, zero errors. This is a
  stronger structural check than dry-run client validation would have been, but it is still not
  `--dry-run=server` against a real cluster, which is named in the README as the next real
  validation step once one exists.
- Verification: confirmed via `git diff --stat` that no application source was modified before
  running checks (only new `infra/k8s/` files and doc updates). `ruff check .`: 2 pre-existing
  `UP042` findings in `src/aida/sql_lineage_parser.py` (str+Enum inheritance), confirmed via
  `git diff --stat HEAD -- src/aida/sql_lineage_parser.py` (empty) to already be present on
  `origin/feature/snowflake-dbt-lineage-mcp` before this work and untouched by it. `mypy src`:
  clean, 190 files (required an `uv sync --all-extras --dev` first — the base `uv run mypy`
  environment didn't have `pydantic` installed for the mypy plugin). Full `pytest` suite: exit
  code 0, no failures (all `.`/`s`/`x` markers across the full run, consistent with the prior
  entry's skip/xfail counts).
- Known limitations, stated in `infra/k8s/README.md` rather than left implicit: no CI builds
  or digest-pins the image yet (#12); no non-`env` secret provider is implemented (AU-10); no
  Ingress/TLS termination, NetworkPolicy, or autoscaling is included (explicitly out of scope,
  left to the deployer's existing platform conventions); resource requests/limits are
  defaults, not measurements; the Temporal-outage readiness coupling (audit remediation #11)
  is unfixed in application code and this manifest's probe wiring cannot paper over it; and
  the datastores themselves (Postgres, Redis, Temporal, Neo4j, Redpanda, object storage) are
  referenced by in-cluster hostname but this directory does not include manifests to actually
  run them — that would be its own tracker item.

## 2026-08-31 — AG-1/AG-2/TS-6 (indirect-injection defense) closed: the 726-line corpus was reachable from nothing

- The 2026-08-30 audit (`04-end-to-end-audit-2026-08-30.md` §2) found AG-1/AG-2/TS-6 all pointed at
  `injection_defense.py`/`injection_corpus.py` (726 lines, a genuinely richer detector than what
  ships live) with zero callers outside their own test file, and separately that
  `ingest_screening.is_eligible_for_model_context` — its own docstring calls it "the one question
  every model-context builder must ask" — had zero callers anywhere, including inside
  `ingest_screening.py` itself. Two distinct gaps, both closed here.
- **Gap 1 — the richer detector never ran on live text.** `ingest_screening.screen_text`
  (`ingest_screening.py:75`), the function actually wired into the live write path
  (`ingestion.py:544`'s `_store_source_sql`, which sets `MetadataViewDefinition`/
  `MetadataRoutine.screening_status` on every ingested view definition and routine body), only ran
  `prompt_risk.DeterministicPromptRiskClassifier` — no multilingual, no obfuscation/encoding, no
  homoglyph coverage. `screen_text` now also runs `injection_defense.screen_metadata`
  (`ingest_screening.py:91`) and quarantines on either detector flagging, tagging the reason code
  `INJECTION_DEFENSE:<threat_type>` so the two detectors stay distinguishable in the stored verdict.
  `SCREENING_VERSION` is now the concatenation of both detectors' versions, so a stored verdict from
  before this change is identifiable for re-screening. `screen_many` now passes each field's name as
  `content_origin` for a better evidence trail; existing callers passing no `content_origin` are
  unaffected (new keyword-only parameter, default `"unknown"`).
- **Gap 2 — `is_eligible_for_model_context` had no consumer, because the consumer it was written for
  doesn't exist yet.** Tracing every reader of the `screening_status` columns
  `MetadataViewDefinition`/`MetadataRoutine` carry (envelope 1.1, gap/02 N2/N12) found none —
  "meaning inference and tool generation," the two consumers envelope_models.py's docstring names as
  the intended readers, are not built. Rather than invent a consumer for those two tables, this
  found the real, live, already-reachable analogue: `mcp_server.py::_transformation_detail`
  (dispatched from `_handle_native_lineage_tool_call`'s `get_transformation_detail` slug, itself
  reached from the real `/mcp` `tools/call` JSON-RPC handler) returns `DbtResource.description` —
  free text pulled from a dbt manifest's `description:` field, source-controlled, and with no stored
  `screening_status` column of its own — directly into an MCP tool response, i.e. straight into the
  calling LLM's context. This is this branch's (`feature/snowflake-dbt-lineage-mcp`) own new
  indirect-injection surface, live and previously completely unscreened.
  `_transformation_detail` (`mcp_server.py:924-935`) now screens `resource.description` live via
  `ingest_screening.screen_text` on this single-row read (explicitly not a bulk path — the module's
  "screen once at write" rationale doesn't apply where there is no write-time verdict to read back)
  and gates it through `is_eligible_for_model_context` before it goes into the response; a quarantined
  description is nulled out and replaced with `description_screening: {status, reason_codes}` rather
  than silently dropped, matching the "quarantine, not deletion" contract (the DB row is untouched —
  only the MCP-served copy is redacted).
- Both fixes proven end to end, not by calling `injection_defense.py`/`is_eligible_for_model_context`
  directly in a test (the exact failure mode the audit found — a passing unit test with no callers is
  indistinguishable from a wired one):
  - `tests/test_envelope_v11.py::test_a_multilingual_indirect_injection_in_a_routine_body_is_quarantined`
    drives a Chinese "ignore all previous instructions" string from `injection_corpus.MULTILINGUAL_INJECTIONS`
    through `_ingest` (the same helper `test_hostile_text_in_a_view_definition_is_quarantined` above
    uses to drive the real ingestion pipeline) embedded in a routine body, and asserts
    `MetadataRoutine.screening_status == "QUARANTINED"` with `INJECTION_DEFENSE:MULTILINGUAL_INJECTION`
    in the reason codes — a case `DeterministicPromptRiskClassifier` alone does not flag (verified:
    `prompt_risk.py`'s classifier has no multilingual signal, only English `\b`-word patterns), so this
    is proof the richer detector, not just some detector, runs live.
  - `tests/test_mcp_server.py::test_transformation_detail_quarantines_a_multilingual_injection_description`
    seeds a fake `DbtResource` with the same corpus string as its `description` and drives it through
    `_handle_native_lineage_tool_call("get_transformation_detail", ...)` — `_transformation_detail`
    is deliberately left unmonkeypatched here, unlike every other `get_transformation_detail` test in
    the file, specifically so its own screening code executes — and asserts the JSON response omits
    the hostile text and carries `description_screening: {"status": "QUARANTINED", ...}`.
    `::test_transformation_detail_passes_through_a_benign_description` is the paired false-positive
    check: an ordinary manifest description passes through unchanged with `"status": "CLEAN"`.
  - The pre-existing `test_hostile_text_in_a_view_definition_is_quarantined` (an
    `INSTRUCTION_OVERRIDE` case `prompt_risk.py` alone already caught) still passes unchanged —
    Gap 1's fix is additive, not a behavior change for cases already caught.
- Tracker rows AG-1, AG-2, TS-6 updated in place with file:line citations for both call sites rather
  than left as the original "delivered" evidence, per AU-2's redefinition of DONE (reachability
  gate).
- Verification: `ruff check .` clean. `mypy src` clean (190 files). Full `pytest` suite green in a
  genuine foreground run — one unrelated flake on first pass,
  `test_bulk_governance_decisions.py::test_bulk_decide_at_scale_round_trips_are_linear_not_quadratic`
  (a wall-clock linear-vs-quadratic ratio assertion, sensitive to the heavy CPU contention from the
  several other concurrent sibling-worktree `pytest` runs sharing this host at the time), confirmed
  by re-running it alone (passed in 37.6s) and unrelated to any file this item touches.
- Known limitations / left for a follow-up: `mcp_server.py` has other free-text fields flowing into
  tool/resource/prompt responses (context-product and business descriptions among them) that were not
  audited or screened here — this item's scope was the one confirmed-live, confirmed-unscreened path
  (`_transformation_detail`'s dbt description) that matches the audit's citation, not a full sweep of
  the MCP server's text surface. `DbtResource` itself still carries no `screening_status` column, so
  `_transformation_detail` screens live on every read rather than once at dbt-artifact write time
  (`dbt_artifacts.py`) the way view/routine text does — cheap enough for this single-row detail call,
  but adding a stored verdict at dbt ingestion would need a `models.py` migration and was out of this
  item's scope (`models.py` is flagged as under concurrent edit; `envelope_models.py` exists
  specifically to avoid touching it).
