# Session Addendum -- 2026-09-04 -- Auto-enqueue AI drafts on ingest

> **Purpose.** New tracker row and evidence for the 2026-09-04 P0-01 fix
> (auto-enqueue AI drafts and semantic-inference on new-table ingest),
> staged here rather than merged into `03-tracker.md` directly because
> that file has extensive uncommitted concurrent edits and a landing
> here would conflict. Fold this row into `03-tracker.md` on the next
> tracker rebase; the file citations, event names and test names below
> are what belongs in the row's evidence column.

## Rows to add / update

### ING-4 -- Auto-enqueue AI drafts on new-table ingest (P0)

Section: **B. Connectors and ingestion** (extends the IN-1..IN-7
series; also referenced from the P0-01 finding in
`04-end-to-end-audit-2026-08-30.md`).

**Problem.** The 2026-08-30 end-to-end audit found that a newly
ingested table sits with no asset-description draft, no
business-annotation proposal, and no glossary-link candidate until a
steward manually POSTs each drafter endpoint. Ingest completes at
`_get_or_create_table` (`src/aida/workflows/activities.py:254-291`),
returning `created_table_ids` to the caller, but nothing consumes that
list to enqueue drafters. Impact: the "AI does the paperwork for you"
value proposition is silently gated on a manual step that stewards
regularly forget.

**Fix (Option B -- outbox event + Kafka-consumer handler).**

- `persist_discovery_snapshot`
  (`src/aida/workflows/activities.py:716`) now snapshots
  `snapshot_scope.created_table_ids` at entry and, at exit, emits one
  `catalog.table.newly_created.v1` outbox event per table it actually
  created this call (the diff, not the accumulator -- safe under
  chunked callers like `batch_ingestion._process_chunk` and the loop
  in `discover_datasource`). Emission is gated on
  `AIDA_AUTO_ENQUEUE_ON_INGEST` (default `true`; new setting in
  `src/atlas/platform/config.py`), and a batch-level
  `AUTO_ENQUEUE_DRAFTS_ON_INGEST` audit row is written alongside.

- New handler `src/aida/newly_created_table_drafter.py` consumes the
  event from the shared `aida.platform.events.v1` Kafka topic (same
  topic the graph projector reads) and calls the *service* functions
  of the two drafters directly (`gather_evidence`,
  `compose_draft_text`, `score_evidence`, `text_fingerprint`,
  `evidence_payload` -- never the HTTP endpoints, so no user-scoped
  `SecurityContext` or bearer token is required on the worker path).
  Reachability: `run_newly_created_table_drafter_consumer` is
  imported and started as a background asyncio task from
  `aida.workflows.worker.run_worker`, so the module is reachable
  from the existing `aida.workflows.worker` `ENTRY_POINTS` row in
  `tests/test_reachability_gate.py` without adding a new deployable
  or a new allowlist entry.

