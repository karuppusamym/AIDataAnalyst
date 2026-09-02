# CT-2 / PR-5 Scale Harness

Companion to `Docs/60-delivery/03-tracker.md` and
`Docs/60-delivery/08-infra-unblock-runbook-2026-09-02.md` (Phase 4's
"CT-2 / PR-5" entry, which deferred this exact harness pending a scale
decision). This directory is that harness. Nothing in it has been run —
this session had no live database or Docker daemon reachable (only a
connected-folder view of the repo on your machine) — so every claim below is
"here is what to run and what to expect", not "here is what happened". Run
it yourself, on your own Docker Desktop, and keep the output.

## The scale decision — read this first

The tracker's CT-2 and PR-5 rows both name a literal exit-condition scale of
**1,000,000 tables / 30,000,000 columns**. That is not something a laptop-grade
Docker Desktop instance can generate, hold, and profile in one sitting — and
the tracker's own P0/P1 delivery pressure doesn't leave room for a multi-day
data-generation run just to produce a number.

This harness instead targets **100,000 tables, averaging ~25 columns/table
(~2,500,000 columns total) — a deliberate 10% proxy of the literal target**,
PostgreSQL only (no cloud connectors — the same on-prem-only scope CN-3
already established for Docker-reachable live proofs).

**What this does and does not prove.** A flat-latency keyset-pagination
result at 100K tables is real evidence that the *mechanism* (composite
index + row-value keyset predicate) doesn't degrade with page depth — that
claim doesn't structurally change character between 100K and 1M rows, since
a B-tree index seek is O(log n) either way, not O(page depth). A clean
continue-as-new chain across 100K tables is real evidence the checkpoint
state machine and Temporal's event-history bound both hold up over many
hops, for real, against a real worker. Neither result is the literal
1,000,000/30,000,000 the tracker's exit condition names. **Running this
harness, however successfully, does not make CT-2 or PR-5 `DONE`.** Per the
tracker's own rule (`03-tracker.md`, "Rules" and the AU-2 live-call-site
rule): the exit condition is the exit condition, "code written" (or "proxy
scale proven") is not `DONE`, and a partial or smaller-scale proof must say
so honestly rather than be dressed up as full certification. Both rows
should stay `IN PROGRESS` after this harness runs, with the new evidence
appended to their existing text, not replacing "no live Postgres at
1M-table scale exists" with "DONE" — replace it with "a 100K-table live
proxy proof exists; the literal 1M/30M scale has still never run."

## What's in this directory

| File | Purpose |
|---|---|
| `README.md` | This file. |
| `ct2_generate_catalog.py` | Bulk-populates Aida's own catalog DB with 100K synthetic tables/~2.5M columns under one synthetic org. |
| `ct2_measure_pagination.py` | Calls the real `list_tables`/`list_columns` endpoint bodies in-process and reports keyset vs. OFFSET latency by page depth. |
| `ct2_cleanup.py` | Deletes the synthetic org `ct2_generate_catalog.py` created. |
| `compose.pr5-source.yml` | Standalone Postgres container — the *source* `PostgresConnector` discovers/profiles for PR-5. |
| `pr5_generate_source_tables.py` | Creates 100,000 minimal real tables inside that standalone container. |
| `compose.pr5-env.override.yml` | Adds the two env vars the dev stack's `api`/`metadata-worker` need to reach that source and raise the per-run table cap — see the file's own header comment. |

## What CT-2's piece proves

`list_tables` / `list_columns` / `list_catalog_rows` (`src/aida/api.py`)
share one keyset-pagination body, `_list_page` (`src/aida/api.py:1745-1802`):
cursor absent -> `OFFSET` branch (pays one `COUNT(*)` and an `OFFSET` that
grows with depth); cursor present -> keyset branch, a single
`tuple_(*columns) > tuple_(*last_values)` row-value predicate
(`src/aida/pagination.py:69-84`) against a composite index whose leading
columns match the endpoint's equality filters and trailing columns match its
`ORDER BY` exactly — `ix_metadata_table_ds_status_name_id` on
`(datasource_id, status, name, id)` for `list_tables`
(`src/atlas/modules/catalog/models.py:108-110`),
`ix_metadata_column_table_status_ordinal_id` on
`(table_id, status, ordinal_position, id)` for `list_columns`
(`src/atlas/modules/catalog/models.py:144-150`). That predicate shape is
exactly what a B-tree range seek satisfies in one index descent, independent
of how many rows came before the cursor — the claim `ct2_measure_pagination.py`
puts numbers to.

**Depth only exists where the pagination is scoped wide.** `list_tables`
paginates over every `MetadataTable` row for one `datasource_id` — with
100,000 tables under a single synthetic datasource (this harness's design),
that's genuine page-depth. `list_columns` paginates within one `table_id`'s
own columns (`src/aida/api.py:2034` filters on `table_id`, not
organization/datasource-wide) — with ~15-35 columns per table by design (this
harness's design, matching "2-3 columns" would have been unrealistic and
"25 avg" was the given target), there is no deep page story for columns to
tell; `ct2_measure_pagination.py` still exercises `list_columns`'s real
cursor path end-to-end, it just doesn't claim a depth result for it. Read
that script's own docstring for the fuller version of this point — it isn't
a gap this harness quietly worked around, it's what "depth" honestly means
for each endpoint given the real query shapes above.

**A gap this investigation found, not fixed:** `MetadataConstraint`
(`src/atlas/modules/catalog/models.py:182-201`) has no composite index
matching `list_constraints`' `ORDER BY (name, id)` filtered on
`(table_id, status)` — only `ix_metadata_constraint_org_type` on
`(organization_id, constraint_type)` exists, unlike the matching indexes
`MetadataTable`/`MetadataColumn`/`MetadataIndex`/`MetadataPartition` all
have. `list_constraints` is therefore not covered by this harness (constraint
counts per table are also small, so it wouldn't have a depth story either)
and its own claim of being "backed by composite indexes matching the
`ORDER BY`" does not hold today. Worth its own tracker note; out of this
harness's scope to fix.

## What PR-5's piece proves

`DatasourceDiscoveryWorkflow.run` (`src/aida/workflows/discovery.py`) runs
`discover_datasource` once, then loops `plan_profile_tasks` ->
`profile_table_task` (batched by `datasource.max_concurrency`) ->
`advance()`, calling `workflow.continue_as_new` once
`ProfilingProgress.tables_processed_this_execution` reaches
`settings.profile_continue_as_new_after_tables` (default 2,000 — see
`should_continue_as_new`, `src/aida/workflows/continuation.py:113-121`),
carrying forward only `ProfilingProgress.to_state()` — a keyset cursor plus
four counters, never a table-id list
(`src/aida/workflows/continuation.py:42-53`). `plan_profile_tasks`
(`src/aida/workflows/activities.py:1276`) itself paginates `MetadataTable`
rows for the run's datasource in bounded pages of
`settings.profile_plan_page_size` (default 500), enforcing
`settings.profile_max_tables_per_run` as the overall cap
(`src/aida/workflows/activities.py:1333`) — raised to allow up to 1,000,000
(`src/atlas/platform/config.py:149`) but still defaulting to 5,000 in dev,
which is why `compose.pr5-env.override.yml` raises it for this run.

At this harness's 100,000-table scale with the default 2,000-table
continue-as-new threshold, a full run should produce **~50
`continue_as_new` hops** — enough hops to show the event-history bound
holding, not a token gesture at 2-3 hops. `profile_table_task`
(`src/aida/workflows/activities.py:1406`) genuinely connects to the source
per table via `PostgresConnector`
(`connector_registry.create(datasource.connector_type, dsn)` — same call
site `discover_datasource` uses, `src/aida/workflows/activities.py:959`), so
this is a real, repeated connection-and-query workload against the source
container, not a stub.

**The real trigger, found rather than invented:**
`POST /datasources/{datasource_id}/analysis-runs`
(`src/aida/api.py:1434`, body `{"mode": "FULL"}`) calls
`reserve_analysis_run` then `_submit_analysis_workflow`
(`src/aida/api.py:407-421`), which calls
`client.start_workflow(DatasourceDiscoveryWorkflow.run, ...)` against the
real Temporal client at `src/aida/api.py:417-421` — a genuine, already-wired
entry point, not something this harness had to bolt on.

**What this harness could NOT determine, and does not paper over:** whether
`profile_table_task`'s sampling/profiling logic degrades gracefully against
a table with **zero rows** (every table `pr5_generate_source_tables.py`
creates is schema-only, no data — see "What to watch for" below). If it
does, you'll see 100,000 clean `SUCCESS` task outcomes; if some profiling
step assumes at least one sampled row, you may see per-table `ERROR`
outcomes instead, which the workflow's own retry policy
(`src/aida/workflows/discovery.py:76-89`) will retry and then, if it keeps
failing, surface as a failed task rather than silently drop. Either way,
that's a real finding this run will produce — not something to guess at now.

## Prerequisites

- Docker Desktop, running.
- This repo's normal Python dev environment (`uv sync`), since the scripts
  import `aida`/`atlas` directly — run them with `uv run python ...` from the
  repo root.
- Every command below assumes you're in the repo root, in the same shell you
  normally run `uv run pytest` from, with `AIDA_ENVIRONMENT=development` set
  (required by `atlas.platform.config.Settings` outside pytest — see
  `ct2_generate_catalog.py`'s own docstring for the exact reason).

```bash
export AIDA_ENVIRONMENT=development
```

## Phase A — CT-2 (self-contained; run this first)

Only needs Aida's own `postgres` service and its migrations — not the full
stack (no Temporal/Redis/Kafka/Neo4j needed for this phase).

```bash
docker compose up -d postgres
docker compose run --rm migrate

uv run python scripts/scale_harness/ct2_generate_catalog.py
# Takes a while at 100,000 tables / ~2.5M columns -- it prints progress every
# batch. Lower --tables (e.g. --tables 10000) first if you want a fast dry
# run of the harness itself before committing to the full 100K.

uv run python scripts/scale_harness/ct2_measure_pagination.py --json-out /tmp/ct2-results.json
```

Read the printed table: the `keyset p50`/`keyset p95` columns should stay
flat (no upward trend) from depth 1 to the deepest depth reported, while
`offset p50` should visibly grow with depth. That contrast — on the same
data, same run — is the CT-2 exit condition's claim, in numbers.

Want a literal page-50,000 depth instead of the default 1/1,000/last? You
need either more tables or a smaller page size (there are only
100,000/`--page-size` pages available):

```bash
uv run python scripts/scale_harness/ct2_measure_pagination.py \
  --page-size 2 --depths 1,1000,50000
# 50,000 sequential keyset hops in one process -- slower to run, but reaches
# the tracker's literal page number at this harness's table count.
```

When you're done and want to reclaim the disk/rows:

```bash
uv run python scripts/scale_harness/ct2_cleanup.py
```

## Phase B — PR-5, part 1: stand up the source (independent of Phase A)

```bash
docker compose -f scripts/scale_harness/compose.pr5-source.yml up -d
uv run python scripts/scale_harness/pr5_generate_source_tables.py
```

This creates 100,000 minimal tables (`id`, `label`, `created_at`) in a
standalone Postgres container on host port 55435 — a real, connectable
Postgres source, not a stub.

## Phase C — PR-5, part 2: real discovery/profiling run through Temporal

This part needs the full dev stack and cannot be scripted end-to-end from
here — it needs a running app, a running Temporal worker, and you watching
Temporal's own UI, exactly the "heavier items, deliberately not turnkey
commands" the infra-unblock runbook already flagged this pair as.

```bash
docker compose -f compose.yaml -f scripts/scale_harness/compose.pr5-env.override.yml up -d
```

Wait for `api` to report healthy (`docker compose ps`), then register the
synthetic org/lob/domain/project/datasource chain through the real API —
every one of these routes is a real, already-wired endpoint in
`src/aida/api.py`, not invented for this harness. Dev-mode auth trusts
whatever `X-Principal-Id`/`X-Roles`/`X-Organization-Id` headers you send
(`src/aida/security.py:95-107`) — `PlatformAdmin` satisfies every role check
below and bypasses the organization check, so one role header works
throughout:

```bash
AUTH=(-H "X-Principal-Id: scale-harness" -H "X-Roles: PlatformAdmin")
API=http://localhost:8000

ORG_ID=$(curl -sS "${AUTH[@]}" -X POST "$API/organizations" \
  -H "Content-Type: application/json" \
  -d '{"name":"PR-5 Scale Harness","slug":"scale-harness-pr5"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

LOB_ID=$(curl -sS "${AUTH[@]}" -X POST "$API/organizations/$ORG_ID/lines-of-business" \
  -H "Content-Type: application/json" \
  -d '{"name":"Scale Harness","code":"SCALEHARNESS"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

DOMAIN_ID=$(curl -sS "${AUTH[@]}" -X POST "$API/lines-of-business/$LOB_ID/data-domains" \
  -H "Content-Type: application/json" \
  -d '{"name":"Scale Harness Domain","code":"SCALEHARNESS"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

PROJECT_ID=$(curl -sS "${AUTH[@]}" -X POST "$API/lines-of-business/$LOB_ID/projects" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"Scale Harness Project\",\"slug\":\"scale-harness-pr5-project\",\"data_domain_id\":\"$DOMAIN_ID\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

DATASOURCE_ID=$(curl -sS "${AUTH[@]}" -X POST "$API/projects/$PROJECT_ID/datasources" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "scale-harness-pr5-source",
        "connector_type": "postgres",
        "dialect": "postgres",
        "environment": "DEV",
        "credential_reference": "env://AIDA_SCALE_HARNESS_PR5_SOURCE_DSN",
        "max_concurrency": 20
      }' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "org=$ORG_ID lob=$LOB_ID domain=$DOMAIN_ID project=$PROJECT_ID datasource=$DATASOURCE_ID"
```

`max_concurrency: 20` (default is 4) controls how many `profile_table_task`
activities `DatasourceDiscoveryWorkflow` runs in parallel per page
(`src/aida/workflows/discovery.py:69-99`) — raise it further if your machine
and the standalone source container can take it; leave it low if not, it
only affects wall-clock time, not correctness.

Now trigger the real run:

```bash
RUN=$(curl -sS "${AUTH[@]}" -X POST "$API/datasources/$DATASOURCE_ID/analysis-runs" \
  -H "Content-Type: application/json" -d '{"mode":"FULL"}')
echo "$RUN" | python3 -m json.tool
RUN_ID=$(echo "$RUN" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
```

### What to watch for and capture as evidence

1. **Poll run status** (this is the ONLY progress the app's own API surfaces
   mid-run — `profiled_tables`/`profiled_columns` on `AnalysisRunRead` are
   set once, at the very end, by `finalize_profile_tasks`
   (`src/aida/workflows/activities.py:1692-1693`), not incrementally per
   continue-as-new hop):

   ```bash
   watch -n 5 "curl -sS ${AUTH[@]} $API/analysis-runs/$RUN_ID | python3 -m json.tool"
   ```

   Capture the final JSON once `status` reaches `COMPLETED` (or `FAILED` —
   either is real evidence; a failure with a captured `error_class`/
   `error_message` is more valuable than no run at all).

2. **Continue-as-new count and mid-run `ProfilingProgress` values** — not
   visible through the REST API (see above), only through Temporal itself.
   Open Temporal Web UI at `http://localhost:8080`, find the workflow by the
   `temporal_workflow_id` from the JSON above (or search
   `discovery-$DATASOURCE_ID-*`), and open its event history. Each
   `WorkflowExecutionContinuedAsNew` event's history closes one execution and
   starts the next — count them (should be ~50 at 100,000 tables / 2,000
   per hop) — and each new execution's `WorkflowExecutionStarted` event's
   input carries the `ProfilingProgress.to_state()` payload
   (`cursor`/`tables_planned_total`/`profiled_tables`/`profiled_columns`) it
   resumed from — read a few of these across the run to confirm the
   counters are strictly increasing and the cursor is advancing, not
   resetting. If you have the `temporal` CLI installed,
   `temporal workflow show --address localhost:7233 --workflow-id <id>` is
   the same information from a terminal.

3. **No single execution's event history should approach Temporal's
   default 51,200-event hard limit** (or the ~10,240-event warning
   threshold) — that's the actual thing continue-as-new exists to prevent.
   Temporal Web UI shows each execution's event count in its summary.

4. **Wall-clock time** for the whole chain (first `WorkflowExecutionStarted`
   to the final `WorkflowExecutionCompleted`) — record it in whatever you
   send back with this evidence.

## Cleanup

```bash
uv run python scripts/scale_harness/ct2_cleanup.py
docker compose -f scripts/scale_harness/compose.pr5-source.yml down -v
```

No automated cleanup script was written for the `scale-harness-pr5` org —
it went through the real API/workflow, not a bulk-insert script, so there's
no single generator script to pair a cleanup script against. Every
`organization_id` foreign key in this schema is `ondelete="RESTRICT"`
(`src/atlas/modules/identity_tenancy/models.py`,
`src/atlas/modules/connectivity/models.py`), so the org row itself can't be
deleted until everything under it is gone — but `DataSource`'s catalog
children cascade from the datasource (`MetadataCatalog.datasource_id`,
`MetadataSchema.catalog_id`, `MetadataTable.schema_id`,
`MetadataColumn.table_id` are all `ondelete="CASCADE"`), so deleting the
`DataSource` row is enough to remove everything `discover_datasource`
persisted. In `psql` against the main `postgres` service, bottom-up:

```sql
DELETE FROM datasource WHERE id = '<DATASOURCE_ID>';   -- cascades catalog/schema/table/column
DELETE FROM project WHERE id = '<PROJECT_ID>';
DELETE FROM data_domain WHERE id = '<DOMAIN_ID>';
DELETE FROM line_of_business WHERE id = '<LOB_ID>';
DELETE FROM organization WHERE id = '<ORG_ID>';
```

## Sending results back

Paste (or attach) `ct2_measure_pagination.py`'s printed table and
`/tmp/ct2-results.json`, and PR-5's final `analysis-runs/$RUN_ID` JSON plus
the Temporal UI's continue-as-new count and total wall-clock time. That's
what turns "IN PROGRESS, no live Postgres at scale" into "IN PROGRESS, a
100K-table live proxy has now run with the following numbers" in
`03-tracker.md` — evidence, not a status flip.
