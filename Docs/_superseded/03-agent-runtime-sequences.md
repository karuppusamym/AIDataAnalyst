# 03 — Agent Runtime and Sequence Diagrams

## 1. Runtime Design Principle

Natural language should not be translated directly to SQL in one step.

The runtime pipeline is:

```text
Understand
→ Plan
→ Resolve Semantics
→ Reuse Tool if Possible
→ Retrieve Context
→ Build Logical Plan
→ Generate SQL
→ Validate
→ Authorize
→ Estimate Cost
→ Execute
→ Validate Result
→ Explain
→ Learn
```

## 2. Standard NL-to-SQL Sequence

```mermaid
sequenceDiagram
    actor User
    participant API
    participant Agent as Agent Orchestrator
    participant Sem as Semantic Retrieval
    participant Graph as Knowledge Graph
    participant Mem as Query Memory
    participant LLM as Model Gateway
    participant Guard as SQL Guard
    participant Policy as Policy Engine
    participant DB as Source Warehouse
    participant Audit

    User->>API: Natural-language question
    API->>Agent: Submit analytical request
    Agent->>Policy: Resolve effective permissions
    Policy-->>Agent: Allowed domains/tables/columns/tools

    Agent->>Mem: Search successful prior queries/tools
    Mem-->>Agent: Candidate reusable patterns

    Agent->>Sem: Resolve concepts, metrics, dimensions
    Sem->>Graph: Expand trusted relationships
    Graph-->>Sem: Join paths + lineage + confidence
    Sem-->>Agent: Ranked context package

    Agent->>LLM: Produce logical analytical plan
    LLM-->>Agent: Steps + required semantic objects

    Agent->>LLM: Generate SQL from constrained context
    LLM-->>Agent: SQL + assumptions

    Agent->>Guard: Parse and validate SQL AST
    Guard->>Policy: Check object-level permissions
    Policy-->>Guard: Allow / deny
    Guard->>DB: EXPLAIN / dry run
    DB-->>Guard: Query plan + estimated cost
    Guard-->>Agent: Validated SQL

    Agent->>DB: Execute approved query
    DB-->>Agent: Result set

    Agent->>LLM: Explain / summarize result
    LLM-->>Agent: Analytical response

    Agent->>Audit: Store complete execution lineage
    Agent-->>API: Result + SQL + explanation + evidence
    API-->>User: Response
```

## 3. Approved Tool Reuse Sequence

```mermaid
sequenceDiagram
    actor User
    participant Agent
    participant ToolReg as Tool Registry
    participant Policy
    participant DB
    participant Audit

    User->>Agent: "Run high-risk customer report for Texas"
    Agent->>ToolReg: Search semantic tool registry
    ToolReg-->>Agent: high_risk_customer_report v1.4
    Agent->>Policy: Check user + agent + tool permission
    Policy-->>Agent: Allowed
    Agent->>ToolReg: Resolve validated parameters
    ToolReg-->>Agent: Parameterized SQL/template
    Agent->>DB: Execute governed tool
    DB-->>Agent: Results
    Agent->>Audit: Record tool execution
    Agent-->>User: Results and explanation
```

## 4. Multi-Step Analytical Request

Example request:

> Find Texas customers whose average spend increased more than 30% in the last six months versus the previous six months and who had at least three complaints. Compare their churn rate to the overall Texas customer population.

Planner:

```text
Step 1 — resolve Texas customer population
Step 2 — compute spend for current six-month window
Step 3 — compute spend for previous six-month window
Step 4 — calculate growth
Step 5 — filter growth > 30%
Step 6 — aggregate complaints
Step 7 — filter complaint_count >= 3
Step 8 — calculate churn rate for cohort
Step 9 — calculate churn rate for Texas population
Step 10 — compare and explain
```

The planner may execute one optimized SQL query or multiple staged queries depending on the warehouse and complexity.

## 5. Metadata Discovery Sequence

```mermaid
sequenceDiagram
    participant Scheduler
    participant Connector
    participant Queue
    participant Profiler
    participant Rel as Relationship Engine
    participant Sem as Semantic Enrichment
    participant PG as Metadata DB
    participant KG as Knowledge Graph

    Scheduler->>Connector: Discover catalogs/schemas/tables
    Connector-->>Scheduler: Metadata inventory
    Scheduler->>PG: Create AnalysisRun + objects
    Scheduler->>Queue: Enqueue profiling tasks

    Queue->>Profiler: Assign table profile
    Profiler->>Connector: Read stats / samples
    Connector-->>Profiler: Profile data
    Profiler->>PG: Save profile + fingerprints

    Scheduler->>Queue: Enqueue relationship candidates
    Queue->>Rel: Validate relationship
    Rel->>PG: Read profiles
    Rel->>Connector: Targeted containment checks
    Connector-->>Rel: Evidence
    Rel->>PG: Save candidate + confidence
    Rel->>KG: Save graph edge candidate

    Scheduler->>Queue: Enqueue semantic enrichment
    Queue->>Sem: Generate business semantics
    Sem->>PG: Save semantic objects
    Sem->>KG: Save concepts and mappings
```

