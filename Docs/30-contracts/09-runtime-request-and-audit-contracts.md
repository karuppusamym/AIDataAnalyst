# Runtime Request and Audit Contracts

> Status: Authoritative, T1. Owner: Architecture.
> The analyst request/response shape, the audit event shape, governance review types, and approval thresholds. Migrated from the retired flat `05-security-governance-api.md`.

## 1. Analyst request

```json
{
  "project_id": "finance-ai",
  "question": "Show monthly revenue by state for the last six months",
  "mode": "analysis",
  "purpose": "management_reporting",
  "max_rows": 5000,
  "include_sql": true
}
```

| Field | Notes |
|---|---|
| `purpose` | Required for purpose-bound operations; feeds ABAC |
| `max_rows` | Bounded by the gateway's hard row limit (`hard_query_row_limit`); the request cannot raise it, and an absent value takes `default_query_row_limit` (see `ROW_LIMIT_APPLIED` in §7) |
| `include_sql` | Returns the executed SQL **with literals redacted** |
| `mode` | `analysis` \| `preview` (plan only, no execution) |

## 2. Analyst response

```json
{
  "request_id": "qry_123",
  "status": "completed",
  "interpretation": {
    "metric": "total_revenue",
    "dimensions": ["state"],
    "time_range": "last_6_months"
  },
  "sql": "SELECT ...",
  "result": {"columns": ["month", "state", "revenue"], "row_count": 212},
  "lineage": {"tables": ["transaction_fact", "customer_dim"], "metrics": ["total_revenue"]},
  "versions": {"semantic": "1.8.0", "policy": "2.4.1", "prompt_risk_classifier": "prc-4"},
  "trust": {"confidence": 0.96, "quality_warnings": [], "freshness": "NOT_CONFIGURED"},
  "execution": {"tool_id": "tool_revenue_by_state", "tool_version": 3, "masked_columns": 0}
}
```

**`interpretation` is the most important field.** It states what Atlas understood the question to mean, in the user's business vocabulary, *before* they read the numbers. A user who disagrees with the interpretation can stop there rather than acting on a correct answer to the wrong question.

### Refusal response

```json
{
  "request_id": "qry_124",
  "status": "denied",
  "denial": {
    "control": "PROMPT_RISK_SCREEN",
    "classifier_version": "prc-4",
    "reason_codes": ["INSTRUCTION_OVERRIDE", "UNBOUNDED_EXTRACTION"],
    "remediation": "Rephrase the question as a specific analytical request."
  }
}
```

Refusals name the control and give a remediation path. They do **not** detail which rule matched — that would hand an attacker the control map (`30-contracts/01-contract-strategy.md` §6).

> **Implementation status (2026-09-20).** The request and response above are design shapes, not the wire contract. The REST route is `POST /v1/datasources/{datasource_id}/agent-analyses`. Its request (`AgentAnalysisRequest` in `src/aida/schemas.py`) carries `question`, an optional `candidate_sql`, `preferred_tool_version_id`, `tool_parameters`, `max_rows` and `context_product_key`; there is no `project_id`, `mode`, `purpose` or `include_sql` field (the datasource is in the path). Its response (`AgentAnalysisResponse`) carries `agent_run_id`, `status`, `generation_source`, `semantic_version`, `policy_version`, `step_trace`, `retrieval_evidence`, `plan_evidence`, `execution` and `explanation`, with no `interpretation`, `trust`, `lineage` or `versions` object. A refusal on this route is an HTTP error, `422` with the reason as a string `detail` (or a structured `409` when a governed tool needs parameters), not a `denial` object.

## 3. Audit event

Target shape:

```json
{
  "event_type": "QUERY_EXECUTED",
  "user_id": "usr_123",
  "agent_id": "finance_analyst_v3",
  "tool_id": null,
  "semantic_version": "1.8.0",
  "policy_version": "2.4.1",
  "organization_id": "org_...",
  "lob_id": "lob_...",
  "tables": ["transaction_fact", "customer_dim"],
  "columns": ["amount", "transaction_date", "state"],
  "warehouse_query_id": "snowflake-01b2...",
  "correlation_id": "cor_...",
  "timestamp": "2026-08-28T23:00:00-04:00"
}
```

