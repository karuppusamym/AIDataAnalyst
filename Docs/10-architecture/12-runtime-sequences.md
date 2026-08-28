# Runtime Sequences

> Status: Authoritative. Owner: Architecture.
> The interaction sequences that show how the modules compose at runtime. Migrated and updated from the retired flat `03-agent-runtime-sequences.md`.

## 1. Governed NL-to-SQL

The full path when no approved tool matches. Note that **policy resolution and prompt-risk screening happen before retrieval**, and that the gateway sits between every model output and the source.

```mermaid
sequenceDiagram
    actor User
    participant API
    participant Agent as Agent runtime (13)
    participant Screen as Prompt-risk screen
    participant Policy as Policy (17)
    participant Mem as Query memory
    participant Retr as Retrieval (12)
    participant Graph as Knowledge graph (10)
    participant MGW as Model gateway (15)
    participant QGW as Query gateway (16)
    participant DB as Source
    participant Audit as Audit (20)

    User->>API: Natural-language question
    API->>Agent: Submit request (AUTHORIZED)
    Agent->>Screen: Classify (SCREENED)
    Screen-->>Agent: Pass · version + score + reason codes
    Note over Screen: A denial stops here —<br/>before any retrieval

    Agent->>Policy: Resolve effective permissions
    Policy-->>Agent: Allowed domains, tables, columns, tools

    Agent->>Mem: Search prior successful patterns
    Mem-->>Agent: Version-compatible candidates

    Agent->>Retr: Resolve concepts, metrics, dimensions (RESOLVED)
    Retr->>Graph: Expand trusted relationships
    Graph-->>Retr: Join paths, lineage, confidence
    Retr-->>Agent: Ranked, policy-filtered context (PLANNED)

    Agent->>MGW: Request logical plan (metadata only)
    MGW-->>Agent: Schema-validated proposal
    Agent->>MGW: Request SQL from constrained context
    MGW-->>Agent: SQL proposal + assumptions (GENERATED)

    Agent->>QGW: Submit ExecutionRequest
    QGW->>QGW: AST parse, allowlist from parsed refs (VALIDATED)
    QGW->>Policy: Check each referenced object
    Policy-->>QGW: Allow / deny
    QGW->>DB: EXPLAIN
    DB-->>QGW: Plan + estimated cost (COSTED)
    QGW->>DB: Execute read-only, bounded
    DB-->>QGW: Result set
    QGW->>QGW: Mask by classification
    QGW-->>Agent: Result + masking + lineage (EXECUTED)

    Agent->>MGW: Explain the result
    MGW-->>Agent: Explanation
    Agent->>Audit: Evidence, versions, decision lineage (EXPLAINED)
    Agent-->>API: Result + interpretation + SQL + evidence
    API-->>User: Response
```

## 2. Approved tool reuse — the preferred path

Shorter, cheaper, safer, and the path Atlas tries first.

```mermaid
sequenceDiagram
    actor User
    participant Agent as Agent runtime (13)
    participant Tools as Tool registry (14)
    participant Policy as Policy (17)
    participant QGW as Query gateway (16)
    participant DB as Source
    participant Audit as Audit (20)

    User->>Agent: "Run the high-risk customer report for Texas"
    Agent->>Tools: Match resolved intent
    Tools-->>Agent: high_risk_customer_report v1.4
    Agent->>Policy: Check user + agent + tool binding
    Policy-->>Agent: Allowed
    Agent->>Tools: Bind typed parameters
    Tools-->>Agent: Parameterized SQL with AST literal binding
    Agent->>QGW: ExecutionRequest (tool-bound)
    QGW->>DB: Validate, cost, execute, mask
    DB-->>QGW: Results
    QGW-->>Agent: Result + evidence
    Agent->>Audit: Record tool invocation
    Agent-->>User: Results + explanation
```

**No model call occurs on this path.** That is the point: cost and risk fall as the tool library matures (differentiator D2).

## 3. Multi-step analytical plan

> *"Find Texas customers whose average spend increased more than 30% in the last six months versus the previous six months and who had at least three complaints. Compare their churn rate to the overall Texas customer population."*

```text
Step 1  resolve the Texas customer population
Step 2  compute spend for the current six-month window
Step 3  compute spend for the previous six-month window
Step 4  calculate growth
Step 5  filter growth > 30%
Step 6  aggregate complaints
Step 7  filter complaint_count >= 3
Step 8  calculate churn rate for the cohort
Step 9  calculate churn rate for the Texas population
Step 10 compare and explain
```

The planner may emit one optimized query or several staged queries depending on the warehouse and complexity. **Every step passes the gateway independently**, and the whole plan is bounded by step, time, token, and cost budgets.

Multi-step plans are **not yet implemented** (tracker AG-4). The current runtime handles single-step requests.

## 4. Promote an analysis to a governed tool

