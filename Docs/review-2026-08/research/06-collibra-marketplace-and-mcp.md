# Competitor Deep Dive: Collibra Data Marketplace, Data Catalog, Integrations/APIs, MCP Server, Data Governance (2026-08)

> **Document Status**: Authoritative Feature-Requirement Source
> **Target Pages**: [Data Marketplace](https://www.collibra.com/products/data-marketplace), [Data Catalog](https://www.collibra.com/products/data-catalog), [Integrations & APIs](https://www.collibra.com/products/integrations-apis), [MCP Server](https://www.collibra.com/products/mcp-server), [Data Governance](https://www.collibra.com/products/data-governance)
> **Reviewed**: 2026-08-29
> **Feeds**: `20-modules/19-context-products-and-mcp.md` (§15.2 CP-*, MCP-2/MCP-3), `60-delivery/02-epic-backlog.md` (EE.10), `60-delivery/00-status.md`

Second review pass at the user's request, going deeper than `08-collibra-lineage-and-platform-analysis-2026-08.md`. Most of what these five pages show was **already anticipated** by the CP-1 through CP-14 requirements in `20-modules/19-context-products-and-mcp.md` §15.2 (written from an earlier pass over Collibra's platform material). This document records what's genuinely new, and updates status for the one area built further this session: MCP lineage tools.

---

## 1. Data Marketplace — concrete capabilities

Search/filter by domain, owner, system, classification, custom attributes; **Collibra AI Copilot** for natural-language discovery; role-based recommendations; quality scores, certifications, and lineage shown per product for trust assessment; **ratings, reviews, comments, and collaborative collections** (community features); self-service request ("shop") with **automated approval routing**, including handoff to **Jira and ServiceNow** for fulfillment; a **Data Notebook** for query/visualize/document/collaborate once access is granted; full audit trail of who requested what, why, when, for how long; admin-curated marketplace content by asset type/status/organization. Seven named personas, explicitly including **autonomous AI agents**.

