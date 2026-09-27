# Lineage Contract

> Status: Authoritative, T1 external contract. Owner: Data Intelligence.
> Covers lineage Atlas ingests (OpenLineage, dbt) and lineage Atlas exposes — including AI decision lineage, which has no market equivalent.

## 1. Lineage node and edge model

```json
{
  "node": {
    "id": "lin_...",
    "kind": "TABLE | COLUMN | METRIC | TOOL | REPORT | AGENT_RUN | SEMANTIC_VERSION | JOB",
    "ref": {"type": "table", "id": "tbl_..."},
    "qualified_name": "bank.customer.account",
    "organization_id": "org_..."
  },
  "edge": {
    "id": "edge_...",
    "kind": "QUERY | VIEW | PROCEDURE | ETL | DBT | BI | AI_DECISION",
    // Implementation status (ST-15, 2026-09-01): ENFORCED. This exact vocabulary is now the
    // agreed set and is enforced at the database level by a CHECK constraint on every edge_kind
    // column of the four lineage-edge tables (openlineage_table_edge, openlineage_column_edge,
    // bi_report_metric_edge, bi_metric_column_edge) -- migration d7b1e5a9c204, mirrored in
    // aida.models.LINEAGE_EDGE_KINDS. The OpenLineage edges default to "ETL" and the BI edges to
    // "BI" (both in the vocabulary); QUERY/VIEW/PROCEDURE/DBT/AI_DECISION are reserved for edge
    // types not yet written to these tables. NOTE: "SUGGESTED_RELATIONSHIP" is NOT part of this
    // vocabulary and never lands in these columns -- it belongs to the separate
    // relationship/grant edge-kind axis (DECLARED_FOREIGN_KEY / SUGGESTED_RELATIONSHIP /
    // APPROVED_RELATIONSHIP_CANDIDATE) carried by CrossBoundaryGrant.edge_kinds and the unified
    // relationship-graph payloads, which is a different vocabulary for a different purpose. The
    // earlier note conflated the two. See 20-modules/09-lineage.md section 5.
    "from_node": "lin_...",
    "to_node": "lin_...",
    "confidence": 1.0,
    "transformation": "DIRECT | DERIVED | AGGREGATED | FILTERED",
    "evidence_ref": "ev_...",
    "observed_at": "2026-08-28T10:00:00Z"
  }
}
```

| Field | Notes |
|---|---|
| `confidence` | 1.0 for parsed/declared lineage; lower for inferred |
| `transformation` | How the value was produced, not what it is |
| `evidence_ref` | Points to the artifact or execution that produced this edge |
| **No values anywhere** | Column identity only (INV-6) |

## 2. Ingested lineage

### 2.1 OpenLineage

Atlas accepts OpenLineage `RunEvent` payloads.

| Element | Mapping |
|---|---|
| `job` | `JOB` node |
| `inputs[]` / `outputs[]` | `TABLE` nodes, resolved against the catalog |
| `columnLineage` facet | `COLUMN` edges with `kind: ETL` |
| `run` | Execution evidence |
| Unresolvable datasets | Recorded as unresolved with the raw name; **surfaced, never silently dropped** |

Producer identity is required. Event size is bounded. Facets containing values are rejected.

### 2.2 dbt manifests

| Element | Mapping |
|---|---|
| `nodes[]` (models) | `TABLE` nodes |
| `sources[]` | `TABLE` nodes |
| `depends_on` | `DBT` edges |
| `tests[]` | Quality evidence references |
| `compiled_code` | Stored with **literals redacted**, plus a SQL hash |

**Constraints.** The raw artifact is not persisted. Artifact SQL is **never executed** by Atlas. dbt remains the transformation compiler and executor; Atlas ingests its output as evidence only.

### 2.3 Query lineage

Emitted by the query gateway on every execution: referenced tables and columns from the parsed AST, and output-to-source column mappings classified `DIRECT` or `DERIVED`. Value-free by construction — the mapping is between column identities.

## 3. AI decision lineage — the differentiator

Conventional lineage answers *where did this data come from*. AI decision lineage answers *why did the agent do this, and what did it decline to do*.

```json
{
  "run_id": "run_...",
  "decisions": [
    {
      "step": "SCREENED",
      "outcome": "PASSED",
      "classifier_version": "prc-4",
      "score": 0.02
    },
    {
      "step": "PLANNED",
      "outcome": "SELECTED",
      "selected": [{"ref": "tbl_123", "rank": 1, "reasons": ["domain_match", "canonical", "certified"]}],
      "rejected": [{"ref": "tbl_987", "reason": "quality_incident_open"}]
    },
    {
      "step": "BOUND",
      "outcome": "TOOL_SELECTED",
      "tool_id": "tool_...", "tool_version": 3,
      "alternative_considered": "GENERATION"
    },
    {
      "step": "VALIDATED",
      "outcome": "PASSED",
      "policy_version": "12",
      "semantic_version": "44"
    },
    {
      "step": "EXECUTED",
      "outcome": "COMPLETED",
      "masked_columns": 2
    }
  ]
}
```

