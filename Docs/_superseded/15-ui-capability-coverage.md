# 15 — Atlas UI Capability Coverage

## Purpose

This ledger distinguishes capabilities available in Atlas from API-only administration and deployment controls. “API implemented” does not mean “UI complete,” and a governance approval does not mean a production integration is active.

## Current Atlas coverage

| Capability | Atlas UI | Scope visible or actionable now | Remaining UI or external boundary |
|---|---|---|---|
| AI analyst | Covered | Questions, pre-retrieval prompt-risk preview with version/score/reason codes, fail-closed blocked plans, governed metadata retrieval, deterministic SQL validation, tool-first execution, optional approved OpenAI/Gemini SQL generation, masking, column lineage, `SCREENED` execution trace and history | Enabling a bank-approved route and credential remains deployment-controlled; multilingual/indirect-injection certification remains a model-risk activity |
| Agent feedback and evaluations | Covered | Helpful/incorrect feedback, repeatable control evaluation runs and findings, and value-free query-memory eligibility/suppression inspection | Benchmark corpus management remains an engineering/model-risk workflow |
| Knowledge graph | Covered for bounded source neighborhoods | Server-side table/schema/catalog search; selectable table nodes; declared and suggested edges; confidence, direction, hop depth, inbound/outbound counts and value-free evidence; one-to-four-hop focus expansion; focus history; zoom; metadata/classification/profile/impact inspector; filters and checker decisions | Cross-source lineage traversal, time/version comparison, saved persona perspectives and million-node rendering virtualization/certification |
| Relationship suggestion | Covered | Discovery, enriched source/target column names, confidence, evidence and maker-checker approval/rejection | Composite/statistical suggestions require approved evidence policy |
| Query lineage | Covered for executed SELECTs | Referenced columns and output-to-source direct/derived lineage on results | Historical lineage search, view/procedure and ETL/OpenLineage views |
| dbt transformations | Covered for manifest artifacts | Register project-to-warehouse ownership, upload `manifest.json`, inspect immutable imports, catalog coverage, models/sources/tests, literal-redacted compiled SQL and dependency lineage | CI/dbt Cloud push, `run_results.json`, column lineage and large-DAG virtualization require the next domain/API increment |
| Business meaning | Covered for metadata-structure inference | Run deterministic/optional approved-model inference; inspect domains, entities, descriptions, table roles, grain, synonyms, suggested questions, confidence and evidence; approve through the common checker queue; browse authoritative annotations and cross-domain FK relationships; promote an approved blueprint to a deterministic governed-tool draft | Steward assignment, glossary/conflict lifecycle, bulk review, very-large-estate prioritization and approved-model UX certification |
| Model route governance | Covered | Immutable versions; OpenAI, Gemini, and private provider choices; model/endpoint alias; residency; retention; capabilities; budgets; credential-reference presence; maker-checker approval; adapter availability; and activation posture | Route selection, credentials, private endpoints and bank workload identity are intentionally deployment-only |
| AI/security runtime posture | Covered | Hybrid runtime, deterministic gates, model-route status, identity verification and credential-provider readiness | Bank production configuration remains external |
| Source fleet and enterprise ingestion | Covered for envelope 1.0, durable chunks, PostgreSQL and SQL Server pull | Tenant inventory, source operations and policy; honest capability matrix and certification; synchronous push; durable manifest creation, checksum/idempotent chunk upload, received/processed progress, Temporal finalization/retry polling, full-snapshot warning, payload-free chunk evidence and history | Remaining adapters, bulk onboarding, signed producers, Kafka/schema-registry intake, batch pause/cancel, connector settings and bank-scale UX/load certification |
| Data quality | Covered for profile-baseline controls | Source selection, current coverage/score/incident/scan-age posture, source-default threshold authoring, explicit source-freshness boundary, incident filters and evidence detail, audited acknowledge/resolve workflow, and immutable observation history | Table-policy bulk authoring, approved watermark configuration, notification routing, seasonality/custom rules and bank-scale incident virtualization |
| Analysis runs | Covered | Organization history, filters, evidence detail, inventory/drift evidence, cancellation and resume actions | Task-level retry/heartbeat drill-down after a task-evidence API is added |
| Metadata explorer | Covered | Tables, columns, classification, constraints, safe profiles, impact and the approved business annotation (domain, entity, role, grain, synonyms, version/confidence) | Index/partition and cross-source search after those APIs exist |
| Semantic governance | Covered for the implemented metric contract | Create/clone model versions, compose physical metrics, inspect definitions, submit and checker decision; approved business annotations are visible in the separate Business meaning workbench | Dimension authoring, glossary binding and conflict functions require broader contracts |
| Governed tools | Covered | Author and version typed parameter contracts, inspect SQL/roles/evidence, submit, approve, execute and request deprecation | Multi-tool plan authoring and formal certification packs remain platform roadmap items |
| Governance queue | Covered | Filtered cross-object queue including business metadata proposals, decision context, rationale capture and independent approve/reject actions | Bulk assignment and policy-specific evidence schemas can enrich the common detail panel |
| Audit evidence | Covered | Organization ledger with action, resource and correlation filters plus full bounded detail | WORM/SIEM retention and export are enterprise integrations |
| Tenant onboarding | Covered | Guided organization, LOB, project and credential-reference-only datasource onboarding with immediate connection verification | Bulk onboarding and entitlement feeds remain enterprise integrations |
| Outbox exception handling | Covered | Tenant-scoped event inventory excludes payloads; filters, retry evidence and authorized dead-letter requeue are actionable | Broker/SIEM deep links are deployment integrations |
| Identity, secrets and private networking | Deployment-only by design | Runtime posture is visible | OIDC issuer/claim mappings, secret adapters, private endpoints and credentials must not be changed from an analyst portal |
| Production topology, DR and certification | Not a portal function | Delivery status exposes the gate | IaC, recovery, penetration, scale and regional certification evidence |