- Idempotency and safety:
  - Skip when the table already has an `AssetDocumentationVersion`
    with `status='APPROVED'` (stewarded description is source of
    truth; never overwrite).
  - Skip when an `AssetDescriptionDraft` with status `DRAFT` or
    `PENDING_APPROVAL` is already open on this table.
  - Skip when the handler has already produced *any* draft for this
    table -- catches at-least-once redelivery of the same event.
  - `handle_newly_created_table` DEFERS the semantic-inference half
    (records a
    `business_semantics.inference.auto_enqueue_deferred.v1` outbox
    row and a `DEFERRED` audit outcome, never a failure) when no
    `AnalysisRun` has reached `COMPLETED` for the datasource, so a
    later profiling-completion pass picks the table up rather than
    the auto-enqueue failing on a race with profiling.
  - Never swallows: an unexpected exception from the drafter is
    logged, recorded as a `FAILED` audit row, and re-raised so the
    Kafka consumer does not commit the offset (INV-6 shape --
    `type(exc).__name__` only, never `str(exc)`; same pattern
    `discover_datasource`'s exception handler uses).

**Evidence.**

- `src/atlas/platform/config.py`: new `auto_enqueue_on_ingest`
  setting.
- `src/aida/workflows/activities.py:716` (`persist_discovery_snapshot`)
  and the new `_emit_newly_created_table_events` +
  `NEWLY_CREATED_TABLE_EVENT_TYPE` helper right above
  `_mark_run_cancelled`.
- `src/aida/newly_created_table_drafter.py` (new; handler +
  Kafka-consumer loop).
- `src/aida/workflows/worker.py`: imports the handler and starts the
  Kafka-consumer loop as a background task when the setting is on.
- `Docs/30-contracts/04-event-catalog.md` -- Catalog section: three
  new rows -- `catalog.table.newly_created.v1`,
  `asset_description.draft.auto_enqueued.v1`,
  `business_semantics.inference.auto_enqueue_deferred.v1` -- so
  `tests/test_event_catalog_gate.py` (TS-11) does not fail on the
  new emissions.

**Tests.** `tests/test_auto_enqueue_on_ingest.py` (new), against a
real in-memory SQLite engine (aiosqlite), mirroring
`tests/test_catalog_pagination.py`:

- `test_ingest_of_three_new_tables_emits_three_newly_created_events`
- `test_reingest_of_the_same_tables_does_not_re_emit_events`
- `test_config_flag_false_suppresses_emission`
- `test_handler_is_idempotent_across_duplicate_events`
- `test_handler_skips_table_with_approved_description`
- `test_handler_defers_semantic_inference_when_no_analysis_run_completed`

**Design choice: Option B, not Option A.** A direct call from
`persist_discovery_snapshot` into the drafter service functions
(Option A) would work but has three drawbacks that the outbox
+ Kafka-consumer path avoids:

1. It couples ingest wall-clock to drafter wall-clock. A slow
   `gather_evidence` (lineage counts, dbt evidence, etc.) directly
   inflates the ingest activity's `start_to_close_timeout`.
2. It has no natural retry point separate from re-running ingest --
   a transient DB error during draft creation would force the
   caller to choose between failing the whole ingest activity and
   silently swallowing the drafter error.
3. It goes against the ADR-0001 "deterministic choke point"
   pattern that `record_outbox` + Kafka projectors is the codebase's
   standard async fan-out.

Option B matches the existing pattern used for lineage projection,
governance events, and semantic-proposal notifications
(`src/aida/projectors/graph_projector.py` and the `record_outbox`
call sites) -- one durable event on a shared topic, a projector per
consumer with its own retry/dead-letter behavior.

**Windows verification checklist (operator's box, Python 3.14).**

1. `uv sync` and `uv run alembic upgrade head` (no new migration --
   this row only adds a setting, an outbox event type and a handler
   module; no schema change).
2. `uv run pytest tests/test_auto_enqueue_on_ingest.py -v` -- expect
   all six tests green.
3. `uv run pytest tests/test_reachability_gate.py` -- expect green;
   the new module is reachable through
   `aida.workflows.worker.run_worker` importing it, no
   `ENTRY_POINTS` / `ALLOWLIST` edit needed.
4. `uv run pytest tests/test_event_catalog_gate.py` -- expect green;
   the three new `.v1` event types are documented in
   `Docs/30-contracts/04-event-catalog.md` Catalog section.
5. `uv run pytest tests/` -- expect all suites green.
6. Live smoke on the dev bank estate: register a datasource with a
   never-before-seen schema, run discovery, then
   `curl .../asset-description-drafts?status=DRAFT` and confirm a
   draft row exists for each newly discovered table (older approved
   descriptions untouched). Then flip
   `AIDA_AUTO_ENQUEUE_ON_INGEST=false`, add another new table, and
   confirm no auto-enqueue happens.

**Honest gap.** The Kafka-consumer half
(`run_newly_created_table_drafter_consumer`) is not exercised in
CI (same standing sandbox limit as the other Kafka-consumer
projectors -- see CN-3, IN-3); `handle_newly_created_table` itself
is fully covered against the real SQLAlchemy models and the real
service functions, and the consumer loop is a thin, standard-shape
`AIOKafkaConsumer` wrapper that mirrors `graph_projector.py`
line-for-line.
