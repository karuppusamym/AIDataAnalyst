# 18 - Oracle and BigQuery Connector Implementation Backlog

## Purpose

This document turns the next connector priority into executable work against the current repository. The immediate goal is not "add more connector names to the registry." The goal is to add Oracle and BigQuery as honest, governed native pull adapters while improving the connector framework so each additional adapter does not duplicate PostgreSQL and SQL Server patterns.

The current priority order is:

1. Refactor the connector framework where the existing seams are already visible.
2. Implement the Oracle connector.
3. Implement the BigQuery connector.
4. Add executable certification and fixture coverage for both.
5. Continue with retrieval, quality, lineage, stewardship, UX, and enterprise trust work after the connector foundation is stronger.

## Existing repo touchpoints

The current implementation already provides the right integration seams:

- `src/aida/connectors/base.py`
- `src/aida/connectors/registry.py`
- `src/aida/connectors/postgres.py`
- `src/aida/connectors/sqlserver.py`
- `src/aida/ingestion.py`
- `src/aida/query_gateway.py`
- `src/aida/workflows/activities.py`
- `tests/test_connectors.py`
- `tests/test_connectors_sqlserver.py`
- `tests/test_ingestion.py`

The backlog below assumes those seams remain authoritative and that new adapters must satisfy the same fail-closed posture: connection test, discovery, explain or estimate, governed read execution, bounded profiling, certification evidence, and honest UI/API status.

## Design decisions for the next connector wave

Before adding Oracle and BigQuery, tighten these design points:

### 1. Replace DSN-only assumptions with connector-owned connection parsing

`ConnectorRegistry.create()` currently passes a single resolved secret string into each connector. That is adequate for PostgreSQL and barely adequate for SQL Server, but it is too narrow for BigQuery and awkward for Oracle.

Required change:

- Keep the secret resolver opaque to callers.
- Let each connector parse its own resolved credential payload.
- Allow connectors to accept either a DSN-like string or a structured JSON secret payload, but require one canonical format per connector and reject partial or ambiguous forms.

Acceptance:

- PostgreSQL and SQL Server continue to work unchanged.
- Oracle has one documented credential contract.
- BigQuery has one documented credential contract.
- Invalid or incomplete credential payloads fail before network access.

### 2. Replace the Postgres-shaped explain contract with a normalized estimate contract

`query_gateway.py` currently expects a Postgres-shaped `{"Plan": {"Total Cost": ...}}` payload, and SQL Server reshapes XML into that same dict. That will not scale cleanly to BigQuery.

Required change:

- Introduce a connector-agnostic query estimate contract, for example:
  - estimate kind
  - estimated rows if available
  - estimated bytes if available
  - estimated cost or score if available
  - raw engine-specific evidence
- Keep blocking policy deterministic in the gateway.
- Do not claim `explain=True` for a connector until the estimate path is implemented and enforced.

Acceptance:

- PostgreSQL and SQL Server map into the normalized estimate shape without losing current guard behavior.
- BigQuery can expose dry-run bytes processed without pretending it has a Postgres cost plan.
- Oracle can remain `explain=False` temporarily if a least-privilege explain path is not yet approved.

### 3. Extract shared discovery assembly helpers

PostgreSQL and SQL Server both convert row-oriented metadata query results into the same `DiscoveredCatalog -> DiscoveredSchema -> DiscoveredTable -> DiscoveredColumn -> DiscoveredConstraint` graph. That assembly logic should not be rewritten for every adapter.

Required change:

- Add shared helper(s) for:
  - column row grouping
  - primary/unique/foreign-key grouping
  - object type normalization
  - ordinal ordering
- Keep dialect-specific SQL in each connector, but move common assembly into a shared module.

Acceptance:

- SQL Server assembly tests remain green after the extraction.
- Oracle reuses the shared builder rather than carrying a third one-off implementation.

