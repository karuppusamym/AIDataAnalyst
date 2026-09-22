# Product Surface Catalog

> Status: Authoritative. Owner: Product.
> Purpose: the inventory of user-facing surfaces Atlas's product design defines, mapped to the module that owns each. This is the "what is the product meant to contain, and who owns it" document. It is **not** a statement of what is built: several surfaces below are partial or pending — Studio (18) is `Partial`: its change sets, tests, diff and impact are on the API and a screen reads and submits them, but nothing in the UI authors one, and its Git binding is deferred (P2) — so read implementation state from `60-delivery/00-status.md` and tracker section P, never from this catalog.
> Claims: competitor statements below assessed 2026-08-28 against vendor-stated public positioning; re-verify by 2026-11-28; sources: `90-reference/03-sources.md`. What a vendor ships, below, is that assessment, not current fact.

## 1. Surface taxonomy

Atlas ships four kinds of surface. Confusing them is how products become bloated.

| Kind | Definition | Governing rule |
|---|---|---|
| **Workbench** | A persona's primary working environment | One per persona job cluster. Adding a workbench requires a persona job in `02-personas-and-jobs.md`. |
| **Workspace** | A focused task environment inside a workbench | Must have a completion state — the user finishes and leaves. |
| **Inspector** | A read-only evidence pane | Never mutates. Always reachable by permalink. |
| **Console** | An operator/admin control surface | Privileged. The backend refuses a caller without the role; the shell lists every screen to every persona, so a console an analyst may not use is one that refuses, not one that is hidden. |

## 2. Surface inventory

> **Implementation status (2026-09-20).** Fifteen screens the shell ships had no row in this inventory and were added on this date; each names its route from `ui-next/src/lib/routes.ts` (`SCREEN_IDS`). Each is mapped to its owning module from its screen header, and its Jobs entry is the closest persona job, or a dash where none fits — a best fit, not a product decision. In the shell, Delegations sits in the Operator area although its job (R3) is the Reviewer's.

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
| Tool plans (`#/analyst/tool-plans`) | Workspace | 14 Tool registry | A4 |
| Marketplace, Consumer area (`#/consumer/marketplace`) | Workspace | 19 Context products | A3 |
| Portfolio analytics, Consumer area (`#/consumer/portfolio-analytics`) | Inspector | 19 | — |

> **Implementation status (2026-09-21).** The palette (Ctrl+K) jumps between the shell's screens and, since R11-AUD08, lists matching catalog tables under the page list with a "Search all" entry; it does not search terms or tools. The Search screen (`#/analyst/search`) calls `GET /v1/search` and `GET /v1/search/suggest` (lexical, table and column names). The fused `GET /v1/organizations/{organization_id}/global-search` route has no ui-next caller. See [module 21](../20-modules/21-experience-shell.md).

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
| Task agents: steward, lineage, quality (`#/steward/task-agents`) | Workspace | 13 Agent runtime, 08 | S1 |
| Negative knowledge (`#/steward/negative-knowledge`) | Workspace | 06 | S1 |
| Cross-source relationships and object resolution (`#/steward/cross-source`) | Workspace | 06 | S1 |
| Transformations, dbt (`#/steward/transformations`) | Workspace | 09 Lineage | S4 |

> **Implementation status (2026-09-21).** The steward workspace (`#/steward/stewardship`) runs the catalog bulk actions (tag, classify, own, certify), the unowned-asset backlog and, since R11-AUD08, a Coverage view of the six-dimension score and its snapshot history. Glossary review (`#/steward/glossary-review`) lists glossary conflicts, proposes a resolution, and generates and submits term-link proposals; Ownership (`#/steward/ownership`) lists assignments, creates and applies ownership rules, and files a leaver reassignment. Business meaning creates and submits glossary terms. Every write that needs a second person opens a review a different reviewer must approve, and approving a conflict resolution marks the conflict resolved without editing either term. Still without a ui-next caller: the route that creates a reviewed bulk operation directly (the Ownership screen only lists them), glossary categories, editing or retiring an ownership rule (there is no route), and coverage at domain or line-of-business scope. The Conflict resolution row above is therefore served as far as proposing and reviewing a resolution; merging or renaming a term as the outcome of one is not built. These screens were tested with mocked transport and in the demo build; the dev database holds no rows for them, so none has been run against the running API with data. Tracker row UX-21 is deferred with R11-C12. See [module 08](../20-modules/08-glossary-and-stewardship.md).

### 2.3 Studio (semantic + tool authoring)

| Surface | Kind | Module | Jobs |
|---|---|---|---|
| Semantic model editor (versioned, diffable) | Workbench | 18 Studio | S1 |
| Tool authoring + parameter contract designer | Workspace | 18, 14 | A4, S1 |
| Test harness (dry-run against fixtures) | Workspace | 18 | S1 |
| Git-backed change sets | Workspace | 18 | S1 |
| Context product builder | Workspace | 19 Context products | S1, P5 |

