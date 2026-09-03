# Module 02 — Connectivity

> Layer L1 · Schema `connectivity` · Owner: Data Platform

## 1. Purpose

Owns the relationship with a data source: how to reach it, what it can actually do, and whether that has been proven. This module is where Atlas's honesty about connector coverage is enforced (INV-9) — a differentiator against vendors whose connector lists overstate depth.

It deliberately does **not** own query execution. Connector execution methods are private to the query gateway (INV-2, ADR-0004).

## 2. Jobs served

P1 (onboard sources at scale), P2 (know which scans are failing and why), P4 (rotate credentials), S4 indirectly.

## 3. Responsibilities

- Datasource registration, connection configuration, and credential-reference binding.
- Connectivity validation and dialect identification.
- **Capability negotiation** — what this adapter can genuinely do.
- Connection pooling and per-source rate limiting.
- Connector certification runs and the honest capability matrix.
- Connector SDK definition for first-party and third-party adapters.
- Source-side connector agent registration and mTLS trust.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Executing analytical queries | 16 query-gateway |
| Persisting metadata | 03 ingestion → 04 catalog |
| Scheduling scans | 03 ingestion (fleet scheduler) |
| Secret values | 01 identity-tenancy → secret manager |

## 5. Domain model

```text
datasource, datasource_connection, network_zone
connector_implementation, connector_capability, connector_version
connector_certification_run, certification_check_result
connector_agent (registration, mTLS identity, heartbeat)
```

## 6. Connector interface

```text
list_catalogs()            list_schemas()          list_tables()
list_columns()             get_constraints()       get_indexes()
get_partitions()           get_table_statistics()  get_view_definition()
get_query_history()        describe_capabilities()
# execution surface — callable ONLY by module 16
_execute_read(...)         _explain(...)           _cancel(...)
```

Capability flags are **derived from certification results**, never hand-declared.

## 7. Public interface

```python
# connectivity/api.py
def register_datasource(scope: TenantScope, spec: DatasourceSpec) -> DatasourceDTO
def test_connection(datasource_id: DatasourceId) -> ConnectionResult
def get_capabilities(datasource_id: DatasourceId) -> CapabilityMatrix
def run_certification(datasource_id: DatasourceId) -> CertificationRun
def get_capability_matrix(scope: TenantScope) -> FleetCapabilityMatrix
def acquire_connection(datasource_id: DatasourceId) -> ConnectionHandle   # gateway-only
```

## 8. HTTP surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/connectors/capability-matrix` | Honest implementation, maturity, transport, version inventory |
| POST | `/v1/datasources` | Register |
| POST | `/v1/datasources/{id}/test-connection` | Verify |
| POST | `/v1/datasources/{id}/connector-certifications` | Run certification |
| GET | `/v1/datasources/{id}/connector-certifications` | History |
| POST | `/v1/connector-agents` | Register a source-side agent |

## 9. Events

Emits `datasource.registered`, `datasource.connection_verified`, `datasource.disabled`, `certification.completed`, `connector_agent.registered`, `connector_agent.heartbeat_lost`.

## 10. Dependencies

01 identity-tenancy (tenancy, secret references).

## 11. Certification model

Certification v1 records six deterministic control-plane checks: implementation registration, opaque secret reference, prior connection evidence, catalog/schema capability declaration, active inventory evidence, and canonical push-contract support.

| Score | Status |
|---|---|
| 100 | `CERTIFIED` |
| 67–99 | `CONDITIONAL` |
| < 67 | `FAILED` |

**This is a control-plane conformance suite, not a substitute for** database-version compatibility, load, failover, network, least-privilege, or vendor-driver certification. Those remain explicit release gates and are the difference between `BETA` and production-ready.

## 12. Current state → target

| Adapter | Now | Target |
|---|---|---|
| PostgreSQL | Implemented for the current contract. CN-3: real, executable version fixture for 16 (`tests/test_postgres_version_fixtures.py`, run live against this sandbox's own Postgres 16.13 — found and fixed a real gap, materialized-view columns/definitions silently dropped from `discover()`); 14 configured identically (`tests/fixtures/postgres_versions/compose.yml`, `connector-version-fixtures` CI job) but not yet executed live — no Docker daemon and no PGDG apt access in this pass's sandbox | 14 leg's first live CI run, load/cancellation/recovery certification, delegated identity |
| Microsoft SQL Server | `BETA` — real Docker fixture, discovery, bounded profiling, SHOWPLAN cost, governed query/masking, 100-point certification | Multi-version, TLS/private-network, delegated-identity certification |
| Oracle | `BETA` — native async adapter (`python-oracledb` thin mode), `ALL_TAB_COLUMNS`/`ALL_CONSTRAINTS` discovery, real session-ID capture, LOB-aware bounded profiling, compose fixture. `explain=False` (least-privilege `PLAN_TABLE` path uncertified). Live container verification outstanding | Live container verification, certified EXPLAIN path, delegated identity |
| BigQuery | `BETA` — discovery via region-qualified INFORMATION_SCHEMA (primary keys; foreign keys honestly omitted, uncertified), dry-run byte estimation gated by a dedicated gateway byte budget, bounded profiling, governed query. Live GCP verification outstanding | Live project verification, multi-region projects, delegated/workload-identity certification |
| Snowflake | `BETA` — multi-database INFORMATION_SCHEMA discovery, partition-pruned EXPLAIN-JSON cost estimate, approximate-distinct bounded profiling, real `sfqid` capture. Live account verification outstanding | Live account verification, certification, version fixtures |
| Databricks | `PLANNED` — no adapter code | Phase A |
| Teradata, Db2 | `PLANNED` | Phase B |
| Files / APIs / BI | `PLANNED` | Phase B |
| Connector agents | Not implemented | Phase B — required for restricted zones (whitespace W9) |

## 13. Open work

| ID | Item | Priority |
|---|---|---|
| CN-1 | Oracle and BigQuery adapters to certified state (code done; live verification outstanding) | P0 |
| CN-2a | Snowflake adapter to certified state (code done; live verification outstanding) | P0 |
| CN-2b | Databricks adapter (no code yet) | P0 |
| CN-3 | Executable vendor/version certification fixtures | P0 |
| CN-4 | Source-side connector agent with mTLS | P1 |
| CN-5 | Delegated / read-only source identities | P0 |
| CN-6 | Public connector SDK + docs (whitespace W6) | P1 |
| CN-7 | Per-connector health scoring in the fleet view | P1 |
| CN-8 | Index and partition extraction across adapters | P1 |
| CN-9 | `Connector.get_query_history()` for Snowflake/BigQuery/Databricks -- the input `aida.query_history_miner` (AT-12) already consumes but that no connector produces yet. Certification (§11) proves a live end-to-end call before `query_history` flips from `False` (AT-D3) | P1 |
