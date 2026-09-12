# Surface-to-control matrix

**Generated file. Do not edit by hand.**
Regenerate with `python scripts/generate_surface_control_matrix.py`;
`--check` fails when this file is out of date with the application.

Review 2026-09-05, section 6 item 1 asks for every REST / MCP / export /
bulk / job / SDK surface to be mapped to its tenant and workspace checks,
required roles, side effects, audit behaviour and cancellation behaviour --
and for that coverage to be *auditable*. Every cell below is derived from
the live FastAPI application and from a static walk of the handler call
graph. Nothing here is hand-maintained.

## What a cell means

- **Required roles** -- the role tuple in the `require_roles` dependency
  FastAPI actually wired into the route. `none declared` means the route
  carries no such dependency and is gated some other way (identity only,
  or a role check inside the handler body so the denial can be audited
  before the 403).
- **Tenant check** -- the handler's call graph reaches an
  `organization_id` boundary reference.
- **Workspace check** -- the handler's call graph reaches the workspace
  authorization gate (`authorization_gate.gate` / `gate_read` /
  `resolve_workspace` / `policy_engine.authorize_enforced`).
- **Side effects** -- `writes` when the call graph reaches a session
  write; `mutating verb, no write found` flags a POST/PUT/PATCH/DELETE
  whose write the walker could not see, which is a finding, not a pass.
- **Writes audit** -- the handler *can reach* `record_audit`. It does not
  prove every path through the handler audits; the INV-7 suite's
  per-route persistence tests are what prove that.
- **Cancellation** -- `cooperative` when the call graph reaches a
  pause/cancel checkpoint; `not cancellable` otherwise. Most
  request-path surfaces are short-lived and legitimately not cancellable.

## What this analysis cannot see

- A control enforced by data rather than by a call (a row-level filter, a
  policy row) is invisible to a call-graph walk.
- A control reached through dynamic dispatch (a registry, `getattr`, a
  handler looked up by string) is not followed.
- `unknown` is emitted wherever the analysis could not determine a cell.
  Those rows are listed in full below rather than dropped.

## Coverage

- Surfaces covered: **470**
- By family: BULK 11, EXPORT 5, JOB 25, MCP 9, REST 419, SDK 1
- Rows with at least one `unknown` cell: **1**
- `unknown` cells in total: **6**

### Gap list -- surfaces the analysis could not fully determine

| Surface | Undetermined cells |
|---|---|
| `MCP ping` | roles, tenant, workspace, side effects, audit, cancellation |

## Matrix

