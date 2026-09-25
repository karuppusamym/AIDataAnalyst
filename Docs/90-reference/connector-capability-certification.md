# Connector capability certification

INV-9: a connector advertises only behaviour that is implemented and passing its
certification. This page renders the committed result
(`src/aida/connectors/capability_certification.json`), which
`scripts/certify_connector_capabilities.py` writes and `--check` verifies. Every
capability flag the platform advertises is **derived** from it: a flag is advertised
only if the connector claims it (`DEFAULT_CAPABILITIES`) and its row here is CERTIFIED.

**Evidence tiers.** LIVE = probed against a real running engine. FIXTURE = proven against
the connector's own driver double; it says the connector's logic works and says nothing
about a real engine. A fixture result is never labelled live.

Suite `connector-capability-certification-v1`, produced 2026-09-24. Regenerating a connector's LIVE
rows needs the sample containers running; `--check` needs nothing.

## Summary

| Connector | Advertised | LIVE | FIXTURE | Not certified | Not applicable | Held |
|---|---|---|---|---|---|---|
| bigquery | 8 | 0 | 9 | 6 | 2 | 0 |
| databricks | 8 | 0 | 8 | 7 | 2 | 0 |
| oracle | 12 | 0 | 13 | 4 | 0 | 0 |
| postgres | 15 | 15 | 0 | 2 | 0 | 0 |
| snowflake | 11 | 0 | 12 | 4 | 1 | 0 |
| sqlserver | 11 | 11 | 0 | 6 | 0 | 0 |

## Uncertified claims

None: every claimed flag is certified.

## Certified but not claimed

The probe passes and the connector declares the flag `False`, so it is not
advertised. Nothing raises a claim automatically: that is a decision for the
connector owner, and a fixture-tier pass says nothing about a real engine.

| Connector | Flag | Tier | Evidence |
|---|---|---|---|
| bigquery | `query_history` | FIXTURE | passed: test_bigquery_get_query_history_maps_rows, test_bigquery_get_query_history_drops_incomplete_rows |
| oracle | `explain` | FIXTURE | EXPLAIN PLAN cost 123, rows 4500 read back; the PLAN_TABLE cleanup and rollback ran. Code path only: whether a least-privilege role may write PLAN_TABLE is unproven (R11-B5) |
| snowflake | `query_history` | FIXTURE | passed: test_snowflake_get_query_history_maps_rows, test_snowflake_get_query_history_drops_incomplete_rows |

## bigquery

Code fingerprint `ded9176cd59a3432` over:

- `aida/capability_states.py`
- `aida/connectors/base.py`
- `aida/connectors/bigquery.py`
- `aida/connectors/discovery.py`
- `aida/connectors/schema_scope.py`
- `aida/connectors/sql_execution.py`

