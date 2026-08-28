# 01 — End-to-End Architecture

## 1. Architecture Goals

The architecture must support:

- Hundreds to hundreds of thousands of tables.
- Multiple databases and schemas per project.
- Cross-schema and cross-database relationships.
- Fact, dimension, snapshot, delta, history, SCD, append-only, and reference tables.
- Partial or missing PK/FK constraints.
- Large warehouses where full scans are not acceptable.
- Semantic understanding of business meaning.
- Natural-language requests ranging from one-step questions to multi-step analytical instructions.
- Reusable analytical tools.
- Multi-model LLM routing.
- Full lineage and impact analysis.
- Strong user, agent, tool, and source-system authorization.
- Low latency for runtime analytical questions.
- Low LLM token usage during metadata maintenance.
- Human review only where confidence is insufficient.

## 2. Logical Architecture

```mermaid
flowchart TB

    subgraph UX[User Experience]
      CHAT[Analyst Chat]
      SQLEDIT[SQL Editor]
      GRAPHUI[Graph Explorer]
      SEMUI[Semantic Model]
      LINEAGEUI[Lineage Explorer]
      TOOLUI[Tool Registry]
      REVIEWUI[Review Queue]
      ADMINUI[Admin / Security]
    end

    subgraph RUNTIME[Agent Runtime]
      ROUTER[Intent Router]
      PLANNER[Task Planner]
      CONTEXT[Context Builder]
      TOOLSEL[Tool Selector]
      SQLGEN[SQL Generator]
      RESULT[Result Analyst]
      MEMORY[Query Memory]
    end

    subgraph GOV[Governance Runtime]
      POLICY[Policy Engine]
      SQLGUARD[SQL Guard]
      COST[Cost Governor]
      AUDIT[Audit]
      IDENTITY[Identity / RBAC / ABAC]
    end

    subgraph SEM[Semantic Intelligence]
      SEMSVC[Semantic Service]
      METRIC[Metric Compiler]
      GRAPH[Graph Service]
      SEARCH[Hybrid Retrieval]
      CONF[Confidence / Evidence]
      CANON[Canonical Table Resolver]
    end

    subgraph META[Metadata Intelligence]
      DISC[Discovery]
      PROF[Profiler]
      KEY[Key Detector]
      REL[Relationship Engine]
      TEMP[Temporal Analyzer]
      CLASS[Data Classifier]
      FAMILY[Table Family Detector]
      DOMAIN[Domain Discovery]
      LIN[Lineage Extractor]
      ENRICH[LLM Semantic Enrichment]
    end

    subgraph STATE[Platform State]
      PG[(PostgreSQL)]
      VEC[(pgvector)]
      NEO[(Neo4j)]
      REDIS[(Redis)]
      OBJ[(Object Storage / Parquet)]
      BUS[(Kafka/Event Bus)]
    end

    subgraph SOURCES[Enterprise Data Sources]
      HIVE[(Hive / Hadoop)]
      SNOW[(Snowflake)]
      DBX[(Databricks)]
      SQLS[(SQL Server)]
      ORA[(Oracle)]
      PGDB[(PostgreSQL)]
      BQ[(BigQuery)]
      FILES[(Parquet / Files)]
      API[(External APIs)]
    end

    UX --> RUNTIME
    RUNTIME --> GOV
    RUNTIME --> SEM
    SEM --> STATE
    META --> STATE
    META --> SOURCES
    GOV --> SOURCES
    RUNTIME --> SOURCES
    BUS --> META
```

## 3. Physical Architecture

```mermaid
flowchart LR
    U[Browser / Client] --> GW[API Gateway]
    GW --> API[Application API]
    API --> AUTH[Identity Provider]
    API --> ORCH[Agent Orchestrator]
    API --> METAAPI[Metadata API]
    API --> GRAPHAPI[Graph API]
    API --> TOOLAPI[Tool Registry API]

    ORCH --> MGW[Model Gateway]
    ORCH --> POLICY[Policy Engine]
    ORCH --> EXEC[Query Execution Gateway]

    METAAPI --> PG[(PostgreSQL + pgvector)]
    GRAPHAPI --> NEO[(Neo4j)]
    API --> REDIS[(Redis)]

    SCHED[Analysis Scheduler] --> QUEUE[Task Queue / Event Bus]
    QUEUE --> WP[Profiling Workers]
    QUEUE --> WR[Relationship Workers]
    QUEUE --> WL[Lineage Workers]
    QUEUE --> WE[Semantic Enrichment Workers]

    WP --> DS[(Source Warehouses)]
    WR --> DS
    WL --> DS
    EXEC --> DS

    WP --> OBJ[(Object Storage)]
    WP --> PG
    WR --> PG
    WR --> NEO
    WL --> NEO
    WE --> MGW
    WE --> PG
    WE --> NEO
```

## 4. Major Components

### 4.1 Connector Service

