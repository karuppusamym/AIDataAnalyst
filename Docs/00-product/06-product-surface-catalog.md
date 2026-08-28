# Product Surface Catalog

> Status: Authoritative. Owner: Product.
> Purpose: the complete inventory of user-facing surfaces Atlas ships, mapped to the module that implements each. This is the "what does the product actually contain" document.

## 1. Surface taxonomy

Atlas ships four kinds of surface. Confusing them is how products become bloated.

| Kind | Definition | Governing rule |
|---|---|---|
| **Workbench** | A persona's primary working environment | One per persona job cluster. Adding a workbench requires a persona job in `02-personas-and-jobs.md`. |
| **Workspace** | A focused task environment inside a workbench | Must have a completion state — the user finishes and leaves. |
| **Inspector** | A read-only evidence pane | Never mutates. Always reachable by permalink. |
| **Console** | An operator/admin control surface | Privileged; never analyst-reachable. |

## 2. Surface inventory

### 2.1 Analyst workbench

| Surface | Kind | Module | Jobs |
|---|---|---|---|
| Ask (analyst console) | Workbench | 13 Agent runtime | A1, A2, A5 |
| Plan preview + refusal explainer | Inspector | 13 | A5 |
| Result evidence pane (lineage, versions, confidence, masking) | Inspector | 09, 13 | A2 |
| Tool catalog and invocation | Workspace | 14 Tool registry | A4, B1 |
| Promote analysis to tool | Workspace | 14 | A4 |
| SQL editor with EXPLAIN | Workspace | 16 Query gateway | A1 |
| Query memory / history | Workspace | 13 | A4 |
| Global search + command palette | Workbench | 12 Retrieval | A3 |
| Asset detail (table/column/metric) | Inspector | 04 Catalog | A3 |

### 2.2 Steward workbench

| Surface | Kind | Module | Jobs |
|---|---|---|---|
| Domain overview + coverage scorecard | Workbench | 08 Glossary & stewardship | S5 |
| Business meaning inference + review | Workspace | 07 Semantic layer | S1 |
| Glossary term lifecycle | Workspace | 08 | S1, S2 |
| Conflict resolution | Workspace | 08 | S2 |
| Bulk ownership / classification / tagging | Workspace | 08, 04 | S3 |
| Relationship review | Workspace | 06 Relationship intelligence | S1 |
| Metric composer | Workspace | 07 | S1 |
| Impact analysis | Inspector | 09 Lineage | S4 |
| Knowledge graph explorer | Workbench | 10 Knowledge graph | S4 |
| Quality policy authoring | Workspace | 11 Data quality | S5 |
| Unowned-asset backlog | Workspace | 08 | S3 |

### 2.3 Studio (semantic + tool authoring)

| Surface | Kind | Module | Jobs |
|---|---|---|---|
| Semantic model editor (versioned, diffable) | Workbench | 18 Studio | S1 |
| Tool authoring + parameter contract designer | Workspace | 18, 14 | A4, S1 |
| Test harness (dry-run against fixtures) | Workspace | 18 | S1 |
| Git-backed change sets | Workspace | 18 | S1 |
| Context product builder | Workspace | 19 Context products | S1, P5 |

> Competitive note: Snowflake ships Semantic Studio with Git integration; Atlan ships Context Engineering Studio. Studio is a parity requirement, differentiated by the fact that Atlas's semantic objects carry policy and compile to executable governed tools.

### 2.4 Reviewer workbench

| Surface | Kind | Module | Jobs |
|---|---|---|---|
| Unified governance queue (all object types) | Workbench | 17 Policy & governance | R1, R3 |
| Proposal detail: evidence, diff, blast radius | Inspector | 17 | R1 |
| Bulk decision with per-item rationale | Workspace | 17 | R3 |
| Delegation and assignment | Workspace | 17 | R3 |
| Decision history | Inspector | 20 Observability & audit | R4 |

### 2.5 Operator console

| Surface | Kind | Module | Jobs |
|---|---|---|---|
| Source fleet + capability matrix | Console | 02 Connectivity | P1, P2 |
| Bulk source onboarding | Workspace | 02 | P1 |
| Connector certification runs | Console | 02 | P1 |
| Ingestion batch monitor (manifests, chunks, replay) | Console | 03 Ingestion | P2 |
| Analysis run monitor (DAG, retries, cancel/resume) | Console | 05 Profiling | P2 |
| Scheduler / backpressure / quota controls | Console | 03, 05 | P2 |
| Projection health + rebuild | Console | 10 Knowledge graph | P3 |
| Outbox / dead-letter management | Console | 20 | P2 |
| Secret provider status + rotation | Console | 01 Identity | P4 |
| Model route governance + activation posture | Console | 15 Model gateway | P5 |
| **Kill switch** | Console | 15 | P5 |
| Cost / showback dashboard | Console | 20 | P6 |
| SLO dashboard | Console | 20 | P2 |

### 2.6 Auditor surface

| Surface | Kind | Module | Jobs |
|---|---|---|---|
| Audit ledger search + export | Workbench | 20 | U1 |
| Agent run evidence replay | Inspector | 13 | U2 |
| Approval chain viewer | Inspector | 17 | U3 |
| Compliance pack generator | Workspace | 20 | U4 |
| Runtime posture attestation | Inspector | 17, 20 | U2 |

### 2.7 Programmatic surfaces

| Surface | Kind | Module |
|---|---|---|
| REST control-plane API (OpenAPI) | API | all |
| MCP server (governed context products) | API | 19 |
| Event stream (outbox → Kafka) | API | 20 |
| Connector SDK | SDK | 02 |
| Tool SDK | SDK | 14 |
| OpenLineage ingestion endpoint | API | 09 |
| Metadata ingestion envelope | API | 03 |

## 3. Surface count discipline

| Persona | Workbenches | Workspaces | Inspectors | Consoles |
|---|:--:|:--:|:--:|:--:|
| Analyst | 2 | 4 | 3 | 0 |
| Business consumer | 0 (uses Analyst, restricted) | 1 | 1 | 0 |
| Steward | 2 | 7 | 2 | 0 |
| Reviewer | 1 | 2 | 2 | 0 |
| Operator | 0 | 2 | 0 | 11 |
| Auditor | 1 | 1 | 3 | 0 |

**Rule.** A new surface requires: (a) a named persona job it serves, (b) a statement of which existing surface it is *not* duplicating, and (c) removal or merge of a surface if the persona's count would exceed the table above. Surface count is a budget, not a backlog.

## 4. Surfaces deliberately not built

| Not built | Why | Alternative |
|---|---|---|
| Dashboard builder | BI tools own this | Export governed metrics to BI |
| Notebook environment | Hex/Databricks own this | MCP context into their notebooks |
| Pipeline/DAG authoring | dbt/Airflow own this | Ingest their artifacts |
| Data-entry / write-back forms | Read-only platform | Out of scope |
| Chat with arbitrary documents | Different problem class | Out of scope this horizon |
| In-product ticketing | ITSM owns this | Webhook to ServiceNow/Jira |

## Related documents

- Personas: `00-product/02-personas-and-jobs.md`
- Experience shell: `20-modules/21-experience-shell.md`
- Module index: `20-modules/00-module-index.md`
