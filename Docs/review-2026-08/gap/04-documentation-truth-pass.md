# Documentation Truth Pass — `gap/02` item C10

Status: **Applied 2026-08-30.** This is a record of changes already made to `Docs/`, not a
proposal. Scope: `00-product/`, `10-architecture/` (excluding ADR-0005/0017/0018),
`20-modules/` (excluding `01-identity-and-tenancy.md`), `30-contracts/`, `40-engineering/`,
`90-reference/`, `Docs/README.md`.

Method: ground truth was taken from the code — `find`, `grep` for each claimed symbol,
`compose.yaml`, `pyproject.toml`, `.github/workflows/ci.yml`, `migrations/versions/` — never
from another document. `gap/01-baseline-reality.md` was used to aim the search, not as
evidence; where it has itself gone stale (§6 below) the code won.

**The device.** Corrections are a blockquote beginning **"Implementation status (2026-08-30)"**
placed at the top of the affected section, stating what is true today and naming the file that
proves it. Design prose underneath is unchanged. 28 such callouts were added, plus inline
`— **planned, not wired**` clauses where a single table cell was the whole defect. Nothing was
deleted except two factually wrong tokens (`legal_entity` in a tenancy path, `1,530 lines` and
its siblings), and both removals are annotated in place.

---

## 1. The shape of what was wrong

The headline finding — 21 modules documented, 1 built — is real but was the *least* misleading
thing in the set, because `README.md` and `20-modules/00-module-index.md` already flagged it.
Four other patterns did more damage:

1. **Named tests that do not exist.** `10-architecture/01-principles-and-invariants.md` opens
   with "An invariant without an automated test is a wish", then names a test for each of the
   nine invariants. Four of those functions exist nowhere in the repository. The document's own
   standard convicts it.
2. **An event catalog that does not match the emitted events.** The single largest divergence
   found. See §3.
3. **"Fails CI" written before CI existed.** CI landed 2026-08-30 with five gates; nine
   documents named gates that still have no job. `40-engineering/03-coding-standards.md` had
   already been corrected by an earlier pass; the other eight had not.
4. **Stale-by-half metrics.** The two documents that argue *for* decomposition were citing line
   counts roughly half of actual, understating their own case.

---

## 2. Changes by document

### `10-architecture/01-principles-and-invariants.md`

| Claim | Evidence it was false |
|---|---|
| Each of INV-1…INV-9 has a named automated test | Grepped every named function across the repo. **`test_projection_rebuild`, `test_no_source_values_in_control_plane`, `test_every_mutation_audits`, `test_capability_matrix_matches_certification` do not exist.** Tests exist for INV-2 (×2), INV-3, INV-4 (×2), INV-5, INV-8. Each absent test now marked **Planned, not written** in place |
| INV-5: "every governed record carries … legal entity …" | `legal_entity` has **zero matches** in `src/` and in `migrations/`. Removed from the statement, with the deletion annotated |
| INV-5 enforcement: "Repository base class requires a tenant scope argument" | **No `Repository` class and no `TenantScope` type exist** in `src/aida/` or `src/atlas/platform/`. Scoping is per-query convention. Marked Planned; the new route-driven test is named as what substitutes for it |
| INV-1 test deletes "the search index" | There is no search index — no dependency, no service, no client. Noted inline |

Kept truthful and left alone: the INV-2 section, which an earlier pass had already corrected and
which I re-verified (the import-linter contract is in `pyproject.toml` and permits exactly one
importer).

### `10-architecture/04-module-decomposition.md`

| Claim | Evidence |
|---|---|
| The 21-module structure, §3 onward, in the present tense | `src/atlas/modules/` contains **one** directory; `identity_tenancy/service.py` is 7 lines reading "Status: scaffold only". Whole-document status block added at the top |
| "`src/aida/` with ~18,000 lines … `models.py` (1,274) … `schemas.py` (1,298) … `api.py` (1,530)" | Re-measured: **~36,500 / 2,721 / 2,222 / 1,837**. Every figure was roughly half of actual. Corrected, with a note that the problem is growing while extraction is deferred |
| §5.2's six import-linter contracts "live in `pyproject.toml` and fail CI" | **None exists in the form described.** `pyproject.toml` has four contracts, at flat-package addresses; `layers`, `no-orm-leakage`, `platform-purity`, `no-cycles` are absent. Replaced with a table of what is actually wired, and `pyproject.toml`'s own comment explaining why a layering contract over `aida` is deferred |
| §6 "one schema per module", 20 schemas | **No `schema=` on any `__table_args__` in `models.py`; no schema reference in any migration.** Everything is in the default schema. MD-1's stated enforcement is not in force |
| §6 tenancy columns include `legal_entity_id` | Absent everywhere. Removed and annotated |
| §7 module anatomy incl. `migrations/` and `pytest src/atlas/modules/<name>` | The one real module has **no `migrations/` directory**; all 34 revisions are in the root `migrations/versions/`, and `pyproject.toml` sets `testpaths = ["tests"]` |
| — (added) | `mypy` is configured `packages = ["aida"]`, so **`src/atlas/` is not type-checked** |

