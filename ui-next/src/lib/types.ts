/* ---------------------------------------------------------------------------
   AUTO-GENERATED -- DO NOT EDIT BY HAND.

   Generated from `app.openapi()`'s `components.schemas` by
   scripts/generate_ui_types.py (tracker UX-14). CI
   (`.github/workflows/ci.yml`'s `ui-types-diff` job) fails the build if this
   file drifts from what that script produces against the current
   `src/aida/schemas.py` / `src/aida/platform_schemas.py`.

   To pick up a schema change:
       uv run python scripts/generate_ui_types.py --accept-baseline
   then commit the result. Do not hand-edit -- the next `--accept-baseline`
   run overwrites any manual change silently.

   Two types the rest of ui-next still needs are deliberately NOT here
   because they are not in the live OpenAPI document yet (`CatalogRowRead`,
   `MetadataTableRead` -- see this script's module docstring for why); they
   live by hand in `./ui-types.ts` instead.
--------------------------------------------------------------------------- */

export interface AccessPolicyCreate {
  code: string;
  name: string;
  description?: string;
  effect: "ALLOW" | "DENY" | "MASK" | "FILTER";
  priority?: number;
  subject_match?: Record<string, unknown>;
  resource_match?: Record<string, unknown>;
  action_match?: string[];
  transform?: Record<string, unknown>;
  condition?: Record<string, unknown>;
  status?: "DRAFT" | "ACTIVE";
}