> **Implementation status (2026-09-20).** The flat `QUERY_EXECUTED` record above is not what is stored. An audit event is a row of the `audit_event` table (`src/atlas/modules/observability_audit/models.py`) with `organization_id` (nullable), `principal_id`, `principal_type`, `action`, `resource_type`, `resource_id`, `outcome`, `correlation_id`, `source_ip`, `details` (a JSON object) and `occurred_at`. A governed execution writes two, `query.execute.requested` and then `query.execute`, the last with `outcome` `SUCCESS` or `DENIED`, `resource_type` `query_execution` and the execution id as `resource_id` (a validation call writes `query.validate.gateway` against the datasource instead). The `query.execute` `details` carry the referenced `tables`, counts (`referenced_column_count`, `lineage_output_count`, `row_count`), the names of the masked and tokenized output columns, `plan_cost`, `estimate_kind`, the estimated rows and bytes, and `finding_codes`. The fields the design puts on the event live on the `query_execution` row that `resource_id` points at: `principal_id`, `sql_hash`, `policy_version`, `semantic_version`, `warehouse_query_id` and the referenced tables, columns and column lineage. `agent_id`, `tool_id` and `lob_id` are not columns of the audit event.

| Property | Guarantee |
|---|---|
| Written in the mutation's transaction | INV-7 — a crash cannot lose it |
| Column identity only, never values | INV-6 |
| Versions pinned | The decision can be re-derived |
| `warehouse_query_id` | Correlates to the source's own logs — a real backend identifier, never a synthetic UUID |
| Append-only | Never updated, never deleted |

The `warehouse_query_id` convention matters for forensics: `postgres-backend:<pid>`, `sqlserver-spid:<spid>`, `oracle-sid:<sid>`. BigQuery, Snowflake and Databricks store the bare job or query id the source returned, with no prefix. An auditor can take this identifier to the DBA team and find the same query in the source's own records.

## 4. Governance review types

Every one flows through the **single** review queue (module 17), never a per-feature approval.

| Review type | Object |
|---|---|
| Inferred relationship | Relationship candidate |
| Semantic mapping | Table or column annotation |
| Canonical table change | Canonical mapping |
| Metric | Metric version |
| Tool | Tool version |
| Model route | Route version |
| Glossary term | Term version |
| Conflict resolution | Term conflict |
| Quality policy | Policy or watermark contract |
| Context product | Product version |
| External integration | Integration registration |
| High-risk policy | Policy version |

## 5. Approval thresholds

Confidence determines the *path*, never the *authority*.

Target banding:

```text
confidence ≥ 0.95        automatic publish IF the object type is low-risk
0.80 ≤ confidence < 0.95  publish with a review flag
confidence < 0.80         human approval required
```

> **Implementation status (2026-09-20).** This banding exists nowhere in `src/`: no code publishes at 0.95 or flags at 0.80. Every governance review item waits for a person, with two narrow exceptions. The reviewer agent may decide an item, but it is off by default (`reviewer_agent_enabled`), refused in production, and limited to the low tiers of a fixed object-type classification (T0 to T3 in `src/aida/review_risk_tiers.py`, capped at T1 whatever the configuration says). And when `lineage_parsed_edges_review_mode` is `require_review`, a parsed lineage edge at or above `lineage_high_confidence_auto_active_threshold` (default 0.9) lands active without review. The constraints below still hold.

**Three constraints on this table.**

1. Thresholds vary by object type and business criticality — they are configuration, not constants.
2. **Model-only inference never exceeds 0.70** (`90-reference/04-analysis-algorithms.md` §4), so it can never reach an auto-publish threshold. That ceiling is what makes automatic publication safe at all.
3. High-risk object types — tools, model routes, policies — have **no** auto-publish path at any confidence. Maker-checker is unconditional there (INV-8).

## 6. Query execution request (internal)

The design of what module 16 enforces. Target: every field is required, and there is no partial-context path.

```text
identity_context   purpose          datasource_id
workload_class     policy_version   timeout
max_rows           max_bytes        correlation_id
sql_or_tool_binding
```

