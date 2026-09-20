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

> **Implementation status (2026-09-20).** There is no per-module database schema: `src/atlas/modules/ingestion/models.py` declares no `schema=`, so "Schema `ingestion`" in the header names the bounded context only. The real tables are `metadata_ingestion_job` (the delivery record for one envelope; there is no separate `ingestion_envelope_record`), `metadata_ingestion_batch` (the manifest) and `metadata_ingestion_chunk`. The scheduling entities above are not tables: a datasource's interval, priority and maintenance window are one `scan_policy` row (`src/atlas/modules/profiling/models.py`), its concurrency ceiling is `datasource.max_concurrency`, and admission and quotas are enforced when a run is reserved (`reserve_analysis_run` in `src/aida/fleet.py`, with `src/aida/lob_concurrency.py` and `src/aida/usage_quotas.py`) against `analysis_run` rows.

## 6. Envelope semantics

| Rule | Behaviour |
|---|---|
| `idempotency_key` | Unique per datasource. Same key + same payload → original job. Same key + different payload → **HTTP 409**. |
| `INCREMENTAL` | Creates/updates objects present. **Never** retires omitted objects. |
| `FULL` | Authoritative for the whole datasource scope; soft-deprecates omitted active objects. Target: requires explicit confirmation. The server asks for none today, and the synchronous endpoint defaults an omitted `snapshot_type` to `FULL` (see [the envelope contract](../30-contracts/05-metadata-ingestion-envelope.md) §4). |
| Locking | Datasource row lock serializes competing snapshots for one source without blocking others. |
| Atomicity | Delivery + catalog changes + graph snapshot event commit in one transaction. |
| Fingerprints | SHA-256 over canonical JSON. Raw payloads are not retained after success. |

### Bounds

| Boundary | Default | Adjustable |
|---|---|---|
| Synchronous envelope | 100 catalogs / 50,000 tables / 250,000 columns | Down only |
| Batch | 1,000 chunks / 1,000,000 tables / 5,000,000 columns | Down only |
| Attributes per object | 50, scalar, bounded | Down only |
| Request size (local proxy) | nginx's own default (1 MiB) on `/v1/` | `ui-next/nginx.conf` |

The 40 MiB figure this row used to carry is stale: `ui-next/nginx.conf` sets `client_max_body_size` only on `/mcp` and `/graphql`, and its `/v1/` location sets none (detail in [the envelope contract](../30-contracts/05-metadata-ingestion-envelope.md) §6).

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

> **Implementation status (2026-09-20).** The signatures below are the design target. No `ingestion/api.py` exists: `src/atlas/modules/ingestion/` holds only `models.py`, `schemas.py` and `router.py`, and none of these functions is defined. The behaviour is in the HTTP handlers in `src/atlas/modules/ingestion/router.py`, backed by `src/aida/ingestion.py` and `src/aida/batch_ingestion.py`.

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
| POST | `/v1/metadata-ingestion-batches/{id}/pause`, `/resume`, `/cancel`, `/replay` |
| PUT, GET | `/v1/datasources/{id}/scan-policy` (the schedule: interval, mode, priority, maintenance window) |
| POST | `/v1/projects/{id}/datasources/bulk-onboard` |
| GET | `/v1/organizations/{id}/fleet-summary`, `/v1/organizations/{id}/fleet-health` |

Route shapes are read from `Docs/90-reference/openapi-baseline.json`; `PUT /v1/datasources/{id}/schedule` was the original design name and does not exist.

Push ingestion accepts `PlatformAdmin`, `MetadataAdmin`, `DataAdmin`, or the workload role `MetadataIngestor`.

> **Implementation status (2026-09-20).** `MetadataIngestor` is named in the push routes' role checks but is not in `PLATFORM_ROLES` (`src/aida/oidc.py`), so it cannot be granted under OIDC: only the development identity can carry it. See [the envelope contract](../30-contracts/05-metadata-ingestion-envelope.md) §9.

## 10. Events

Emits `metadata.discovery.snapshot.v1` (an envelope applied, whether by the synchronous endpoint or a finished batch; the catalog's `ingestion.delivered`), `metadata.ingestion.batch.queued.v1` (a batch finalized and submitted; the catalog's `batch.finalized`) and, for the operator controls, `metadata.ingestion.batch.paused.v1`, `.cancelled.v1`, `.resumed.v1` and `.replayed.v1`.

> **Implementation status (2026-09-20).** `ingestion.rejected`, `batch.created`, `batch.chunk_received`, `batch.failed`, `fleet.admission_rejected` and `fleet.backpressure_engaged` are design targets that no code emits; a rejection or an admission refusal surfaces as the refused request. Their catalog rows stay in [04-event-catalog.md](../30-contracts/04-event-catalog.md) as target names.

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
| Durable batches | Implemented — manifests, chunks, replay, cross-chunk FK, deferred FULL reconciliation, and operator pause/resume/cancel/replay controls (IN-2) | Maximum-scale recovery certification |
| Fleet scheduling | Implemented — HA polling, priority, windows, quotas, admission, backpressure | Fairness and capacity proof at 1,000+ sources |
| Kafka intake | Not implemented | Phase B with schema registry |
| Signed producers | Not implemented | Phase B |
| Bulk onboarding | Implemented (IN-1) — `POST /v1/projects/{project_id}/datasources/bulk-onboard` registers up to 200 datasources per call and reports each item as `SUCCEEDED` or `FAILED`; no connectivity probe runs per item | — |

## 13. Open work

| ID | Item | Priority |
|---|---|---|
| IN-1 | Bulk source onboarding (delivered: tracker IN-1 DONE) | P0 |
| IN-2 | Operator pause/cancel/replay controls (delivered: tracker IN-2 DONE) | P1 |
| IN-3 | Kafka intake + schema registry compatibility | P1 |
| IN-4 | Signed workload producer identity and per-producer rate policy | P1 |
| IN-5 | Envelope extension: BI, pipeline, topic, file, ML assets | P1 |
| IN-6 | Maximum-scale recovery certification | P0 |
| IN-7 | Fleet fairness testing at target scale | P1 |