### `10-architecture/06-data-architecture.md` and `03-logical-architecture.md`

Both list seven stores as though deployed. Verified per store:

| Store | Finding |
|---|---|
| pgvector | **Extension only.** `infra/postgres/init.sql` runs `CREATE EXTENSION … vector`, but no embedding column exists in any model or migration and nothing reads or writes a vector. `retrieval.py`'s own comment defers it to "Phase 2 when the embedding column is added" |
| Search index | **Does not exist.** No search-engine dependency, service or client anywhere |
| Object storage | **Not wired.** MinIO runs in `compose.yaml`, but there is **no object-storage client in the dependency list** (no `boto3`, no `minio`) and nothing in `src/` touches it. Profiling artifacts, evidence packs and the WORM archive are all target |
| PostgreSQL / Neo4j / Kafka / Redis | Genuinely wired — driver dependency, compose service and calling code for each |

`06`'s tenancy path also carried `legal_entity`; removed, with a pointer to ADR-0018 and module
01 as the authority rather than a restatement (that work is in flight).

### `10-architecture/07-event-and-messaging-model.md`

| Claim | Evidence |
|---|---|
| Eight topics, `atlas.catalog.v1` … `atlas.audit.v1`, each with its own partition key | **One topic.** `projectors/outbox_publisher.py` publishes every outbox row to `aida.platform.events.v1`, keyed by `aggregate_id`, event type in a Kafka header. No `atlas.*` string appears in `src/`. The broker-ACL isolation model that depends on per-topic keys is therefore also target |
| Eight Temporal workflows (§8) | **Two.** `@workflow.defn` yields only `DatasourceDiscoveryWorkflow` and `MetadataBatchIngestionWorkflow`. `ProjectionRebuild` in particular does not exist — the same hole as INV-1's missing test and the never-run rebuild drill |
| Schema-registry `BACKWARD` compatibility "checked in CI" | No schema registry in `compose.yaml` or the dependency list; no such CI step |

### `10-architecture/08-workers-and-workflows.md`

Nine worker classes documented; **four have running code** (Discovery, Profiling, Batch
ingestion, Projection). Classification, Relationship, Lineage, Quality and Semantic are
request-path code, not workers — verified by locating each in `intelligence_api.py`,
`quality_service.py`, `semantic_inference.py`, `openlineage.py`. "Embedding generation" cannot
exist because there is no embedding column.

### `10-architecture/09-deployment-topology.md`

`atlas-api`/`-worker`/`-projector`/`-scheduler` are target names; `compose.yaml` runs `api`,
`metadata-worker`, `fleet-scheduler`, `outbox-publisher`, `graph-projector` — five processes,
not four, and `src/atlas/entrypoints/` does not exist. **`atlas-connector-agent` does not exist
in any form**, which also makes the `connector_agent.*` events in the event catalog
unimplementable today. "SAST, DAST, dependency and container scans in CI" — none is wired and
no such tool is in the `dev` extras.

### `10-architecture/10-performance-and-scale-model.md` and `00-product/01-vision-and-goals.md`

"CI fails on regression beyond these thresholds" — there is no performance job, no
`tests/performance/`, and **no p95 in the document has ever been measured**, so there is no
baseline to regress against. Goal G4 ("prove scale rather than assert it") is annotated as the
one goal currently unmet, which is the honest reading of its own wording.

### `20-modules/00-module-index.md`

Gained two code-sourced columns — **"Module dir?"** and **"Lives today in"** — one row per
module, each verified against `src/`. Two notes were added because the table is easy to
misread: the columns answer only "does the directory exist", and the two axes are independent —
**modules 16 and 19 are among the best-implemented parts of the platform and have no module
directory at all, while module 01 has the only directory and the least of it filled in.**

