# Competitor Deep Dive: Collibra Data Lineage & Collibra Platform (2026-08)

> **Document Status**: Authoritative Feature-Requirement Source
> **Target Pages**: [Collibra Data Lineage](https://www.collibra.com/products/data-lineage), [Collibra Platform](https://www.collibra.com/products/collibra-platform)
> **Reviewed**: 2026-08-29
> **Feeds**: `60-delivery/02-epic-backlog.md` (EA.9, EE.8–EE.11), `60-delivery/05-gap-register.md` ("Newly identified gaps"), `20-modules/09-lineage.md` (LN-7, LN-9), `20-modules/19-context-products-and-mcp.md`

This supplements `02-collibra-analysis.md` with a screenshot-driven review of Collibra's two most consequential product pages, done at the user's request. It is the source of truth for the "Unified Lineage Explorer" and "AI Control Plane parity" work opened in this revision.

---

## 1. Collibra Data Lineage — what the page shows

Collibra's lineage page demonstrates capability depth beyond a node-graph:

- Nested system → database → schema → table visualization, not a flat table list.
- Expandable table nodes that show actual columns, with column-to-column connector lines drawn between them.
- Switchable graph modes: **Catalog Lineage**, **Impact Analysis**, and an **AI Project Overview** view that places AI/ML assets in the same graph as data assets.
- Reports, dashboards, policies, standards, quality issues, and AI models rendered as first-class nodes in the same graph as tables — not a separate screen.
- A transformation viewer showing calculation/derivation logic directly beneath the graph for a selected edge.
- Export to SVG, PNG, PDF, and a graph CSV.

### Capability comparison (this session's build vs. Collibra Data Lineage)

| Collibra capability | Our status before this revision | Our status after this revision |
|---|---|---|
| Interactive table/system graph | Yes — bounded knowledge graph with search, direction, depth, zoom, inspector | Unchanged |
| dbt dependency lineage | Yes — manifest ingestion, models, sources, tests, edges | Unchanged |
| OpenLineage ingestion | Yes — table and column edges persisted | Unchanged |
| **One merged graph across FK + suggested + dbt + OpenLineage** | **No** — three separate, unlinked workbenches | **Yes** — `GET /v1/datasources/{id}/unified-lineage/graph` (`unified_lineage_api.py`) |
| **Transitive upstream/downstream impact** | **No** — `/v1/metadata/tables/{id}/impact` counts direct references only | **Yes** — `GET /v1/datasources/{id}/unified-lineage/impact/{node_id}`, bounded BFS over the merged graph (`unified_lineage.py::traverse`) |
| Column-level visualization | Partial — dbt UI matches columns by identical name, not authoritative mapping | Unchanged — still a gap (see §3) |
| Transformation code viewer | Partial — literal-redacted dbt SQL shown separately | Unchanged |
| Cross-system ETL-to-BI lineage | No | Unchanged |
| Reports/dashboards as graph nodes | No | Unchanged |
| Policies/quality/standards overlaid on lineage | No unified visualization | Unchanged |
| View/stored-procedure lineage | No | Unchanged (tracked as EA.9 / LN-2) |
| Export to SVG/PNG/PDF/CSV | No | Unchanged |
| Unmatched dbt/OpenLineage nodes surfaced | No — resources without a matched table silently disappeared from any graph view | **Yes** — synthetic nodes (`dbt:<id>`, `openlineage:<namespace>:<name>`) keep them visible and traversable |

---

## 2. Collibra Platform — "Enterprise AI Control Plane"

Collibra's platform page reframes the product away from "catalog" toward **preparing governed context for AI agents and monitoring AI assets and their usage**. Per the page, as of 2026-08-29: Data Products and Lineage MCP are described as **August 2026 GA**; the context compiler as a **July 2026 preview**; Control Tower controls as **October 2026 GA** (future-dated at time of review).

### Platform capability comparison

| Platform capability | Our status |
|---|---|
| Catalog and business glossary | Implemented |
| Semantic models and governed metrics | Implemented, versioned, maker-checker |
| Governed AI analyst | Implemented |
| Deterministic query/action controls | Strong — SQL validation, cost controls, masking, roles, audit |
| Data quality and incidents | Partial |
| Technical lineage | Partial → **stronger after this revision** (unified graph + transitive impact) |
| MCP server | Implemented for resources, governed/native tools, prompts, budgets, and bounded access-request writes |
| **Natural-language lineage MCP tools** (upstream/downstream graph, fuzzy asset resolution, transformation detail, impact-as-a-tool) | **Implemented foundation 2026-08-29**; corpus and estate-scale certification remain |
| AI asset/use-case/agent registry | **Implemented foundation 2026-08-29**; provider sync and dependency visualization remain |
| AI assessments and Trust Scores | **Implemented foundation 2026-08-29**; managed templates, remediation, retirement, and history remain |
| Governed context compiler (semantic models → Snowflake Semantic Views / Databricks Metric Views / OSI / ODCS / YAML) | **Implemented foundation 2026-08-29**; external validators and file delivery remain |
| Data product registry | **Implemented foundation 2026-08-29** |
| Data contract registry (products, schemas, SLAs, versions, producers/consumers — distinct from `data_contracts.py`'s ingestion-contract validation) | **Implemented foundation 2026-08-29** |
| Data marketplace and access requests | **Implemented foundation 2026-08-29**; entitlement-provider fulfillment remains |
| Workflow designer | Missing — workflows are fixed application processes |

---

## 3. New and updated gaps opened by this review

The lineage-specific items are new (LN-9 through LN-12). The platform-level items were
**already tracked in detail** as CP-1 through CP-14 in
`20-modules/19-context-products-and-mcp.md` §15.2 from an earlier pass over the same Collibra
platform material — this review did not need to re-derive them, only confirm they still match
the current page and wire them into the epic backlog and gap register, which had not yet
happened.

| ID | Gap | Status after this revision |
|:--:|---|---|
| LN-7 | Transitive cross-kind impact traversal | **Delivered** (table-level; column-level, view/procedure, and BI edges remain open) |
| LN-9 *(new)* | One canonical graph merging FK + suggested + dbt + OpenLineage edges | **Delivered** — `unified_lineage_api.py` |
| LN-10 *(new)* | Authoritative column-to-column mapping (replace dbt's identical-name matching) | Open — P1 |
| LN-11 *(new)* | View/stored-procedure/BI nodes in the unified graph | Open — P1, depends on EA.9 |
| LN-12 *(new)* | Graph export: SVG, PNG, PDF, CSV | Open — P2 |
| CP-6 | Lineage MCP tools: upstream/downstream traversal, fuzzy asset resolution, transformation detail, impact-as-a-tool | **Delivered 2026-08-29** (`EE.10`) — all four tools live; fuzzy-resolution corpus benchmark and a dedicated leak test remain open |
| CP-7 / CP-8 | Unified AI registry, assessments, and trust scoring | **Foundation delivered 2026-08-29** — mounted lifecycle/assessment APIs and deterministic explainable trust scoring; managed templates, remediation/retirement, sync, history, and graph UI remain |
| CP-5 | Governed context compiler (semantic model → external context formats) | **Delivered 2026-08-29** (`EE.9`) — MCP/REST/OSI/ODCS/Snowflake/Databricks targets; YAML target is a valid-but-non-idiomatic subset |
| CP-2 / CP-3 | Data product and data contract registries (versioned, lifecycle-managed) | **Foundation delivered and tested 2026-08-29** (`EE.8`) |

Full epic-level acceptance criteria for LN-10 through LN-12 and EE.8 through EE.11 are recorded
in `60-delivery/02-epic-backlog.md`. CP-1 through CP-14 acceptance criteria remain the
authoritative detail in `20-modules/19-context-products-and-mcp.md` §15.2.

## 4. What this revision intentionally does not attempt

Consistent with `60-delivery/05-gap-register.md`'s operating principle ("deliberate simplifications, not omissions"): this revision ships the *first* milestone of the Unified Lineage Explorer plan only — one canonical graph API and transitive impact. It does not build a new UI view, column-level authoritative mapping, export, BI/view/procedure adapters, or any of the Platform-level items in §2. Those are opened as tracked gaps, not silently deferred.

## Related documents

- `competitors/02-collibra-analysis.md` — the original Collibra deep dive this supplements
- `20-modules/09-lineage.md` — module ownership and open work (LN-7, LN-9 through LN-12)
- `20-modules/19-context-products-and-mcp.md` — MCP-1
- `60-delivery/02-epic-backlog.md` — epic acceptance criteria
- `60-delivery/05-gap-register.md` — gap register entries
- `90-reference/03-sources.md` — source URLs
