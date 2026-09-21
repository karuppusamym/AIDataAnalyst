# Workers and Workflows

> Status: Authoritative. Owner: Architecture.
> Scope: how background work is decomposed, scheduled, bounded, and recovered. This is where "hundreds of thousands of tables" becomes tractable.

## 1. The core idea

> **Review correction (2026-09-09):** The DAG below is a target decomposition, not proof that every stage has a dedicated worker. `workflows/worker.py` registers discovery and ingestion on one configured task queue, and `workflows/scheduler.py` starts both on the same one. Queue separation and fleet-wide isolation described later are deployment requirements until verified. This was **not** changed by the 2026-09-09 remediation pass, deliberately: [AR-09](15-agent-architecture-critical-review.md) closes on measured multi-tenant load, recovery and fairness experiments, and separating queues without them would look like progress while establishing nothing. Older dated worker-status rows are historical snapshots, not a current inventory.

**Metadata analysis is a distributed job/DAG execution problem, not an agent problem** (P7).

The naive design — one autonomous agent per table — fails on four counts: cost scales linearly with model calls, there is no dependency ordering (cross-table relationships need both tables profiled first), permissions become unbounded, and failure is unattributable.

The Atlas design: an **analysis run** expands into a **task DAG**, tasks are executed by **bounded, idempotent workers**, and models are invoked **selectively, on aggregated metadata, after deterministic work is complete**.

```mermaid
flowchart TD
    A[AnalysisRun created] --> B[Scope discovery]
    B --> C[DAG generation]
    C --> D1[Table task: profile]
    C --> D2[Table task: profile]
    C --> D3[Table task: profile]
    D1 & D2 & D3 --> E[Classification + key inference]
    E --> F[Cross-table relationship candidates]
    F --> G[Lineage extraction]
    G --> H[Quality baseline comparison]
    H --> I["Selective semantic inference<br/>bounded metadata batches"]
    I --> L[Proposal and evidence]
    L --> J{Independent governed decision}
    J -->|approved| K[Apply authorized change]
    J -->|rejected or deferred| N[Retain outcome and evidence]
    K --> M[Outbox → projections]
```

**Measured claims versus design intent.** Profiling fans out as bounded deterministic work; its actual cost depends on the source and profiling policy. Current semantic enrichment groups at most 25 tables per model call (`semantic_inference.py`). Enriching 100,000 selected tables therefore entails 4,000 nominal batch attempts before failures or retries, not the previously claimed roughly 50. This arithmetic is not a throughput benchmark. Confidence alone does not authorize publication; automated review has open boundary defects documented in AR-01 through AR-04.

## 2. Worker classes

> **Implementation status (2026-09-20).** Of the nine worker classes below, **four have
> running code** and five are target. Verified against `src/aida/workflows/`,
> `src/aida/projectors/` and `compose.yaml`:
>
> | Class | Today |
> |---|---|
> | Discovery | **Built** — `discover_datasource` activity, `DatasourceDiscoveryWorkflow` |
> | Profiling | **Built** — `plan_profile_tasks` / `profile_table_task` / `finalize_profile_tasks`; the fan-out DAG in §1 is real for this class |
> | Batch ingestion | **Built** — `MetadataBatchIngestionWorkflow`, `src/aida/batch_ingestion.py` |
> | Projection | **Built** — `projectors/graph_projector.py` and `projectors/outbox_publisher.py`, run as their own compose services behind the `events` / `graph` profiles. This class is the outbox → Neo4j path only: there is no vector or search projector (see the Semantic row) |
> | Classification | **Not a worker.** Deterministic rules run inline (`classify_column_name` in `workflows/activities.py`) |
> | Relationship | **Not a worker.** Candidate handling is request-path code in `src/aida/intelligence_api.py` |
> | Lineage | **Not a worker.** Ingestion is request-path (`openlineage.py`, `dbt_artifacts.py`). View and procedure definition parsing exists (`sql_lineage_parser.py`, `procedure_lineage.py`) but is called from API routes, the lineage agent and the scheduler's context-rebuild pass, not from a lineage worker. Per-source status is in `20-modules/09-lineage.md` |
> | Quality | **Not a separate worker.** Evaluation (`quality_service.py`) runs inside the `finalize_profile_tasks` activity and from `quality_api.py` |
> | Semantic | **Not a worker.** Inference is request-path (`semantic_inference.py`). Embedding generation exists but is not a worker: it fills the `embedding` table (`bytea` vectors, no `pgvector` column — ADR-0019), written by `rebuild_vector_index` in `src/aida/vector_index_service.py`, driven by the fleet-scheduler's `run_vector_index_rebuild_pass` and by `POST /v1/organizations/{organization_id}/retrieval/vector-index/rebuild` (`src/aida/retrieval_ops_api.py`). The pass skips and the endpoint refuses while `embedding_provider` is `unset`, the shipped default |
>
> The bounds in §3 and the DAG in §1 are accurate for the four built classes. Read them as
> target for the other five.