### 4. Remove fake connector instantiation from capability exposure

`src/aida/ingestion.py` currently instantiates connectors with placeholder DSNs to expose capability defaults. That pattern will get worse with Oracle and BigQuery.

Required change:

- Register default capabilities directly in the registry definition, or
- expose capabilities as a class-level/static contract that does not require fake credentials.

Acceptance:

- `default_capabilities()` does not construct connectors with fake secrets.
- Capability reporting stays honest for implemented and planned connectors.

## Workstream A - Connector framework hardening

### Scope

- Add shared discovery assembly utilities.
- Add normalized query estimate contract and gateway mapping.
- Refactor capability exposure away from placeholder connector construction.
- Add connector-specific credential payload parsing boundaries.
- Add reusable connector test fixtures for registry, parsing, capability, and contract behavior.

### Files expected to change

- `src/aida/connectors/base.py`
- `src/aida/connectors/registry.py`
- `src/aida/ingestion.py`
- `src/aida/query_gateway.py`
- `src/aida/connectors/sqlserver.py`
- `src/aida/connectors/postgres.py`
- `tests/test_connectors.py`
- `tests/test_connectors_sqlserver.py`
- new shared connector utility module under `src/aida/connectors/`

### Exit criteria

- Existing PostgreSQL and SQL Server tests still pass.
- The gateway enforces deterministic estimate policy through a connector-agnostic contract.
- Registry metadata can describe capabilities without constructing connectors.

## Workstream B - Oracle connector

### Target outcome

Add an `oracle` native pull adapter that reaches feature parity with the current SQL Server slice where technically and operationally reasonable.

### Recommended implementation shape

- New module: `src/aida/connectors/oracle.py`
- Registry status: change `oracle` from `PLANNED` to `IMPLEMENTED`
- Maturity target: `BETA`
- Transport target: `PULL` and `PUSH`
- Dialect target: `oracle`

### Functional scope

- `test_connection`
- metadata discovery for catalogs/schema-owner/tables/views/columns
- primary, unique, and foreign-key discovery
- governed read execution
- bounded profiling
- estimate or explain support only if it works under the intended least-privilege model

### Technical tasks

1. Choose and document one Oracle credential contract.
   Recommendation:
   use a resolved secret payload that clearly distinguishes host, port, service name, user, and password, rather than supporting multiple ad hoc Oracle URL variants.

2. Select the Python driver strategy.
   Recommendation:
   prefer a thin-mode pure-Python-compatible path if it preserves the project's low local setup burden; only introduce a client-library dependency if the required feature set or bank standard demands it.

3. Implement discovery queries against Oracle metadata views.
   Required outputs:
   schemas, tables, views, columns, defaults, nullability, and constraints mapped into the shared discovery shape.

4. Implement bounded profiling.
   Required outputs:
   row count estimate if available, bounded sampled row count, null counts, non-null counts, approximate or exact distinct count policy, and length bounds.

5. Implement governed execution.
   Required outputs:
   read-only query execution, timeout handling, warehouse query identifier if Oracle exposes one through a safe path.

6. Implement estimate or explain handling.
   Rule:
   if a least-privilege explain path is not available, ship the connector honestly with `explain=False` first rather than fabricating partial support.

7. Add unit and integration tests.
   Required coverage:
   credential parsing, identifier quoting, discovery assembly, capability declaration, estimate behavior, and bounded profiling invariants.

### Acceptance criteria

- Oracle is listed in `connector_registry.supported_types`.
- Oracle datasource onboarding, connection testing, discovery, and profiling run through the existing APIs and workflows.
- Query gateway behavior remains fail closed.
- Certification output shows Oracle as implemented with accurate capability evidence.

## Workstream C - BigQuery connector

### Target outcome

Add a `bigquery` native pull adapter optimized for governed metadata retrieval and dry-run-aware analytical control rather than pretending BigQuery behaves like a traditional OLTP database.

