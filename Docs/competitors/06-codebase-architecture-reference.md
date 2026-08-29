# Codebase Architecture & Internals Reference

> **Status**: Authoritative living reference for engineers working on Atlas / Bank Data Intelligence Platform.  
> **Source tree**: `src/aida/` — FastAPI + SQLAlchemy + asyncpg Python backend; `ui/` — single-file vanilla JS/CSS portal.  
> **All module sizes and line counts as of 2026-08-28.**

---

## 1. System Entry Points

| Entry | File | Role |
|---|---|---|
| HTTP API server | [`main.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/main.py) | FastAPI app factory, lifespan (Temporal connect), Prometheus metrics middleware, health routes, router mounting |
| Settings | [`config.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/config.py) | Pydantic-settings `Settings` class loaded from env / `.env` with `AIDA_` prefix. All feature flags, limits, and secret refs live here. Production validator rejects insecure configs at startup. |
| Database session | [`db.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/db.py) | SQLAlchemy async session factory, `get_session()` FastAPI dependency |
| Security | [`security.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/security.py) + [`oidc.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/oidc.py) | `SecurityContext` dataclass, `require_roles` / `enforce_organization` guards, OIDC JWKS verifier |
| Secrets | [`secrets.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/secrets.py) | `SecretResolver` dispatches `env://`, `vault://`, `aws-sm://`, `azure-kv://` credential references |

---

## 2. Module Map

### 2.1 Core Execution Pipeline

```
User / Agent Question
        │
        ▼
[agent_orchestrator.py] GovernedAgentOrchestrator.run()
  ├── DeterministicPromptRiskClassifier [prompt_risk.py]  ← 7 regex signals, block threshold 0.8
  ├── GovernedRetriever [agent_intelligence.py]           ← lexical keyword scan (tables, columns,
  │       tools, dbt resources, business annotations)         metrics, glossary)
  ├── GovernedPlanner [agent_intelligence.py]             ← strategy: GOVERNED_TOOL > DEV_SQL > MODEL
  │
  ├── [Strategy: GOVERNED_TOOL]
  │   └── render_tool_sql [tool_rendering.py]             ← parameterized SQL template rendering
  │
  ├── [Strategy: MODEL_GATEWAY]
  │   └── ProviderNeutralModelGateway [model_gateway.py]  ← OpenAI Responses / Gemini generateContent
  │       ├── credential from SecretResolver
  │       ├── token budget enforcement
  │       └── structured output (SqlGenerationOutput pydantic model)
  │
  └── QueryExecutionGateway [query_gateway.py]
      ├── SqlGuard.validate() [sql_guard.py]              ← AST parse, read-only, no wildcards, LIMIT inject
      ├── allowed_tables()                                ← deny unlisted/unscanned tables
      ├── connector.estimate_read_query()                 ← plan cost check before execution
      ├── connector.execute_read_query()                  ← deterministic connector execution
      ├── _sensitive_output_names()                       ← PII/CONFIDENTIAL column lookup
      └── row-level ***MASKED*** substitution             ← output masking, never raw values
```

### 2.2 API Routers (mounted in `main.py`)