The row that matters most: **module 18 (studio) has no code of any kind** — zero matches for
`studio` in `src/` or `ui/`.

### `20-modules/09-lineage.md` and `30-contracts/06-lineage-contract.md`

The sharpest finding after the event catalog.

| Claim | Evidence |
|---|---|
| `edge_kind ∈ {QUERY, VIEW, PROCEDURE, ETL, DBT, BI, AI_DECISION}` | `edge_kind` is a free-text `String(30)` defaulting to `"ETL"`, and **the only value the code ever assigns explicitly is `"SUGGESTED_RELATIONSHIP"`** (`intelligence_api.py:1125`, `unified_lineage_api.py:750`) — a value absent from the documented enum. None of `QUERY`, `VIEW`, `PROCEDURE`, `DBT`, `BI`, `AI_DECISION` is ever written. No DB constraint restricts the column |
| "Partitioned by time" | No `PARTITION` in any migration |
| View and stored-procedure lineage from definitions | **No parser exists** — searched `parse_view`, `view_ddl`, `CREATE VIEW`, `parse_procedure`, `procedure_body`; no matches |
| BI lineage from Tableau/Power BI/Looker | No extractor, connector or ingestion path |
| `record_query_lineage` / `record_ai_decision` in the public interface | **Neither function exists.** Marked `# PLANNED - no such function` in the interface block |
| "Each decision becomes an `AI_DECISION` edge … traversable in the same graph" | No `AI_DECISION` edge is ever written. Agent runs are audited but not projected into the lineage graph |

Genuinely built and now stated as such: query lineage from executed SELECTs
(`extract_column_lineage()`, DIRECT vs DERIVED), OpenLineage ETL ingestion, dbt manifests.

### `20-modules/13-agent-runtime.md`

All eleven states exist with a real transition table, and **SCREENED-before-retrieval is
verified** (screening at `agent_orchestrator.py:236`, retrieval at `:308`). But `VALIDATED`,
`COSTED`, `EXECUTED`, `EXPLAINED`, `COMPLETED` are appended in a single `for` loop at
`:532-538`, *after* `query_gateway.execute()` returned. The work is real — it happens inside the
gateway — but at the orchestrator level these are **retroactive trace entries, not five
checkpoints**, so the table's "Failure behaviour: Deny" column describes gateway behaviour, not a
runtime gate. Annotated, with a pointer to `gap/02` row C3.

### `30-contracts/04-event-catalog.md`

The largest divergence in the set. Extracted every `event_type=` passed to `record_outbox`
across `src/aida/` and compared:

- The platform emits **~55 event types, all `.v1`-suffixed** — `datasource.registered.v1`,
  `metadata.discovery.snapshot.v1`, `query.execution.completed.v1`,
  `relationship_candidate.decided.v1`, `governance.review_requested.v1`, `workspace.created.v1`, …
- **Most catalogued rows match nothing.** Confirmed absent: `principal.created`, `tenant.created`,
  `ingestion.delivered`, `catalog.object.created`, `catalog.object.changed`, `profile.completed`,
  `classification.assigned`, `key.inferred`, `relationship.candidate_generated`,
  `relationship.approved`, `table_family.detected`, `semantic.proposal_created`,
  `lineage.edge_created`, `quality.observation_recorded`, `quality.sla_breached`,
  `agent.run_started`, `execution.requested`, `model.route_version_created`,
  `model.kill_switch_engaged`, `policy.version_published`, `audit.event_recorded`,
  `graph.rebuild.started`, `retrieval.index_lagging`.
- **The Semantics-and-glossary section is the exception** and is broadly accurate; its `.v1` rows
  were written against the code.
- "Publishing an uncatalogued event fails CI" is false, and **that missing gate is why the drift
  accumulated unnoticed** — the causal link is now stated in the document.

I did **not** rename anything. Consumers key on the emitted names, so reconciliation is a code
decision, not a docs edit. Raised as a tracker row in §5.

### `30-contracts/01`, `02`, `03`

OpenAPI schema diff, docs lint, and fake-parity all marked planned: no such CI steps exist, no
released spec is committed to diff against, and there are no module `contracts.py` fakes to run
parity against (the one that exists is a 9-line stub).

### `40-engineering/01-development-spec.md`

The "read before writing code" document told an engineer to put code in a directory that does
not exist. §5 now routes via the module index's "Lives today in" column, and §6 states that a
generated module is currently **inert**: no module schema, `migrations/` not wired into Alembic,
`TenantScope` has no base class, `tests/` outside `testpaths`, and "add its import-linter layer"
is impossible because no layers contract exists.