> Competitive note: Snowflake ships Semantic Studio with Git integration; Atlan ships Context Engineering Studio. Studio is a parity requirement, differentiated by the fact that Atlas's semantic objects carry policy and compile to executable governed tools.

> **Implementation status (2026-09-21).** The Studio screen (`#/steward/studio`) lists change sets and shows their items, diff, impact and latest eval run, and, since R11-AUD08, lets the four roles that may write Studio (DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin) create a change set, add or remove items while it is DRAFT, run the tests, detect conflicts against a published state the author supplies, mine eval questions and submit. The API's writes did not survive their own request until 2026-09-21 (five routes flushed and never committed, so an author could not have reached submission through the API); `tests/test_studio_writes_persist.py` now sends each call as its own request. The test run reports totals, not the reason an item failed. Git-backed change sets have no code (deferred, P2). See [module 18](../20-modules/18-studio.md).

### 2.4 Reviewer workbench

| Surface | Kind | Module | Jobs |
|---|---|---|---|
| Unified governance queue (all object types) | Workbench | 17 Policy & governance | R1, R3 |
| Proposal detail: evidence, diff, blast radius | Inspector | 17 | R1 |
| Bulk decision with per-item rationale | Workspace | 17 | R3 |
| Delegation and assignment (the shell files Delegations under Operator: `#/operator/delegations`) | Workspace | 17 | R3 |
| Decision history | Inspector | 20 Observability & audit | R4 |
| Agent inbox (`#/inbox/inbox`) | Workbench | 13 Agent runtime, 17 | R1, P5 |
| Reviewer agent (`#/reviewer/reviewer-agent`) | Workspace | 17 | R3 |

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
| Agent roster (`#/operator/agent-roster`) | Inspector | 13 | U2 |
| AI registry: trust score and remediation (`#/operator/ai`) | Console | 15 | P5 |
| Access policies + authorization simulation (`#/operator/access-policies`) | Console | 17 | — |
| Workspace access: members, source bindings, BI connections (`#/operator/workspace-access`) | Console | 01 | — |
| Administration: organization, workspace, project, source setup (`#/operator/administration`) | Console | 01, 02 | P1 |

> **Implementation status (2026-09-21).** The organization-wide kill switch (`POST /v1/organizations/{organization_id}/kill-switch/engage` and `.../release`, and its state at `GET /v1/organizations/{organization_id}/kill-switch`) has a panel on the AI governance screen (`ui-next/src/screens/AiGovernanceKillSwitch.tsx`, R11-AUD08): every role the read admits sees whether it is engaged, by whom and why, and routes stopped one at a time are listed read-only; Engage and Release are PlatformAdmin only, each behind a confirmation that states the effect and asks for a reason. The per-agent switch is in the Agent inbox (`ui-next/src/screens/AgentInboxScreen.tsx`), which shows a banner while one is engaged; releasing one agent's switch still has no screen. Persona job P5 (stop AI immediately) is therefore reachable in the UI at organization scope. The panel was tested with mocked transport and opened in a real browser by the demo rehearsal; neither engage nor release has been exercised against the running stack.

### 2.6 Auditor surface

| Surface | Kind | Module | Jobs |
|---|---|---|---|
| Audit ledger search + export | Workbench | 20 | U1 |
| Agent run evidence replay | Inspector | 13 | U2 |
| Approval chain viewer | Inspector | 17 | U3 |
| Compliance pack generator | Workspace | 20 | U4 |
| Runtime posture attestation | Inspector | 17, 20 | U2 |
| Policy refusals (`#/auditor/refusals`) | Inspector | 09 | U2 |

> **Implementation status (2026-09-20).** The Audit ledger screen (`#/auditor/audit`) lists and filters events, and Compliance packs (`#/auditor/compliance`) generates evidence packs. The ledger's export (`/v1/organizations/{organization_id}/audit-events/export.jsonl`) has no ui-next caller, so the export half of the first row is API-only.

### 2.7 Developer workbench

The audience that *consumes* governed context rather than reading it: agent and
integration engineers. Recorded as its own workbench by `ADR-0028` — the
programmatic surfaces in 2.8 all existed with no human surface at all, which is
why the MCP server shipped complete and undiscoverable.

| Surface | Kind | Module | Jobs |
|---|---|---|---|
| Context product registry + compiler | Workbench | 19 Context products | S1, P5 |
| Staged rollout (consumer version bindings) | Workspace | 19 | P5 |
| Agent gateway — connection + client configuration | Inspector | 19, 14 | P5 |
| Agent gateway — exposure (`tools/list`, `prompts/list` as an agent sees them) | Inspector | 14, 19 | P5 |
| Agent gateway — consumption edges, allowed and refused | Inspector | 20 Observability & audit | P5, U2 |

> The gateway is read-only by construction. It never executes a tool or reads a
> resource on a caller's behalf; it shows what the caller is already entitled
> to see, so it cannot become a second path to data.

### 2.8 Programmatic surfaces

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
| Developer | 1 | 1 | 3 | 0 |
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