| Flag | Claimed | Advertised | Status | Tier | Probe | Evidence |
|---|---|---|---|---|---|---|
| `catalogs` | yes | yes | CERTIFIED | FIXTURE | discover() returns the project as its catalog | discover() returned one catalog, named for the project |
| `schemas` | yes | yes | CERTIFIED | FIXTURE | discover() returns each dataset as a schema with its tables | dataset retail came back as a schema holding customer and active_customer |
| `constraints` | yes | yes | CERTIFIED | FIXTURE | discover() returns the primary-key constraint from the keys view | customer: PRIMARY_KEY(customer_id) came back from the fixture's KEY_COLUMN_USAGE rows. Foreign keys are honestly omitted. Fixture only: whether the real view exposes the `constraint_type` column this query filters on is not something a double can show |
| `indexes` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | discover() is asked for indexes across the whole estate | discover() returned no index for any of 7 tables: no index read exists |
| `partitions` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | discover() is asked for partitions across the whole estate | discover() returned no partition for any of 7 tables: no partition read |
| `explain` | yes | yes | CERTIFIED | FIXTURE | estimate_read_query() issues a dry-run job through a client double and returns the bytes | a dry-run job with the query cache off was issued; 123456789 bytes read back as the score |
| `query_history` | no | no | CERTIFIED | FIXTURE | get_query_history() through a client double maps job rows to entries | passed: test_bigquery_get_query_history_maps_rows, test_bigquery_get_query_history_drops_incomplete_rows |
| `delegated_identity` | no | no | NOT_CERTIFIED (NOT_EXERCISED) | - | not probed: nothing a test double can exercise | not claimed; bigquery authenticates with the DSN credential alone, so there is no delegated path for a double to exercise |
| `approximate_statistics` | yes | yes | CERTIFIED | FIXTURE | profile_table() through the driver double returns null, non-null and distinct counts | 10 null, 990 non-null, 900 distinct read back from the aggregate; the statement sent uses APPROX_COUNT_DISTINCT |
| `views` | yes | yes | CERTIFIED | FIXTURE | discover() returns a view's definition text | passed: test_bigquery_discover_round_trips_a_view_definition |
| `routines` | yes | yes | CERTIFIED | FIXTURE | discover() returns a routine with its body and parameters | passed: test_a_routine_round_trips_with_its_parameters_and_description |
| `object_comments` | yes | yes | CERTIFIED | FIXTURE | discover() returns schema, table and column descriptions | passed: test_descriptions_land_at_every_level_bigquery_exposes |
| `grants` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | discover() is asked for grants across the whole estate | discover() returned no grant for any of 4 schemas: BigQuery grants are Cloud IAM bindings, which the SQL grant envelope does not model |
| `triggers` | no | no | NOT_APPLICABLE | - | engine fact: the engine has no such object | bigquery has no trigger object (aida.discovery_selection._NO_TRIGGER_KIND) |
| `sequences` | no | no | NOT_APPLICABLE | - | engine fact: the engine has no such object | bigquery has no sequence object (aida.discovery_selection._NO_SEQUENCE_KIND) |
| `value_range_profiling` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | profile_column_values() is called to see whether the connector returns ranges and values | profile_column_values() fails closed with ConnectorValueProfilingUnsupported |
| `distribution_entropy_profiling` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | profile_table() is asked for a column's entropy through the driver double | frequency_entropy_bits is None and the facet status is UNSUPPORTED/NOT_IMPLEMENTED |

## databricks

Code fingerprint `dc6772f5ab447e5e` over:

- `aida/capability_states.py`
- `aida/connectors/base.py`
- `aida/connectors/databricks.py`
- `aida/connectors/discovery.py`
- `aida/connectors/schema_scope.py`
- `aida/connectors/sql_execution.py`