| Router File | Prefix / Tags | Key Surfaces |
|---|---|---|
| [`api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/api.py) | `/v1` | Data sources, catalogs, schemas, tables, columns, analysis runs, agent runs, query executions |
| [`semantic_api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/semantic_api.py) | `/v1` `semantic-governance` | Semantic model versions, metrics, maker-checker governance reviews, enrichment proposal application |
| [`semantic_intelligence_api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/semantic_intelligence_api.py) | `/v1` | Business inference, metadata annotation proposals |
| [`tool_api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/tool_api.py) | `/v1` | Governed tools, tool versions, publication workflow |
| [`intelligence_api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/intelligence_api.py) | `/v1` | AI analyst endpoint, retrieval hits, analysis runs |
| [`ingestion_api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/ingestion_api.py) | `/v1` | Push/pull ingestion, batch envelope delivery, Temporal workflow triggering |
| [`quality_api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/quality_api.py) | `/v1` | Quality rules, profiles, baseline comparison, incident lifecycle |
| [`dbt_api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/dbt_api.py) | `/v1` | dbt project registration, manifest.json import, dbt resource browser |
| [`glossary_api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/glossary_api.py) | `/v1` | Business glossary terms, ownership, lifecycle |
| [`ai_governance_api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/ai_governance_api.py) | `/v1` | Model route registration, approval, budget caps |
| [`operational_api.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/operational_api.py) | `/v1` | Fleet schedules, source fleet matrix, operational evidence |

### 2.3 Connectors (`src/aida/connectors/`)

| File | Type | Status | Key Capabilities |
|---|---|---|---|
| [`base.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/connectors/base.py) | Abstract base | — | `Connector` ABC: `discover()`, `estimate_read_query()`, `execute_read_query()`, `profile_table()` |
| [`postgres.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/connectors/postgres.py) | PostgreSQL | BETA | `asyncpg`, EXPLAIN cost, constraint discovery, column profiling |
| [`sqlserver.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/connectors/sqlserver.py) | SQL Server | BETA | SHOWPLAN_XML cost, column profiling |
| [`oracle.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/connectors/oracle.py) | Oracle | BETA | Pull discovery, governed read; no EXPLAIN path yet |
| [`bigquery.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/connectors/bigquery.py) | BigQuery | BETA | INFORMATION_SCHEMA discovery, dry-run cost estimate, governed execution |
| [`snowflake.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/connectors/snowflake.py) | Snowflake | BETA | Multi-database INFORMATION_SCHEMA discovery, EXPLAIN USING JSON cost estimate, governed execution (`sfqid`), bounded profiling |
| [`registry.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/connectors/registry.py) | Registry | — | `ConnectorRegistry`, `register()` for postgres/sqlserver/oracle/bigquery/snowflake; `declare_planned()` for Databricks / Teradata / Db2 |
| [`discovery.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/connectors/discovery.py) | Shared helpers | — | DDL row assembly, constraint parsing shared across connectors |

**Implemented (BETA, unverified against a live source outside test fixtures)**: `oracle`, `bigquery`, `snowflake`. **Planned but not yet implemented**: `databricks`, `teradata`, `db2`.

### 2.4 Key Service Modules

| Module | Responsibility |
|---|---|
| [`sql_guard.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/sql_guard.py) | AST validation: read-only, no wildcards, no forbidden functions, no cross/unbounded joins, LIMIT enforcement |
| [`prompt_risk.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/prompt_risk.py) | 7 regex risk signals: instruction override, credential extraction, masking bypass, privilege escalation, unbounded data extraction |
| [`model_gateway.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/model_gateway.py) | `ProviderNeutralModelGateway`: route approval check, token budget, `OpenAIResponsesProvider`, `GeminiGenerateContentProvider`, retry with backoff |
| [`semantic_inference.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/semantic_inference.py) | Metadata-only inference of business domains, entities, descriptions, safe tool blueprints |
| [`dbt_artifacts.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/dbt_artifacts.py) | manifest.json parser: extracts nodes, sources, exposures, column descriptions, compiled SQL hashes |
| [`quality_service.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/quality_service.py) | Volume/null-rate baseline profiling, schema fingerprint change detection, incident lifecycle |
| [`knowledge_graph.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/knowledge_graph.py) | Neo4j client wrapper: 1-to-4 hop bounded neighborhood queries over metadata node-edge model |
| [`events.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/events.py) | `record_audit()`, `record_outbox()` — immutable audit ledger + Kafka outbox entries |
| [`agent_runtime.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/agent_runtime.py) | `RuntimeStage` enum + `RuntimeState` finite-state machine with explicit stage transitions |
| [`tool_rendering.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/tool_rendering.py) | Jinja/template-based SQL rendering from `GovernedToolVersion.sql_template` with typed parameter substitution |

---

## 3. Data Model Highlights (`models.py`, 67KB)

Key SQLAlchemy ORM models (all soft-deleted via `status` column):

| Model | Purpose |
|---|---|
| `Organization` | Top-level tenant |
| `Project` | Groups datasources, semantic models, dbt projects |
| `DataSource` | Connection descriptor: `connector_type`, `dialect`, `credential_reference` |
| `MetadataCatalog / MetadataSchema / MetadataTable / MetadataColumn` | Hierarchical technical catalog, `classification` for PII/PHI |
| `AnalysisRun` | A completed metadata scan/analysis run (required before AI analyst can run) |
| `GovernedTool / GovernedToolVersion` | Maker-checker governed SQL tool with `parameter_schema` and `sql_template` |
| `AgentRun` | Full trace of one AI analyst request: `prompt_risk`, `plan_evidence`, `generation_source`, `step_trace` |
| `QueryExecution` | Immutable record of a gateway-executed SQL query: HMAC hash, lineage, masked columns, cost |
| `SemanticModelVersion / SemanticMetricVersion` | Business metric definitions with grain, aggregation, source table binding |
| `ModelRouteConfiguration` | Approved LLM route: provider, model ID, capabilities, budget, credential reference |
| `MetadataEnrichmentProposal` | AI-generated pending business domain / entity proposal awaiting checker approval |
| `GlossaryTermVersion` | Business glossary term lifecycle |
| `DbtProject / DbtArtifactImport / DbtResource` | dbt manifest.json import registry |

---

## 4. Retrieval Strategy (Current)

The `GovernedRetriever` in [`agent_intelligence.py`](file:///c:/Users/karup/AGProjects/AIDataAnalyst/src/aida/agent_intelligence.py) performs **lexical keyword scan** only:

1. Tokenizes the user question → stop-word filtered terms.
2. SQL `LIKE` queries across `MetadataTable.name`, `MetadataColumn.name`.
3. Scans `GovernedToolVersion` (published tools) for name/description match.
4. Scans `DbtResource` descriptions.
5. Scans `MetadataBusinessAnnotation` (approved annotations, synonyms, questions).
6. Scores hits by term overlap ratio, boosts governed tool matches.
7. Planner chooses `GOVERNED_TOOL` if score > `agent_tool_match_threshold` (default 0.55).

**Gap**: No vector embeddings, no BM25 ranking, no hybrid retrieval. This is the primary retrieval quality gap vs. Atlan's Context Lakehouse.

---

## 5. Gaps vs. Market Leaders (Ranked by Priority)

| # | Gap | Impact | Files to Create / Modify |
|---|---|---|---|
| 1 | **MCP Server implemented but not verified with an external client** | `src/aida/mcp_server.py` (652 lines, tested, mounted in `main.py`) implements the JSON-RPC MCP protocol with role-eligible tool/resource exposure, but has never been driven by a real external agent client | `src/aida/mcp_server.py`, `main.py` |
| 2 | **Lexical retrieval only** | LLM plans miss semantically relevant tables when names are cryptic | `src/aida/retrieval.py` (new hybrid engine), `agent_intelligence.py` |
| 3 | **No Databricks / Teradata / Db2 pull adapters** | Connector breadth gap vs. Atlan 80+, Collibra 100+ (BigQuery and Snowflake adapters now exist but are unverified against a live warehouse) | `src/aida/connectors/registry.py` (`declare_planned` entries) |
| 4 | **UI lineage DAG is basic** | Atlan field-level lineage is top sales feature | `ui/app.js` |
| 5 | **No data contracts engine** | Banks need schema-drift and SLA assertions | `src/aida/data_contracts.py` |

---

## 6. Improvement Build Plan

### Phase 1 — MCP Server (This Sprint)
**File**: `src/aida/mcp_server.py`  
**Why first**: Most differentiation from doing this quickly. Atlan already ships MCP; shipping ours means Claude/Cursor can query our governed catalog without bypassing our gateway.

**Design**:
- JSON-RPC 2.0 over HTTP POST `/mcp` (standard MCP transport).
- Methods: `tools/list`, `tools/call`, `resources/list`, `resources/read`.
- `tools/list` → queries DB for published `GovernedToolVersion` records.
- `tools/call` → routes through `GovernedAgentOrchestrator` — our full gateway stack applies (prompt risk, AST guard, masking, audit).
- `resources/list` → exposes metadata catalog assets as MCP resources.
- All calls require `Authorization: Bearer <OIDC token>` — same as REST API.

### Phase 2 — Hybrid Retrieval Engine
**File**: `src/aida/retrieval.py`  
Adds `pgvector` embeddings + BM25 scoring fused with current lexical hits.

### Phase 3 — BigQuery Pull Connector
**File**: `src/aida/connectors/bigquery.py`  
Follows `Connector` ABC exactly. Uses `google-cloud-bigquery` with service account JSON credential reference.