Split by failure and scaling characteristics, not by domain.

| Class | Work | Scaling axis | Isolation unit | Bounds |
|---|---|---|---|---|
| Discovery | Catalog inventory, drift, tombstoning | sources × objects | per source | objects per scan, scan duration |
| Profiling | Value-free statistics, sampling | tables × columns | per table task | rows sampled, column batch size, table count |
| Classification | Deterministic PII/sensitivity rules | columns | per column batch | batch size |
| Relationship | Candidate generation, evidence scoring | pruned table pairs | per candidate batch | candidate cap, no N×N full-value comparison |
| Lineage | Query-log/view/procedure parsing, OpenLineage, dbt | statements/artifacts | per artifact | statements per run, artifact size |
| Quality | Baseline comparison, incident lifecycle | tables × policies | per policy evaluation | policies per run |
| Semantic | Metadata-only inference, embedding generation | domains/objects | per proposal | tokens, objects per prompt |
| Projection | Outbox → Neo4j / vector / search | event throughput | per event | batch size, in-flight |
| Batch ingestion | Chunk processing, FK resolution, reconciliation | chunks | per chunk | chunk size, cumulative admission |

## 3. Bounds — the rule that makes scale safe

**Every worker operation has an explicit configured bound and returns a truncation reason when it hits one** (P3). Unbounded is a defect, not a performance characteristic.

| Bound | Default | Configurable |
|---|---|---|
| Profile sample rows per table | Adaptive by table size, hard cap | Down only |
| Columns profiled per batch | Configured | Yes |
| Tables per analysis run | Configured | Yes |
| Relationship candidates per table | Configured | Down only |
| Graph traversal | 1–4 hops, node/edge caps | Down only |
| Lineage statements per extraction run | Configured | Yes |
| Model tokens per inference | Per-route budget | Per route |
| Chunks per ingestion batch | 1,000 | Down only |
| Tables / columns per batch | 1M / 5M | Down only |
| Synchronous envelope | 100 catalogs / 50k tables / 250k columns | Down only |
| Query rows / bytes / seconds | Per workload class | Per LOB |

**Why "down only" appears so often.** A bound raised without certification is how a safe system becomes an incident. Raising a hard bound requires performance and privacy evidence, recorded in `60-delivery/03-tracker.md`.

## 4. Fleet scheduling

The scheduler decides *which source gets capacity next*. At thousands of sources, this is the difference between a platform and a queue.

| Concern | Mechanism |
|---|---|
| HA | Leader election by a PostgreSQL advisory lock: only the leader runs the loop, so a restart or a second replica does not double-schedule (see the status note below) |
| Priority | Per-source priority class |
| Fairness | Round-robin within priority class, so one huge source cannot starve the fleet |
| Maintenance windows | Per-source allowed windows; work is deferred, not failed |
| Quotas | Per-organization and per-LOB concurrency quotas |
| Admission control | A source at capacity is not admitted; requests queue with visible depth |
| Backpressure | Downstream saturation (worker pool, DB, source) reduces admission rather than causing failures |
| Cancellation | Cancel propagates to running activities and reconciles state |
| Bulkhead | **One source's failure never affects unrelated sources** |

