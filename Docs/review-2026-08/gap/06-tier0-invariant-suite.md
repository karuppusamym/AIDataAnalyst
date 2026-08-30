# Tier-0 invariant suite — all nine invariants executable

> Gap item **E4**. Tracker row **ST-03**. Written 2026-08-30.
> Status: **all nine Tier-0 invariants are now proven by tests that run in the default
> `pytest` invocation with no external service.** Two strict `xfail`s remain; both name a
> gap in the *codebase*, not in the suite, and both are set out in full below.

## 1. What changed

Before this item, `tests/test_tier0_invariants.py` formalised four of nine invariants
(INV-2, INV-3, INV-4, INV-8) and its own docstring explained why the other five were left
alone: they were thought to need a live Neo4j/search stack, a full ingestion pipeline, an
all-endpoints harness, and a certification-result store.

Four of those five turned out to need none of that. What they needed was enumeration —
deriving the subject list from the live FastAPI application, the connector registry and the
parsed source tree, instead of from a hand-written list — plus strict in-memory doubles for
the two places the platform actually touches infrastructure. The fifth, INV-9, is proven at
the observable level and honestly recorded as unenforced at the derivation level.

| Invariant | Before | Now | Where |
|---|---|---|---|
| INV-1 single authoritative store | none | 8 tests | `tests/test_inv1_single_authoritative_store.py` |
| INV-2 one execution choke point | 2 tests | 2 tests (unchanged) | `tests/test_tier0_invariants.py` |
| INV-3 model output is never authority | 1 test | 1 test (unchanged) | `tests/test_tier0_invariants.py` |
| INV-4 fail closed | 11 tests | 11 tests (unchanged) | `tests/test_tier0_invariants.py` |
| INV-5 tenant isolation | none¹ | 51 tests | `tests/test_inv5_tenant_isolation.py` |
| INV-6 value-freedom | none | 25 tests | `tests/test_inv6_value_freedom.py` |
| INV-7 attributability | none | 9 tests + 1 strict xfail | `tests/test_inv7_attributability.py` |
| INV-8 maker ≠ checker | 9 tests | 9 tests (unchanged) | `tests/test_tier0_invariants.py` |
| INV-9 honest capability reporting | none | 27 tests + 1 strict xfail | `tests/test_inv9_capability_honesty.py` |

¹ Two workspace-authorization INV-5 tests were added to `tests/test_tier0_invariants.py`
concurrently with this work by another writer; they prove one entry point deeply against a
real in-memory SQLite database. The new module proves the whole API surface. They are
complementary and both are kept — see §9.

Shared harnesses live in `tests/support/` (`app_surface.py`, `doubles.py`). Nothing in that
package asserts anything; it exists so each invariant test can enumerate rather than
sample.

**No test in this suite is skipped.** `@pytest.mark.skip` does not appear anywhere in it.

## 2. The design rule these tests follow

A cross-tenant test that drives three hand-picked endpoints silently stops covering the
system the day someone adds a fourth. Every test here therefore derives its subject list at
run time:

- **Routes** come from the live `FastAPI` app. This needed work: since FastAPI 0.141,
  `app.include_router(...)` leaves a lazy `_IncludedRouter` placeholder and `app.routes`
  returns **3** routes, not 199. `tests/support/app_surface.iter_api_routes` walks
  `original_router.routes` recursively and asserts it found at least 100, so the day that
  layout changes again the suite fails loudly instead of quietly checking three health
  endpoints.
- **Connectors** come from `connector_registry.definitions`.
- **Columns** come from `models.Base.registry.mappers`.
- **Cypher statements** come from an AST walk of every `.run(...)` / `.execute_query(...)`
  call on a graph receiver in `src/aida`.
- **Mutating endpoints** are *derived* — a route counts if its HTTP verb says so **or** its
  call graph reaches a session write — so a GET that quietly writes is caught too.

