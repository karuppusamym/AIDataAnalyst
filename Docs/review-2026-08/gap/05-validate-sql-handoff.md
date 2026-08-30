# N14 — `validate_sql`: handoff

Status: implemented. Item **N14** (`02-gap-diff-and-plan.md`, "highest value per
line of code in this plan") and item **C4 / ST-11** (lineage ↔ gateway cycle).

The premise held: everything needed already existed inside
`QueryExecutionGateway.execute`. The work was splitting *validation* from
*execution* without letting the two drift, and exposing the validator over MCP
and HTTP.

---

## 1. What was built

### `src/aida/sql_validation.py` (new)

The finding vocabulary and the pure, value-free assembly of a report. It holds
**no** connector access and **no** database access, deliberately:
`aida.connectors.execution_access` is protected by the import-linter contract
"INV-2 connector SQL execution is reachable only from the query gateway", so the
only module allowed to reach a source is `aida.query_gateway`. Putting the
catalog reads or the dry run in this module would have required widening that
contract — the one change the contract's own comment says needs an ADR.

Contents: `SqlFinding`, `SqlValidationReport`, `EstimateOutcome`,
`ColumnReference`, and the pure builders `findings_from_guard`,
`row_limit_finding`, `findings_from_catalog`, `findings_from_columns`,
`findings_from_estimate`, `resolve_column_references`, `locally_defined_names`,
`build_report`.

### `src/aida/query_gateway.py` (refactored)

* `QueryExecutionGateway._run_validation` — **the single deterministic
  pipeline.** sqlglot parse → `SqlGuard` read-only, structural and limit checks
  → referenced table/column extraction → catalog resolution via
  `allowed_tables` → per-object authorisation → column resolution via the new
  `_catalog_columns` → column-lineage extraction → dry-run estimate through
  `gate_query_estimate` when `capabilities.explain` is advertised.
* `QueryExecutionGateway.validate` — public entry point. Calls
  `_run_validation`, records the attempt to the audit trail, commits, returns a
  value-free `SqlValidationReport`. Never executes.
* `QueryExecutionGateway.execute` — **now calls `_run_validation` too.** It no
  longer contains a line of validation logic of its own: it takes the report,
  copies the persisted evidence off it, raises `QueryRejected` with
  `report.rejection_reason()` when the report is invalid, and otherwise runs
  `execute_read_query` against the executor the validation pass already opened.
  Validation and execution cannot disagree, because there is one
  implementation and two entry points.
* `_catalog_columns` — active column names for the referenced tables, keyed with
  the same qualified/unqualified variants `allowed_tables` uses, bounded by the
  statement's own table list rather than loading the datasource's whole column
  catalog.

The phases short-circuit exactly as before: the source is contacted for a dry
run only once the statement has passed the guard and every referenced object has
resolved and authorised. A bad statement never reaches the warehouse.

### `src/aida/sql_validation_api.py` (new)

`POST /v1/datasources/{datasource_id}/sql-validations`. **Not wired into
`api.py` / `main.py`** — see §4. An invalid statement is a `200` with
`valid: false`, not a 4xx: the findings *are* the answer, and an error status
would make the iterate-against-the-compiler loop harder to consume. 4xx is
reserved for the request being unusable (unknown datasource, cross-org access,
disabled datasource).

Kept separate from the existing `POST /v1/query/validate`, which is the
*guard-only* check (parse, read-only, structural rules) with no datasource, no
catalog binding, no authorisation and no cost estimate. Both are useful; the
cheap one needs no source contact at all.

### `src/aida/mcp_server.py`

Native tool `atlas__validate_sql`, in the existing `atlas__` convention:
advertised in `tools/list` with a full JSON schema, dispatched in `tools/call`,
role-gated with the same `_tool_role_eligible` helper the governed tools use,
re-checked inside the handler (a `tools/call` can arrive without a preceding
`tools/list`), and refusing an ineligible caller with the identical wording used
for an unknown tool — the anti-enumeration shape the lineage and marketplace
tools already use. It rides the standard `tools/call` MCP budget bucket
(`REQUEST_MINUTE` + `TOOL_DAY`). There is no agent bypass.

### `pyproject.toml`

One appended `[[tool.importlinter.contracts]]` block — see §5.

### `tests/test_sql_validation.py` (new, 16 tests)

---

## 2. Finding-code vocabulary

These strings are a published contract: an agent branches on them, so they are
**append-only**. Renaming one is a breaking change to every MCP client.

| Code | Severity | `ref` | Raised when |
|---|---|---|---|
| `SQL_PARSE_ERROR` | ERROR | — | The statement does not parse for the datasource's dialect |
| `READ_ONLY_QUERY_REQUIRED` | ERROR | — | The statement is not a query |
| `MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN` | ERROR | — | DDL / DML / transaction / admin command anywhere in the tree |
| `SELECT_INTO_FORBIDDEN` | ERROR | — | `SELECT … INTO` |
| `EXACTLY_ONE_STATEMENT_REQUIRED` | ERROR | — | Batch submitted |
| `CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN` | ERROR | — | A join with no `ON` / `USING` |
| `SELECT_WILDCARD_FORBIDDEN` | ERROR | — | `SELECT *` (bare `COUNT(*)` is allowed) |
| `FORBIDDEN_FUNCTION` | ERROR | function name | A function reaching outside the query engine |
| `UNKNOWN_OR_UNAUTHORIZED_TABLE` | ERROR | qualified table | Not an ACTIVE table in this datasource's catalog binding for this org |
| `UNKNOWN_COLUMN` | ERROR | `table.column` or `column` | No ACTIVE column of that name on the referenced table |
| `COST_CEILING_EXCEEDED` | ERROR | — | Cost-plan dry run over `max_query_estimate_cost`; `detail: {plan_cost, limit}` |
| `BYTE_BUDGET_EXCEEDED` | ERROR | — | Byte-shaped dry run over `max_query_estimate_bytes`; `detail: {plan_cost, limit}` |
| `ESTIMATE_UNAVAILABLE_FOR_CONNECTOR` | ERROR | — | Connector does not advertise `capabilities.explain`; fails closed (INV-4) |
| `ROW_LIMIT_APPLIED` | **INFO** | — | Always, for a query: `detail: {applied_row_limit, requested_limit, default_row_limit, hard_row_limit, clamped}` |

`valid` is `false` iff at least one `ERROR` finding is present. `ROW_LIMIT_APPLIED`
is informational and reports both the bound applied and whether it clamped the
request, which is the "row limit applied/clamped" case in one code rather than
two.

**Not implemented, and why.** The target document's example also shows
`MISSING_SOFT_DELETE_FILTER` and `UNVERIFIED_JOIN`. Both need data this system
does not have yet: the first needs the negative-knowledge store (**N16**), the
second needs verified join paths from the relationship graph (`get_join_path`).
Neither is a gateway change; both slot in as additional `findings_from_*`
builders in `sql_validation.py` once their sources exist.

### INV-6 (no source values in findings)

Findings carry object names, machine codes, static hints and numbers only. Two
specific decisions:

* **The sqlglot parse-error message is withheld.** It quotes the offending SQL
  fragment, literal values included — e.g. `Expecting ). … WHERE x =
  'private@example.com'`. Only the `SQL_PARSE_ERROR` code survives.
  *This also fixes a pre-existing leak:* `execute` previously raised
  `QueryRejected(", ".join(validation.violations))` and persisted
  `str(exc)[:1000]` into `query_execution.error_message`, so a parse failure
  wrote a user literal into the control plane. It no longer does.
* Any SQL echoed back (`report.normalized_sql`) is passed through
  `redact_sql_literals` first. The executable form — normalised, literals intact
  — is carried on a private `_ValidationOutcome` and never leaves the gateway.

---

## 3. Behaviour changes to `execute()`

Deliberate, and a direct consequence of "validation and execution must not
drift":

1. **`UNKNOWN_COLUMN` now blocks execution.** It never could before, because
   the check did not exist. The alternative — a finding that validation reports
   but execution ignores — is exactly the drift this item exists to prevent.
   The check is conservative by construction: it is skipped entirely when the
   catalog knows nothing about any referenced table; a name the query defines
   for itself (an explicit `AS` alias, a CTE column) is never checked; a
   qualified column is checked against its own table only when that table is in
   the catalog map; an unqualified column is checked against the union of the
   referenced tables' columns, which is SQL's own resolution rule. By the time
   the check runs, `UNKNOWN_OR_UNAUTHORIZED_TABLE` has already established that
   every referenced table is in the same catalog.
2. **Rejection messages.** `UNKNOWN_OR_UNAUTHORIZED_TABLES: …`,
   `QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR`, `QUERY_COST_EXCEEDS_POLICY: x > y`
   and `QUERY_BYTES_EXCEED_POLICY: x > y` are reproduced verbatim, so the HTTP
   422 body and the persisted `error_message` keep their shape. The one change
   is `SQL_PARSE_ERROR: <parser message>` → `SQL_PARSE_ERROR` (see INV-6 above),
   plus a new `UNKNOWN_COLUMNS: …` message for the new check.
3. The `query.execute` audit detail gains `finding_codes`.

### INV-2

`execute_read_query` still has exactly one call site in the codebase, inside
`QueryExecutionGateway.execute`. The validation path reaches
`estimate_read_query` only. All three enforcement layers stay green:
the import-linter contract, `mypy --strict`, and
`tests/test_tier0_invariants.py::test_no_connector_execution_outside_gateway`
(which scans for the *attribute name*, so `sql_validation.py` and
`sql_validation_api.py` do not so much as mention it).
`tests/test_sql_validation.py::test_validation_never_calls_execute_read_query`
proves it dynamically: the fake connector's `execute_read_query` raises, so any
validation path that reached it would fail the suite rather than touch a source.

---

## 4. Wiring the orchestrator must apply

`api.py` and `main.py` were being edited concurrently and were not touched.
Two lines, both in `src/aida/main.py`:

**Import** — insert in the alphabetically sorted import block, between
`from aida.semantic_intelligence_api import router as semantic_intelligence_router`
and `from aida.stewardship_api import router as stewardship_router`:

```python
from aida.sql_validation_api import router as sql_validation_router
```

**Registration** — add alongside the other `include_router` calls (position is
not significant; after `app.include_router(product_marketplace_router)` and
before the `mcp_router` block reads well):

```python
app.include_router(sql_validation_router)
```

Nothing in `api.py` needs to change. Verify with:

```
ruff check src && mypy src && lint-imports
```

---

## 5. C4 / ST-11 — the rule is now a contract

Appended to `pyproject.toml`:

```toml
name = "C4 / ST-11 lineage and intelligence modules never import the query gateway"
type = "forbidden"
source_modules = [
  "aida.unified_lineage", "aida.unified_lineage_api", "aida.lineage_cache",
  "aida.lineage_graph_store", "aida.openlineage", "aida.openlineage_api",
  "aida.knowledge_graph", "aida.agent_intelligence", "aida.intelligence_api",
  "aida.semantic_inference",
]
forbidden_modules = ["aida.query_gateway"]
```

Verified by `grep` to pass before it was written (every module above has zero
occurrences of `query_gateway`), then confirmed **KEPT** by `lint-imports`. It
constrains no existing edge; it stops the cycle being reintroduced.

**One module is deliberately excluded.** `aida.semantic_intelligence_api`
imports `SENSITIVE_CLASSES` from `aida.query_gateway` today. That is a shared
classification constant, not a call back into execution — a misplaced-constant
problem rather than a cycle — but listing the module would either break the
contract or require an exemption, and `05-ci-cd-and-release.md` forbids
exemptions. **Follow-up (small):** move `SENSITIVE_CLASSES` to a leaf module
(`aida.security_types` or a new `aida.classification`), update the two
importers, and add `aida.semantic_intelligence_api` to `source_modules` above.
Not done here because `api.py`/`schemas.py` ownership sat with other streams
this cycle.

---

## 6. The model change deliberately not made

**Requirement:** no new columns and no new enum values in `models.py`.

`QueryExecutionGateway.validate` therefore records the attempt as an audit event
only — action **`query.validate.gateway`**, resource `datasource`, outcome
`SUCCESS` / `DENIED`, with `details` carrying the HMAC'd `sql_hash`, referenced
tables, referenced column count, `finding_codes`, `applied_row_limit`,
`plan_cost` and `executed: false`. **No `QueryExecution` row is persisted.**

The action name is `query.validate.gateway` rather than plain `query.validate`
because `query.validate` is already taken by the guard-only
`POST /v1/query/validate` route in `api.py`, and conflating a parse check with a
full catalog-and-cost validation in the same audit action would make the trail
unreadable.

**Why no row.** Writing a `QueryExecution` for something that was never executed
would corrupt the execution ledger: `ix_query_execution_org_status` and every
operational metric computed from that table would start counting validations as
executions, and `status` would need a value (`VALIDATED`) that no consumer of
that column expects.

**The change that would be needed, if validations should be first-class
evidence** — recommended, but out of scope here:

* Preferred: a new `query_validation` table — `id`, `organization_id`,
  `datasource_id`, `principal_id`, `dialect`, `sql_hash`, `normalized_sql`
  (redacted), `referenced_tables`, `referenced_columns`, `column_lineage`,
  `findings` (JSON), `valid`, `plan_cost`, `elapsed_ms`, `created_at`, plus an
  `ix_query_validation_org_created` index — with an Alembic migration. This
  keeps the execution ledger clean and makes "how many times did an agent have
  to iterate before it produced valid SQL" a query rather than a log grep,
  which is the metric that tells you whether the context products are working.
* Cheaper alternative: a `kind` discriminator column on `query_execution`
  (`EXECUTION` | `VALIDATION`, defaulting to `EXECUTION`) plus a `VALIDATED`
  status value, and a filter on every existing consumer of the table. Cheaper to
  migrate, but it makes every existing query on `query_execution` wrong until
  each is updated — which is the kind of change that gets half-done.

Neither was made. The audit event is sufficient for "who asked the platform to
check what SQL, and what did it say", which is the compliance question; the
missing capability is the *analytics* one.

---

## 7. Verification

```
### ruff check src tests/test_sql_validation.py
All checks passed!

### mypy src
Success: no issues found in 112 source files

### lint-imports
identity_tenancy module privacy KEPT
INV-2 connector SQL execution is reachable only from the query gateway KEPT
security_types never depends on api (leaf-module ratchet) KEPT
C4 / ST-11 lineage and intelligence modules never import the query gateway KEPT

Contracts: 4 kept, 0 broken.

### pytest tests/test_sql_validation.py tests/test_sql_guard.py \
###        tests/test_query_masking.py tests/test_mcp_server.py \
###        tests/test_tier0_invariants.py
81 passed, 30 warnings, 2 errors in 2.56s

### pytest tests/ --ignore=tests/test_inv5_tenant_isolation.py
422 passed, 30 warnings, 18 errors in 5.90s
```

**The errors are not from this work and are not in files this stream owns.**
All 18 (plus the ignored collection error) are `ModuleNotFoundError: No module
named 'aiosqlite'` — `tests/test_tier0_invariants.py::test_cross_tenant_denial`,
`::test_authorization_defaults_to_deny_without_membership` and all of
`tests/test_workspace_authorization.py`, which are new DB-backed tests added
concurrently by another stream against a package that is not installed in the
Linux virtualenv and is not in `pyproject.toml`'s `dev` extra. Separately,
`tests/test_inv5_tenant_isolation.py` fails to import
`tests.support.app_surface` (`No module named 'tests'` — the tests directory has
no `__init__.py` and no `conftest.py` adding the root to `sys.path`), and
`ruff check .` reports one `F401` for an unused `importlib` in
`tests/support/app_surface.py`. Whoever owns those files needs to add
`aiosqlite` to the `dev` extra, make `tests` importable, and drop that import.
`ruff check src tests/test_sql_validation.py` is clean.

Zero test failures. 16 new tests in `tests/test_sql_validation.py`: applied and
clamped row limits, write statement refused before any source contact,
unparseable SQL with the parser message withheld, unauthorised table, unknown
column, projection aliases and CTE names not misreported, cost ceiling, byte
budget, connector without EXPLAIN failing closed, no literal values anywhere in
the report or the audit detail, the distinct audit action with no
`QueryExecution` row, `execute_read_query` never reached, and the MCP tool's
schema, role gate and value-free JSON payload.
