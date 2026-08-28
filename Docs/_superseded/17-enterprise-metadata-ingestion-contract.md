# 17 — Enterprise Metadata Ingestion Contract

## Delivered scope

Atlas now has one versioned metadata envelope for source-native pull adapters, authenticated push producers, and future broker consumers. PostgreSQL remains the authoritative store. A successful delivery updates the same catalog, schema, table, column, constraint, fingerprint, tombstone, audit, outbox, analysis-run, and graph-projection paths used by pull discovery.

PostgreSQL, Microsoft SQL Server, and Oracle native pull adapters are implemented at `BETA` maturity. Snowflake, Databricks, Teradata, and Db2 remain visibly `PLANNED`. The contract lets adapter teams and external producers build against one stable persistence boundary without overstating vendor certification.

## API surface

| Endpoint | Purpose |
|---|---|
| `GET /v1/connectors/capability-matrix` | Honest implementation, maturity, transport, version, and capability inventory |
| `POST /v1/datasources/{id}/metadata-ingestions` | Validate and atomically apply envelope `1.0` |
| `GET /v1/datasources/{id}/metadata-ingestions` | Paginated delivery and change evidence |
| `POST /v1/datasources/{id}/metadata-ingestion-batches` | Create an idempotent large-estate manifest |
| `GET /v1/datasources/{id}/metadata-ingestion-batches` | Paginated batch progress and completion evidence |
| `POST /v1/metadata-ingestion-batches/{id}/chunks` | Upload a checksum-addressed numbered chunk |
| `GET /v1/metadata-ingestion-batches/{id}/chunks` | Paginated chunk status and checksum evidence; payload is never exposed |
| `POST /v1/metadata-ingestion-batches/{id}/finalize` | Seal a complete manifest and submit its Temporal workflow |
| `GET /v1/metadata-ingestion-batches/{id}` | Poll durable workflow progress or failure evidence |
| `POST /v1/datasources/{id}/connector-certifications` | Persist deterministic source conformance evidence |
| `GET /v1/datasources/{id}/connector-certifications` | Paginated certification history |

All endpoints use tenant enforcement and explicit roles. Mutations write attributable audit and transactional outbox records. Push ingestion accepts `PlatformAdmin`, `MetadataAdmin`, `DataAdmin`, or the workload-oriented `MetadataIngestor` role.

## Envelope 1.0

```json
{
  "envelope_version": "1.0",
  "idempotency_key": "cmdb:2026-08-27:0001",
  "producer": "bank-metadata-bridge",
  "transport": "PUSH",
  "snapshot_type": "INCREMENTAL",
  "emitted_at": "2026-08-27T20:00:00Z",
  "catalogs": [
    {
      "name": "bank",
      "attributes": {"region": "us-east"},
      "schemas": [
        {
          "name": "customer",
          "tables": [
            {
              "name": "account",
              "object_type": "BASE_TABLE",
              "columns": [
                {
                  "name": "account_id",
                  "ordinal_position": 1,
                  "physical_type": "bigint",
                  "nullable": false
                }
              ],
              "constraints": [
                {
                  "name": "account_pk",
                  "constraint_type": "PRIMARY_KEY",
                  "columns": ["account_id"]
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

## Required semantics

- `idempotency_key` is unique per datasource. Replaying the same key and payload returns the original job. Reusing the key with a different payload returns HTTP 409.
- `INCREMENTAL` creates or updates objects present in the envelope and never retires omitted objects.
- `FULL` is authoritative for the complete datasource scope and soft-deprecates active objects omitted from the envelope. The Atlas UI requires an explicit warning confirmation.
- Deliveries acquire a datasource row lock. This serializes competing push snapshots for one source without blocking unrelated sources.
- The delivery and catalog changes commit atomically. A graph snapshot event is written in the same transaction for asynchronous projection.
- Payload fingerprints are SHA-256 over canonical JSON. Raw payloads are not retained in the ingestion job.

## Durable batch semantics

Large inventories use a persisted manifest and one to 1,000 numbered chunks. The batch key is unique per datasource; each chunk number and chunk key are unique inside the manifest. Exact manifest and chunk replays return the original records, while reuse with different content returns HTTP 409.

Finalization is allowed only when the exact sequence `1..expected_chunks` exists. Temporal owns execution, heartbeats and bounded exponential retries. A failed batch retains validated chunk payloads for an authorized retry and creates a replacement analysis run linked through `resumed_from_run_id`. Successful completion stores only fingerprints, counts, statuses and timestamps, and physically clears payload JSON with SQL `NULL`.

Chunks commit independently so a retry resumes already processed work. Object fingerprints keep reapplication idempotent. Atlas runs a second value-free metadata pass to resolve foreign keys whose referenced table arrived in another chunk. A `FULL` batch accumulates stable object identities across every chunk and runs omission reconciliation only after all chunks have succeeded; it can never retire metadata from a partial delivery. `INCREMENTAL` batches never retire omissions.

Deployment boundaries default to 1,000 chunks, 1,000,000 tables and 5,000,000 columns per batch and remain configurable downward. Cumulative table/column admission is enforced under the batch lock during every upload and rechecked before processing. Each chunk still obeys envelope validation and value-free limits. Finalization fails closed when Temporal is disabled or unavailable instead of leaving a stranded pseudo-queued job; the local Atlas proxy accepts at most 40 MiB per request.

## Validation and privacy boundaries

- Nested names, sizes, object types, constraint types, foreign-key cardinality, local-column references, duplicate columns, and duplicate ordinals are validated before persistence.
- The synchronous boundary is 100 catalogs, 50,000 tables, and 250,000 columns. Larger estates use the durable batch contract; Kafka/schema-registry intake remains a future transport feeding the same manifest boundary.
- Attributes are scalar, bounded, and limited to 50 per object.
- Attribute keys associated with samples, row values, passwords, secrets, tokens, or credentials are rejected.
- Technical default expressions and descriptions are allowed but bounded. Producers remain responsible for excluding literal regulated values.

## Certification suite v1

The deterministic suite records six checks: implementation registration, opaque secret reference, prior connection evidence, catalog/schema capability declaration, active inventory evidence, and canonical push-contract support. A source is `CERTIFIED` only when all checks pass, `CONDITIONAL` at 67–99, and otherwise `FAILED`.

This is a control-plane conformance suite, not a substitute for database-version compatibility, load, failover, network, least-privilege, or vendor-driver certification. Those remain explicit release gates.

## Remaining work

1. Add remaining native pull adapters and versioned fixtures in bank priority order.
2. Add Kafka intake, schema-registry compatibility, admission quotas and explicit pause/cancel/replay operator controls around the delivered Temporal batch engine.
3. Add signed workload producer identities and per-producer authorization/rate policy.
4. Add OpenLineage, BI, pipeline, topic, file, API, and ML asset envelopes.
5. Publish per-version connector load, cancellation, retry, least-privilege, and recovery evidence.