Each decision becomes an `AI_DECISION` edge, making the agent's reasoning **traversable in the same graph as data lineage** (the traversal half is target; see the note below).

> **Implementation status (2026-09-20).** The recording half is built; the same-graph half is not. `record_decision` and `record_decisions` in `src/aida/ai_decision_lineage.py` (the function module 09's interface calls `record_ai_decision` does not exist under that name) persist one row per decision in the `ai_decision_record` table, and `src/aida/agent_orchestrator.py` calls them for retrieval selections and rejections, tool selections and rejections, and refusals. The recorded shape is not the `step`/`outcome` JSON above: each row has a `decision_type` (`RETRIEVAL_SELECTED`, `RETRIEVAL_REJECTED`, `TOOL_SELECTED`, `TOOL_REJECTED` or `REFUSAL`), a `source_node` and `target_node`, a `reason`, an `evidence` object and a `control_version`. They are read at `GET /v1/ai-decisions/{run_id}`, `GET /v1/ai-decisions/asset/{asset_id}` and `GET /v1/ai-decisions/refusals`, and appear as `AI_DECISION` items in a table's evidence (`GET /v1/metadata/tables/{table_id}/evidence`). They are not edges in the graph a lineage read returns (`GET /v1/datasources/{datasource_id}/unified-lineage/graph`), so a traversal still cannot follow an agent's reasoning alongside data lineage.

**Why competitors cannot record this.** The `rejected` array requires a runtime in which refusal is a first-class, deterministic event — which requires the execution choke point (ADR-0004) and prompt-risk screening (ADR-0013). A product whose agent simply runs has nothing to record, because it refuses nothing.

## 4. Exposed lineage API

```http
GET /v1/datasources/{datasource_id}/unified-lineage/graph?node_limit=300&edge_limit=1500
GET /v1/datasources/{datasource_id}/unified-lineage/impact/{node_id}?depth=5
GET /v1/data-domains/{domain_id}/unified-lineage/graph
GET /v1/metadata/tables/{table_id}/impact
GET /v1/query-executions/{execution_id}/lineage
GET /v1/ai-decisions/{run_id}?organization_id=<uuid>
```

`GET /v1/lineage/upstream`, `GET /v1/lineage/downstream`, `GET /v1/tables/{id}/impact` and `GET /v1/agent-runs/{id}/decision-lineage` were the original design names and do not exist; `Docs/90-reference/openapi-baseline.json` is the route inventory.

| Rule | Detail |
|---|---|
| Depth bounded | The impact traversal takes `depth` 1–8, default 5, server-enforced. The graph read has no depth; it is bounded by `node_limit` and `edge_limit` |
| Node/edge caps | Per response, with **explicit truncation reasons** |
| Policy filtering | Applied before traversal — an unauthorized node is not a hidden node, it is an absent one |
| Kind filtering | Target: consumers select which edge kinds they want. Today the graph read filters by `suggestion_status` and `include_pending_edges`, not by edge kind |
| Values | Never |

## 5. Impact analysis

```json
{
  "node": {"type": "table", "id": "tbl_123"},
  "affected": {
    "metrics": [{"id": "met_1", "version": 4}],
    "tools": [{"id": "tool_1", "version": 3, "status": "PUBLISHED"}],
    "semantic_annotations": [{"id": "ann_1"}],
    "dbt_models": [{"unique_id": "model.bank.fct_exposure"}],
    "reports": [],
    "quality_policies": [{"id": "qp_1"}]
  },
  "truncated": false
}
```

Impact is what a steward checks before approving a change and what a reviewer sees as blast radius. Coverage across all edge kinds is the target; today it covers metrics, tools, and relationships.

## 6. Value-freedom summary

| Artifact | Treatment |
|---|---|
| Executed SQL | Literals redacted; hash retained |
| dbt compiled SQL | Literals redacted; hash retained; raw artifact not persisted |
| View / procedure definitions | Literals redacted |
| OpenLineage facets | Value-bearing facets rejected |
| Output-to-source mappings | Column identity only |
| AI decision records | Reason codes and refs only |

## Related documents

- Lineage module: `20-modules/09-lineage.md`
- Agent runtime: `20-modules/13-agent-runtime.md`