### `40-engineering/02-repository-layout.md`, `04-testing-strategy.md`, `05-ci-cd-and-release.md`

Line counts corrected (same as §04-module-decomposition). Target-layout, module-layout and
test-placement sections marked target, with the four `tests/` subdirectories noted as absent —
`tests/` is flat: 44 files, 339 functions. The Tier-0 table gained a **Status** column with the
two extra real tests the document omitted. Tiers 2, 4 and 5 do not exist at all. Merge gates:
**five of fourteen are wired**, tabulated gate by gate.

### `90-reference/01-glossary.md`, `00-product/07-packaging-and-editions.md`

`legal entity` removed from the tenancy-hierarchy definition and from the metering line, both
annotated. Also noted that **metering itself is not implemented** — no meter, usage record or
showback surface in `src/` (grep returned only `parameters` false positives).

### `Docs/README.md`

A dated pass note stating the convention, so a reader knows what an "Implementation status"
blockquote means and that prose around it may describe intent.

---

## 3. Claims I could not verify

| # | Claim | Where | Why unresolved |
|---|---|---|---|
| U1 | Relationship-candidate **inference algorithm** — whether scoring runs in-platform or candidates arrive pre-approved | `20-modules/06`, `90-reference/04` | `graph_projector.py` handles approve/reject events and `intelligence_api.py` holds `RelationshipCandidate`, but I did not locate a scoring implementation. `gap/01` §4 row 9 reached the same impasse. Left unannotated rather than guess |
| U2 | Whether the ~55 emitted event names are a deliberate scheme or accretion | `30-contracts/04` | Needs an author, not a grep. Determines whether reconciliation renames code or restates the catalog |
| U3 | `90-reference/04-analysis-algorithms.md` scoring weights and thresholds | `90-reference/04` | Prose describes algorithms at a level that does not map 1:1 to functions. Would need a line-by-line read of `intelligence_api.py` and `semantic_inference.py` against each formula — larger than this pass. **Left untouched; still unverified** |
| U4 | Whether Neo4j projection covers approved *inferred* relationships or only declared FKs | `20-modules/10` | Module 10's own §11 already says "Approved inferred relationships — Not projected". Consistent with the code I read, so left as-is, but I did not prove the negative |
| U5 | `10-architecture/11-capacity-and-cost-model.md` sizing tiers and cost figures | `10-architecture/11` | Unfalsifiable from code — they are estimates, not claims about the system. Deliberately not annotated |
| U6 | `00-product/03,04,05` competitor claims | `00-product/` | Out of scope for a code-truth pass; they are claims about other vendors |

---

## 4. Two things the orchestrator should know

**The tree moved under me.** Three claims I wrote were correct when written and stale within the
hour, because the concurrent tenancy session is landing code:

- `test_cross_tenant_denial` (INV-5) **did not exist** at the start of this pass and **exists
  now** (`tests/test_inv5_tenant_isolation.py`, route-table-driven). I corrected my own edits in
  `01-principles-and-invariants.md` and `04-testing-strategy.md`; the invariant tally is now
  **5 of 9 built, 4 planned**.
- A **fourth** import-linter contract (`C4 / ST-11 lineage and intelligence modules never import
  the query gateway`) landed mid-pass. Every "three contracts" reference was updated to four,
  and the affected callouts now tell the reader to re-read `pyproject.toml` rather than trust
  the count.
- `src/aida/sql_validation.py` and `sql_validation_api.py` (review item **N14, `validate_sql`**)
  appeared during the pass. **I did not document them** — module 16's spec is mine but this is
  another stream's capability and theirs to describe. Flagging so it is not lost.

**`gap/01-baseline-reality.md` is now partly stale** and is the document the review tells people
to read first. Three of its statements are no longer true: CI does not exist (it does,
`.github/workflows/ci.yml`); the gateway-exclusivity contract is not wired (it is); workspaces
have no model or table (`Workspace`, `WorkspaceMembership`, `SourceBinding` are in `models.py`
and in `migrations/versions/f1a2b3c4d5e6_adr_0018_three_axis_tenancy.py`). Its §1 line counts
(34,669 LOC / ~78 modules) are also low against today's 36,465 / 87. I do not own that file. It
needs a dated "superseded in part" header or the same treatment applied here.

---

## 5. Proposed tracker rows

