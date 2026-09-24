# Read-only posture by engine

Dated 2026-09-24, tracker row R11-MP22. What stops a governed statement from changing a
source, per engine, and what is checked about the account Atlas connects as.

Every engine shares the first layer: the query gateway (`QueryExecutionGateway.execute` in
`src/aida/query_gateway.py`) parses each statement with `SqlGuard.validate`
(`src/aida/sql_guard.py`) and refuses anything that is not a single read. What differs is
whether a second layer exists.

| Engine | Second layer at execution | Account probed at discovery |
|---|---|---|
| PostgreSQL | Each read runs in `connection.transaction(readonly=True)` with a statement timeout (`src/aida/connectors/postgres.py`) | Yes: database CREATE, CREATE on any user schema, or INSERT/UPDATE/DELETE/TRUNCATE on any user table (`POSTGRES_WRITE_PROBE`) |
| SQL Server | None enforced by the server. `readonly=True` on the pytds connection sets the TDS read-only application intent, which routes to a readable replica and refuses no write | Yes: database-level INSERT/UPDATE/DELETE/ALTER, or db_owner, db_datawriter or db_ddladmin membership (`SQLSERVER_WRITE_PROBE`). Object-level grants are not checked |
| Oracle | The connector rolls back after each read (`src/aida/connectors/oracle.py`) | No |
| Snowflake, BigQuery, Databricks | None; the account's grants | No |

## What happens when an account can write

`check_source_write_access` in `src/aida/workflows/activities.py` runs the probe right after
the connection test at the start of every discovery run. A writable account is logged
(`source_account_can_write`) and recorded as an audit row (`datasource.source_account_can_write`)
naming what was found. With `source_write_access_policy` set to `REFUSE`, discovery stops;
the default is `WARN`. A probe that itself fails is logged and never stops discovery.

## Recommendation

Connect every source with an account that has read grants only. On the engines with no
second layer and no probe, that account is the only thing behind the parser.