export interface AccessPolicyRead {
  id: string;
  organization_id: string;
  code: string;
  version: number;
  name: string;
  description: string;
  effect: string;
  priority: number;
  subject_match: Record<string, unknown>;
  resource_match: Record<string, unknown>;
  action_match: string[];
  transform: Record<string, unknown>;
  condition: Record<string, unknown>;
  origin: string;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface AgentAnalysisRequest {
  question: string;
  candidate_sql?: string | null;
  preferred_tool_version_id?: string | null;
  tool_parameters?: Record<string, unknown>;
  max_rows?: number | null;
}

export interface AgentAnalysisResponse {
  agent_run_id: string;
  status: string;
  generation_source: string;
  semantic_version: string | null;
  policy_version: string;
  step_trace: Record<string, unknown>[];
  retrieval_evidence: Record<string, unknown>[];
  plan_evidence: Record<string, unknown>;
  execution: QueryExecutionResponse;
  explanation: string;
}

/** Whether this agent's plan has a real, code-backed auto-apply branch */
export interface AgentAutoApplyRead {
  has_auto_apply_branch: boolean;
  threshold: number | null;
  threshold_source: string | null;
  evidence: string;
}

/** Body of `POST .../eval-gate/evaluate`. `steward_authored_verdicts` */
export interface AgentEvalGateEvaluateRequest {
  steward_authored_verdicts?: AgentEvalGateVerdictInput[];
}

/** The gate's current state -- deliverable 3: what a steward reads, */
export interface AgentEvalGateRead {
  verdict: "PASS" | "FAIL" | "INSUFFICIENT_DATA";
  threshold: number;
  minimum_exemplars: number;
  total_exemplars: number;
  passed_exemplars: number;
  pass_rate: number | null;
  failing_case_ids: string[];
  verdicts: AgentEvalGateVerdictRead[];
  reason: string;
  evaluated_at: string;
}

/** One externally-computed `STEWARD_AUTHORED` replay verdict, submitted */
export interface AgentEvalGateVerdictInput {
  case_id: string;
  matched: boolean;
  drift?: string[];
  detail?: string;
}

export interface AgentEvalGateVerdictRead {
  case_id: string;
  source: "CONFIRMED_RUN" | "STEWARD_AUTHORED";
  matched: boolean;
  drift: string[];
  detail: string;
}

export interface AgentEvaluationRunRead {
  id: string;
  organization_id: string;
  principal_id: string;
  suite_version: string;
  status: string;
  scenario_count: number;
  passed_count: number;
  failed_count: number;
  pass_rate: number;
  findings: Record<string, unknown>[];
  created_at: string;
  updated_at: string;
}

/** The "task plan" half of this row's exit condition: not a static, */
export interface AgentMethodSummaryRead {
  scope: "ORGANIZATION_WIDE";
  note: string;
  window_days: number;
  sampled_runs: number;
  by_strategy: Record<string, number>;
  average_confidence: number | null;
  tool_first: ToolFirstRateSummaryRead;
}

/** EA.10c AI registry data for one agent's latest version -- the */
export interface AgentPurposeRead {
  asset_id: string;
  asset_key: string;
  version: number;
  status: string;
  name: string;
  description: string;
  intended_use: string;
  owner_principal: string;
  provider_type: string;
  risk_tier: string;
  documentation_url: string | null;
}

export interface AgentRetrievalPreviewRead {
  datasource_id: string;
  retrieval_evidence: Record<string, unknown>[];
  plan_evidence: Record<string, unknown>;
}

export interface AgentRetrievalPreviewRequest {
  question: string;
  candidate_sql_available?: boolean;
}

export interface AgentRosterEntryRead {
  purpose: AgentPurposeRead;
  method: AgentMethodSummaryRead;
  recent_results: AgentRunOutcomeRead[];
  recent_results_total: number;
  auto_apply: AgentAutoApplyRead;
}

export interface AgentRosterRead {
  organization_id: string;
  generated_at: string;
  window_days: number;
  agents: AgentRosterEntryRead[];
  total_agents: number;
}

export interface AgentRunGroundingReceiptsRead {
  agent_run_id: string;
  fragment_count: number;
  fragments: GroundingFragmentReceiptRead[];
}

/** One recent `AgentRun`'s outcome -- the "live results" half of this */
export interface AgentRunOutcomeRead {
  run_id: string;
  status: string;
  strategy: string | null;
  confidence: number | null;
  generation_source: string;
  created_at: string;
  failure_reason: string | null;
}

export interface AgentRunRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  principal_id: string;
  status: string;
  generation_source: string;
  model_route: string | null;
  semantic_version: string | null;
  policy_version: string;
  query_execution_id: string | null;
  step_trace: Record<string, unknown>[];
  retrieval_evidence: Record<string, unknown>[];
  grounding_fragment_digests: Record<string, unknown>[];
  plan_evidence: Record<string, unknown>;
  recommended_tool_version_id: string | null;
  failure_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface AiAssessmentControlResult {
  control_key: string;
  title: string;
  weight?: number;
  outcome: "PASS" | "FAIL" | "NOT_APPLICABLE";
  evidence_reference?: string | null;
  finding?: string | null;
}

export interface AiAssessmentCreate {
  framework: "EU_AI_ACT" | "NIST_AI_RMF" | "AI_UC_1" | "CUSTOM";
  framework_version: string;
  control_results: AiAssessmentControlResult[];
}

export interface AiAssessmentRead {
  framework: "EU_AI_ACT" | "NIST_AI_RMF" | "AI_UC_1" | "CUSTOM";
  framework_version: string;
  control_results: AiAssessmentControlResult[];
  id: string;
  organization_id: string;
  ai_asset_version_id: string;
  status: string;
  score: number;
  findings: Record<string, unknown>[];
  assessed_by: string;
  created_at: string;
  updated_at: string;
}

export interface AiAssessmentTemplateRead {
  template_key: string;
  framework: string;
  framework_version: string;
  title: string;
  controls: AiAssessmentControlResult[];
}

export interface AiAssetCreate {
  name: string;
  description: string;
  intended_use: string;
  owner_principal: string;
  provider_type: string;
  risk_tier: "LOW" | "MEDIUM" | "HIGH" | "PROHIBITED";
  documentation_url?: string | null;
  context_product_version_ids?: string[];
  model_route_ids?: string[];
  policy_control_ids?: string[];
  evaluation_evidence?: Record<string, unknown>;
  runtime_evidence?: Record<string, unknown>;
  asset_key: string;
  asset_kind: "AI_USE_CASE" | "MODEL" | "AGENT";
}

export interface AiAssetDefinition {
  name: string;
  description: string;
  intended_use: string;
  owner_principal: string;
  provider_type: string;
  risk_tier: "LOW" | "MEDIUM" | "HIGH" | "PROHIBITED";
  documentation_url?: string | null;
  context_product_version_ids?: string[];
  model_route_ids?: string[];
  policy_control_ids?: string[];
  evaluation_evidence?: Record<string, unknown>;
  runtime_evidence?: Record<string, unknown>;
}

export interface AiAssetVersionRead {
  name: string;
  description: string;
  intended_use: string;
  owner_principal: string;
  provider_type: string;
  risk_tier: "LOW" | "MEDIUM" | "HIGH" | "PROHIBITED";
  documentation_url?: string | null;
  context_product_version_ids?: string[];
  model_route_ids?: string[];
  policy_control_ids?: string[];
  evaluation_evidence?: Record<string, unknown>;
  runtime_evidence?: Record<string, unknown>;
  id: string;
  organization_id: string;
  asset_id: string;
  asset_key: string;
  asset_kind: string;
  version: number;
  status: string;
  fingerprint: string;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AiDecisionRead {
  id: string;
  organization_id: string;
  run_id: string;
  decision_type: string;
  source_node: string;
  target_node: string;
  reason: string;
  evidence: Record<string, unknown>;
  control_version: string | null;
  decided_at: string;
}

export interface AiDependencyGraphRead {
  nodes: Record<string, unknown>[];
  edges: Record<string, unknown>[];
}

export interface AiProviderSyncRequest {
  provider_type: string;
  external_reference: string;
  documentation_url?: string | null;
  evaluation_evidence?: Record<string, unknown>;
  runtime_evidence?: Record<string, unknown>;
}

export interface AiRemediationCreate {
  finding_key: string;
  title: string;
  description: string;
  owner_principal: string;
  due_at?: string | null;
}

export interface AiRemediationRead {
  finding_key: string;
  title: string;
  description: string;
  owner_principal: string;
  due_at?: string | null;
  id: string;
  organization_id: string;
  ai_asset_version_id: string;
  status: string;
  resolution_evidence: Record<string, unknown>;
  created_by: string;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AiRemediationUpdate {
  status: "OPEN" | "IN_PROGRESS" | "RESOLVED" | "ACCEPTED_RISK";
  resolution_evidence?: Record<string, unknown>;
}

export interface AiRuntimeStatusRead {
  orchestration_mode: "HYBRID";
  runtime: string;
  runtime_version: string;
  model_route_status: "CONFIGURED" | "NOT_CONFIGURED";
  model_generation_enabled: boolean;
  available_model_providers: string[];
  development_sql_override_enabled: boolean;
  identity_provider: string;
  identity_verification: string;
  oidc_configured: boolean;
  credential_provider: string;
  credential_provider_available: boolean;
  enterprise_security_ready: boolean;
  deterministic_controls: string[];
  optional_framework_adapters: string[];
  data_retention_statement: string;
}

export interface AiTrustFactorRead {
  factor: string;
  score: number;
  maximum: number;
  reason: string;
  evidence?: Record<string, unknown>;
}

export interface AiTrustScoreRead {
  ai_asset_version_id: string;
  score: number;
  grade: "TRUSTED" | "CONDITIONAL" | "UNTRUSTED" | "BLOCKED";
  factors: AiTrustFactorRead[];
  blockers: string[];
  computed_at: string;
}

export interface AnalysisRunCreate {
  mode?: string;
}

export interface AnalysisRunRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  resumed_from_run_id: string | null;
  mode: string;
  trigger_type: string;
  priority: number;
  status: string;
  temporal_workflow_id: string | null;
  discovered_catalogs: number;
  discovered_schemas: number;
  discovered_tables: number;
  discovered_columns: number;
  discovered_constraints: number;
  created_objects: number;
  changed_objects: number;
  deprecated_objects: number;
  profiled_tables: number;
  profiled_columns: number;
  error_class: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnalysisTaskRead {
  id: string;
  analysis_run_id: string;
  table_id: string | null;
  task_type: string;
  task_key: string;
  status: string;
  attempt_count: number;
  max_attempts: number;
  started_at: string | null;
  last_heartbeat_at: string | null;
  completed_at: string | null;
  heartbeat_detail: Record<string, unknown>;
  error_class: string | null;
  error_message: string | null;
  retry_history: Record<string, unknown>[];
  created_at: string;
  updated_at: string;
}

export interface ArchiveStatusRead {
  total_archives: number;
  total_events_archived: number;
  latest_archive_id: string | null;
  latest_checksum: string | null;
  legal_hold_count: number;
  status: string;
}

export interface AssetCertificationRead {
  id: string;
  organization_id: string;
  table_id: string;
  column_id: string | null;
  asset_type: string;
  status: string;
  rationale: string;
  certified_by: string;
  expires_at: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface AssetDescriptionDraftGenerate {
  table_ids: string[];
}

export interface AssetDescriptionDraftRead {
  id: string;
  organization_id: string;
  table_id: string;
  table_name: string;
  drafted_text: string;
  accuracy_score: number;
  clarity_score: number;
  style_score: number;
  completeness_score: number;
  overall_score: number;
  evidence: Record<string, unknown>;
  status: string;
  governance_review_id: string | null;
  published_version_id: string | null;
  created_by: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

/** AT-14: apply ONE accept/reject decision to the reproducibly-drawn */
export interface AssetDescriptionSampleDecide {
  draft_ids: string[];
  sample_size?: number | null;
  sample_fraction?: number | null;
  seed: number;
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

export interface AssetDescriptionSampleDecisionResultRead {
  decision: "APPROVE" | "REJECT";
  seed: number;
  batch_size: number;
  sample_size: number;
  drawn_draft_ids: string[];
  unsampled_draft_ids: string[];
  succeeded_count: number;
  failed_count: number;
  results: AssetDescriptionSampleItemRead[];
}

/** AT-14: draw a reproducible sample from a batch of PENDING_APPROVAL */
export interface AssetDescriptionSampleDraw {
  draft_ids: string[];
  sample_size?: number | null;
  sample_fraction?: number | null;
  seed?: number | null;
}

export interface AssetDescriptionSampleDrawRead {
  seed: number;
  batch_size: number;
  sample_size: number;
  drawn_draft_ids: string[];
  drawn_drafts: AssetDescriptionDraftRead[];
}

export interface AssetDescriptionSampleItemRead {
  draft_id: string;
  status: "SUCCEEDED" | "FAILED";
  reason?: string | null;
}

export interface AssetDocumentationVersionCreate {
  aliases?: string[];
  readme: string;
  owner_principal?: string | null;
}

export interface AssetDocumentationVersionRead {
  id: string;
  organization_id: string;
  documentation_id: string;
  table_id: string;
  version: number;
  status: string;
  aliases: string[];
  readme: string;
  owner_principal: string | null;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
}

/** UX-13: `GET /v1/metadata/tables/{id}/evidence` -- composes business */
export interface AssetEvidenceRead {
  table_id: string;
  table_name: string;
  generated_at: string;
  items: EvidenceItemRead[];
}

export interface AssetTermLinkCreate {
  term_id: string;
}

export interface AssetTermLinkRead {
  id: string;
  organization_id: string;
  table_id: string;
  term_id: string;
  term_key: string;
  display_name: string;
  definition: string;
  linked_by: string;
  link_type: string;
  confidence: number;
  source_annotation_id: string | null;
  created_at: string;
}

export interface AuthorizationProbeRead {
  allowed: boolean;
  reason_code: string;
  workspace_id: string | null;
  binding_id: string | null;
  matched_policy_code: string | null;
  masked_classifications: string[];
  row_filters: string[];
  evaluated_policy_count: number;
}

/** Ask the policy engine what it would decide, without performing the action. */
export interface AuthorizationProbeRequest {
  workspace_id: string;
  action: "READ_METADATA" | "READ_DATA" | "PROPOSE" | "APPROVE" | "EXECUTE_TOOL" | "CONSUME_CONTEXT" | "EXPORT";
  resource_type: string;
  resource_id?: string | null;
  datasource_id?: string | null;
  schema_name?: string | null;
  classifications?: string[];
  certification?: string | null;
  quality_state?: string | null;
  freshness_state?: string | null;
  principal_kind?: "HUMAN" | "AGENT" | "SERVICE";
}

export interface AuthorizationSimulationRead {
  workspace_id: string;
  decisions: SimulatedDecision[];
}

/** "Who could see this?" -- one resource, several hypothetical subjects. */
export interface AuthorizationSimulationRequest {
  workspace_id: string;
  action: "READ_METADATA" | "READ_DATA" | "PROPOSE" | "APPROVE" | "EXECUTE_TOOL" | "CONSUME_CONTEXT" | "EXPORT";
  resource_type: string;
  resource_id?: string | null;
  datasource_id?: string | null;
  schema_name?: string | null;
  classifications?: string[];
  certification?: string | null;
  quality_state?: string | null;
  freshness_state?: string | null;
  subjects: SimulatedSubject[];
}

export interface BiArtifactImportRead {
  id: string;
  organization_id: string;
  connection_id: string;
  artifact_fingerprint: string;
  bi_tool: string;
  generated_at: string | null;
  status: string;
  report_count: number;
  metric_count: number;
  report_metric_edge_count: number;
  metric_column_edge_count: number;
  matched_column_count: number;
  unmatched_column_count: number;
  imported_by: string;
  created_at: string;
  updated_at: string;
}

export interface BiArtifactImportRequest {
  bi_tool: "TABLEAU" | "POWER_BI" | "LOOKER";
  artifact: Record<string, unknown>;
}

export interface BiConnectionCreate {
  datasource_id: string;
  bi_tool: "TABLEAU" | "POWER_BI" | "LOOKER";
  connection_key: string;
  display_name: string;
  site_or_workspace?: string | null;
}

export interface BiConnectionRead {
  id: string;
  organization_id: string;
  project_id: string;
  datasource_id: string;
  bi_tool: string;
  connection_key: string;
  display_name: string;
  site_or_workspace: string | null;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface BiLineageRead {
  artifact_import_id: string;
  reports: BiReportNodeRead[];
  metrics: BiMetricNodeRead[];
  report_metric_edges: BiReportMetricEdgeRead[];
  metric_column_edges: BiMetricColumnEdgeRead[];
  report_count: number;
  metric_count: number;
  matched_column_count: number;
  unmatched_column_count: number;
}

export interface BiMetricColumnEdgeRead {
  id: string;
  metric_id: string;
  source_database_name: string | null;
  source_schema_name: string | null;
  source_table_name: string;
  source_column_name: string;
  matched_table_id: string | null;
  matched_column_id: string | null;
  edge_kind: string;
}

export interface BiMetricNodeRead {
  id: string;
  artifact_import_id: string;
  external_id: string;
  name: string;
  field_type: string;
  datasource_name: string | null;
  formula_hash: string | null;
  formula_present: boolean;
  created_at: string;
  updated_at: string;
}

export interface BiReportMetricEdgeRead {
  id: string;
  report_id: string;
  metric_id: string;
  edge_kind: string;
}

export interface BiReportNodeRead {
  id: string;
  artifact_import_id: string;
  parent_report_id: string | null;
  external_id: string;
  name: string;
  report_type: string;
  project_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface BulkStewardshipOperationCreate {
  operation_type: "ASSIGN_OWNERSHIP" | "LINK_TERM" | "DEPRECATE_TERM" | "CERTIFY_ASSET";
  subject_type: "TABLE" | "TERM";
  subject_ids: string[];
  owner_type?: "INDIVIDUAL" | "GROUP" | null;
  owner_principal?: string | null;
  term_id?: string | null;
  rationale?: string | null;
  expires_at?: string | null;
  source_rule_id?: string | null;
}

export interface BulkStewardshipOperationRead {
  id: string;
  organization_id: string;
  operation_type: string;
  subject_type: string;
  subject_ids: string[];
  parameters: Record<string, unknown>;
  status: string;
  governance_review_id: string;
  requested_by: string;
  applied_by: string | null;
  applied_at: string | null;
  applied_count: number;
  created_at: string;
  updated_at: string;
}

export interface BusinessAssignmentCreate {
  business_node_id: string;
  target_type: "PROJECT" | "WORKSPACE" | "DATASOURCE" | "TABLE" | "COLUMN" | "VIEW" | "METRIC" | "GLOSSARY_TERM" | "DATA_PRODUCT" | "KNOWLEDGE_PAGE";
  target_id: string;
  confidence?: number | null;
}

export interface BusinessAssignmentRead {
  id: string;
  organization_id: string;
  business_node_id: string;
  target_type: string;
  target_id: string;
  assignment_kind: string;
  confidence: number | null;
  assigned_by: string;
  confirmed_by: string | null;
  effective_from: string;
  effective_to: string | null;
  status: string;
}

export interface BusinessMapEdgeRead {
  id: string;
  edge_type: "DOMAIN_CONTAINS_ENTITY" | "ENTITY_REPRESENTED_BY_TABLE" | "CROSS_DOMAIN_FOREIGN_KEY";
  source_node_id: string;
  target_node_id: string;
  evidence?: Record<string, unknown>;
}

export interface BusinessMapNodeRead {
  id: string;
  node_type: "DOMAIN" | "ENTITY" | "TABLE";
  label: string;
  parent_id: string | null;
  metadata?: Record<string, unknown>;
}

export interface BusinessMapRead {
  organization_id: string;
  nodes: BusinessMapNodeRead[];
  edges: BusinessMapEdgeRead[];
  domain_count: number;
  entity_count: number;
  table_count: number;
  cross_domain_edge_count: number;
  truncated: boolean;
}

export interface BusinessNodeCreate {
  kind: "LOB" | "SUB_LOB" | "DOMAIN" | "SUB_DOMAIN" | "CONCEPT";
  name: string;
  code: string;
  parent_id?: string | null;
  description?: string;
  owner_principal?: string | null;
}

export interface BusinessNodeRead {
  id: string;
  organization_id: string;
  parent_id: string | null;
  kind: string;
  name: string;
  code: string;
  description: string;
  owner_principal: string | null;
  origin: string;
  effective_from: string;
  effective_to: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface BusinessNodeRollupRead {
  business_node_id: string;
  descendant_node_count: number;
  assigned_by_target_type: Record<string, number>;
  as_of: string;
  computed_at?: string | null;
}

/** RL-2: the steward-set canonical member for an APPROVED table family, if any. */
export interface CanonicalTableMappingRead {
  id: string;
  organization_id: string;
  family_candidate_id: string;
  canonical_table_id: string;
  canonical_qualified_name: string;
  resolved_by: string;
  rationale: string;
  is_steward_override: boolean;
  created_at: string;
  updated_at: string;
}

/** Steward decision naming (or clearing) the canonical member of an APPROVED family. */
export interface CanonicalTableOverrideRequest {
  table_id?: string | null;
  rationale: string;
}

export interface CatalogBulkActionItemRead {
  subject_id: string;
  status: "SUCCEEDED" | "FAILED";
  reason?: string | null;
}

export interface CatalogBulkActionRunRead {
  id: string;
  organization_id: string;
  action: string;
  selection_mode: string;
  parameters: Record<string, unknown>;
  requested_count: number;
  succeeded_count: number;
  failed_count: number;
  results: CatalogBulkActionItemRead[];
  requested_by: string;
  created_at: string;
}

export interface CatalogBulkCertifyRequest {
  table_ids?: string[] | null;
  filter?: CatalogBulkSelectionFilter | null;
  rationale: string;
  expires_at: string;
}

export interface CatalogBulkClassifyRequest {
  table_ids?: string[] | null;
  column_ids?: string[] | null;
  filter?: CatalogBulkSelectionFilter | null;
  column_name_pattern?: string;
  classification: "UNCLASSIFIED" | "PUBLIC" | "INTERNAL" | "CONFIDENTIAL" | "PII" | "PHI" | "PCI" | "SECRET";
}

export interface CatalogBulkOwnRequest {
  table_ids?: string[] | null;
  filter?: CatalogBulkSelectionFilter | null;
  owner_type: "INDIVIDUAL" | "GROUP";
  owner_principal: string;
}

export interface CatalogBulkSelectionFilter {
  datasource_id: string;
  match_field?: "TABLE_NAME" | "SCHEMA_NAME" | "QUALIFIED_NAME";
  match_pattern: string;
}

export interface CatalogBulkTagRequest {
  table_ids?: string[] | null;
  filter?: CatalogBulkSelectionFilter | null;
  tag_key: string;
  tag_value?: string | null;
}

/** Module 04's ``CertificationDecision``: certify the table itself, or one column. */
export interface CertificationDecisionRequest {
  asset_type?: "TABLE" | "COLUMN";
  column_id?: string | null;
  rationale: string;
  expires_at: string;
}

export interface ClassificationDecisionRead {
  classification: string;
  decision: string;
  reasons: string[];
  contributing_policy_ids: string[];
}

export interface ClassificationFeedIngestRequest {
  source: string;
  records: ClassificationFeedRecord[];
}

export interface ClassificationFeedIngestResponse {
  source: string;
  total: number;
  matched: number;
  changed: number;
  unmatched: string[];
}

export interface ClassificationFeedRecord {
  schema_name: string;
  table_name: string;
  column_name: string;
  classification: string;
  confidence?: number | null;
  note?: string | null;
}

export interface ColumnProfileRead {
  column_id: string;
  column_name: string;
  classification: string;
  null_count: number;
  non_null_count: number;
  approximate_distinct_count: number;
  min_length: number | null;
  max_length: number | null;
}

export interface CompliancePackRead {
  id: string;
  organization_id: string;
  name: string;
  framework: string;
  period_start: string;
  period_end: string;
  sections: Record<string, unknown>[];
  status: string;
  checksum: string;
  generated_by: string;
  generated_at: string;
  created_at: string;
  updated_at: string;
}

export interface CompositeKeyCandidateDecision {
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

export interface CompositeKeyCandidateRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  table_id: string;
  column_ids: string[];
  detection_rule: string;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
  created_by: string;
  reviewed_by: string | null;
  review_reason: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface CompositeRelationshipCandidateDiscoveryRequest {
  max_candidates?: number;
}

export interface CompositeRelationshipCandidateMemberRead {
  ordinal: number;
  source_column_id: string;
  target_column_id: string;
  source_column_name: string;
  target_column_name: string;
}

export interface CompositeRelationshipCandidateRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  source_table_id: string;
  target_table_id: string;
  detection_rule: string;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
  created_by: string;
  reviewed_by: string | null;
  review_reason: string | null;
  reviewed_at: string | null;
  members: CompositeRelationshipCandidateMemberRead[];
  created_at: string;
  updated_at: string;
}

export interface ConnectorCapabilityRead {
  connector_type: string;
  display_name: string;
  dialect: string;
  implementation_status: string;
  transports: string[];
  maturity: string;
  version: string;
  notes: string;
  capabilities: Record<string, boolean>;
}

export interface ConnectorCertificationRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  connector_type: string;
  connector_version: string;
  suite_version: string;
  status: string;
  score: number;
  checks: Record<string, unknown>[];
  initiated_by: string;
  completed_at: string;
  created_at: string;
  updated_at: string;
}

export interface ConnectorHealthFactorRead {
  name: string;
  score: number;
  maximum: number;
  reason: string;
  evidence: Record<string, unknown>;
}

export interface ConnectorHealthScoreRead {
  datasource_id: string;
  score: number;
  status: string;
  factors: ConnectorHealthFactorRead[];
  blockers: string[];
  computed_at: string;
}

/** One consumer of the resource: who/what, over which channel it most */
export interface ConsumerFooterEntryRead {
  consumer_id: string;
  consumer_type: string;
  channel: string;
  consumption_count: number;
  last_consumed_at: string;
}

/** CX-4 consumption lineage, scoped to one specific version of one */
export interface ConsumerFooterRead {
  resource_type: string;
  resource_id: string;
  version: number | null;
  generated_at: string;
  total_consumption_events: number;
  consumers: ConsumerFooterEntryRead[];
  total_consumers: number;
}

export interface ConsumptionRecordPage {
  items: ConsumptionRecordRead[];
  total: number;
  limit: number;
  offset: number;
}

export interface ConsumptionRecordRead {
  id: string;
  organization_id: string;
  consumer_id: string;
  consumer_type: string;
  resource_type: string;
  resource_id: string;
  channel: string;
  correlation_id: string;
  policy_decision: string;
  business_purpose?: string | null;
  details?: Record<string, unknown>;
  consumed_at: string;
}

export interface ContextCompilationDriftRead {
  target: "MCP" | "REST" | "YAML" | "OSI" | "ODCS" | "SNOWFLAKE_SEMANTIC_VIEW" | "DATABRICKS_METRIC_VIEW";
  drifted: boolean;
  expected_hash: string;
  deployed_hash: string;
  changed_paths: string[];
}

export interface ContextCompilationDriftRequest {
  target: "MCP" | "REST" | "YAML" | "OSI" | "ODCS" | "SNOWFLAKE_SEMANTIC_VIEW" | "DATABRICKS_METRIC_VIEW";
  deployed_hash?: string | null;
  deployed_content?: string | null;
}

export interface ContextCompilationRead {
  target: "MCP" | "REST" | "YAML" | "OSI" | "ODCS" | "SNOWFLAKE_SEMANTIC_VIEW" | "DATABRICKS_METRIC_VIEW";
  content_type: string;
  content: string;
  artifact_hash: string;
  source_fingerprint: string;
  generated_from: Record<string, unknown>;
}

export interface ContextCompilationValidateRequest {
  target: "MCP" | "REST" | "YAML" | "OSI" | "ODCS" | "SNOWFLAKE_SEMANTIC_VIEW" | "DATABRICKS_METRIC_VIEW";
  content: string;
}

export interface ContextCompilationValidationRead {
  target: "MCP" | "REST" | "YAML" | "OSI" | "ODCS" | "SNOWFLAKE_SEMANTIC_VIEW" | "DATABRICKS_METRIC_VIEW";
  valid: boolean;
  findings: string[];
}

/** AT-7(b): pin `consumer_principal_id` (the path parameter) to this */
export interface ContextProductConsumerBindingCreate {
  bound_version_id: string;
}

export interface ContextProductConsumerBindingRead {
  id: string;
  organization_id: string;
  product_id: string;
  consumer_principal_id: string;
  bound_version_id: string;
  bound_version_number: number;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ContextProductCreate {
  name: string;
  description: string;
  purpose: string;
  owner_type: "INDIVIDUAL" | "GROUP";
  owner_principal: string;
  table_ids?: string[];
  semantic_model_version_ids?: string[];
  glossary_term_version_ids?: string[];
  eligible_tool_version_ids?: string[];
  allowed_consumer_roles: string[];
  lineage_depth?: number;
  quality_requirements?: ContextProductQualityRequirements;
  policy_summary?: ContextProductPolicySummary;
  support_window_days?: number | null;
  product_key: string;
}

export interface ContextProductPolicySummary {
  source_values?: "GATEWAY_ONLY";
  retention?: "NO_RAW_CONTEXT";
  permitted_actions?: ("READ_CONTEXT" | "INVOKE_ELIGIBLE_TOOLS")[];
}

export interface ContextProductQualityRequirements {
  minimum_score?: number;
  deny_on_critical_incident?: boolean;
}

export interface ContextProductRead {
  id: string;
  organization_id: string;
  project_id: string;
  product_key: string;
  lifecycle_status: string;
  created_by: string;
  latest_version: ContextProductVersionRead;
  created_at: string;
  updated_at: string;
}

/** Both ADR-0017 SS9 axes for one context product version, composed for an */
export interface ContextProductScopeRead {
  context_product_version_id: string;
  product_data_domain_id: string;
  data_domain_ids: string[];
  ungranted_data_domain_ids: string[];
  business_domain_names: string[];
  cross_domain: boolean;
  table_count: number;
  unresolved_table_ids: string[];
}

export interface ContextProductVersionCreate {
  name: string;
  description: string;
  purpose: string;
  owner_type: "INDIVIDUAL" | "GROUP";
  owner_principal: string;
  table_ids?: string[];
  semantic_model_version_ids?: string[];
  glossary_term_version_ids?: string[];
  eligible_tool_version_ids?: string[];
  allowed_consumer_roles: string[];
  lineage_depth?: number;
  quality_requirements?: ContextProductQualityRequirements;
  policy_summary?: ContextProductPolicySummary;
  support_window_days?: number | null;
  based_on_version_id?: string | null;
}

export interface ContextProductVersionRead {
  name: string;
  description: string;
  purpose: string;
  owner_type: "INDIVIDUAL" | "GROUP";
  owner_principal: string;
  table_ids?: string[];
  semantic_model_version_ids?: string[];
  glossary_term_version_ids?: string[];
  eligible_tool_version_ids?: string[];
  allowed_consumer_roles: string[];
  lineage_depth?: number;
  quality_requirements?: ContextProductQualityRequirements;
  policy_summary?: ContextProductPolicySummary;
  support_window_days?: number | null;
  id: string;
  organization_id: string;
  product_id: string;
  product_key: string;
  version: number;
  status: string;
  fingerprint: string;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  published_at: string | null;
  based_on_version_id: string | null;
  created_at: string;
  updated_at: string;
  superseded_at?: string | null;
  support_window_ends_at?: string | null;
  superseded_by_version_id?: string | null;
}

export interface ContextProductVersionUpdate {
  name: string;
  description: string;
  purpose: string;
  owner_type: "INDIVIDUAL" | "GROUP";
  owner_principal: string;
  table_ids?: string[];
  semantic_model_version_ids?: string[];
  glossary_term_version_ids?: string[];
  eligible_tool_version_ids?: string[];
  allowed_consumer_roles: string[];
  lineage_depth?: number;
  quality_requirements?: ContextProductQualityRequirements;
  policy_summary?: ContextProductPolicySummary;
  support_window_days?: number | null;
}

export interface ContractFieldDefinition {
  name: string;
  data_type: string;
  required?: boolean;
  description?: string | null;
  classification?: string | null;
}

export interface ContractQualityRuleDefinition {
  rule_key: string;
  rule_type: "NOT_NULL" | "UNIQUE" | "ACCEPTED_VALUES" | "FRESHNESS" | "CUSTOM";
  field_name?: string | null;
  severity?: "INFO" | "WARNING" | "CRITICAL";
  parameters?: Record<string, unknown>;
}

export interface CostShowbackRead {
  organization_id: string;
  period_start: string;
  period_end: string;
  generated_at: string;
  cost_basis: string;
  rows: LobCostRowRead[];
  totals: CostShowbackTotalsRead;
}

export interface CostShowbackTotalsRead {
  datasource_count: number;
  query_count: number;
  completed_count: number;
  rejected_count: number;
  failed_count: number;
  total_row_count: number;
  total_elapsed_ms: number;
  total_plan_cost_units: number | null;
}

export interface CoverageDimensionRead {
  covered: number;
  total: number;
  percentage: number;
}

export interface CrossBoundaryGrantCreate {
  target_data_domain_id: string;
  edge_kinds?: string[];
  reason: string;
  expires_at?: string | null;
}

export interface CrossBoundaryGrantRead {
  id: string;
  organization_id: string;
  source_data_domain_id: string;
  target_data_domain_id: string;
  edge_kinds: string[];
  reason: string;
  status: string;
  requested_by: string;
  approved_by: string | null;
  approved_at: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface CrossSourceObjectResolutionDiscoveryRequest {
  max_candidates?: number;
  max_datasource_pairs?: number;
  target_data_domain_id?: string | null;
}

export interface CrossSourceRelationshipCandidateDiscoveryRequest {
  max_candidates?: number;
  max_datasource_pairs?: number;
  target_data_domain_id?: string | null;
}

export interface CrossSourceResolutionCandidateDecision {
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

export interface CrossSourceResolutionCandidateRead {
  id: string;
  organization_id: string;
  source_datasource_id: string;
  source_table_id: string;
  target_datasource_id: string;
  target_table_id: string;
  detection_rule: string;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
  created_by: string;
  reviewed_by: string | null;
  review_reason: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

/** CT-2: `Page` variant for the high-volume catalog list endpoints (tables, */
export interface CursorPage<T = unknown> {
  items: T[];
  limit: number;
  offset: number;
  total?: number | null;
  next_cursor?: string | null;
}

export interface DataContractCreate {
  compatibility_mode?: "BACKWARD" | "FORWARD" | "FULL" | "NONE";
  schema_definition: ContractFieldDefinition[];
  quality_rules?: ContractQualityRuleDefinition[];
  freshness_sla_minutes?: number | null;
  availability_sla_percent?: number | null;
  producer_principal: string;
  consumer_roles?: string[];
}

export interface DataContractVersionRead {
  compatibility_mode?: "BACKWARD" | "FORWARD" | "FULL" | "NONE";
  schema_definition: ContractFieldDefinition[];
  quality_rules?: ContractQualityRuleDefinition[];
  freshness_sla_minutes?: number | null;
  availability_sla_percent?: number | null;
  producer_principal: string;
  consumer_roles?: string[];
  id: string;
  organization_id: string;
  product_id: string;
  version: number;
  status: string;
  compatibility_status: string;
  compatibility_findings: Record<string, unknown>[];
  fingerprint: string;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DataDomainCreate {
  name: string;
  code: string;
  parent_domain_id?: string | null;
}

export interface DataDomainRead {
  id: string;
  organization_id: string;
  line_of_business_id: string;
  parent_domain_id: string | null;
  name: string;
  code: string;
  is_default: boolean;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface DataProductCreate {
  name: string;
  description: string;
  domain_name: string;
  owner_principal: string;
  usage_terms: string;
  classification: "PUBLIC" | "INTERNAL" | "CONFIDENTIAL" | "RESTRICTED";
  certification_status?: "UNCERTIFIED" | "CERTIFIED" | "EXPIRED";
  quality_score?: number | null;
  lineage_coverage?: number;
  context_product_version_id?: string | null;
  discoverable_roles?: string[];
  consumer_roles?: string[];
  ports: DataProductPortDefinition[];
  product_key: string;
}

export interface DataProductPortDefinition {
  port_key: string;
  direction: "INPUT" | "OUTPUT";
  name: string;
  description: string;
  asset_type: "TABLE" | "SEMANTIC_MODEL" | "CONTEXT_PRODUCT" | "API";
  asset_id: string;
}

export interface DataProductVersionCreate {
  name: string;
  description: string;
  domain_name: string;
  owner_principal: string;
  usage_terms: string;
  classification: "PUBLIC" | "INTERNAL" | "CONFIDENTIAL" | "RESTRICTED";
  certification_status?: "UNCERTIFIED" | "CERTIFIED" | "EXPIRED";
  quality_score?: number | null;
  lineage_coverage?: number;
  context_product_version_id?: string | null;
  discoverable_roles?: string[];
  consumer_roles?: string[];
  ports: DataProductPortDefinition[];
}

export interface DataProductVersionRead {
  name: string;
  description: string;
  domain_name: string;
  owner_principal: string;
  usage_terms: string;
  classification: "PUBLIC" | "INTERNAL" | "CONFIDENTIAL" | "RESTRICTED";
  certification_status?: "UNCERTIFIED" | "CERTIFIED" | "EXPIRED";
  quality_score?: number | null;
  lineage_coverage?: number;
  context_product_version_id?: string | null;
  discoverable_roles?: string[];
  consumer_roles?: string[];
  ports: DataProductPortDefinition[];
  id: string;
  organization_id: string;
  product_id: string;
  product_key: string;
  version: number;
  status: string;
  fingerprint: string;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DataQualityIncidentRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  table_id: string;
  table_name: string;
  policy_id: string | null;
  latest_observation_id?: string | null;
  anomaly_type: string;
  severity: string;
  status: string;
  summary: string;
  evidence: Record<string, unknown>;
  occurrence_count: number;
  first_observed_at: string;
  last_observed_at: string;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  resolution_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface DataQualityIncidentTransition {
  status: "ACKNOWLEDGED" | "RESOLVED";
  reason: string;
}

export interface DataQualityPolicyRead {
  table_id?: string | null;
  name?: string;
  enabled?: boolean;
  volume_change_percent?: number;
  null_rate_change_percent?: number;
  schema_change_enabled?: boolean;
  metadata_scan_max_age_minutes?: number;
  id: string;
  organization_id: string;
  datasource_id: string;
  scope_key: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface DataQualityPolicyUpsert {
  table_id?: string | null;
  name?: string;
  enabled?: boolean;
  volume_change_percent?: number;
  null_rate_change_percent?: number;
  schema_change_enabled?: boolean;
  metadata_scan_max_age_minutes?: number;
}

export interface DataQualitySummaryRead {
  datasource_id: string;
  table_count: number;
  observed_table_count: number;
  status_counts: Record<string, number>;
  open_incident_count: number;
  critical_incident_count: number;
  average_quality_score: number | null;
  last_observed_at: string | null;
  metadata_scan_age_minutes: number | null;
  metadata_scan_status: string;
  source_freshness_status: "NOT_CONFIGURED";
}

export interface DataSourceBulkOnboardItemRead {
  index: number;
  name: string;
  status: "SUCCEEDED" | "FAILED";
  datasource_id?: string | null;
  reason?: string | null;
}

export interface DataSourceBulkOnboardRequest {
  datasources: DataSourceCreate[];
}

export interface DataSourceBulkOnboardResultRead {
  requested_count: number;
  succeeded_count: number;
  failed_count: number;
  results: DataSourceBulkOnboardItemRead[];
}

export interface DataSourceCreate {
  name: string;
  connector_type: string;
  dialect: string;
  environment: string;
  network_zone?: string;
  credential_reference: string;
  max_concurrency?: number;
}

export interface DataSourceRead {
  name: string;
  connector_type: string;
  dialect: string;
  environment: string;
  network_zone?: string;
  credential_reference: string;
  max_concurrency?: number;
  id: string;
  organization_id: string;
  line_of_business_id: string;
  data_domain_id: string;
  project_id: string;
  status: string;
  capabilities: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface DataSourceUpdate {
  enabled?: boolean | null;
  max_concurrency?: number | null;
  network_zone?: string | null;
}

export interface DbtArtifactImportRead {
  id: string;
  organization_id: string;
  dbt_project_id: string;
  manifest_fingerprint: string;
  dbt_schema_version: string;
  dbt_version: string | null;
  invocation_id: string | null;
  generated_at: string | null;
  status: string;
  resource_count: number;
  model_count: number;
  source_count: number;
  test_count: number;
  lineage_edge_count: number;
  matched_resource_count: number;
  unmatched_resource_count: number;
  imported_by: string;
  created_at: string;
  updated_at: string;
}

export interface DbtArtifactImportRequest {
  manifest: Record<string, unknown>;
  catalog?: Record<string, unknown> | null;
  run_results?: Record<string, unknown> | null;
}

export interface DbtLineageEdgeRead {
  id: string;
  source_resource_id: string;
  target_resource_id: string;
  edge_type: string;
  source_column?: string;
  target_column?: string;
  transformation_type?: string | null;
  confidence?: string | null;
}

export interface DbtLineageNodeRead {
  id: string;
  unique_id: string;
  label: string;
  resource_type: string;
  materialization: string | null;
  matched_table_id: string | null;
  test_status?: string | null;
}

export interface DbtLineageRead {
  artifact_import_id: string;
  nodes: DbtLineageNodeRead[];
  edges: DbtLineageEdgeRead[];
  resource_count: number;
  edge_count: number;
  catalog_match_count: number;
}

export interface DbtProjectCreate {
  project_key: string;
  display_name: string;
  datasource_id: string;
  repository_url?: string | null;
  target_name?: string;
}

export interface DbtProjectRead {
  id: string;
  organization_id: string;
  project_id: string;
  datasource_id: string;
  project_key: string;
  display_name: string;
  repository_url: string | null;
  target_name: string;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface DelegationCreate {
  delegate_principal_id: string;
  delegated_roles: string[];
  reason: string;
  starts_at?: string | null;
  expires_at: string;
}

export interface DelegationRead {
  id: string;
  organization_id: string;
  delegator_principal_id: string;
  delegate_principal_id: string;
  delegated_roles: string[];
  reason: string;
  starts_at: string;
  expires_at: string;
  status: string;
  created_by: string;
  revoked_by: string | null;
  revoked_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DetokenizeRead {
  value: string;
  detokenized_at: string;
}

export interface DetokenizeRequest {
  token: string;
  purpose: string;
  datasource_id?: string | null;
}

/** Same merged FK + suggested + dbt + OpenLineage + view/procedure graph as */
export interface DomainLineageGraphRead {
  data_domain_id: string;
  datasource_ids: string[];
  nodes: UnifiedLineageNodeRead[];
  edges: UnifiedLineageEdgeRead[];
  counts_by_source: Record<string, number>;
  returned_node_count?: number;
  returned_edge_count?: number;
  node_limit?: number;
  edge_limit?: number;
  truncated?: boolean;
  truncation_reasons?: string[];
  withheld_cross_boundary_domain_ids?: string[];
}

export interface EntitlementOperation {
  action: "PROVISION" | "REVOKE";
}

export interface EntitlementReportRead {
  id: string;
  organization_id: string;
  subject_principal_id: string;
  subject_principal_type: string;
  is_self_service: boolean;
  requested_by: string;
  workspace_memberships: WorkspaceEntitlementRead[];
  source_entitlements: SourceEntitlementRead[];
  abac_classification_decisions: ClassificationDecisionRead[];
  abac_note: string;
  checksum: string;
  generated_at: string;
  created_at: string;
  updated_at: string;
}

export interface EvaluationResponse {
  contract_id: string;
  violations: Record<string, unknown>[];
  enforcement_action: string;
  allowed: boolean;
  reason?: string | null;
}

/** UX-13: one claim in an asset's evidence pane. */
export interface EvidenceItemRead {
  category: string;
  claim: string;
  source: string;
  occurred_at?: string | null;
}

export interface ExecutionRead {
  id: string;
  organization_id: string;
  plan_id: string;
  started_at: string;
  completed_at: string | null;
  budget_consumed: Record<string, unknown>;
  status: string;
  executed_by: string;
  created_at: string;
  updated_at: string;
}

export interface FleetSummaryRead {
  organization_id: string;
  datasource_statuses: Record<string, number>;
  analysis_run_statuses: Record<string, number>;
  scan_policies_enabled: number;
  scan_policies_due: number;
  pending_outbox_events: number;
  dead_letter_outbox_events: number;
  generated_at: string;
}

export interface FreshnessConfigRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  table_id: string;
  watermark_column: string;
  classification: string;
  threshold_minutes: number;
  retention_days: number;
  status: string;
  approved_by: string | null;
  approved_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface FreshnessConfigUpsert {
  watermark_column: string;
  classification?: string;
  threshold_minutes: number;
  retention_days?: number;
}

export interface FreshnessStatusRead {
  table_id: string;
  status: string;
  last_watermark: string | null;
  age_minutes: number | null;
  threshold_minutes: number | null;
  evidence: Record<string, unknown>;
}

export interface GatewaySqlValidationRequest {
  sql: string;
  max_rows?: number | null;
  workspace_id?: string | null;
}

export interface GatewaySqlValidationResponse {
  valid: boolean;
  dialect: string;
  findings: SqlFindingRead[];
  normalized_sql?: string | null;
  referenced_tables: string[];
  referenced_columns: string[];
  applied_row_limit?: number | null;
  column_lineage: Record<string, unknown>[];
  estimate: QueryEstimateRead;
  rejection_reason?: string | null;
}

export interface GenerateEntitlementReportRequest {
  principal_id?: string | null;
  principal_type?: string;
}

export interface GeneratePackRequest {
  framework: "MODEL_RISK" | "BCBS_239" | "ACCESS_REVIEW" | "AI_USAGE" | "CHANGE_CONTROL";
  period_start: string;
  period_end: string;
  name?: string | null;
}

export interface GlossaryCategoryCreate {
  category_key: string;
  display_name: string;
  description: string;
  parent_id?: string | null;
}

export interface GlossaryCategoryRead {
  category_key: string;
  display_name: string;
  description: string;
  parent_id?: string | null;
  id: string;
  organization_id: string;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface GlossaryConflictCreate {
  term_id?: string | null;
  conflict_type: "DEFINITION" | "SYNONYM_COLLISION" | "SOURCE_DISAGREEMENT";
  position_a: Record<string, unknown>;
  position_b: Record<string, unknown>;
  assigned_owner?: string | null;
}

export interface GlossaryConflictRead {
  id: string;
  organization_id: string;
  term_id: string | null;
  conflict_type: string;
  status: string;
  position_a: Record<string, unknown>;
  position_b: Record<string, unknown>;
  assigned_owner: string | null;
  raised_by: string;
  proposed_resolution: string | null;
  proposed_definition: string | null;
  resolution_rationale: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface GlossaryConflictResolution {
  resolution: "ACCEPT_POSITION_A" | "ACCEPT_POSITION_B" | "MERGE" | "RETAIN_BOTH";
  resolved_definition?: string | null;
  rationale: string;
}

export interface GlossaryLinkProposalGenerate {
  minimum_confidence?: number;
  limit?: number;
}

export interface GlossaryTermCreate {
  term_key: string;
  display_name: string;
  definition: string;
  category_id?: string | null;
  synonyms?: string[];
  owner_principal?: string | null;
}

export interface GlossaryTermDeprecationRequest {
  reason: string;
}

export interface GlossaryTermVersionCreate {
  display_name: string;
  definition: string;
  category_id?: string | null;
  synonyms?: string[];
  owner_principal?: string | null;
}

export interface GlossaryTermVersionRead {
  id: string;
  organization_id: string;
  term_id: string;
  term_key: string;
  category_id: string | null;
  lifecycle_status: string;
  version: number;
  status: string;
  display_name: string;
  definition: string;
  synonyms: string[];
  owner_principal: string | null;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface GovernanceDecisionRequest {
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

export interface GovernanceReviewBulkDecisionItemRead {
  review_id: string;
  status: "SUCCEEDED" | "FAILED";
  reason?: string | null;
}

export interface GovernanceReviewBulkDecisionRequest {
  review_ids?: string[] | null;
  filter?: GovernanceReviewBulkSelectionFilter | null;
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
  rationale_by_review_id?: Record<string, string> | null;
}

export interface GovernanceReviewBulkDecisionResultRead {
  decision: "APPROVE" | "REJECT";
  selection_mode: "EXPLICIT" | "FILTER";
  requested_count: number;
  succeeded_count: number;
  failed_count: number;
  truncated: boolean;
  results: GovernanceReviewBulkDecisionItemRead[];
}

/** Reuses `list_governance_reviews`'s existing filter shape (status, */
export interface GovernanceReviewBulkSelectionFilter {
  object_type?: string | null;
  status?: string;
}

/** Structured version delta for one pending (or decided) governance review. */
export interface GovernanceReviewDiffRead {
  review_id: string;
  object_type: string;
  object_id: string;
  diffable: boolean;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  entries?: SemanticFieldDeltaRead[];
  message?: string | null;
}

export interface GovernanceReviewRead {
  id: string;
  organization_id: string;
  object_type: string;
  object_id: string;
  requested_action: string;
  status: string;
  requested_by: string;
  decided_by: string | null;
  decision_reason: string | null;
  decided_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface GovernedToolVersionCreate {
  slug: string;
  name: string;
  description: string;
  datasource_id: string;
  semantic_model_version_id?: string | null;
  sql_template: string;
  parameters?: ToolParameterDefinition[];
  allowed_roles: string[];
}

export interface GovernedToolVersionRead {
  id: string;
  tool_id: string;
  organization_id: string;
  project_id: string;
  slug: string;
  version: number;
  status: string;
  name: string;
  description: string;
  datasource_id: string;
  semantic_model_version_id: string | null;
  sql_template: string;
  referenced_tables: string[];
  parameters: ToolParameterDefinition[];
  allowed_roles: string[];
  fingerprint: string;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
  usage_count?: number;
}

export interface GraphEdgeRead {
  id: string;
  edge_type: "DECLARED_FOREIGN_KEY" | "SUGGESTED_RELATIONSHIP";
  source_node_id: string;
  target_node_id: string;
  source_label: string;
  target_label: string;
  source_columns: string[];
  target_columns: string[];
  status: string;
  confidence: number;
  evidence: Record<string, unknown>;
  candidate_id?: string | null;
}

export interface GraphNodeRead {
  id: string;
  node_type: "TABLE";
  label: string;
  qualified_name: string;
  object_type: string;
  status: string;
  column_count: number;
  sensitive_column_count: number;
  depth?: number;
  inbound_edge_count?: number;
  outbound_edge_count?: number;
}

/** Opaque frontend Graph Explorer state, plus queryable metadata. */
export interface GraphPerspectiveCreate {
  datasource_id?: string | null;
  name: string;
  description?: string | null;
  allowed_viewer_roles?: string[];
  view_state?: Record<string, unknown>;
}

export interface GraphPerspectiveRead {
  id: string;
  organization_id: string;
  datasource_id: string | null;
  name: string;
  description: string | null;
  owner_principal: string;
  allowed_viewer_roles: string[];
  view_state: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

/** All fields optional: only owner-supplied fields are applied (owner-only, see the API). */
export interface GraphPerspectiveUpdate {
  name?: string | null;
  description?: string | null;
  allowed_viewer_roles?: string[] | null;
  view_state?: Record<string, unknown> | null;
}

export interface GraphSearchRead {
  datasource_id: string;
  query: string;
  items: GraphNodeRead[];
  total: number;
  truncated: boolean;
}

export interface GraphSummaryRead {
  datasource_id: string;
  catalogs: number;
  schemas: number;
  tables: number;
  columns: number;
  sensitive_columns: number;
  constraints: number;
  foreign_key_relationships: number;
  projection_status: string;
  projection_lag: Record<string, number>;
}

/** One resolved AT-6 grounding-fragment digest: what was hashed, and -- */
export interface GroundingFragmentReceiptRead {
  object_type: string;
  object_id: string;
  fragment_digest: string;
  annotation_version_id: string | null;
  annotation_version: number | null;
  annotation_status: string | null;
  business_name: string | null;
  business_description: string | null;
  digest_verified: boolean;
}

export interface HTTPValidationError {
  detail?: ValidationError[];
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  dependencies?: Record<string, string>;
}

export interface ImpactAnalysisRead {
  table_id: string;
  table_name: string;
  semantic_metric_version_ids: string[];
  governed_tool_version_ids: string[];
  approved_relationship_candidate_ids: string[];
  dbt_resource_ids?: string[];
  downstream_object_count: number;
}

export interface KillSwitchEngageRequest {
  reason: string;
  route_key?: string | null;
}

export interface KillSwitchReleaseRequest {
  reason: string;
  route_key?: string | null;
}

export interface KillSwitchStateRead {
  id: string;
  organization_id: string;
  route_key: string;
  scope: "ORGANIZATION" | "ROUTE";
  engaged: boolean;
  reason: string | null;
  engaged_by: string | null;
  engaged_at: string | null;
  released_by: string | null;
  released_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeGraphRead {
  datasource_id: string;
  nodes: GraphNodeRead[];
  edges: GraphEdgeRead[];
  total_tables: number;
  total_declared_edges: number;
  total_suggested_edges: number;
  pending_suggestions: number;
  truncated: boolean;
  focus_node_id?: string | null;
  direction?: "BOTH" | "REFERENCES" | "REFERENCED_BY";
  requested_depth?: number;
  returned_node_count?: number;
  returned_edge_count?: number;
  node_limit?: number;
  edge_limit?: number;
  truncation_reasons?: string[];
}

/** GL-7: reassign every ACTIVE `OwnershipAssignment` a leaving principal */
export interface LeaverReassignmentRequest {
  leaving_principal: string;
  successor_principal: string;
  owner_type?: "INDIVIDUAL" | "GROUP";
  assignment_ids?: string[] | null;
  rationale: string;
}

export interface LiftSuppressionRequest {
  reason: string;
}

export interface LineOfBusinessCreate {
  name: string;
  code: string;
}

export interface LineOfBusinessRead {
  name: string;
  code: string;
  id: string;
  organization_id: string;
  status: string;
  created_at: string;
  updated_at: string;
}

/** One column-level lineage edge extracted from SQL. */
export interface LineageEdgeRead {
  source_table: string;
  source_column: string;
  target_table: string;
  target_column: string;
  transformation_type: string;
  confidence: string;
  dialect: string;
}

export interface LobCostRowRead {
  line_of_business_id: string;
  line_of_business_code: string;
  line_of_business_name: string;
  datasource_count: number;
  query_count: number;
  completed_count: number;
  rejected_count: number;
  failed_count: number;
  total_row_count: number;
  total_elapsed_ms: number;
  total_plan_cost_units: number | null;
}

export interface MarketplaceAccessRequestCreate {
  purpose: string;
  duration_days?: number;
}

export interface MarketplaceAccessRequestRead {
  id: string;
  organization_id: string;
  data_product_version_id: string;
  requested_by: string;
  purpose: string;
  duration_days: number;
  status: string;
  governance_review_id: string;
  decided_by: string | null;
  decision_reason: string | null;
  decided_at: string | null;
  expires_at: string | null;
  revoked_by: string | null;
  revoked_at: string | null;
  fulfillment_status: string;
  fulfillment_provider: string | null;
  fulfillment_reference: string | null;
  fulfillment_error: string | null;
  fulfilled_at: string | null;
  created_at: string;
  updated_at: string;
}

/** HTTP-facing wrapper around ``ConversationalMarketplaceResult``: the same */
export interface MarketplaceDiscoveryResponse {
  results: Page;
  resolved_filters: MarketplaceFilterResolution;
  prompt_risk_decision: "ALLOW" | "BLOCK";
  prompt_risk_reason_codes: string[];
  prompt_risk_score: number;
}

/** The structured contract a marketplace question resolves to: exactly */
export interface MarketplaceFilterResolution {
  q?: string | null;
  domain?: string | null;
  classification?: "PUBLIC" | "INTERNAL" | "CONFIDENTIAL" | "RESTRICTED" | null;
  sort?: "personalized" | "catalog";
  rationale_codes?: string[];
}

export interface MeRead {
  principal_id: string;
  principal_type: string;
  organization_id: string | null;
  roles: string[];
  persona: string | null;
  identity_provider: string;
}

export interface MetadataBusinessAnnotationRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  table_id: string;
  schema_name: string;
  table_name: string;
  domain_id: string;
  domain_key: string;
  domain_name: string;
  entity_id: string;
  entity_key: string;
  entity_name: string;
  source_proposal_id: string;
  version: number;
  business_name: string;
  business_description: string;
  table_role: string;
  grain_statement: string;
  synonyms: string[];
  suggested_questions: string[];
  tags: string[];
  confidence: number;
  approved_by: string;
  approved_at: string;
  created_at: string;
  updated_at: string;
}

export interface MetadataCatalogEnvelope {
  name: string;
  source_description?: string | null;
  attributes?: Record<string, string | number | boolean | null>;
  schemas: MetadataSchemaEnvelope[];
}

export interface MetadataColumnEnvelope {
  name: string;
  ordinal_position: number;
  physical_type: string;
  nullable: boolean;
  default_expression?: string | null;
  source_description?: string | null;
  attributes?: Record<string, string | number | boolean | null>;
}

export interface MetadataConstraintEnvelope {
  name: string;
  constraint_type: "PRIMARY_KEY" | "UNIQUE" | "FOREIGN_KEY";
  columns: string[];
  referenced_schema?: string | null;
  referenced_table?: string | null;
  referenced_columns?: string[];
}

/** One privilege held by one grantee on one source object. */
export interface MetadataGrantEnvelope {
  grantee: string;
  grantee_type?: "USER" | "ROLE" | "GROUP" | "PUBLIC";
  privilege: string;
  object_type?: "TABLE" | "VIEW" | "PROCEDURE" | "FUNCTION" | "SCHEMA" | "SEQUENCE";
  object_name: string;
  schema_name?: string | null;
  is_grantable?: boolean;
}

export interface MetadataIngestionBatchCreate {
  envelope_version?: "1.0" | "1.1";
  batch_key: string;
  producer: string;
  snapshot_type?: "FULL" | "INCREMENTAL";
  expected_chunks: number;
}

export interface MetadataIngestionBatchRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  analysis_run_id: string | null;
  batch_key: string;
  envelope_version: string;
  producer: string;
  snapshot_type: string;
  expected_chunks: number;
  received_chunks: number;
  processed_chunks: number;
  status: string;
  temporal_workflow_id: string | null;
  object_counts: Record<string, unknown>;
  change_counts: Record<string, unknown>;
  submitted_by: string;
  finalized_at: string | null;
  completed_at: string | null;
  error_class: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface MetadataIngestionChunkCreate {
  chunk_number: number;
  chunk_key: string;
  emitted_at: string;
  catalogs: MetadataCatalogEnvelope[];
}

export interface MetadataIngestionChunkRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  batch_id: string;
  chunk_number: number;
  chunk_key: string;
  emitted_at: string;
  payload_fingerprint: string;
  object_counts: Record<string, unknown>;
  change_counts: Record<string, unknown>;
  status: string;
  processed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface MetadataIngestionCreate {
  envelope_version?: "1.0" | "1.1";
  idempotency_key: string;
  producer: string;
  transport?: "PUSH" | "STREAM";
  snapshot_type?: "FULL" | "INCREMENTAL";
  emitted_at: string;
  catalogs: MetadataCatalogEnvelope[];
}

export interface MetadataIngestionRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  analysis_run_id: string | null;
  idempotency_key: string;
  envelope_version: string;
  producer: string;
  transport: string;
  snapshot_type: string;
  payload_fingerprint: string;
  status: string;
  object_counts: Record<string, unknown>;
  change_counts: Record<string, unknown>;
  submitted_by: string;
  error_class: string | null;
  error_message: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

/** A stored procedure or function, with its body when the source exposes it. */
export interface MetadataRoutineEnvelope {
  name: string;
  routine_type: "FUNCTION" | "PROCEDURE";
  language?: string | null;
  body_sql?: string | null;
  parameters?: MetadataRoutineParameterEnvelope[];
  return_type?: string | null;
  is_deterministic?: boolean | null;
  security_mode?: "DEFINER" | "INVOKER" | null;
  source_description?: string | null;
  truncated?: boolean;
  unavailable_reason?: string | null;
  attributes?: Record<string, string | number | boolean | null>;
}

export interface MetadataRoutineParameterEnvelope {
  name?: string | null;
  ordinal_position: number;
  mode?: "IN" | "OUT" | "INOUT" | "VARIADIC" | "TABLE";
  physical_type: string;
  default_expression?: string | null;
}

export interface MetadataSchemaEnvelope {
  name: string;
  source_description?: string | null;
  attributes?: Record<string, string | number | boolean | null>;
  tables: MetadataTableEnvelope[];
  routines?: MetadataRoutineEnvelope[];
  grants?: MetadataGrantEnvelope[];
}

export interface MetadataTableEnvelope {
  name: string;
  object_type: string;
  source_description?: string | null;
  view_definition?: MetadataViewDefinitionEnvelope | null;
  attributes?: Record<string, string | number | boolean | null>;
  columns: MetadataColumnEnvelope[];
  constraints?: MetadataConstraintEnvelope[];
}

/** The text a view is defined by, and how much of it the source would give. */
export interface MetadataViewDefinitionEnvelope {
  definition_sql?: string | null;
  is_materialized?: boolean;
  is_updatable?: boolean | null;
  check_option?: string | null;
  truncated?: boolean;
  unavailable_reason?: string | null;
}

export interface MetricSuggestionProposalGenerate {
  limit?: number;
}

export interface ModelRouteConfigurationCreate {
  route_key: string;
  display_name: string;
  provider_type: "OPENAI" | "GOOGLE_GEMINI" | "AZURE_OPENAI" | "AWS_BEDROCK" | "GOOGLE_VERTEX" | "OPENAI_COMPATIBLE_PRIVATE" | "ON_PREM";
  model_id: string;
  endpoint_alias: string;
  credential_reference?: string | null;
  data_residency: string;
  retention_policy: "ZERO_RETENTION" | "BANK_MANAGED" | "PROVIDER_CONTRACT";
  capabilities: ("SQL_GENERATION" | "EXPLANATION" | "EMBEDDINGS" | "CLASSIFICATION")[];
  max_input_tokens?: number;
  max_output_tokens?: number;
  timeout_seconds?: number;
}

export interface ModelRouteConfigurationRead {
  id: string;
  organization_id: string;
  route_key: string;
  version: number;
  status: string;
  display_name: string;
  provider_type: string;
  model_id: string;
  endpoint_alias: string;
  uses_credential_reference: boolean;
  data_residency: string;
  retention_policy: string;
  capabilities: string[];
  max_input_tokens: number;
  max_output_tokens: number;
  timeout_seconds: number;
  fingerprint: string;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  selected_by_runtime: boolean;
  adapter_available: boolean;
  activation_status: string;
  created_at: string;
  updated_at: string;
}

/** SM-5: request a deterministically-rendered multi-table JOIN tool */
export interface MultiTableToolBlueprintRequest {
  slug: string;
  name: string;
  description: string;
  datasource_id: string;
  semantic_model_version_id?: string | null;
  table_ids: string[];
  allowed_roles: string[];
}

export interface NativePolicySyncDecisionRequest {
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

export interface NativePolicySyncTableRequest {
  schema_name: string;
  table_name: string;
}

export interface NativeStatementRead {
  kind: string;
  sql: string;
  target_schema: string;
  target_table: string;
  target_column: string | null;
  policy_code: string;
}

export interface NativeSyncPlanRead {
  datasource_id: string;
  connector_type: string;
  schema_name: string;
  table_name: string;
  row_policy_count: number;
  column_policy_count: number;
  statements: NativeStatementRead[];
  unsupported: string[];
}

export interface NegativeAssertionRead {
  id: string;
  organization_id: string;
  assertion_type: string;
  subject_id: string;
  predicate: Record<string, unknown>;
  evidence: Record<string, unknown>;
  rejected_by: string;
  rejected_at: string;
  suppression_active: boolean;
  material_change_hash: string | null;
  suppression_lifted_at: string | null;
  suppression_lifted_by: string | null;
  lift_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface NotificationEventRead {
  id: string;
  organization_id: string;
  incident_id: string;
  rule_id: string;
  channel: string;
  recipients: string[];
  status: string;
  dedup_key: string;
  sent_at: string | null;
  escalated_at: string | null;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface NotificationRuleCreate {
  name: string;
  conditions?: Record<string, unknown>;
  channel: "EMAIL" | "WEBHOOK" | "ITSM";
  recipients: string[];
  escalation_after_minutes?: number | null;
  enabled?: boolean;
}

export interface NotificationRuleRead {
  id: string;
  organization_id: string;
  name: string;
  conditions: Record<string, unknown>;
  channel: string;
  recipients: string[];
  escalation_after_minutes: number | null;
  enabled: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface NotificationRuleUpdate {
  name?: string | null;
  conditions?: Record<string, unknown> | null;
  channel?: "EMAIL" | "WEBHOOK" | "ITSM" | null;
  recipients?: string[] | null;
  escalation_after_minutes?: number | null;
  enabled?: boolean | null;
}

export interface OpenLineageColumnEdgeRead {
  id: string;
  input_dataset_namespace: string;
  input_dataset_name: string;
  input_table_id: string | null;
  input_column_name: string;
  output_dataset_namespace: string;
  output_dataset_name: string;
  output_table_id: string | null;
  output_column_name: string;
  transformation_type: string | null;
  transformation_subtype: string | null;
  edge_kind: string;
  created_at: string;
  updated_at: string;
}

export interface OpenLineageDatasetRead {
  id: string;
  direction: string;
  namespace: string;
  name: string;
  matched_table_id: string | null;
  schema_fields: string[];
  created_at: string;
  updated_at: string;
}

export interface OpenLineageIngestRequest {
  datasource_id: string;
  event: Record<string, unknown>;
}

export interface OpenLineageRunEventRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  event_fingerprint: string;
  event_type: string;
  event_time: string;
  producer: string;
  schema_url: string | null;
  job_namespace: string;
  job_name: string;
  run_id: string;
  status: string;
  input_dataset_count: number;
  output_dataset_count: number;
  table_edge_count: number;
  column_edge_count: number;
  unresolved_dataset_count: number;
  imported_by: string;
  created_at: string;
  updated_at: string;
  datasets?: OpenLineageDatasetRead[];
  table_edges?: OpenLineageTableEdgeRead[];
  column_edges?: OpenLineageColumnEdgeRead[];
}

export interface OpenLineageTableEdgeRead {
  id: string;
  input_dataset_namespace: string;
  input_dataset_name: string;
  input_table_id: string | null;
  output_dataset_namespace: string;
  output_dataset_name: string;
  output_table_id: string | null;
  edge_kind: string;
  created_at: string;
  updated_at: string;
}

export interface OrganizationCreate {
  name: string;
  slug: string;
}

export interface OrganizationIntegrationPolicyRead {
  id: string;
  organization_id: string;
  transformation_metadata_integrations: Record<string, boolean>;
  created_at: string;
  updated_at: string;
}

export interface OrganizationIntegrationPolicyWrite {
  transformation_metadata_integrations?: Record<string, boolean>;
}

export interface OrganizationRead {
  name: string;
  slug: string;
  id: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface OutboxEventRead {
  id: string;
  organization_id: string | null;
  aggregate_type: string;
  aggregate_id: string;
  event_type: string;
  status: string;
  attempt_count: number;
  next_attempt_at: string;
  last_error: string | null;
  occurred_at: string;
  published_at: string | null;
}

export interface OwnershipRuleCreate {
  rule_key: string;
  display_name: string;
  match_field: "TABLE_NAME" | "SCHEMA_NAME" | "QUALIFIED_NAME" | "DOMAIN_KEY" | "TAG";
  match_pattern: string;
  owner_type: "INDIVIDUAL" | "GROUP";
  owner_principal: string;
}

export interface OwnershipRuleRead {
  rule_key: string;
  display_name: string;
  match_field: "TABLE_NAME" | "SCHEMA_NAME" | "QUALIFIED_NAME" | "DOMAIN_KEY" | "TAG";
  match_pattern: string;
  owner_type: "INDIVIDUAL" | "GROUP";
  owner_principal: string;
  id: string;
  organization_id: string;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface Page {
  items: unknown[];
  limit: number;
  offset: number;
  total: number;
}

export interface PlanBudgetCreate {
  max_steps?: number;
  max_time_seconds?: number;
  max_tokens?: number;
  max_cost_units?: number;
}

export interface PlanStepCreate {
  sequence: number;
  tool_id: string;
  tool_version: string;
  parameters?: Record<string, unknown>;
  dependencies?: number[];
  timeout_seconds?: number;
  expected_cost?: number;
}

export interface PlaybookCreate {
  name: string;
  action: "TAG" | "CLASSIFY" | "OWN" | "CERTIFY";
  datasource_id: string;
  match_field?: "TABLE_NAME" | "SCHEMA_NAME" | "QUALIFIED_NAME";
  match_pattern: string;
  column_name_pattern?: string | null;
  action_parameters: Record<string, unknown>;
  schedule_interval_minutes: number;
  auto_apply_max_items?: number;
  enabled?: boolean;
}

export interface PlaybookRead {
  id: string;
  organization_id: string;
  name: string;
  action: string;
  datasource_id: string;
  match_field: string;
  match_pattern: string;
  column_name_pattern: string | null;
  action_parameters: Record<string, unknown>;
  schedule_interval_minutes: number;
  auto_apply_max_items: number;
  enabled: boolean;
  created_by: string;
  last_run_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PlaybookRunResultRead {
  playbook_id: string;
  matched_count: number;
  outcome: string;
  bulk_action_run_id: string | null;
  bulk_stewardship_operation_id: string | null;
  governance_review_id: string | null;
}

export interface PlaybookUpdate {
  match_pattern?: string | null;
  column_name_pattern?: string | null;
  action_parameters?: Record<string, unknown> | null;
  schedule_interval_minutes?: number | null;
  auto_apply_max_items?: number | null;
  enabled?: boolean | null;
}

export interface PolicyNativeSyncRequestCreate {
  schema_name: string;
  table_name: string;
  reason: string;
}

export interface PolicyNativeSyncRequestRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  connector_type: string;
  schema_name: string;
  table_name: string;
  statements: Record<string, unknown>[];
  row_policy_count: number;
  column_policy_count: number;
  unsupported: string[];
  status: string;
  requested_by: string;
  request_reason: string;
  decided_by: string | null;
  decision_reason: string | null;
  decided_at: string | null;
  applied_at: string | null;
  apply_error: string | null;
  created_at: string;
}

export interface PortfolioAccessRead {
  requests_created: number;
  requests_pending: number;
  requests_approved: number;
  requests_rejected: number;
  requests_revoked: number;
  requests_expired: number;
  active_grants: number;
  grants_expiring_within_30_days: number;
  fulfillment_pending: number;
  fulfillment_provisioned: number;
  fulfillment_failed: number;
  fulfillment_revoked: number;
}

export interface PortfolioAnalyticsSummaryRead {
  generated_at: string;
  window_days: number;
  low_quality_threshold: number;
  lifecycle: PortfolioLifecycleRead;
  access: PortfolioAccessRead;
  usage: PortfolioUsageRead;
  quality: PortfolioQualityRead;
  queues: PortfolioQueueRead;
  top_products: PortfolioTopProductRead[];
}

export interface PortfolioAnalyticsTrendsRead {
  generated_at: string;
  window_days: number;
  bucket_days: number;
  points: PortfolioTrendPointRead[];
}

export interface PortfolioLifecycleRead {
  data_products_total: number;
  data_products_active: number;
  data_products_candidate: number;
  data_products_retired: number;
  data_product_versions_draft: number;
  data_product_versions_review_required: number;
  data_product_versions_published: number;
  data_product_versions_retired: number;
  data_contract_versions_draft: number;
  data_contract_versions_review_required: number;
  data_contract_versions_published: number;
  context_products_total: number;
  context_product_versions_draft: number;
  context_product_versions_review_required: number;
  context_product_versions_published: number;
  context_product_versions_deprecated: number;
}

export interface PortfolioQualityRead {
  published_products: number;
  scored_products: number;
  average_quality_score: number | null;
  low_quality_products: number;
  certified_products: number;
  uncertified_products: number;
  average_lineage_coverage: number | null;
}

export interface PortfolioQueueRead {
  review_required_data_product_versions: number;
  review_required_data_contract_versions: number;
  review_required_context_product_versions: number;
  pending_marketplace_access_requests: number;
}

export interface PortfolioTopProductRead {
  data_product_version_id: string;
  product_key: string;
  name: string;
  domain_name: string;
  certification_status: string;
  quality_score: number | null;
  lineage_coverage: number;
  access_request_count: number;
  approved_access_count: number;
  context_read_count: number;
}

export interface PortfolioTrendPointRead {
  bucket_start: string;
  bucket_end: string;
  access_requests: number;
  context_reads: number;
  mcp_operations: number;
  mcp_tool_calls: number;
  agent_runs: number;
  governed_tool_runs: number;
  model_gateway_runs: number;
  query_executions: number;
}

export interface PortfolioUsageRead {
  unique_context_consumers: number;
  unique_mcp_consumers: number;
  unique_agent_principals: number;
  context_product_reads: number;
  mcp_operations: number;
  mcp_resource_reads: number;
  mcp_prompt_reads: number;
  mcp_tool_calls: number;
  mcp_control_operations: number;
  agent_runs: number;
  governed_tool_agent_runs: number;
  model_gateway_agent_runs: number;
  development_override_agent_runs: number;
  policy_blocked_agent_runs: number;
  query_executions: number;
  governed_tool_executions: number;
}

export interface ProcedureLineageEdgeRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  source_table: string;
  source_column: string;
  target_table: string;
  target_column: string;
  source_table_id: string | null;
  source_column_id: string | null;
  target_table_id: string | null;
  target_column_id: string | null;
  transformation_type: string;
  confidence: string;
  dialect: string;
  sql_hash: string;
  created_at: string;
  updated_at: string;
}

export interface ProfilingExceptionDecisionRequest {
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

/** PR-2: request a policy-approved range/top-value profiling exception. */
export interface ProfilingExceptionPolicyCreate {
  classification: string;
  reason: string;
  retention_days: number;
}

export interface ProfilingExceptionPolicyRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  classification: string;
  status: string;
  retention_days: number;
  requested_by: string;
  request_reason: string;
  decided_by: string | null;
  decision_reason: string | null;
  decided_at: string | null;
  revoked_by: string | null;
  revoked_at: string | null;
  revocation_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProfilingExceptionRevokeRequest {
  reason: string;
}

export interface ProjectCreate {
  name: string;
  slug: string;
  data_domain_id?: string | null;
}

export interface ProjectRead {
  name: string;
  slug: string;
  data_domain_id: string;
  id: string;
  organization_id: string;
  line_of_business_id: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface QualityRulePackRead {
  name: string;
  enabled?: boolean;
  interval_minutes?: number;
  id: string;
  organization_id: string;
  datasource_id: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface QualityRulePackUpsert {
  name: string;
  enabled?: boolean;
  interval_minutes?: number;
}

export interface QualityRuleRead {
  name: string;
  rule_type: "TABLE_ROW_COUNT_MIN" | "TABLE_ROW_COUNT_MAX" | "COLUMN_NULL_RATE_MAX";
  table_id: string;
  column_id?: string | null;
  threshold: number;
  enabled?: boolean;
  id: string;
  organization_id: string;
  rule_pack_id: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface QualityRuleUpsert {
  name: string;
  rule_type: "TABLE_ROW_COUNT_MIN" | "TABLE_ROW_COUNT_MAX" | "COLUMN_NULL_RATE_MAX";
  table_id: string;
  column_id?: string | null;
  threshold: number;
  enabled?: boolean;
}

export interface QueryEstimateRead {
  plan_cost?: number | null;
  kind?: string | null;
  estimated_rows?: number | null;
  estimated_bytes?: number | null;
}

export interface QueryExecutionRequest {
  sql: string;
  max_rows?: number | null;
  semantic_version?: string | null;
  workspace_id?: string | null;
}

export interface QueryExecutionResponse {
  execution_id: string;
  status: string;
  normalized_sql: string;
  referenced_tables: string[];
  referenced_columns: string[];
  column_lineage: Record<string, unknown>[];
  plan_cost: number;
  warehouse_query_id: string | null;
  row_count: number;
  elapsed_ms: number;
  masked_columns: string[];
  rows: Record<string, unknown>[];
}

export interface QueryFeedbackRead {
  id: string;
  organization_id: string;
  agent_run_id: string;
  principal_id: string;
  rating: string;
  created_at: string;
  updated_at: string;
}

export interface QueryFeedbackUpsert {
  rating: "HELPFUL" | "NOT_HELPFUL" | "INCORRECT";
  comment?: string | null;
}

export interface QueryLineageRead {
  execution_id: string;
  datasource_id: string;
  status: string;
  referenced_tables: string[];
  referenced_columns: string[];
  column_lineage: Record<string, unknown>[];
  semantic_version: string | null;
  policy_version: string;
}

export interface RelationshipCandidateBulkDecisionItemRead {
  candidate_id: string;
  status: "SUCCEEDED" | "FAILED";
  reason?: string | null;
}

export interface RelationshipCandidateBulkDecisionRequest {
  candidate_ids?: string[] | null;
  filter?: RelationshipCandidateBulkSelectionFilter | null;
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

export interface RelationshipCandidateBulkDecisionResultRead {
  decision: "APPROVE" | "REJECT";
  selection_mode: "EXPLICIT" | "FILTER";
  requested_count: number;
  succeeded_count: number;
  failed_count: number;
  truncated: boolean;
  results: RelationshipCandidateBulkDecisionItemRead[];
}

export interface RelationshipCandidateBulkSelectionFilter {
  datasource_id: string;
  min_confidence?: number | null;
  max_confidence?: number | null;
  detection_rule?: string | null;
}

export interface RelationshipCandidateCalibrationBucketRead {
  confidence_low: number;
  confidence_high: number;
  decided_count: number;
  approved_count: number;
  rejected_count: number;
  observed_approval_rate: number | null;
}

export interface RelationshipCandidateCalibrationRead {
  datasource_id: string | null;
  bucket_width: number;
  total_decided: number;
  ground_truth_overrides_applied: number;
  buckets: RelationshipCandidateCalibrationBucketRead[];
  methodology_note: string;
}

export interface RelationshipCandidateDecision {
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

/** One field-level entry of a candidate's ``nothing -> this edge`` diff. */
export interface RelationshipCandidateDiffEntryRead {
  field: string;
  change: "added" | "removed" | "changed";
  after?: unknown;
}

export interface RelationshipCandidateDiscoveryRequest {
  max_candidates?: number;
}

export interface RelationshipCandidateImpactRead {
  impact_score: number;
  source_table_impact: number;
  target_table_impact: number;
  depth: number;
  node_limit: number;
  truncated: boolean;
}

export interface RelationshipCandidateRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  target_datasource_id: string;
  source_table_id: string;
  source_column_id: string;
  target_table_id: string;
  target_column_id: string;
  detection_rule: string;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
  created_by: string;
  reviewed_by: string | null;
  review_reason: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RelationshipCandidateReviewItemRead {
  candidate: RelationshipCandidateRead;
  diff: RelationshipCandidateDiffEntryRead[];
  impact: RelationshipCandidateImpactRead;
}

export interface RelationshipCandidateReviewQueueRead {
  datasource_id: string;
  items: RelationshipCandidateReviewItemRead[];
  limit: number;
  offset: number;
  scanned_count: number;
  total_pending_count: number;
  truncated: boolean;
}

export interface RenameCandidateDecision {
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

export interface RenameCandidateRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  analysis_run_id: string;
  schema_id: string;
  old_table_id: string;
  new_table_id: string;
  detection_rule: string;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
  created_by: string;
  reviewed_by: string | null;
  review_reason: string | null;
  reviewed_at: string | null;
  merged_at: string | null;
  created_at: string;
  updated_at: string;
}

/** One governance-review-queue proposal: its own review/decision fields */
export interface ReviewQueueProposalRead {
  review_id: string;
  organization_id: string;
  object_type: string;
  object_id: string;
  requested_action: string;
  status: string;
  requested_by: string;
  decided_by: string | null;
  decision_reason: string | null;
  decided_at: string | null;
  created_at: string;
  confidence?: number | null;
  evidence?: EvidenceItemRead[];
  diff: GovernanceReviewDiffRead;
}

/** A composed batch of review-queue proposals plus the filters that */
export interface ReviewQueueRead {
  organization_id: string;
  status_filter: string | null;
  object_type_filter: string | null;
  inference_run_id_filter: string | null;
  generated_at: string;
  proposals: ReviewQueueProposalRead[];
  total_proposals: number;
  by_status: Record<string, number>;
  by_object_type: Record<string, number>;
  diffable_count: number;
}

export interface ScanPolicyRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  enabled: boolean;
  interval_minutes: number;
  mode: string;
  priority: number;
  usage_boost_enabled: boolean;
  base_priority: number;
  computed_usage_boost: number;
  usage_boost_updated_at: string | null;
  maintenance_start_hour_utc: number | null;
  maintenance_end_hour_utc: number | null;
  next_run_at: string;
  last_triggered_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ScanPolicyUpsert {
  enabled?: boolean;
  interval_minutes: number;
  mode?: "FULL" | "INCREMENTAL";
  priority?: number;
  usage_boost_enabled?: boolean;
  maintenance_start_hour_utc?: number | null;
  maintenance_end_hour_utc?: number | null;
  start_at?: string | null;
}

/** Typeahead suggestion for command palette. */
export interface SearchSuggestion {
  text: string;
  object_type: string;
  object_id: string;
  display_name: string;
  qualified_name?: string | null;
  score: number;
}

/** One field-level difference, as returned to a reviewer. */
export interface SemanticFieldDeltaRead {
  field: string;
  change: "added" | "removed" | "changed";
  before?: unknown;
  after?: unknown;
}

export interface SemanticInferenceRequest {
  max_tables?: number;
  use_model?: boolean;
}

export interface SemanticInferenceRunRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  analysis_run_id: string | null;
  status: string;
  engine_mode: string;
  engine_version: string;
  model_route: string | null;
  table_count: number;
  proposal_count: number;
  model_enriched_count: number;
  rule_only_count: number;
  created_by: string;
  completed_at: string | null;
  error_summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface SemanticMetricCreate {
  slug: string;
  name: string;
  description: string;
  aggregation: "SUM" | "COUNT" | "AVG" | "MIN" | "MAX";
  grain: string;
  source_table_id: string;
  measure_column_id?: string | null;
  default_time_column_id?: string | null;
  allowed_dimension_column_ids?: string[];
}

export interface SemanticMetricVersionRead {
  id: string;
  semantic_model_version_id: string;
  metric_id: string;
  metric_slug: string;
  metric_name: string;
  version: number;
  status: string;
  description: string;
  aggregation: string;
  grain: string;
  source_table_id: string;
  measure_column_id: string | null;
  default_time_column_id: string | null;
  allowed_dimension_column_ids: string[];
  fingerprint: string;
  created_by: string;
  created_at: string;
}

export interface SemanticModelCloneRequest {
  name: string;
  change_summary: string;
}

export interface SemanticModelVersionCreate {
  name: string;
  change_summary: string;
  based_on_version_id?: string | null;
}

export interface SemanticModelVersionRead {
  id: string;
  organization_id: string;
  project_id: string;
  version: number;
  name: string;
  change_summary: string;
  status: string;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  published_at: string | null;
  based_on_version_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface SimulatedDecision {
  principal_kind: "HUMAN" | "AGENT" | "SERVICE";
  roles: string[];
  allowed: boolean;
  reason_code: string;
  matched_policy_code: string | null;
  masked_classifications: string[];
  row_filters: string[];
}

/** One hypothetical "what if this principal asked" case (PG-8). */
export interface SimulatedSubject {
  principal_kind?: "HUMAN" | "AGENT" | "SERVICE";
  roles?: string[];
  purpose?: string | null;
}

export interface SlaStatusResponse {
  contract_id: string;
  compliant: boolean;
  uptime_percent: number;
  violations_in_period: number;
  breach_minutes: number;
  period_start: string;
  period_end: string;
}

export interface SloBudgetRead {
  slo_id: string;
  slo_key: string;
  name: string;
  target: number;
  current_value: number | null;
  budget_remaining: number | null;
  window_days: number;
  status: string;
}

export interface SloDefinitionCreate {
  slo_key: string;
  name: string;
  target: number;
  window_days: number;
  threshold: number;
}

export interface SloDefinitionRead {
  id: string;
  organization_id: string;
  slo_key: string;
  name: string;
  target: number;
  window_days: number;
  threshold: number;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface SourceBindingCreate {
  datasource_id: string;
  purpose: string;
  schema_scope?: string[];
  permitted_classifications?: string[];
  masking_profile?: string;
  max_query_cost?: number | null;
}

export interface SourceBindingDecision {
  decision: "APPROVE" | "REJECT";
  valid_for_days?: number;
  rationale?: string;
}

export interface SourceBindingRead {
  id: string;
  organization_id: string;
  workspace_id: string;
  datasource_id: string;
  schema_scope: string[];
  permitted_classifications: string[];
  masking_profile: string;
  purpose: string;
  max_query_cost: number | null;
  status: string;
  requested_by: string;
  approved_by: string | null;
  approved_at: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourceEntitlementRead {
  workspace_id: string;
  datasource_id: string;
  datasource_name: string;
  line_of_business_code: string | null;
  line_of_business_name: string | null;
  schema_scope: string[];
  permitted_classifications: string[];
  masking_profile: string;
  purpose: string;
  expires_at: string | null;
}

export interface SqlFindingRead {
  code: string;
  severity: string;
  ref?: string | null;
  hint: string;
  detail?: Record<string, unknown>;
}

export interface SqlValidationRequest {
  sql: string;
  dialect?: string;
  max_rows?: number | null;
}

export interface SqlValidationResponse {
  valid: boolean;
  normalized_sql: string | null;
  referenced_tables: string[];
  referenced_columns: string[];
  violations: string[];
  applied_row_limit: number | null;
}

export interface StewardshipCoverageRead {
  organization_id: string;
  datasource_id: string | null;
  domain_id?: string | null;
  line_of_business_id?: string | null;
  table_count: number;
  overall_score: number;
  dimensions: Record<string, CoverageDimensionRead>;
  unowned_table_ids: string[];
  computed_at: string;
}

export interface StudioChangeItemCreate {
  object_type: "METRIC" | "TOOL" | "TERM" | "CONTEXT_PRODUCT";
  object_id: string;
  operation: "CREATE" | "UPDATE" | "DELETE";
  before_snapshot?: Record<string, unknown> | null;
  after_snapshot?: Record<string, unknown> | null;
}

export interface StudioChangeItemRead {
  id: string;
  organization_id: string;
  change_set_id: string;
  object_type: string;
  object_id: string;
  operation: string;
  before_snapshot: Record<string, unknown> | null;
  after_snapshot: Record<string, unknown> | null;
  diff: Record<string, unknown> | null;
  test_status: string;
  created_at: string;
  updated_at: string;
}

export interface StudioChangeSetCreate {
  name: string;
}

export interface StudioChangeSetRead {
  id: string;
  organization_id: string;
  name: string;
  author: string;
  status: string;
  base_version_hash: string;
  conflict_status: string;
  created_at: string;
  updated_at: string;
}

export interface StudioConflict {
  object_type: string;
  object_id: string;
  field_name: string;
  change_set_value: unknown;
  current_value: unknown;
}

export interface StudioDiffRead {
  change_set_id: string;
  items: Record<string, unknown>[];
}

export interface StudioEvalMiningResult {
  consumption_edges_scanned: number;
  bi_edges_scanned: number;
  questions_created: number;
  questions_already_mined: number;
  truncated: boolean;
}

export interface StudioEvalQuestionRead {
  id: string;
  organization_id: string;
  object_type: string;
  object_id: string;
  evidence_source: string;
  evidence_edge_id: string;
  label: string;
  mined_at: string;
  created_at: string;
  updated_at: string;
}

export interface StudioEvalResultRead {
  eval_question_id: string;
  object_type: string;
  object_id: string;
  label: string;
  passed: boolean;
  evidence: Record<string, unknown>;
}

export interface StudioEvalRunRead {
  id: string;
  change_set_id: string;
  started_at: string;
  completed_at: string | null;
  passed: boolean;
  evidence: Record<string, unknown>;
  results: StudioEvalResultRead[];
}

export interface StudioImpactPreview {
  change_set_id: string;
  affected_object_count: number;
  affected_objects: Record<string, unknown>[];
}

export interface StudioParameterContractValidateRequest {
  sql_template: string;
  dialect?: string;
  parameters?: Record<string, unknown>[];
}

export interface StudioParameterContractValidateResult {
  valid: boolean;
  errors: string[];
  definitions: Record<string, unknown>[];
  sample_rendered_sql?: string | null;
}

export interface StudioTestResultRead {
  id: string;
  change_set_id: string;
  started_at: string;
  completed_at: string | null;
  passed: boolean;
  evidence: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface TableFamilyCandidateDecision {
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

export interface TableFamilyCandidateRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  schema_id: string;
  family_type: "SNAPSHOT" | "HISTORY" | "DELTA" | "SCD";
  member_table_ids: string[];
  base_table_id: string | null;
  detection_rule: string;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
  created_by: string;
  reviewed_by: string | null;
  review_reason: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface TableFamilyDiscoveryRequest {
  max_candidates?: number;
}

export interface TableProfileRead {
  id: string;
  analysis_run_id: string;
  table_id: string;
  row_count_estimate: number | null;
  sampled_row_count: number;
  profile_version: string;
  status: string;
  created_at: string;
  columns: ColumnProfileRead[];
}

/** A resolved table reference; the return type of ``resolve_canonical``. */
export interface TableRef {
  table_id: string;
  qualified_name: string;
}

export interface TermSemanticBindingCreate {
  term_id: string;
  semantic_object_type?: "METRIC";
  semantic_object_id: string;
}

export interface TermSemanticBindingRead {
  id: string;
  organization_id: string;
  term_id: string;
  term_key: string;
  term_display_name: string;
  term_definition: string;
  semantic_object_type: string;
  semantic_object_id: string;
  semantic_object_name: string;
  status: string;
  requested_by: string;
  approved_by: string | null;
  approved_at: string | null;
  governance_review_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface TokenRevocationRead {
  token_identifier: string;
  subject: string;
  organization_id: string | null;
  self_revocation: boolean;
  revoked_at: string;
  token_expires_at: string;
}

export interface TokenRevocationRequest {
  token: string;
  reason: string;
}

export interface ToolCertificationCaseCreate {
  case_key: string;
  description: string;
  parameters?: Record<string, unknown>;
  expectation: ToolCertificationExpectation;
}

export interface ToolCertificationCaseRead {
  id: string;
  organization_id: string;
  tool_id: string;
  case_key: string;
  description: string;
  parameters: Record<string, unknown>;
  expectation: Record<string, unknown>;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ToolCertificationDecisionRequest {
  decision: "APPROVE" | "REJECT";
  reason?: string | null;
}

export interface ToolCertificationExpectation {
  expect: "ACCEPT" | "REJECT";
  sql_contains?: string[];
  error_contains?: string | null;
}

export interface ToolCertificationRunCreate {
  rationale: string;
  expires_at: string;
}

export interface ToolCertificationRunRead {
  id: string;
  organization_id: string;
  tool_id: string;
  tool_version_id: string;
  suite_version: string;
  corpus_fingerprint: string;
  status: string;
  total_cases: number;
  passed_cases: number;
  score: number;
  results: Record<string, unknown>[];
  rationale: string;
  executed_by: string;
  certified_by: string | null;
  decision_reason: string | null;
  issued_at: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ToolCertificationStatusRead {
  tool_id: string;
  tool_version_id: string | null;
  certified: boolean;
  run_id: string | null;
  certified_by: string | null;
  issued_at: string | null;
  expires_at: string | null;
  expired_run_id: string | null;
  expired_at: string | null;
}

/** A PUBLISHED context product that would be affected by this tool */
export interface ToolDeprecationDependentContextProductRead {
  context_product_version_id: string;
  product_id: string;
  product_key: string;
  version: number;
  name: string;
  reason: "ELIGIBLE_TOOL" | "SHARED_TABLE";
}

/** Another PUBLISHED governed tool that depends on a table the */
export interface ToolDeprecationDependentToolRead {
  tool_version_id: string;
  tool_id: string;
  slug: string;
  version: number;
  name: string;
  shared_table_count: number;
}

/** Blast radius of deprecating a governed tool version, computed fresh */
export interface ToolDeprecationImpactRead {
  tool_version_id: string;
  tool_id: string;
  slug: string;
  version: number;
  status: string;
  dependency_tables: string[];
  downstream_nodes: UnifiedLineageImpactNodeRead[];
  downstream_truncated: boolean;
  dependent_tool_versions: ToolDeprecationDependentToolRead[];
  dependent_context_products: ToolDeprecationDependentContextProductRead[];
  active_consumer_count: number;
  recent_execution_count: number;
  lookback_days: number;
  requested_depth: number;
  node_limit: number;
  total_blast_radius: number;
}

export interface ToolExecutionRequest {
  parameters?: Record<string, unknown>;
  max_rows?: number | null;
}

export interface ToolExecutionResponse {
  tool_execution_id: string;
  tool_version_id: string;
  tool_slug: string;
  tool_version: number;
  execution: QueryExecutionResponse;
  quality_gate?: Record<string, unknown> | null;
}

export interface ToolFirstRateRead {
  organization_id: string;
  window_days: number;
  tool_first_executions: number;
  freeform_executions: number;
  total_executions: number;
  rate: number | null;
  by_source: Record<string, number>;
  target_rate: number;
  meets_target: boolean | null;
  computed_at: string;
}

/** TL-6's `aida.tool_first_rate.ToolFirstRate`, embedded verbatim -- */
export interface ToolFirstRateSummaryRead {
  tool_first_executions: number;
  freeform_executions: number;
  total_executions: number;
  rate: number | null;
  by_source: Record<string, number>;
  target_rate: number;
  meets_target: boolean | null;
}

export interface ToolParameterDefinition {
  name: string;
  parameter_type: "STRING" | "INTEGER" | "NUMBER" | "BOOLEAN" | "DATE";
  required?: boolean;
  default?: unknown | null;
  allowed_values?: unknown[] | null;
  minimum?: number | null;
  maximum?: number | null;
  max_length?: number | null;
  sensitive?: boolean;
}

export interface ToolPlanCreate {
  name: string;
  steps: PlanStepCreate[];
  budget?: PlanBudgetCreate;
}

export interface ToolPlanDetailRead {
  id: string;
  organization_id: string;
  name: string;
  budget: Record<string, unknown>;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  steps: ToolPlanStepRead[];
}

export interface ToolPlanRead {
  id: string;
  organization_id: string;
  name: string;
  budget: Record<string, unknown>;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ToolPlanStepRead {
  id: string;
  plan_id: string;
  sequence: number;
  tool_id: string;
  tool_version: string;
  parameters: Record<string, unknown>;
  dependencies: number[];
  timeout_seconds: number;
  expected_cost: number;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  evidence: Record<string, unknown>;
  error_message: string | null;
}

/** One typed edge merged from declared FKs, approved/candidate column */
export interface UnifiedLineageEdgeRead {
  id: string;
  edge_source: "FOREIGN_KEY" | "SUGGESTED_RELATIONSHIP" | "DBT_DEPENDENCY" | "OPENLINEAGE_ETL" | "VIEW_DEFINITION" | "PROCEDURE_DEFINITION";
  source_node_id: string;
  target_node_id: string;
  source_label: string;
  target_label: string;
  status: string;
  confidence: number;
  source_columns?: string[];
  target_columns?: string[];
  evidence?: Record<string, unknown>;
}

export interface UnifiedLineageGraphRead {
  datasource_id: string;
  nodes: UnifiedLineageNodeRead[];
  edges: UnifiedLineageEdgeRead[];
  counts_by_source: Record<string, number>;
  returned_node_count?: number;
  returned_edge_count?: number;
  node_limit?: number;
  edge_limit?: number;
  truncated?: boolean;
  truncation_reasons?: string[];
}

export interface UnifiedLineageImpactNodeRead {
  node_id: string;
  node_kind: "TABLE" | "DBT_MODEL" | "DBT_SOURCE" | "DBT_SEED" | "DBT_SNAPSHOT" | "UNRESOLVED_DATASET";
  label: string;
  qualified_name: string;
  depth: number;
  contributing_edge_sources: ("FOREIGN_KEY" | "SUGGESTED_RELATIONSHIP" | "DBT_DEPENDENCY" | "OPENLINEAGE_ETL" | "VIEW_DEFINITION" | "PROCEDURE_DEFINITION")[];
}

/** Transitive upstream/downstream impact, replacing direct-reference */
export interface UnifiedLineageImpactRead {
  datasource_id: string;
  focus_node_id: string;
  focus_node_kind: "TABLE" | "DBT_MODEL" | "DBT_SOURCE" | "DBT_SEED" | "DBT_SNAPSHOT" | "UNRESOLVED_DATASET";
  focus_label: string;
  upstream: UnifiedLineageImpactNodeRead[];
  downstream: UnifiedLineageImpactNodeRead[];
  requested_depth: number;
  node_limit: number;
  upstream_truncated: boolean;
  downstream_truncated: boolean;
}

/** One node in the merged lineage graph: a catalog table, or -- when a dbt */
export interface UnifiedLineageNodeRead {
  id: string;
  node_kind: "TABLE" | "DBT_MODEL" | "DBT_SOURCE" | "DBT_SEED" | "DBT_SNAPSHOT" | "UNRESOLVED_DATASET";
  label: string;
  qualified_name: string;
  matched_table_id?: string | null;
  resolved?: boolean;
  depth?: number;
  inbound_edge_count?: number;
  outbound_edge_count?: number;
}

export interface UnownedAssetBacklogRouteRequest {
  datasource_id?: string | null;
  domain_id?: string | null;
  line_of_business_id?: string | null;
}

export interface UnownedAssetBacklogRouteResult {
  organization_id: string;
  routed: UnownedAssetEscalationRead[];
  escalated: UnownedAssetEscalationRead[];
  escalated_tier2: UnownedAssetEscalationRead[];
  resolved_count: number;
}

export interface UnownedAssetEscalationRead {
  id: string;
  organization_id: string;
  table_id: string;
  first_detected_unowned_at: string;
  status: string;
  candidate_owner: string | null;
  notification_rule_id: string | null;
  channel: string | null;
  recipients: string[];
  dedup_key: string | null;
  routed_at: string | null;
  escalated_at: string | null;
  escalated_tier2_at: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ValidationError {
  loc: (string | number)[];
  msg: string;
  type: string;
}

export interface ValidationIssueRead {
  step_sequence: number;
  issue: string;
  severity: string;
}

export interface ValidationResponse {
  valid: boolean;
  issues: ValidationIssueRead[];
}

export interface ViewLineageEdgeRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  source_table: string;
  source_column: string;
  target_table: string;
  target_column: string;
  source_table_id: string | null;
  source_column_id: string | null;
  target_table_id: string | null;
  target_column_id: string | null;
  transformation_type: string;
  confidence: string;
  dialect: string;
  sql_hash: string;
  created_at: string;
  updated_at: string;
}

export interface ViewLineageParseRequest {
  sql: string;
  dialect?: string;
}

export interface ViewLineageParseResponse {
  edges: LineageEdgeRead[];
  confidence: string;
  dialect: string;
  sql_hash: string;
  errors?: string[];
  persisted_edge_count?: number;
}

/** N11: request a deterministically-rendered single-view tool draft */
export interface ViewToolBlueprintRequest {
  slug: string;
  name: string;
  description: string;
  datasource_id: string;
  semantic_model_version_id?: string | null;
  table_id: string;
  allowed_roles: string[];
}

export interface WorkspaceCreate {
  name: string;
  slug: string;
  purpose?: string;
  isolation_boundary_id?: string | null;
  monthly_cost_ceiling?: number | null;
}

export interface WorkspaceEntitlementRead {
  workspace_id: string;
  workspace_name: string;
  workspace_slug: string;
  role: string;
  granted_by: string;
  expires_at: string | null;
}

export interface WorkspaceMembershipCreate {
  principal_id: string;
  principal_kind?: "HUMAN" | "AGENT" | "SERVICE";
  role: "viewer" | "analyst" | "steward" | "reviewer" | "workspace_owner";
  expires_at?: string | null;
}

export interface WorkspaceMembershipRead {
  id: string;
  organization_id: string;
  workspace_id: string;
  principal_id: string;
  principal_kind: string;
  role: string;
  granted_by: string;
  expires_at: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceRead {
  id: string;
  organization_id: string;
  isolation_boundary_id: string | null;
  name: string;
  slug: string;
  purpose: string;
  status: string;
  monthly_cost_ceiling: number | null;
  created_at: string;
  updated_at: string;
}
