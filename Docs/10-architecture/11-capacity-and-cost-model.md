# Capacity and Cost Model

> Status: Authoritative planning model. Owner: Platform.
> Sizing, workload isolation, backpressure, cost governance, and the metric set. Migrated and updated from the retired flat `06-deployment-scaling-operations.md`.

## 1. Workload classes and connection isolation

The most important operational rule in this document: **a metadata profiler must never consume the warehouse connections interactive users need.**

Separate connection pools are maintained per `datasource × environment × credential × workload class`, each with its own concurrency and cost limits.

| Class | Purpose | Priority |
|---|---|---|
| `INTERACTIVE_QUERY` | Analyst and tool execution | Highest — protected under contention |
| `METADATA_DISCOVERY` | Catalog inventory | Medium |
| `PROFILING` | Bounded statistics | Low — yields to interactive |
| `RELATIONSHIP_VALIDATION` | Evidence checks | Low |
| `LINEAGE` | Query-log and definition extraction | Low |
| `ADMIN` | Operator queries | Medium |

Class is a required field on every `ExecutionRequest` (module 16). A request without one is rejected, not defaulted.

## 2. Task queues

Separate queues per worker class, so a profiling backlog cannot starve projection and a slow source cannot delay quality evaluation.

```text
metadata.discovery        profile.table            profile.column
relationship.candidate    relationship.validate    lineage.extract
semantic.enrich           embedding.generate       semantic.publish
quality.evaluate          batch.chunk              audit.events
```

## 3. Capacity model

| Tier | Scale | Topology |
|---|---|---|
| **Small** | ≤ 1,000 tables · ≤ 25,000 columns · ≤ 5 concurrent users | 1 API, 1 PostgreSQL, 1 Neo4j, Redis, 4–10 workers |
| **Medium** | 1,000–20,000 tables · 25K–500K columns · 10–200 users | Autoscaled workers, durable queue, read replicas, larger Neo4j, partitioned analysis workloads, object-storage artifacts |
| **Large** | 20,000–100,000+ tables · 500K–5M+ columns · multiple LOBs | Metadata partitioning, distributed queues, domain-scoped scans, per-source schedulers, workload governance, graph partition strategy, staged indexing, multi-region planning |
| **Target** | 1M tables · 30M columns · 1,000 sources · 50 tenants | Full scale-out; see `10-architecture/10-performance-and-scale-model.md` |

## 4. Backpressure

```text
source begins throttling
  → source latency rises
  → profiler observes throttling
  → scheduler reduces admission concurrency
  → queue grows safely, with visible depth
  → interactive workload remains protected
```

**The property being defended:** degradation is absorbed by background work, never by the analyst. A design in which backpressure surfaces as failed interactive queries has the priority inverted.

## 5. Redis usage

| Use it for | Never use it for |
|---|---|
| User sessions | Semantic source of truth |
| Distributed locks | Permanent lineage store |
| Request and short-lived agent state | Permanent audit store |
| Metadata and semantic retrieval cache | Anything authoritative (INV-1) |
| Query result cache where policy allows | |
| Rate limiting and idempotency keys | |

## 6. Cost governance

### Warehouse cost

Tracked per execution and aggregated per LOB: estimated bytes scanned, credits, query duration, rows scanned, partitions scanned.

### Model cost

Tracked per generation: model, input tokens, output tokens, task type, cache hit, retry count, estimated cost.

### Budget policy

Routing is a cost decision as much as a quality one:

```text
table semantic enrichment      → small model by default
complex ambiguous domain       → reasoning model
unchanged table (fingerprint match) → NO model call at all
```

The third line matters most. Fingerprint-based skipping (`90-reference/04-analysis-algorithms.md` §10) removes the majority of model calls on a rescan, which is what makes recurring analysis of a large estate affordable.

### The economic property to protect

Tool-first execution means **cost per answered question should fall** as the tool library matures (differentiator D2). Track it as a trend. If it is flat or rising while tool coverage grows, the tool-matching path has regressed.

## 7. Metrics

### Analysis

```text
analysis_runs_total          analysis_task_latency        tasks_failed
tables_profiled_per_minute   relationship_candidates      review_queue_depth
semantic_publish_latency     bound_hits_total             admission_rejections
```

### Runtime

```text
agent_request_latency        semantic_retrieval_latency   tool_reuse_rate
sql_generation_rate          sql_validation_failure_rate  warehouse_execution_latency
query_success_rate           user_correction_rate         refusal_rate_by_control
```

### Model

```text
tokens_per_request           cost_per_request             semantic_enrichment_cost
model_failure_rate           model_route_distribution     budget_consumption
```

### Quality of the intelligence itself

These measure whether Atlas is *right*, not whether it is *fast*. They require a labelled corpus and are the hardest to instrument — which is why they are usually missing from products in this category.

```text
verified_relationship_precision    canonical_table_accuracy
text_to_sql_execution_accuracy     text_to_sql_semantic_accuracy
metric_resolution_accuracy
```

**Execution accuracy vs. semantic accuracy.** A query can run successfully and answer the wrong question. Tracking only execution accuracy produces a metric that looks excellent while users lose trust.

## 8. Logging context

Every structured log line carries:

```text
trace_id            request_id          correlation_id
analysis_run_id     task_id             organization_id
user_id             agent_id            tool_id
model_id            datasource_id       warehouse_query_id
```

**Never logged:** passwords, tokens, unmasked secrets, sensitive result payloads, raw question text, SQL literals (INV-6). Enforced by scrubbing middleware, not by convention.

## 9. High availability targets

| Component | Requirement |
|---|---|
| PostgreSQL | Replication with automated failover; backup and tested restore |
| Neo4j | Cluster or rebuild-from-authoritative |
| Redis | HA with failover |
| Queue | Durable, replicated |
| Object storage | Versioning enabled |
| Temporal | Clustered with namespace isolation |

## Related documents

- Performance and scale model: `10-architecture/10-performance-and-scale-model.md`
- Deployment topology: `10-architecture/09-deployment-topology.md`
- Workers and workflows: `10-architecture/08-workers-and-workflows.md`
- Analysis algorithms: `90-reference/04-analysis-algorithms.md`