## 6. Human Review Sequence

```mermaid
sequenceDiagram
    participant Engine
    participant Review
    actor Steward
    participant Metadata
    participant KG

    Engine->>Review: Create ambiguous relationship item
    Review-->>Steward: Show evidence and candidates
    Steward->>Review: Approve candidate B
    Review->>Metadata: Save HUMAN_VERIFIED decision
    Review->>Metadata: Mark alternatives REJECTED
    Review->>KG: Publish verified edge
```

## 7. Promote Analysis to Tool

```mermaid
sequenceDiagram
    actor Analyst
    participant Agent
    participant Guard
    participant ToolReg
    participant Reviewer
    participant Policy

    Analyst->>Agent: Refine analysis until correct
    Analyst->>Agent: Save this as a reusable tool
    Agent->>Guard: Normalize SQL and identify parameters
    Guard-->>Agent: Safe parameterized definition
    Agent->>ToolReg: Create DRAFT tool version
    ToolReg->>Reviewer: Request approval
    Reviewer->>ToolReg: Approve
    ToolReg->>Policy: Bind roles/agents
    ToolReg-->>Analyst: Tool PUBLISHED
```

## 8. Tool Execution Lineage

Every tool call should create a trace:

```text
User
→ Agent
→ Tool version
→ Semantic version
→ Policy version
→ Data source
→ Tables
→ Columns
→ Query
→ Warehouse execution ID
→ Result metadata
```

## 9. Semantic Change Impact Sequence

```mermaid
sequenceDiagram
    participant Scan
    participant Meta
    participant Graph
    participant Impact
    participant Review

    Scan->>Meta: Detect column/table change
    Meta->>Graph: Update technical metadata
    Graph->>Impact: Find dependent metrics/tools/reports
    Impact-->>Review: Blast-radius report
    Review-->>Meta: Approve semantic migration
```

## 10. Agent Identity and Authorization Sequence

```mermaid
sequenceDiagram
    actor User
    participant IdP
    participant Agent
    participant Policy
    participant Tool
    participant DB

    User->>IdP: Authenticate
    IdP-->>Agent: User claims / roles
    Agent->>Policy: user + agent + requested action
    Policy->>Policy: Intersection of permissions
    Policy-->>Agent: Effective authorization
    Agent->>Tool: Invoke only if permitted
    Tool->>DB: Execute using allowed source identity
```

Effective permission:

```text
User permissions
∩ Agent permissions
∩ Tool permissions
∩ Project permissions
∩ Source-system permissions
```

## 11. Query Failure Recovery

Possible flow:

```mermaid
flowchart TD
    A[Generated Query] --> B[AST Validation]
    B -->|Fail| C[Deterministic Repair]
    C --> B
    B -->|Pass| D[EXPLAIN]
    D -->|Invalid SQL| E[LLM Repair With Error Context]
    E --> B
    D -->|Too Expensive| F[Rewrite / Aggregate / Sample]
    F --> B
    D -->|Pass| G[Execute]
    G -->|Runtime Error| H{Retryable?}
    H -->|Yes| I[Retry]
    H -->|No| J[Return Explainable Failure]
```

Set a strict repair-attempt limit to avoid uncontrolled LLM loops.

## 12. Query Memory Sequence

```mermaid
sequenceDiagram
    actor User
    participant Agent
    participant Memory
    participant Sem
    participant Guard
    participant DB

    User->>Agent: Ask analytical question
    Agent->>Memory: Retrieve similar successful requests
    Memory-->>Agent: Prior query patterns + scores
    Agent->>Sem: Verify semantic compatibility
    Sem-->>Agent: Current semantic mappings
    Agent->>Guard: Adapted SQL
    Guard-->>Agent: Valid
    Agent->>DB: Execute
    DB-->>Agent: Results
    Agent->>Memory: Store successful execution + feedback
```

## 13. Runtime State Machine

```mermaid
stateDiagram-v2
    [*] --> RECEIVED
    RECEIVED --> AUTHORIZED
    AUTHORIZED --> PLANNING
    PLANNING --> RETRIEVING_CONTEXT
    RETRIEVING_CONTEXT --> TOOL_MATCH
    TOOL_MATCH --> TOOL_EXECUTION: approved tool found
    TOOL_MATCH --> QUERY_GENERATION: no tool
    QUERY_GENERATION --> VALIDATING
    VALIDATING --> COST_CHECK
    COST_CHECK --> EXECUTING
    TOOL_EXECUTION --> EXECUTING
    EXECUTING --> ANALYZING
    ANALYZING --> COMPLETED
    VALIDATING --> FAILED
    COST_CHECK --> REJECTED
    EXECUTING --> FAILED
```