Target: missing any field is a rejection, not a default (`20-modules/16-query-gateway.md` §5).

> **Implementation status (2026-09-20).** The gateway takes a different request, and a missing row limit is defaulted rather than rejected. `workload_class` appears nowhere in `src/`, and `QueryExecutionGateway.execute` takes no `policy_version`, `timeout` or `max_bytes`: the row limit, timeout and byte or cost ceilings come from settings. The real argument list is in `20-modules/16-query-gateway.md` §5.

## 7. SQL validation finding codes

Returned by the gateway's validation path — `POST /v1/datasources/{id}/sql-validations` and the
`validate_sql` MCP tool. **This list is append-only.** Renaming a code is a breaking change to
every MCP client and to every agent that has learned to react to one, so a code is added, never
repurposed, and never removed.

Moved here 2026-08-30 from `review-2026-08/gap/05-validate-sql-handoff.md`, where a published
contract had no business living.

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
| `TABLE_VALUED_SOURCE_FORBIDDEN` | ERROR | — | A table-valued function, linked-server call or remote rowset used as a source (`OPENQUERY`, `OPENROWSET`, `TABLE(...)`): it cannot be resolved against the catalog |
| `LOCKING_READ_FORBIDDEN` | ERROR | — | `FOR UPDATE` / `FOR SHARE`, or a T-SQL locking table hint: the gateway only runs stateless, non-locking reads |
| `UNKNOWN_OR_UNAUTHORIZED_TABLE` | ERROR | qualified table | Not an ACTIVE table in this datasource's catalog binding for this org |
| `UNKNOWN_COLUMN` | ERROR | `table.column` or `column` | No ACTIVE column of that name on the referenced table |
| `CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE` | ERROR | qualified table | The request was made through a context product and the table resolves but the product does not name it (a product narrows the datasource allowlist, never widens it); `detail` carries the product version |
| `CONTEXT_PRODUCT_TABLE_UNRESOLVED` | ERROR | qualified table | The request was made through a context product and the reference does not resolve to exactly one active table, so it cannot be shown to be inside the product |
| `COST_CEILING_EXCEEDED` | ERROR | — | Cost-plan dry run over `max_query_estimate_cost`; `detail: {plan_cost, limit}` |
| `BYTE_BUDGET_EXCEEDED` | ERROR | — | Byte-shaped dry run over `max_query_estimate_bytes`; `detail: {plan_cost, limit}` |
| `ESTIMATE_UNAVAILABLE_FOR_CONNECTOR` | ERROR | — | Connector does not advertise `capabilities.explain`; fails closed (INV-4) |
| `ROW_LIMIT_APPLIED` | **INFO** | — | Always, for a query: `detail: {applied_row_limit, requested_limit, default_row_limit, hard_row_limit, clamped}` |

An invalid statement is a **200 with `valid: false`**, not a 4xx. The findings *are* the answer the
caller asked for, and turning them into an error status breaks the iterate-against-the-compiler
loop that makes generated SQL converge. 4xx is reserved for the request itself being unusable —
unknown datasource, cross-organization access, a disabled datasource — and **403 with a reason
code** for an authorization refusal, which no amount of correcting the SQL will fix.

Two rules constrain what a finding may say:

* **No source values (INV-6).** `ref` carries identifiers — a table name, a column name, a function
  name — never a literal from the statement. The sqlglot parser message is deliberately withheld
  from `SQL_PARSE_ERROR` because it quotes the literal it choked on.
* **Fail closed (INV-4).** `ESTIMATE_UNAVAILABLE_FOR_CONNECTOR` is an ERROR, not a warning. A
  connector that cannot cost a statement does not get to run it.

Validation and execution share one pipeline (`_run_validation`), so a statement validation reports
as valid is a statement execution will accept. Two entry points that could disagree would make the
loop above worse than useless.

## Related documents

- API conventions: `30-contracts/02-api-conventions.md`
- Contract strategy: `30-contracts/01-contract-strategy.md`
- Agent runtime: `20-modules/13-agent-runtime.md`
- Policy and governance: `20-modules/17-policy-and-governance.md`