Where something must be excluded, it is excluded by an explicit named dict with a reason
per entry, and each such list has a companion test asserting it is still true (entries name
real routes, and no entry has silently started passing). A stale exclusion is a hole nobody
can see; these lists cannot go stale without failing.

## 3. INV-1 — single authoritative store

> PostgreSQL holds authoritative state. Neo4j, vector indexes, search indexes, Redis, and
> object-storage indexes are rebuildable projections and are never read as truth for an
> authorization, approval, or correctness decision.
> **Enforcement.** Projections are written only by outbox projectors, never by request-path
> code. No service dual-writes PostgreSQL and a projection.

**Proven.** `tests/test_inv1_single_authoritative_store.py`.

*The no-dual-write half* is proven exhaustively. Every Cypher statement in `src/aida` is
located by AST — by call site, not by keyword, because "MATCH" and "RETURN" appear in SQL
and prose in twenty-nine modules and a keyword scan reports all of them as graph clients
when two of them are. Each statement is classified read or write (`CREATE CONSTRAINT` /
`CREATE INDEX` are separated out as idempotent schema DDL). Writes must live under
`src/aida/projectors/`. Request-path readers must appear on a closed, reviewed list, which
today contains exactly two entries, both of which degrade to PostgreSQL:
`api.get_graph_summary` reconciles graph counts against PostgreSQL in the same handler, and
`lineage_graph_store.read_bounded_impact` returns `None` on any graph error so the caller
recomputes from the authoritative store.

A fourth test requires every projector write to use `MERGE` rather than a bare `CREATE` —
P5 idempotency, and the property that makes at-least-once redelivery survivable.

*The rebuildable half* is proven as **replay determinism from authoritative state**.
`project_discovery` is driven twice against a fixed PostgreSQL fixture, with the recorded
graph discarded in between ("delete the graph entirely"), and the two projections must be
byte-identical; a third test asserts every authoritative row's id appears in the rebuilt
projection ("assert full reconstruction"). Two further tests assert every projected node
carries its full tenancy path (INV-5 inside INV-1) and that the projection payload contains
no field outside the structural-metadata set (INV-6 inside INV-1).

**Harness.** `RecordingGraphDriver` captures the `(statement, parameters)` stream verbatim.
It is deliberately **not** a Cypher interpreter: a hand-written interpreter would be a
second, unreviewed implementation of the graph store, and a test that passes because two of
my own approximations agree proves nothing. `ModelRoutedSession` answers
`session.scalars(select(Model)…)` by inspecting the statement's entity rather than by call
order, so the double survives a projector reordering its loads, and raises on an unmodelled
entity so a new node type fails loudly rather than being silently omitted.

**What is still not proven.** This does not prove Neo4j applies the projection correctly,
because no Neo4j is running. **Gap item E5 (projection rebuild drill) remains open and this
suite does not claim otherwise.** What it removes is the failure mode where the projector
stopped being replayable months before anyone attempted the drill.

## 4. INV-5 — tenant isolation is total

> Every governed record carries an organization boundary… Authorization defaults to deny.

**Proven.** `tests/test_inv5_tenant_isolation.py`, 51 tests over all **199** API routes.

1. **No anonymous route.** The dependency tree FastAPI built for each route must contain
   `get_security_context` (directly or via the closure `require_roles` returns). Exactly
   three routes do not: `/health/live`, `/health/ready`, `/metrics`. A second test asserts
   that set has not grown.
2. **Cross-tenant denial, driven.** All **44** routes carrying `{organization_id}` are
   invoked directly with a foreign organization in the path, every non-`PlatformAdmin` role
   at once (so a denial cannot come from a missing role), and an `ExplodingSession` that
   raises on any attribute access. All 44 raise `HTTPException(403, "cross-organization
   access denied")` **before touching the database** — which is the stronger claim: a
   tenancy check that fires after the query has run is a filter, not isolation.
   Request bodies are built with `model_construct()` (validation skipped) precisely so that
   a handler which reads the body before checking the tenant fails this test.