| Flag | Claimed | Advertised | Status | Tier | Probe | Evidence |
|---|---|---|---|---|---|---|
| `catalogs` | yes | yes | CERTIFIED | FIXTURE | discover() returns the configured catalog with its comment | passed: test_databricks_discover_assembles_catalog_with_constraints |
| `schemas` | yes | yes | CERTIFIED | FIXTURE | discover() returns the schema with its tables | passed: test_databricks_discover_assembles_catalog_with_constraints |
| `constraints` | yes | yes | CERTIFIED | FIXTURE | discover() returns PRIMARY KEY and FOREIGN KEY constraints from information_schema rows | passed: test_databricks_discover_assembles_catalog_with_constraints |
| `indexes` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | discover() is asked for indexes across the whole estate | discover() returned no index for any of 7 tables: no index read exists |
| `partitions` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | discover() is asked for partitions across the whole estate | discover() returned no partition for any of 7 tables: no partition read |
| `explain` | yes | yes | CERTIFIED | FIXTURE | estimate_read_query() runs EXPLAIN COST through a cursor double and parses its statistics | passed: test_databricks_estimate_read_query_uses_explain_cost |
| `query_history` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | get_query_history() is called to see whether the connector reads warehouse history | get_query_history() fails closed with ConnectorQueryHistoryUnsupported |
| `delegated_identity` | no | no | NOT_CERTIFIED (NOT_EXERCISED) | - | not probed: nothing a test double can exercise | not claimed; databricks authenticates with the DSN credential alone, so there is no delegated path for a double to exercise |
| `approximate_statistics` | yes | yes | CERTIFIED | FIXTURE | profile_table() through a cursor double returns bounded counts and approximate distincts | passed: test_databricks_profile_table_computes_bounded_stats |
| `views` | yes | yes | CERTIFIED | FIXTURE | discover() returns a view's definition text from information_schema.views | passed: test_a_databricks_view_definition_round_trips |
| `routines` | yes | yes | CERTIFIED | FIXTURE | discover() returns a routine with its body and parameters | passed: test_a_databricks_routine_round_trips_with_its_parameters |
| `object_comments` | yes | yes | CERTIFIED | FIXTURE | discover() returns catalog, schema, table and column comments | passed: test_databricks_discover_assembles_catalog_with_constraints |
| `grants` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | discover() is asked for grants across the whole estate | discover() returned no grant for any of 4 schemas: the grant axis is not implemented (Unity Catalog's privilege model is not the SQL grant model) |
| `triggers` | no | no | NOT_APPLICABLE | - | engine fact: the engine has no such object | databricks has no trigger object (aida.discovery_selection._NO_TRIGGER_KIND) |
| `sequences` | no | no | NOT_APPLICABLE | - | engine fact: the engine has no such object | databricks has no sequence object (aida.discovery_selection._NO_SEQUENCE_KIND) |
| `value_range_profiling` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | profile_column_values() is called to see whether the connector returns ranges and values | profile_column_values() fails closed with ConnectorValueProfilingUnsupported |
| `distribution_entropy_profiling` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | profile_table() is asked for a column's entropy through the driver double | frequency_entropy_bits is None and the facet status is UNSUPPORTED/NOT_IMPLEMENTED |

## oracle

Code fingerprint `d350b5a01f5bd60f` over:

- `aida/capability_states.py`
- `aida/connectors/base.py`
- `aida/connectors/discovery.py`
- `aida/connectors/oracle.py`
- `aida/connectors/schema_scope.py`
- `aida/connectors/sql_execution.py`

| Flag | Claimed | Advertised | Status | Tier | Probe | Evidence |
|---|---|---|---|---|---|---|
| `catalogs` | yes | yes | CERTIFIED | FIXTURE | discover() returns the database as its catalog | discover() returned one catalog, named for the database |
| `schemas` | yes | yes | CERTIFIED | FIXTURE | discover() returns each owner as a schema, with its objects | SALES, SALES_TMP, HR and FIN came back; SALES holds FACT_ORDERS and DIM_CUSTOMER |
| `constraints` | yes | yes | CERTIFIED | FIXTURE | discover() returns PRIMARY KEY and FOREIGN KEY constraints | FACT_ORDERS: PRIMARY_KEY(ID) and FOREIGN_KEY(ID) -> DIM_CUSTOMER(ID) |
| `indexes` | yes | yes | CERTIFIED | FIXTURE | discover() returns the index rows with their columns and flags | FACT_ORDERS: IX_FACT_ORDERS on (ID), unique and primary |
| `partitions` | yes | yes | CERTIFIED | FIXTURE | discover() returns partition name, type and key columns | FACT_ORDERS: partition P2026, RANGE, key (ID); bounds are deliberately not read |
| `explain` | no | no | CERTIFIED | FIXTURE | estimate_read_query() runs EXPLAIN PLAN through a driver double and returns its cost | EXPLAIN PLAN cost 123, rows 4500 read back; the PLAN_TABLE cleanup and rollback ran. Code path only: whether a least-privilege role may write PLAN_TABLE is unproven (R11-B5) |
| `query_history` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | get_query_history() is called to see whether the connector reads warehouse history | get_query_history() fails closed with ConnectorQueryHistoryUnsupported |
| `delegated_identity` | no | no | NOT_CERTIFIED (NOT_EXERCISED) | - | not probed: nothing a test double can exercise | not claimed; oracle authenticates with the DSN credential alone, so there is no delegated path for a double to exercise |
| `approximate_statistics` | yes | yes | CERTIFIED | FIXTURE | profile_table() through the driver double returns null, non-null and distinct counts | 10 null, 990 non-null, 900 distinct read back from the aggregate; the statement sent uses COUNT(DISTINCT |
| `views` | yes | yes | CERTIFIED | FIXTURE | discover() returns view and materialized-view definition text | V_REVENUE definition came back; the materialized view came back flagged |
| `routines` | yes | yes | CERTIFIED | FIXTURE | discover() returns procedures, functions and package members | PROC_LOAD, FN_TAX, package RISK_PKG and its members SCORE and RECALC came back |
| `object_comments` | yes | yes | CERTIFIED | FIXTURE | discover() returns table and column comments | FACT_ORDERS table comment and its first column's comment came back |
| `grants` | yes | yes | CERTIFIED | FIXTURE | discover() returns object privileges with grantee and privilege | SELECT on FACT_ORDERS and EXECUTE on RISK_PKG, both to REPORTING, came back |
| `triggers` | yes | yes | CERTIFIED | FIXTURE | discover() returns a trigger with table, timing and event | TRG_ORDERS_AUDIT on FACT_ORDERS: AFTER ROW INSERT, body present |
| `sequences` | yes | yes | CERTIFIED | FIXTURE | discover() returns a sequence declaration and never its position | SEQ_ORDERS increment 1, cache 20; no statement read LAST_NUMBER |
| `value_range_profiling` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | profile_column_values() is called to see whether the connector returns ranges and values | profile_column_values() fails closed with ConnectorValueProfilingUnsupported |
| `distribution_entropy_profiling` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | profile_table() is asked for a column's entropy through the driver double | frequency_entropy_bits is None and the facet status is UNSUPPORTED/NOT_IMPLEMENTED |

## postgres

Code fingerprint `eee937f3a99eaf51` over:

- `aida/capability_states.py`
- `aida/connectors/base.py`
- `aida/connectors/discovery.py`
- `aida/connectors/postgres.py`
- `aida/connectors/postgres_pool.py`
- `aida/connectors/schema_scope.py`
- `aida/connectors/sql_execution.py`

LIVE rows ran against: driver asyncpg 0.31.0, server_version PostgreSQL 17.11.

| Flag | Claimed | Advertised | Status | Tier | Probe | Evidence |
|---|---|---|---|---|---|---|
| `catalogs` | yes | yes | CERTIFIED | LIVE | discover() returns the connected database as its catalog | discover() returned 1 catalog holding 3 schemas |
| `schemas` | yes | yes | CERTIFIED | LIVE | discover() returns the fixture schema, with its tables, inside it | schema c14_probe came back with 6 objects, e.g. customer, order_fact |
| `constraints` | yes | yes | CERTIFIED | LIVE | discover() returns the PRIMARY KEY, UNIQUE and FOREIGN KEY constraints the fixture made | customer: PRIMARY_KEY(customer_id) and UNIQUE(email); order_fact: FOREIGN_KEY(customer_id) -> customer(customer_id) |
| `indexes` | yes | yes | CERTIFIED | LIVE | discover() returns the secondary index the fixture created | order_fact_customer_idx on (customer_id) came back |
| `partitions` | yes | yes | CERTIFIED | LIVE | discover() returns the range partitions of order_fact | order_fact: 2 RANGE partitions keyed on order_date |
| `explain` | yes | yes | CERTIFIED | LIVE | estimate_read_query() returns a real plan cost for a statement, without running it | EXPLAIN_COST returned plan cost 8.17, estimated rows 1 |
| `query_history` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | LIVE | get_query_history() is called on the live connector to see whether it reads history | get_query_history() fails closed with ConnectorQueryHistoryUnsupported |
| `delegated_identity` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | LIVE | the connector runs as some identity; probe whether it is ever anyone but its own login | the session runs as the DSN's own login; the connector takes no per-caller identity |
| `approximate_statistics` | yes | yes | CERTIFIED | LIVE | profile_table() returns bounded value-free counts matching the fixture, and the scope | segment: 4 distinct, 0 null, 16 non-null over a 16-row scan reported FULL; a 5-row bound sampled 5 and reported SAMPLE |
| `views` | yes | yes | CERTIFIED | LIVE | discover() returns the view's definition text, not just its name | customer_orders: definition_sql present and reads order_fact; customer_orders_mv came back flagged is_materialized |
| `routines` | yes | yes | CERTIFIED | LIVE | discover() returns a function and a procedure with bodies and parameters | order_total FUNCTION (1 parameter) and touch_customer PROCEDURE, both with body text |
| `object_comments` | yes | yes | CERTIFIED | LIVE | discover() returns the schema, table and column comments the fixture attached | schema, table customer and column customer.segment each returned their comment |
| `grants` | yes | yes | CERTIFIED | LIVE | discover() returns the SELECT grant to PUBLIC that the fixture made on customer | SELECT on customer granted to PUBLIC came back |
| `triggers` | yes | yes | CERTIFIED | LIVE | discover() returns the trigger the fixture created, with table and event | customer_audit on customer: BEFORE UPDATE |
| `sequences` | yes | yes | CERTIFIED | LIVE | discover() returns the sequence's declaration, never its position | invoice_seq declared start 100, increment 5 |
| `value_range_profiling` | yes | yes | CERTIFIED | LIVE | profile_column_values() returns min, max and top values that match the fixture | segment: min alpha, max gamma, top values alpha x8 then beta x4 |
| `distribution_entropy_profiling` | yes | yes | CERTIFIED | LIVE | profile_table() returns the Shannon entropy of customer.segment, matching the analytic | segment entropy 1.7500 bits, analytic value 1.75 |

## snowflake

Code fingerprint `bb5f50ad1e0176df` over:

- `aida/capability_states.py`
- `aida/connectors/base.py`
- `aida/connectors/discovery.py`
- `aida/connectors/schema_scope.py`
- `aida/connectors/snowflake.py`
- `aida/connectors/sql_execution.py`

| Flag | Claimed | Advertised | Status | Tier | Probe | Evidence |
|---|---|---|---|---|---|---|
| `catalogs` | yes | yes | CERTIFIED | FIXTURE | discover() returns the database as its catalog | passed: test_snowflake_discover_assembly |
| `schemas` | yes | yes | CERTIFIED | FIXTURE | discover() returns the schema with its tables and columns | passed: test_snowflake_discover_assembly |
| `constraints` | yes | yes | CERTIFIED | FIXTURE | discover() returns PRIMARY KEY and FOREIGN KEY constraints | passed: test_snowflake_discover_assembly |
| `indexes` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | discover() is asked for indexes across the whole estate | discover() returned no index for any of 8 tables: no index read exists |
| `partitions` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | discover() is asked for partitions across the whole estate | discover() returned no partition for any of 8 tables: the adapter has no partition read; only EXPLAIN's pruning counters mention partitions |
| `explain` | yes | yes | CERTIFIED | FIXTURE | estimate_read_query() runs EXPLAIN USING JSON through a driver double and parses the plan | EXPLAIN USING JSON sent; plan parsed to 50000 rows, 10 MiB, 85% partition pruning. An unparseable plan falls back to score 1.0, the cheapest, which this does not exercise |
| `query_history` | no | no | CERTIFIED | FIXTURE | get_query_history() through a cursor double maps history rows to entries | passed: test_snowflake_get_query_history_maps_rows, test_snowflake_get_query_history_drops_incomplete_rows |
| `delegated_identity` | yes | yes | CERTIFIED | FIXTURE | a DSN carrying authenticator and token reaches the driver connect() as such, with no password | authenticator=oauth and the token reached connect(), and no password was sent. Pass-through only: the URI form of the DSN drops `token`, and nothing here supplies a per-user token |
| `approximate_statistics` | yes | yes | CERTIFIED | FIXTURE | profile_table() through the driver double returns null, non-null and distinct counts | 10 null, 990 non-null, 900 distinct read back from the aggregate; the statement sent uses APPROX_COUNT_DISTINCT |
| `views` | yes | yes | CERTIFIED | FIXTURE | discover() returns a view's definition text | passed: test_snowflake_discover_round_trips_a_view_definition |
| `routines` | yes | yes | CERTIFIED | FIXTURE | discover() returns a routine with its body, return type and parsed parameters | passed: test_a_routine_round_trips_with_parameters_parsed_from_its_signature |
| `object_comments` | yes | yes | CERTIFIED | FIXTURE | discover() returns catalog, schema, table and column comments | passed: test_comments_land_at_every_level_snowflake_exposes |
| `grants` | yes | yes | CERTIFIED | FIXTURE | discover() returns schema-level grants | passed: test_schema_grants_land_on_the_schema |
| `triggers` | no | no | NOT_APPLICABLE | - | engine fact: the engine has no such object | snowflake has no trigger object (aida.discovery_selection._NO_TRIGGER_KIND) |
| `sequences` | yes | yes | CERTIFIED | FIXTURE | discover() returns a sequence's declaration and never its position | passed: test_a_snowflake_sequence_is_its_declaration_and_never_its_position |
| `value_range_profiling` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | profile_column_values() is called to see whether the connector returns ranges and values | profile_column_values() fails closed with ConnectorValueProfilingUnsupported |
| `distribution_entropy_profiling` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | FIXTURE | profile_table() is asked for a column's entropy through the driver double | frequency_entropy_bits is None and the facet status is UNSUPPORTED/NOT_IMPLEMENTED |

## sqlserver

Code fingerprint `74661199e7b1f583` over:

- `aida/capability_states.py`
- `aida/connectors/base.py`
- `aida/connectors/discovery.py`
- `aida/connectors/schema_scope.py`
- `aida/connectors/sql_execution.py`
- `aida/connectors/sqlserver.py`

LIVE rows ran against: driver python-tds 1.17.1, server_version SQL Server 16.0.4265.3 (Developer Edition (64-bit)).

| Flag | Claimed | Advertised | Status | Tier | Probe | Evidence |
|---|---|---|---|---|---|---|
| `catalogs` | yes | yes | CERTIFIED | LIVE | discover() returns the connected database as its catalog | discover() returned 1 catalog holding 1 schemas |
| `schemas` | yes | yes | CERTIFIED | LIVE | discover() returns the fixture schema, with its tables, inside it | schema c14_probe came back with 4 objects, e.g. customer, order_fact |
| `constraints` | yes | yes | CERTIFIED | LIVE | discover() returns the PRIMARY KEY, UNIQUE and FOREIGN KEY constraints the fixture made | customer: PRIMARY_KEY(customer_id) and UNIQUE(email); order_fact: FOREIGN_KEY(customer_id) -> customer(customer_id) |
| `indexes` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | LIVE | discover() is asked for the index on order_fact (ix_order_customer) the fixture created | order_fact has ix_order_customer, and discover() returned no index for it |
| `partitions` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | LIVE | discover() is asked for the partitions of c14_probe.part_t, which the fixture partitioned | part_t is partitioned into 3 ranges, and discover() returned no partition |
| `explain` | yes | yes | CERTIFIED | LIVE | estimate_read_query() returns a real plan cost for a statement, without running it | SHOWPLAN_XML returned plan cost 0.003283, estimated rows 1 |
| `query_history` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | LIVE | get_query_history() is called on the live connector to see whether it reads history | get_query_history() fails closed with ConnectorQueryHistoryUnsupported |
| `delegated_identity` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | LIVE | the connector runs as some identity; probe whether it is ever anyone but its own login | the session runs as the DSN's own login; the connector takes no per-caller identity |
| `approximate_statistics` | yes | yes | CERTIFIED | LIVE | profile_table() returns bounded value-free counts matching the fixture, and the scope | segment: 4 distinct, 0 null, 16 non-null over a 16-row scan reported FULL; a 5-row bound sampled 5 and reported SAMPLE |
| `views` | yes | yes | CERTIFIED | LIVE | discover() returns the view's definition text, not just its name | customer_orders: definition_sql present and reads order_fact |
| `routines` | yes | yes | CERTIFIED | LIVE | discover() returns a function and a procedure with bodies and parameters | order_total FUNCTION (1 parameter) and touch_customer PROCEDURE, both with body text |
| `object_comments` | yes | yes | CERTIFIED | LIVE | discover() returns the schema, table and column comments the fixture attached | schema, table customer and column customer.segment each returned their comment |
| `grants` | yes | yes | CERTIFIED | LIVE | discover() returns the object-level SELECT grant the fixture gave the connector's login | SELECT on customer granted to the connector's own login came back |
| `triggers` | yes | yes | CERTIFIED | LIVE | discover() returns the trigger the fixture created, with table and event | customer_audit on customer: AFTER UPDATE |
| `sequences` | yes | yes | CERTIFIED | LIVE | discover() returns the sequence's declaration, never its position | invoice_seq declared start 100, increment 5 |
| `value_range_profiling` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | LIVE | profile_column_values() is called to see if the connector returns ranges and top values | profile_column_values() fails closed with ConnectorValueProfilingUnsupported |
| `distribution_entropy_profiling` | no | no | NOT_CERTIFIED (NOT_IMPLEMENTED) | LIVE | profile_table() is asked for the entropy of customer.segment | frequency_entropy_bits is None and the facet status is UNSUPPORTED/NOT_IMPLEMENTED |
