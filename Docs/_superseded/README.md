# Superseded Documents

Documents that have been replaced. Nothing here is unique: every durable fact was migrated into
its successor first, and the mapping below names that successor. They are kept rather than deleted
so that a claim can be traced back to where it came from.

Two intakes so far:

* **2026-08-28** — files `01`–`18`, the flat document set replaced by the foldered structure.
* **2026-08-30** — files `19`–`29`, retired during the documentation consolidation. Status had
  accumulated in twelve places, four of which disagreed with each other; it now lives in
  `60-delivery/00-status.md` alone.

The repository *is* under git, so these files also exist in history — an earlier version of this
README said otherwise and was wrong. They can be deleted whenever the mapping below has been
reviewed.

## Where each document went

### 2026-08-30 intake — documentation consolidation

| Superseded file | Replaced by | Why |
|---|---|---|
| `19-application-planning-roadmap.md` | `00-product/01`, `03`, `04`, `05`, `60-delivery/01-roadmap.md` | A parallel strategy-and-roadmap document that duplicated the product folder and the roadmap, and had drifted from both |
| `20-atlan-analysis.md` | `review-2026-08/research/02-atlan.md` | Superseded by deeper primary-source research — Personas/Purposes, the metadata-vs-data-policy enforcement question, the popularity formula, the MCP tool surface |
| `21-collibra-analysis.md` | `review-2026-08/research/01-collibra.md`, plus `research/05` and `06` | Same vendor, three documents. The research file carries the lineage source matrix by mechanism, the MCP tool split, the AI Copilot's documented limits and the pricing table |
| `22-alation-analysis.md` | `review-2026-08/research/03-alation-purview-unity-ainative.md` | Superseded by research that decomposes the Articles / Document Hubs object model, which is the source of the wiki design in `target/01` |
| `23-cloud-catalogs-purview-databricks.md` | `review-2026-08/research/03-alation-purview-unity-ainative.md` | Same two products, covered further — Databricks ABAC and the Genie curated-knowledge store |
| `24-codebase-gap-analysis.md` | `60-delivery/00-status.md`, `03-tracker.md` | An internal codebase audit misfiled under competitor analysis, and stale — it predates the 2026-08 review's evidence-level audit |
| `25-codebase-architecture-reference.md` | `10-architecture/`, `40-engineering/02-repository-layout.md` | Line-count-based internals reference, accurate as of 2026-08-28 and stale since. Structure belongs in the architecture folder where it is maintained |
| `26-mcp-server-integration-guide.md` | `20-modules/19-context-products-and-mcp.md`; the client setup was lifted into `40-engineering/07-local-runbook.md` §9b | Stale, and its one piece of unique operational content now sits in the runbook where someone would look for it |
| `27-review-baseline-reality.md` | `review-2026-08/gap/04-documentation-truth-pass.md`, `60-delivery/00-status.md` | The 2026-08 review's opening snapshot of the codebase. Deliberately a point-in-time measurement, and almost every number in it has since moved — CI, contract count, test count, the workspace model. The truth pass supersedes its doc-vs-code table with fresher evidence |
| `28-implementation-status-matrix-2026-08.md` | `60-delivery/00-status.md` §4 | Merged, whole, into the single status document |
| `29-enterprise-gap-register-2026-08.md` | `60-delivery/00-status.md` §5, §7, §9 | Merged. Its closed rows moved to `06-accomplishment-log.md`; a status document that lists what is already done stops being scannable |

### 2026-08-28 intake — flat to foldered structure

| Superseded file | Migrated into |
|---|---|
| `01-architecture.md` | `10-architecture/03-logical-architecture.md`, `06-data-architecture.md` |
| `02-metadata-analysis-engine.md` | `90-reference/04-analysis-algorithms.md`, `20-modules/05`, `06`, `10-architecture/08-workers-and-workflows.md` |
| `03-agent-runtime-sequences.md` | `10-architecture/12-runtime-sequences.md` |
| `04-data-semantic-lineage-model.md` | `10-architecture/06-data-architecture.md`, `20-modules/07`, `09` |
| `05-security-governance-api.md` | `50-security/01-security-architecture.md`, `30-contracts/02`, `09`, `20-modules/17` |
| `06-deployment-scaling-operations.md` | `10-architecture/09-deployment-topology.md`, `11-capacity-and-cost-model.md` |
| `07-engineering-backlog.md` | `60-delivery/02-epic-backlog.md` |
| `08-enterprise-assumptions-decisions.md` | `10-architecture/adr/` (ADR-0001 … ADR-0010) |
| `09-development-plan.md` | `60-delivery/01-roadmap.md` |
| `10-accomplishment-log.md` | `60-delivery/06-accomplishment-log.md` (verbatim) |
| `11-local-operations-runbook.md` | `40-engineering/07-local-runbook.md` |
| `12-enterprise-gap-register.md` | `60-delivery/05-gap-register.md`, itself superseded 2026-08-30 by `60-delivery/00-status.md` |
| `13-threat-model.md` | `50-security/02-threat-model.md` |
| `14-implementation-status-matrix.md` | `60-delivery/04-status-matrix.md`, itself superseded 2026-08-30 by `60-delivery/00-status.md`; plus `10-architecture/adr/` |
| `15-ui-capability-coverage.md` | `20-modules/21-experience-shell.md`, `60-delivery/00-status.md` |
| `16-market-comparison-and-product-strategy.md` | `00-product/01`, `03`, `04`, `05`, `60-delivery/01-roadmap.md` |
| `17-enterprise-metadata-ingestion-contract.md` | `30-contracts/05-metadata-ingestion-envelope.md`, `20-modules/03-ingestion.md` |
| `18-oracle-bigquery-implementation-backlog.md` | `60-delivery/07-connector-implementation-backlog.md` |

Start at [`../README.md`](../README.md).