3. **The other 155 routes** cannot be probed that way — there is no organization argument to
   poison — so they are proven structurally: the handler must reach `enforce_organization`,
   `require_organization`, `policy_engine.authorize`, or an `organization_id` filter,
   following the call graph through the module-private loaders (`_load_datasource`,
   `_source`, `_table`, `_version_scope`, `_project_scope`, `_event_scope`) where the
   enforcement actually lives. Eight routes reach none, all listed with a reason: the three
   health/metrics endpoints, `GET /v1/ai/runtime-status` (Settings-derived posture),
   `GET /v1/ai-assessment-templates` (static questionnaire catalogue),
   `GET /v1/connectors/capability-matrix` (process-global registry),
   `POST /v1/context-compiler/validate` (pure validation, no session), and
   `POST /v1/organizations` (creates the boundary itself; `PlatformAdmin` only).
4. **Background workers.** Every registered Temporal activity plus the four projector entry
   points must reach an `organization_id` scope. All do except one — see §8.

**Call-graph resolution note.** Method calls are resolved by name against the calling
module and the modules it imports, never against the whole tree. Searching everywhere linked
`session_factory()` in a health check to an unrelated `organization_id` filter three modules
away and made the scan meaningless.

## 5. INV-6 — value-freedom of control-plane state

> Raw source business values do not enter platform tables, logs, traces, events, profiles,
> model context, or evidence records by default.

**Proven.** `tests/test_inv6_value_freedom.py`, 25 tests.

The specced test runs a full end-to-end fixture with sentinel values. There is no end-to-end
fixture here, so the property is proven at **the boundary where source values actually enter
the process**: `QueryExecutionGateway.execute` is driven in-process with a `FakeSqlExecutor`
returning sentinel-laden rows and a SQL statement carrying a sentinel literal. Every ORM row
the gateway stages — `QueryExecution`, both `AuditEvent`s, the `OutboxEvent` — is then
rendered column by column (JSON columns through `json.dumps`, so a sentinel buried in a
nested `details` dict is as findable as one in a varchar) and searched.

The test asserts the run reached `COMPLETED` and that the sentinel *did* reach the caller's
result set, so the scan cannot pass by having examined nothing.

A permanent **negative control** sits next to it: the same run with
`redact_sql_literals` monkeypatched to the identity function must find the literal. Without
it, a change that broke `_persisted_values` would leave the main test green forever.

The rest is enumeration:

- **Persisted SQL redaction** and **column-lineage evidence**, parameterized over every
  dialect in the registry (`postgres`, `oracle`, `tsql`, `bigquery`, `snowflake`) — adding a
  connector extends the guarantee automatically. Both also assert the *structure* survives,
  so a redaction that destroyed the table names would not pass.
- **The audit digest** is HMAC-keyed: value-free, and a different key gives a different
  digest, so a stored record cannot be re-derived by anyone without the server key.
- **Profiles contain statistics only**: every field of `TableProfileSnapshot` /
  `ColumnProfileSnapshot` is checked by name against value-bearing fragments. `min_length`
  is a length; `min_value` would be a value, and would fail — which is why this checks names
  rather than merely "no strings".
- **Ingestion validators**, parameterized over all six forbidden attribute fragments
  (`sample`, `row_value`, `password`, `secret`, `token`, `credential`), each driven through
  the real `MetadataIngestionCreate` validator, with a companion test asserting a clean
  envelope is accepted.
