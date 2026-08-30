Status: research input for the Aug-2026 architecture review. Vendor claims are cited, not endorsed.

# Competitive Teardown: Alation, Microsoft Purview, Databricks Unity Catalog, Secoda/Select Star

## Verdict in five lines per vendor

**Alation** — Best-in-class structured wiki layer (Articles/Templates/Document Hubs) and the only vendor with a decade-mature query-log-driven behavioral engine feeding both stewardship and DQ rules. AIOS (July 2026) is a rebrand with one genuinely new data point: a published agent-accuracy-lift benchmark (60%→100% from metadata fixes alone). MCP/Agent SDK exists but churns fast (breaking changes every minor version). Lineage is inferred (query-log/connector), not runtime-native. Steal the Articles/Templates object model; don't copy the AI-doc-generation maturity — it's behind Secoda/Select Star.

**Microsoft Purview** — Only vendor where a glossary term is itself a policy-propagation mechanism (attach a term to a data product, its access policy cascades automatically) and only vendor natively unified with MIP sensitivity labels across the whole M365 estate. Consumption/vCore pricing is structurally different from seat licensing — can be much cheaper or a surprise bill depending on scan frequency and estate size. Lineage has well-documented, practitioner-reported gaps (Databricks↔Power BI, Synapse Dedicated Pools). No first-party MCP for governance as of Aug 2026. Steal the policy-cascading-glossary-term mechanic; don't inherit the two-catalog-system problem (Purview + Fabric OneLake overlap) or the dual-permission steward friction.

**Databricks Unity Catalog** — Only platform where governance is enforced *inside the compute engine at query time* (ABAC via tag-driven UDF policies) rather than orchestrated against external warehouses, and only platform with runtime-native (not log-inferred) column lineage via system tables. Genie's curated-knowledge-store architecture (instructions, trusted assets, benchmarks, knowledge mining from lineage+query history) is the closest published reference architecture to what we're building for a SQL agent. Hard constraint: everything here is Databricks-workload-scoped — lineage, ABAC enforcement, and Genie grounding all stop at the platform boundary. OSS Unity Catalog (Iceberg/Hive-REST-compatible) is a genuine open-protocol bet neither Collibra nor Atlan has. Steal the ABAC mechanics and the Genie knowledge-store shape wholesale; don't assume any of it reaches non-Databricks systems without extra engineering.

**Secoda + Select Star** — Both treat AI-generated documentation as the *default* first-pass state of every asset (not an opt-in enrichment), both explicitly chain lineage + query logs + BI titles into generation, both visually distinguish AI-suggested vs. human-confirmed text at the field level. Secoda publishes real architectural detail (multi-model routing, verification-first prompting, synthetic-data-trained retrieval embeddings) and offers genuine self-hosted/VPC deployment. Select Star is SaaS-only on AWS (no VPC/on-prem) but is a launch partner in Snowflake's Open Semantic Interchange, giving it the most concrete "existing BI dashboards → AI-ready semantic models" pipeline of any vendor reviewed. Neither has anything as structurally deep as Alation's Article/Template/Hub model — their "wiki" is field-level description text, not a freeform documented layer. Steal the default-generation-with-provenance-marking UX and Secoda's verification-first prompting pattern; don't expect either to give you Alation-grade structured documentation objects.

---

## A. ALATION

### Platform positioning — AIOS (supersedes "Agentic Data Intelligence Platform"/ADOP branding)
Alation's current top-level brand (launched July 14, 2026) is **AIOS — the "Alation Intelligence Operating System"** — explicitly framed by CEO Satyen Sangani as *not* a bolt-on product but "the interconnected system that already lives at the core of Alation," extended to serve agents as well as people ([Alation blog](https://www.alation.com/blog/introducing-aios-alation-intelligence-operating-system/), [Globe and Mail release](https://www.theglobeandmail.com/investing/markets/markets-news/GlobeNewswire/36774217/alation-launches-aios-all-new-intelligence-operating-system-for-enterprise-ai/)). AIOS integrates five elements — **agents, context, data, governance, feedback loops** — and targets three failure layers: data (agents on bad data), context (stale definitions/rules), and agent (drift between agent config and live tools). Alation cites a benchmark where a SQL agent's accuracy rose from 60%→100% across two iterations purely from metadata corrections, no model change — a concrete "context, not model, is the bottleneck" argument. Production references: Georgia-Pacific ($25M intercompany-transfer trust restoration), Daimler Truck NA (supply-chain agents on "living" metadata), Euromonitor (traceable agent answers for reputational risk).

