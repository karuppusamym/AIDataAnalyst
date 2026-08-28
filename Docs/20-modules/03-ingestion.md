# Module 03 — Ingestion

> Layer L1 · Schema `ingestion` · Owner: Data Platform

## 1. Purpose

Gets metadata into Atlas from any transport — native pull, authenticated push, source-side agent, or broker — through **one canonical envelope and one authoritative persistence path** (ADR-0012). Also owns fleet scheduling: deciding which source gets capacity next across thousands of sources.

## 2. Jobs served

P1 (onboard 200 sources this quarter), P2 (know which scans are failing and why).

## 3. Responsibilities

- Envelope validation, idempotency, and atomic application.
- Durable batch ingestion: manifests, checksum-addressed chunks, resumable processing.
- `FULL` vs `INCREMENTAL` snapshot semantics and omission reconciliation.
- Cross-chunk foreign-key resolution.
- Fleet scheduling: priority, fairness, maintenance windows, quotas, admission control, backpressure.
- Delivery evidence and change history.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Reaching the source | 02 connectivity |
| Storing catalog objects | 04 catalog |
| Profiling | 05 profiling |
| Graph projection | 10 knowledge-graph |

## 5. Domain model

```text
ingestion_job, ingestion_envelope_record
ingestion_batch (manifest), ingestion_chunk
fleet_schedule, admission_state, maintenance_window, source_quota
```

## 6. Envelope semantics

| Rule | Behaviour |
|---|---|
| `idempotency_key` | Unique per datasource. Same key + same payload → original job. Same key + different payload → **HTTP 409**. |
| `INCREMENTAL` | Creates/updates objects present. **Never** retires omitted objects. |
| `FULL` | Authoritative for the whole datasource scope; soft-deprecates omitted active objects. Requires explicit confirmation. |
| Locking | Datasource row lock serializes competing snapshots for one source without blocking others. |
| Atomicity | Delivery + catalog changes + graph snapshot event commit in one transaction. |
| Fingerprints | SHA-256 over canonical JSON. Raw payloads are not retained after success. |

### Bounds

| Boundary | Default | Adjustable |
|---|---|---|
| Synchronous envelope | 100 catalogs / 50,000 tables / 250,000 columns | Down only |
| Batch | 1,000 chunks / 1,000,000 tables / 5,000,000 columns | Down only |
| Attributes per object | 50, scalar, bounded | Down only |
| Request size (local proxy) | 40 MiB | — |

Attribute keys associated with samples, row values, passwords, secrets, tokens, or credentials are **rejected** (INV-6).

## 7. Batch recovery model

The most complex correctness path in the system.

1. Manifest declares `expected_chunks`; chunk numbers and keys are unique within the batch.
2. Finalization requires the exact sequence `1..expected_chunks` — no gaps.
3. Temporal owns execution with heartbeats and bounded exponential retries.
4. **Chunks commit independently**, so retry resumes rather than restarts.
5. Object fingerprints make reapplication idempotent.
6. A second value-free pass resolves foreign keys whose referenced table arrived in another chunk.
7. **A `FULL` batch runs omission reconciliation only after every chunk succeeds** — it can never retire metadata from a partial delivery.
8. On success, payload JSON is physically cleared (SQL `NULL`); fingerprints, counts, statuses, and timestamps remain.
9. On failure, validated chunk payloads are retained for authorized retry; a replacement analysis run is linked via `resumed_from_run_id`.
10. Finalization **fails closed** when Temporal is unavailable rather than leaving a stranded pseudo-queued job.

Point 7 is the one that matters most: a partial `FULL` that reconciled would soft-delete metadata that exists — data loss from a transient network failure.

## 8. Public interface

```python
# ingestion/api.py
def ingest_envelope(scope, datasource_id, envelope: MetadataEnvelope) -> IngestionJobDTO
def create_batch(scope, datasource_id, manifest: BatchManifest) -> BatchDTO
def upload_chunk(batch_id, chunk: ChunkUpload) -> ChunkDTO
def finalize_batch(batch_id) -> BatchDTO
def get_batch_progress(batch_id) -> BatchProgressDTO
def schedule_scan(datasource_id, policy: SchedulePolicy) -> ScheduleDTO
def get_fleet_state(scope) -> FleetStateDTO
```

## 9. HTTP surface

| Method | Path |
|---|---|
| POST | `/v1/datasources/{id}/metadata-ingestions` |
| GET | `/v1/datasources/{id}/metadata-ingestions` |
| POST | `/v1/datasources/{id}/metadata-ingestion-batches` |
| GET | `/v1/datasources/{id}/metadata-ingestion-batches` |
| POST | `/v1/metadata-ingestion-batches/{id}/chunks` |
| GET | `/v1/metadata-ingestion-batches/{id}/chunks` |
| POST | `/v1/metadata-ingestion-batches/{id}/finalize` |
| GET | `/v1/metadata-ingestion-batches/{id}` |
| PUT | `/v1/datasources/{id}/schedule` |

Push ingestion accepts `PlatformAdmin`, `MetadataAdmin`, `DataAdmin`, or the workload role `MetadataIngestor`.

## 10. Events

Emits `ingestion.delivered`, `ingestion.rejected`, `batch.created`, `batch.chunk_received`, `batch.finalized`, `batch.failed`, `fleet.admission_rejected`, `fleet.backpressure_engaged`.

## 11. Fleet scheduling

| Concern | Mechanism |
|---|---|
| HA | Leader election with policy polling; restart does not double-schedule |
| Priority | Per-source priority class |
| Fairness | Round-robin within class — one huge source cannot starve the fleet |
| Maintenance windows | Work deferred, not failed |
| Quotas | Per-organization and per-LOB concurrency |
| Admission | Source at capacity is not admitted; queue depth is visible |
| Backpressure | Saturation reduces admission rather than causing failures |
| **Bulkhead** | **One source's failure never affects unrelated sources** |

## 12. Current state → target

| Aspect | Now | Target |
|---|---|---|
| Envelope 1.0 | Implemented with atomic sync ingestion | Envelope 1.1 for BI/pipeline/topic/file/ML assets |
| Durable batches | Implemented — manifests, chunks, replay, cross-chunk FK, deferred FULL reconciliation | Pause/cancel/replay operator controls |
| Fleet scheduling | Implemented — HA polling, priority, windows, quotas, admission, backpressure | Fairness and capacity proof at 1,000+ sources |
| Kafka intake | Not implemented | Phase B with schema registry |
| Signed producers | Not implemented | Phase B |
| Bulk onboarding | Not implemented | Phase A |

## 13. Open work

| ID | Item | Priority |
|---|---|---|
| IN-1 | Bulk source onboarding | P0 |
| IN-2 | Operator pause/cancel/replay controls | P1 |
| IN-3 | Kafka intake + schema registry compatibility | P1 |
| IN-4 | Signed workload producer identity and per-producer rate policy | P1 |
| IN-5 | Envelope extension: BI, pipeline, topic, file, ML assets | P1 |
| IN-6 | Maximum-scale recovery certification | P0 |
| IN-7 | Fleet fairness testing at target scale | P1 |
