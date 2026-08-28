# 11 — Local Operations Runbook

## Purpose

This runbook operates the production-shaped local environment. Local Docker is for engineering and integration validation, not a production topology.

## Start and verify

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:8000/health/ready
./scripts/verify-local.ps1
```

The verifier creates an isolated organization/LOB/project, runs PostgreSQL and Microsoft SQL Server connectivity/discovery/profile/certification paths, validates SQL Server SHOWPLAN/query/masking, exercises synchronous and durable-batch ingestion, configures data quality, proves prompt-risk blocking and benign `SCREENED/ALLOW`, executes governed SQL and tool-first agents, confirms masking and value-free lineage, exercises maker-checker governance and scheduling, verifies graph search/expansion policy, proves model-route approval remains separate from activation, and reconciles the projection.

## Service endpoints

| Service | Endpoint | Use |
|---|---|---|
| Atlas product portal | `http://localhost:3000` | Agentic analyst, lineage, governance and operational workbenches |
| Platform API/OpenAPI | `http://localhost:8000/docs` | API exploration |
| Temporal UI | `http://localhost:8080` | Workflow history and retries |
| Redpanda Console | `http://localhost:8081` | Event topics and consumer groups |
| Neo4j Browser | `http://localhost:7474` | Metadata projection inspection |
| MinIO Console | `http://localhost:9001` | Object storage inspection |

Local credentials are intentionally non-production values in `compose.yaml`.

## Routine evidence checks

```powershell
docker compose logs --tail 100 api metadata-worker outbox-publisher graph-projector
docker compose exec postgres psql -U aida -d aida -c "select status, count(*) from analysis_run group by status"
docker compose exec postgres psql -U aida -d aida -c "select status, count(*) from query_execution group by status"
docker compose exec postgres psql -U aida -d aida -c "select status, count(*) from data_quality_observation group by status"
docker compose exec postgres psql -U aida -d aida -c "select status, severity, count(*) from data_quality_incident group by status, severity"
docker compose exec postgres psql -U aida -d aida -c "select status, count(*) from outbox_event group by status"
```

Expected invariants:

- all completed analysis runs have a Temporal workflow ID;
- profiling persists no source values;
- quality observations retain only counts, rates, identifiers and fingerprints; metadata scan age is not source-row freshness;
- all query executions are `COMPLETED`, `REJECTED`, or `FAILED` with audit evidence;
- pending outbox events drain to `PUBLISHED`;
- completed ingestion chunks expose only checksums/counts and have SQL-NULL payload storage;
- the graph is a projection and can be rebuilt from authoritative PostgreSQL state and events.

## Enterprise ingestion verification

The full local verifier exercises connector-matrix honesty, deterministic certification, canonical incremental delivery, synchronous and batch replay, conflicting key/content denial, Temporal completion, chunk evidence and successful payload non-exposure:

```powershell
./scripts/verify-local.ps1
```

For manual operation, open **Source fleet** at `http://localhost:3000`. Run connection verification and at least one pull scan before certification. Canonical delivery defaults to `INCREMENTAL`. For large estates, create a batch manifest, upload every numbered chunk, then finalize it; Temporal progress and checksums remain visible after successful payload cleanup. Use `FULL` only when all chunks together represent the entire datasource scope; omission retirement is deferred until every chunk succeeds.

Inspect database and API consistency with:

```powershell
docker compose run --rm migrate alembic check
Invoke-RestMethod http://localhost:8000/health/ready
docker compose exec postgres psql -U aida -d aida -c "select status, count(*) from metadata_ingestion_batch group by status"
docker compose exec postgres psql -U aida -d aida -c "select status, count(*) from metadata_ingestion_chunk group by status"
```

Do not submit sample rows or secrets in metadata attributes. The contract rejects common value-bearing keys, but producer-side classification and review remain required for descriptions and default expressions.

The SQL Server fixture source needs `db_datareader` for governed SELECT and database-scoped `SHOWPLAN` for no-execution cost estimation. The init sidecar uses `sqlcmd -b`; a credential, DDL or grant failure must leave the service failed rather than silently producing a partial fixture.

## Safe restart

```powershell
docker compose restart api metadata-worker outbox-publisher graph-projector
```

Temporal histories, PostgreSQL metadata, Kafka logs, Neo4j projection data, Redis state, and object storage survive service restarts through named volumes.

## Stop

```powershell
docker compose stop
```

Do not use `docker compose down -v` unless destruction of all local platform and sample data is explicitly intended.

## Failure triage

1. Check `/health/ready` and `docker compose ps`.
2. Use the correlation ID returned by the API to find structured logs and `audit_event` records.
3. Inspect the Temporal workflow for activity retry history and heartbeat progress.
4. Inspect `outbox_event` before Kafka when a projection is stale.
5. Treat PostgreSQL as authoritative; repair or replay a projection instead of editing Neo4j directly.

## Production substitutions

Before deployment, configure the implemented OIDC verifier with the bank issuer/audience/JWKS and claim mappings, register the selected enterprise-secret adapter, replace single-node services with managed/HA platforms, use read-only delegated source identity, and integrate the bank policy bundle. Production configuration rejects development identity, the development SQL override, weak audit keys, non-HTTPS remote JWKS, and `env` credentials.