**Mapping**: covered in principle by CP-4 ("Data marketplace and access requests" — Missing). New detail CP-4 didn't have: community features (ratings/reviews/collections) and ITSM-fulfillment handoff (Jira/ServiceNow) are now folded into its acceptance criteria. **Data Notebook is not a new gap** — our Governed AI Analyst workspace already is a post-access, governed query/analysis surface; the real gap is scoping that workspace's visible assets to a marketplace grant, which is inseparable from CP-4 itself (there's no marketplace to scope from yet).

## 2. Data Catalog — concrete capabilities

100+ native integrations; automated discovery, classification (including PII/PHI), and description generation; profiling and sampling statistics; embedded semantic layer tying technical assets to glossary/policy; quality metrics surfaced from Data Quality & Observability; certification; **data contracts** (technical spec + quality guarantees) and **data sharing agreements** (usage/access/compliance terms — distinct from a contract); data product creation and publishing to the Marketplace; AI Copilot for NL search; interactive lineage-style diagrams; Data Notebook again.

**Mapping**: automated classification is already implemented in our platform (`MetadataColumn.classification`, semantic inference); 100+ integrations is the pre-existing connector-fleet gap (`00-status.md`); data contracts map to CP-3. **Data sharing agreements are a new, narrower concept** — usage/access/compliance terms bound to a specific consumer grant, not a schema contract — folded into CP-3's acceptance criteria as a sub-type rather than a new ID, since it composes the same registry.

## 3. Integrations & APIs — concrete capabilities

Three integration tiers: Collibra-supported (native), partner (via the **Collibra Marketplace** — an *integration* marketplace, a different thing from the *Data* Marketplace in §1, worth keeping distinct in our own naming), and custom via open APIs. Java APIs and REST APIs; no GraphQL, webhooks, or SDK mentioned on this page. Named integrations: Databricks, AWS, GCP, Microsoft, SAP, Snowflake, Tableau, S3. No concrete connector count published on this page itself (the 100+ figure is from the Catalog page).

**Mapping**: fully covered by the pre-existing connector-fleet and CP-13 gaps; nothing new. A Java SDK is not worth matching — REST is the right surface for our stack.

## 4. MCP Server — concrete capabilities (the most actionable page)

Supports Claude (claude.ai, Desktop, Code), ChatGPT/OpenAI API, Databricks, Snowflake Cortex, Microsoft Copilot, GitHub Copilot, Cursor, VS Code, and any MCP-compliant client. **25+ tools**, split into:

- **Read**: asset discovery/search, glossary term retrieval, upstream/downstream lineage, technical/business lineage graph exploration, semantic context for tables/columns, data contract retrieval, classification search.
- **Write**: asset creation/editing, catalog entry enrichment, data contract manifest push/pull, classification match add/remove, glossary term proposals.

Governance: "every action uses Collibra's existing permission model" — permissions cascade from the parent instance into MCP, exactly the principle our `mcp_server.py` already follows for governed SQL tools and now for lineage tools. OAuth 2.0 app registration; endpoint pattern `https://[instance].collibra.com/rest/mcp`. The stated differentiator from a plain API: **fuzzy name matching and concept mapping**, so a client can ask for "the customers table" rather than construct an exact identifier.

### What we built this session against this page

`mcp_server.py` gained two native MCP tools reusing the unified-lineage graph from EA.14 (not GovernedToolVersion-backed, so they're always available to any role in `UNIFIED_LINEAGE_READER_ROLES`, same as the REST routes):

- `atlas__get_lineage_graph` — the merged FK/suggested/dbt/OpenLineage graph.
- `atlas__get_lineage_impact` — transitive upstream/downstream impact for one node.

Both are read-only, value-free, eligible-tool-gated the same way governed SQL tools are (an ineligible caller gets the same "not found or not published" response, never a distinguishable denial), and unit tested in `tests/test_mcp_server.py` without a database (role gating, argument validation, org-scoping, and success/error payload shape — 7 new tests).

### What's still open

| ID | Gap | Status |
|:--:|---|---|
| CP-6 (partial) | Lineage MCP tools: upstream/downstream and impact done; **fuzzy asset resolution and transformation-detail-as-a-tool remain open** | Partial, was Missing |
| MCP-2 *(new)* | **MCP write operations** — Collibra lets an agent create/edit assets, propose glossary terms, and manage classification matches through MCP, all still gated by the same permission model. We currently expose only reads and governed SQL execution; there is no MCP path to catalog stewardship | Open — P1 |
| MCP-3 *(new)* | **Fuzzy entity resolution for MCP tool arguments** — our new lineage tools and the existing catalog tools all require exact UUIDs; Collibra's "fuzzy name matching and concept mapping" lets a client say "customers" instead | Open — P1 |

## 5. Data Governance — concrete capabilities

Automated real-time policy validation with violation flagging; approval workflows embeddable in **Slack and Microsoft Teams**; role/responsibility-based access accountability; business glossary as shared source of truth; AI-generated table/column/diagram descriptions and auto-classification (stewardship automation); **Data Helpdesk** for tracking and resolving *user-reported* data issues (distinct from our system-generated `DataQualityIncident` rows — this is a ticketing surface for people, not an anomaly detector); usage analytics; domains/communities operating model; named compliance use cases (BCBS 239, Solvency II).

**Mapping**: policy validation, roles, glossary, and AI-assisted description/classification are already implemented in some form on our platform. Slack/Teams-embedded approvals and a user-facing Data Helpdesk are genuinely new detail, folded into CP-11 ("Workflow automation") rather than new IDs — they're instances of "a workflow template with a chat-surface delivery channel," which is what CP-11 already specifies.

## 6. Net new work opened by this review

Only two genuinely new items came out of five pages, because the earlier CP-1..14 pass already anticipated the rest: **MCP-2** (write operations through MCP) and **MCP-3** (fuzzy entity resolution). Both are recorded in `20-modules/19-context-products-and-mcp.md` §14 open work and referenced from `60-delivery/00-status.md`.

## Related documents

- `review-2026-08/research/05-collibra-lineage-and-platform.md` — the prior review this extends
- `20-modules/19-context-products-and-mcp.md` — CP-6, MCP-2, MCP-3
- `60-delivery/02-epic-backlog.md` — EE.10 acceptance detail
- `90-reference/03-sources.md` — source URLs
