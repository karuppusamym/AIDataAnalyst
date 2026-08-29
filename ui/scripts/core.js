/* Shared state and DOM rendering utilities for the static UI. */
(function initializeAtlasUiCore() {
  const state = {
    organizations: [], organizationId: null, lobs: [], projects: [], sources: [],
    fleet: null, runs: [], reviews: [], audit: [], runtime: null, evaluations: [],
    agentRuns: [], tables: [], semanticModels: [], semanticMetrics: [], selectedSemantic: null,
    tools: [], selectedTool: null, graph: null, relationships: [], modelRoutes: [],
    memory: [], outbox: [], pendingDecision: null, metricTables: [], metricColumns: [],
    dbtProjects: [], dbtImports: [], dbtResources: [], dbtLineage: null,
    selectedDbtProjectId: null, selectedDbtImportId: null,
    dbtDagMode: "dag", dbtDagZoom: 1.0, dbtDagSearch: "",
    dbtDagExpandedNodes: new Set(), dbtDagSelectedNodeId: null, dbtGraphEngine: null,
    openlineageEvents: [], integrationPolicy: null,
    semanticInferenceRuns: [], enrichmentProposals: [], businessAnnotations: [],
    businessMap: null, graphFocusHistory: [], graphSelectedNodeId: null,
    graphZoom: 1, graphDatasourceId: null, graphSearchResults: [],
    qualitySummary: null, qualityPolicies: [], qualityObservations: [], qualityIncidents: [],
    pendingQualityIncidentId: null,
    connectorMatrix: [], connectorCertifications: [], metadataIngestions: [],
    metadataBatches: [], metadataBatchChunks: [], selectedBatchId: null,
    persona: "all", paletteEntries: [], paletteActiveIndex: -1,
    selectedTableId: null, selectedAssetTab: "overview", glossaryTerms: [],
    glossaryCategories: [], glossaryLinkProposals: [], glossaryConflicts: [],
    ownershipRules: [], ownershipAssignments: [], bulkStewardshipOperations: [],
    stewardshipCoverage: null, selectedAssetDocumentation: null, selectedAssetLinks: [],
    selectedAssetOwnership: []
  };

  const roles = "PlatformAdmin,OrganizationAdmin,ProjectAdmin,MetadataAdmin,MetadataIngestor,DataAdmin,SemanticAdmin,DataSteward,ToolDeveloper,ToolConsumer,AgentDeveloper,Reviewer,MetadataReviewer,Auditor,Operations,Analyst,Viewer";
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const setHtml = (id, html) => { const node = document.getElementById(id); if (node) node.innerHTML = html; };
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, character => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;"})[character]);
  const when = value => value ? new Intl.DateTimeFormat(undefined, {dateStyle:"medium", timeStyle:"short"}).format(new Date(value)) : "Not recorded";
  const human = value => String(value ?? "Unknown").replaceAll("_", " ").toLowerCase().replace(/\b\w/g, character => character.toUpperCase());
  const statusClass = value => ["FAILED", "SUBMISSION_FAILED", "REJECTED", "DEAD_LETTER", "DISABLED", "SUPPRESSED", "UNPARSEABLE", "CRITICAL", "STALE"].includes(value) ? "bad" : ["PENDING", "PLANNED", "CONDITIONAL", "QUEUED", "RUNNING", "PROCESSING", "PROFILING", "REVIEW_REQUIRED", "NOT_CONFIGURED", "APPROVED_NOT_SELECTED", "GENERATION_DISABLED", "ADAPTER_REGISTRATION_REQUIRED", "UNMATCHED", "WARNING", "NO_BASELINE", "OPEN"].includes(value) ? "warn" : ["ACTIVE", "CERTIFIED", "PASS", "IMPLEMENTED", "COMPLETED", "PUBLISHED", "APPROVED", "UP", "ELIGIBLE", "READY", "IMPORTED", "PARSED", "MATCHED", "HEALTHY", "CURRENT", "RESOLVED", "ACKNOWLEDGED"].includes(value) ? "" : "neutral";
  const badge = value => `<span class="status ${statusClass(value)}">${esc(human(value))}</span>`;
  const empty = (title, detail="") => `<div class="empty-state"><strong>${esc(title)}</strong>${detail ? `<span>${esc(detail)}</span>` : ""}</div>`;
  const table = (heads, rows, emptyText="No records found") => rows.length ? `<table class="data-table"><thead><tr>${heads.map(head => `<th>${head}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>` : empty(emptyText);
  const selectOptions = (items, label, blank="") => `${blank ? `<option value="">${esc(blank)}</option>` : ""}${items.map(item => `<option value="${item.id}">${esc(label(item))}</option>`).join("")}`;
  const asNumberOrNull = value => String(value ?? "").trim() === "" ? null : Number(value);
  const preserveSelect = (id, html) => {
    const node = document.getElementById(id);
    if (!node) return;
    const previous = node.value;
    node.innerHTML = html;
    if ([...node.options].some(option => option.value === previous)) node.value = previous;
  };
  const populateProjectSources = (id, projectId) => {
    const items = state.sources.filter(item => item.project_id === projectId);
    preserveSelect(id, selectOptions(items, item => item.name, items.length ? "" : "No sources in project"));
  };

  window.AtlasUI = { state, roles, $, $$, setHtml, esc, when, human, statusClass, badge, empty, table, selectOptions, asNumberOrNull, preserveSelect, populateProjectSources };
})();
