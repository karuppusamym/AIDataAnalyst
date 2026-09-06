# Connection pool math and shared-worker budgets

Review 2026-09-05, section 5:

> **Connection pools** — Multiple process types consume connections → Document
> total pool math and measure against actual database limits.
>
> **Tenant fairness** — Large tenants can dominate shared sweeps → Tenant
> budgets, fair scheduling, oldest-backlog metrics.

This page is the arithmetic for the first and the settings contract for the
second. It is deliberately a *documentation and measurement* page: no behaviour
depends on it, and every number below is either read out of a file in this
repository (and says which) or is an input an operator has to supply for their
own deployment. Nothing here is an invented replica count.

## 1. Where a connection comes from

One process holds exactly one SQLAlchemy engine. `atlas.platform.db.get_engine`
is `@lru_cache`-decorated, so every `session_factory()` in a process — request
handlers, Temporal activities, background loops, projectors — draws from the
same pool. There is no second engine anywhere in `src/`: a repository-wide
search for `create_async_engine` finds one call site, in
`src/atlas/platform/db.py`.

That engine is built with:

| Setting | Env var | Default | Bounds | Where |
|---|---|---|---|---|
| `database_pool_size` | `AIDA_DATABASE_POOL_SIZE` | **10** | 1–100 | `src/atlas/platform/config.py` |
| `database_max_overflow` | `AIDA_DATABASE_MAX_OVERFLOW` | **20** | 0–200 | `src/atlas/platform/config.py` |

Neither is overridden anywhere in the repository — not in `compose.yaml`, not in
`.env.example`, not in `infra/k8s/base/configmap.yaml`. Every shipped process
therefore runs on the defaults.

So, per process:

```
steady_connections   = database_pool_size                       = 10
ceiling_connections  = database_pool_size + database_max_overflow = 30
```

`pool_size` is what the pool keeps open once warm; `max_overflow` is what it
will additionally open under burst and then discard. The ceiling is what
matters for exhaustion, because a burst is exactly when every process bursts.

## 2. Where the connections go

`compose.yaml` defines five long-lived processes that use the platform database,
each at one instance:

| Process | Command | Instances in `compose.yaml` |
|---|---|---|
| api | `uvicorn aida.main:app` | 1 |
| metadata-worker | `python -m aida.workflows.worker` | 1 |
| fleet-scheduler | `python -m aida.workflows.scheduler` | 1 |
| outbox-publisher | `python -m aida.projectors.outbox_publisher` | 1 |
| graph-projector | `python -m aida.projectors.graph_projector` | 1 |

`migrate` also connects, through Alembic's own engine (`migrations/env.py`), but
it is one-shot and every other service declares
`depends_on: migrate: service_completed_successfully`, so it never overlaps.
`seed` is behind the `seed` profile and is not part of a plain `docker compose up`.

The API process additionally runs two in-process background loops
(`_audit_archive_loop` and `_temporal_reconnect_loop` in `src/aida/main.py`).
They add no engine — they draw from the same 30 — but they do consume pool slots
concurrently with request handling, so the API's effective request concurrency
is slightly below its ceiling.

## 3. The formula

```
total_ceiling = Σ over process types:  replicas_i × (pool_size_i + max_overflow_i)
available     = max_connections − superuser_reserved_connections − other_clients
headroom      = available − total_ceiling
```

`other_clients` is everything else that connects to the same PostgreSQL
instance and is not an Atlas process: an operator's psql session, a monitoring
exporter, a backup agent, a BI tool pointed at the control plane. An operator
must supply it; this repository cannot know it.

## 4. The two topologies this repository actually defines

### 4.1 `compose.yaml` (local / demo)

The database is `pgvector/pgvector:pg17`. Nothing in this repository overrides
`max_connections`: `infra/postgres/init.sql` contains a single
`CREATE EXTENSION` statement and no `ALTER SYSTEM`. The image therefore ships
PostgreSQL's own defaults — `max_connections = 100`,
`superuser_reserved_connections = 3` — so **97** connections are available to
the `aida` role. Confirm rather than assume on any given host with
`SHOW max_connections;` (see §6).

