# Connector Implementation Backlog

> Status: Authoritative for the current connector wave. Owner: Data Platform.
> Concrete, code-level backlog for the framework hardening and the next adapters. Migrated and updated from the retired flat `18-oracle-bigquery-implementation-backlog.md`.
>
> **Note on paths.** File paths below refer to the current `src/aida/` layout. After the refactor (`40-engineering/06-refactor-plan.md`), the equivalents are under `src/atlas/modules/connectivity/` and `src/atlas/modules/query_gateway/`.

## 1. Framework changes required before the next adapters

Four design points must be tightened first. Adding Oracle and BigQuery on top of the current shape would multiply the defects.

### 1.1 Connector-owned credential parsing

**Problem.** `ConnectorRegistry.create()` passes a single resolved secret *string* into each connector. Adequate for PostgreSQL, barely adequate for SQL Server, too narrow for BigQuery, awkward for Oracle.

**Change.** Keep the secret resolver opaque to callers. Let each connector parse its own resolved credential payload — a DSN-like string *or* a structured JSON payload — but require **one canonical format per connector** and reject partial or ambiguous forms **before any network access**.

**Acceptance.** PostgreSQL and SQL Server unchanged; Oracle and BigQuery each have one documented credential contract; invalid payloads fail before a socket is opened.

### 1.2 Normalized query-estimate contract

**Problem.** `query_gateway.py` expects a PostgreSQL-shaped `{"Plan": {"Total Cost": ...}}` payload, and SQL Server reshapes XML into that same dict. BigQuery's dry-run bytes-processed does not fit that shape at all.

**Change.** Introduce a connector-agnostic estimate contract:

```text
estimate_kind        COST | BYTES | ROWS | NONE
estimated_rows       optional
estimated_bytes      optional
estimated_cost       optional
raw_evidence         engine-specific, retained for audit
```

Blocking policy stays deterministic **in the gateway**. A connector does not claim `explain=True` until its estimate path is implemented and enforced (INV-9).

**Acceptance.** PostgreSQL and SQL Server map into the normalized shape without losing current guard behaviour; BigQuery exposes dry-run bytes without pretending to have a cost plan; Oracle may remain `explain=False` while its least-privilege explain path is uncertified.

### 1.3 Shared discovery assembly helpers

**Problem.** PostgreSQL and SQL Server each convert row-oriented metadata results into the same `DiscoveredCatalog → Schema → Table → Column → Constraint` graph. Oracle would be a third copy.

**Change.** Extract shared helpers for column-row grouping, primary/unique/foreign-key grouping, object-type normalization, and ordinal ordering. Dialect-specific SQL stays in each connector; common assembly moves to a shared module.

**Acceptance.** SQL Server assembly tests stay green after extraction; Oracle reuses the shared builder rather than carrying a third implementation.

> **Status:** partially delivered. The Oracle adapter already reuses `aida.connectors.discovery` helpers (`build_table_map_from_column_rows`, `append_grouped_key_rows`, `append_grouped_foreign_key_rows`, `assemble_catalog`).

### 1.4 Remove fake connector instantiation from capability exposure

**Problem.** `src/aida/ingestion.py` instantiates connectors with placeholder DSNs to expose capability defaults. This gets worse with every adapter, and it risks a capability being reported from a connector that could never actually connect.

**Change.** Register default capabilities in the registry definition, or expose them as a class-level contract that requires no credentials.

**Acceptance.** `default_capabilities()` constructs no connectors with fake secrets; capability reporting stays honest for implemented and planned connectors (INV-9).

## 2. Workstream A — Framework hardening

**Scope.** Shared discovery assembly · normalized estimate contract and gateway mapping · capability exposure without placeholder construction · connector-specific credential parsing boundaries · reusable connector test fixtures.

**Files expected to change:** `connectors/base.py`, `connectors/registry.py`, `ingestion.py`, `query_gateway.py`, `connectors/sqlserver.py`, `connectors/postgres.py`, `tests/test_connectors.py`, `tests/test_connectors_sqlserver.py`, plus a new shared connector utility module.

**Exit criteria.** Existing PostgreSQL and SQL Server tests pass; the gateway enforces deterministic estimate policy through a connector-agnostic contract; registry metadata describes capabilities without constructing connectors.

## 3. Workstream B — Oracle