| Surface | Family | Handler | Required roles | Tenant check | Workspace check | Side effects | Writes audit | Cancellation |
|---|---|---|---|---|---|---|---|---|
| `GET /v1/organizations/{organization_id}/stewardship/bulk-operations` | BULK | `aida.stewardship_api.list_bulk_stewardship_operations` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `POST /v1/governance/reviews/bulk-decision` | BULK | `aida.semantic_api.bulk_decide_governance_reviews` | DataSteward, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/lineage/parsed-edges/bulk-decide` | BULK | `aida.parsed_lineage_review_api.bulk_decide_parsed_lineage_edges` | DataSteward, MetadataReviewer, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/stewardship/bulk-operations` | BULK | `aida.stewardship_api.create_bulk_stewardship_operation` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/tables/bulk-certify` | BULK | `atlas.modules.catalog.router.bulk_certify_tables` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/tables/bulk-classify` | BULK | `atlas.modules.catalog.router.bulk_classify_columns` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/tables/bulk-own` | BULK | `atlas.modules.catalog.router.bulk_assign_ownership` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/tables/bulk-tag` | BULK | `atlas.modules.catalog.router.bulk_tag_tables` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/ownership-assignments/bulk-reaffirm` | BULK | `aida.stewardship_api.bulk_reaffirm_ownership_assignments` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/datasources/bulk-onboard` | BULK | `atlas.modules.connectivity.router.bulk_onboard_datasources` | DataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/relationship-candidates/bulk-decision` | BULK | `aida.intelligence_api.bulk_decide_relationship_candidates` | DataSteward, MetadataReviewer, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `GET /v1/compliance/packs/{pack_id}/download` | EXPORT | `aida.compliance_api.download_compliance_pack` | ComplianceOfficer, DataSteward, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/context-product-versions/{version_id}/compile/download` | EXPORT | `aida.context_compiler_api.download_context_compilation` | AgentDeveloper, Analyst, DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `GET /v1/datasources/{datasource_id}/model/export.xlsx` | EXPORT | `aida.model_export_api.export_datasource_model` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/unified-lineage/impact/{node_id}/export` | EXPORT | `aida.lineage_evidence_export_api.export_unified_lineage_impact` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/metadata/tables/{table_id}/evidence/export` | EXPORT | `aida.asset_evidence_api.export_asset_evidence` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/agent-runs/{agent_run_id}/grounding-receipts` | JOB | `aida.api.get_agent_run_grounding_receipts` | AgentDeveloper, Analyst, Auditor, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/agent-runs/{agent_run_id}` | JOB | `aida.api.get_agent_run` | AgentDeveloper, Analyst, Auditor, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/ai/runtime-status` | JOB | `aida.api.ai_runtime_status` | AgentDeveloper, Analyst, Auditor, PlatformAdmin, Viewer | no | no | read | no | not cancellable |
| `GET /v1/analysis-runs/{run_id}/tasks/{task_id}` | JOB | `aida.api.get_analysis_run_task` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/analysis-runs/{run_id}/tasks` | JOB | `aida.api.list_analysis_run_tasks` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/analysis-runs/{run_id}` | JOB | `aida.api.get_analysis_run` | DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/agent-runs` | JOB | `aida.api.list_agent_runs` | AgentDeveloper, Analyst, Auditor, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/analysis-runs` | JOB | `aida.api.list_analysis_runs` | DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/semantic-inference-runs` | JOB | `aida.semantic_intelligence_api.list_semantic_inference_runs` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/analysis-runs` | JOB | `aida.operational_api.list_organization_analysis_runs` | DataAdmin, MetadataAdmin, Operations, OrganizationAdmin, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/tools/{tool_id}/certification-runs` | JOB | `aida.tool_api.list_tool_certification_runs` | AgentDeveloper, Analyst, Auditor, PlatformAdmin, Reviewer, SemanticAdmin, ToolDeveloper, Viewer | yes | no | read | no | not cancellable |
| `POST /v1/agent-runs/{run_id}/tool-blueprint` | JOB | `aida.tool_api.prepare_analysis_tool` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | no | writes | yes | not cancellable |
| `POST /v1/analysis-runs/{analysis_run_id}/quality-evaluation` | JOB | `aida.quality_api.replay_quality_evaluation` | DataAdmin, DataSteward, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/analysis-runs/{run_id}/cancel` | JOB | `aida.api.cancel_analysis_run` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/analysis-runs/{run_id}/resume` | JOB | `aida.api.resume_analysis_run` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/analysis-runs` | JOB | `aida.api.create_analysis_run` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/semantic-inference-runs` | JOB | `aida.semantic_intelligence_api.create_semantic_inference_run` | DataAdmin, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/lineage-agent/run` | JOB | `aida.lineage_agent_api.start_lineage_agent_run` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/quality-agent/run` | JOB | `aida.quality_agent_api.start_quality_agent_run` | DataAdmin, DataSteward, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/reviewer-agent/run` | JOB | `aida.agent_contract_api.run_reviewer_agent` | MetadataReviewer, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/steward-agent/run` | JOB | `aida.steward_agent_api.start_steward_agent_run` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/playbooks/{playbook_id}/run` | JOB | `aida.playbooks_api.run_playbook_now` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/tool-certification-runs/{run_id}/decision` | JOB | `aida.tool_api.decide_tool_certification` | PlatformAdmin, Reviewer, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/tool-versions/{version_id}/certification-runs` | JOB | `aida.tool_api.execute_tool_certification` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | no | writes | yes | not cancellable |
| `PUT /v1/agent-runs/{agent_run_id}/feedback` | JOB | `aida.intelligence_api.upsert_query_feedback` | AgentDeveloper, Analyst, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `MCP initialize` | MCP | `aida.mcp_server._handle_initialize` | per-tool role eligibility (see `_tool_role_eligible`) | no | no | read | no | not cancellable |
| `MCP ping` | MCP | `unknown` | unknown | unknown | unknown | unknown | unknown | unknown |
| `MCP prompts/get` | MCP | `aida.mcp_server._handle_prompts_get` | per-tool role eligibility (see `_tool_role_eligible`) | yes | yes | writes | yes | not cancellable |
| `MCP prompts/list` | MCP | `aida.mcp_server._handle_prompts_list` | per-tool role eligibility (see `_tool_role_eligible`) | yes | yes | writes | yes | not cancellable |
| `MCP resources/list` | MCP | `aida.mcp_server._handle_resources_list` | per-tool role eligibility (see `_tool_role_eligible`) | yes | yes | writes | yes | not cancellable |
| `MCP resources/read` | MCP | `aida.mcp_server._handle_resources_read` | per-tool role eligibility (see `_tool_role_eligible`) | yes | yes | writes | yes | not cancellable |
| `MCP tools/call` | MCP | `aida.mcp_server._handle_tools_call` | per-tool role eligibility (see `_tool_role_eligible`) | yes | yes | writes | yes | cooperative |
| `MCP tools/list` | MCP | `aida.mcp_server._handle_tools_list` | per-tool role eligibility (see `_tool_role_eligible`) | yes | yes | writes | yes | not cancellable |
| `POST /mcp` | MCP | `aida.mcp_server.mcp_endpoint` | none declared | yes | yes | writes | yes | cooperative |
| `DELETE /v1/asset-term-links/{link_id}` | REST | `aida.glossary_api.delete_asset_term_link` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `DELETE /v1/context-products/{product_id}/bindings/{consumer_principal_id}` | REST | `aida.context_product_api.delete_context_product_consumer_binding` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `DELETE /v1/notification-rules/{rule_id}` | REST | `aida.notification_api.delete_notification_rule` | DataAdmin, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `DELETE /v1/playbooks/{playbook_id}` | REST | `aida.playbooks_api.delete_playbook` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `DELETE /v1/quality-rule-packs/{rule_pack_id}` | REST | `aida.quality_api.delete_rule_pack` | DataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `DELETE /v1/quality-rules/{rule_id}` | REST | `aida.quality_api.delete_rule` | DataAdmin, DataSteward, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `DELETE /v1/studio/change-sets/{change_set_id}/items/{item_id}` | REST | `aida.studio_api.remove_item` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `DELETE /v1/term-semantic-bindings/{binding_id}` | REST | `aida.semantic_api.delete_term_semantic_binding` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `GET /api/v1/organizations/{organization_id}/consumption-lineage/by-consumer` | REST | `aida.consumption_lineage_api.list_consumption_by_consumer` | none declared | yes | no | read | no | not cancellable |
| `GET /api/v1/organizations/{organization_id}/consumption-lineage/by-resource` | REST | `aida.consumption_lineage_api.list_consumption_for_resource` | none declared | yes | no | read | no | not cancellable |
| `GET /api/v1/organizations/{organization_id}/consumption-lineage/graph` | REST | `aida.consumption_lineage_api.list_consumption_graph` | none declared | yes | no | read | no | not cancellable |
| `GET /health/live` | REST | `aida.main.liveness` | none declared | no | no | read | no | not cancellable |
| `GET /health/ready` | REST | `aida.main.readiness` | none declared | no | no | read | no | not cancellable |
| `GET /metrics` | REST | `aida.main.metrics` | none declared | no | no | read | no | not cancellable |
| `GET /v1/access-review/reports/{report_id}` | REST | `aida.access_review_api.get_entitlement_report` | none declared | yes | no | read | no | not cancellable |
| `GET /v1/access-review/reports` | REST | `aida.access_review_api.list_entitlement_reports` | none declared | yes | no | read | no | not cancellable |
| `GET /v1/agent-contract-requests/{request_id}` | REST | `aida.agent_contract_request_api.get_agent_contract_request` | AgentDeveloper, Auditor, DataSteward, ModelRiskManager, Operations, PlatformAdmin, Reviewer | yes | no | read | no | not cancellable |
| `GET /v1/ai-assessment-templates` | REST | `aida.ai_registry_api.list_ai_assessment_templates` | AgentDeveloper, Auditor, DataScientist, DataSteward, ModelRiskManager, PlatformAdmin, Reviewer, Viewer | no | no | read | no | not cancellable |
| `GET /v1/ai-asset-versions/{version_id}/dependencies` | REST | `aida.ai_registry_api.get_ai_dependency_graph` | AgentDeveloper, Auditor, DataScientist, DataSteward, ModelRiskManager, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/ai-asset-versions/{version_id}/eval-gate` | REST | `aida.ai_registry_api.get_agent_eval_gate` | AgentDeveloper, Auditor, DataScientist, DataSteward, ModelRiskManager, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/ai-asset-versions/{version_id}/remediations` | REST | `aida.ai_registry_api.list_ai_remediations` | AgentDeveloper, Auditor, DataScientist, DataSteward, ModelRiskManager, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/ai-asset-versions/{version_id}/trust-history` | REST | `aida.ai_registry_api.get_ai_asset_trust_history` | AgentDeveloper, Auditor, DataScientist, DataSteward, ModelRiskManager, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/ai-asset-versions/{version_id}/trust` | REST | `aida.ai_registry_api.get_ai_asset_trust` | AgentDeveloper, Auditor, DataScientist, DataSteward, ModelRiskManager, PlatformAdmin, Reviewer, Viewer | yes | no | writes | yes | not cancellable |
| `GET /v1/ai-decisions/asset/{asset_id}` | REST | `aida.ai_decision_lineage_api.get_asset_decisions` | Analyst, DataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/ai-decisions/refusals` | REST | `aida.ai_decision_lineage_api.list_refusals` | DataAdmin, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/ai-decisions/{run_id}` | REST | `aida.ai_decision_lineage_api.get_run_decisions` | Analyst, DataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/bi-artifact-imports/{artifact_id}/lineage` | REST | `aida.bi_api.get_bi_lineage` | Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/bi-artifact-imports/{artifact_id}/reports` | REST | `aida.bi_api.list_bi_reports` | Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/bi-connections/{connection_id}/artifact-imports` | REST | `aida.bi_api.list_bi_artifact_imports` | Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/business-nodes/{node_id}/rollup` | REST | `atlas.modules.identity_tenancy.router.get_rollup` | Analyst, DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer, Steward | yes | no | read | no | not cancellable |
| `GET /v1/compliance/packs/{pack_id}` | REST | `aida.compliance_api.get_compliance_pack` | ComplianceOfficer, DataSteward, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/compliance/packs` | REST | `aida.compliance_api.list_compliance_packs` | ComplianceOfficer, DataSteward, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/connectors/capability-matrix` | REST | `atlas.modules.ingestion.router.connector_capability_matrix` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | no | no | read | no | not cancellable |
| `GET /v1/context-product-versions/{version_id}/compile` | REST | `aida.context_compiler_api.compile_context_product_version` | AgentDeveloper, Analyst, DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `GET /v1/context-product-versions/{version_id}/scope` | REST | `aida.context_product_api.get_context_product_version_scope` | Analyst, Auditor, DataSteward, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/context-product-versions/{version_id}` | REST | `aida.context_product_api.get_context_product_version` | Analyst, Auditor, DataSteward, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | writes | yes | not cancellable |
| `GET /v1/context-products/{product_id}/bindings` | REST | `aida.context_product_api.list_context_product_consumer_bindings` | Auditor, DataSteward, PlatformAdmin, Reviewer, SemanticAdmin | yes | no | read | no | not cancellable |
| `GET /v1/context-products/{product_id}/versions` | REST | `aida.context_product_api.list_context_product_versions` | Analyst, Auditor, DataSteward, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/data-contracts/{contract_id}/sla-status` | REST | `aida.runtime_contracts_api.get_sla_status` | DataEngineer, DataSteward, PlatformAdmin, Viewer | yes | no | writes | yes | not cancellable |
| `GET /v1/data-contracts/{contract_id}/violations` | REST | `aida.runtime_contracts_api.list_contract_violations` | DataEngineer, DataSteward, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/data-domains/{domain_id}/cross-boundary-grants` | REST | `atlas.modules.identity_tenancy.router.list_cross_boundary_grants` | DataAdmin, OrganizationAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/data-domains/{domain_id}/unified-lineage/graph` | REST | `aida.unified_lineage_api.get_domain_unified_lineage_graph` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/data-products/{product_id}/contracts` | REST | `aida.product_marketplace_api.list_data_contracts` | Analyst, Auditor, DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/business-annotations` | REST | `aida.semantic_intelligence_api.list_business_annotations` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/canonical-table/resolve` | REST | `aida.intelligence_api.resolve_canonical_table` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/composite-key-candidates` | REST | `aida.composite_key_api.list_composite_key_candidates` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/connector-certifications` | REST | `atlas.modules.ingestion.router.list_connector_certifications` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/cross-source-object-resolution-candidates` | REST | `aida.intelligence_api.list_cross_source_object_resolution_candidates` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/freshness/{table_id}` | REST | `aida.quality_api.get_freshness_status` | Analyst, DataAdmin, DataSteward, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/freshness` | REST | `aida.quality_api.list_freshness_configs` | Analyst, DataAdmin, DataSteward, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/graph-summary` | REST | `aida.api.get_graph_summary` | Analyst, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/health` | REST | `aida.operational_api.get_datasource_health` | Analyst, DataAdmin, MetadataAdmin, Operations, OrganizationAdmin, PlatformAdmin, ProjectAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/knowledge-graph/neighborhood` | REST | `aida.intelligence_api.get_knowledge_graph_neighborhood` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/knowledge-graph/search` | REST | `aida.intelligence_api.search_knowledge_graph` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/knowledge-graph` | REST | `aida.intelligence_api.get_knowledge_graph` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/metadata-enrichment-proposals` | REST | `aida.semantic_intelligence_api.list_metadata_enrichment_proposals` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/metadata-ingestion-batches` | REST | `atlas.modules.ingestion.router.list_metadata_ingestion_batches` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/metadata-ingestions` | REST | `atlas.modules.ingestion.router.list_metadata_ingestions` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/model-imports` | REST | `aida.model_import_api.list_model_imports` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/openlineage-events` | REST | `aida.openlineage_api.list_openlineage_run_events` | Auditor, DataAdmin, DataSteward, MetadataAdmin, Operations, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/procedures/{routine_id}/lineage` | REST | `aida.procedure_lineage_api.list_deep_procedure_lineage` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/profiling-exception-policies` | REST | `aida.api.list_profiling_exception_policies` | DataAdmin, DataSteward, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/quality-incidents` | REST | `aida.quality_api.list_quality_incidents` | Analyst, DataAdmin, DataSteward, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/quality-observations` | REST | `aida.quality_api.list_quality_observations` | Analyst, DataAdmin, DataSteward, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/quality-policies` | REST | `aida.quality_api.list_quality_policies` | DataAdmin, DataSteward, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/quality-rule-packs` | REST | `aida.quality_api.list_rule_packs` | Analyst, DataAdmin, DataSteward, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/quality-summary` | REST | `aida.quality_api.quality_summary` | Analyst, DataAdmin, DataSteward, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/quality/external-signals` | REST | `aida.quality_api.list_external_quality_signals` | Analyst, DataAdmin, DataSteward, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/query-memory` | REST | `aida.intelligence_api.list_query_memory` | AgentDeveloper, Auditor, DataAdmin, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/relationship-candidates/composite` | REST | `aida.intelligence_api.list_composite_relationship_candidates` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/relationship-candidates/review-queue` | REST | `aida.intelligence_api.get_relationship_candidate_review_queue` | Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/relationship-candidates` | REST | `aida.intelligence_api.list_relationship_candidates` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/rename-candidates` | REST | `aida.intelligence_api.list_rename_candidates` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/scan-policy` | REST | `atlas.modules.connectivity.router.get_scan_policy` | DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/table-family-candidates` | REST | `aida.table_family_api.list_table_family_candidates_for_datasource` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/tables` | REST | `aida.api.list_tables` | Analyst, MetadataAdmin, PlatformAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/unified-lineage/graph` | REST | `aida.unified_lineage_api.get_unified_lineage_graph` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}/unified-lineage/impact/{node_id}` | REST | `aida.unified_lineage_api.get_unified_lineage_impact` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/datasources/{datasource_id}` | REST | `aida.operational_api.get_datasource` | Analyst, DataAdmin, MetadataAdmin, Operations, OrganizationAdmin, PlatformAdmin, ProjectAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/dbt-artifact-imports/{artifact_id}/lineage` | REST | `aida.dbt_api.get_dbt_lineage` | Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/dbt-artifact-imports/{artifact_id}/resources` | REST | `aida.dbt_api.list_dbt_resources` | Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/dbt-projects/{dbt_project_id}/artifact-imports` | REST | `aida.dbt_api.list_dbt_artifact_imports` | Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/descriptions/withdrawals` | REST | `aida.description_withdrawal_api.list_description_withdrawals` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/documents/{document_id}/claims` | REST | `aida.document_ingestion_api.list_document_claims` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/documents/{document_id}/mappings` | REST | `aida.document_ingestion_api.list_document_mappings` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/documents/{document_id}/sections` | REST | `aida.document_ingestion_api.list_document_sections` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/documents/{document_id}` | REST | `aida.document_ingestion_api.get_document` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/glossary-term-versions/{version_id}/consumers` | REST | `aida.glossary_api.get_glossary_term_version_consumers` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/glossary-terms/{term_id}/semantic-bindings` | REST | `aida.semantic_api.list_term_semantic_bindings` | Analyst, DataSteward, PlatformAdmin, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/governance/reviews/queue/summary` | REST | `aida.review_queue_api.get_review_queue_summary` | DataSteward, PlatformAdmin, Reviewer, SemanticAdmin | yes | no | read | no | not cancellable |
| `GET /v1/governance/reviews/queue` | REST | `aida.review_queue_api.get_review_queue` | DataSteward, PlatformAdmin, Reviewer, SemanticAdmin | yes | no | read | no | not cancellable |
| `GET /v1/governance/reviews/{review_id}/diff` | REST | `aida.semantic_api.get_governance_review_diff` | DataSteward, PlatformAdmin, Reviewer, SemanticAdmin | yes | no | read | no | not cancellable |
| `GET /v1/governance/reviews` | REST | `aida.semantic_api.list_governance_reviews` | DataSteward, PlatformAdmin, Reviewer, SemanticAdmin | yes | no | read | no | not cancellable |
| `GET /v1/lineage/parsed-edges/review-queue` | REST | `aida.parsed_lineage_review_api.get_parsed_lineage_review_queue` | DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/lines-of-business/{lob_id}/data-domains` | REST | `atlas.modules.identity_tenancy.router.list_data_domains` | DataAdmin, OrganizationAdmin, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/lines-of-business/{lob_id}/projects` | REST | `atlas.modules.identity_tenancy.router.list_projects` | DataAdmin, OrganizationAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/marketplace/access-requests` | REST | `aida.product_marketplace_api.list_marketplace_access_requests` | Analyst, Auditor, DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/marketplace/products/ask` | REST | `aida.marketplace_discovery.ask_marketplace` | Analyst, DataConsumer, DataScientist, PlatformAdmin, Viewer | yes | no | writes | yes | not cancellable |
| `GET /v1/marketplace/products` | REST | `aida.product_marketplace_api.search_marketplace` | Analyst, DataConsumer, DataScientist, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/me` | REST | `aida.persona_api.get_me` | none declared | yes | no | read | no | not cancellable |
| `GET /v1/metadata-ingestion-batches/{batch_id}/chunks` | REST | `atlas.modules.ingestion.router.list_metadata_ingestion_chunks` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/metadata-ingestion-batches/{batch_id}` | REST | `atlas.modules.ingestion.router.get_metadata_ingestion_batch` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/metadata/tables/{table_id}/business-annotation` | REST | `aida.semantic_intelligence_api.get_table_business_annotation` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/metadata/tables/{table_id}/documentation` | REST | `aida.glossary_api.get_asset_documentation` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/metadata/tables/{table_id}/evidence` | REST | `aida.asset_evidence_api.get_asset_evidence` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/metadata/tables/{table_id}/glossary-links` | REST | `aida.glossary_api.list_asset_term_links` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/metadata/tables/{table_id}/impact` | REST | `aida.intelligence_api.table_impact_analysis` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/model-imports/{batch_id}/changes` | REST | `aida.model_import_api.list_model_import_changes` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/negative-knowledge/search` | REST | `aida.negative_knowledge_api.search_negative_assertions` | DataEngineer, DataSteward, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/negative-knowledge/{subject_id}` | REST | `aida.negative_knowledge_api.get_subject_assertions` | DataEngineer, DataSteward, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/notification-rules` | REST | `aida.notification_api.list_notification_rules` | DataAdmin, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/notifications` | REST | `aida.notification_api.list_notifications` | DataAdmin, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/observability/archive/status` | REST | `atlas.modules.observability_audit.router.get_archive_status` | DataAdmin, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/observability/cost/showback` | REST | `atlas.modules.observability_audit.router.get_cost_showback` | ComplianceOfficer, DataAdmin, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/observability/slo/{slo_id}/budget` | REST | `atlas.modules.observability_audit.router.get_slo_budget` | DataAdmin, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/observability/slo` | REST | `atlas.modules.observability_audit.router.list_slo_definitions` | DataAdmin, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/openlineage-events/{event_id}` | REST | `aida.openlineage_api.get_openlineage_run_event` | Auditor, DataAdmin, DataSteward, MetadataAdmin, Operations, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/organizations/{organization_id}/access-policies` | REST | `atlas.modules.identity_tenancy.router.list_access_policies` | DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/agent-contract-requests` | REST | `aida.agent_contract_request_api.list_agent_contract_requests` | AgentDeveloper, Auditor, DataSteward, ModelRiskManager, Operations, PlatformAdmin, Reviewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/agent-evaluations` | REST | `atlas.modules.identity_tenancy.router.list_agent_evaluations` | AgentDeveloper, Auditor, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/agent-inbox` | REST | `aida.agent_contract_api.get_agent_inbox` | AgentDeveloper, Analyst, Auditor, DataSteward, MetadataReviewer, ModelRiskManager, Operations, PlatformAdmin, Reviewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/agent-tasks` | REST | `aida.agent_contract_api.list_agent_tasks` | AgentDeveloper, Auditor, DataSteward, ModelRiskManager, Operations, PlatformAdmin, Reviewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/agents/{ai_asset_version_id}/contract` | REST | `aida.agent_contract_api.get_agent_contract` | AgentDeveloper, Auditor, DataSteward, ModelRiskManager, Operations, PlatformAdmin, Reviewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/ai-agents/roster` | REST | `aida.agent_roster_api.get_agent_roster` | AgentDeveloper, Auditor, DataScientist, DataSteward, ModelRiskManager, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/ai-assets` | REST | `aida.ai_registry_api.list_ai_assets` | AgentDeveloper, Auditor, DataScientist, DataSteward, ModelRiskManager, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/asset-description-drafts` | REST | `aida.asset_description_api.list_asset_description_drafts` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/audit-events` | REST | `aida.operational_api.list_audit_events` | Auditor, Operations, OrganizationAdmin, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/business-map` | REST | `aida.semantic_intelligence_api.get_business_map` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/business-nodes` | REST | `atlas.modules.identity_tenancy.router.get_business_tree` | Analyst, DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer, Steward | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/catalog-bulk-actions/{run_id}` | REST | `atlas.modules.catalog.router.get_catalog_bulk_action_run` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/catalog-bulk-actions` | REST | `atlas.modules.catalog.router.list_catalog_bulk_action_runs` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/catalog/rows` | REST | `atlas.modules.catalog.router.list_catalog_rows` | Analyst, MetadataAdmin, PlatformAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/column-description-drafts` | REST | `aida.column_description_api.list_column_description_drafts` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/consumption-lineage/by-consumer` | REST | `aida.consumption_lineage_api.list_consumption_by_consumer` | none declared | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/consumption-lineage/by-resource` | REST | `aida.consumption_lineage_api.list_consumption_for_resource` | none declared | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/consumption-lineage/graph` | REST | `aida.consumption_lineage_api.list_consumption_graph` | none declared | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/data-domains` | REST | `aida.operational_api.list_organization_data_domains` | DataAdmin, Operations, OrganizationAdmin, PlatformAdmin, ProjectAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/datasources` | REST | `aida.operational_api.list_organization_datasources` | Analyst, DataAdmin, MetadataAdmin, Operations, OrganizationAdmin, PlatformAdmin, ProjectAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/delegations` | REST | `aida.delegation_api.list_delegations` | Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/fleet-health` | REST | `aida.operational_api.organization_fleet_health` | Auditor, Operations, OrganizationAdmin, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/fleet-summary` | REST | `aida.operational_api.fleet_summary` | Auditor, Operations, OrganizationAdmin, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/global-search` | REST | `aida.semantic_api.global_semantic_search` | Analyst, DataSteward, PlatformAdmin, SemanticAdmin, Viewer | yes | no | read | no | cooperative |
| `GET /v1/organizations/{organization_id}/glossary-categories` | REST | `aida.stewardship_api.list_glossary_categories` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/glossary-conflicts` | REST | `aida.stewardship_api.list_glossary_conflicts` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/glossary-link-proposals` | REST | `aida.stewardship_api.list_glossary_link_proposals` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/glossary-terms` | REST | `aida.glossary_api.list_glossary_terms` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/integration-policy` | REST | `aida.api.get_organization_integration_policy` | OrganizationAdmin, PlatformAdmin | yes | no | writes | no | not cancellable |
| `GET /v1/organizations/{organization_id}/kill-switch` | REST | `aida.ai_governance_api.list_kill_switch_state` | AgentDeveloper, Auditor, DataSteward, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/lineage-agent` | REST | `aida.lineage_agent_api.get_lineage_agent_state` | AgentDeveloper, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, ModelRiskManager, Operations, PlatformAdmin, Reviewer, SemanticAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/lines-of-business` | REST | `atlas.modules.identity_tenancy.router.list_lines_of_business` | DataAdmin, OrganizationAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/metric-conflicts` | REST | `aida.semantic_api.list_metric_formula_collisions` | Analyst, DataSteward, PlatformAdmin, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/metric-suggestions` | REST | `aida.metric_suggestion_api.list_metric_suggestion_proposals` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/model-routes` | REST | `aida.ai_governance_api.list_model_routes` | AgentDeveloper, Auditor, DataSteward, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/notifications/governance` | REST | `aida.retrieval_ops_api.list_governance_notifications` | Analyst, Auditor, DataSteward, MetadataAdmin, Operations, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/ontology-versions` | REST | `aida.ontology_api.list_ontology_versions` | DataSteward, MetadataAdmin, PlatformAdmin, Reviewer | yes | yes | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/outbox-events` | REST | `aida.operational_api.list_outbox_events` | Auditor, Operations, OrganizationAdmin, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/ownership-assignments` | REST | `aida.stewardship_api.list_ownership_assignments` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/ownership-rules` | REST | `aida.stewardship_api.list_ownership_rules` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/playbooks` | REST | `aida.playbooks_api.list_playbooks` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/portfolio-analytics/summary` | REST | `aida.product_marketplace_api.portfolio_analytics_summary` | Analyst, Auditor, DataProductOwner, DataSteward, MetadataAdmin, Operations, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/portfolio-analytics/trends` | REST | `aida.product_marketplace_api.portfolio_analytics_trends` | Analyst, Auditor, DataProductOwner, DataSteward, MetadataAdmin, Operations, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/projects` | REST | `aida.operational_api.list_organization_projects` | DataAdmin, Operations, OrganizationAdmin, PlatformAdmin, ProjectAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/quality-agent` | REST | `aida.quality_agent_api.get_quality_agent_state` | AgentDeveloper, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, ModelRiskManager, Operations, PlatformAdmin, Reviewer, SemanticAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/retrieval/vector-index` | REST | `aida.retrieval_ops_api.get_vector_index_status` | Analyst, Auditor, DataSteward, MetadataAdmin, Operations, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/reviewer-agent/disagreement-rates` | REST | `aida.agent_contract_api.get_disagreement_rates` | AgentDeveloper, Auditor, DataSteward, ModelRiskManager, Operations, PlatformAdmin, Reviewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/reviewer-agent/samples` | REST | `aida.agent_contract_api.list_audit_samples` | AgentDeveloper, Auditor, DataSteward, ModelRiskManager, Operations, PlatformAdmin, Reviewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/reviewer-agent` | REST | `aida.agent_contract_api.get_reviewer_agent_state` | AgentDeveloper, Auditor, DataSteward, ModelRiskManager, Operations, PlatformAdmin, Reviewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/steward-agent` | REST | `aida.steward_agent_api.get_steward_agent_state` | AgentDeveloper, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, ModelRiskManager, Operations, PlatformAdmin, Reviewer, SemanticAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/stewardship/coverage/snapshots` | REST | `aida.stewardship_api.list_stewardship_coverage_snapshots` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/stewardship/coverage` | REST | `aida.stewardship_api.get_stewardship_coverage` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/stewardship/documentation-worklist` | REST | `aida.stewardship_api.list_documentation_worklist` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/stewardship/unowned-backlog` | REST | `aida.stewardship_api.list_unowned_asset_backlog` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/tool-first-rate` | REST | `aida.operational_api.organization_tool_first_rate` | Auditor, Operations, OrganizationAdmin, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}/workspaces` | REST | `atlas.modules.identity_tenancy.router.list_workspaces` | Analyst, DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer, Steward | yes | no | read | no | not cancellable |
| `GET /v1/organizations/{organization_id}` | REST | `atlas.modules.identity_tenancy.router.get_organization` | Auditor, Operations, OrganizationAdmin, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/organizations` | REST | `atlas.modules.identity_tenancy.router.list_organizations` | Auditor, Operations, OrganizationAdmin, PlatformAdmin | no | no | read | no | not cancellable |
| `GET /v1/playbooks/{playbook_id}` | REST | `aida.playbooks_api.get_playbook` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/procedure-lineage/capability-matrix` | REST | `aida.procedure_lineage_api.get_procedure_lineage_capability_matrix` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | no | no | read | no | not cancellable |
| `GET /v1/projects/{project_id}/bi-connections` | REST | `aida.bi_api.list_bi_connections` | Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/projects/{project_id}/context-products` | REST | `aida.context_product_api.list_context_products` | Analyst, Auditor, DataSteward, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/projects/{project_id}/data-products` | REST | `aida.product_marketplace_api.list_data_products` | Analyst, Auditor, DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/projects/{project_id}/datasources` | REST | `atlas.modules.connectivity.router.list_datasources` | DataAdmin, OrganizationAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/projects/{project_id}/dbt-projects` | REST | `aida.dbt_api.list_dbt_projects` | Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | no | writes | no | not cancellable |
| `GET /v1/projects/{project_id}/documents` | REST | `aida.document_ingestion_api.list_documents` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/projects/{project_id}/semantic-model-versions` | REST | `aida.semantic_api.list_semantic_model_versions` | Analyst, DataSteward, PlatformAdmin, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/projects/{project_id}/tools` | REST | `aida.tool_api.list_tools` | AgentDeveloper, Analyst, PlatformAdmin, SemanticAdmin, ToolDeveloper, Viewer | yes | yes | writes | yes | not cancellable |
| `GET /v1/projects/{project_id}` | REST | `aida.operational_api.get_project` | DataAdmin, Operations, OrganizationAdmin, PlatformAdmin, ProjectAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/quality-incidents/{incident_id}/triage` | REST | `aida.quality_api.get_quality_incident_triage` | Analyst, DataAdmin, DataSteward, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/quality-rule-packs/{rule_pack_id}/rules` | REST | `aida.quality_api.list_rules` | Analyst, DataAdmin, DataSteward, Operations, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/query-executions/{execution_id}/lineage` | REST | `aida.api.get_query_lineage` | AgentDeveloper, Analyst, Auditor, MetadataAdmin, PlatformAdmin | yes | no | read | no | not cancellable |
| `GET /v1/relationship-candidates/confidence-calibration` | REST | `aida.intelligence_api.get_relationship_candidate_confidence_calibration` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/schemas/{schema_id}/table-family-candidates` | REST | `aida.table_family_api.list_table_family_candidates_for_schema` | Auditor, DataAdmin, MetadataAdmin, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/search/suggest` | REST | `aida.search_api.search_suggest` | Analyst, DataAdmin, DataSteward, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/search` | REST | `aida.search_api.global_search` | Analyst, DataAdmin, DataSteward, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/semantic-metric-versions/{version_id}/consumers` | REST | `aida.semantic_api.get_semantic_metric_version_consumers` | Analyst, DataSteward, PlatformAdmin, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/semantic-metrics/{metric_id}/glossary-bindings` | REST | `aida.semantic_api.list_metric_glossary_bindings` | Analyst, DataSteward, PlatformAdmin, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/semantic-model-versions/{model_id}/consumers` | REST | `aida.semantic_api.get_semantic_model_version_consumers` | Analyst, DataSteward, PlatformAdmin, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/semantic-model-versions/{model_id}/metrics` | REST | `aida.semantic_api.list_metric_versions` | Analyst, DataSteward, PlatformAdmin, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/studio/change-sets/{change_set_id}/context-product-materializations` | REST | `aida.studio_api.list_context_product_materializations` | Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/studio/change-sets/{change_set_id}/diff` | REST | `aida.studio_api.view_diff` | Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/studio/change-sets/{change_set_id}/eval` | REST | `aida.studio_api.get_latest_eval_run` | Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/studio/change-sets/{change_set_id}/impact` | REST | `aida.studio_api.impact_preview` | Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/studio/change-sets/{change_set_id}/items` | REST | `aida.studio_api.list_items` | Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/studio/change-sets/{change_set_id}` | REST | `aida.studio_api.get_change_set` | Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/studio/change-sets` | REST | `aida.studio_api.list_change_sets` | Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/studio/eval/questions` | REST | `aida.studio_api.list_eval_questions` | Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/table-family-candidates/{family_candidate_id}/canonical` | REST | `aida.intelligence_api.get_canonical_mapping` | Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, MetadataReviewer, PlatformAdmin, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/tables/{table_id}/certification` | REST | `atlas.modules.catalog.router.get_table_certification` | Analyst, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/tables/{table_id}/column-documentation` | REST | `aida.column_documentation_api.list_column_documentation` | Analyst, MetadataAdmin, PlatformAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/tables/{table_id}/columns` | REST | `aida.api.list_columns` | Analyst, MetadataAdmin, PlatformAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/tables/{table_id}/constraints` | REST | `aida.api.list_constraints` | Analyst, MetadataAdmin, PlatformAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/tables/{table_id}/description` | REST | `aida.column_documentation_api.get_table_description` | Analyst, MetadataAdmin, PlatformAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/tables/{table_id}/indexes` | REST | `aida.api.list_indexes` | Analyst, MetadataAdmin, PlatformAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/tables/{table_id}/partitions` | REST | `aida.api.list_partitions` | Analyst, MetadataAdmin, PlatformAdmin, Viewer | yes | yes | read | no | not cancellable |
| `GET /v1/tables/{table_id}/profile` | REST | `aida.api.get_latest_table_profile` | Analyst, MetadataAdmin, PlatformAdmin, Viewer | yes | yes | writes | yes | not cancellable |
| `GET /v1/tool-plans/{plan_id}/evidence` | REST | `aida.tool_plans_api.get_plan_evidence` | DataEngineer, PlatformAdmin, ToolDeveloper, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/tool-plans/{plan_id}` | REST | `aida.tool_plans_api.get_tool_plan` | DataEngineer, PlatformAdmin, ToolDeveloper, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/tool-plans` | REST | `aida.tool_plans_api.list_tool_plans` | DataEngineer, PlatformAdmin, ToolDeveloper, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/tool-versions/{version_id}/deprecation-impact` | REST | `aida.tool_api.get_tool_deprecation_impact` | Auditor, PlatformAdmin, Reviewer, SemanticAdmin, ToolDeveloper | yes | no | read | no | not cancellable |
| `GET /v1/tools/{tool_id}/certification-cases` | REST | `aida.tool_api.list_tool_certification_cases` | AgentDeveloper, Analyst, Auditor, PlatformAdmin, Reviewer, SemanticAdmin, ToolDeveloper, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/tools/{tool_id}/certification-status` | REST | `aida.tool_api.get_tool_certification_status` | AgentDeveloper, Analyst, Auditor, PlatformAdmin, Reviewer, SemanticAdmin, ToolDeveloper, Viewer | yes | no | read | no | not cancellable |
| `GET /v1/workspaces/{workspace_id}/members` | REST | `atlas.modules.identity_tenancy.router.list_members` | Analyst, DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer, Steward | yes | no | read | no | not cancellable |
| `GET /v1/workspaces/{workspace_id}/source-bindings` | REST | `atlas.modules.identity_tenancy.router.list_source_bindings` | Analyst, DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer, Steward | yes | no | read | no | not cancellable |
| `GET /v1/workspaces/{workspace_id}` | REST | `atlas.modules.identity_tenancy.router.get_workspace` | Analyst, DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer, Steward | yes | no | read | no | not cancellable |
| `PATCH /v1/datasources/{datasource_id}` | REST | `atlas.modules.connectivity.router.update_datasource` | DataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `PATCH /v1/playbooks/{playbook_id}` | REST | `aida.playbooks_api.update_playbook` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/access-review/entitlements/generate` | REST | `aida.access_review_api.generate_entitlement_report` | none declared | yes | no | writes | yes | not cancellable |
| `POST /v1/ai-asset-versions/{version_id}/assessments` | REST | `aida.ai_registry_api.assess_ai_asset_version` | Auditor, ModelRiskManager, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/ai-asset-versions/{version_id}/eval-gate/evaluate` | REST | `aida.ai_registry_api.evaluate_agent_eval_gate_endpoint` | AgentDeveloper, DataScientist, ModelRiskManager, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/ai-asset-versions/{version_id}/provider-sync` | REST | `aida.ai_registry_api.sync_ai_provider_evidence` | AgentDeveloper, DataScientist, ModelRiskManager, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/ai-asset-versions/{version_id}/remediations` | REST | `aida.ai_registry_api.create_ai_remediation` | Auditor, ModelRiskManager, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/ai-asset-versions/{version_id}/submit` | REST | `aida.ai_registry_api.submit_ai_asset_version` | AgentDeveloper, DataScientist, ModelRiskManager, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/ai-assets/{asset_id}/retire` | REST | `aida.ai_registry_api.request_ai_asset_retirement` | AgentDeveloper, DataScientist, ModelRiskManager, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/ai-assets/{asset_id}/versions` | REST | `aida.ai_registry_api.create_ai_asset_version` | AgentDeveloper, DataScientist, ModelRiskManager, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/asset-description-drafts/{draft_id}/submit` | REST | `aida.asset_description_api.submit_asset_description_draft` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/asset-documentation-versions/{version_id}/submit` | REST | `aida.glossary_api.submit_asset_documentation_version` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/authorization-probes` | REST | `atlas.modules.identity_tenancy.router.probe_authorization` | Analyst, DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer, Steward | yes | no | mutating verb, no write found | no | not cancellable |
| `POST /v1/bi-connections/{connection_id}/artifact-imports` | REST | `aida.bi_api.import_bi_artifact` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/column-description-drafts/{draft_id}/submit` | REST | `aida.column_description_api.submit_column_description_draft` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/compliance/packs/generate` | REST | `aida.compliance_api.generate_compliance_pack` | ComplianceOfficer, DataSteward, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/composite-key-candidates/{candidate_id}/decision` | REST | `aida.composite_key_api.decide_composite_key_candidate` | DataSteward, MetadataReviewer, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/composite-relationship-candidates/{group_id}/decision` | REST | `aida.intelligence_api.decide_composite_relationship_candidate` | DataSteward, MetadataReviewer, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/context-compiler/validate` | REST | `aida.context_compiler_api.validate_context_compilation` | AgentDeveloper, Analyst, DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | no | no | mutating verb, no write found | no | not cancellable |
| `POST /v1/context-product-versions/{version_id}/compile/drift` | REST | `aida.context_compiler_api.inspect_context_compilation_drift` | AgentDeveloper, Analyst, DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/context-product-versions/{version_id}/deprecate` | REST | `aida.context_product_api.request_context_product_deprecation` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/context-product-versions/{version_id}/submit` | REST | `aida.context_product_api.submit_context_product_version` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/context-products/{product_id}/versions` | REST | `aida.context_product_api.create_context_product_version` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/cross-source-object-resolution-candidates/{candidate_id}/decision` | REST | `aida.intelligence_api.decide_cross_source_object_resolution_candidate` | DataSteward, MetadataReviewer, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/data-contract-versions/{contract_id}/submit` | REST | `aida.product_marketplace_api.submit_data_contract` | DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/data-contracts/{contract_id}/evaluate` | REST | `aida.runtime_contracts_api.evaluate_data_contract` | DataEngineer, DataSteward, PlatformAdmin, Viewer | yes | no | writes | yes | not cancellable |
| `POST /v1/data-domains/{domain_id}/cross-boundary-grants` | REST | `atlas.modules.identity_tenancy.router.request_cross_boundary_grant` | DataAdmin, DataSteward, OrganizationAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/data-domains/{domain_id}/cross-source-object-resolution-candidates/discover` | REST | `aida.intelligence_api.discover_cross_source_object_resolution_candidates` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/data-domains/{domain_id}/relationship-candidates/discover-cross-source` | REST | `aida.intelligence_api.discover_cross_source_relationship_candidates` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/data-product-versions/{version_id}/retire` | REST | `aida.product_marketplace_api.request_data_product_retirement` | DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/data-product-versions/{version_id}/submit` | REST | `aida.product_marketplace_api.submit_data_product_version` | DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/data-products/{product_id}/contracts` | REST | `aida.product_marketplace_api.create_data_contract` | DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/data-products/{product_id}/versions` | REST | `aida.product_marketplace_api.create_data_product_version` | DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/agent-analyses` | REST | `aida.api.run_agent_analysis` | AgentDeveloper, Analyst, PlatformAdmin | yes | yes | writes | yes | cooperative |
| `POST /v1/datasources/{datasource_id}/agent-retrieval-preview` | REST | `aida.api.preview_agent_retrieval` | AgentDeveloper, Analyst, PlatformAdmin, Viewer | yes | yes | mutating verb, no write found | no | cooperative |
| `POST /v1/datasources/{datasource_id}/classification-feed/ingest` | REST | `aida.api.ingest_datasource_classification_feed` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/connector-certifications` | REST | `atlas.modules.ingestion.router.certify_datasource_connector` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/freshness-config/{table_id}/approve` | REST | `aida.quality_api.approve_freshness_config` | DataAdmin, DataSteward, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/metadata-ingestion-batches` | REST | `atlas.modules.ingestion.router.create_metadata_ingestion_batch` | DataAdmin, MetadataAdmin, MetadataIngestor, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/metadata-ingestions` | REST | `atlas.modules.ingestion.router.ingest_metadata_envelope` | DataAdmin, MetadataAdmin, MetadataIngestor, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/model/import` | REST | `aida.model_import_api.upload_model_workbook` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/native-policy-sync/preview` | REST | `aida.policy_native_sync_api.preview_native_policy_sync` | DataAdmin, DataSteward, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/procedures/{routine_id}/lineage/parse` | REST | `aida.procedure_lineage_api.parse_deep_procedure_lineage_endpoint` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/profiling-exception-policies` | REST | `aida.api.request_profiling_exception_policy` | DataAdmin, DataSteward, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/quality-rule-packs` | REST | `aida.quality_api.create_rule_pack` | DataAdmin, DataSteward, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/quality/external-signals` | REST | `aida.quality_api.ingest_external_quality_signal` | DataAdmin, DataSteward, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/query-executions` | REST | `aida.api.execute_query` | Analyst, PlatformAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/relationship-candidates/discover-composite` | REST | `aida.intelligence_api.discover_composite_relationship_candidates` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/relationship-candidates/discover` | REST | `aida.intelligence_api.discover_relationship_candidates` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/sql-validations` | REST | `aida.sql_validation_api.validate_sql` | AgentDeveloper, Analyst, PlatformAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/datasources/{datasource_id}/test` | REST | `atlas.modules.connectivity.router.test_datasource` | DataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/dbt-projects/{dbt_project_id}/artifact-imports` | REST | `aida.dbt_api.import_dbt_manifest` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/delegations/{delegation_id}/revoke` | REST | `aida.delegation_api.revoke_delegation` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/descriptions/withdrawals` | REST | `aida.description_withdrawal_api.create_description_withdrawal` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/documents/{document_id}/extract-claims` | REST | `aida.document_ingestion_api.extract_claims` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/documents/{document_id}/map` | REST | `aida.document_ingestion_api.map_document` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/glossary-conflicts/{conflict_id}/resolution` | REST | `aida.stewardship_api.submit_conflict_resolution` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/glossary-link-proposals/{proposal_id}/submit` | REST | `aida.stewardship_api.submit_glossary_link_proposal` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/glossary-term-versions/{version_id}/submit` | REST | `aida.glossary_api.submit_glossary_term_version` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/glossary-terms/{term_id}/deprecate` | REST | `aida.stewardship_api.deprecate_glossary_term` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/glossary-terms/{term_id}/versions` | REST | `aida.glossary_api.create_glossary_term_version` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/governance/reviews/{review_id}/decision` | REST | `aida.semantic_api.decide_governance_review` | DataSteward, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/lineage/openlineage` | REST | `aida.openlineage_api.ingest_openlineage_run_event` | DataAdmin, MetadataAdmin, MetadataIngestor, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/lineage/parsed-edges/{edge_id}/decision` | REST | `aida.parsed_lineage_review_api.decide_parsed_lineage_edge` | DataSteward, MetadataReviewer, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/lines-of-business/{lob_id}/data-domains` | REST | `atlas.modules.identity_tenancy.router.create_data_domain` | DataAdmin, OrganizationAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/lines-of-business/{lob_id}/projects` | REST | `atlas.modules.identity_tenancy.router.create_project` | PlatformAdmin, ProjectAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/marketplace/access-requests/{request_id}/entitlement` | REST | `aida.product_marketplace_api.fulfill_marketplace_entitlement` | Operations, OrganizationAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/marketplace/access-requests/{request_id}/revoke` | REST | `aida.product_marketplace_api.revoke_marketplace_access` | DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/marketplace/products/{version_id}/access-requests` | REST | `aida.product_marketplace_api.request_marketplace_access` | Analyst, DataConsumer, DataScientist, PlatformAdmin, Viewer | yes | no | writes | yes | not cancellable |
| `POST /v1/metadata-enrichment-proposals/{proposal_id}/promote-tool` | REST | `aida.semantic_intelligence_api.promote_enrichment_tool_blueprint` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | yes | writes | yes | not cancellable |
| `POST /v1/metadata-ingestion-batches/{batch_id}/cancel` | REST | `atlas.modules.ingestion.router.cancel_metadata_ingestion_batch` | DataAdmin, MetadataAdmin, MetadataIngestor, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/metadata-ingestion-batches/{batch_id}/chunks` | REST | `atlas.modules.ingestion.router.upload_metadata_ingestion_chunk` | DataAdmin, MetadataAdmin, MetadataIngestor, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/metadata-ingestion-batches/{batch_id}/finalize` | REST | `atlas.modules.ingestion.router.finalize_metadata_ingestion_batch` | DataAdmin, MetadataAdmin, MetadataIngestor, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/metadata-ingestion-batches/{batch_id}/pause` | REST | `atlas.modules.ingestion.router.pause_metadata_ingestion_batch` | DataAdmin, MetadataAdmin, MetadataIngestor, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/metadata-ingestion-batches/{batch_id}/replay` | REST | `atlas.modules.ingestion.router.replay_metadata_ingestion_batch` | DataAdmin, MetadataAdmin, MetadataIngestor, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/metadata-ingestion-batches/{batch_id}/resume` | REST | `atlas.modules.ingestion.router.resume_metadata_ingestion_batch` | DataAdmin, MetadataAdmin, MetadataIngestor, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/metadata/tables/{table_id}/documentation-versions` | REST | `aida.glossary_api.create_asset_documentation_version` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/metadata/tables/{table_id}/glossary-links` | REST | `aida.glossary_api.create_asset_term_link` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/metric-suggestions/{proposal_id}/submit` | REST | `aida.metric_suggestion_api.submit_metric_suggestion_proposal` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/model-imports/{batch_id}/changes/exclusion` | REST | `aida.model_import_api.set_model_import_exclusion` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/model-imports/{batch_id}/submit` | REST | `aida.model_import_api.submit_model_import` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/model-routes/{route_id}/submit` | REST | `aida.ai_governance_api.submit_model_route` | AgentDeveloper, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/negative-knowledge/{assertion_id}/lift-suppression` | REST | `aida.negative_knowledge_api.lift_assertion_suppression` | DataSteward, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/notification-rules` | REST | `aida.notification_api.create_notification_rule` | DataAdmin, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/notifications/{notification_id}/acknowledge` | REST | `aida.notification_api.acknowledge_notification` | DataAdmin, DataSteward, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/observability/slo` | REST | `atlas.modules.observability_audit.router.create_slo_definition` | DataAdmin, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/ontology-versions/{version_id}/submit` | REST | `aida.ontology_api.submit_ontology_version` | DataSteward, MetadataAdmin, PlatformAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/access-policies` | REST | `atlas.modules.identity_tenancy.router.create_access_policy` | OrganizationAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/agent-contract-requests` | REST | `aida.agent_contract_request_api.submit_agent_contract_request` | AgentDeveloper, ModelRiskManager, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/agent-evaluations` | REST | `aida.api.run_agent_evaluation` | AgentDeveloper, Auditor, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/agents/{ai_asset_version_id}/contract/kill` | REST | `aida.agent_contract_api.engage_agent_kill_switch` | AgentDeveloper, ModelRiskManager, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/agents/{ai_asset_version_id}/contract/release` | REST | `aida.agent_contract_api.release_agent_kill_switch` | AgentDeveloper, ModelRiskManager, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/ai-assets` | REST | `aida.ai_registry_api.create_ai_asset` | AgentDeveloper, DataScientist, ModelRiskManager, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/asset-description-drafts/generate` | REST | `aida.asset_description_api.generate_asset_description_drafts` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/asset-description-drafts/sample-review/decide` | REST | `aida.asset_description_api.decide_asset_description_sample_review` | DataSteward, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/asset-description-drafts/sample-review/draw` | REST | `aida.asset_description_api.draw_asset_description_sample_review` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/business-assignments` | REST | `atlas.modules.identity_tenancy.router.create_business_assignment` | DataAdmin, OrganizationAdmin, PlatformAdmin, Steward | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/business-nodes` | REST | `atlas.modules.identity_tenancy.router.create_business_node` | DataAdmin, OrganizationAdmin, PlatformAdmin, Steward | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/column-description-drafts/generate` | REST | `aida.column_description_api.generate_column_description_drafts` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/delegations` | REST | `aida.delegation_api.grant_delegation` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/glossary-categories` | REST | `aida.stewardship_api.create_glossary_category` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/glossary-conflicts/detect` | REST | `aida.stewardship_api.detect_glossary_conflicts` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/glossary-conflicts` | REST | `aida.stewardship_api.create_glossary_conflict` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/glossary-link-proposals/generate` | REST | `aida.stewardship_api.generate_glossary_link_proposals` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/glossary-terms` | REST | `aida.glossary_api.create_glossary_term` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/kill-switch/engage` | REST | `aida.ai_governance_api.engage_kill_switch` | PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/kill-switch/release` | REST | `aida.ai_governance_api.release_kill_switch` | PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/lines-of-business` | REST | `atlas.modules.identity_tenancy.router.create_line_of_business` | OrganizationAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/metric-conflicts/detect` | REST | `aida.semantic_api.detect_metric_formula_collisions` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/metric-suggestions/generate` | REST | `aida.metric_suggestion_api.generate_metric_suggestion_proposals` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/model-routes` | REST | `aida.ai_governance_api.create_model_route` | AgentDeveloper, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/notifications/governance/test` | REST | `aida.retrieval_ops_api.send_test_governance_notification` | MetadataAdmin, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/ontology-versions` | REST | `aida.ontology_api.create_ontology_version` | DataSteward, MetadataAdmin, PlatformAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/ownership-rules` | REST | `aida.stewardship_api.create_ownership_rule` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/playbooks` | REST | `aida.playbooks_api.create_playbook` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/retrieval/vector-index/rebuild` | REST | `aida.retrieval_ops_api.rebuild_vector_index_endpoint` | MetadataAdmin, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/reviewer-agent/pre-review` | REST | `aida.agent_contract_api.run_pre_review` | MetadataReviewer, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/reviewer-agent/resume` | REST | `aida.agent_contract_api.resume_reviewer_agent` | MetadataReviewer, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/reviewer-agent/samples/{sample_id}/resolve` | REST | `aida.agent_contract_api.resolve_sample` | MetadataReviewer, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/reviewer-agent/suspend` | REST | `aida.agent_contract_api.suspend_reviewer_agent` | MetadataReviewer, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/stewardship/coverage/snapshots` | REST | `aida.stewardship_api.snapshot_stewardship_coverage` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/stewardship/leaver-reassignment` | REST | `aida.stewardship_api.request_leaver_reassignment` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/stewardship/unowned-backlog/route` | REST | `aida.stewardship_api.route_unowned_asset_backlog` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations/{organization_id}/workspaces` | REST | `atlas.modules.identity_tenancy.router.create_workspace_route` | DataAdmin, OrganizationAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/organizations` | REST | `atlas.modules.identity_tenancy.router.create_organization` | PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/outbox-events/{event_id}/requeue` | REST | `aida.operational_api.requeue_outbox_event` | Operations, OrganizationAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/ownership-assignments/{assignment_id}/reaffirm` | REST | `aida.stewardship_api.reaffirm_ownership_assignment` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/ownership-rules/{rule_id}/apply` | REST | `aida.stewardship_api.apply_ownership_rule` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/profiling-exception-policies/{policy_id}/decision` | REST | `aida.api.decide_profiling_exception_policy` | DataSteward, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/profiling-exception-policies/{policy_id}/revoke` | REST | `aida.api.revoke_profiling_exception_policy` | DataSteward, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/bi-connections` | REST | `aida.bi_api.create_bi_connection` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/context-products` | REST | `aida.context_product_api.create_context_product` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/data-products` | REST | `aida.product_marketplace_api.create_data_product` | DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/datasources` | REST | `atlas.modules.connectivity.router.create_datasource` | DataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/dbt-projects` | REST | `aida.dbt_api.create_dbt_project` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/documents` | REST | `aida.document_ingestion_api.upload_document` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/semantic-model-versions` | REST | `aida.semantic_api.create_semantic_model_version` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/tool-blueprints/from-procedure` | REST | `aida.procedure_tool_api.create_procedure_tool_blueprint` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | yes | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/tool-blueprints/from-view` | REST | `aida.tool_api.create_view_tool_blueprint` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | yes | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/tool-blueprints/multi-table` | REST | `aida.tool_api.create_multi_table_tool_blueprint` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | yes | writes | yes | not cancellable |
| `POST /v1/projects/{project_id}/tools` | REST | `aida.tool_api.create_tool_version` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | yes | writes | yes | not cancellable |
| `POST /v1/quality-incidents/{incident_id}/transition` | REST | `aida.quality_api.transition_quality_incident` | DataAdmin, DataSteward, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/quality-rule-packs/{rule_pack_id}/evaluate` | REST | `aida.quality_api.evaluate_rule_pack_now` | DataAdmin, DataSteward, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/quality-rule-packs/{rule_pack_id}/rules` | REST | `aida.quality_api.create_rule` | DataAdmin, DataSteward, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/query/validate` | REST | `aida.api.validate_query` | AgentDeveloper, Analyst, PlatformAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/relationship-candidates/{candidate_id}/decision` | REST | `aida.intelligence_api.decide_relationship_candidate` | DataSteward, MetadataReviewer, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/rename-candidates/{candidate_id}/decision` | REST | `aida.intelligence_api.decide_rename_candidate` | DataSteward, MetadataReviewer, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/schemas/{schema_id}/table-family-candidates/discover` | REST | `aida.table_family_api.discover_table_family_candidates` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/security/tokens/detokenize` | REST | `aida.detokenization_api.detokenize_value` | none declared | yes | no | writes | yes | not cancellable |
| `POST /v1/security/tokens/revoke` | REST | `aida.token_revocation_api.revoke_token` | none declared | yes | no | writes | yes | not cancellable |
| `POST /v1/semantic-metrics/{metric_id}/glossary-bindings` | REST | `aida.semantic_api.create_term_semantic_binding` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/semantic-model-versions/{model_id}/metrics` | REST | `aida.semantic_api.create_metric_version` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/semantic-model-versions/{model_id}/submit` | REST | `aida.semantic_api.submit_semantic_model_for_review` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/semantic-model-versions/{source_model_id}/clone` | REST | `aida.semantic_api.clone_semantic_model_version` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/source-bindings/{binding_id}/decision` | REST | `atlas.modules.identity_tenancy.router.decide_source_binding` | DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `POST /v1/studio/change-sets/{change_set_id}/detect-conflicts` | REST | `aida.studio_api.detect_conflicts_endpoint` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/studio/change-sets/{change_set_id}/items` | REST | `aida.studio_api.add_item` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/studio/change-sets/{change_set_id}/submit` | REST | `aida.studio_api.submit_change_set` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/studio/change-sets/{change_set_id}/test` | REST | `aida.studio_api.run_tests` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/studio/change-sets` | REST | `aida.studio_api.create_change_set` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/studio/context-products/validate` | REST | `aida.studio_api.validate_context_product_contract_endpoint` | Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | no | no | mutating verb, no write found | no | not cancellable |
| `POST /v1/studio/eval/mine` | REST | `aida.studio_api.mine_eval_suite` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/studio/parameter-contracts/validate` | REST | `aida.studio_api.validate_parameter_contract_endpoint` | Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer | no | no | mutating verb, no write found | no | not cancellable |
| `POST /v1/table-family-candidates/{candidate_id}/decision` | REST | `aida.table_family_api.decide_table_family_candidate` | DataSteward, MetadataReviewer, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/table-family-candidates/{family_candidate_id}/canonical/override` | REST | `aida.intelligence_api.override_canonical_table` | DataSteward, MetadataReviewer, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/tables/{table_id}/certification/revoke` | REST | `atlas.modules.catalog.router.revoke_table_certification` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/tables/{table_id}/certification` | REST | `atlas.modules.catalog.router.certify_table_asset` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/tables/{table_id}/column-description-drafts/submit` | REST | `aida.column_description_api.submit_table_column_description_drafts` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/tables/{table_id}/column-worksheet` | REST | `aida.model_import_api.save_column_worksheet` | DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin | yes | yes | writes | yes | not cancellable |
| `POST /v1/tables/{table_id}/composite-key-candidates/discover` | REST | `aida.composite_key_api.discover_composite_key_candidates` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/tool-plans/recommend` | REST | `aida.tool_plans_api.recommend_tool_plan` | DataEngineer, PlatformAdmin, ToolDeveloper | yes | no | writes | yes | not cancellable |
| `POST /v1/tool-plans/{plan_id}/cancel` | REST | `aida.tool_plans_api.cancel_tool_plan` | PlatformAdmin, ToolDeveloper | yes | no | writes | yes | not cancellable |
| `POST /v1/tool-plans/{plan_id}/execute` | REST | `aida.tool_plans_api.execute_tool_plan` | DataEngineer, PlatformAdmin, ToolDeveloper | yes | yes | writes | yes | not cancellable |
| `POST /v1/tool-plans/{plan_id}/validate` | REST | `aida.tool_plans_api.validate_tool_plan` | DataEngineer, PlatformAdmin, ToolDeveloper | yes | no | writes | yes | not cancellable |
| `POST /v1/tool-plans` | REST | `aida.tool_plans_api.create_tool_plan` | DataEngineer, PlatformAdmin, ToolDeveloper | yes | no | writes | yes | not cancellable |
| `POST /v1/tool-versions/{version_id}/deprecation-submit` | REST | `aida.tool_api.submit_tool_deprecation` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | no | writes | yes | not cancellable |
| `POST /v1/tool-versions/{version_id}/execute` | REST | `aida.tool_api.execute_tool` | AgentDeveloper, Analyst, PlatformAdmin, ToolConsumer | yes | yes | writes | yes | not cancellable |
| `POST /v1/tool-versions/{version_id}/submit` | REST | `aida.tool_api.submit_tool_for_review` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | no | writes | yes | not cancellable |
| `POST /v1/tools/{tool_id}/certification-cases` | REST | `aida.tool_api.create_tool_certification_case` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | no | writes | yes | not cancellable |
| `POST /v1/workspaces/{workspace_id}/authorization-simulations` | REST | `atlas.modules.identity_tenancy.router.simulate_authorization` | Analyst, DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer, Steward | yes | no | mutating verb, no write found | no | not cancellable |
| `POST /v1/workspaces/{workspace_id}/members` | REST | `atlas.modules.identity_tenancy.router.add_member` | DataAdmin, OrganizationAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `POST /v1/workspaces/{workspace_id}/source-bindings` | REST | `atlas.modules.identity_tenancy.router.request_source_binding` | Analyst, DataAdmin, OrganizationAdmin, PlatformAdmin, Reviewer, Steward | yes | no | writes | yes | not cancellable |
| `PUT /v1/ai-remediations/{remediation_id}` | REST | `aida.ai_registry_api.update_ai_remediation` | Auditor, ModelRiskManager, PlatformAdmin, Reviewer | yes | no | writes | yes | not cancellable |
| `PUT /v1/asset-description-drafts/{draft_id}` | REST | `aida.asset_description_api.edit_asset_description_draft` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `PUT /v1/column-description-drafts/{draft_id}` | REST | `aida.column_description_api.edit_column_description_draft` | DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin | yes | yes | writes | yes | not cancellable |
| `PUT /v1/context-product-versions/{version_id}` | REST | `aida.context_product_api.update_context_product_version` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `PUT /v1/context-products/{product_id}/bindings/{consumer_principal_id}` | REST | `aida.context_product_api.set_context_product_consumer_binding` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `PUT /v1/data-product-versions/{version_id}` | REST | `aida.product_marketplace_api.update_data_product_version` | DataProductOwner, DataSteward, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `PUT /v1/datasources/{datasource_id}/freshness-config/{table_id}` | REST | `aida.quality_api.upsert_freshness_config` | DataAdmin, DataSteward, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `PUT /v1/datasources/{datasource_id}/quality-policies` | REST | `aida.quality_api.upsert_quality_policy` | DataAdmin, DataSteward, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `PUT /v1/datasources/{datasource_id}/scan-policy` | REST | `atlas.modules.connectivity.router.upsert_scan_policy` | DataAdmin, MetadataAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `PUT /v1/notification-rules/{rule_id}` | REST | `aida.notification_api.update_notification_rule` | DataAdmin, Operations, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `PUT /v1/organizations/{organization_id}/agents/{ai_asset_version_id}/contract` | REST | `aida.agent_contract_api.put_agent_contract` | AgentDeveloper, ModelRiskManager, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `PUT /v1/organizations/{organization_id}/integration-policy` | REST | `aida.api.update_organization_integration_policy` | OrganizationAdmin, PlatformAdmin | yes | no | writes | yes | not cancellable |
| `PUT /v1/semantic-metric-versions/{version_id}` | REST | `aida.semantic_api.edit_metric_draft` | DataSteward, PlatformAdmin, SemanticAdmin | yes | no | writes | yes | not cancellable |
| `SDK aida_tool_sdk.ToolDraftClient.submit_draft` | SDK | `aida.tool_api.create_tool_version` | PlatformAdmin, SemanticAdmin, ToolDeveloper | yes | yes | writes | yes | not cancellable |
