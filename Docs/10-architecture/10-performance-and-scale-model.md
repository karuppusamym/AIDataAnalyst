# Performance and Scale Model

> Status: Authoritative targets; measurement status tracked in `60-delivery/03-tracker.md`.
> Owner: Architecture + Platform.
> Principle: **an unmeasured target is a wish.** Every number here has a named test that produces it.

## 1. Why this document is a product document

`00-product/05-differentiation-and-whitespace.md` states that proof is the third strategic move. Competitors publish benchmarks; Atlas currently does not. Until these numbers are measured and published, "better than the market on scale" is not a claim Atlas may make in any material.

## 2. Scale targets

The estate Atlas must handle without qualification.

| Dimension | Target | Stretch |
|---|---|---|
| Data sources | 1,000 concurrent registered | 5,000 |
| Catalog objects (tables + views) | 1,000,000 | 5,000,000 |
| Columns | 30,000,000 | 150,000,000 |
| Organizations / tenants | 50 | 200 |
| Lines of business | 500 | 2,000 |
| Concurrent analyst sessions | 500 | 2,000 |
| Agent runs / day | 50,000 | 250,000 |
| Governed tools | 10,000 | 50,000 |
| Lineage edges | 100,000,000 | 500,000,000 |
| Audit events / day | 5,000,000 | 25,000,000 |

## 3. Latency targets

Excluding source execution time and model provider time, which Atlas does not control.

| Operation | p50 | p95 | p99 | Test |
|---|---|---|---|---|
| Authorization decision | 10 ms | **50 ms** | 100 ms | `perf_authz` |
| Control-plane API read | 60 ms | **300 ms** | 600 ms | `perf_api_read` |
| Control-plane API write | 100 ms | 400 ms | 800 ms | `perf_api_write` |
| Search first paint | 300 ms | **1 s** | 2 s | `perf_search` |
| Analyst plan preview (metadata-grounded) | 800 ms | **2 s** | 4 s | `perf_plan` |
| Prompt-risk screening | 5 ms | 20 ms | 40 ms | `perf_screen` |
| Hybrid retrieval | 50 ms | 120 ms | 250 ms | `perf_retrieval` |
| SQL AST validation | 8 ms | 30 ms | 60 ms | `perf_validate` |
| Graph neighbourhood (bounded, 1–4 hops) | 400 ms | **2 s** | 4 s | `perf_graph` |
| Impact analysis | 500 ms | 2 s | 5 s | `perf_impact` |
| Evidence assembly | 20 ms | 50 ms | 100 ms | `perf_evidence` |

**Composite budget.** Total Atlas overhead on the interactive path ≤ 300 ms p95 (`03-logical-architecture.md` §6). Bolded rows are the published external commitments.

## 4. Throughput targets

| Operation | Target | Test |
|---|---|---|
| Metadata ingestion (batch) | 100,000 objects / minute sustained | `perf_ingest_batch` |
| Discovery scan | 10,000 tables / hour / source | `perf_discovery` |
| Profiling | 1,000 tables / hour / worker | `perf_profiling` |
| Projection (outbox → Neo4j) | 10,000 events / minute | `perf_projection` |
| Lineage extraction | 50,000 statements / hour | `perf_lineage` |
| Quality evaluation | 5,000 table-policies / hour | `perf_quality` |
| Audit write | 10,000 events / second burst | `perf_audit` |

## 5. Recovery targets

| Operation | Target | Test |
|---|---|---|
| Neo4j rebuild (1M objects) | < 4 hours | `drill_rebuild_graph` |
| Vector index rebuild (1M objects) | < 6 hours | `drill_rebuild_vector` |
| Search index rebuild (1M objects) | < 2 hours | `drill_rebuild_search` |
| PostgreSQL PITR restore | < 4 hours (RTO) | `drill_pitr` |
| Temporal failover | < 15 minutes | `drill_temporal_failover` |
| Batch ingestion resume after forced restart | No reprocessing of committed chunks | `drill_batch_resume` |
| Credential rotation | Zero failed requests | `drill_secret_rotation` |
| Model kill switch | < 60 seconds to full stop | `drill_kill_switch` |