**Target.** `oracle` native pull adapter at parity with the SQL Server slice where technically and operationally reasonable. Registry `IMPLEMENTED` · maturity `BETA` · transports `PULL` and `PUSH` · dialect `oracle`.

**Functional scope.** `test_connection` · discovery of catalogs/schema-owners/tables/views/columns · primary, unique, and foreign-key discovery · governed read execution · bounded profiling · estimate support **only if it works under the intended least-privilege model**.

### Status — largely delivered, live verification outstanding

| Task | State |
|---|---|
| One documented credential contract | **Done** — canonical `oracle://user:password@host:port/service_name`; partial or ambiguous forms rejected before network access |
| Driver strategy | **Done** — `python-oracledb` thin mode, genuine async API, no Oracle Client install required |
| Discovery queries | **Done** — `ALL_TAB_COLUMNS`/`ALL_OBJECTS` for columns, `ALL_CONSTRAINTS`/`ALL_CONS_COLUMNS` for keys, scoped by `OWNER` excluding Oracle-supplied schemas; uppercase-folded names normalized before shared assembly |
| Bounded profiling | **Done** — type-aware per-column expressions; LOB-like types (`BLOB`, `CLOB`, `NCLOB`, `LONG`, `LONG RAW`, `BFILE`, `XMLTYPE`), which reject `COUNT(DISTINCT …)` and `TO_CHAR(…)`, fall back to **honest static placeholders** rather than failing the batch or fabricating a value |
| Governed execution | **Done** — real session identifier via `SYS_CONTEXT('USERENV','SID')`, recorded as `oracle-sid:<sid>`, matching the SQL Server and PostgreSQL convention of a real backend-scoped identifier |
| Estimate / explain | **Deliberately disabled** — `EXPLAIN PLAN … / plan_table` lookup is implemented, but `capabilities.explain=False` ships because a least-privilege `PLAN_TABLE` write path is uncertified against a real bank-scoped role. The gateway fails closed with `QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR` |
| Unit tests | **Done** — 14 tests covering credential parsing, identifier quoting, capability declaration, discovery assembly, LOB-aware profiling |
| Compose fixture | **Written, unverified** — `gvenzl/oracle-free:23-slim` with least-privilege `retail`/`risk` owners and a read-only `source` user, mirroring the PostgreSQL and SQL Server fixture schema including the cross-schema FK requiring an explicit `GRANT REFERENCES` |
| **Live container verification** | **OUTSTANDING** — `docker compose up` and live connection/discovery/profiling against the fixture have not been run |

**Remaining acceptance.** Live fixture run with exact discovery counts; 100-point certification; a certified least-privilege `PLAN_TABLE` path before `explain` is enabled.

## 4. Workstream C — BigQuery

**Target.** `bigquery` native pull adapter optimized for governed metadata retrieval and **dry-run-aware** analytical control — not pretending BigQuery behaves like a traditional OLTP database. Registry `IMPLEMENTED` · maturity `BETA` · transports `PULL` and `PUSH` · dialect `bigquery`.

**Functional scope.** `test_connection` · discovery of projects/datasets/tables/views/columns · constraint discovery only where reliably exposed · dry-run estimation · governed read execution · bounded profiling within explicit byte and row limits.

### Status — implemented, live verification outstanding

