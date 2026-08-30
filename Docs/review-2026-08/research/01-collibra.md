Status: research input for the Aug-2026 architecture review. Vendor claims are cited, not endorsed.

# Collibra Competitive Teardown — Enterprise AI Control Plane (August 2026)

Deep teardown of Collibra for a team designing a competing bank-internal governed AI data-analyst / metadata-intelligence platform. Primary sources prioritized (productresources.collibra.com, developer.collibra.com, marketplace.collibra.com, official blog); secondary sources (G2, AWS Marketplace reviews, Murdio, Atlan, checkthat.ai) flagged inline as such.

## Verdict in five lines

- **Genuinely good at:** a rigorous, inheritable metamodel (community/domain/asset/responsibility) that doubles as an ownership+permission substrate; a regulator-credible lineage narrative for BCBS 239/DORA-style reporting *when the source is on its supported-parser list*; and — new in 2025-26 — an actual open-source MCP server (`chip`) with ~29 scoped read/write tools, the most concrete "governed AI agent substrate" shipped by any incumbent catalog vendor to date.
- **Genuinely bad at:** lineage coverage collapses outside ~30 named JDBC/ETL/BI integrations (mainframe, stored-procedure-heavy legacy SQL, Power BI DirectQuery all fall to manual/custom); UI/navigation and steward curation burden are the most repeated complaint across G2 and AWS Marketplace reviews; TCO is brutal (operational staffing estimated at ~6x annual license cost, full enterprise rollout 12-18 months); most of the "AI Control Plane" 2025-26 surface (AI Copilot, agent trust dashboards) is Preview, not GA.
- **Worth copying (mechanism, not brand):** the strict, inheritable metamodel as a permission+ownership substrate; the MCP tool taxonomy (discovery → lineage-traversal → classification → contract retrieval → scoped writeback) as the shape of our own agent tool surface; scorecards surfaced inline at the point of consumption (marketplace listing), not in a separate dashboard.
- **Worth refusing:** the everything-is-one-graph bloat that requires a CDO-level program to configure before day-one value; per-connector/per-module metered packaging; the shelfware failure mode caused by shipping governance-as-compliance-checkbox instead of an immediate self-service win.
- **Bottom line:** Collibra is the metadata/lineage/governance system of record to benchmark against, not to license — its data model and MCP tool shape are worth studying closely; its packaging, UI, and curation-first workflow are exactly what a bank-internal build should avoid repeating.

---

## 1. Platform Shape — Positioning, Editions, Bundling

