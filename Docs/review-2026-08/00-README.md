# Independent Architecture Review — August 2026

> Status: **Closed and largely acted on.** This started as a clean-room second opinion and is now
> a mixture of durable reference (research, target designs), records of work that shipped, and two
> decisions still waiting on an owner. For current status, read `60-delivery/00-status.md` — not
> this folder. Nothing here is maintained as status.

The review was commissioned to answer one question: *given what the market actually ships in 2026,
and given what this codebase actually does, what should the production-grade product be?*

## How this pass was run

1. **Deep competitor research** — Collibra, Atlan, Alation, Microsoft Purview, Databricks Unity
   Catalog, and two AI-native auto-documentation catalogers (Secoda, Select Star). Primary sources
   where possible: vendor docs, developer portals, release notes, open-source repos. Reviews and
   analyst material used only for weaknesses and cost.
2. **Baseline map of the existing design** — all 90+ files under `Docs/` read and mapped.
3. **Independent audit of the code** — every significant doc claim checked against the code that
   is supposed to implement it.
4. **Clean-room target design** — written from requirements and market reality without obligation
   to the existing architecture, *then* reconciled against it.

The order mattered: the designs in `target/` were not produced by editing the current docs.

## What is in here, and what each part is for

### Still live — decisions waiting on an owner

| File | Decision |
|---|---|
| `decisions/01-repo-hygiene.md` | 19 tracked files and 7.9 MB under `scratch/`, 22 MB `.git`. Untrack, or rewrite history? It touches a shared branch |
| `decisions/02-embedding-model.md` | Which embedding model. `embedding_model_id` is `unset`, so nothing produces a vector, and `index_signature` pins the choice — getting it wrong means reindexing everything |

### Durable reference — read these for the *why*

| File | Contents |
|---|---|
| `research/01`–`06` | Per-vendor primary-source detail: Collibra (×3), Atlan, the Alation/Purview/Unity/AI-native group, and the cross-vendor synthesis. All vendor research now lives here |
| `target/00-design-brief.md` | The architectural stance and the three-axis argument that ADR-0018 was accepted on |
| `target/01-metadata-graph-wiki.md` | The wiki and document-ingestion object models (N8, N10). Nothing in `20-modules/` covers this ground |
| `target/02-lineage-inference-review.md` | View and procedure parsing designs, and the degradation table that keeps N3 honest |
| `target/03-context-tools-agents-mcp.md` | Tool generators B/C/D and the agent registry (N11–N13, N15) |
| `target/04-tenancy-roles-workspaces.md` | The roles, policy and source-binding design. **Largely shipped** — see ADR-0018 |
| `target/05-target-architecture.md` | The 16-module target map, and INV-10, which is proposed and not yet accepted |
| `gap/10-how-to-verify-this-work.md` | How to check any of this yourself, and the dataset shapes behind every benchmark in ADR-0019 and ADR-0020 |

### Historical — records of work that shipped

`gap/02` is the original plan with the per-item week and risk estimates; the open items from it now
live in `60-delivery/03-tracker.md`. `gap/04` and `gap/05`–`gap/09` are completion records: each
keeps the design rationale for why the code looks the way it does, which is worth keeping, and each
has had its status section corrected or marked closed.

| File | What it records |
|---|---|
| `gap/02-gap-diff-and-plan.md` | Keep / correct / new / debt / drop, with cost and risk per item. **The historical plan** |
| `gap/04-documentation-truth-pass.md` | Which claim in which document was false, and what code proved it. 28 dated callouts applied |
| `gap/05-validate-sql-handoff.md` | `validate_sql` (N14). Carries the **fourteen-code finding vocabulary**, which is an append-only published contract |
| `gap/06-tier0-invariant-suite.md` | Per-test design rationale for the nine invariants, and the 20-of-20 mutation-testing table |
| `gap/07-envelope-v11.md` | Envelope 1.1's storage model and the four defended shape decisions |
| `gap/08-envelope-v11-connectors.md` | The dictionary view behind each axis on Oracle, Snowflake and BigQuery, and the nine truncation and permission surprises |
| `gap/09-inv7-audit-closeout.md` | The thirteen-endpoint audit-action vocabulary and the lazy-default-row carve-out |

## The three findings that mattered most

Recorded as they were written, with what happened to each.

1. **The code was better than the docs suggested, and the docs claimed a structure that did not
   exist.** The query gateway, the AST-bound tool renderer, the five connectors and the JSON-RPC
   MCP server were genuine engineering. The 21-module decomposition the architecture documents are
   written around had 1 of 21 modules built, and that one was a scaffold. → *Acted on:* the
   documentation truth pass (`gap/04`, tracker ST-12) applied 28 dated callouts. The decomposition
   itself is still ahead of the code — tracker ST-05 onward.

2. **Five things the product requirements depend on had zero code foundation:** the wiki, document
   upload-and-map, workspace-as-a-grantable-container, cross-source federated query, and
   view/procedure-to-tool generation. → *Acted on:* the workspace shipped (ADR-0018, N6/N7/N9). The
   other four are still greenfield — tracker N8, N10, N11–N13.

3. **The stack was heavier than the problem.** Neo4j, Kafka and Temporal were all in the topology;
   only Temporal clearly earned its place. → *Acted on:* ADR-0020 settled the graph store by
   measurement rather than preference, which makes Neo4j removable (C7, awaiting go-ahead). Kafka
   deferral is C8, unstarted.

## What this review did not do

It did not assume its own conclusions were adopted. Where a proposal was accepted it became an ADR
— 0018, 0019 and 0020 — and where it was rejected or revised, the ADR says so. ADR-0020's argument
was rewritten from cost-based to measurement-based after being challenged, and it carries an
explicit reversal threshold rather than a conclusion.