| # | Task | State |
|---|---|---|
| 1 | Credential contract | **Done** — one canonical structured JSON payload (`project_id`, `location`, `auth_method` of `service_account` \| `workload_identity`, `service_account_info` required only for the former); unknown fields, missing fields, and cross-populated fields (e.g. `service_account_info` under `workload_identity`) are all rejected before any network access |
| 2 | Hierarchy mapping | **Done** — GCP project → catalog, dataset → schema, matching every other connector's internal model |
| 3 | Discovery | **Done** — region-qualified `INFORMATION_SCHEMA.COLUMNS`/`TABLES` across every dataset in the project in one query pair, reusing `aida.connectors.discovery` assembly helpers. Primary-key constraints only; **foreign-key metadata (`CONSTRAINT_COLUMN_USAGE`) is honestly omitted** rather than guessed at, since that view's shape has not been certified against a live project. `column_default` is likewise omitted rather than assumed present on every BigQuery version |
| 4 | Estimate via dry run | **Done** — `dry_run=True` job gives `total_bytes_processed`; row estimate honestly omitted (BigQuery dry runs do not provide one); raw evidence retained |
| 5 | Gateway extension | **Done** — `query_gateway.gate_query_estimate()` is a new connector-agnostic, unit-tested pure function. It selects the byte-budget branch (`Settings.max_bigquery_dry_run_bytes`, independent of `max_postgres_plan_cost`) structurally via `QueryEstimate.estimated_bytes is not None`, so PostgreSQL/SQL Server/Oracle's cost-plan gating is untouched and no gateway change is needed for the next byte-billed connector |
| 6 | Governed execution | **Done** — read-only query submission, `.result(timeout=...)`, BigQuery job ID captured as `bigquery-job:<job_id>` in the same style as `oracle-sid:`/`sqlserver-spid:`. Cancel handling is **not** implemented — no connector implements it yet (tracked as QG-4) |
| 7 | Bounded profiling | **Done** — explicit row bound (`LIMIT`), explicit byte bound (`maximum_bytes_billed` on the job config, enforced by BigQuery before the query runs), and the caller's timeout on every profiling query. REPEATED (array) columns get fully static placeholders (BigQuery rejects `COUNT`/`SUM` on ARRAY-typed arguments); non-repeated RECORD/STRUCT/BYTES/GEOGRAPHY/JSON get a placeholder distinct-count and length only |
| 8 | Tests | **Done** — 29 tests: credential parsing (valid and 11 invalid/ambiguous forms), hierarchy mapping, capability declaration, identifier quoting, region-dataset mapping, discovery assembly (including the foreign-key omission), profiling-expression fallback for REPEATED and complex scalar types, and `gate_query_estimate` (byte budget allow/reject, cost-budget fallback for non-byte estimates, non-finite-score rejection) |
| **Live GCP verification** | **OUTSTANDING** — no live GCP project or credentials were available in this session; `test_connection`/discovery/dry-run estimate/execution/profiling have not been run against a real BigQuery project |

**Acceptance.** Listed in `connector_registry.supported_types`; represented in the matrix and certification path; the gateway **blocks oversized dry-run estimates deterministically before execution**; Atlas and API surfaces describe capabilities honestly.

**Why the byte budget matters more here than elsewhere.** On BigQuery, an unbounded query is not a slow query — it is an invoice. The cost gate is the primary control, not a secondary one.

**Remaining acceptance.** Live project run with exact discovery counts against a real region-qualified `INFORMATION_SCHEMA`; certification; validation that the region-qualified INFORMATION_SCHEMA views behave as documented across BigQuery API versions.

## 5. Workstream D — Certification and fixtures

**Required test layers**

| Layer | Coverage |
|---|---|
| Unit | Parsing, capability declaration, identifier quoting, discovery assembly, estimate normalization |
| Contract | Registry exposure, ingestion capability payloads, gateway fail-closed behaviour |
| Integration | Connection, discovery, profiling, governed query execution |

**Exit criteria.** Oracle and BigQuery each have dedicated connector test files; `tests/test_ingestion.py` asserts honest planned-vs-implemented state; **the verifier and documentation do not imply live support where fixtures or credentials are absent.**

## 6. Execution order

| Sprint | Work |
|---|---|
| 1 | Framework hardening: normalize the estimate contract, extract shared discovery assembly, remove placeholder capability instantiation |
| 2 | Oracle: live fixture verification, certification, registry and ingestion updates |
| 3 | BigQuery: implement, unit-test, extend the gateway for dry-run byte budgets |
| 4 | Integration fixtures and end-to-end certification evidence; update Atlas onboarding and capability messaging; close documentation and verifier gaps |

## 7. Definition of done for this increment

- Oracle and BigQuery are both honest native pull adapters with live evidence.
- **No connector requires fake instantiation for capability reporting.**
- Query estimation is connector-agnostic and still deterministic.
- Both pass unit, contract, and integration coverage comparable to PostgreSQL and SQL Server.
- Atlas and API surfaces describe implemented versus planned support **without overstating breadth** (INV-9).

## Related documents

- Connectivity module: `20-modules/02-connectivity.md`
- Query gateway: `20-modules/16-query-gateway.md`
- Tracker: `60-delivery/03-tracker.md` §B
- Accomplishment log: `60-delivery/06-accomplishment-log.md`