2026 positioning has shifted from "data governance platform" to **"The Enterprise AI Control Plane"** — homepage tagline: "context and control for every agent you put in production" ([collibra.com](https://www.collibra.com/)).

**Product line** (marketed as one platform, sold as separable modules): AI Command Center ([collibra.com/products/ai-command-center](https://www.collibra.com/products/ai-command-center)), Data Catalog ([collibra.com/products/data-catalog](https://www.collibra.com/products/data-catalog)), Data Governance, Data Lineage, Data Quality & Observability, Data Marketplace, Data Access (formerly "Protect"), Data Privacy, Core Services incl. Data Notebook. Platform page ([collibra.com/products/collibra-platform](https://www.collibra.com/products/collibra-platform)) avoids publishing discrete SKU boundaries; in practice modules and connectors are separate line items (§14).

Recent inorganic move: acquisition of **Deasy Labs** (Oct 2025) to extend governance to unstructured data / AI-ready context chunks for RAG ([press release](https://www.collibra.com/company/newsroom/press-releases/collibra-acquires-deasy-labs), [TechTarget](https://www.techtarget.com/searchdatamanagement/news/366627998/Collibras-acquisition-of-Deasy-targets-unstructured-data)).

Third-party validation: named **Leader**, first-ever Gartner MQ for Data and Analytics Governance Platforms, repeated second cycle ([press release](https://www.collibra.com/company/newsroom/press-releases/collibra-named-a-leader-in-gartner-magic-quadrant-for-data-and-analytics-governance), [2025 MQ](https://www.collibra.com/resources/2025-gartner-magic-quadrant-data-analytics-governance-platforms)) — note MQ "Leader" doesn't score implementation burden, which is where complaints concentrate.

**Take / Leave:** Leave the "single sprawling platform, priced module-by-module" packaging model — it's the direct cause of the cost complaints in §14. Take the positioning discipline: naming AI Command Center as a distinct governance surface for agents (not bolting AI features onto the catalog UI) is a clean separation we should mirror.

---

## 2. Metamodel / Operating Model

**Hierarchy** ([co_om-basics.htm](https://productresources.collibra.com/docs/collibra/latest/Content/co_om-basics.htm), [to_operating-model.htm](https://productresources.collibra.com/docs/collibra/latest/Content/to_operating-model.htm)):

- **Communities** — top-level org containers, typically one per division/LOB, can nest sub-communities and domains.
- **Domains** — logical groupings of one asset-type family within exactly one community, uniquely named per community.
- **Assets** — atomic governed object, belongs to exactly one domain.
- **Asset types** — templates; five OOTB parents: **Business Asset, Data Asset, Governance Asset, Issue, Technology Asset** ([ref_ootb-asset-types.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Assets/AssetTypes/ref_ootb-asset-types.htm)).
- **Attribute types / Attributes** — metadata fields on assets.
- **Relation types / Relations** — bidirectional links between asset types (e.g., `Data Element sources/targets Data Element` underlies Business Lineage, §5).
- **Complex relations** — "objectified" many-to-many associations carrying their own attributes ([co_complex-relations.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Assets/Characteristics/ComplexRelations/co_complex-relations.htm)).
- **Scopes** — bind domain/attribute/relation-type configuration to specific asset types without touching global config.

**OOTB asset hierarchy highlights:** *Business Asset* → Business Term (Acronym, Critical Data Element subtypes), Business Dimension (**Business Process, Line of Business, Data Category, Data Domain**), Measure/KPI, Report (vendor BI-Report subtypes: Looker/Power BI/Tableau/etc.), **Data Product**, Business Qualifier. *Data Asset* → Data Element (Column, Field, Data Attribute), Data Structure (Data Entity, Data Model), Data Set, Code Set, Data Quality Job, Data Product Ports. Plus extensive vendor-specific BI hierarchies (Looker, MicroStrategy, Power BI, Tableau, SAC, Sigma, ThoughtSpot, SSRS).

**Mapping a bank's LOB/sub-LOB:** "Line of Business" and "Data Domain" exist as **Business Dimension** asset subtypes — separate from the Community/Domain *container* hierarchy used for permissioning. Banks routinely conflate "Community = LOB" (org/permission construct) with "Line of Business asset" (governance taxonomy construct); this is called out as a root cause of stalled rollouts ([Murdio banking piece](https://murdio.com/insights/collibra-implementation-banking/)).

**2026 — "Guided Stewardship" operating model.** A new OOTB alternate model layered on the classic primitives, three data layers ([to_guided-stewardship.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Catalog/GuidedStewardship/to_guided-stewardship.htm), [OperatingModel/to_catalog-om.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Catalog/GuidedStewardship/OperatingModel/to_catalog-om.htm)):
- **Physical** — Database/Schema/Table/Column, mostly auto-created via Catalog registration.
- **Semantic** — System/Data Model/Data Entity/Data Attribute, tree-shaped, system-specific.
- **Conceptual** — Line of Business/Data Domain/Data Concept, enterprise-wide, **many-to-many** so one concept maps to multiple systems.

Two AI assists: **Semantic Assistant** (suggests column descriptions, maps physical columns → semantic attributes, replaces the old "Physical Data Connector" feature) and **Semantic Model Editor** (AI-generated structure suggestions). New: automatic stitching between Columns and Data Categories from classification output.

**(e) Limitations:** G2 — "steep learning curve—especially when...setting up hierarchies, and configuring lineage diagrams" ([G2 pros/cons](https://www.g2.com/products/collibra/reviews?qs=pros-and-cons)). Murdio: without a CDO/Solution Architect owning operating-model design *before* configuration, the platform becomes shelfware.

**Take / Leave:** Take the core primitive set (typed container hierarchy + typed relations + inheritable responsibility roles) — it is a sound, reusable pattern for our own metadata substrate and maps cleanly onto graph-native storage. Leave the five-parent-type/hundreds-of-subtype OOTB sprawl — it is over-general for a single bank's need and is exactly what drives the "steep learning curve" complaints; we should ship a narrow, opinionated schema (LOB, system, table, column, term, policy, model, agent) rather than a generic metamodel a bank has to sculpt for 12-18 months.

---

## 3. Data Catalog

Central inventory built by registering data sources via the Integrations page (JDBC/S3/GCS/ADLS registration tab; separate metadata/ETL/BI/AI integration-configuration tab) ([to_register-integrate.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Catalog/to_register-integrate.htm)). Ingestion runs through **Edge** (§12) via **Catalog JDBC connectors** ([ref_catalog-connector-overview.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Edge/JDBCConnections/ref_catalog-connector-overview.htm)) or native integrations for Snowflake, Databricks Unity Catalog, Microsoft Fabric, SAP Datasphere, S3/GCS/ADLS, SAP Signavio, SageMaker Unified Studio, and AI-model registries (Anthropic, AWS Bedrock, Azure ML, Gemini, MLflow, Snowflake Cortex). Scheduled sync after initial connection.

**Classification/PII:** "Unified Data Classification" assigns **data classes** (e.g., "phone number") to Column assets; a Guided Stewardship tool then auto-relates the classified Column to a **Data Category** asset (the PII/PHI grouping used by Protect, §8) ([co_about-data-classification.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Catalog/DataClassification/co_about-data-classification.htm)). **Detection method (regex vs ML) is not disclosed** in fetched docs — direct vendor question for any evaluation.

**Search/ranking:** relevance = spelling similarity + term frequency in field + density (occurrence % relative to field length, favoring short precise fields) ([co_relevance.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Search/co_relevance.htm)); tunable via Search boost settings, index customization, plus a separate **Recommenders** feature on catalog pages.

**Data Marketplace / shopping-cart flow** (rebuilt 2025-26, [blog](https://www.collibra.com/blog/meet-the-new-collibra-data-marketplace-a-curated-shopping-experience-for-data-products), [product page](https://www.collibra.com/products/data-marketplace)): browse/search with trust indicators (quality score, certification, lineage) → request → workflow routes to steward/owner (optionally to Jira/ServiceNow) → on approval, query access via **Collibra Data Notebook** ([collibra.com/products/core-services/data-notebook](https://www.collibra.com/products/core-services/data-notebook)).

**(c) Business goal:** BCBS 239 blog claims 34% reduction in time on data-quality issue resolution, 57% reduction in audit response time ([blog](https://www.collibra.com/blog/four-ways-data-lineage-powers-bcbs-239-compliance)) — vendor-reported, unverified.

**(e) Limitations:** Murdio guide flags heavy professional-services dependency for classification/enrichment ([murdio.com/insights/collibra-data-catalog](https://murdio.com/insights/collibra-data-catalog/)); G2 — "navigation challenges and unclear language," "very technical and not intuitive User Interface" for non-steward personas.

**Take / Leave:** Take the shopping-cart request flow with inline trust indicators and the direct hop into a query notebook on approval — this is the correct UX shape for self-service in a bank. Leave the split "asset page vs marketplace listing vs notebook" three-surface fragmentation reviewers complain about; collapse discovery, trust signal, and query into one screen for our build.

---

## 4. Business Glossary & Stewardship

Business terms are a Business Asset subtype (§2); term-to-asset linking uses relation types. Approval runs through the generic **workflow engine** (§6) — canonical OOTB flow is "**Simple Approval**" ([developer.collibra.com/.../simple-approval-configuration](https://developer.collibra.com/workflows/out-of-the-box-workflows-walk-throughs/simple-approval/simple-approval-configuration)). Stewardship roles use **Responsibilities** (§13) — resource-role assignments (Business Steward, Owner, Community Manager, Data Steward) that **inherit down the hierarchy**: e.g. a Community Manager on "Enterprise" is automatically Community Manager on every child domain/asset unless overridden ([Responsibilities doc](https://productresources.collibra.com/docs/collibra/2023.01/Content/Responsibilities/co_responsibilities.htm)) — Collibra's closest analogue to a RACI matrix, rendered per-resource rather than as one enterprise-wide view.

**Take / Leave:** Take inheritable responsibility roles wholesale — it is the right pattern for scaling ownership assignment across thousands of assets without manual per-object grants. Leave the lack of a single enterprise-wide RACI view; build a rollup screen from day one instead of leaving it per-resource.

---

## 5. Lineage

**Cloud-only product** ([co_collibra-data-lineage.htm](https://productresources.collibra.com/docs/collibra/latest/Content/CollibraDataLineage/co_collibra-data-lineage.htm)) — not shipped with on-prem/self-hosted core, relevant if evaluating CPSH.

**Two types:** **Technical lineage** — table/column-level cross-system, for engineers; stitched nodes render yellow, unstitched gray (a visible coverage-gap signal worth copying). **Business lineage** — auto-generated purely from the `Data Element sources/targets Data Element` relation between already-cataloged assets, no separate manual step.

**Creation:** via **Edge** (current) or the legacy CLI **Lineage Harvester**, which **reached end-of-life 31 July 2026**.

**Supported sources** ([ref_technical-lineage-supported-data-sources.htm](https://productresources.collibra.com/docs/collibra/latest/Content/CollibraDataLineage/ref_technical-lineage-supported-data-sources.htm)):

| Category | Mechanism | Examples |
|---|---|---|
| JDBC/SQL DBs | Native SQL parsing | Redshift, Azure SQL/Synapse, BigQuery (views/SQL only), Greenplum, Hive, Db2 (no stored procs), Oracle, PostgreSQL, SQL Server, MySQL (no stored procs), Netezza, SAP HANA, Snowflake, Spark SQL, Sybase, Teradata |
| Cloud-native | System tables/vendor API | Databricks Unity Catalog (`system.access.column_lineage`, needs `system.query.history` for SQL detail), GCP (Data Lineage API) |
| ETL — OpenLineage | Fluentd/OpenLineage events | Airflow 2.7+, AWS Glue (via OpenLineage Spark) |
| ETL — native API | API pull | Azure Data Factory, dbt Cloud, Fivetran, Informatica IICS, Matillion |
| ETL — file parsing | Static export parsing | IBM DataStage, Informatica PowerCenter (SQL overrides only), SSIS, dbt Core |
| BI tools | API-based | Looker, MicroStrategy, **Power BI (DirectQuery/live connections NOT supported)**, Qlik (metadata only, no technical lineage yet), Sigma, SSRS-PBRS, Tableau, ThoughtSpot |
| Everything else | Manual/custom | Mainframe/COBOL, most legacy on-prem ETL, unlisted sources |

**This is the sharpest limitation for a bank:** mainframe, stored-procedure-heavy MySQL/Db2/Netezza/Spark SQL logic, and Power BI DirectQuery all fall outside automatic parsing. Real-user confirmation: an AWS Marketplace reviewer states "technical lineage views and diagram views are very confusing" from node complexity, and flags confusion between business vs technical lineage among end users ([AWS review](https://aws.amazon.com/marketplace/reviews/reviews-list/prodview-6gqg3yc2e2rsu/review/9a06055f-e5ad-371f-a8ad-182482a2f548)).

**(c) Bank framing:** marketed explicitly against **BCBS 239** — tracing risk exposures to source, validating accuracy, automated reconciliation, supervisory-review evidence ([blog](https://www.collibra.com/blog/four-ways-data-lineage-powers-bcbs-239-compliance)); also **CCAR, IFRS-17, FRTB, SOX, GLBA, GDPR/CCPA** ([financial-services page](https://www.collibra.com/use-cases/industry/financial-services)).

**Take / Leave:** Take the yellow/gray stitched-vs-unstitched visual signal and the "business lineage is a free derived view of technical lineage" design — cheap and high-value. Leave any assumption that SQL-parsing lineage will "just work" across a bank's real estate; budget explicitly for the mainframe/stored-procedure/DirectQuery gap with a custom extraction layer from day one rather than discovering it mid-program.


---

## 6. Workflow Engine

Built on **BPMN 2.0**, executed by **Flowable** (migrated from the older Activiti engine — [Clever Republic write-up](https://www.cleverrepublic.com/resources/blog/a-new-bpmn-engine-in-collibra-activiti-to-flowable/)). **Workflow Designer** UI models approval/governance/notification flows as BPMN diagrams, with **Groovy** script tasks attached for logic ([developer.collibra.com/workflows](https://developer.collibra.com/workflows)); script tasks get pre-instantiated Java-API handles (`assetApi`, `attributeApi`, etc.). OOTB workflows include **Simple Approval** ([walkthrough](https://developer.collibra.com/workflows/out-of-the-box-workflows-walk-throughs/simple-approval/simple-approval-configuration)), Assessments Approval (used by AI Governance, §9), and issue-management flows. Additional workflows distributed via Marketplace ("Data Product Workflows," "Guided Stewardship Workflow + Sample content").

**Take / Leave:** Take BPMN-as-the-workflow-substrate — a standards-based engine with visual diagrams is the right choice for auditable approval chains a bank examiner can read. Leave Groovy-as-the-only-extension-language; a modern build should let stewards/engineers extend workflows in the same language as the rest of the platform (e.g., TypeScript/Python) rather than requiring a JVM scripting skill most data teams don't have.

---

## 7. Data Quality & Observability (Collibra DQ, ex-OwlDQ)

Runs as containerized microservices (Kubernetes) — pushed down to customer warehouse compute or executed via a dedicated Spark engine, avoiding forced data movement ([product page](https://www.collibra.com/products/data-quality-and-observability)).

**Adaptive rule discovery:** AI-assisted rule authoring, reusable SQL rule templates, claimed coverage expansion "in days, not months" ([demo](https://www.collibra.com/resources/adaptive-and-custom-rules)).

**Anomaly detection & scorecards:** automated + custom monitors; on trigger, lineage traces root cause upstream and impacted products downstream. **Quality scorecards** surface directly on Data Marketplace listings (trust score visible while shopping) and roll up to executive dashboards; scores link to policies and Critical Data Elements for audit evidence.

**Automation vs manual:** alerts route to owners; remediation tracked natively or handed to Jira/ServiceNow. DQ score is structurally attached to the catalog asset page — the concrete catalog-linkage mechanism. No GA/preview breakdown found in fetched docs for the AI-authoring claims — verify directly.

**Take / Leave:** Take "quality score inline at the point of consumption" (marketplace listing, not a separate DQ dashboard) — directly reduces the tool-switching friction reviewers complain about elsewhere. Leave the OwlDQ-lineage (a bolted-on acquisition still visibly a separate execution engine/Spark cluster) as the architecture model; build quality checks as a native extension of the same lineage/metadata graph rather than a parallel compute product wired in after the fact.

---

## 8. Protect / Data Access / Privacy

Rebranded toward **"Data Access"** in nav ([collibra.com/products/protect](https://www.collibra.com/products/protect)) but docs still say "Collibra Protect" ([to_collibra-protect.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Protect/to_collibra-protect.htm)).

**Policy model:** **Data Protection Standards** — column-based masking tied to data category/classification, uniform across query/API/browse, targeted at user groups. **Data Access Rules** — take precedence over standards; support access restriction, masking, or **row filtering**, enabling differential access per group.

**Enforcement:** masking, redaction, hashing, row filtering. **Not real-time interception** — system "periodically synchronizes" policy state with target platforms and pushes enforcement via JDBC/REST, translating policy into platform-native controls.

**Supported platforms:** AWS Lake Formation, BigQuery, **Databricks** (GA per [Q4 2024 release note](https://www.collibra.com/blog/q4-2024-release-protect-for-databricks-is-now-ga)), **Snowflake**; custom via API elsewhere. **"Data source policies (in preview)"** extends scope further ([co_data-source-policies.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Protect/co_data-source-policies.htm)) — explicitly Preview.

**Gap:** fetched documentation shows **no consent-management mechanism** — this is an access/masking control plane, not consent orchestration.

**Take / Leave:** Take the two-tier policy model (baseline Standards + overriding Rules with row filtering) — clean and auditable. Leave periodic-sync enforcement as the target architecture; for a bank, real-time policy evaluation at query time (not eventual-consistency push) is a hard requirement our design should meet natively rather than retrofit.

---

## 9. AI Governance Module

Distinct asset types/workflows within AI Command Center ([co_about-ai-governance.htm](https://productresources.collibra.com/docs/collibra/latest/Content/AIGovernance/co_about-ai-governance.htm)).

**Tracked entities:** AI Use Case (Business Asset domain), AI Model/AI Model Version/AI Base Model/AI Agent (Technology Asset domain), AI Project, Assessment Review. **OOTB domains:** "AI Models and Agents" (Technology Asset), "AI Use Cases" (Business Asset).

**Screens:** Model Registry / Agent Registry pages; AI asset detail page; lifecycle tracker (assessment templates + sign-off status); Responsibilities tab.

**9 assessment templates:** Business Context, Data and AI Models, Legal and Ethics, Risks and Safeguards, **EU AI Act Assessment**, **NIST AI RMF** (mapped to Govern/Map/Measure/Manage), Model Business Context, Model Information Collection, Model Data Collection, **AIUC-1 Compliance Assessment** for agents (template arriving May 2026).

**Process:** Business Steward registers use case with minimal fields → stakeholders complete assessments → assessments auto-create relations linking use case ↔ model(s) ↔ agent(s) → Assessment Owner submits → **Assessments Approval** workflow (§6) → Business Steward approves/rejects → approved answers populate asset page.

**Automated traceability:** ingesting from a connected AI platform (Anthropic, AWS Bedrock/SageMaker, Azure AI Foundry/ML, Databricks, Google Vertex AI, MLflow, SAP AI Core, Snowflake Cortex) **auto-stitches** lineage data → model → business use case. **Azure AI Foundry traceability is explicitly GA**; "Operational Trust for AI Agents" (fleet dashboard, Databricks Agent Bricks) is explicitly **Preview**.

**Take / Leave:** Take the assessment-template-as-structured-intake pattern (auto-linking use case ↔ model ↔ agent on submission) — directly reusable for our own AI use-case/model risk register. Leave the nine-template, multi-framework-simultaneously design as shipped; a bank-internal build should ship one opinionated risk-tiering flow (mapped to our actual regulatory obligations) rather than a generic template library stewards must curate.

---

## 10. AI / Agentic Surface (2025-2026) — GA vs Preview, Precisely

| Capability | Status | Notes |
|---|---|---|
| AI Command Center core (registry, Trust Score, dashboards, compliance templates, CLI registration) | **GA** | [collibra.com/products/ai-command-center](https://www.collibra.com/products/ai-command-center) |
| Automated Traceability for Azure AI Foundry | **GA** | Named explicitly |
| Operational Trust for AI Agents (fleet dashboard, Databricks Agent Bricks) | **Preview** | Explicit label |
| AIUC-1 assessment template | **Coming May 2026** | Not yet shipped |
| **Collibra AI Copilot** | **Preview** | [productresources.collibra.com](https://productresources.collibra.com/docs/collibra/latest/Content/CollibraAI/co_cai-co-pilot.htm); commercial cloud-only, **not available on Government or self-hosted** deployments |
| Data source policies (Protect) | **Preview** | Explicit label |
| Semantic Assistant / Semantic Model Editor | Shipping w/ Guided Stewardship, maturity unclear | |
| MCP server ("chip") | **GA, open-source (Apache-2.0)** | See below |

**Collibra AI Copilot** — chat assistant in the platform menu bar, three configurable agents: **Data and Analytics Discovery** (Reports, Data Notebooks, BI Reports, Data Products, AI Models, Data Sets — respects View permission, needs a description on the asset), **Business Definitions** (Business Terms/Acronyms/KPIs/Measures), **Collibra Documentation** (current-release docs). Plus **Semantic Model Agents** for Guided Stewardship. Configured on an **AI Agents settings page** (per-agent activation, up to 3 example questions/100 chars, content-scope filters; 300-char welcome message) ([co_settings-ai-copilot.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Settings/AIAgents/co_settings-ai-copilot.htm)). **Documented hard limits:** cannot count assets or return full lists, max 100 relations/asset, no column-level retrieval, no asset history/user activity, no status-based filtering. Knowledge base refreshes **daily**; permission/deletion changes apply immediately. Meters usage in **Collibra Units**; underlying LLM provider **undisclosed**.

**Collibra MCP Server ("chip")** — official, open-source, Go, Apache-2.0, [github.com/collibra/chip](https://github.com/collibra/chip). Bridges any MCP client to a live tenant. **~21 read tools** (`discover_business_glossary`, `discover_data_assets`, `get_asset_details`, `get_business_term_data`, `get_column_semantics`, upstream/downstream/transformation lineage queries, keyword/data-class/lineage search, data-contract and semantic-layer retrieval) and **~8 write tools** (asset create/edit with markdown→HTML, classifications/data-class associations, responsibility/steward assignment, data-contract manifest management). Stdio or HTTP transport; auth via server-side credentials file (`~/.config/collibra/mcp.yaml`) or per-client Basic Auth (HTTP mode); permission-scoped (most tools require `dgc.ai-copilot` or classification-specific scopes). Experimental "skills" catalog for multi-step guided workflows. **This is the most directly relevant artifact in the whole teardown** — its tool surface (glossary discovery, lineage traversal, classification, contract retrieval, scoped writeback) is close to what a bank-grade AI data-analyst agent needs. Third-party listings also exist (Databricks-flavored MCP, a "local version" on Marketplace; a consultancy, Assured Consulting Solutions, separately claims an Aug-2025 "industry-first" MCP server for Collibra — clarify provenance directly with Collibra before relying on either).

Official MCP blog frames use cases as (1) company-wide chatbots with "certified sources" resolving KPI/definition ambiguity, (2) automated data-product comparison/dedup ([blog](https://www.collibra.com/blog/enabling-governed-ai-everywhere-with-collibra-model-context-protocol-server)). Security delegated to "the transport layer" for authorization — thin in the marketing copy; the GitHub repo's Basic Auth model is the concrete mechanism.

**Take / Leave:** Take the MCP tool taxonomy almost verbatim as a starting checklist (discovery, definitions, lineage traversal, classification lookup, scoped writeback with a dedicated permission scope) — it's a well-shaped, minimal agent-tool surface for governed data work. Leave the AI Copilot's undisclosed-model, metered-per-query, preview-only, cloud-only design; our agent should be model-transparent, cost-predictable, and available in whatever deployment mode (including on-prem) the bank actually runs.


---

## 11. APIs & Extensibility

- **Core REST API v2** — primary CRUD for assets/communities/domains/types ([developer.collibra.com/api/references/data-governance](https://developer.collibra.com/api/references/data-governance)).
- **Knowledge Graph API (GraphQL, "Data Graph")** — SQL-like filtering, sorting, pagination over assets/communities/domains/types ([developer.collibra.com/api/graphql/knowledge-graph](https://developer.collibra.com/api/graphql/knowledge-graph)); introduced as **beta** ([blog](https://www.collibra.com/blog/knowledge-graph-api-beta-simplifying-data-retrieval-with-graphql)) — confirm current GA status directly.
- **Import/Export APIs**, **Search API**, **Catalog Data Classification REST API v1** ([developer.collibra.com/api/references/catalog-classification](https://developer.collibra.com/api/references/catalog-classification)).
- **Collibra Marketplace** — extension ecosystem: Integrations (custom/harvester/partner connectors, JDBC drivers), Workflows (BPMN packages), Metadata solutions, Admin/Dev tools, UI add-ons, standalone apps, **MCP servers**; organized by solution type / target product / publisher (Commercial / Partner / Community) ([marketplace.collibra.com](https://marketplace.collibra.com/)).
- **Connect/Connector framework** for custom Edge-hosted connectors — the "Connector 2.0" developer page 404'd during this research pass; confirm current path via [developer.collibra.com/sitemap.md](https://developer.collibra.com/sitemap.md).

**Take / Leave:** Take having both a REST CRUD API and a GraphQL graph-query API as complementary surfaces (transactional writes vs. flexible relationship queries) — directly applicable to our own metadata service split. Leave the marketplace-as-primary-extensibility-model; a bank-internal platform doesn't need a public plugin economy, it needs a small, well-tested internal connector SDK.

---

## 12. Architecture

**SaaS-first:** "distributed microservices architecture" on **AWS and GCP**, Docker/Kubernetes for components like DQ&O. Metadata store is called the **"Collibra Metadata Graph"** — centralized graph repository underlying lineage/relationship mapping, exposed via the GraphQL Knowledge Graph API. **Underlying storage engine is not disclosed** in current docs (historically the community understood earlier architecture generations to combine a graph store — Titan/JanusGraph-era — with Elasticsearch for search indexing; treat as unconfirmed for the 2026 stack, not current fact).

**Edge** runs *inside the customer network* ([co_about-edge.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Edge/co_about-edge.htm)): "a cluster of Linux servers for accessing and processing data close to where it resides." Three parts: (1) configuration UI in Collibra Platform, (2) an "integration capability repository" of downloadable capability packages (JDBC drivers, ETL/BI connectors, lineage agents), (3) the Edge site itself, deployed via Kubernetes/Edge CLI near the data source. Two deployment models: **Commercial/SaaS** (Edge on customer infra, Platform in Collibra's cloud) and **Self-Hosted (CPSH)** (customer runs both). Vault/credential management and storage-connection config stay local to Edge — metadata processing, and crucially raw data during profiling/sampling, never has to leave the customer network. This is the core architectural sell for regulated banks. **Edge capabilities** are versioned, individually toggled add-ons per site.

Note: **Collibra Data Lineage is explicitly cloud-only** — a CPSH deployment needs explicit confirmation of which lineage capability remains available.

**Take / Leave:** Take the Edge pattern outright — split "control plane in the cloud/central, execution plane inside the customer network near the data" is the correct architecture for a bank that will never let raw data leave its perimeter, and directly informs how our own agent's data-access layer should be deployed (agent reasoning centrally, tool execution/data touch inside the bank's network boundary). Leave the SaaS-only lineage service as a hard architectural constraint for us; lineage generation should run wherever the data runs, not require a cloud-only product tier.

---

## 13. Roles, Permissions, Tenancy

Three-layer model ([Settings/RolesAndPermissions docs](https://productresources.collibra.com/docs/collibra/latest/Content/Settings/RolesAndPermissions/Roles/GlobalRoles/to_global-roles.htm)):

- **Global roles** — bundle global permissions, gate *which applications* a user can open (Sysadmin → Settings; Catalog Author → Data Catalog; DataSteward → Stewardship app; Data Quality Admin → DQ&O; Glossary → glossary functionality; Policy Manager → governance assets; Assessments Admin → assessments/templates). Not resource-scoped.
- **Resource roles** — assigned per community/domain/asset via **Responsibilities** (§4); scopes *what a user can do on that specific resource* (Business Steward, Owner, Community Manager...), inherited down the containment hierarchy.
- **Responsibilities** — the join table between users/groups and resource roles on a resource; assign once at Community level, inherit to every child, override locally.

AI Governance layers its own role/permission extension on the same mechanics ([co_ai-gov-global-roles-permissions.htm](https://productresources.collibra.com/docs/collibra/latest/Content/AIGovernance/co_ai-gov-global-roles-permissions.htm)).

For a large bank, the practical tenancy model is one Platform tenant, LOB/division-level Communities for org isolation, Edge sites per network zone/region, and global+resource role combinations for standard steward/owner/consumer tiers — a heavily manual, admin-configured model with no lighter-weight self-service tenancy primitive documented.

**Take / Leave:** Take the global-role (app-gating) + resource-role (object-scoped, inheritable) split — it cleanly separates "can you open this product" from "what can you do on this specific asset," which is the right shape for our agent's permission checks too (tool-level gating plus per-object ACL). Leave the fully manual, admin-configured-only tenancy model; ours should support programmatic/API-driven role assignment at onboarding scale rather than requiring UI-driven admin work for every new team.

---

## 14. Pricing / Packaging Signals and Implementation Complaints

**No public price list.** Figures below are third-party/estimate sources (checkthat.ai, Atlan, PeerSpot/G2/Vendr) — **directional, not contractual**.

- Base tiers (annual): **Standard ~$122,600** (20 creator users), **Premier ~$192,800** (20 users, unlimited metadata connectors), **Ultimate ~$294,900** (30 users) — [checkthat.ai](https://checkthat.ai/brands/collibra/pricing).
- Add-ons: Metadata Connector ~$10,400/ea, Technical Lineage Connector ~$10,400/ea, BI/ETL Integration ~$26,000/ea, ERP Integration ~$38,500/ea, **Data Quality module ~$156,100**, **AI Governance module ~$104,000**, Privacy module ~$28,900, Premium/Signature support ~$95,250/yr, non-prod environment ~$20,700/yr each.
- Real deployments: mid-market Standard (50 users) ≈ **$176,054 Year 1** (incl. 35% implementation services); Fortune-500 Ultimate (200+ users) ≈ **$2,040,788 Year 1** (incl. 45% implementation services).
- Comparables: Vendr median annual spend **$197,142** (range $170,650–$223,202); AWS Marketplace list points $170k/12mo, $340k/24mo, $510k/36mo.
- Services rates: ~$500/hr senior consultant, ~$250/hr cloud engineer, ~$160/hr data analyst.
- Repeated rule of thumb: **operational staffing to run Collibra can reach ~6x the annual license cost** (Atlan analysis — bias-flagged competitor source, but the multiplier also appears independently in checkthat.ai's benchmark).
- Negotiation color (PeerSpot/G2): enterprise procurement found Collibra "did not negotiate at all... the very first price they quoted, they almost always stuck to [the] same."

**Implementation timelines (banking-specific):** MVP **4–6 months**, full enterprise rollout **12–18 months**; recommend spending the first 30 days identifying the single most urgent regulatory pain point, then a 30–90 day pilot proving lineage on one critical report ([Murdio](https://murdio.com/insights/collibra-implementation-banking/)). Atlan separately cites a **25-month average time-to-ROI** sourced from G2 reviews.

**Recurring complaints (G2, n=102 reviews; AWS Marketplace):**
- Implementation: "setup, bugs, and inconsistent API stability" (7 reviews); "setup, configuration, and user engagement can be overwhelming" (6 reviews).
- Cost: "high licensing fees and challenges in adoption impacting user experience" (5 reviews); explicit barrier for smaller orgs.
- UI/navigation: "navigation challenges and unclear language" (5 reviews); "the biggest barrier...is navigation... not intuitive enough"; "Very technical and not intuitive User Interface."
- Learning curve: steep, "especially when...setting up hierarchies, and configuring lineage diagrams."
- Support/reliability: one reviewer needs premium support "every 2-4 weeks" for job failures; repeated API inconsistency complaints.
- Lineage UX: "technical lineage views and diagram views are very confusing," complex node structures, business-vs-technical lineage confusion among non-technical users.
- Adoption failure (Murdio): incomplete/unusable metadata (no descriptions/owners/quality signals), governance framed as compliance checkbox not a real problem-solver, generic/forgettable training, change fatigue from reorg/tool-priority shifts. Success factor: **visible quick wins within 30-60 days**; otherwise shelfware, especially absent a CDO/Solution Architect owning the operating model.

**Take / Leave:** Take nothing from the packaging model — per-connector, per-module metering is precisely the cost structure a bank-internal build should avoid, since we control both product and infrastructure cost directly. Leave the 12-18 month "operating-model-first" rollout sequencing as a *default*; design our platform so a single team gets a working, narrow slice (one LOB, one source system, one agent capability) live in weeks, then expand — the opposite of Collibra's program-first pattern.

---

## What to steal

- **Inheritable, resource-scoped responsibility model** — assign ownership once at a container level, inherit down, override locally. Cleanly solves ownership-at-scale without per-object admin work (§4, §13).
- **MCP tool taxonomy** — discovery → definitions → lineage traversal → classification lookup → scoped writeback, each behind its own permission scope. Near-directly reusable as the initial tool list for our own agent (§10).
- **Stitched vs. unstitched visual signal in lineage** (yellow/gray nodes) — cheap, high-value way to show coverage gaps instead of pretending lineage is complete (§5).
- **Edge-style split architecture** — control plane centralized, connector/data-touching execution runs inside the bank's network boundary, credentials/vault stay local (§12).
- **Trust/quality signal inline at the point of consumption** (marketplace listing, not a separate dashboard) — reduces tool-switching (§3, §7).
- **Structured assessment-template intake with auto-linking** (use case ↔ model ↔ agent on submission) for an AI/model risk register (§9).
- **Global-role (app access) + resource-role (object-scoped) split** as the permission model shape, including for our agent's own tool-gating logic (§13).

## What to refuse

- **Generic five-parent-type metamodel a bank must sculpt for a year** — ship a narrow, opinionated schema instead (§2).
- **Per-connector/per-module metered pricing** — misaligns incentives and is the direct driver of the ~6x operational-cost multiplier reported by users (§14).
- **Cloud-only lineage / AI Copilot tiers that exclude self-hosted deployments** — a regulated bank needs deployment-mode parity from day one, not a "coming to self-hosted eventually" roadmap item (§5, §10).
- **Program-first, CDO-led 12-18 month rollout sequencing** — leads directly to the shelfware failure mode; sequence for a working narrow slice in weeks (§14).
- **Undisclosed-model, metered-per-query AI assistant** with hard caps (no counting, no full lists, 100-relation limit) — our agent needs to be model-transparent and not silently truncate governance-critical answers (§10).
- **Periodic-sync policy enforcement** for access/masking — real-time evaluation at query time is a hard requirement we should meet natively, not retrofit (§8).
- **Groovy-only workflow extensibility** — locks governance logic behind a JVM scripting skill most data/platform teams don't carry (§6).

## Screenshot targets (not yet captured)

1. [collibra.com/products/ai-command-center](https://www.collibra.com/products/ai-command-center) — AI Command Center hero: registry, trust score, dashboard mockups.
2. [collibra.com/products/collibra-platform](https://www.collibra.com/products/collibra-platform) — overall platform module diagram.
3. [collibra.com/products/data-catalog](https://www.collibra.com/products/data-catalog) — Data Catalog search/asset-page mockups.
4. [collibra.com/products/data-lineage](https://www.collibra.com/products/data-lineage) — lineage diagram marketing screenshots.
5. [productresources.collibra.com/.../ref_technical-lineage-viewer.htm](https://productresources.collibra.com/docs/collibra/latest/Content/CollibraDataLineage/TechnicalLineage/ref_technical-lineage-viewer.htm) — technical lineage viewer UI docs (yellow/gray node states).
6. [collibra.com/products/data-marketplace](https://www.collibra.com/products/data-marketplace) — shopping-cart/data-product marketplace UI.
7. [collibra.com/blog/meet-the-new-collibra-data-marketplace...](https://www.collibra.com/blog/meet-the-new-collibra-data-marketplace-a-curated-shopping-experience-for-data-products) — new marketplace UI walkthrough screenshots.
8. [collibra.com/products/data-quality-and-observability](https://www.collibra.com/products/data-quality-and-observability) — DQ scorecards/monitors UI.
9. [collibra.com/resources/adaptive-and-custom-rules](https://www.collibra.com/resources/adaptive-and-custom-rules) — DQ rule-authoring demo screens.
10. [collibra.com/products/protect](https://www.collibra.com/products/protect) — Data Access/Protect policy UI.
11. [collibra.com/resources/collibra-protect-factsheet](https://www.collibra.com/resources/collibra-protect-factsheet) — Protect no-code policy screens.
12. [collibra.com/products/core-services/data-notebook](https://www.collibra.com/products/core-services/data-notebook) — Data Notebook query/collab UI.
13. [collibra.com/resources/collibra-data-notebook-feature-overview](https://www.collibra.com/resources/collibra-data-notebook-feature-overview) — Data Notebook feature demo.
14. [productresources.collibra.com/.../co_cai-co-pilot.htm](https://productresources.collibra.com/docs/collibra/latest/Content/CollibraAI/co_cai-co-pilot.htm) — AI Copilot chat widget documentation (contains UI screenshots).
15. [collibra.com/resources/collibra-ai-copilot](https://www.collibra.com/resources/collibra-ai-copilot) — AI Copilot marketing/demo page.
16. [productresources.collibra.com/.../co_settings-ai-copilot.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Settings/AIAgents/co_settings-ai-copilot.htm) — AI Agents admin settings screen.
17. [productresources.collibra.com/.../co_about-ai-governance.htm](https://productresources.collibra.com/docs/collibra/latest/Content/AIGovernance/co_about-ai-governance.htm) — AI Governance registry/lifecycle-tracker UI.
18. [collibra.com/resources/collibra-ai-governance-staying-compliant-with-the-eu-ai-act](https://www.collibra.com/resources/collibra-ai-governance-staying-compliant-with-the-eu-ai-act) — EU AI Act assessment template screens.
19. [productresources.collibra.com/.../to_guided-stewardship.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Catalog/GuidedStewardship/to_guided-stewardship.htm) — Guided Stewardship physical/semantic/conceptual layer UI.
20. [productresources.collibra.com/.../co_asset-pages.htm](https://productresources.collibra.com/docs/collibra/latest/Content/Assets/co_asset-pages.htm) — anatomy of a standard asset page.
21. [marketplace.collibra.com](https://marketplace.collibra.com/) — Marketplace listing grid (connectors/MCP servers/workflows).
22. [collibra.com/tour](https://www.collibra.com/tour) — interactive product-tour hub (choose-your-path UI).
23. [collibra.com/live-demo-series](https://www.collibra.com/live-demo-series) — recorded demo video series landing page.
24. [collibra.com/demo](https://www.collibra.com/demo) — demo request page, often shows a static product screenshot.
25. [collibra.com/resources/2025-gartner-magic-quadrant-data-analytics-governance-platforms](https://www.collibra.com/resources/2025-gartner-magic-quadrant-data-analytics-governance-platforms) — MQ graphic (positioning, not UI, but useful for competitive-deck context).