## 6. UI performance

| Requirement | Target |
|---|---|
| Table list, 1M rows | Virtualized; smooth scroll; no browser lockup |
| Graph explorer | Level-of-detail rendering; bounded node/edge caps with explicit truncation reasons |
| Search results | First results < 1 s; progressive load |
| Bulk operations | 10,000-item selection without freeze; background execution with progress |
| Large DAG (dbt) | Virtualized; collapsible; no full-graph render |
| Time to interactive | < 3 s on a corporate-standard laptop |

**The design commitment behind these.** A browser must never download an enterprise graph. Server-side policy caps depth, nodes, and edges, and returns explicit truncation evidence rather than silently partial results (`ADR-0010`).

## 7. Cost model

| Cost driver | Control | Measurement |
|---|---|---|
| Source query compute | Cost gate before execution; row/byte/time caps per workload class | Per-LOB showback |
| Model tokens | Per-route budget contract with hard caps | Per-route, per-tenant spend |
| Profiling load on sources | Adaptive sampling; maintenance windows; per-source concurrency | Source-reported load |
| Storage | Partitioning, retention policy, artifact offload to object storage | Per-schema growth |
| Projection compute | Batch size tuning; incremental projection | Consumer lag and CPU |

**The economic property to protect.** Tool-first execution means cost per answer should *fall* as the tool library matures (differentiator D2). Track cost-per-answered-question as a trend, not as an absolute. If it is flat or rising while tool coverage grows, the tool-matching path has regressed.

## 8. Required proof

Assertions become claims only after these produce evidence:

| Proof type | What it establishes | Cadence |
|---|---|---|
| Load test | Sustained throughput at target scale | Per release |
| Soak test | No leak or degradation over 72 h | Per release |
| Spike test | Behaviour at 10× burst | Per release |
| Failure injection | Source timeouts, broker duplication, projector crash, DB failover | Per quarter |
| Projection rebuild timing | Recovery targets | Per quarter |
| Migration rehearsal | Upgrade and rollback on production-like data | Per release |
| Connector certification | Per-vendor, per-version behaviour under load, cancellation, recovery | Per connector version |
| Security testing | SAST, DAST, penetration, adversarial SQL corpus | Per release / annually |
| Accessibility | Keyboard, focus order, ARIA, contrast, screen reader | Per release |
| Browser regression | Supported browser matrix | Per release |

## 9. Regression gates

CI fails on regression beyond these thresholds. Performance is defended continuously, not measured once.

| Gate | Threshold |
|---|---|
| API p95 | > 10% regression vs. baseline |
| Authorization decision p95 | > 50 ms absolute |
| Retrieval p95 | > 15% regression |
| Query validation p95 | > 15% regression |
| Memory per worker | > 20% regression |
| Projection throughput | > 15% regression |

## 10. Current measurement status

Honest position as of the baseline date. This table is the gap between the architecture and the claim.

| Area | Status |
|---|---|
| Unit / contract suite | Passing (121 tests) |
| Static quality (ruff, strict mypy) | Clean |
| Migration drift | Single head, applied |
| Local end-to-end fixture | Passing, including batch replay and cross-chunk FK resolution |
| **Load / soak / spike** | **Not run** |
| **Failure injection** | **Partial** — component retries exercised only |
| **Projection rebuild timing** | **Not measured** |
| **Restore / DR drill** | **Not run** |
| **Penetration test** | **Not run** |
| **Accessibility audit** | **Not run** |
| **Connector version certification** | **Control-plane conformance only** |

Every "not run" is a tracked item in `60-delivery/03-tracker.md` with an owner.

## Related documents

- Logical architecture: `10-architecture/03-logical-architecture.md`
- Data architecture: `10-architecture/06-data-architecture.md`
- Testing strategy: `40-engineering/04-testing-strategy.md`
- Tracker: `60-delivery/03-tracker.md`