The prior "Agentic Data Intelligence Platform" (launched March 3, 2025) is still the product-page framing for five modules: Search/Discovery, Data Governance (Policy Center), Data Lineage, Data Quality, Data Products Marketplace — layered on an **Active Metadata Graph**, workflow automation, **ALLIE AI** (Alation's ML core), Open Connector Framework, and AI Agent SDK ([product page](https://www.alation.com/product/agentic-data-intelligence-platform/)). >600 customers, 40% of Fortune 100 ([announcement](https://www.alation.com/news-and-press/alation-announces-agentic-platform-reinventing-the-data-catalog-for-ai-era/)).

**Take / Leave:** Take the "compounding metadata correction improves agent accuracy" framing as a validation story to reuse internally — it's the same bet we're making. Leave the AIOS branding exercise itself; it's positioning, not new mechanics.

### Behavioral Analysis Engine (BAE) — historic differentiator, now productized
Alation's original moat: it **passively ingests SQL query logs** ([Query Log Ingestion docs](https://docs.alation.com/en/latest/sources/CatalogSources/QueryLogIngestion.html)) to compute **popularity scores** (which tables/columns/dashboards are actually used, by whom, how often) rather than relying on manual tagging. This is now explicitly named the **Behavioral Analysis Engine (BAE)** and reused as the rule-suggestion mechanism for **Data Quality Agent**: BAE "learns from query patterns, usage metrics, and behavior signals to identify critical data elements" and auto-recommends DQ checks a steward can accept/modify rather than hand-write ([Data Quality Agent product page](https://www.alation.com/product/data-quality-agent/)). The **Analytics Stewardship** app surfaces a Curation Progress dashboard tying usage data to where curation effort has the highest payoff, explicitly positioned against "IT-centric" static catalogs ([Analytics Stewardship blog](https://www.alation.com/blog/introducing-alation-analytics-stewardship-unlocking-the-business-value-from-governance/)).

**Take / Leave:** Take the pattern of one behavioral signal (query logs) feeding *two* downstream systems (curation priority AND DQ rule suggestion) — that's a reusable architectural idea, not vendor-specific IP. Leave any notion this requires Alation's specific engine; it's just "ingest query logs, score usage, route the score into multiple consumers."

### TrustCheck — in-tool trust flags
Visual trust indicators (**Endorsed / Warning / Deprecated**) attached to catalog objects, surfaced directly inside BI tools and SQL clients (not just the catalog UI) via **Alation Anywhere** browser/desktop integration, so an analyst in Tableau/Excel/a SQL IDE sees a flag before running a query against a table ([docs](https://docs.alation.com/en/latest/welcome/BestPractices/UseTrustFlagstoProceedwithConfidence.html)). Stewards and Catalog Admins configure flags; original feature dates to 2018 ([SiliconANGLE 2018](https://siliconangle.com/2018/07/12/can-your-data-be-trusted-alation-rolls-out-trustcheck-for-visual-verification-cubeconversations/), [launch PR](https://alation.com/press-releases/alation-introduces-agile-information-stewardship-with-trustcheck)).

**Take / Leave:** Take the principle: trust state must render *at the point of use* (in the SQL/BI tool), not only inside the catalog UI — a governed AI SQL agent should refuse or flag on a Deprecated-tagged table the same way. Leave the specific three-state taxonomy if our risk model needs more granularity (e.g., a bank likely needs a fourth "regulatory-hold" state).

### Compose — intelligent SQL editor
Embedded SQL editor merging query execution with catalog metadata in one pane: color-coded quality/trust indicators next to tables, popularity-driven query/table suggestions, one-click publish-to-catalog for reusable queries, and **interactive SQL forms** letting non-technical users modify a published query via dropdowns/filters without writing SQL ([product page](https://www.alation.com/product/compose/)).

**Take / Leave:** Take "interactive SQL forms" — parameterizing a vetted query into a dropdown/filter UI is functionally identical to a Genie trusted-asset/UC-function pattern (see Section C) and is a good non-technical-user escape hatch alongside a chat agent. Leave the general-purpose SQL editor itself; our agent should be the primary interface, not a human-facing IDE.

### Articles / Article Groups / Document Hubs / Templates + typed custom fields — EXPANDED

This is Alation's most directly reusable structure for an auto-compiled wiki. The object model has four layers:

**1. Document Hubs (top-level containers).** A Document Hub is a standalone wiki *space* — independent of the data-catalog navigation tree — that can be scoped per business domain, team, or governance program ([Document Hub Basics](https://www.alation.com/docs/en/latest/welcome/DocumentHubs/index.html)). Each hub has its own permission set, separate from catalog-object permissions, so a domain team can own and curate its own wiki without touching underlying data-source ACLs. Hubs contain **Folders** (for taxonomy/navigation, [Manage Folders](https://docs.alation.com/en/latest/steward/Documentation/DocumentHubs/ManageFolders.html)) and **Documents** ([Manage Documents](https://www.alation.com/docs/en/latest/steward/Documentation/DocumentHubs/ManageDocuments.html)). Permissions on hubs follow Alation's standard **role-based access control (RBAC)** and support **inheritance from folder down to document**, so a permission set at the folder level cascades to child documents unless explicitly overridden at the document level — the same 7 platform roles (Server Admin, Catalog Admin, Source Admin, Steward, Composer, Explorer, Viewer) gate hub-level actions, layered with hub-specific grants ([Document Hub Permissions](https://docs.alation.com/en/latest/steward/Documentation/DocumentHubs/DocumentHubPermissions.html), [Access/Roles/Permissions](https://docs.alation.com/en/latest/welcome/CatalogBasics/AccessRolesPermissions.html)). Full permission-tier documentation (exact view/edit/manage split) isn't published in detail in the crawlable docs — treat the tier boundary as "read vs. curate vs. administer" until confirmed against a live tenant.

**2. Articles (the wiki-page primitive).** An Article is a freeform, versioned, rich-text content object that either (a) attaches to one or more catalog assets — a table, column, report, glossary term — becoming that asset's documentation tab, or (b) stands alone inside a Document Hub as a general knowledge-base page ([Work with Articles](https://docs.alation.com/en/latest/steward/Documentation/Articles/WorkwithArticles.html)). Articles support a **List View** (flat browse) and a **Taxonomy View** (hierarchical, by Article Group), have built-in **Conversations** (threaded comments per article, for review/discussion without leaving the page), and are collaboratively editable with sharing controls. Critically, the *same* Article object type is used whether the content is "column-level definition" (narrow, asset-scoped) or "onboarding runbook for the lending-risk domain" (broad, hub-scoped) — one primitive, two attachment modes.

**3. Article Groups (taxonomy).** Articles are organized into **Article Groups**, which function as a tagging/taxonomy layer independent of the folder hierarchy — an Article can belong to a group ("Regulatory Definitions," "Onboarding") that cuts across multiple hubs/folders, giving a second, orthogonal navigation axis on top of the hub/folder tree ([Create Article Group](https://www.alation.com/docs/en/latest/steward/Documentation/Articles/CreateArticleGroup.html)).

**4. Templates + typed custom fields (the schema layer).** Every catalog object type and every Article can have a **Template** applied — a reusable metadata schema defining which fields render on that object's documentation page ([About Templates and Fields](https://docs.alation.com/en/latest/steward/TemplatesAndCustomFields/AboutTemplatesAndFields.html)). Field types:
- **Rich text** — freeform prose (the "wiki paragraph" field).
- **Reference field** — a typed pointer to another catalog object (e.g., "Owning System" pointing at a specific data source object), giving structured relationships instead of prose links.
- **People-set field** — assigns one or more stakeholders (e.g., "Data Steward," "Escalation Contact") with actual user/group identity, not free text.
- **Multi-select picker** — controlled-vocabulary tags (e.g., regulatory regime: GDPR/SOX/BSA-AML), enforcing consistent values across every asset using the template.
- **Object-set field** — a collection of typed references (e.g., "Related Critical Data Elements"), for many-to-many structured relationships.
Templates can be **fixed-order** (locked field sequence, for compliance-mandated documentation shapes) or **adjustable-order** (steward can rearrange), and **field-level permissions** independently control who can view vs. edit each field — so, e.g., a "Regulatory Classification" field can be steward-editable while a "Business Description" field is open to any Composer, on the same template.

**Why this matters for an auto-compiled wiki:** the Alation model proves the right decomposition is (a) a generation-agnostic content primitive (Article) that can be either narrowly asset-attached or broadly domain-standalone, (b) a taxonomy layer (Article Group) orthogonal to physical folder location, (c) a typed-field schema layer (Template) so structured facts (owner, regulatory tag, related CDE) don't get buried in prose and *can* be queried/validated programmatically, and (d) permission scoping at the space (hub) level with folder→document inheritance and per-field override. What Alation has **not** done is make Article *generation* itself AI-native — the Documentation Agent (2025) ingests/organizes/connects existing content to assets, but there's no publicly documented equivalent of Secoda/Select Star's default-on, provenance-marked AI drafting *of the prose itself* inside this structure. That gap is exactly the opportunity: bolt Secoda/Select Star-grade generation onto an Alation-grade object model.

**Take / Leave:** Take the four-layer decomposition (Hub → Article/Article Group → Template/typed fields) near-verbatim as the schema for our auto-compiled wiki; take the fixed-order-template-for-compliance pattern specifically for regulatory documentation. Leave Alation's manual-curation-first workflow — we should generate Article content by default (Secoda/Select Star pattern) and route to human review only via the provenance-marked diff, not require a steward to author from a blank template.

### Lineage
Built from three sources: (1) **Query Log Ingestion** parses SQL logs to infer flow; (2) **OCF connector metadata extraction** captures structural lineage during ingestion; (3) **OpenLineage** integration (incl. Apache Airflow, beta) and direct API push for orchestration tools ([docs](https://docs.alation.com/en/latest/analyst/Lineage/index.html)). Supports table- and column-level lineage; versioned as Lineage V2/V3 (feature-flagged by deployment).

**Take / Leave:** Take multi-source lineage fusion (logs + connector metadata + OpenLineage) as the right approach when you don't control the compute engine. Leave the expectation that log-inferred lineage will ever be as complete as runtime-native capture (see Databricks, Section C) — budget for gaps.

### Open Connector Framework (OCF), Data Products, Data Health
- **OCF**: SDK-based partner/customer-buildable connector framework, superseding native (hardcoded) connectors; 120+ pre-built connectors ([connectors page](https://www.alation.com/product/connectors/)); customers migrate native→OCF over time ([migration docs](https://www.alation.com/docs/en/latest/OpenConnectorFramework/InstallandManageConnectors/MigrateNativeSourcestoOCF/index.html)).
- **Data Products Marketplace**: data products built on the **Open Data Product Specification (ODPS)** — "machine-readable, context-rich," consumable by humans *and* agents; **Data Products Builder Agent** auto-generates certified products without code; certification enforces quality/ownership/compliance gates before marketplace publication; request-and-approval access workflow works identically whether the requester is a human or an agent ([product page](https://www.alation.com/product/data-products-marketplace/)). "Chat with Your Data" claims up to 60% more accurate NL-to-answer with full join/definition transparency.
- **Data Health** (rebrand of Data Quality, agentic since March 2025): **Data Quality Agent** auto-generates DQ rules from BAE signals, surfaces issues in-catalog and via Slack/Teams/email/BI-tool alerts (Alation Anywhere), shows a rollup quality score, prioritizes by business impact ([announcement](https://www.alation.com/news-and-press/alation-announces-agentic-data-quality-solution/), [product page](https://www.alation.com/product/data-quality-agent/)).

**Take / Leave:** Take ODPS as a candidate open spec to align our own "data product" object to, rather than inventing our own schema. Leave the "same approval workflow for human and agent requesters" as an idea worth stealing directly — don't build a separate agent-access path.

### AI Agent SDK / MCP
Open-source Python SDK ([GitHub](https://github.com/Alation/alation-ai-agent-sdk)) exposing catalog-context search, lineage graph resolution (upstream/downstream), custom-field retrieval, data-product access, and DQ-via-SQL-analysis as tools; **native MCP server** (STDIO and HTTP modes) works with Claude Desktop, VS Code, LangChain, LibreChat. Auth via service-account client_id/secret (recommended) or bearer token; user-account auth deprecated in 1.0.0rc1. Notable churn: Context Tool deprecated in 1.0.0rc2 (removal Feb 2026), `update_catalog_asset_metadata` and `check_job_status` tools removed in 1.0.0.

**Take / Leave:** Take nothing structurally new here — it's a standard MCP-over-REST wrapper. Leave any assumption of API stability; pin versions if we ever integrate against it.

### Roles, Stewardship Workbench, Analytics Stewardship
Roles/license types: **Server Admin, Catalog Admin, Source Admin, Steward, Composer, Explorer, Viewer** ([Roles Overview](https://docs.alation.com/en/latest/welcome/CatalogBasics/RolesOverview.html)). **Stewardship Workbench** is a bulk-curation UI accessible to Steward/Composer/Source Admin/Catalog Admin/Server Admin roles for mass metadata edits ([docs](https://docs.alation.com/en/latest/steward/StewardshipWorkbench/index.html)). **Analytics Stewardship** dashboards close the loop by showing stewards the *downstream behavioral effect* of their curation.

**Take / Leave:** Take the "show the steward the behavioral effect of their own curation" feedback loop — it's a cheap, high-value retention mechanic for any human-in-the-loop review UI we build. Leave the 7-role taxonomy as-is; a bank's IAM model will need finer regulatory-role granularity anyway.

---

## B. MICROSOFT PURVIEW (Unified Catalog, 2026 state)

### Classic Data Map/Data Catalog → Unified Catalog re-platform
The **classic Purview governance portal** (Data Map = asset inventory/scanning/lineage engine; classic Data Catalog = business glossary/search UI) is being superseded by **Unified Catalog**, a single SaaS layer sitting *on top of* the Data Map ("We invested in a strong platform that has an inventory... Now we're providing better tools to manage it as it grows") ([Unified Catalog overview](https://learn.microsoft.com/en-us/purview/unified-catalog)). The shift is philosophical: classic Purview was **asset-centric/inventory-focused** (passive discovery); Unified Catalog is **business-concept-driven and governance/value-focused**, organized around **federated governance** — centralized standards, decentralized self-service ownership. Data Map remains the underlying scanning/classification/lineage engine; Unified Catalog is the governance/business layer.

**Take / Leave:** Take the explicit split between "asset inventory engine" and "governance/business layer" as an architectural pattern — it maps cleanly onto separating our metadata-crawling pipeline from our governance/wiki layer. Leave the two-portal transition cost; build the split cleanly from day one instead of retrofitting it.

### Core objects
- **Governance Domains**: boundary for common ownership/discovery ("mini catalog inside Unified Catalog"); types = Functional unit, Line of business, Data domain, Regulatory, Project ([docs](https://learn.microsoft.com/en-us/purview/unified-catalog-governance-domains)).
- **Data Products**: group tables/files/Power BI reports etc. into one requestable unit — explicitly sold as turning "15 separate access requests" into one ([docs](https://learn.microsoft.com/en-us/purview/unified-catalog-data-products)).
- **Glossary Terms**: "evolved from static to active objects" — terms **carry access/handling policies that cascade automatically** to any data product they're attached to (e.g., an HR "Feedback Results" term auto-applies an access policy to every labeled product) — this is Purview's most distinctive governance mechanic: policy-by-tag propagation through the glossary itself, not a separate policy engine.
- **Critical Data Elements (CDEs)**: logical grouping mapping physically-different columns (e.g., "CustID" and "CID") to one governed concept, carrying DQ rules and access policies.
- **OKRs**: link data products to business objectives/measurable KPIs, explicitly bridging governance to business value tracking.

**Take / Leave:** Take the policy-cascades-from-glossary-term mechanic directly — it's the cleanest "attach a business concept, inherit its governance" pattern of any vendor here and is a strong candidate primitive for our own critical-data-element/glossary design. Leave the "Regulatory" domain type as a literal category; a bank will want regulatory scope as a cross-cutting attribute, not a peer of "Line of business."

### Data Quality
Six OOB dimensions (completeness, consistency, conformity, accuracy, freshness, uniqueness) plus custom expression-based rules, applied at column level and rolled up to asset→data product→governance-domain scores. **AI-suggested rules**: AI recommends columns to profile, with mandatory human refinement before rules activate ([docs](https://learn.microsoft.com/en-us/purview/unified-catalog-data-quality)). Hard limits: **200 DQ rules max per asset per scan**; scans run on **Apache Spark 3.5 / Delta Lake 3.2.1**; only Managed Identity auth supported; Google BigQuery lacks VNet support; Parquet only supported in two specific directory layouts.

**Take / Leave:** Take the hierarchical score rollup (rule→asset→product→domain) as a clean aggregation model. Leave the 200-rules-per-asset cap as a design constraint we shouldn't inherit — a bank's CDEs may need more granular rule coverage than that on the highest-risk tables.

### Health Management
Four components — **Controls** (score data-estate health against standards, global-set/domain-executed, customizable thresholds), **Data Quality** (as above), **Actions** (Preview — converts anomalies into assigned, trackable remediation tasks), **Reports** (adoption/classification/governance summaries) ([docs](https://learn.microsoft.com/en-us/purview/unified-catalog-data-health-management)). Roles: **Data Health Owner** (CRUD) / **Data Health Reader** (read-only).

**Take / Leave:** Take "Actions" as a UI pattern — converting a detected anomaly into an owned, trackable remediation task (not just an alert) closes a loop most catalogs leave open. Leave the Preview-stage immaturity as a warning: this is not yet a mature enterprise workflow engine.

### Data Map: scanning, classification, sensitivity labels
Scans registered sources for 150+ built-in classifiers (sensitive-information-type detection) and integrates natively with **Microsoft Purview Information Protection (MIP)** sensitivity labels, so a label applied in M365/MIP is the *same* label surfaced on a scanned data asset in the catalog — a genuinely unique cross-product integration.

**Take / Leave:** Take nothing directly (we don't own an equivalent M365-wide labeling stack), but note the *pattern*: one label taxonomy, enforced identically wherever data lives, is the target state — worth replicating with our own single source of truth for sensitivity classification across warehouse + docs + BI.

### Lineage — capability and known limitations
Native connectors auto-capture lineage from Azure Data Factory, Synapse pipelines, Power BI, dbt, Airflow (preview). Community/practitioner threads document persistent gaps: end-to-end lineage frequently **breaks between Azure Databricks and Power BI** ([Q&A thread](https://learn.microsoft.com/en-us/answers/questions/5773525/end-to-end-lineage-not-visible-between-azure-datab)), Synapse Dedicated SQL Pool and private-endpoint SQL Server lineage has documented restrictions ([Q&A thread](https://learn.microsoft.com/en-us/answers/questions/5808041/inquiry-on-purview-lineage-limitations-for-synapse)), and visualization usability complaints are common enough to warrant an open guidance thread ([Q&A thread](https://learn.microsoft.com/en-us/answers/questions/5845759/request-for-guidance-simplifying-data-lineage-visu)).

**Take / Leave:** Take the warning at face value: connector-stitched, multi-hop lineage across heterogeneous platforms breaks in practice, not just in theory. Leave any plan that assumes cross-platform lineage will "just work" from connectors alone — budget explicit reconciliation/validation tooling.

### Fabric/OneLake, Unity Catalog, Snowflake
Purview and **Microsoft Fabric's own OneLake catalog are separate, overlapping systems** that Microsoft explicitly says organizations need *both* of ([Medium analysis citing MS positioning](https://medium.com/@marcoOesterlin/understanding-microsoft-fabric-onelake-catalog-vs-microsoft-purview-unified-catalog-e3db5674a10a)). Databricks Unity Catalog registers as a scannable source via a documented connector ([register-scan doc](https://learn.microsoft.com/en-in/purview/register-scan-azure-databricks-unity-catalog)), but automating *lineage* out of UC into Purview requires extra engineering. Purview-vs-UC is positioned as governance-layer-vs-platform-native-governance, not equivalent scope ([Atlan comparison](https://atlan.com/know/purview-vs-databricks-unity-catalog/)).

**Take / Leave:** Take this as a cautionary architecture note: two catalogs that overlap in scope inside one vendor's own stack is a real failure mode. Leave duplicate-catalog risk unaddressed at our own peril — decide up front whether our platform is the system of record or a federation layer over Databricks/Snowflake catalogs, not both ambiguously.

### Roles/permissions: collections vs. governance domains
Two parallel RBAC systems: **classic collections-based RBAC** (flat/nested collection hierarchy, technical/source-centric, centralized) vs. **Unified Catalog's 3-tier model**: Tenant (Data Governance, Data Source Administrators, Purview Administrators) → Catalog (Data Governance Administrator, Data Health Owner/Reader, Global Catalog Reader, Governance Domain Creator, Global Asset Curator) → Governance Domain (Domain Owner/Reader, Data Product Owner, Data Steward, Local Catalog Reader, Data Profile/Quality Steward, Data Quality Reader) ([docs](https://learn.microsoft.com/en-US/purview/governance-roles-permissions)). Notably, **Data Product Owners/Stewards need dual permissions** — a Unified Catalog domain role *and* a separate Data Map reader role — to actually add assets to a data product.

**Take / Leave:** Take the tenant→catalog→domain three-tier shape as a sound RBAC skeleton. Leave the dual-permission requirement (domain role + separate Data Map role for the same action) — that's friction to design out, not replicate.

### APIs, Atlas compatibility, Copilot/agents, MCP
Classic Data Map exposes **Atlas 2.2-compatible REST APIs** plus native Purview REST APIs for asset/lineage CRUD ([Atlas API docs](https://docs.azure.cn/en-us/purview/data-gov-api-atlas-2-2)). **Security Copilot agents in Purview** (2026) are compliance/security-focused, not catalog-focused: DLP Triage Agent (NL-instruction-to-classification-logic), Insider Risk Triage Agent, Data Security Posture Management Posture Agent (NL search across M365 content), Data Security Investigations Posture Agent (credential discovery) ([docs](https://learn.microsoft.com/en-us/purview/copilot-in-purview-agents-overview)) — these run on provisioned **Security Compute Units (SCUs)**, files ≤2MB, 30-day lookback, active-mode-DLP-only. **There is no confirmed first-party MCP server for Purview Unified Catalog/governance** as of this research.

**Take / Leave:** Take the NL-instruction-to-structured-logic pattern from the DLP Triage Agent (plain English → a logic_expression AST) as a UX idea for policy authoring in our own platform. Leave any assumption Purview has an agent-ready catalog MCP surface today — it doesn't.

### Pricing — the sharp differentiator vs. Collibra/Atlan
Purview is **consumption-based, not seat/tier licensed**: Data Map billed in **Capacity Units (CU)** — 1 CU = up to 10GB metadata storage + 25 ops/sec, auto-scaling; **scanning/classification billed in vCore-hours** (full vs. incremental scans consume differently); **Unified Catalog billed per unique governed data asset** linked to governance concepts, region/currency-variable ([Data Map pricing guide](https://learn.microsoft.com/en-us/purview/data-gov-classic-pricing-data-map), [Azure pricing page](https://azure.microsoft.com/en-us/pricing/details/purview/)). Microsoft Q&A threads document real confusion/cost-overrun cases ([Azure cost increase thread](https://learn.microsoft.com/en-au/answers/questions/249616/azure-cost-increase-while-using-purview)).

**Take / Leave:** Take consumption-based cost modeling as worth evaluating for our own platform's internal chargeback (cost scales with estate size, not headcount, which may suit a bank's actual usage pattern). Leave the vCore/CU pricing opacity — practitioner threads suggest it's genuinely hard to forecast; don't replicate that lack of predictability in an internal cost model.

---

## C. DATABRICKS UNITY CATALOG

### Three-level namespace, metastore, external locations
Standard `catalog.schema.table` three-level namespace; one **metastore** per region typically attaches to many workspaces; **external locations** + **storage credentials** decouple cloud-storage access (managed identity/service principal, IAM role) from table definitions, enabling **credential vending** to non-Databricks clients ([What is Unity Catalog](https://docs.databricks.com/aws/en/data-governance/unity-catalog/)). **Delta Sharing** is the open cross-org sharing protocol, letting a bank share governed data externally without copying it or requiring the recipient run Databricks.

**Take / Leave:** Take credential vending as the right shape for "let external tools read governed data without duplicating access-control logic." Leave nothing here to avoid — this is standard, sound catalog architecture worth matching.

### ABAC — the headline governance differentiator vs. Collibra/Atlan — EXPANDED

Unity Catalog ABAC (GA 2026) is **policy-based, not object-by-object GRANT-based**: instead of granting a role privileges on each table one at a time, an admin defines a policy once against a **tag condition**, and it auto-applies to every object — present and future — matching that condition ([blog](https://www.databricks.com/blog/abac-row-filtering-and-column-masking-policies-governed-tags-and-data-classification-are-now), [core concepts](https://docs.databricks.com/aws/en/data-governance/unity-catalog/abac/core-concepts)).

**Governed tags.** Account-level key-value pairs, definable with an optional **allowed-values list** (e.g., a `pii` tag restricted to `ssn`/`address`/`email`) or as a key-only flag (e.g., a `consent` tag with no value). Tags attach to catalogs, schemas, tables, columns, or models. **Hierarchical inheritance**: a tag set on a catalog or schema is automatically inherited by every child table — *except at the column level*, where tags must be applied directly to each column (no inheritance from table to column). Example from Databricks' own tutorial:
```
ALTER TABLE abac_tutorial.customers.profiles
ALTER COLUMN ssn_number
SET TAGS ('pii' = 'ssn');
```

**Three policy types**, all created via `CREATE POLICY`:

1. **Row filter policy** — excludes rows where a bound UDF returns FALSE. Syntax:
```
CREATE [ OR REPLACE ] POLICY policy_name
ON { CATALOG catalog_name | SCHEMA schema_name | TABLE table_name }
ROW FILTER function_name
TO principal [, ...]
[ EXCEPT principal [, ...] ]
FOR TABLES
[ WHEN condition ]
[ MATCH COLUMNS condition [ [ AS ] alias ] [, ...] ]
[ USING COLUMNS ( function_arg [, ...] ) ]
```
Worked example (hide EU-address rows from anyone querying a table with a column tagged `pii=address`):
```
CREATE POLICY hide_eu_customers
ON SCHEMA abac_tutorial.customers
ROW FILTER is_not_eu_address
TO `account users`
FOR TABLES
MATCH COLUMNS has_tag_value('pii', 'address') AS addr_col
USING COLUMNS (addr_col);
```
The policy is defined once at the *schema* level; it silently attaches to every current and future table in that schema that has a column tagged `pii=address`.

2. **Column mask policy** — replaces a matched column's returned value via a bound UDF. Syntax:
```
CREATE [ OR REPLACE ] POLICY policy_name
ON { CATALOG catalog_name | SCHEMA schema_name | TABLE table_name }
COLUMN MASK function_name
TO principal [, ...]
[ EXCEPT principal [, ...] ]
FOR TABLES
[ WHEN condition ]
[ MATCH COLUMNS condition [ [ AS ] alias ] [, ...] ]
ON COLUMN alias
[ USING COLUMNS ( function_arg [, ...] ) ]
```
Worked example (mask any column tagged `pii=ssn` to `***-**-****`):
```
CREATE POLICY redact_ssn_policy
ON SCHEMA abac_tutorial.customers
COLUMN MASK redact_ssn
TO `account users`
FOR TABLES
MATCH COLUMNS has_tag_value('pii', 'ssn') AS ssn_col
ON COLUMN ssn_col;
```

3. **GRANT policy (Beta)** — dynamically grants a privilege when a tag condition matches, no UDF required, for scenarios where the goal is conditional access rather than filtering/masking.

**Condition functions.** `WHEN` and `MATCH COLUMNS` clauses support: `has_tag()` / `has_tag_value()` (tag-based, usable in both row-filter and column-mask policies); `has_identity_attribute_value()` / `has_identity_attribute_tag_match()` (column-mask `WHEN` only — ties the policy to attributes of the querying *user's* identity, enabling classic ABAC "user attribute must match row/column attribute" logic, e.g., a loan officer only unmasking SSNs for their assigned region); `has_context_attribute()` / `has_context_attribute_value()` (row-filter/column-mask `WHEN` only — session/request context, e.g., time of day, client IP class, or a custom session tag set by the calling application).

**Why this is the differentiator:** the policy is defined against a *classification*, not a list of objects, so onboarding a new table that happens to get tagged `pii=ssn` automatically inherits the masking rule with zero admin action — this is enforcement *inside the query engine*, at execution time, not a separate access-request/approval workflow layered on top. Full requirements/quotas/limitations are documented separately ([requirements doc](https://docs.databricks.com/aws/en/data-governance/unity-catalog/abac/requirements)).

**Take / Leave:** Take the entire mechanic as close to verbatim as our compute layer allows: tag-condition-triggered policies (not object enumeration), column-level tag non-inheritance (forces explicit column classification — a good safety default, not a bug), and the identity-attribute/context-attribute condition functions as the template for "user role must match row's regional tag" logic a bank will need constantly. Leave nothing here — this is the single most reusable mechanism in the entire teardown for our access-control design.

### Lineage — native, not inferred
Captured automatically for every query on Databricks compute (no manual instrumentation) at both table and **column level**, queryable directly as **`system.access.table_lineage`** and **`system.access.column_lineage`** system tables, aggregated across all workspaces on a metastore ([docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-lineage)). Known caps: column lineage fails on path-based sources (`s3://...` instead of table refs) and UDF-obscured mappings; **no lineage before Sept 1, 2024**; renamed objects break continuity; Spark SQL checkpoints/RDDs unsupported; system tables retain only a rolling 1-year window.

**Take / Leave:** Take runtime-capture-as-system-tables as the gold standard when we control the compute engine — query it with SQL, no separate lineage-parsing pipeline needed. Leave the assumption this generalizes to non-Databricks compute; for us it likely means log/connector-inferred lineage everywhere except wherever we run our own governed query execution.

### Metrics views — governed semantic layer
Unity Catalog **metric views** separate **measures** (e.g., sum(revenue)/count(distinct customer)) from **dimensions** (group-by/filter fields), defined once and reusable across any consuming tool — BI (Power BI, Tableau, Sigma), notebooks, SQL editor, dashboards/alerts, **and Genie agents** ([docs](https://docs.databricks.com/aws/en/uc-semantics/metric-views/)).

**Take / Leave:** Take the measure/dimension separation as the correct semantic-layer primitive for our own metrics catalog — define once, group-by-anything at query time, and make it the single object every consumer (chat agent, dashboard, ad hoc SQL) resolves against.

### AI/BI Genie — curated knowledge store — EXPANDED (reference architecture for our SQL agent)

Genie (GA) is deliberately **not** "point an LLM at the schema." It is grounded in a **per-space curated semantic knowledge store** that a data analyst explicitly builds and tunes, combining manual curation with automated mining ([GA blog](https://www.databricks.com/blog/aibi-genie-now-generally-available), [tune-quality docs](https://docs.databricks.com/aws/en/genie/tune-quality)).

**Instructions.** Configured at *Configure → Instructions*, three sub-types share a single budget: **100 total instructions per agent**, where general instructions, example SQL queries, and SQL functions each count against the same cap. Instruction text should be written "the way a user would naturally ask it" — Genie retrieves by matching user phrasing against instruction phrasing.

**General knowledge / metadata (Configure → Data).** Agent-specific table and column descriptions layered *on top of* Unity Catalog's own comments — lets an analyst give Genie a narrower, plainer-language gloss than the underlying technical schema comment, and lets them **hide irrelevant columns** entirely so Genie's search space stays small and precise.

**Synonyms / entity matching.** "Entity matching" provides curated lists of distinct values per column — up to **120 columns, 1,024 values each** — so Genie can resolve a user's colloquial term ("Florida") to the actual stored value ("FL"). This is a controlled-vocabulary layer distinct from the LLM's own world knowledge; it's populated from real column value distributions, not free association.

**Join relationships.** Explicit join definitions (left table, right table, join condition, cardinality: many-to-one / one-to-many / one-to-one) teach Genie the actual foreign-key graph rather than making it infer joins from column-name heuristics. When multiple joins could involve the same table, Genie **auto-generates aliases** for the right-hand side to avoid ambiguity in the SQL it writes.

**SQL Expressions (business-concept definitions).** Reusable named filters/measures/fields defined once under *Configure → Instructions → SQL Expressions* — e.g., a KPI formula — so every question that touches that KPI resolves to the identical, admin-approved SQL fragment rather than the LLM re-deriving the formula per question.

**Certified metrics.** Genie can pull metric definitions directly from Unity Catalog **metric views** (see above) as an additional trusted source of measures — governance and semantics share the same object, so a metric certified for BI is the same metric Genie uses.

**Trusted assets (verified question→SQL pairs) — the highest-precision layer.** A trusted asset is a **parameterized Unity Catalog SQL table function**, registered to a dedicated schema, requiring `CAN EDIT` on the Genie space to create and `EXECUTE` on the function for end users to invoke it. Concrete worked example from Databricks' own docs:
```
CREATE OR REPLACE FUNCTION users.user_name.open_opps_in_region (
    regions ARRAY<STRING>
    COMMENT 'List of regions. Example: ["APAC", "EMEA"]' DEFAULT NULL
)
COMMENT 'Addresses questions about the pipeline in the specified regions...'
RETURNS TABLE(Region STRING, `Opportunity Name` STRING, ...)
AS
  SELECT ...
  WHERE o.forecastcategory = 'Pipeline'
    AND (isnull(regions) OR array_contains(regions, region__c))
```
Matching behavior at query time is a hard split: if the user's question **exactly invokes** the function/pattern the trusted asset covers, Genie calls the function directly and the response is labeled **"Trusted"** (domain-expert-verified, no fresh SQL generation, highest confidence). If the user's question merely **relates to or paraphrases** an example, the trusted asset instead just "provides context and guides Genie in generating" *new* SQL — the response is **not** labeled Trusted. All SQL Functions included as instructions are automatically treated as trusted assets. Best-practice guidance: use `DEFAULT NULL` optional parameters with explicit NULL-checks, enumerate valid column values and date formats in comments, and put every Genie-space function in one dedicated schema for manageability. Databricks is explicit that trusted assets are **not a substitute for general instructions** — reserve them for well-established, recurring questions where an exact verified answer matters more than flexibility.

**Automated knowledge mining.** Genie analyzes **Unity Catalog lineage and query history** to proactively "suggest new instructions or highlight recurring patterns" — i.e., it watches what real queries against the underlying tables actually look like (via `system.access.*` lineage tables and query history) and proposes candidate instructions/examples an admin can approve, rather than waiting for an analyst to hand-author every instruction from scratch.

**In-session knowledge extraction.** During a live chat, Genie "extracts semantic information, proposes concise knowledge snippets" from the conversation itself for the user to approve — turning a successful ad hoc interaction directly into a durable instruction without a separate authoring step. This is the tightest human-in-the-loop feedback cycle documented among all vendors reviewed: generate → use → extract-as-candidate-knowledge → approve → persist.

**Ask for Review.** End users can flag any Genie response for space-admin verification/correction, independent of the automatic extraction above — a second, human-initiated feedback channel.

**Benchmarks.** A dedicated "Benchmarks" feature holds **curated test questions paired with expected SQL answers**, used to systematically score a space's accuracy before and after tuning changes — the practitioner-recommended bar is **>80% benchmark accuracy before production rollout** ([Kanerika 2026 review](https://kanerika.com/blogs/databricks-genie/)). Databricks' own docs describe Benchmarks as the mechanism to "evaluate and improve your agent's performance" but do not publicly detail the scoring algorithm (exact-match vs. execution-result-match) — treat this as an implementation detail to test empirically rather than assume.

**Governance.** Genie is "built natively on the Data + AI Platform," so every ABAC row filter / column mask / GRANT policy (Section above) applies transparently to whatever data Genie's generated SQL touches — there is no separate Genie permission model to keep in sync.

**Documented 2026 limitations.** Requires Unity Catalog governance already fully stood up. Cross-platform (non-Databricks) analytics needs external engineering. Multi-hypothesis "why" investigative questions are weak — **Genie Deep Research** was still in development as of early 2026. **Explicitly not designed** for customer-facing production analytics apps (that's Databricks Apps' job). Accuracy is entirely a function of curation quality, not a platform guarantee.

**Take / Leave:** Take the entire knowledge-store shape as our SQL agent's reference architecture: (1) instructions with a hard budget forcing curation discipline, not unlimited sprawl; (2) explicit entity/synonym value lists distinct from LLM world knowledge; (3) explicit join-graph definitions instead of column-name-heuristic joins; (4) a two-tier trust model — exact-match trusted-asset functions (verified, labeled) vs. paraphrase-guided generation (unlabeled, lower confidence) — which is the single most important mechanic to replicate, since it gives users an honest confidence signal instead of uniform LLM-generated-SQL risk; (5) automated knowledge mining from lineage+query history as the ongoing curation-cost-reducer; (6) in-session knowledge extraction with admin approval as the tightest feedback loop available; (7) a benchmark suite with expected-SQL pairs as the pre-production gate, with an explicit accuracy bar before go-live. Leave the 100-instruction-total cap as Databricks' specific tuning, not a law of nature — but keep *some* cap to force curation discipline rather than unbounded prompt stuffing.

### UC functions as agent tools / Managed MCP / Agent Bricks
UC-registered SQL/Python **functions become directly callable agent tools** with automatic governance enforcement — an agent invoking a UC function is subject to the same GRANT/ABAC rules as a human user ([docs](https://docs.databricks.com/aws/en/generative-ai/agent-framework/create-custom-tool)). **Managed MCP servers** (2026) expose three governed surfaces out of the box: **Genie** (structured-data NL access), **AI/Vector Search** (unstructured docs), **UC Functions** (deterministic tool calls) — and critically, these managed servers **automatically respect the calling user's existing UC permissions**, no separate ACL layer to maintain ([announcement](https://www.databricks.com/en/blog/announcing-managed-mcp-servers-unity-catalog-and-mosaic-ai-integration)). **Agent Bricks' Supervisor Agent** (GA) orchestrates multiple Genie spaces/agents and can register custom MCP servers hosted via Databricks Apps for internal-service connectivity ([Agent Bricks blog](https://www.databricks.com/blog/agent-bricks-governed-enterprise-agent-platform)). Bank limitation: nothing here natively reaches *external* legacy systems (mainframes, vendor APIs outside Databricks) — a bank still needs custom MCP servers via Databricks Apps for that reach.

**Take / Leave:** Take "MCP server inherits caller's existing permissions automatically" as a non-negotiable requirement for our own MCP surface — never build a parallel ACL system for agent access. Leave the assumption this covers everything; plan an explicit external-system MCP bridge from day one, same gap Databricks has.

### Open-sourced Unity Catalog (OSS)
Apache 2.0, hosted at **LF AI & Data (Linux Foundation)** ([blog](https://www.databricks.com/blog/open-sourcing-unity-catalog)). Manages Delta, Iceberg (via UniForm), Parquet, CSV/JSON tables, plus Volumes and AI functions/tools together in one catalog. **REST API is compatible with both Apache Hive Metastore API and Apache Iceberg's REST catalog API**, with credential vending for centralized storage-access governance. Existing hosted-UC customers get zero-disruption compatibility.

**Take / Leave:** Take the "speak an open, existing protocol (Iceberg REST / Hive Metastore) rather than invent a proprietary one" principle if we ever expose our own catalog externally — it buys ecosystem tool compatibility for free. Leave OSS UC itself as out of scope unless we adopt Databricks compute directly.

---

## D. AI-NATIVE AUTO-DOCUMENTATION CATALOGERS: Secoda & Select Star

(Castor/CastorDoc was acquired by **Coalesce** in 2026 ([Coalesce announcement](https://coalesce.io/company-news/coalesce-expands-data-platform-castordoc-acquisition-introduces-catalog/)) and folded into Coalesce's transformation platform; Metaphor's technology was acquired by **KPMG** to power internal AI/data-management tooling ([KPMG release](https://kpmg.com/us/en/media/news/kpmg-acquires-metaphors-technology-platform.html)) — neither is a standalone competitive product anymore, so Secoda and Select Star are the two strongest independent 2026 picks.)

### Secoda
- **Auto-documentation**: embedded AI assistant "populate[s] descriptions and create[s] documentation for you" across dbt, Snowflake, BigQuery, Redshift, Tableau, Looker sources, pulling resource names, popularity, lineage, queries, and existing descriptions as context; customer-quoted claim of 90% reduction in documentation time ([product page](https://www.secoda.co/documentation)).
- **Architecture**: hybrid multi-model routing — **Claude Opus** for complex reasoning, **Sonnet 4** for lighter follow-ups, automatic failover on rate limits, Anthropic prompt-caching for repeated queries ([technical guide](https://www.secoda.co/blog/ultimate-technical-guide-secoda-ai)). RAG pipeline: query-intent classification → hybrid keyword + fine-tuned-sentence-transformer semantic retrieval → context assembly (descriptions/owners/tags/usage/lineage) → grounded generation with mandatory source citation. **Verification-first prompting** forces the model to confirm an asset exists before claiming facts about it; **real SQL execution/metadata lookups** replace assumption-based answers; **progressive validation** builds multi-step queries incrementally, testing intermediate assumptions. Custom retrieval embeddings are trained on a **synthetic** dataset mimicking real Secoda usage patterns specifically to avoid training on customer data.
- **Human-in-the-loop**: users tag responses with sentiment + quality-indicator feedback (accuracy, staleness) logged for ongoing model evaluation; a 10-message session warning nudges users toward human verification as conversational context drift risk rises; planned "AI automation blocks" will formalize human-review/approval steps inside automations.
- **Deployment**: genuine **self-hosted** deployment — all services shipped as Docker-compatible container images, deployable via AWS ECS, Kubernetes (EKS/GKE/generic Helm), or Docker Compose for trials ([self-hosted docs](https://docs.secoda.co/enterprise/self-hosted-secoda)) — a real differentiator for banks requiring in-VPC or air-gapped-adjacent deployment (though explicit air-gap/FedRAMP-equivalent certification isn't documented).
- **MCP server**: 8 tools — `search_data_assets`, `search`, `run_sql` (live warehouse query execution through the agent), `retrieve_entity`, `entity_lineage`, `glossary`, `get_secoda_docs`, `chart` — auth via workspace API token, respects existing workspace/AI permission scoping ([MCP docs](https://docs.secoda.co/features/ai-assistant/secoda-mcp-server)).

**Take / Leave:** Take verification-first prompting (model must confirm an asset exists before describing it) and synthetic-data-trained retrieval embeddings (avoids ever training on customer metadata) as mandatory design patterns for our own generation pipeline. Leave the `run_sql`-via-MCP tool as a security question, not a template — live query execution through an MCP tool needs the same ABAC-style enforcement as Section C, not just workspace-token auth.

### Select Star
- **Auto-documentation**: uses **OpenAI and Anthropic** models to generate table/column/dashboard/chart descriptions, drawing on schema structure, associated SQL, existing docs, dashboard/chart titles+formulas, and **downstream propagation patterns** (how a column's meaning flows through lineage) ([docs](https://docs.selectstar.com/features/auto-documentation)). UX: click-to-generate per field or **bulk-generate** across all undocumented columns in a table with one-click bulk removal; suggested text renders in gray, human-confirmed text in black — a clean visual provenance signal for what's AI-authored vs. human-verified.
- **Semantic layer / Open Semantic Interchange**: Select Star is a **launch partner in Snowflake's Open Semantic Interchange (OSI)** initiative ([resource page](https://www.selectstar.com/resources/snowflake-ai-ready-semantic-model)) — reverse-engineers existing Looker/Tableau/Power BI dashboards into governed, vendor-neutral OSI semantic models (5-step: Connect → Scan → Reverse-engineer → Govern → Publish), then publishes them to Snowflake Cortex Analyst, ChatGPT, or Claude.
- **Security/compliance**: **SOC 2 Type II** certified since May 2021 (Security/Confidentiality/Availability, no exceptions), encryption in transit/at rest, third-party pentesting ([security page](https://www.selectstar.com/security-compliance)). By default **never reads data values or executes queries against source data** — metadata-only ingestion; PII columns can be tagged and are automatically stripped from query logs before any AI processing. Deployment is **SaaS-only on AWS** — no VPC/on-prem option documented. Data-deletion SLA: 10 business days.
- **MCP server**: exposes metadata/catalog (datasets, dashboards, fields, metrics), lineage graphs, semantic-layer/glossary terms, and ownership/popularity context to Cursor, Claude Desktop, VS Code Copilot via one API; listed on **AWS Marketplace's dedicated AI Agents and Tools category** (July 2025) ([AWS listing](https://aws.amazon.com/marketplace/pp/prodview-fdyk6m33drfp2), [product page](https://www.selectstar.com/product/mcp-for-data)).

**Take / Leave:** Take the gray-vs-black text provenance UX directly — it's the cheapest, clearest way to show "AI draft, unconfirmed" vs. "human verified" inline, and take the metadata-only-by-default posture (never read data values without explicit opt-in) as a hard requirement for anything touching a bank's regulated data. Leave Select Star's SaaS-only deployment model as unsuitable for a bank as-is; if we studied their generation pipeline we would still need Secoda-style self-hosting.

---

## Cross-Vendor Comparison Table

| Dimension | Alation | Microsoft Purview | Databricks Unity Catalog | Secoda | Select Star |
|---|---|---|---|---|---|
| **Auto-documentation** | Documentation Agent auto-ingests/organizes docs, connects to assets; BAE-driven suggestions | AI-suggested DQ/profiling columns only; no general asset-description generation | None (relies on comments/tags; Genie curates *query* knowledge, not asset docs) | Default-path AI generation with multi-model routing, verification-first prompting, 90% time-reduction claim | Default-path AI generation from schema+SQL+lineage+BI titles, bulk-generate, visual AI-vs-human provenance |
| **Wiki/knowledge-base generation** | Articles + Article Groups + Document Hubs + Templates/Custom Fields — deep, structured, permissioned wiki layer (mature, not AI-native yet) | None (Unified Catalog glossary is structured metadata, not freeform wiki) | None (metric views define semantics, not prose docs) | Field-level AI docs; no dedicated freeform wiki/hub construct | Field-level AI docs; no dedicated freeform wiki/hub construct |
| **Lineage automation depth** | Query-log + connector + OpenLineage inferred; table & column level; needs config per version (V2/V3) | Connector-based (ADF/Synapse/Power BI/dbt/Airflow); well-documented gaps (Databricks-Power BI, Synapse Dedicated Pools) | Native runtime capture (Spark-level), table+column, queryable as system tables; Databricks-workload-only | Ingested/inferred from connected sources; standard depth | Ingested/inferred; explicitly feeds auto-doc generation via propagation analysis |
| **Tool/agent generation** | Data Products Builder Agent (no-code, ODPS-based, agent+human consumable) | Security Copilot agents (compliance/DLP-focused, not data-analyst-focused) | UC functions as governed agent tools; Agent Bricks Supervisor orchestration | MCP tools incl. live `run_sql` execution | MCP read/search tools (no live query execution exposed) |
| **MCP maturity** | First-party OSS SDK + MCP server (STDIO/HTTP), but churning fast (breaking changes each minor version) | No first-party MCP for governance/catalog (Copilot agents are SCU-based, separate stack) | First-party Managed MCP (Genie, AI Search, UC Functions), auto-inherits UC permissions | First-party MCP, 8 tools, workspace-token auth | First-party MCP, AWS-Marketplace-listed, IDE-native |
| **Access-model sophistication** | RBAC (7 roles) + TrustCheck flags; policy enforcement mostly catalog-side | 3-tier RBAC (Tenant/Catalog/Domain) + policy-cascading glossary terms; dual-permission friction for stewards | ABAC: tag-driven row filters/column masks/dynamic GRANTs enforced at query-engine level, auto-applies to future objects | Metadata-only by default; no data-value access; workspace-scoped | Metadata-only by default; explicit PII-log-scrubbing; workspace-scoped |
| **Deployment flexibility for a bank** | Alation Cloud Service (SaaS) + on-prem/self-managed historically supported | Azure-native SaaS only (consumption-priced) — tied to Azure region/tenant | Multi-cloud (AWS/Azure/GCP) SaaS control plane; OSS UC for self-hosted/open catalog protocol needs | Self-hosted via Docker/K8s/ECS/Helm — most flexible for VPC/air-gap-leaning banks | SaaS-only on AWS, no VPC/on-prem option — least flexible |

---

## Screenshot targets (not yet captured)

1. [Alation Agentic Data Intelligence Platform product page](https://www.alation.com/product/agentic-data-intelligence-platform/) — dashboard/module overview screenshots.
2. [Alation AIOS blog](https://www.alation.com/blog/introducing-aios-alation-intelligence-operating-system/) — AIOS architecture diagram, agent-context-data-governance visual.
3. [Alation Compose product page](https://www.alation.com/product/compose/) — SQL editor with trust indicators UI.
4. [Alation Data Quality Agent product page](https://www.alation.com/product/data-quality-agent/) — quality score dashboard, check-status UI.
5. [Alation Data Products Marketplace product page](https://www.alation.com/product/data-products-marketplace/) — marketplace browsing UI.
6. [Alation TrustCheck best-practices doc](https://docs.alation.com/en/latest/welcome/BestPractices/UseTrustFlagstoProceedwithConfidence.html) — trust-flag iconography in context.
7. [Microsoft Purview Unified Catalog overview](https://learn.microsoft.com/en-us/purview/unified-catalog) — governance domain / data product screenshots.
8. [Governance Domains in Unified Catalog docs](https://learn.microsoft.com/en-us/purview/unified-catalog-governance-domains) — domain creation UI.
9. [Data Quality in Unified Catalog docs](https://learn.microsoft.com/en-us/purview/unified-catalog-data-quality) — rule builder, scoring UI.
10. [Health Management Actions page docs](https://learn.microsoft.com/en-us/purview/unified-catalog-data-health-management-actions-page) — actions/Kanban-style remediation UI.
11. [Security Copilot Agents in Purview overview](https://learn.microsoft.com/en-us/purview/copilot-in-purview-agents-overview) — DLP triage agent chat/config UI.
12. [Databricks Unity Catalog "What is Unity Catalog" docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/) — Catalog Explorer three-level namespace UI.
13. [Unity Catalog Lineage docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-lineage) — lineage graph visualization.
14. [Unity Catalog ABAC blog (GA announcement)](https://www.databricks.com/blog/abac-row-filtering-and-column-masking-policies-governed-tags-and-data-classification-are-now) — policy editor UI, governed tags UI.
15. [AI/BI Genie GA blog](https://www.databricks.com/blog/aibi-genie-now-generally-available) — Genie chat interface, trusted-assets/benchmark UI.
16. [Unity Catalog Metric Views docs](https://docs.databricks.com/aws/en/uc-semantics/metric-views/) — metric view definition UI.
17. [Agent Bricks: governed enterprise agent platform blog](https://www.databricks.com/blog/agent-bricks-governed-enterprise-agent-platform) — Supervisor Agent orchestration UI.
18. [Secoda documentation product page](https://www.secoda.co/documentation) — AI-generated description UI, before/after.
19. [Secoda data catalog product page](https://www.secoda.co/data-catalog) — catalog search/browse UI.
20. [Select Star automated data catalog product page](https://www.selectstar.com/product/data-catalog) — lineage graph + gray/black provenance text UI.

---

## What to steal

- **Alation's four-layer wiki model**: Document Hub (permission-scoped space) -> Article (generation-agnostic content primitive, asset-attached or standalone) -> Article Group (orthogonal taxonomy) -> Template with typed custom fields (rich text, reference, people-set, multi-select, object-set) and per-field permission overrides. This is the schema for our auto-compiled wiki.
- **Databricks ABAC verbatim**: governed tags with hierarchical inheritance (column-level exempted, forcing explicit classification), policies bound to tag conditions via `has_tag()`/`MATCH COLUMNS` rather than object enumeration, and identity-attribute/context-attribute condition functions for "user attribute must match row/column attribute" logic.
- **Genie's two-tier trust model**: exact-match, verified, parameterized "trusted asset" functions labeled Trusted at response time, vs. paraphrase-guided fresh SQL generation labeled unlabeled/lower-confidence — give users an honest per-answer confidence signal, don't uniformly badge all agent-generated SQL the same way.
- **Genie's automated knowledge mining + in-session extraction loop**: mine lineage/query history for candidate instructions; extract knowledge snippets from live chat sessions for admin approval — minimizes manual curation burden.
- **Genie Benchmarks**: a persistent suite of question->expected-SQL pairs as the pre-production and regression gate for our own SQL agent, with an explicit accuracy threshold before go-live.
- **Purview's policy-cascading glossary term**: attaching governance policy to a business-concept tag so it propagates automatically to every data product carrying that tag.
- **Secoda's verification-first prompting and synthetic-data-trained embeddings**: never let the generator assert a fact about an asset it hasn't confirmed exists; never train retrieval models on real customer metadata.
- **Select Star's gray/black text provenance convention**: cheapest possible UI signal for AI-draft vs. human-confirmed content, applied at the field level.
- **Secoda's self-hosted container deployment model** (Docker/ECS/K8s/Helm) as the baseline for anything we ship that a bank must run inside its own network boundary.
- **Alation's "show the steward the downstream behavioral effect of their curation"** feedback loop, and the "same access-request workflow regardless of whether the requester is human or agent" pattern from Alation's Data Products Marketplace.

## What to refuse

- **Purview's dual-permission steward friction** (a domain role plus a separate Data Map role required for one action) and its two-overlapping-catalog problem (Purview vs. Fabric OneLake) inside its own stack.
- **Purview's vCore/Capacity-Unit pricing opacity** — practitioner threads show real forecasting difficulty; don't build an internal cost model with that unpredictability.
- **Alation's MCP/Agent SDK version churn** (breaking changes every minor release, deprecated tools) — pin and stabilize any equivalent surface we ship.
- **Select Star's SaaS-only, no-VPC deployment model** — a non-starter for bank-internal data as our own baseline.
- **The assumption that connector-stitched, cross-platform lineage "just works"** — Purview's Databricks-Power BI and Synapse gaps show this fails in practice; treat cross-platform lineage as needing explicit reconciliation tooling, not a connector checkbox.
- **Genie's and Purview's DQ engines' hard caps** (200 rules/asset/scan, 100-instruction budget) as literal numbers to copy — the *pattern* of bounded curation is good, the specific vendor-tuned limits are not.
- **Treating any vendor's MCP tool surface as safe by default** — Secoda's `run_sql`-over-MCP and similar live-execution tools need the same ABAC-grade enforcement as Section C's Databricks policies, not workspace-token auth alone.
- **Databricks Genie and Unity Catalog ABAC's implicit assumption that governance stops at the platform boundary** — don't inherit an architecture where our own agent's guarantees silently stop working the moment a query touches a non-native system.