```mermaid
sequenceDiagram
    actor Analyst
    participant Agent as Agent runtime (13)
    participant Tools as Tool registry (14)
    participant Gov as Governance (17)
    actor Checker

    Analyst->>Agent: Successful run — promote this
    Agent->>Tools: create_draft_from_run(run_id)
    Tools->>Tools: Deterministically render SQL → parameterized template
    Note over Tools: The MODEL does not author this.<br/>Parameters are inferred from<br/>the redacted literals.
    Tools-->>Analyst: Draft + inferred parameters
    Analyst->>Tools: Confirm parameter contract
    Analyst->>Gov: Submit for review
    Gov->>Checker: Queue with evidence and blast radius
    Checker->>Gov: Approve with rationale
    Note over Gov: maker ≠ checker, platform-enforced
    Gov->>Tools: Publish v1
    Tools-->>Agent: Preferred for matching intents
```

## 5. Human review of an inferred relationship

```mermaid
sequenceDiagram
    participant Rel as Relationships (06)
    participant Gov as Governance (17)
    actor Steward
    participant Graph as Knowledge graph (10)

    Rel->>Gov: Submit candidate + evidence + confidence
    Gov->>Steward: Queue item with decomposed evidence
    alt Approved
      Steward->>Gov: Approve + rationale
      Gov->>Rel: Mark approved
      Rel->>Graph: Project relationship
    else Rejected
      Steward->>Gov: Reject + rationale
      Gov->>Rel: Record NEGATIVE KNOWLEDGE
      Note over Rel: Not re-proposed unless<br/>evidence changes materially
    end
```

## 6. Semantic change impact

```mermaid
sequenceDiagram
    actor Steward
    participant Studio as Studio (18)
    participant Lin as Lineage (09)
    participant Gov as Governance (17)
    actor Checker

    Steward->>Studio: Edit metric definition
    Studio->>Lin: preview_impact(change_set)
    Lin-->>Studio: Affected tools, metrics, dbt models, reports
    Studio->>Studio: Run tests against synthetic fixtures
    Studio->>Gov: Submit as ONE proposal
    Gov->>Checker: Evidence + diff + blast radius
    Checker->>Gov: Approve
    Gov->>Studio: Publish new version
    Note over Studio: Query memory bound to the<br/>superseded version is suppressed
```

## 7. Agent identity and authorization

```mermaid
sequenceDiagram
    participant Client as External MCP client
    participant Ident as Identity (01)
    participant CX as Context products (19)
    participant Policy as Policy (17)
    participant QGW as Query gateway (16)

    Client->>Ident: Present workload identity
    Ident-->>Client: Verified principal + tenant scope
    Client->>CX: Read context product v3
    CX->>Policy: authorize(principal, read, product, purpose)
    Policy-->>CX: Allow · policy_version pinned
    CX-->>Client: Context + eligible tools + policy summary
    Client->>CX: Invoke eligible tool
    CX->>Policy: authorize(principal, invoke, tool)
    CX->>QGW: ExecutionRequest
    QGW-->>CX: Bounded, masked result
    CX-->>Client: Result + evidence
```

**The symmetry to notice.** An external agent's tool invocation goes through the *same* gateway as a native run. There is no privileged internal path and no unprivileged external one.

## 8. Query failure recovery

```mermaid
flowchart TD
    A[Execute] --> B{Outcome}
    B -->|source timeout| C[Classify transient → retry with backoff]
    B -->|cost rejected| D[Deny with the cost estimate and ceiling]
    B -->|policy denied| E[Deny naming the control, not the rule]
    B -->|parse failure| F[Deny — model output was invalid]
    B -->|source unavailable| G[Isolate: this source only, others unaffected]
    C --> H{Retries exhausted?}
    H -->|no| A
    H -->|yes| I[Fail with classified error + correlation ID]
    D & E & F & G & I --> J[Record refusal in evidence and decision lineage]
```

Every terminal state records **why**, and every refusal becomes an `AI_DECISION` edge. This is what makes "explain what did not happen" possible (whitespace W3).

## 9. Query memory

```mermaid
sequenceDiagram
    participant Agent as Agent runtime (13)
    participant Mem as Query memory
    participant Sem as Semantic layer (07)

    Agent->>Mem: Search by resolved intent shape
    Mem->>Sem: Check pinned semantic version currency
    alt Version current and no negative feedback
      Mem-->>Agent: Candidate pattern (informs planning)
    else Version superseded or feedback negative
      Mem-->>Agent: Suppressed
    end
    Note over Agent,Mem: Memory INFORMS planning.<br/>It never bypasses validation,<br/>policy, cost, or masking.
```

## Related documents

- Logical architecture: `10-architecture/03-logical-architecture.md`
- Agent runtime: `20-modules/13-agent-runtime.md`
- Query gateway: `20-modules/16-query-gateway.md`
- Tool and agent contract: `30-contracts/07-tool-and-agent-contract.md`