For `Docs/60-delivery/03-tracker.md`. I do not own that file; these are ready to paste.
Column formats match the target sections — section A carries a `Mod` column, section H does not.

### 5.1 Change one existing row (section A)

`ST-03`'s exit condition says "formalizes 4 of 9". INV-5's test landed 2026-08-30, so it is now
5 of 9 and the remaining list is shorter by one:

```
| ST-03 | Tier 0 invariant suite (9 tests) | all | 0 | P0 | IN PROGRESS | — | `tests/test_tier0_invariants.py` plus `tests/test_inv5_tenant_isolation.py` formalize 5 of 9 (INV-2, INV-3, INV-4, INV-5, INV-8), all passing and unskipped. INV-5 landed 2026-08-30 as a route-table-driven suite that also asserts every route is authenticated, every route reaches a tenant-boundary check, and every background worker is tenant-scoped, each with a closed exemption list. Remaining 4: INV-1/INV-6 need a live Neo4j+search replay harness; INV-7 needs an all-endpoints mutation/audit harness; INV-9 needs a certification-result store that doesn't exist yet |
```

### 5.2 New rows — section A (Structural foundation)

```
| ST-12 | Documentation truth pass (`gap/02` C10) | all | 0 | P0 | DONE | — | Applied 2026-08-30. Every structural claim in `00-product/`, `10-architecture/`, `20-modules/`, `30-contracts/`, `40-engineering/`, `90-reference/` and `Docs/README.md` is either true of the code or carries a dated `Implementation status` callout naming the file that proves otherwise. 28 callouts added; `20-modules/00-module-index.md` gained code-sourced `Module dir?` and `Lives today in` columns for all 21 modules. Record and evidence: `Docs/review-2026-08/gap/04-documentation-truth-pass.md` |
| ST-13 | Refresh `gap/01-baseline-reality.md` against the post-Phase-0 tree | all | 0 | P1 | TODO | — | Three of its claims are now false (CI absent, gateway-exclusivity contract unwired, no workspace model) and its LOC figures are low. Header dated and corrected, or a "superseded in part" note added |
| ST-14 | Reconcile emitted event names with `30-contracts/04-event-catalog.md` | all | 0 | P1 | TODO | — | Decide per event whether to rename the emitted `.v1` type or restate the catalog row, then land whichever. Exit: every `event_type=` argument in `src/` appears in the catalog and vice versa. Blocked on the U2 authorial question in `gap/04` §3 |
| ST-15 | `edge_kind` vocabulary matches the lineage contract | 09 | 0 | P1 | TODO | — | Code assigns only `SUGGESTED_RELATIONSHIP` (absent from the documented enum) and defaults to `ETL`; `QUERY`/`VIEW`/`PROCEDURE`/`DBT`/`BI`/`AI_DECISION` are never written. Exit: one agreed vocabulary, a DB-level constraint enforcing it, and `30-contracts/06` matching |
```

### 5.3 New rows — section H (Testing, performance and certification)

Section H has no `Mod` column.

```
| TS-11 | Event-catalog CI gate | 0 | P0 | TODO | — | A test asserts every `event_type=` published from `src/` appears in `30-contracts/04-event-catalog.md`. This is the gate whose absence let the catalog drift; cheap, and it stops the drift recurring after ST-14 |
| TS-12 | Doc-claim regression test for named artefacts | 0 | P1 | TODO | — | A test asserting that every test function name, module path and import-linter contract name cited in `Docs/` resolves to something real. Exit: the class of defect fixed by ST-12 cannot silently return |
```

### 5.4 Row to retire

`TS-2` ("Reflection-generated tenant denial coverage / Every endpoint and worker") is satisfied
by `tests/test_inv5_tenant_isolation.py` as of 2026-08-30. Mark `DONE` with that file as the exit
evidence, or merge it into `ST-03`.

---

## 6. What was deliberately not done

- **No design was deleted.** Every target structure is intact; it is now labelled.
- **Tenancy was not restated.** `20-modules/01-identity-and-tenancy.md`, ADR-0005/0017/0018 and
  `migrations/` belong to the concurrent session. Where an owned document asserted a tenancy
  shape, it now points at module 01 and ADR-0018 as the authority instead of competing with them.
  The one exception is `legal_entity`, removed wherever it appeared because it is verifiably
  absent from the code and `gap/02` C2/D3 recommends never building it.
- **`60-delivery/**` and `review-2026-08/**` were not edited** beyond creating this file.
- **`90-reference/04-analysis-algorithms.md` was left untouched** — see U3.