### Recommended implementation shape

- New module: `src/aida/connectors/bigquery.py`
- Registry status: add `bigquery` as `IMPLEMENTED`
- Maturity target: `BETA`
- Transport target: `PULL` and `PUSH`
- Dialect target: `bigquery`

### Functional scope

- `test_connection`
- metadata discovery for projects/datasets/tables/views/columns
- constraint discovery only if exposed reliably through approved metadata views
- dry-run query estimation
- governed read execution
- bounded profiling within explicit byte and row limits

### Technical tasks

1. Define the credential contract.
   Recommendation:
   accept one structured resolved secret payload for service-account or approved workload-identity configuration rather than forcing BigQuery into a fake DSN string.

2. Decide the catalog hierarchy mapping.
   Recommendation:
   map GCP project to catalog and dataset to schema so the current internal model stays consistent.

3. Implement discovery through BigQuery metadata views and APIs.
   Required outputs:
   datasets, tables, views, columns, object types, descriptions when available, and honest omission of unsupported metadata.

4. Implement estimate handling through dry runs.
   Required outputs:
   estimated bytes processed, any row estimate the engine provides, and raw engine evidence for audit.

5. Extend query-gateway policy for BigQuery-specific controls.
   Required outputs:
   deterministic byte budget checks in addition to SQL shape validation.

6. Implement governed execution.
   Required outputs:
   read-only query submission, timeout/cancel handling, and job ID capture for audit.

7. Implement bounded profiling carefully.
   Rule:
   profile queries must carry explicit sample, byte, and timeout bounds so the connector does not create uncontrolled cost.

8. Add unit and integration tests.
   Required coverage:
   credential parsing, hierarchy mapping, capability declaration, dry-run estimate normalization, and policy-bound profiling behavior.

### Acceptance criteria

- BigQuery is listed in `connector_registry.supported_types`.
- BigQuery is represented in the registry matrix and certification path.
- The gateway blocks oversized dry-run estimates deterministically before execution.
- Atlas and API surfaces describe BigQuery capabilities honestly.

## Workstream D - Certification and fixtures

### Scope

- Add connector-specific tests similar to `tests/test_connectors_sqlserver.py`
- Extend ingestion/certification tests for Oracle and BigQuery registry honesty
- Add repeatable integration targets for both connectors where practical
- Update local and Docker documentation with explicit prerequisites and limitations

### Required test layers

1. Unit tests
   - parsing
   - capability declaration
   - identifier quoting
   - discovery assembly
   - estimate normalization

2. Contract tests
   - registry exposure
   - ingestion capability payloads
   - query gateway fail-closed behavior

3. Integration tests
   - connection
   - discovery
   - profiling
   - governed query execution

### Exit criteria

- Oracle and BigQuery each have dedicated connector test files.
- `tests/test_ingestion.py` asserts honest planned vs implemented connector state.
- The verifier and docs do not imply live support where fixtures or credentials are absent.

## Suggested execution order

### Sprint 1

- Harden connector framework
- normalize query estimate contract
- extract shared discovery assembly
- remove placeholder capability instantiation

### Sprint 2

- Implement Oracle connector
- add Oracle unit tests
- wire registry, ingestion, and certification updates

### Sprint 3

- Implement BigQuery connector
- add BigQuery unit tests
- extend query gateway for dry-run byte budgets

### Sprint 4

- Add integration fixtures and end-to-end certification evidence
- update Atlas onboarding and capability messaging
- close documentation and verifier gaps

## Definition of done for this connector increment

This increment is done when:

- Oracle and BigQuery are both implemented as honest native pull adapters.
- No connector requires fake instantiation for capability reporting.
- Query estimation is connector-agnostic and still deterministic.
- New connectors pass unit, contract, and integration coverage comparable to PostgreSQL and SQL Server.
- Atlas and API surfaces describe implemented versus planned support without overstating breadth.
