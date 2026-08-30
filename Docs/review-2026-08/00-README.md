# Independent Architecture Review — August 2026

Status: **Proposal. Nothing in the existing `Docs/` tree has been modified.**

This folder is a clean-room second opinion on Atlas/AIDA, commissioned to answer one
question: *given what the market actually ships in 2026, and given what this codebase
actually does, what should the production-grade product be?*

## How this pass was run

1. **Deep competitor research** — Collibra, Atlan, Alation, Microsoft Purview,
   Databricks Unity Catalog, and two AI-native auto-documentation catalogers
   (Secoda, Select Star). Primary sources only where possible: vendor docs,
   developer portals, release notes, open-source repos. Reviews and analyst
   material used only for weaknesses and cost.
2. **Baseline map of the existing design** — all 90+ files under `Docs/`
   (excluding `_superseded/` and `competitors/`) read and mapped.
3. **Independent audit of the code** — `src/aida/`, `src/atlas/`, `ui/`, `tests/`,
   `migrations/`, `pyproject.toml`. Every significant doc claim checked against
   the code that is supposed to implement it.
4. **Clean-room target design** — written without obligation to the existing
   architecture, then reconciled against it.

The order matters. The target design in `target/` was written from requirements and
market reality, *not* by editing the current docs. `gap/02-gap-diff-and-plan.md` is
where the two meet.

## Read in this order

| # | File | What it answers |
|---|---|---|
| 1 | `gap/01-baseline-reality.md` | What does this system actually do today, as opposed to what the docs say? Read this first — it changes how you read everything else. |
| 2 | `research/04-cross-vendor-synthesis.md` | What does the market ship, what is genuinely uncontested, and what is a trap? |
| 3 | `target/00-design-brief.md` | The architectural stance, the three-axis model, and the stack verdict. This is where I disagree with the current design. |
| 4 | `target/01`–`04` | The four requested design threads in detail. |
| 5 | `target/05-target-architecture.md` | Modules, stores, runtime flows. |
| 6 | `gap/02-gap-diff-and-plan.md` | Keep / correct / rewrite / drop, with cost and risk per item. **This is the decision document.** |

Per-vendor detail lives in `research/01`–`03` and is reference material, not narrative.

## The three findings that matter most

1. **The code is better than the docs suggest, and the docs claim a structure that
   does not exist.** The Query Execution Gateway, the AST-bound tool renderer, the
   five real connectors and the 1,776-line JSON-RPC MCP server are genuine,
   working, non-trivial engineering. But the 21-module decomposition the
   architecture documents are written around has 1 of 21 modules built, and that
   one is a 69-line scaffold. Every document phrased in terms of
   `src/atlas/modules/*` describes a plan, not the system.

2. **Five things the product requirements depend on have zero code foundation:**
   the wiki, document upload-and-map, workspace-as-a-grantable-container,
   cross-source federated query, and view/procedure-to-tool generation. These are
   not partially built. They are greenfield, and three of them are load-bearing
   for the stated product.

3. **The stack is heavier than the problem.** Neo4j, Kafka and Temporal are all in
   the deployment topology; only Temporal clearly earns its place. Three of the
   eight overdue operational drills exist only because of stores that a bounded
   metadata graph does not need. See `target/00-design-brief.md` §6.

## What this review does not do

It does not modify existing documents, change any code, or assume its own
conclusions are adopted. `gap/02` proposes; you dispose.