| | Per process | × instances | Total |
|---|---:|---:|---:|
| Steady (`pool_size`) | 10 | 5 | **50** |
| Ceiling (`pool_size + max_overflow`) | 30 | 5 | **150** |

- Steady: 50 of 97 — **47 connections of headroom (48%)**.
- Ceiling: 150 of 97 — **oversubscribed by 53 connections (155% of available)**.

**What that means.** The local topology is comfortable at rest and cannot
service a simultaneous full burst of all five processes. It has not fallen over
because the five do not normally burst together: the API is idle while a
discovery runs, and the projectors are event-driven. The failure mode is not
gradual — it is `asyncpg` raising "too many connections for role" (or SQLAlchemy
`TimeoutError` waiting on checkout) on whichever process asks last.

**What would exhaust it.** Any three processes at their ceiling
(3 × 30 = 90) leaves 7 connections for the other two, which have a combined
steady requirement of 20. The realistic trigger is a large-source discovery —
the Temporal worker driving toward its ceiling — concurrent with a graph
projection rebuild of the same source and normal API traffic. That is not a
hypothetical combination: a discovery *emits* the event the projector rebuilds
from, so the two are correlated by construction.

**A second, related ceiling.** The Temporal worker
(`src/aida/workflows/worker.py`) constructs `Worker(...)` without
`max_concurrent_activities`, and `temporalio`'s default fixed tuner allocates
**100** activity slots. Every activity that touches the database takes a
connection from a pool whose ceiling is **30**. The worker's real concurrency
limit is therefore pool checkout, not activity slots, by a factor of ~3.3 — and
the 70 activities that cannot get a connection wait rather than fail, which
surfaces as activity-level latency rather than as a pool error. Either bound
the worker (`max_concurrent_activities=...`) or raise its pool deliberately;
leaving them mismatched means the visible symptom names the wrong resource.

### 4.2 `infra/k8s/base/` (the only Kubernetes manifests in the repository)

`infra/k8s/base/deployment.yaml` defines exactly one Deployment, `aida-api`,
with `replicas: 3`. There is **no** Deployment for the worker, the scheduler,
the outbox publisher or the graph projector.

| | Per process | × replicas | Total |
|---|---:|---:|---:|
| api steady | 10 | 3 | 30 |
| api ceiling | 30 | 3 | **90** |

Against a 97-connection server that is 93% of available at ceiling, with the
worker, scheduler and both projectors still to be accounted for. **This
repository does not define their replica counts, and this page will not invent
them.** An operator deploying them must complete the table:

```
total_ceiling = 3 × 30                       # api, from deployment.yaml
              + replicas_worker    × 30
              + replicas_scheduler × 30
              + replicas_outbox    × 30
              + replicas_projector × 30
```

and then either raise `max_connections`, lower `AIDA_DATABASE_MAX_OVERFLOW`, or
put a connection pooler (PgBouncer in transaction mode) in front. Note that a
transaction-mode pooler is not transparent to every feature — session-scoped
state such as advisory locks and `SET LOCAL` behaves differently — so that is a
deliberate architectural choice, not a configuration tweak.

### 4.3 A worked sizing example

For a deployment with 3 API replicas and 2 of each background process, on a
server sized at `max_connections = 500` with 20 connections reserved for
operators and monitoring:

```
total_ceiling = 3×30 + 2×30 + 2×30 + 2×30 + 2×30 = 90 + 240 = 330
available     = 500 − 3 − 20                      = 477
headroom      = 477 − 330                         = 147  (31% spare)
```

The same deployment on a stock `max_connections = 100` server has
`headroom = 77 − 330 = −253` and cannot run.

## 5. Shared-sweep budgets (tenant fairness)

The graph projector is a shared sweep: one process consumes one Kafka topic on
behalf of every tenant. Without a budget, one organization's burst of discovery
events is projected to completion before any other organization's single event
is looked at.