- **The whole schema by reflection**: every mapped column across every model, against
  value-bearing name fragments. This is a naming ratchet and says so — it cannot prove a
  column named `notes` is value-free. What it does is fail the moment someone adds
  `sample_values`, `raw_question` or `result_rows` to a control-plane table, which is how
  this invariant would actually break. Two exemptions, both argued:
  `query_memory_evidence.question_hash` and `agent_run.question_hash` (keyed HMAC
  fingerprints — the invariant's own prescribed form) and
  `metadata_business_annotation.suggested_questions` (authored prompts, not source data).

**What is still not proven.** The ingestion and profiling pipelines are not driven
end-to-end — they cannot run without a source database. Their value-freedom is covered
structurally (validators, profile dataclasses, schema reflection) rather than by sentinel
sweep. Closing that needs the same fixture infrastructure as gap items E5/E10.

## 6. INV-7 — attributability of high-impact actions

> Every mutation produces an audit record carrying actor identity, resource, action, tenant
> boundary, correlation ID, and timestamp, written in the same transaction as the mutation.

**Proven.** `tests/test_inv7_attributability.py`, 9 tests + 1 strict xfail.

- **Every mutation audits.** 99 mutating routes (verb ∪ derived-write) must reach
  `record_audit` transitively.
- **The record's contents.** Built through the real `record_audit` and checked field by
  field against the six attributes the invariant names. `occurred_at` is populated by
  SQLAlchemy at INSERT time, so an un-flushed instance legitimately has `None` there; the
  test asserts the column is `NOT NULL` *and* carries a default, which together make a
  timestamp-less audit row impossible without needing a live database.
- **The helper stays strict.** `record_audit`'s attributable parameters must have no
  defaults, so an audited call site cannot silently omit one.
- **The same-transaction clause** is proven twice: behaviourally, with a
  `_TransactionWitness` that snapshots the staged batch at every commit boundary and
  requires an `AuditEvent` in each; and statically, by asserting that within
  `QueryExecutionGateway.execute` the first `record_audit` precedes the first
  `session.commit()` — which holds for every branch, including the rejection paths a fake
  source never reaches.

### 6.1 An INV-7 breach — 13 endpoints — **CLOSED 2026-08-30**

> **Status update (2026-08-30).** All thirteen endpoints now audit. The strict xfail this section
> describes is gone; `test_every_mutation_audits` passes with an **empty** exemption dict, and
> `test_no_unaudited_mutation_remains` asserts it stays empty. The closure record — the endpoint →
> handler → audit-action-name mapping, and the reasoning behind each name — is
> `gap/09-inv7-audit-closeout.md`. §12 rows 1 and 3 below are likewise done. The section is kept
> because the *finding* is the useful part: a data-driven scan found thirteen governed mutations
> that a hand-written test list would have missed.

#### The finding, as originally recorded

`_KNOWN_UNAUDITED_MUTATIONS` in that module is **not an exemption, it is a finding.**
Thirteen endpoints commit governed state with no audit record:

| Endpoint | What it writes |
|---|---|
| `POST /v1/ai-asset-versions/{version_id}/provider-sync` | rewrites provider evidence, runtime evidence and the version fingerprint; outbox event, no audit row |
| `POST /v1/ai-asset-versions/{version_id}/remediations` | creates an `AiRemediation`; outbox event, no audit row |
| `POST /v1/ai-asset-versions/{version_id}/submit` | transitions an AI asset version into review |
| `POST /v1/ai-assets/{asset_id}/retire` | requests retirement of a registered AI asset |
| `POST /v1/ai-assets/{asset_id}/versions` | creates an AI asset version |
| `PUT /v1/ai-remediations/{remediation_id}` | updates remediation status and evidence |
| `POST /v1/data-contract-versions/{contract_id}/submit` | submits a data contract version for governance review |
| `POST /v1/data-products/{product_id}/contracts` | creates a data contract |
| `POST /v1/data-products/{product_id}/versions` | creates a data product version |
| `PUT /v1/data-product-versions/{version_id}` | updates a data product version in place |
| `POST /v1/data-product-versions/{version_id}/submit` | submits a data product version for review |
| `POST /v1/data-product-versions/{version_id}/retire` | requests retirement of a published version |
| `POST /v1/marketplace/access-requests/{request_id}/revoke` | revokes a marketplace entitlement — an access-removal event, the most audit-relevant action in the marketplace |

**Required change under `src/` (not made — this workstream does not own those files):** add
a `record_audit(...)` call to each, inside the same transaction as the mutation, following
the pattern already used at `ai_registry_api.py:252/413/674` and
`product_marketplace_api.py:538/1010/1650`. Eleven of the thirteen already emit an outbox
event, so the tenant boundary and resource id are already in hand at the call site.

`test_every_mutation_audits` passes today by skipping exactly these thirteen, so a
**fourteenth** fails immediately. `test_no_unaudited_mutation_remains` is a
`xfail(strict=True)` asserting the list is empty: it fails today, and turns into a **hard
failure** the moment the endpoints are fixed, which forces the list to be deleted rather
than quietly outliving the bug.

### 6.2 A judgement call for Architecture

Eight read endpoints stage a row as a side effect — `ensure_default_domain` and
`ensure_organization_integration_policy` lazily create a per-organization default. They are
listed in `_LAZY_DEFAULT_WRITE_ROUTES`. They do write governed rows, so a literal reading of
INV-7 requires an audit entry; but the row records no actor decision, and an audit entry per
GET would bury the trail rather than enrich it. **This is not settled in the test file.**
Architecture should decide whether "mutation" in INV-7 means "stages a row" or "records an
actor's decision", and the invariants document should say which.

## 7. INV-9 — honest capability reporting

> A connector, adapter, or feature advertises only behaviour that is implemented and passing
> its certification suite. Planned capability is displayed as planned.
> **Enforcement.** Capability flags are derived from the certification result, not
> hand-declared.

**Proven.** `tests/test_inv9_capability_honesty.py`, 27 tests + 1 strict xfail, driven over
the registry rather than a list of connectors.

- **Advertised == implemented.** Each of the five IMPLEMENTED connectors is constructed and
  its `capabilities` compared to the registry's advertised dict, with a further check that
  the dict covers every flag on `ConnectorCapabilities` (a missing key reads as "absent",
  not "false"). A companion test fails if a registered connector has no test credential
  payload, so a new connector cannot silently drop out of the comparison.
- **IMPLEMENTED means executable.** Each must actually be a `SqlExecutor`, or
  `open_execution_session` would turn every query against that source into a 500 rather than
  a denial.
- **Planned is displayed as planned.** Each PLANNED connector (`databricks`, `teradata`,
  `db2`) must advertise `{}`, `NOT_CERTIFIED`, version `0.0.0`, and must not be
  constructible.
- **The customer-facing surface.** `default_capabilities` — what
  `GET /v1/connectors/capability-matrix` renders — is driven over every definition and must
  return `{}` for anything not IMPLEMENTED.
- **The load-bearing consequence.** `explain` is the one flag with teeth: the gateway will
  not run a statement it cannot cost. Driving the real gateway with `explain=False` must
  produce a denial (Oracle advertises exactly that today), with a companion test proving an
  `explain=True` run completes — otherwise a gateway that rejected everything would pass the
  denial test while proving nothing.
- **At least one honest `False`.** A registry where every flag is `True` would satisfy every
  agreement test above while telling a customer nothing. This fails if the flags ever become
  uniformly optimistic.

### 7.1 The enforcement clause is not implemented

`ingestion.default_capabilities` returns `ConnectorDefinition.capabilities` **verbatim** —
the hand-written dict. `connector_certification_evidence` runs six checks, of which exactly
one (`hierarchy_contract`) reads a capability flag, and it reads only `catalogs` and
`schemas`. **`explain`, `constraints`, `indexes`, `partitions`, `query_history`,
`delegated_identity` and `approximate_statistics` are never certified.**

`test_capability_flags_are_derived_from_certification` is a `xfail(strict=True)` saying
exactly that. It is accompanied by `test_certification_evidence_still_only_covers_the_
hierarchy_flags`, which runs the real certification suite and pins the *size* of the gap —
if a check for `explain` is added, that test fails and the xfail's stated reason must be
rewritten. An honest gap statement has to be maintained, not written once.

**Required change under `src/` (not made):** extend `connector_certification_evidence` with
a check per capability flag, and change `default_capabilities` to derive the advertised dict
from the most recent `ConnectorCertificationRun` for that datasource rather than from
`definition.capabilities`. This depends on gap item **E12** (connector + lineage-parser
certification corpus) for the checks to have anything to assert against.

## 8. INV-5 — one worker without an explicit tenant predicate

`aida.workflows.activities.plan_profile_tasks` selects `MetadataTable.id` filtered on
`datasource_id` only, having already loaded the datasource from the analysis run. It is
**correct** — a datasource belongs to exactly one organization — but it is the only query in
the platform that relies on the FK instead of restating the boundary, so it loses the
defence in depth every other query has. It is listed in `_TRANSITIVELY_SCOPED_WORKERS` with
that reason, and a companion assertion removes the entry from the list the day the predicate
is added.

**Suggested change under `src/` (not made):** add
`MetadataTable.organization_id == datasource.organization_id` to that `select`.

## 9. Tests confirmed capable of failing

Every property below was broken deliberately — via monkeypatch in a scratch harness run
**outside the repository** (`~/scratch-c/verify_red.py`, not committed, not under
`AIDataAnalyst/`) — and the corresponding test confirmed to go red. 20 of 20.

| # | Property broken | Test that went red |
|---|---|---|
| 1 | A `MERGE … SET` added to `api.py` | `test_projections_are_written_only_by_projectors` |
| 2 | A projector write using `CREATE` instead of `MERGE` | `test_projection_writes_are_idempotent` |
| 3 | `policy_engine.py` starts reading the graph | `test_request_path_graph_access_is_read_only_and_closed` |
| 4 | `load_projection` made non-deterministic | `test_projection_rebuild` |
| 5 | `load_projection` drops the column rows | `test_the_rebuilt_projection_accounts_for_every_authoritative_row` |
| 6 | `enforce_organization` made a no-op in `api.py` | `test_cross_tenant_denial` (failed on `ExplodingSession`, i.e. it caught the DB access, not just the status code) |
| 7 | An anonymous route mounted | `test_every_route_requires_an_authenticated_principal` |
| 8 | `api.list_tables` loses its boundary check | `test_every_route_reaches_a_tenant_boundary_check` |
| 9 | `discover_datasource` loses its tenant scope | `test_every_background_worker_is_tenant_scoped` |
| 10 | SQL redaction disabled | `test_persisted_sql_has_literals_redacted_in_every_dialect` |
| 11 | A value-bearing column present in the schema | `test_no_mapped_column_is_named_for_a_source_value` |
| 12 | A profile snapshot gains `sample_values` | `test_profile_snapshots_carry_statistics_only` |
| 13 | The ingestion validator stops rejecting | `test_ingestion_rejects_value_bearing_attribute_keys` |
| 14 | A fourteenth unaudited mutation appears | `test_every_mutation_audits` |
| 15 | The audit record loses its tenant boundary | `test_audit_record_carries_every_attribute_the_invariant_names` |
| 16 | The gateway commits before auditing | `test_the_gateway_stages_its_audit_before_committing` |
| 17 | Oracle advertises `explain=True` | `test_advertised_capabilities_match_the_implementation` |
| 18 | A PLANNED connector advertises a capability | `test_planned_capability_is_displayed_as_planned` |
| 19 | The capability matrix leaks a planned capability | `test_the_capability_matrix_never_advertises_an_uncertified_capability` |
| 20 | An uncostable query allowed to execute | `test_a_connector_that_cannot_explain_is_refused_execution` |

`test_the_control_plane_scan_would_notice_a_leak` (INV-6) is the same idea made permanent:
it lives *in* the suite and fails if the leak detector ever stops looking at anything.

Six further tests exist purely as tripwires on the enumerations themselves
(`test_the_registry_is_populated`, `test_the_cypher_scan_finds_the_statements_it_is_supposed_to`,
`test_the_organization_scoped_route_set_is_not_empty`,
`test_the_mutation_set_is_derived_not_empty`,
`test_the_schema_reflection_actually_sees_the_schema`,
`iter_api_routes`'s built-in route-count assertion), because an enumeration that returns
nothing is the one way a suite of this shape passes while checking nothing.

## 10. Verification

```
$ ruff check tests
All checks passed!

$ pytest -q tests/test_tier0_invariants.py
25 passed

$ pytest                       # full suite
560 passed, 2 xfailed, 30 warnings in 13.31s
```

Per module: `test_tier0_invariants` 25 passed · `test_inv1_single_authoritative_store` 8
passed · `test_inv5_tenant_isolation` 51 passed · `test_inv6_value_freedom` 25 passed ·
`test_inv7_attributability` 9 passed + 1 xfailed · `test_inv9_capability_honesty` 27 passed
+ 1 xfailed.

**Zero failures, zero skips, zero errors.** The two xfails are §6.1 and §7.1.

**One environment note.** `aiosqlite==0.21.0` (already pinned in `pyproject.toml`'s `dev`
extra) was missing from the local `venv-atlas` and was installed with
`uv pip install --python …`. Without it the two workspace-authorization INV-5 tests added
concurrently to `tests/test_tier0_invariants.py` error at collection. No dependency change
was made to `pyproject.toml`.

## 11. Proposed tracker row

Replacement for row `ST-03` in `Docs/60-delivery/03-tracker.md` (this workstream does not
edit that file):

```markdown
| ST-03 | Tier 0 invariant suite (9 tests) | all | 0 | P0 | DONE | — | All nine invariants executable in the default `pytest` run with no external service, no skips — verified 2026-08-30 (`ruff check tests` clean; full suite 560 passed, 2 xfailed). `tests/test_tier0_invariants.py` keeps INV-2/3/4/8 plus workspace-level INV-5; INV-1, INV-5 (API surface), INV-6, INV-7 and INV-9 land in `tests/test_inv{1,5,6,7,9}_*.py` on shared harnesses in `tests/support/`. Data-driven throughout: all 199 FastAPI routes enumerated (44 organization-scoped ones driven with a foreign tenant against a session that raises on first use), every connector in the registry, every mapped column, every Cypher statement in `src/aida`. 20 of 20 properties confirmed capable of failing by deliberate mutation. Two strict xfails record codebase gaps, not suite gaps: 13 endpoints in `ai_registry_api`/`product_marketplace_api` commit governed state with no audit row (INV-7), and capability flags are hand-declared rather than derived from certification (INV-9, needs E12). INV-1's live rebuild drill (E5) and INV-6's full ingestion sentinel sweep still need infrastructure; both are proven in-process instead and say so. Detail: `Docs/review-2026-08/gap/06-tier0-invariant-suite.md` |
```

Row `TS-1` in the same file ("Same as ST-03") follows it.

## 12. Summary of changes requested under `src/`

None were made; this workstream owns only tests and this document.

| # | File | Change | Invariant |
|---|---|---|---|
| 1 | `src/aida/ai_registry_api.py` (6 handlers), `src/aida/product_marketplace_api.py` (7 handlers) | add `record_audit(...)` in the mutation's transaction | INV-7 (§6.1) |
| 2 | `src/aida/ingestion.py` | extend `connector_certification_evidence` with a check per capability flag; derive `default_capabilities` from the latest `ConnectorCertificationRun` | INV-9 (§7.1), blocked on E12 |
| 3 | `src/aida/workflows/activities.py` | add an explicit `organization_id` predicate to `plan_profile_tasks` | INV-5 (§8) |
| 4 | `Docs/10-architecture/01-principles-and-invariants.md` | state whether INV-7's "mutation" covers lazily-created default rows | INV-7 (§6.2) |