> **Implementation status (2026-09-20).** Leader election exists (R11-AUD04). Every
> `fleet-scheduler` replica runs `run_scheduler` in `src/aida/workflows/scheduler.py`, but only the
> one holding a PostgreSQL session-level advisory lock calls `run_scheduler_iteration`; the mechanism
> is `src/aida/scheduler_leadership.py`. The lock is `pg_try_advisory_lock(0x61746C6173667363)` on
> one dedicated, long-lived connection kept outside the pool, so it is never handed to an unrelated
> request. A standby logs `scheduler_standby` once and retries every 5 seconds; a replica that
> acquires it logs `scheduler_became_leader`. The leader re-verifies the connection with a round trip
> before every iteration and fails closed: a dropped, timed-out or erroring connection means no
> leader, no new pass, `scheduler_lost_leadership`, and a return to retrying. The lock is released
> when the loop ends by cancellation or by an exception out of a pass. More than one replica is now
> safe to run. On a non-PostgreSQL dialect (the SQLite tests) a replica is the sole leader and logs
> `scheduler_leadership_not_enforced`.
>
> What this does not give you:
>
> * **Failover time** is the 5-second retry plus however long PostgreSQL takes to drop the old
>   session. When the leader's process dies the operating system closes its socket and PostgreSQL
>   drops the session at once. When its host or network disappears without closing anything,
>   PostgreSQL learns of it only through TCP keepalives, whose default is the operating system's
>   (about two hours on Linux); set `tcp_keepalives_idle`, `tcp_keepalives_interval` and
>   `tcp_keepalives_count` on the server to shorten it. The isolated leader stops itself on its next
>   check, so this is a scheduling gap, not a double run.
> * **It is exclusion, not fencing.** A pass already running when leadership is lost is allowed to
>   finish, so a new leader can overlap the old one by at most one iteration. Scan admission has its
>   own guard for that overlap: `process_scan_policy` claims each due `ScanPolicy` under a row lock,
>   advances its `next_run_at` in the same transaction, and starts the workflow under a deterministic
>   id.
> * **Cadence trackers restart on failover.** Several passes rate-limit themselves with in-process
>   trackers, which a standby never had, so a new leader runs each rate-limited pass once as soon as
>   it takes over. The footprint gauges are published only by the leader's process.
> * **The lock needs a stable session.** Point `AIDA_DATABASE_URL` at PostgreSQL directly or through
>   a session-mode pooler; behind a transaction-mode pooler the lock means nothing. It costs the
>   leader one connection on top of the pooled budget in `13-connection-pool-and-worker-budgets.md`
>   (a standby holds one only for the moment each retry takes), which that page's per-process
>   ceiling of 30 does not count.
> * **Test coverage is partial.** The loop is tested against a fake lock that replicas share
>   (`tests/test_scheduler_leadership.py`), and the provider against a fake engine. The advisory-lock
>   SQL runs against a real PostgreSQL only in a test that skips unless
>   `AIDA_SCHEDULER_LEADER_TEST_DATABASE_URL` is set, and **no failover drill has been run**.
>
> The loop is also more than fleet scheduling: `run_scheduler_iteration` runs 23 periodic passes
> (cancellation reconciliation, priority rebalancing, owner routing, rule packs, graph
> reconciliation, roll-up and vector-index rebuilds, quality freshness, change signals, expiry
> sweeps, delivery workers and others) before it admits due scan policies, so treat it as the
> platform's general maintenance loop.

**The bulkhead property is the most important one.** In a bank estate, some sources are always broken — a credential expired, a firewall changed, a database is in maintenance. A design in which those failures consume the shared worker pool degrades everything. Per-source isolation plus admission control keeps a broken source a *local* problem.

## 5. Idempotency and recovery

Every activity must satisfy:

| Property | Meaning | Test |
|---|---|---|
| Idempotent | Running twice produces the same state as once | Re-run an activity mid-workflow; assert no duplicates |
| Heartbeating | Long activities report liveness | Kill a worker mid-activity; assert timely detection |
| Resumable | Retry continues rather than restarts | Force restart mid-batch; assert processed chunks are not reprocessed |
| Cancellable | Cancellation leaves consistent state | Cancel mid-run; assert no partial writes and correct status |
| Bounded | Explicit caps with truncation reasons | Exceed a bound; assert explicit truncation, not silent partial |
| Attributable | Emits audit and evidence | Assert audit rows for every mutation |

### Batch ingestion recovery (worked example)

The most complex recovery path, delivered today:

