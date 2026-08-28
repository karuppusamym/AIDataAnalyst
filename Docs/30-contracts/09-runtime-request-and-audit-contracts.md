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
| `max_rows` | Bounded by the workload-class cap; the request cannot raise it |
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

## 3. Audit event

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

| Property | Guarantee |
|---|---|
| Written in the mutation's transaction | INV-7 — a crash cannot lose it |
| Column identity only, never values | INV-6 |
| Versions pinned | The decision can be re-derived |
| `warehouse_query_id` | Correlates to the source's own logs — a real backend identifier, never a synthetic UUID |
| Append-only | Never updated, never deleted |

The `warehouse_query_id` convention matters for forensics: PostgreSQL backend pid, `sqlserver-spid:<spid>`, `oracle-sid:<sid>`. An auditor can take this identifier to the DBA team and find the same query in the source's own records.

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

```text
confidence ≥ 0.95        automatic publish IF the object type is low-risk
0.80 ≤ confidence < 0.95  publish with a review flag
confidence < 0.80         human approval required
```

**Three constraints on this table.**

1. Thresholds vary by object type and business criticality — they are configuration, not constants.
2. **Model-only inference never exceeds 0.70** (`90-reference/04-analysis-algorithms.md` §4), so it can never reach an auto-publish threshold. That ceiling is what makes automatic publication safe at all.
3. High-risk object types — tools, model routes, policies — have **no** auto-publish path at any confidence. Maker-checker is unconditional there (INV-8).

## 6. Query execution request (internal)

The contract module 16 enforces. Every field is required; there is no partial-context path.

```text
identity_context   purpose          datasource_id
workload_class     policy_version   timeout
max_rows           max_bytes        correlation_id
sql_or_tool_binding
```

Missing any field is a rejection, not a default (`20-modules/16-query-gateway.md` §5).

## Related documents

- API conventions: `30-contracts/02-api-conventions.md`
- Contract strategy: `30-contracts/01-contract-strategy.md`
- Agent runtime: `20-modules/13-agent-runtime.md`
- Policy and governance: `20-modules/17-policy-and-governance.md`