Responsibilities:

- Credential and connection configuration.
- Connectivity validation.
- Dialect identification.
- Metadata API abstraction.
- Query execution abstraction.
- Capability discovery.
- Connection pooling.
- Rate limiting.
- Source-specific feature flags.

Each connector should expose a normalized interface:

```text
list_catalogs()
list_schemas()
list_tables()
list_columns()
get_constraints()
get_indexes()
get_partitions()
get_table_statistics()
get_view_definition()
sample_rows()
execute_profile_query()
explain_query()
execute_query()
get_query_history()
```

### 4.2 Metadata Analysis Scheduler

The scheduler does not "send every table to an agent."

It:

1. Creates an `AnalysisRun`.
2. Discovers the scope.
3. Creates a DAG of deterministic jobs.
4. Assigns priorities.
5. Enforces warehouse concurrency limits.
6. Retries transient failures.
7. Skips unchanged objects.
8. Creates cross-table analysis only after prerequisites complete.
9. Triggers LLM enrichment selectively.
10. Publishes a new semantic version only after validation.

### 4.3 Metadata Store

PostgreSQL is authoritative for:

- Project configuration
- data-source registrations
- catalogs/schemas/tables/columns
- profiling summaries
- relationship candidates
- evidence and confidence
- semantic objects
- metric definitions
- tools
- policies
- model configurations
- analysis runs and tasks
- review decisions
- versions
- query memory
- audit references

### 4.4 Knowledge Graph

Neo4j stores graph-native relationships such as:

```text
Database -HAS_SCHEMA-> Schema
Schema -HAS_TABLE-> Table
Table -HAS_COLUMN-> Column
Column -REFERENCES-> Column
Table -DERIVES_FROM-> Table
Metric -USES_COLUMN-> Column
BusinessEntity -REPRESENTED_BY-> Table
Tool -READS_FROM-> Table
Agent -CAN_CALL-> Tool
Report -USES_METRIC-> Metric
```

### 4.5 Hybrid Retrieval

Runtime retrieval should combine:

```text
lexical search
+
vector similarity
+
graph traversal
+
metadata filters
+
usage history
+
confidence
+
security filtering
```

A useful ranking model is:

```text
final_score =
  semantic_similarity
  * domain_relevance
  * confidence_factor
  * canonical_table_factor
  * usage_factor
  * permission_factor
```

### 4.6 Semantic Query Engine

The semantic layer provides concepts such as:

- Business entities
- dimensions
- measures
- metrics
- time semantics
- grain
- valid joins
- filter rules
- default aggregations
- synonyms
- canonical tables
- history behavior

The agent should reason primarily over semantic objects rather than raw physical metadata.

## 5. Runtime Query Path

```mermaid
flowchart TD
    Q[User Question] --> I[Intent Classification]
    I --> P[Multi-Step Plan]
    P --> S[Semantic Entity Resolution]
    S --> T{Approved Tool Exists?}
    T -->|Yes| TC[Tool Call]
    T -->|No| R[Hybrid Metadata Retrieval]
    R --> JP[Graph Join Planning]
    JP --> LQ[Logical Query Plan]
    LQ --> SQL[SQL Generation]
    SQL --> AST[AST Parse]
    AST --> POL[Policy Check]
    POL --> COST[EXPLAIN / Cost Check]
    COST --> EXEC[Execute]
    TC --> EXEC
    EXEC --> VAL[Result Validation]
    VAL --> ANA[Analysis / Explanation]
    ANA --> RESP[User Response]
    RESP --> SAVE{Save as Tool?}
    SAVE -->|Yes| REG[Tool Registry]
```

## 6. Separation of Control Plane and Data Plane

### Control Plane

Contains:

- metadata
- semantic model
- policies
- models
- tools
- agent definitions
- lineage
- audit
- job orchestration

### Data Plane

Contains:

- source warehouse queries
- result sets
- temporary analytical data
- approved execution environments

The platform should avoid permanently copying source business data unless a feature explicitly requires it.

## 7. Architecture Decision Summary

### Use PostgreSQL + Neo4j + Vector Index

Reason:

- PostgreSQL provides transactional metadata consistency.
- Neo4j provides graph traversal and lineage.
- Vectors provide semantic retrieval.
- These problems are complementary, not substitutes.

### Do Not Use a Vector DB as the Source of Truth

Vector stores do not represent authoritative PK/FK semantics, versioning, approval, or transactional relationships well.

### Do Not Use the LLM as the Profiler

Profiling is cheaper, faster, reproducible, and more accurate when deterministic.

### Do Not Use One Agent Per Table

The unit of execution is an analysis task, scheduled within a dependency graph.

## 8. Quality Attributes

Priority order:

1. correctness
2. security
3. explainability
4. reproducibility
5. metadata freshness
6. query latency
7. cost efficiency
8. scalability
9. extensibility