1. A manifest declares `expected_chunks`. Chunks upload with checksums; numbers and keys are unique within the batch.
2. Finalization requires the exact sequence `1..expected_chunks` — no gaps.
3. Temporal owns execution with heartbeats and bounded exponential retries.
4. **Chunks commit independently**, so a retry resumes already-processed work.
5. Object fingerprints keep reapplication idempotent.
6. A second value-free pass resolves foreign keys whose referenced table arrived in a different chunk.
7. A `FULL` batch accumulates stable object identities across every chunk and runs omission reconciliation **only after all chunks succeed** — it can never retire metadata from a partial delivery.
8. On success, payload JSON is physically cleared (SQL `NULL`); only fingerprints, counts, statuses, and timestamps remain.
9. On failure, validated chunk payloads are retained for authorized retry, and a replacement analysis run is linked via `resumed_from_run_id`.

**Point 7 is the one to internalize.** A partial FULL delivery that ran reconciliation would soft-delete metadata that exists — data loss from a transient network failure. Deferring reconciliation until completeness is proven is what makes FULL safe.

## 6. Selective model invocation

Models are expensive, slow, and non-deterministic. The worker design minimizes calls without losing semantic value.

| Rule | Effect |
|---|---|
| Deterministic first | Never invoke a model for something a rule computes |
| Aggregate before invoking | Current implementation: up to 25 selected tables per call; grouping by domain/family is an optimization target |
| Only after deterministic completion | The model sees structure, keys, classifications, and baselines — not raw metadata |
| Metadata only | Identifiers, types, classifications, constraints, deterministic baselines. Never sample values (INV-6) |
| Structured output | Strict schema validation; malformed output is discarded, not repaired |
| Bounded | Tokens, retries, and timeout per route budget |
| Proposal only | Output enters the review queue, never authoritative state (INV-3) |

**Economics to measure.** Track selected tables, tokens per batch, failed attempts, deterministic fallbacks and cost per accepted result. Fixed-size batching still scales linearly with selected table count. Reuse and incremental selection can reduce volume; low-hundreds call counts are not established for a full 100,000-table enrichment run.

## 7. Worker deployment

| Deployment unit | Worker classes | Scaling signal | Failure mode |
|---|---|---|---|
| `atlas-worker` | Discovery, profiling, classification, relationship, lineage, quality, semantic | Temporal task-queue depth | Task retried on another worker |
| `atlas-projector` | Projection | Kafka consumer lag | Rebalance; offsets uncommitted |
| `atlas-scheduler` | Fleet scheduling, policy polling, periodic maintenance passes | Singleton by leader election; extra replicas stand by rather than scale | Standby takes over once PostgreSQL releases the old leader's lock; never drilled |
| `atlas-batch` (optional) | Batch ingestion (isolated when volume warrants) | Batch queue depth | Chunk-level resume |

The design intent is separate task queues per worker class, so a profiling backlog cannot starve projection and a slow source cannot delay quality evaluation.

> **Implementation status (2026-09-20).** The deployment-unit names above are target names, and
> the queue separation is not built. There is **one** Temporal task queue, `aida-metadata` (the
> `temporal_task_queue` setting in `src/atlas/platform/config.py`, set the same in
> `compose.yaml`), served by the single `metadata-worker` process
> (`src/aida/workflows/worker.py`). That worker registers `DatasourceDiscoveryWorkflow`,
> `MetadataBatchIngestionWorkflow` and their activities, so discovery, profiling and batch
> ingestion share one queue and one process. The classification, relationship, lineage, quality
> and semantic worker classes do not exist (§2), and there is no `atlas-batch` unit. Projection
> is not a Temporal queue: it is the `outbox-publisher` plus the Kafka-consuming
> `graph-projector`. The scheduler has no standby: see the status note in §4.

## 8. Observability requirements

Every worker class emits:

| Signal | Purpose |
|---|---|
| Task started / completed / failed / cancelled, with reason | Health |
| Duration histogram per task type | Capacity planning |
| Retry count and classification (transient vs. permanent) | Failure triage |
| Bound-hit counters with truncation reasons | Detects estates outgrowing configured limits |
| Per-source success rate | Fleet health scoring |
| Queue depth and admission rejections | Backpressure visibility |
| Projection lag per projection per tenant | INV-1 confidence |

SLOs in `10-architecture/10-performance-and-scale-model.md`.

## Related documents

- Event model: `10-architecture/07-event-and-messaging-model.md`
- Service extraction: `10-architecture/05-service-extraction-plan.md`
- Ingestion module: `20-modules/03-ingestion.md`
- Profiling module: `20-modules/05-profiling-and-classification.md`