## Coverage conclusion

Atlas now covers every user-facing API workflow implemented in the current PostgreSQL connector slice: onboarding, governed analysis, catalog/impact, dbt transformation evidence, business-semantic inference/review/map/tool promotion, semantic metric authoring and clone, governed tool lifecycle, bounded Graph Explorer search/focus/expansion/evidence inspection, profile-baseline data quality and incident operations, model-route governance, source operations, query memory, event exceptions, execution evidence, audit search and maker-checker decisions. Deployment credentials, model activation, identity mappings, networking, DR and certifications intentionally remain outside the portal.

## Asset-first redesign

The R22 portal redesign changes Atlas from a capability-first console into an asset-first intelligence workspace without weakening the governed API boundaries. It introduces a lighter grouped product shell, a discovery-oriented home experience, a filterable three-pane asset explorer, and a unified table workspace with Overview, Columns, Lineage, Intelligence, and Data quality tabs. Existing business annotations, profile evidence, constraints, impact, semantic metrics, tools, quality incidents, and graph navigation are reused rather than copied into a second source of truth.

The following Atlan-style product functions require new durable contracts and are not represented as working controls until those contracts exist:

| Product function | Current evidence | Required backend increment |
|---|---|---|
| Glossary and term lifecycle | Approved table-level business annotations | Durable terms, categories, ownership, versioning, conflicts, links and review policy |
| Aliases and rich asset documentation | Technical descriptions and inferred business names | Versioned aliases/README content, suggestion provenance, Apply/Discard and feedback |
| Saved filters, favorites and collections | Client-side search and persona filtering | User preferences, collection definitions, membership rules and sharing policy |
| Cross-source universal search | Loaded-source command palette and bounded graph search | Tenant-scoped indexed search over tables, columns, metrics, tools, terms and dbt resources |
| Unified activity timeline | Separate audit, analysis, quality and review evidence | Asset-scoped event projection with bounded retention and authorization |
| AI model and application inventory | Governed model route versions and evaluations | Model/application entities, versions, ownership, use cases, risk links and usage evidence |
| Agent collections studio | Runtime controls, agent runs and evaluations | Governed collection specifications, scheduled evaluation, health scoring and lifecycle review |

## Next product increment

R22 delivers the asset-first portal on top of the resumable ingestion, SQL Server, Oracle, semantic-inference, quality and graph slices. The next work must add durable product contracts and certified scale rather than decorative screens:

1. Oracle is delivered (`BETA`, `explain=False` pending EXPLAIN PLAN privilege review); add BigQuery next as the following native enterprise connector on the same contract, then continue with the remaining priority connector set and executable vendor/version certification fixtures.
2. Add connector-approved freshness watermarks, quality ownership/escalation, custom rules and notification routing; then certify induced anomaly and recovery behavior at scale.
3. Add glossary lifecycle, steward ownership, versioned asset documentation, aliases and ambiguity/conflict review before presenting those controls in the asset workspace.
4. Add tenant-scoped indexed universal search plus durable favorites, saved filters and governed collections.
5. Add vector and graph expansion to hybrid retrieval with large-catalog benchmarks.
6. Connect the bounded traversal contract to transitive lineage, ETL/OpenLineage ingestion, saved perspectives and virtualized cross-source rendering.
7. Bind the client-side persona navigation (Analyst, Steward, Platform operator, Auditor) to the bank's approved OIDC group contract so persona is derived from identity rather than a local browser choice, and complete accessibility validation.