`aida.graph_projection.TenantFairQueue` buffers consumed events and round-robins
between organizations. The policy the review says needs product input is
expressed as two explicit, conservatively-defaulted settings, both read by
`TenantBudget.from_env`:

| Setting | Env var | Default | What it bounds |
|---|---|---:|---|
| `events_per_round` | `AIDA_GRAPH_PROJECTOR_TENANT_EVENT_BUDGET` | **8** | The maximum number of consecutive events one organization may be served **while another organization has work waiting**. A tenant alone in the buffer is never interrupted. |
| `max_buffered_events` | `AIDA_GRAPH_PROJECTOR_MAX_BUFFERED_EVENTS` | **512** | The size of the fair-share buffer. Reaching it is backpressure — the projector stops fetching until it drains — not an error. |

A third setting bounds the projector's memory per rebuild:

| Setting | Env var | Default | What it bounds |
|---|---|---:|---|
| chunk size | `AIDA_GRAPH_PROJECTION_CHUNK_ROWS` | **5000** | Source rows held in memory at once during a projection rebuild. Clamped to 50–50,000. |

These are read from the process environment rather than from `Settings` on
purpose: they are projector-process tuning knobs, and none of them changes what
the platform is willing to do — only how it paces itself. Their names are
distinct enough not to be flagged by `Settings.reject_unrecognized_aida_env_vars`,
which only rejects `AIDA_*` names that are *close* matches of real settings.

**8 is a starting point, not a finding.** It was chosen to be small enough that
no tenant waits long behind another and large enough that the single-tenant case
never pays for the machinery. The evidence to replace it with a measured value
is exported continuously:

| Metric | What it tells you |
|---|---|
| `aida_graph_projection_oldest_backlog_seconds` | How long the most-delayed tenant's oldest event has been waiting. If this stays near zero, the budget is not binding and does not need tuning. |
| `aida_graph_projection_backlog_events` / `_backlog_tenants` | Buffer depth and how many distinct tenants are competing. |
| `aida_graph_projection_tenant_budget_yields_total` | How often the budget actually preempted a tenant. Zero means one tenant is never contending with another and the policy is inert. |
| `aida_graph_projection_lag_seconds` | Event `occurred_at` → projection complete. The end-to-end freshness the budget trades against. |

The organization id of the most-delayed tenant is emitted as a **structlog
field**, never as a metric label. Finding F17 in the same review is about
unbounded metric label cardinality; a tenant identifier is exactly that shape.

## 6. How to measure this rather than believe it

The arithmetic above is a ceiling, not an observation. On a running system:

```sql
-- What the server will actually allow.
SHOW max_connections;
SHOW superuser_reserved_connections;

-- What is in use right now, by client application and state.
SELECT application_name, state, count(*)
FROM pg_stat_activity
WHERE datname = current_database()
GROUP BY application_name, state
ORDER BY count(*) DESC;

-- The high-water mark that matters: how close the busiest moment got.
SELECT count(*) AS in_use,
       current_setting('max_connections')::int AS server_limit
FROM pg_stat_activity
WHERE datname = current_database();
```

Sample the third query during a large-source discovery — the correlated-burst
scenario in §4.1 — rather than at rest. The number that would justify changing
any setting on this page is that peak, not the ceiling computed here.

The projection rebuild's own memory and wall-clock cost can be measured
directly with `scripts/scale_harness/gp1_measure_projection_memory.py`, which
runs the pre-change whole-estate load and the current chunked load against the
same synthetic estate and reports peak heap and duration for both.

## 7. What is not established here

- No load, soak or recovery test was run against this topology. Section 5 of the
  review says so of the whole system, and it remains true of this page.
- The `max_connections = 100` figure for `compose.yaml` is the documented
  default of the PostgreSQL image with nothing in this repository overriding it,
  verified by reading `compose.yaml` and `infra/postgres/init.sql`. It was not
  read back from a running server.
- The Kubernetes topology is incomplete in this repository (API only), so its
  total is a partial figure by construction, not a target.
