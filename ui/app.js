const { state, $, $$, setHtml, esc, when, human, badge, empty, table, selectOptions, asNumberOrNull, preserveSelect, populateProjectSources, api, fetchAll, renderTable, integrationFlags, dbtEnabled, transformationMetadataSurfaceEnabled, renderTransformationOverview, renderDbtDisabledState, renderIntegrationPolicy, applyIntegrationPolicyVisibility, loadIntegrationPolicy, renderDbtProjects, renderOpenLineageHistory, renderDbtImports, renderDbtArtifact, loadDbtArtifact, selectDbtProject, loadDbtProjects, loadOpenLineage, showDbtResource } = window.AtlasUI;

let knowledgeGraphEngine = null;

function notify(message, success=false) {
  const region = document.getElementById("alert-region");
  if (region) { region.setAttribute("role", success ? "status" : "alert"); region.setAttribute("aria-live", success ? "polite" : "assertive"); }
  setHtml("alert-region", `<div class="alert ${success ? "success" : ""}">${esc(message)}</div>`);
  window.setTimeout(() => setHtml("alert-region", ""), 5500);
}

async function loadOrganizations(preferredId=null) {
  state.organizations = await fetchAll("/v1/organizations");
  const remembered = preferredId || localStorage.getItem("aida-organization");
  state.organizationId = state.organizations.some(item => item.id === remembered) ? remembered : state.organizations.at(-1)?.id || null;
  const html = selectOptions(state.organizations, item => item.name, state.organizations.length ? "" : "No organizations");
  ["organization-select", "lob-organization"].forEach(id => preserveSelect(id, html));
  if (state.organizationId) {
    $("#organization-select").value = state.organizationId;
    $("#lob-organization").value = state.organizationId;
  }
}

async function loadHierarchy() {
  const [lobs, projects, sources] = await Promise.all([
    fetchAll(`/v1/organizations/${state.organizationId}/lines-of-business`),
    fetchAll(`/v1/organizations/${state.organizationId}/projects`),
    fetchAll(`/v1/organizations/${state.organizationId}/datasources`)
  ]);
  const lobMap = new Map(lobs.map(item => [item.id, item]));
  const projectMap = new Map(projects.map(item => [item.id, item]));
  state.lobs = lobs;
  state.projects = projects.map(item => ({...item, lobName: lobMap.get(item.line_of_business_id)?.name || "Unknown LOB"}));
  state.sources = sources.map(item => ({...item, projectName: projectMap.get(item.project_id)?.name || "Unknown project", lobName: lobMap.get(item.line_of_business_id)?.name || "Unknown LOB"}));
  populateSelectors();
  renderHierarchy();
}

function populateSelectors() {
  const sourceHtml = selectOptions(state.sources, item => `${item.name} / ${item.projectName}`, state.sources.length ? "" : "No sources");
  ["analyst-source","catalog-source","meaning-source","relationship-source","schedule-source","memory-source","quality-source","certification-source","ingestion-source","batch-source"].forEach(id => preserveSelect(id, sourceHtml));
  preserveSelect("run-source-filter", `<option value="ALL">All sources</option>${selectOptions(state.sources, item => item.name)}`);
  const projectHtml = selectOptions(state.projects, item => `${item.name} / ${item.lobName}`, state.projects.length ? "" : "No projects");
  ["semantic-project","tools-project","transform-project","datasource-project"].forEach(id => preserveSelect(id, projectHtml));
  preserveSelect("project-lob", selectOptions(state.lobs, item => `${item.name} (${item.code})`, state.lobs.length ? "" : "No lines of business"));
  populateProjectSources("tool-author-source", $("#tools-project")?.value);
  populateProjectSources("metric-source", $("#semantic-project")?.value);
  populateProjectSources("dbt-source", $("#transform-project")?.value);
  populateProjectSources("openlineage-source", $("#transform-project")?.value);
}

async function loadOrganizationData() {
  if (!state.organizationId) {
    state.integrationPolicy = null;
    renderIntegrationPolicy();
    applyIntegrationPolicyVisibility();
    renderHierarchy();
    showView("administration");
    return;
  }
  ["sources-table","runs-table","governance-table","audit-table","recent-runs","evaluation-table","model-routes-table"].forEach(id => setHtml(id, '<div class="loading">Loading governed records</div>'));
  await loadHierarchy();
  const [fleet, runs, reviews, runtime, evaluations] = await Promise.all([
    api(`/v1/organizations/${state.organizationId}/fleet-summary`),
    fetchAll(`/v1/organizations/${state.organizationId}/analysis-runs`),
    fetchAll("/v1/governance/reviews?status=PENDING"),
    api("/v1/ai/runtime-status"),
    fetchAll(`/v1/organizations/${state.organizationId}/agent-evaluations`)
  ]);
  Object.assign(state, {fleet, runs, reviews, runtime, evaluations});
  await loadIntegrationPolicy();
  await loadAudit();
  renderCore();
  await Promise.all([loadAgentRuns(), loadTables(), loadDbtProjects(), loadOpenLineage(), loadBusinessMeaning(), loadGlossary(), loadSemanticModels(), loadTools(), loadRelationships(), loadSchedule(), loadModelRoutes(), loadQuality(), loadEnterpriseIngestion()]);
}

function renderCore() {
  const completed = state.fleet.analysis_run_statuses.COMPLETED || 0;
  const activeRuns = ["QUEUED","RUNNING","PROFILING"].reduce((sum, key) => sum + (state.fleet.analysis_run_statuses[key] || 0), 0);
  const activeSources = state.fleet.datasource_statuses.ACTIVE || 0;
  const metrics = [
    ["Active sources", activeSources, `${state.sources.length} registered across ${state.lobs.length} LOBs`],
    ["Metadata runs", completed, `${activeRuns} currently active`],
    ["Pending decisions", state.reviews.length, "Independent checker queue"],
    ["Delivery exceptions", state.fleet.dead_letter_outbox_events, state.fleet.dead_letter_outbox_events ? "Operator action required" : "No dead letters"]
  ];
  setHtml("metric-grid", metrics.map(([label,value,detail]) => `<div class="metric"><p>${label}</p><strong>${value}</strong><small>${detail}</small></div>`).join(""));
  setHtml("source-metrics", [
    ["Registered",state.sources.length,"Organization fleet"], ["Active",activeSources,"Admitted for work"],
    ["Scheduled",state.fleet.scan_policies_enabled,"Durable policies"], ["Due now",state.fleet.scan_policies_due,"Scheduler backlog"]
  ].map(([a,b,c]) => `<div class="metric"><p>${a}</p><strong>${b}</strong><small>${c}</small></div>`).join(""));
  const alerts = [];
  if (state.reviews.length) alerts.push([`${state.reviews.length} governance decisions waiting`, "Open review center", "governance"]);
  if (state.fleet.dead_letter_outbox_events) alerts.push([`${state.fleet.dead_letter_outbox_events} event deliveries need recovery`, "Open operations", "operations"]);
  if (!state.runtime.enterprise_security_ready) alerts.push(["Enterprise security activation is incomplete", "Review AI and identity posture", "agents"]);
  setHtml("home-alerts", alerts.map(([title,action,view]) => `<div class="attention"><div><strong>${esc(title)}</strong><span>${esc(action)}</span></div><button class="button small" data-go="${view}">Open</button></div>`).join(""));
  renderEstate(); renderRuns("recent-runs", 5); renderRuns("runs-table", 500); renderSources(); renderGovernance(); renderAudit(); renderRuntime(); renderEvaluations();
  $("#review-nav-count").textContent = state.reviews.length;
  $("#outbox-tab-count").textContent = state.fleet.dead_letter_outbox_events;
}

function renderEstate() {
  const rows = state.lobs.map(lob => {
    const projects = state.projects.filter(item => item.line_of_business_id === lob.id);
    const sources = state.sources.filter(item => item.line_of_business_id === lob.id);
    const active = sources.filter(item => item.status === "ACTIVE").length;
    const percent = sources.length ? Math.round(active / sources.length * 100) : 0;
    return `<div class="estate-row"><div><strong>${esc(lob.name)}</strong><small>${esc(lob.code)}</small></div><div><strong>${projects.length} projects</strong><small>${sources.length} sources</small></div><div><div class="progress"><span style="width:${percent}%"></span></div><small>${active} active / ${sources.length} registered</small></div></div>`;
  }).join("");
  setHtml("estate-summary", rows || empty("No lines of business", "Use Platform setup to create the first ownership boundary."));
  const reviewRows = state.reviews.slice(0, 6).map(review => `<div class="estate-row"><div><strong>${esc(human(review.object_type))}</strong><small>${esc(review.requested_action)}</small></div><div><strong>${esc(review.requested_by)}</strong><small>${when(review.created_at)}</small></div><button class="row-action" data-review="${review.id}">Review</button></div>`).join("");
  setHtml("review-summary", reviewRows || empty("Queue is clear", "No governed changes are waiting for a checker."));
}

function filteredRuns() {
  const status = $("#run-status-filter")?.value || "ALL";
  const source = $("#run-source-filter")?.value || "ALL";
  return state.runs.filter(run => (status === "ALL" || run.status === status) && (source === "ALL" || run.datasource_id === source));
}

function renderRuns(target, limit) {
  const items = target === "runs-table" ? filteredRuns() : state.runs;
  const rows = items.slice(0, limit).map(run => {
    const source = state.sources.find(item => item.id === run.datasource_id);
    const action = ["QUEUED","RUNNING","PROFILING","CANCELLATION_REQUESTED"].includes(run.status) ? `<button class="row-action danger" data-cancel-run="${run.id}">Cancel</button>` : ["FAILED","CANCELLED","SUBMISSION_FAILED"].includes(run.status) ? `<button class="row-action" data-resume-run="${run.id}">Resume</button>` : "";
    return `<tr><td><button class="link-button" data-run-detail="${run.id}">${esc(source?.name || run.trigger_type)}</button><span class="secondary-cell">${esc(run.id)}</span></td><td>${badge(run.status)}</td><td>${esc(human(run.mode))}</td><td>${run.discovered_tables} tables / ${run.discovered_columns} columns</td><td>${run.created_objects} / ${run.changed_objects} / ${run.deprecated_objects}</td><td>${when(run.created_at)}</td><td>${action}</td></tr>`;
  });
  renderTable(target, ["Source / run","Status","Mode","Inventory","Created / changed / retired","Started","Action"], rows, "No analysis runs match this view");
}

function renderSources() {
  const rows = state.sources.map(source => `<tr><td><span class="primary-cell">${esc(source.name)}</span><span class="secondary-cell">${esc(source.projectName)} / ${esc(source.lobName)}</span></td><td>${badge(source.status)}</td><td>${esc(source.connector_type)} / ${esc(source.dialect)}</td><td>${esc(source.environment)}</td><td>${esc(source.network_zone)}</td><td>${source.max_concurrency}</td><td><button class="row-action" data-test-source="${source.id}">Test</button><button class="row-action" data-scan="${source.id}">Scan now</button><button class="row-action ${source.status === "DISABLED" ? "" : "danger"}" data-toggle="${source.id}" data-enabled="${source.status === "DISABLED"}">${source.status === "DISABLED" ? "Enable" : "Disable"}</button></td></tr>`);
  renderTable("sources-table", ["Source","Status","Connector","Environment","Network zone","Concurrency","Actions"], rows, "No sources are registered");
}

async function loadEnterpriseIngestion() {
  const certificationSourceId = $("#certification-source")?.value;
  const ingestionSourceId = $("#ingestion-source")?.value;
  const batchSourceId = $("#batch-source")?.value;
  const [matrix, certifications, ingestions, batches] = await Promise.all([
    api("/v1/connectors/capability-matrix"),
    certificationSourceId ? fetchAll(`/v1/datasources/${certificationSourceId}/connector-certifications`) : Promise.resolve([]),
    ingestionSourceId ? fetchAll(`/v1/datasources/${ingestionSourceId}/metadata-ingestions`) : Promise.resolve([]),
    batchSourceId ? fetchAll(`/v1/datasources/${batchSourceId}/metadata-ingestion-batches`) : Promise.resolve([])
  ]);
  state.selectedBatchId = batches.some(item => item.id === state.selectedBatchId) ? state.selectedBatchId : batches[0]?.id || null;
  const chunks = state.selectedBatchId ? await fetchAll(`/v1/metadata-ingestion-batches/${state.selectedBatchId}/chunks`) : [];
  Object.assign(state, {connectorMatrix:matrix, connectorCertifications:certifications, metadataIngestions:ingestions, metadataBatches:batches, metadataBatchChunks:chunks});
  renderEnterpriseIngestion();
}

function renderEnterpriseIngestion() {
  const implemented = state.connectorMatrix.filter(item => item.implementation_status === "IMPLEMENTED").length;
  setHtml("connector-matrix-status", badge(implemented ? "IMPLEMENTED" : "PLANNED"));
  const matrixRows = state.connectorMatrix.map(item => {
    const capabilityCount = Object.values(item.capabilities).filter(Boolean).length;
    return `<tr><td><span class="primary-cell">${esc(item.display_name)}</span><span class="secondary-cell">${esc(item.connector_type)} / ${esc(item.dialect)}</span></td><td>${badge(item.implementation_status)}</td><td>${badge(item.maturity)}</td><td>${esc(item.transports.join(" + "))}</td><td>${capabilityCount || "Contract only"}</td></tr>`;
  });
  renderTable("connector-matrix", ["Connector","Delivery","Maturity","Transports","Capabilities"], matrixRows, "No connector definitions are registered");

  const latest = state.connectorCertifications[0];
  if (!latest) {
    setHtml("certification-summary", empty("No certification evidence", "Run the deterministic source conformance suite after connection and inventory discovery."));
  } else {
    const checkRows = latest.checks.map(check => `<div class="cert-check"><span>${badge(check.status)}</span><strong>${esc(human(check.name))}</strong><small>${esc(check.evidence)}</small></div>`).join("");
    setHtml("certification-summary", `<div class="cert-score"><div><span>Latest result</span><strong>${latest.score}</strong><small>out of 100</small></div><div>${badge(latest.status)}<p>${esc(latest.suite_version)} / ${when(latest.completed_at)}</p></div></div><div class="cert-checks">${checkRows}</div>`);
  }
  const certificationRows = state.connectorCertifications.map(item => `<tr><td><button class="link-button" data-certification-detail="${item.id}">${esc(item.suite_version)}</button><span class="secondary-cell">${esc(item.connector_type)} ${esc(item.connector_version)}</span></td><td>${badge(item.status)}</td><td>${item.score}</td><td>${esc(item.initiated_by)}</td><td>${when(item.completed_at)}</td></tr>`);
  renderTable("certification-history", ["Suite","Status","Score","Operator","Completed"], certificationRows, "No prior certification runs");

  const ingestionRows = state.metadataIngestions.map(item => `<tr><td><button class="link-button" data-ingestion-detail="${item.id}">${esc(item.producer)}</button><span class="secondary-cell">${esc(item.idempotency_key)}</span></td><td>${badge(item.status)}</td><td>${esc(human(item.transport))} / ${esc(human(item.snapshot_type))}</td><td>${item.object_counts.tables || 0} / ${item.object_counts.columns || 0}</td><td>${item.change_counts.created_objects || 0} / ${item.change_counts.changed_objects || 0} / ${item.change_counts.deprecated_objects || 0}</td><td>${when(item.completed_at || item.created_at)}</td></tr>`);
  renderTable("ingestion-history", ["Producer / key","Status","Delivery","Tables / columns","Created / changed / retired","Completed"], ingestionRows, "No canonical metadata deliveries for this source");

  const openBatches = state.metadataBatches.filter(item => ["DRAFT","FAILED","SUBMISSION_FAILED"].includes(item.status));
  preserveSelect("batch-select", selectOptions(openBatches, item => `${item.batch_key} · ${item.received_chunks}/${item.expected_chunks} chunks`, openBatches.length ? "" : "No open batches"));
  if (state.selectedBatchId && openBatches.some(item => item.id === state.selectedBatchId)) $("#batch-select").value = state.selectedBatchId;
  const selected = state.metadataBatches.find(item => item.id === state.selectedBatchId);
  if (!selected) setHtml("batch-progress", empty("No batch selected", "Create a manifest or select a batch from the history."));
  else {
    const percent = Math.round((selected.processed_chunks / Math.max(1, selected.expected_chunks)) * 100);
    setHtml("batch-progress", `<div class="boundary-callout"><span class="boundary-icon">${percent}%</span><div><strong>${esc(selected.batch_key)} · ${esc(human(selected.status))}</strong><p>${selected.received_chunks}/${selected.expected_chunks} received · ${selected.processed_chunks}/${selected.expected_chunks} processed · ${selected.object_counts.tables || 0} tables · ${selected.object_counts.columns || 0} columns</p></div></div>`);
  }
  const batchRows = state.metadataBatches.map(item => `<tr><td><button class="link-button" data-batch-select-row="${item.id}">${esc(item.batch_key)}</button><span class="secondary-cell">${esc(item.producer)}</span></td><td>${badge(item.status)}</td><td>${esc(human(item.snapshot_type))}</td><td>${item.received_chunks} / ${item.expected_chunks}</td><td>${item.processed_chunks} / ${item.expected_chunks}</td><td>${item.change_counts.created_objects || 0} / ${item.change_counts.changed_objects || 0} / ${item.change_counts.deprecated_objects || 0}</td><td>${when(item.completed_at || item.created_at)}</td></tr>`);
  renderTable("batch-history", ["Batch / producer","Status","Snapshot","Received","Processed","Created / changed / retired","Updated"], batchRows, "No durable ingestion batches for this source");
  const chunkRows = state.metadataBatchChunks.map(item => `<tr><td>${item.chunk_number}</td><td><button class="link-button" data-chunk-detail="${item.id}">${esc(item.chunk_key)}</button></td><td>${badge(item.status)}</td><td>${item.object_counts.tables || 0} / ${item.object_counts.columns || 0}</td><td>${esc(item.payload_fingerprint.slice(0, 12))}…</td><td>${when(item.processed_at || item.created_at)}</td></tr>`);
  renderTable("batch-chunks", ["Chunk","Idempotency key","Status","Tables / columns","Checksum","Updated"], chunkRows, "The selected batch has no uploaded chunks");
}

function renderGovernance() {
  const type = $("#review-type-filter")?.value || "ALL";
  const maker = $("#review-maker-filter")?.value.trim().toLowerCase() || "";
  const visible = state.reviews.filter(review => (type === "ALL" || review.object_type === type) && (!maker || review.requested_by.toLowerCase().includes(maker)));
  const byType = state.reviews.reduce((acc, item) => (acc[item.object_type] = (acc[item.object_type] || 0) + 1, acc), {});
  const stewardshipCount = (byType.BULK_STEWARDSHIP_OPERATION || 0) + (byType.GLOSSARY_CONFLICT || 0) + (byType.GLOSSARY_LINK_PROPOSAL || 0);
  const metrics = [["Pending total",state.reviews.length,"Independent decisions"],["Business meaning",byType.METADATA_ENRICHMENT_PROPOSAL || 0,"Metadata approval"],["Stewardship",stewardshipCount,"Ownership, conflicts, and links"],["Semantic",byType.SEMANTIC_MODEL_VERSION || 0,"Publication"],["Tools",byType.GOVERNED_TOOL_VERSION || 0,"Publish / deprecate"],["Model routes",byType.MODEL_ROUTE_CONFIGURATION || 0,"Approval only"]];
  setHtml("governance-metrics", metrics.map(([a,b,c]) => `<div class="metric"><p>${a}</p><strong>${b}</strong><small>${c}</small></div>`).join(""));
  const rows = visible.map(review => `<tr><td><span class="primary-cell">${esc(human(review.object_type))}</span><span class="secondary-cell">${esc(review.object_id)}</span></td><td>${esc(human(review.requested_action))}</td><td>${badge(review.status)}</td><td>${esc(review.requested_by)}</td><td>${when(review.created_at)}</td><td><button class="row-action" data-review="${review.id}">Review decision</button></td></tr>`);
  renderTable("governance-table", ["Governed object","Requested action","Status","Maker","Requested","Checker"], rows, "No pending reviews match these filters");
}

async function loadBusinessMeaning() {
  const sourceId = $("#meaning-source")?.value;
  if (!sourceId) {
    state.semanticInferenceRuns = [];
    state.enrichmentProposals = [];
    state.businessAnnotations = [];
    state.businessMap = null;
    renderBusinessMeaning();
    return;
  }
  const [runs, proposals, annotations, businessMap] = await Promise.all([
    fetchAll(`/v1/datasources/${sourceId}/semantic-inference-runs`),
    fetchAll(`/v1/datasources/${sourceId}/metadata-enrichment-proposals`),
    fetchAll(`/v1/datasources/${sourceId}/business-annotations`),
    api(`/v1/organizations/${state.organizationId}/business-map`)
  ]);
  state.semanticInferenceRuns = runs;
  state.enrichmentProposals = proposals;
  state.businessAnnotations = annotations;
  state.businessMap = businessMap;
  renderBusinessMeaning();
}

function renderBusinessMeaning() {
  const latestRun = state.semanticInferenceRuns[0];
  const pending = state.enrichmentProposals.filter(item => item.status === "PENDING_REVIEW").length;
  const promoted = state.enrichmentProposals.filter(item => item.promoted_tool_version_id).length;
  const domainCount = state.businessMap?.domain_count || 0;
  setHtml("meaning-metrics", [
    ["Tables assessed", latestRun?.table_count || 0, latestRun ? `Latest ${human(latestRun.engine_mode)}` : "Run inference after metadata scan"],
    ["Awaiting review", pending, "Independent steward decisions"],
    ["Approved meaning", state.businessAnnotations.length, `${domainCount} governed domains`],
    ["Tool blueprints promoted", promoted, "Draft only; publication remains reviewed"]
  ].map(([label,value,detail]) => `<div class="metric"><p>${label}</p><strong>${value}</strong><small>${esc(detail)}</small></div>`).join(""));
  setHtml("meaning-engine", latestRun ? badge(latestRun.engine_mode) : badge("NOT_RUN"));

  const proposalRows = state.enrichmentProposals.map(item => {
    const proposal = item.payload || {};
    const action = item.status === "PENDING_REVIEW"
      ? `<button class="row-action" data-review="${item.governance_review_id}">Review</button>`
      : item.status === "APPROVED" && proposal.tool_blueprint?.recommended && !item.promoted_tool_version_id
        ? `<button class="row-action" data-promote-blueprint="${item.id}">Create tool draft</button>`
        : item.promoted_tool_version_id ? badge("TOOL_DRAFT_CREATED") : "";
    return `<tr><td><button class="link-button" data-proposal-detail="${item.id}">${esc(proposal.business_name || item.table_name)}</button><span class="secondary-cell">${esc(item.schema_name)}.${esc(item.table_name)}</span></td><td><span class="primary-cell">${esc(proposal.domain_name || "Unassigned")}</span><span class="secondary-cell">${esc(proposal.entity_name || "Unassigned")}</span></td><td>${esc(human(proposal.table_role))}<span class="secondary-cell">${Math.round((item.confidence || 0) * 100)}% confidence / ${esc(human(item.engine_type))}</span></td><td>${badge(item.status)}</td><td>${action}</td></tr>`;
  });
  renderTable("meaning-proposals", ["Business object / table","Domain / entity","Role / evidence","Status","Action"], proposalRows, "No inference proposals yet");

  const annotationRows = state.businessAnnotations.map(item => `<tr><td><button class="link-button" data-annotation-detail="${item.id}">${esc(item.business_name)}</button><span class="secondary-cell">${esc(item.schema_name)}.${esc(item.table_name)} / v${item.version}</span></td><td><span class="primary-cell">${esc(item.domain_name)}</span><span class="secondary-cell">${esc(item.entity_name)}</span></td><td>${esc(human(item.table_role))}<span class="secondary-cell">${esc(item.grain_statement)}</span></td><td>${when(item.approved_at)}<span class="secondary-cell">${esc(item.approved_by)}</span></td></tr>`);
  renderTable("business-annotations", ["Business object","Domain / entity","Role / grain","Approved"], annotationRows, "No business annotations have been approved");

  const map = state.businessMap;
  const domainNodes = map?.nodes?.filter(node => node.node_type === "DOMAIN") || [];
  const entityNodes = map?.nodes?.filter(node => node.node_type === "ENTITY") || [];
  const tableNodes = map?.nodes?.filter(node => node.node_type === "TABLE") || [];
  setHtml("business-map-status", map ? badge(map.truncated ? "TRUNCATED" : "AUTHORITATIVE") : badge("EMPTY"));
  setHtml("business-map", domainNodes.map(domain => {
    const entities = entityNodes.filter(entity => entity.parent_id === domain.id);
    return `<div class="business-domain-card"><div><span class="domain-mark">${esc(domain.label.slice(0, 2).toUpperCase())}</span><h3>${esc(domain.label)}</h3><small>${entities.length} entities</small></div>${entities.map(entity => { const tables = tableNodes.filter(node => node.parent_id === entity.id); return `<section><strong>${esc(entity.label)}</strong><span>${tables.map(node => esc(node.label)).join(" · ") || "No linked tables"}</span></section>`; }).join("")}</div>`;
  }).join("") || empty("No approved business map", "Approve inference proposals to establish governed domains and entities."));
  const crossEdges = map?.edges?.filter(edge => edge.edge_type === "CROSS_DOMAIN_FOREIGN_KEY") || [];
  const labelById = new Map((map?.nodes || []).map(node => [node.id, node.label]));
  setHtml("cross-domain-edges", crossEdges.length ? `<div class="cross-domain-list"><h3>Cross-domain relationships</h3>${crossEdges.map(edge => `<div><span>${esc(labelById.get(edge.source_node_id))}</span><b>&rarr;</b><span>${esc(labelById.get(edge.target_node_id))}</span><small>${esc((edge.evidence.source_columns || []).join(", "))} → ${esc((edge.evidence.target_columns || []).join(", "))}</small></div>`).join("")}</div>` : `<p class="form-note">Cross-domain edges appear when approved table annotations are connected by authoritative foreign keys.</p>`);
}

async function loadGlossary() {
  if (!state.organizationId) return;
  const [terms,categories,coverage,proposals,conflicts,rules,operations,assignments] = await Promise.all([
    fetchAll(`/v1/organizations/${state.organizationId}/glossary-terms`),
    fetchAll(`/v1/organizations/${state.organizationId}/glossary-categories`),
    api(`/v1/organizations/${state.organizationId}/stewardship/coverage`),
    fetchAll(`/v1/organizations/${state.organizationId}/glossary-link-proposals`),
    fetchAll(`/v1/organizations/${state.organizationId}/glossary-conflicts`),
    fetchAll(`/v1/organizations/${state.organizationId}/ownership-rules`),
    fetchAll(`/v1/organizations/${state.organizationId}/stewardship/bulk-operations`),
    fetchAll(`/v1/organizations/${state.organizationId}/ownership-assignments`)
  ]);
  Object.assign(state, {glossaryTerms:terms, glossaryCategories:categories, stewardshipCoverage:coverage, glossaryLinkProposals:proposals, glossaryConflicts:conflicts, ownershipRules:rules, bulkStewardshipOperations:operations, ownershipAssignments:assignments});
  renderGlossary();
  renderStewardship();
}

function renderGlossary() {
  const categoryNames = new Map(state.glossaryCategories.map(item => [item.id, item.display_name]));
  const cards = state.glossaryTerms.map(term => `<article class="glossary-card ${term.lifecycle_status === "DEPRECATED" ? "muted-card" : ""}"><div class="glossary-card-head"><span class="term-mark">${esc(term.display_name.slice(0, 2).toUpperCase())}</span><div><strong>${esc(term.display_name)}</strong><small>${esc(categoryNames.get(term.category_id) || "Uncategorized")} / ${esc(term.term_key)} / v${term.version}</small></div>${badge(term.lifecycle_status === "DEPRECATED" ? "DEPRECATED" : term.status)}</div><p>${esc(term.definition)}</p><div class="chip-list compact">${term.synonyms.map(value => `<span>${esc(value)}</span>`).join("") || "<span>No synonyms</span>"}</div><div class="glossary-card-foot"><span>Owner: ${esc(term.owner_principal || "Unassigned")}</span><div class="record-actions">${term.status === "DRAFT" ? `<button class="button small" data-submit-term="${term.id}">Submit for review</button>` : ""}${term.status === "APPROVED" && term.lifecycle_status === "ACTIVE" ? `<button class="button secondary small" data-deprecate-term="${term.term_id}">Deprecate</button>` : ""}</div></div></article>`).join("");
  setHtml("glossary-terms", cards || empty("No glossary terms", "Create a governed definition to establish reusable business language."));
  const categoryOptions = selectOptions(state.glossaryCategories, item => item.display_name, "Uncategorized");
  preserveSelect("glossary-term-category", categoryOptions);
  preserveSelect("glossary-category-parent", selectOptions(state.glossaryCategories, item => item.display_name, "Top level"));
  const approved = state.glossaryTerms.filter(term => term.status === "APPROVED" && term.lifecycle_status === "ACTIVE").map(term => ({...term, id:term.term_id}));
  preserveSelect("bulk-glossary-term", selectOptions(approved, item => `${item.display_name} (${item.term_key})`, "Not applicable"));
}

function renderStewardship() {
  const coverage = state.stewardshipCoverage;
  const dimensionLabels = {documented:"Documented", owned:"Owned", classified:"Classified", certified:"Certified", quality_monitored:"Quality monitored", semantically_mapped:"Semantically mapped"};
  const coverageHtml = coverage ? `<div class="coverage-score"><span>Estate trust coverage</span><strong>${Number(coverage.overall_score).toFixed(0)}%</strong><small>${coverage.table_count} active assets / ${coverage.unowned_table_ids.length} shown unowned</small></div><div class="coverage-dimensions">${Object.entries(coverage.dimensions).map(([key,value]) => `<div><span><b style="width:${value.percentage}%"></b></span><strong>${esc(dimensionLabels[key] || human(key))}</strong><small>${value.covered}/${value.total} / ${Number(value.percentage).toFixed(0)}%</small></div>`).join("")}</div>` : empty("Coverage unavailable");
  setHtml("stewardship-coverage", coverageHtml);
  const proposalRows = state.glossaryLinkProposals.map(item => `<tr><td><span class="primary-cell">${esc(item.term_display_name)}</span><span class="secondary-cell">${esc(item.table_name)}</span></td><td>${Math.round(item.confidence * 100)}%<span class="secondary-cell">${esc(human(item.evidence.strategy))}</span></td><td>${badge(item.status)}</td><td>${item.status === "DRAFT" ? `<button class="row-action" data-submit-link-proposal="${item.id}">Submit</button>` : item.status === "REVIEW_REQUIRED" ? `<button class="row-action" data-review="${item.governance_review_id}">Review</button>` : ""}</td></tr>`);
  renderTable("glossary-link-proposals", ["Term / asset","Evidence","Status","Action"], proposalRows, "No inferred links awaiting action");
  const conflictRows = state.glossaryConflicts.map(item => `<tr><td><span class="primary-cell">${esc(human(item.conflict_type))}</span><span class="secondary-cell">${esc(item.position_a.display_name || "Position A")} vs ${esc(item.position_b.display_name || "Position B")}</span></td><td>${badge(item.status)}</td><td>${esc(item.assigned_owner || "Unassigned")}</td><td>${item.status === "OPEN" ? `<button class="row-action" data-resolve-conflict="${item.id}">Resolve</button>` : ""}</td></tr>`);
  renderTable("glossary-conflicts", ["Conflict","Status","Steward","Action"], conflictRows, "No unresolved glossary conflicts");
  const ruleRows = state.ownershipRules.map(item => `<tr><td><span class="primary-cell">${esc(item.display_name)}</span><span class="secondary-cell">${esc(item.rule_key)}</span></td><td>${esc(human(item.match_field))}<span class="secondary-cell">${esc(item.match_pattern)}</span></td><td>${esc(item.owner_principal)}<span class="secondary-cell">${esc(human(item.owner_type))}</span></td><td><button class="row-action" data-apply-ownership-rule="${item.id}">Apply</button></td></tr>`);
  renderTable("ownership-rules", ["Rule","Match","Owner","Action"], ruleRows, "No ownership routing rules");
  const operationRows = state.bulkStewardshipOperations.slice(0, 100).map(item => `<tr><td><span class="primary-cell">${esc(human(item.operation_type))}</span><span class="secondary-cell">${item.subject_ids.length} ${esc(human(item.subject_type))} subjects</span></td><td>${badge(item.status)}</td><td>${item.applied_count}</td><td>${item.status === "REVIEW_REQUIRED" ? `<button class="row-action" data-review="${item.governance_review_id}">Review</button>` : when(item.applied_at)}</td></tr>`);
  renderTable("bulk-stewardship-operations", ["Operation","Status","Applied","Decision"], operationRows, "No bulk stewardship activity");
}

async function loadAudit() {
  if (!state.organizationId) return;
  const params = new URLSearchParams();
  [["action", $("#audit-action")?.value], ["resource_type", $("#audit-resource")?.value], ["correlation_id", $("#audit-correlation")?.value]].forEach(([key,value]) => { if (value?.trim()) params.set(key, value.trim()); });
  const suffix = params.toString() ? `?${params}` : "";
  state.audit = await fetchAll(`/v1/organizations/${state.organizationId}/audit-events${suffix}`);
  renderAudit();
}

function renderAudit() {
  const rows = state.audit.map(event => `<tr><td><button class="link-button" data-audit-detail="${event.id}">${esc(human(event.action))}</button><span class="secondary-cell">${esc(event.correlation_id)}</span></td><td>${esc(event.principal_id)}<span class="secondary-cell">${esc(event.principal_type)}</span></td><td>${esc(human(event.resource_type))}<span class="secondary-cell">${esc(event.resource_id || "Not recorded")}</span></td><td>${badge(event.outcome)}</td><td>${when(event.occurred_at)}</td></tr>`);
  renderTable("audit-table", ["Action / correlation","Principal","Resource","Outcome","Occurred"], rows, "No audit events match these filters");
}

function renderRuntime() {
  const runtime = state.runtime;
  $("#analyst-route-badge").innerHTML = badge(runtime.model_route_status);
  setHtml("runtime-controls", runtime.deterministic_controls.slice(0, 8).map((control,index) => `<div><span class="control-icon">${String(index + 1).padStart(2,"0")}</span><p><strong>${esc(human(control))}</strong><small>Enforced outside model output</small></p><b>ENFORCED</b></div>`).join(""));
  const values = [["Orchestration",runtime.orchestration_mode,runtime.runtime],["Model route",runtime.model_route_status,`${runtime.available_model_providers.join(" / ")} adapters; generation enabled: ${runtime.model_generation_enabled}`],["Identity",runtime.identity_provider,human(runtime.identity_verification)],["Secrets",runtime.credential_provider,runtime.credential_provider_available ? "Adapter available" : "Adapter registration required"]];
  setHtml("ai-runtime", values.map(([a,b,c]) => `<div class="metric"><p>${a}</p><strong class="metric-text ${["NOT_CONFIGURED","development","env"].includes(b) ? "warn-text" : ""}">${esc(human(b))}</strong><small>${esc(c)}</small></div>`).join(""));
}

function renderEvaluations() {
  const rows = state.evaluations.map(item => `<tr><td><button class="link-button" data-evaluation="${item.id}">${esc(item.suite_version)}</button><span class="secondary-cell">${esc(item.id)}</span></td><td>${badge(item.status)}</td><td>${item.passed_count} / ${item.scenario_count}</td><td>${(item.pass_rate * 100).toFixed(0)}%</td><td>${when(item.created_at)}</td></tr>`);
  renderTable("evaluation-table", ["Suite / run","Status","Passed","Rate","Executed"], rows, "No evaluation evidence is available");
}

function renderHierarchy() {
  const selected = state.organizations.find(item => item.id === state.organizationId);
  setHtml("setup-org-summary", selected?.name || "Not selected");
  const rows = state.lobs.map(lob => {
    const projectCount = state.projects.filter(item => item.line_of_business_id === lob.id).length;
    const sourceCount = state.sources.filter(item => item.line_of_business_id === lob.id).length;
    return `<div><strong>${esc(lob.name)} (${esc(lob.code)})</strong><small>${projectCount} projects / ${sourceCount} sources</small></div>`;
  }).join("");
  setHtml("hierarchy-summary", `<div class="hierarchy-tree">${rows || `<div><strong>No hierarchy yet</strong><small>Create a line of business to continue.</small></div>`}</div>`);
}

async function loadAgentRuns() {
  const sourceId = $("#analyst-source")?.value;
  state.agentRuns = sourceId ? await fetchAll(`/v1/datasources/${sourceId}/agent-runs`) : [];
  renderAgentRuns();
}

function renderAgentRuns() {
  const rows = state.agentRuns.map(run => { const risk = run.plan_evidence.prompt_risk || {}; return `<tr><td><span class="primary-cell">${esc(human(run.generation_source))}</span><span class="secondary-cell">${esc(run.id)}</span></td><td>${badge(run.status)}</td><td>${esc(human(run.plan_evidence.strategy || "Unknown"))}<span class="secondary-cell">${Math.round((run.plan_evidence.confidence || 0) * 100)}% confidence · prompt ${esc(human(risk.decision || "not screened"))}</span></td><td>${run.retrieval_evidence.length}</td><td>${run.step_trace.length}</td><td>${when(run.created_at)}</td><td><button class="row-action" data-trace="${run.id}">Evidence</button>${run.status === "COMPLETED" ? `<button class="row-action" data-feedback="${run.id}" data-rating="HELPFUL">Helpful</button><button class="row-action" data-feedback="${run.id}" data-rating="INCORRECT">Incorrect</button>` : ""}</td></tr>`; });
  renderTable("agent-runs-table", ["Generation / run","Status","Plan","Retrieved","Steps","Started","Actions"], rows, "No analyst runs for this source");
}

function renderTrace(trace=[]) {
  setHtml("agent-trace", trace.length ? trace.map(step => `<div class="trace-step ${step.control_type === "DETERMINISTIC" ? "" : "hybrid"}"><span>${String(step.sequence).padStart(2,"0")}</span><strong>${esc(human(step.stage))}</strong><small>${esc(human(step.control_type))}</small>${step.details ? `<em>${esc(Object.entries(step.details).map(([key,value]) => `${human(key)}: ${Array.isArray(value) ? value.join(", ") : value ?? "Not recorded"}`).join(" / "))}</em>` : ""}</div>`).join("") : empty("No execution selected"));
}

function renderPlan(plan, hits, target="retrieval-preview") {
  const evidence = hits.slice(0, 8).map(hit => `<div><strong>${esc(hit.display_name)}</strong><span>${esc(human(hit.object_type))} / ${Math.round(hit.score * 100)}%</span></div>`).join("");
  const risk = plan.prompt_risk || {}; const riskReasons = (risk.reason_codes || []).map(human).join(" / ");
  const riskEvidence = risk.decision ? `<div class="prompt-risk ${risk.decision === "BLOCK" ? "blocked" : ""}"><div><span>Prompt safety</span>${badge(risk.decision)}</div><strong>${Math.round((risk.score || 0) * 100)}% risk · ${esc(risk.classifier_version || "not versioned")}</strong><small>${esc(riskReasons)}</small></div>` : "";
  setHtml(target, `<div class="plan-preview"><div class="plan-head">${badge(plan.strategy)}<strong>${Math.round(plan.confidence * 100)}% confidence</strong></div>${riskEvidence}<p>${esc((plan.reason_codes || []).map(human).join(" / "))}</p>${plan.required_parameters?.length ? `<p class="warn-text">Required parameters: ${esc(plan.required_parameters.join(", "))}</p>` : ""}<div class="compact-list">${evidence || `<div><span>${plan.strategy === "BLOCKED" ? "Retrieval was not started because the prompt policy denied the request." : "No governed metadata matched."}</span></div>`}</div></div>`);
}

async function previewPlan() {
  const source = $("#analyst-source").value;
  const question = $("#analyst-question").value.trim();
  if (!source || !question) return notify("Choose a source and enter a business question.");
  setHtml("retrieval-preview", '<div class="loading">Retrieving governed evidence</div>');
  try {
    const result = await api(`/v1/datasources/${source}/agent-retrieval-preview`, {method:"POST", body:JSON.stringify({question, candidate_sql_available:Boolean($("#analyst-sql").value.trim())})});
    renderPlan(result.plan_evidence, result.retrieval_evidence);
  } catch (error) { setHtml("retrieval-preview", ""); notify(error.message); }
}

async function runAnalysis() {
  const source = $("#analyst-source").value;
  const question = $("#analyst-question").value.trim();
  const candidateSql = $("#analyst-sql").value.trim();
  if (!source || !question) return notify("Choose a source and enter a business question.");
  const button = $("#run-analysis"); button.disabled = true; button.textContent = "Running controls";
  setHtml("analysis-result", '<div class="loading">Planning, validating, costing, executing, and masking</div>');
  try {
    const result = await api(`/v1/datasources/${source}/agent-analyses`, {method:"POST", body:JSON.stringify({question, candidate_sql:candidateSql || null, tool_parameters:{}, max_rows:Number($("#analyst-limit").value)})});
    $("#analysis-status").innerHTML = badge(result.status);
    renderQueryResult(result.execution, result.explanation);
    renderPlan(result.plan_evidence, result.retrieval_evidence);
    renderTrace(result.step_trace);
    notify(`Governed ${human(result.plan_evidence.strategy)} analysis completed.`, true);
  } catch (error) {
    setHtml("analysis-result", `<div class="route-block"><strong>Execution stopped safely</strong><p>${esc(error.message)}</p></div>`);
    $("#analysis-status").innerHTML = badge("REJECTED");
  } finally { button.disabled = false; button.textContent = "Run governed analysis"; await loadAgentRuns(); }
}

async function validateSql() {
  const sql = $("#analyst-sql").value.trim();
  if (!sql) return notify("Enter candidate SQL to validate.");
  const sourceId = $("#analyst-source").value;
  const dialect = state.sources.find(item => item.id === sourceId)?.dialect || "postgres";
  try {
    const result = await api("/v1/query/validate", {method:"POST", body:JSON.stringify({sql, dialect, max_rows:Number($("#analyst-limit").value)})});
    setHtml("retrieval-preview", `<div class="plan-preview"><div class="plan-head">${badge(result.valid ? "VALID" : "REJECTED")}<strong>${result.applied_row_limit || 0} row ceiling</strong></div><p>${esc(result.violations.length ? result.violations.join(" / ") : "AST, statement type, and deterministic limits passed.")}</p><div class="evidence-strip"><span>${result.referenced_tables.length} tables</span><span>${result.referenced_columns.length} columns</span></div></div>`);
  } catch (error) { notify(error.message); }
}

function renderLineage(lineage=[]) {
  if (!lineage.length) return "";
  const cards = lineage.map((item, idx) => {
    const isDirect = item.lineage_type === "DIRECT";
    const sources = (item.source_columns || []).map(s => `
      <span class="lineage-src-pill">
        <span class="lineage-src-tbl">${esc(s.table || "source")}</span>
        <span class="lineage-src-col">${esc(s.column)}</span>
      </span>
    `).join("") || '<span class="lineage-src-pill"><span class="lineage-src-col">Computed Expression</span></span>';
    
    const transforms = (item.transformations || []).map(t => `<span class="lineage-transform-tag">${esc(t)}</span>`).join("");
    
    return `
      <div class="lineage-dag-card ${isDirect ? "direct" : "derived"}">
        <div class="lineage-card-sources">${sources}</div>
        <div class="lineage-card-flow">
          <svg class="lineage-arrow-icon" viewBox="0 0 24 24" width="16" height="16">
            <path d="M5 12h14M12 5l7 7-7 7" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          ${transforms ? `<div class="lineage-transforms-strip">${transforms}</div>` : ""}
        </div>
        <div class="lineage-card-target">
          <span class="lineage-target-col">${esc(item.output_column)}</span>
          <span class="lineage-type-badge ${isDirect ? "direct" : "derived"}">${esc(item.lineage_type || "DERIVED")}</span>
        </div>
      </div>
    `;
  }).join("");

  return `
    <div class="lineage-panel">
      <div class="lineage-panel-head">
        <h4>Governed Column-Level Lineage DAG</h4>
        <span class="lineage-stat-badge">${lineage.length} Projected Columns</span>
      </div>
      <div class="lineage-dag-canvas">
        ${cards}
      </div>
    </div>
  `;
}

function renderQueryResult(execution, explanation, target="analysis-result") {
  const rows = execution.rows || [];
  const headers = rows[0] ? Object.keys(rows[0]) : [];
  const body = rows.map(row => `<tr>${headers.map(header => `<td>${esc(row[header])}</td>`).join("")}</tr>`);
  const rowsTarget = `${target}-rows`;
  setHtml(target, `<p class="answer-text">${esc(explanation || "Governed execution completed.")}</p><div class="evidence-strip"><span>${execution.row_count} rows</span><span>${execution.elapsed_ms} ms</span><span>Cost ${execution.plan_cost}</span><span>${execution.masked_columns.length} masked</span><span>${(execution.referenced_columns || []).length} referenced columns</span></div><div class="result-scroll"><div id="${rowsTarget}"></div></div>${renderLineage(execution.column_lineage)}<span class="secondary-cell">Execution ${esc(execution.execution_id)} / ${esc(execution.normalized_sql)}</span>`);
  renderTable(rowsTarget, headers, body, "Query returned no rows");
}

async function loadTables() {
  const sourceId = $("#catalog-source")?.value;
  state.tables = sourceId ? await fetchAll(`/v1/datasources/${sourceId}/tables`) : [];
  if (!state.tables.some(item => item.id === state.selectedTableId)) state.selectedTableId = null;
  populateCatalogFilters();
  renderTables();
}

function populateCatalogFilters() {
  const typeFilter = $("#catalog-type-filter");
  const statusFilter = $("#catalog-status-filter");
  if (!typeFilter || !statusFilter) return;
  const selectedType = typeFilter.value || "ALL";
  const selectedStatus = statusFilter.value || "ALL";
  const types = [...new Set(state.tables.map(item => item.object_type).filter(Boolean))].sort();
  const statuses = [...new Set(state.tables.map(item => item.status).filter(Boolean))].sort();
  typeFilter.innerHTML = `<option value="ALL">All types</option>${types.map(value => `<option value="${esc(value)}">${esc(human(value))}</option>`).join("")}`;
  statusFilter.innerHTML = `<option value="ALL">All statuses</option>${statuses.map(value => `<option value="${esc(value)}">${esc(human(value))}</option>`).join("")}`;
  typeFilter.value = types.includes(selectedType) ? selectedType : "ALL";
  statusFilter.value = statuses.includes(selectedStatus) ? selectedStatus : "ALL";
}

function renderTables() {
  const query = $("#catalog-search")?.value.trim().toLowerCase() || "";
  const type = $("#catalog-type-filter")?.value || "ALL";
  const status = $("#catalog-status-filter")?.value || "ALL";
  const visible = state.tables.filter(item => {
    const searchMatch = !query || `${item.name} ${item.object_type} ${item.status} ${item.source_description || ""}`.toLowerCase().includes(query);
    return searchMatch && (type === "ALL" || item.object_type === type) && (status === "ALL" || item.status === status);
  });
  const cards = visible.map(item => `<button class="asset-card ${state.selectedTableId === item.id ? "active" : ""}" data-table="${item.id}" type="button"><span class="asset-kind">${esc(String(item.object_type || "TB").slice(0, 2).toUpperCase())}</span><span class="asset-card-copy"><strong>${esc(item.name)}</strong><small>${esc(human(item.object_type))} / ${esc(item.source_description || "Technical metadata asset")}</small><span class="asset-card-meta"><i>${esc(human(item.status))}</i><i>${esc(String(item.id).slice(0, 8))}</i></span></span><span class="asset-chevron">&rsaquo;</span></button>`).join("");
  setHtml("tables-table", cards || empty("No assets match", "Adjust the search or clear one of the filters."));
  setHtml("catalog-count", `<strong>${visible.length}</strong><span> of ${state.tables.length} assets</span>`);
}

async function showTable(id) {
  state.selectedTableId = id;
  renderTables();
  setHtml("table-detail", '<div class="loading">Loading table evidence</div>');
  try {
    const [columns,constraints,impact,profile,annotation,documentation,termLinks,ownership] = await Promise.all([fetchAll(`/v1/tables/${id}/columns`), fetchAll(`/v1/tables/${id}/constraints`), api(`/v1/metadata/tables/${id}/impact`), api(`/v1/tables/${id}/profile`).catch(() => null), api(`/v1/metadata/tables/${id}/business-annotation`).catch(error => error.status === 404 ? null : Promise.reject(error)), api(`/v1/metadata/tables/${id}/documentation`).catch(error => error.status === 404 ? null : Promise.reject(error)), fetchAll(`/v1/metadata/tables/${id}/glossary-links`), fetchAll(`/v1/organizations/${state.organizationId}/ownership-assignments?subject_type=TABLE&subject_id=${id}`)]);
    state.selectedAssetDocumentation = documentation;
    state.selectedAssetLinks = termLinks;
    state.selectedAssetOwnership = ownership;
    const item = state.tables.find(tableItem => tableItem.id === id);
    const sensitive = columns.filter(column => ["PII","PCI","PHI","SECRET","CONFIDENTIAL"].includes(column.classification)).length;
    const columnRows = columns.map(column => `<div class="column-row"><span class="column-symbol">${esc(String(column.physical_type || "C").slice(0, 2).toUpperCase())}</span><span><strong>${esc(column.name)}</strong><small>${esc(column.physical_type)}${column.nullable ? " / nullable" : " / required"}</small></span>${badge(column.classification || "UNCLASSIFIED")}</div>`).join("");
    const constraintRows = constraints.map(constraint => `<div class="relationship-row"><span class="relationship-icon">FK</span><span><strong>${esc(constraint.name)}</strong><small>${esc(human(constraint.constraint_type))} / ${esc(constraint.columns.join(", "))}</small></span></div>`).join("");
    const businessOverview = annotation ? `<div class="business-summary"><div class="section-heading"><div><span class="section-icon coral">BM</span><div><strong>${esc(annotation.business_name)}</strong><small>${esc(annotation.domain_name)} / ${esc(annotation.entity_name)}</small></div></div>${badge("APPROVED")}</div><p>${esc(annotation.business_description)}</p><div class="evidence-strip"><span>${esc(human(annotation.table_role))}</span><span>Grain: ${esc(annotation.grain_statement)}</span><span>Version ${annotation.version}</span></div></div>` : `<div class="ai-suggestion"><span class="section-icon coral">AI</span><div><strong>Add business context</strong><p>No approved meaning exists yet. Run metadata inference to propose a name, domain, grain, synonyms, questions, and safe tool blueprints.</p><button class="button small" data-go="meaning">Open business meaning</button></div></div>`;
    const questionRows = (annotation?.suggested_questions || []).map(question => `<div class="question-row"><span>?</span><p>${esc(question)}</p></div>`).join("");
    const documentationSection = documentation ? `<section><div class="section-heading"><div><span class="section-icon blue">RD</span><div><strong>Asset documentation</strong><small>Version ${documentation.version} / owner ${esc(documentation.owner_principal || "Unassigned")}</small></div></div>${badge(documentation.status)}</div><p class="asset-readme">${esc(documentation.readme)}</p><div class="chip-list">${documentation.aliases.map(alias => `<span>${esc(alias)}</span>`).join("") || "<span>No aliases</span>"}</div><div class="record-actions spaced"><button class="button small" data-edit-documentation="${id}">New version</button>${documentation.status === "DRAFT" ? `<button class="button primary small" data-submit-documentation="${documentation.id}">Submit for review</button>` : ""}</div></section>` : `<div class="ai-suggestion"><span class="section-icon blue">RD</span><div><strong>Document this asset</strong><p>Add business-friendly aliases, ownership, and durable usage guidance.</p><button class="button small" data-edit-documentation="${id}">Create documentation</button></div></div>`;
    const termsSection = `<section><div class="section-heading"><div><span class="section-icon sand">GL</span><div><strong>Linked glossary terms</strong><small>Approved reusable business language</small></div></div><button class="button small" data-link-term="${id}">Link term</button></div><div class="linked-terms">${termLinks.map(link => `<div><span><strong>${esc(link.display_name)}</strong><small>${esc(link.definition)}</small></span><button class="icon-button" data-remove-term="${link.id}" title="Remove term" type="button">&#215;</button></div>`).join("") || empty("No linked terms")}</div></section>`;
    const ownershipSection = `<section><div class="section-heading"><div><span class="section-icon mint">OW</span><div><strong>Accountability and trust</strong><small>Reviewed individual/group ownership and time-bound certification</small></div></div><div class="record-actions"><button class="button small" data-assign-asset-owner="${id}">Assign owner</button><button class="button secondary small" data-certify-asset="${id}">Certify</button></div></div><div class="linked-terms">${ownership.map(item => `<div><span><strong>${esc(item.owner_principal)}</strong><small>${esc(human(item.owner_type))} / ${esc(human(item.assignment_kind))}</small></span>${badge(item.status)}</div>`).join("") || empty("No accountable owner")}</div></section>`;
    const qualityForTable = state.qualityIncidents.filter(record => record.table_id === id);
    const tab = state.selectedAssetTab || "overview";
    setHtml("table-detail", `<div class="asset-detail-head"><div class="asset-title"><span class="asset-kind large">${esc(String(item?.object_type || "TB").slice(0, 2).toUpperCase())}</span><div><p class="asset-path">${esc(human(item?.object_type))} / governed source</p><h2>${esc(item?.name)}</h2></div></div><div class="asset-head-actions">${badge(item?.status)}<button class="icon-button" type="button" title="Asset identifier">&#9432;</button></div></div><div class="asset-statbar"><span><strong>${columns.length}</strong> columns</span><span><strong>${sensitive}</strong> sensitive</span><span><strong>${constraints.length}</strong> constraints</span><span><strong>${profile ? profile.sampled_row_count : 0}</strong> profiled rows</span><span><strong>${impact.downstream_object_count}</strong> downstream</span></div><div class="asset-tabs" role="tablist" aria-label="Asset detail sections">${[["overview","Overview"],["columns",`Columns ${columns.length}`],["lineage","Lineage"],["intelligence","Intelligence"],["quality","Data quality"]].map(([key,label]) => `<button class="${tab === key ? "active" : ""}" id="asset-tab-${key}" role="tab" aria-selected="${tab === key}" aria-controls="asset-pane-${key}" tabindex="${tab === key ? 0 : -1}" data-asset-tab="${key}" type="button">${label}</button>`).join("")}</div><div class="asset-tab-panel ${tab === "overview" ? "active" : ""}" role="tabpanel" id="asset-pane-overview" aria-labelledby="asset-tab-overview" data-asset-pane="overview"><section><div class="section-heading"><div><span class="section-icon blue">OV</span><div><strong>Description</strong><small>Technical and approved business context</small></div></div></div><p>${esc(item?.source_description || annotation?.business_description || "No source description has been provided for this asset.")}</p></section>${businessOverview}<section><div class="section-heading"><div><span class="section-icon sand">RS</span><div><strong>Relationships</strong><small>Declared structural evidence</small></div></div></div>${constraintRows || empty("No declared relationships")}</section></div><div class="asset-tab-panel ${tab === "columns" ? "active" : ""}" role="tabpanel" id="asset-pane-columns" aria-labelledby="asset-tab-columns" data-asset-pane="columns"><div class="column-list-head"><strong>Column name</strong><span>Classification</span></div>${columnRows || empty("No columns discovered")}</div><div class="asset-tab-panel ${tab === "lineage" ? "active" : ""}" role="tabpanel" id="asset-pane-lineage" aria-labelledby="asset-tab-lineage" data-asset-pane="lineage"><section><div class="section-heading"><div><span class="section-icon mint">LN</span><div><strong>Downstream impact</strong><small>Governed dependencies connected to this asset</small></div></div></div><div class="impact-grid"><div><strong>${impact.semantic_metric_version_ids.length}</strong><span>Semantic metrics</span></div><div><strong>${impact.governed_tool_version_ids.length}</strong><span>Governed tools</span></div><div><strong>${impact.dbt_resource_ids.length}</strong><span>dbt resources</span></div><div><strong>${impact.approved_relationship_candidate_ids.length}</strong><span>Approved links</span></div></div><button class="button secondary small" data-go="relationships">Explore knowledge graph</button></section></div><div class="asset-tab-panel ${tab === "intelligence" ? "active" : ""}" role="tabpanel" id="asset-pane-intelligence" aria-labelledby="asset-tab-intelligence" data-asset-pane="intelligence">${annotation ? `<section><div class="section-heading"><div><span class="section-icon coral">IN</span><div><strong>Business intelligence</strong><small>Approved semantic context</small></div></div>${badge("APPROVED")}</div><div class="definition-grid"><div><span>Domain</span><strong>${esc(annotation.domain_name)}</strong></div><div><span>Entity</span><strong>${esc(annotation.entity_name)}</strong></div><div><span>Role</span><strong>${esc(human(annotation.table_role))}</strong></div></div><p class="form-note spaced">Synonyms: ${esc(annotation.synonyms.join(", ") || "None")}</p>${questionRows ? `<div class="question-list"><h3>Suggested questions</h3>${questionRows}</div>` : ""}</section>` : businessOverview}</div><div class="asset-tab-panel ${tab === "quality" ? "active" : ""}" role="tabpanel" id="asset-pane-quality" aria-labelledby="asset-tab-quality" data-asset-pane="quality"><section><div class="section-heading"><div><span class="section-icon mint">DQ</span><div><strong>Quality evidence</strong><small>Value-free profile baseline and durable incidents</small></div></div>${qualityForTable.length ? badge("WARNING") : badge(profile ? "HEALTHY" : "NOT_CONFIGURED")}</div><div class="impact-grid"><div><strong>${profile ? profile.sampled_row_count : 0}</strong><span>Profiled rows</span></div><div><strong>${qualityForTable.length}</strong><span>Incidents</span></div><div><strong>${sensitive}</strong><span>Sensitive fields</span></div></div><button class="button secondary small" data-go="quality">Open quality workbench</button></section></div>`);
    $("#table-detail [data-asset-pane=\"intelligence\"]")?.insertAdjacentHTML("afterbegin", `${ownershipSection}${documentationSection}${termsSection}`);
  } catch (error) { setHtml("table-detail", empty("Table evidence unavailable", error.message)); }
}

async function loadSemanticModels() {
  const projectId = $("#semantic-project")?.value;
  state.semanticModels = projectId ? await fetchAll(`/v1/projects/${projectId}/semantic-model-versions`) : [];
  if (!state.semanticModels.some(item => item.id === state.selectedSemantic?.id)) state.selectedSemantic = state.semanticModels[0] || null;
  renderSemanticList();
  if (state.selectedSemantic) await selectSemantic(state.selectedSemantic.id); else { setHtml("semantic-editor", empty("No semantic versions", "Create a governed draft to begin.")); setHtml("semantic-metrics", empty("No metrics")); }
}

function renderSemanticList() {
  setHtml("semantic-table", `<div class="record-list">${state.semanticModels.map(item => `<button class="record-card ${state.selectedSemantic?.id === item.id ? "active" : ""}" data-model="${item.id}"><span><strong>${esc(item.name)}</strong><small>Version ${item.version} / ${esc(item.change_summary)}</small></span>${badge(item.status)}</button>`).join("") || empty("No semantic models")}</div>`);
}

async function selectSemantic(id) {
  state.selectedSemantic = state.semanticModels.find(item => item.id === id) || null;
  renderSemanticList();
  const model = state.selectedSemantic;
  if (!model) return;
  const actions = `${model.status === "DRAFT" ? `<button class="button small" data-add-metric="${model.id}">Add metric</button><button class="button small" data-submit-model="${model.id}">Submit for review</button>` : ""}<button class="button small" data-clone-model="${model.id}">Clone version</button>`;
  setHtml("semantic-editor", `<div class="panel-heading"><div><p class="eyebrow">VERSION ${model.version}</p><h2>${esc(model.name)}</h2></div>${badge(model.status)}</div><p>${esc(model.change_summary)}</p><div class="definition-grid"><div><span>Maker</span><strong>${esc(model.created_by)}</strong></div><div><span>Approved by</span><strong>${esc(model.approved_by || "Not approved")}</strong></div><div><span>Based on</span><strong>${esc(model.based_on_version_id || "New baseline")}</strong></div></div><div class="record-actions spaced">${actions}</div>`);
  const data = await api(`/v1/semantic-model-versions/${id}/metrics?limit=500`);
  state.semanticMetrics = data.items;
  const rows = state.semanticMetrics.map(metric => `<tr><td><span class="primary-cell">${esc(metric.metric_name)}</span><span class="secondary-cell">${esc(metric.metric_slug)} / v${metric.version}</span></td><td>${esc(metric.aggregation)}</td><td>${esc(metric.grain)}</td><td>${badge(metric.status)}</td><td><button class="row-action" data-record-title="Metric definition" data-record='${esc(JSON.stringify(metric))}'>Details</button></td></tr>`);
  renderTable("semantic-metrics", ["Metric","Aggregation","Grain","Status","Evidence"], rows, "This version has no metric definitions");
}

async function prepareMetricComposer() {
  const model = state.selectedSemantic;
  if (!model || model.status !== "DRAFT") return notify("Select a draft semantic version before adding a metric.");
  populateProjectSources("metric-source", model.project_id);
  await loadMetricTables();
  $("#metric-dialog").showModal();
}

async function loadMetricTables() {
  const sourceId = $("#metric-source").value;
  state.metricTables = sourceId ? await fetchAll(`/v1/datasources/${sourceId}/tables`) : [];
  preserveSelect("metric-table", selectOptions(state.metricTables, item => item.name, state.metricTables.length ? "" : "No active tables"));
  await loadMetricColumns();
}

async function loadMetricColumns() {
  const tableId = $("#metric-table").value;
  state.metricColumns = tableId ? await fetchAll(`/v1/tables/${tableId}/columns`) : [];
  const optional = selectOptions(state.metricColumns, item => `${item.name} (${item.physical_type})`, "None");
  preserveSelect("metric-measure", optional); preserveSelect("metric-time", optional);
  preserveSelect("metric-dimensions", selectOptions(state.metricColumns, item => `${item.name} (${human(item.classification)})`));
}

async function loadTools() {
  const projectId = $("#tools-project")?.value;
  state.tools = projectId ? await fetchAll(`/v1/projects/${projectId}/tools`) : [];
  if (!state.tools.some(item => item.id === state.selectedTool?.id)) state.selectedTool = state.tools[0] || null;
  renderToolList();
  if (state.selectedTool) selectTool(state.selectedTool.id); else setHtml("tool-detail", empty("No tool versions", "Create a parameter-bound SQL tool to begin."));
}

function renderToolList() {
  const filter = $("#tool-status-filter")?.value || "ALL";
  const visible = state.tools.filter(item => filter === "ALL" || item.status === filter);
  setHtml("tools-table", `<div class="record-list">${visible.map(item => `<button class="record-card ${state.selectedTool?.id === item.id ? "active" : ""}" data-tool="${item.id}"><span><strong>${esc(item.name)}</strong><small>${esc(item.slug)} / version ${item.version}</small></span>${badge(item.status)}</button>`).join("") || empty("No tool versions match")}</div>`);
}

function selectTool(id) {
  state.selectedTool = state.tools.find(item => item.id === id) || null;
  renderToolList();
  const tool = state.selectedTool;
  if (!tool) return;
  const actions = `${tool.status === "DRAFT" ? `<button class="button small" data-submit-tool="${tool.id}">Submit for review</button>` : ""}${tool.status === "PUBLISHED" ? `<button class="button small" data-deprecate-tool="${tool.id}">Request deprecation</button>` : ""}<button class="button small" data-new-version="${tool.id}">New version</button>`;
  setHtml("tool-detail", `<div class="panel-heading"><div><p class="eyebrow">${esc(tool.slug)} / VERSION ${tool.version}</p><h2>${esc(tool.name)}</h2></div>${badge(tool.status)}</div><p>${esc(tool.description)}</p><div class="definition-grid"><div><span>Data source</span><strong>${esc(state.sources.find(item => item.id === tool.datasource_id)?.name || tool.datasource_id)}</strong></div><div><span>Allowed roles</span><strong>${esc(tool.allowed_roles.join(", "))}</strong></div><div><span>Parameters</span><strong>${tool.parameters.length}</strong></div><div><span>Referenced tables</span><strong>${esc(tool.referenced_tables.join(", ") || "Validated at execution")}</strong></div><div><span>Maker</span><strong>${esc(tool.created_by)}</strong></div><div><span>Fingerprint</span><strong>${esc(tool.fingerprint.slice(0, 16))}</strong></div></div><pre class="code-block">${esc(tool.sql_template)}</pre><div class="record-actions">${actions}</div>`);
  const execute = $("#execute-tool");
  execute.disabled = tool.status !== "PUBLISHED";
  $("#tool-execution-title").textContent = tool.status === "PUBLISHED" ? `${tool.name} v${tool.version}` : "Publish this version before execution";
  setHtml("tool-parameters", tool.status === "PUBLISHED" ? tool.parameters.map(parameter => parameterInput(parameter)).join("") : "");
  setHtml("tool-result", "");
}

function parameterInput(parameter) {
  const required = parameter.required ? "required" : "";
  const bounds = `${parameter.minimum != null ? `min="${parameter.minimum}"` : ""} ${parameter.maximum != null ? `max="${parameter.maximum}"` : ""}`;
  if (parameter.parameter_type === "BOOLEAN") return `<label class="checkbox"><input name="${esc(parameter.name)}" type="checkbox" /> ${esc(human(parameter.name))}</label>`;
  if (parameter.allowed_values?.length) return `<label>${esc(human(parameter.name))}<select name="${esc(parameter.name)}" ${required}>${parameter.allowed_values.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join("")}</select></label>`;
  const type = ["INTEGER","NUMBER"].includes(parameter.parameter_type) ? "number" : parameter.parameter_type === "DATE" ? "date" : parameter.sensitive ? "password" : "text";
  const step = parameter.parameter_type === "NUMBER" ? "any" : "1";
  return `<label>${esc(human(parameter.name))}<input name="${esc(parameter.name)}" type="${type}" ${["INTEGER","NUMBER"].includes(parameter.parameter_type) ? `step="${step}" ${bounds}` : ""} ${parameter.max_length ? `maxlength="${parameter.max_length}"` : ""} ${required} value="${esc(parameter.default ?? "")}" /></label>`;
}

function addToolParameter(initial={}) {
  const node = document.createElement("div");
  node.className = "parameter-row";
  node.innerHTML = `<label>Name<input data-param="name" required pattern="[a-z][a-z0-9_]{0,63}" value="${esc(initial.name || "")}" /></label><label>Type<select data-param="parameter_type">${["STRING","INTEGER","NUMBER","BOOLEAN","DATE"].map(type => `<option ${type === (initial.parameter_type || "STRING") ? "selected" : ""}>${type}</option>`).join("")}</select></label><label class="checkbox"><input data-param="required" type="checkbox" ${initial.required === false ? "" : "checked"} /> Required</label><label class="checkbox"><input data-param="sensitive" type="checkbox" ${initial.sensitive ? "checked" : ""} /> Sensitive</label><label>Allowed values<input data-param="allowed_values" placeholder="NY,NJ" value="${esc((initial.allowed_values || []).join(","))}" /></label><button type="button" class="icon-button" data-remove-parameter aria-label="Remove parameter">&#215;</button>`;
  $("#tool-parameter-builder").append(node);
}

function collectToolParameters() {
  return $$("#tool-parameter-builder .parameter-row").map(row => {
    const value = key => row.querySelector(`[data-param="${key}"]`);
    const allowed = value("allowed_values").value.split(",").map(item => item.trim()).filter(Boolean);
    return {name:value("name").value, parameter_type:value("parameter_type").value, required:value("required").checked, sensitive:value("sensitive").checked, ...(allowed.length ? {allowed_values:allowed} : {})};
  });
}

function openToolAuthor(existing=null) {
  const form = $("#tool-author-form"); form.reset(); setHtml("tool-parameter-builder", "");
  populateProjectSources("tool-author-source", $("#tools-project").value);
  if (existing) {
    form.elements.slug.value = existing.slug; form.elements.name.value = existing.name; form.elements.description.value = existing.description;
    form.elements.datasource_id.value = existing.datasource_id; form.elements.allowed_roles.value = existing.allowed_roles.join(","); form.elements.sql_template.value = existing.sql_template;
    existing.parameters.forEach(addToolParameter);
  } else addToolParameter();
  $("#tool-dialog").showModal();
}

async function executeSelectedTool(form) {
  const tool = state.selectedTool;
  if (!tool || tool.status !== "PUBLISHED") return;
  const data = new FormData(form); const parameters = {};
  tool.parameters.forEach(definition => {
    let value = definition.parameter_type === "BOOLEAN" ? form.elements[definition.name].checked : data.get(definition.name);
    if (value === "" && !definition.required) return;
    if (definition.parameter_type === "INTEGER") value = Number.parseInt(value, 10);
    if (definition.parameter_type === "NUMBER") value = Number(value);
    parameters[definition.name] = value;
  });
  setHtml("tool-result", '<div class="loading">Validating parameters and executing through the query gateway</div>');
  try {
    const result = await api(`/v1/tool-versions/${tool.id}/execute`, {method:"POST", body:JSON.stringify({parameters})});
    renderQueryResult(result.execution, `${result.tool_slug} version ${result.tool_version} completed.`, "tool-result");
    notify("Governed tool execution completed.", true);
  } catch (error) { setHtml("tool-result", empty("Tool execution stopped", error.message)); notify(error.message); }
}

async function loadRelationships(options={}) {
  const sourceId = $("#relationship-source")?.value;
  if (!sourceId) { state.graph = null; return renderGraph(); }
  const sourceChanged = state.graphDatasourceId !== sourceId;
  if (sourceChanged) {
    state.graphDatasourceId = sourceId; state.graphFocusHistory = []; state.graphSelectedNodeId = null; state.graphSearchResults = [];
    setHtml("graph-search-results", ""); $("#graph-search-results")?.classList.remove("active");
  }
  const focusId = options.focusId || null;
  if (options.pushHistory) {
    const previousFocus = state.graph?.focus_node_id || "OVERVIEW";
    if (previousFocus !== focusId) state.graphFocusHistory.push(previousFocus);
  }
  const path = focusId
    ? `/v1/datasources/${sourceId}/knowledge-graph/neighborhood?focus_table_id=${encodeURIComponent(focusId)}&depth=${encodeURIComponent($("#graph-depth").value)}&direction=${encodeURIComponent($("#graph-direction").value)}&node_limit=100&edge_limit=500`
    : `/v1/datasources/${sourceId}/knowledge-graph?limit=500`;
  state.graph = await api(path);
  state.relationships = state.graph.edges.filter(edge => edge.edge_type === "SUGGESTED_RELATIONSHIP");
  state.graphSelectedNodeId = focusId || null;
  renderGraph();
  if (state.graphSelectedNodeId) await selectGraphNode(state.graphSelectedNodeId, false);
  else setHtml("graph-node-detail", empty("Select a table node", "Columns, classifications, edge evidence and downstream impact will appear here."));
}

function visibleGraphEdges() {
  if (!state.graph) return [];
  const filter = $("#graph-edge-filter")?.value || "ALL";
  return state.graph.edges.filter(edge => filter === "ALL" || edge.edge_type === filter || edge.status === filter);
}

function renderGraph() {
  const graph = state.graph;
  if (!graph) { setHtml("graph-metrics", ""); knowledgeGraphEngine?.setData([], [], {emptyHtml: empty("No source selected")}); setHtml("relationships-table", empty("No source selected")); return; }
  const visibleCount = graph.returned_node_count || graph.nodes.length; const visibleEdges = graph.returned_edge_count || graph.edges.length;
  const metrics = [[graph.focus_node_id ? "Visible nodes" : "Tables",graph.focus_node_id ? visibleCount : graph.total_tables,graph.focus_node_id ? `${graph.requested_depth} hop governed neighborhood` : "Current source"],["Visible edges",visibleEdges,`${graph.total_declared_edges} declared estate-wide`],["Suggestions",graph.total_suggested_edges,"Metadata-only"],["Pending",graph.pending_suggestions,"Checker queue"]];
  setHtml("graph-metrics", metrics.map(([a,b,c]) => `<div class="metric"><p>${a}</p><strong>${b}</strong><small>${c}</small></div>`).join(""));
  $("#graph-status").innerHTML = badge(graph.truncated ? "BOUNDED" : "CURRENT");
  const focusNode = graph.nodes.find(node => node.id === graph.focus_node_id);
  $("#graph-title").textContent = focusNode ? `Neighborhood / ${focusNode.label}` : "Estate relationship overview";
  $("#graph-scope-note").textContent = focusNode ? `${human(graph.direction)} · ${graph.requested_depth} hop maximum · ${visibleCount} nodes and ${visibleEdges} edges returned.` : "Select a node to inspect governed metadata, or focus it to expand a bounded neighborhood.";
  $("#graph-overview").disabled = !graph.focus_node_id; $("#graph-back").disabled = !state.graphFocusHistory.length;
  setHtml("graph-boundary-note", `<strong>Safe exploration boundary</strong><span>This graph contains metadata, classifications, aggregate profile evidence and approved relationships. It never renders raw customer, account or transaction values.</span>${graph.truncation_reasons?.length ? `<div class="graph-truncation">Bounded by ${esc(graph.truncation_reasons.map(human).join(", "))}. Refine the search or focus a nearby node.</div>` : ""}`);
  const rows = state.relationships.map(edge => `<tr><td><span class="primary-cell">${esc(edge.source_label)}</span><span class="secondary-cell">${esc(edge.source_columns.join(", "))}</span></td><td><span class="primary-cell">${esc(edge.target_label)}</span><span class="secondary-cell">${esc(edge.target_columns.join(", "))}</span></td><td>${Math.round(edge.confidence * 100)}%</td><td>${badge(edge.status)}</td><td>${esc(edge.evidence.source_values_inspected === false ? "Metadata only / no values" : "Bounded evidence")}</td><td>${edge.status === "PENDING" ? `<button class="row-action" data-relationship="${edge.candidate_id}" data-decision="APPROVE">Approve</button><button class="row-action danger" data-relationship="${edge.candidate_id}" data-decision="REJECT">Reject</button>` : "Decision retained"}</td></tr>`);
  renderTable("relationships-table", ["Source","Target","Confidence","Status","Evidence boundary","Checker"], rows, "No relationship suggestions");
  renderGraphStage(graph);
}

function knowledgeGraphNodeHtml(data, meta) {
  const classes = ["atlas-node-card"];
  if (data.sensitive_column_count) classes.push("is-sensitive");
  if (data.isFocus) classes.push("is-focus");
  if (meta.selected) classes.push("is-selected");
  if (data.agMatch) classes.push("is-match");
  if (data.agDim) classes.push("is-dim");
  return `<button type="button" class="${classes.join(" ")}" data-graph-node="${data.id}" title="${esc(data.qualified_name || "")}" aria-pressed="${meta.selected}">`
    + `<span class="ag-title">${esc(data.label || data.id)}</span>`
    + `<span class="ag-sub">${data.column_count || 0} columns \u00b7 ${data.sensitive_column_count || 0} sensitive</span>`
    + `<span class="ag-meta"><span class="ag-pill">${data.inbound_edge_count || 0} in</span><span class="ag-pill">${data.outbound_edge_count || 0} out</span>${data.depth ? `<span class="ag-pill">hop ${data.depth}</span>` : ""}</span>`
    + `</button>`;
}

function renderGraphStage(graph) {
  if (!knowledgeGraphEngine) {
    knowledgeGraphEngine = new window.AtlasUI.AtlasGraph("graph-stage", {
      direction: "LR",
      nodeHtml: knowledgeGraphNodeHtml,
      matchNode: (data, q) => `${data.label || ""} ${data.qualified_name || ""}`.toLowerCase().includes(q),
      onNodeExpand: data => loadRelationships({focusId:data.id, pushHistory:true}).catch(error => notify(error.message))
    });
  }
  const edges = visibleGraphEdges();
  const connectedIds = new Set(edges.flatMap(edge => [edge.source_node_id, edge.target_node_id]));
  let nodes = graph.nodes.filter(node => connectedIds.has(node.id));
  if (!nodes.length) nodes = graph.nodes;
  nodes = nodes.slice(0, 90);
  const allowed = new Set(nodes.map(node => node.id));
  const cyNodes = nodes.map(node => ({
    id: node.id, w: 190, h: 96,
    data: {
      label: node.label, qualified_name: node.qualified_name, column_count: node.column_count,
      sensitive_column_count: node.sensitive_column_count, inbound_edge_count: node.inbound_edge_count,
      outbound_edge_count: node.outbound_edge_count, depth: node.depth || 0,
      isFocus: node.id === graph.focus_node_id
    }
  }));
  const cyEdges = edges.filter(edge => allowed.has(edge.source_node_id) && allowed.has(edge.target_node_id)).map(edge => ({
    id: edge.candidate_id || `${edge.source_node_id}->${edge.target_node_id}`,
    source: edge.source_node_id, target: edge.target_node_id,
    classes: edge.edge_type === "DECLARED_FOREIGN_KEY" ? "declared" : (edge.status || "suggested").toLowerCase()
  }));
  knowledgeGraphEngine.setData(cyNodes, cyEdges, {
    selectId: state.graphSelectedNodeId,
    emptyHtml: empty("No relationships in view", "Broaden the edge filter or focus a different table.")
  });
}

async function selectGraphNode(nodeId, redraw=true) {
  const node = state.graph?.nodes.find(item => item.id === nodeId); if (!node) return;
  state.graphSelectedNodeId = nodeId; if (redraw && knowledgeGraphEngine) knowledgeGraphEngine.select(nodeId);
  setHtml("graph-node-detail", '<div class="loading">Loading governed node evidence</div>');
  try {
    const [columns,impact,annotation,profile] = await Promise.all([
      fetchAll(`/v1/tables/${nodeId}/columns`), api(`/v1/metadata/tables/${nodeId}/impact`),
      api(`/v1/metadata/tables/${nodeId}/business-annotation`).catch(error => error.status === 404 ? null : Promise.reject(error)),
      api(`/v1/tables/${nodeId}/profile`).catch(error => error.status === 404 ? null : Promise.reject(error))
    ]);
    const connected = state.graph.edges.filter(edge => edge.source_node_id === nodeId || edge.target_node_id === nodeId);
    const sensitive = columns.filter(column => ["PII","PCI","PHI","SECRET","CONFIDENTIAL"].includes(column.classification)).length;
    const columnRows = columns.slice(0, 60).map(column => `<div class="graph-column-row"><strong>${esc(column.name)}</strong>${badge(column.classification)}<small>${esc(column.physical_type)} · ${column.nullable ? "nullable" : "required"}</small></div>`).join("");
    const edgeRows = connected.slice(0, 40).map(edge => { const outgoing = edge.source_node_id === nodeId; const other = outgoing ? edge.target_label : edge.source_label; return `<div class="graph-edge-row"><strong>${outgoing ? "References" : "Referenced by"} ${esc(other)}</strong>${badge(edge.status)}<small>${esc((outgoing ? edge.source_columns : edge.target_columns).join(", "))} · ${Math.round(edge.confidence*100)}% confidence · no source values inspected</small></div>`; }).join("");
    setHtml("graph-node-detail", `<div class="graph-node-summary"><div>${badge(node.object_type)} ${sensitive ? badge("SENSITIVE") : ""}</div><h3>${esc(annotation?.business_name || node.label)}</h3><p>${esc(node.qualified_name)}</p>${annotation ? `<p>${esc(annotation.business_description)}</p>` : `<p>No approved business annotation is attached yet.</p>`}<div class="graph-node-facts"><div><span>Columns</span><strong>${columns.length}</strong></div><div><span>Sensitive</span><strong>${sensitive}</strong></div><div><span>Connected edges</span><strong>${connected.length}</strong></div><div><span>Downstream objects</span><strong>${impact.downstream_object_count}</strong></div><div><span>Profile sample</span><strong>${profile?.sampled_row_count ?? "Not profiled"}</strong></div><div><span>Graph depth</span><strong>${node.depth || 0}</strong></div></div><div class="graph-node-actions"><button class="button primary small" data-graph-focus="${node.id}">Focus and expand</button><button class="button secondary small" data-graph-catalog="${node.id}">Open catalog detail</button></div><section class="graph-node-section"><h4>Columns and classifications</h4><div class="graph-column-list">${columnRows || empty("No active columns")}</div>${columns.length > 60 ? `<p class="form-note">Showing 60 of ${columns.length} columns.</p>` : ""}</section><section class="graph-node-section"><h4>Relationship evidence</h4><div class="graph-edge-list">${edgeRows || empty("No visible edges", "Increase depth or change direction to inspect more relationships.")}</div></section></div>`);
  } catch (error) { setHtml("graph-node-detail", empty("Node evidence unavailable", error.message)); }
}

async function searchGraph() {
  const sourceId = $("#relationship-source").value; const query = $("#graph-search").value.trim();
  if (!sourceId || query.length < 2) return notify("Enter at least two characters to search the graph.");
  setHtml("graph-search-results", '<div class="loading">Searching governed metadata</div>'); $("#graph-search-results").classList.add("active");
  const result = await api(`/v1/datasources/${sourceId}/knowledge-graph/search?q=${encodeURIComponent(query)}&limit=25`); state.graphSearchResults = result.items;
  setHtml("graph-search-results", result.items.length ? result.items.map(node => `<button class="graph-search-result" data-graph-search-node="${node.id}"><strong>${esc(node.label)}</strong><small>${esc(node.qualified_name)} · ${node.column_count} columns</small></button>`).join("") + (result.truncated ? `<p class="form-note">Showing 25 of ${result.total} matches. Refine the search for a narrower result.</p>` : "") : empty("No graph nodes matched", "Try a table, schema, or catalog name."));
}

async function loadModelRoutes() {
  state.modelRoutes = state.organizationId ? await fetchAll(`/v1/organizations/${state.organizationId}/model-routes`) : [];
  const rows = state.modelRoutes.map(route => `<tr><td><span class="primary-cell">${esc(route.display_name)}</span><span class="secondary-cell">${esc(route.route_key)} / version ${route.version}</span></td><td>${badge(route.status)}</td><td>${esc(human(route.provider_type))}<span class="secondary-cell">${esc(route.model_id)}</span></td><td>${esc(route.data_residency)} / ${esc(human(route.retention_policy))}</td><td>${badge(route.activation_status)}<span class="secondary-cell">Adapter ${route.adapter_available ? "registered" : "not registered"}</span></td><td>${route.status === "DRAFT" ? `<button class="row-action" data-submit-route="${route.id}">Submit</button>` : `<button class="row-action" data-record-title="Model route definition" data-record='${esc(JSON.stringify(route))}'>Details</button>`}</td></tr>`);
  renderTable("model-routes-table", ["Route","Governance","Provider","Residency / retention","Activation","Action"], rows, "No governed model route definitions");
}

async function loadMemory() {
  const sourceId = $("#memory-source").value; if (!sourceId) return setHtml("memory-table", empty("No source selected"));
  const status = $("#memory-status").value; const suffix = status === "ALL" ? "" : `?memory_status=${encodeURIComponent(status)}`;
  state.memory = await fetchAll(`/v1/datasources/${sourceId}/query-memory${suffix}`);
  const rows = state.memory.map(item => `<tr><td><span class="primary-cell">${esc(item.agent_run_id)}</span><span class="secondary-cell">Execution ${esc(item.query_execution_id)}</span></td><td>${badge(item.status)}</td><td>${item.positive_feedback_count}</td><td>${item.negative_feedback_count}</td><td>${esc(item.semantic_version || "Unpinned")}</td><td>${when(item.updated_at)}</td></tr>`);
  renderTable("memory-table", ["Agent / execution","Eligibility","Positive","Negative","Semantic version","Updated"], rows, "No value-free query memory evidence matches");
}

async function loadOutbox() {
  const params = new URLSearchParams(); const status = $("#outbox-status").value; const type = $("#outbox-type").value.trim();
  if (status !== "ALL") params.set("status", status); if (type) params.set("event_type", type);
  state.outbox = await fetchAll(`/v1/organizations/${state.organizationId}/outbox-events${params.toString() ? `?${params}` : ""}`);
  const rows = state.outbox.map(item => `<tr><td><span class="primary-cell">${esc(human(item.event_type))}</span><span class="secondary-cell">${esc(item.id)}</span></td><td>${esc(human(item.aggregate_type))}<span class="secondary-cell">${esc(item.aggregate_id)}</span></td><td>${badge(item.status)}</td><td>${item.attempt_count}</td><td>${esc(item.last_error || "None")}</td><td>${when(item.occurred_at)}</td><td>${item.status === "DEAD_LETTER" ? `<button class="row-action" data-requeue="${item.id}">Requeue</button>` : ""}</td></tr>`);
  renderTable("outbox-table", ["Event","Aggregate","Status","Attempts","Last error","Occurred","Recovery"], rows, "No delivery events match these filters");
}

async function loadQuality() {
  const sourceId = $("#quality-source")?.value;
  if (!sourceId) {
    Object.assign(state, {qualitySummary:null, qualityPolicies:[], qualityObservations:[], qualityIncidents:[]});
    return renderQuality();
  }
  ["quality-incidents-table","quality-observations-table"].forEach(id => setHtml(id, '<div class="loading">Loading quality evidence</div>'));
  const [summary, policies, observations, incidents] = await Promise.all([
    api(`/v1/datasources/${sourceId}/quality-summary`),
    fetchAll(`/v1/datasources/${sourceId}/quality-policies`),
    fetchAll(`/v1/datasources/${sourceId}/quality-observations`),
    fetchAll(`/v1/datasources/${sourceId}/quality-incidents`)
  ]);
  Object.assign(state, {qualitySummary:summary, qualityPolicies:policies, qualityObservations:observations, qualityIncidents:incidents});
  renderQuality();
}

function renderQuality() {
  const summary = state.qualitySummary;
  if (!summary) {
    setHtml("quality-metrics", "");
    setHtml("quality-incidents-table", empty("No source selected", "Register or select a source to configure baseline controls."));
    setHtml("quality-observations-table", empty("No quality observations"));
    $("#quality-nav-count").textContent = "0";
    return;
  }
  const metrics = [
    ["Observed tables", `${summary.observed_table_count} / ${summary.table_count}`, "Latest profile coverage"],
    ["Average score", summary.average_quality_score ?? "—", "Latest observation per table"],
    ["Active incidents", summary.open_incident_count, `${summary.critical_incident_count} critical`],
    ["Metadata scan", human(summary.metadata_scan_status), summary.metadata_scan_age_minutes == null ? "No completed observation" : `${Math.round(summary.metadata_scan_age_minutes)} minutes ago`]
  ];
  setHtml("quality-metrics", metrics.map(([label,value,detail]) => `<div class="metric"><p>${esc(label)}</p><strong>${esc(value)}</strong><small>${esc(detail)}</small></div>`).join(""));
  $("#quality-nav-count").textContent = summary.open_incident_count;

  const policy = state.qualityPolicies.find(item => item.table_id == null);
  const form = $("#quality-policy-form");
  if (policy && form) {
    ["name","volume_change_percent","null_rate_change_percent","metadata_scan_max_age_minutes"].forEach(key => form.elements[key].value = policy[key]);
    form.elements.schema_change_enabled.checked = policy.schema_change_enabled;
    form.elements.enabled.checked = policy.enabled;
  }
  setHtml("quality-policy-status", policy ? badge(policy.enabled ? "ACTIVE" : "DISABLED") : badge("SYSTEM_DEFAULT"));

  const incidentStatus = $("#quality-incident-status")?.value || "ACTIVE";
  const incidents = state.qualityIncidents.filter(item => incidentStatus === "ALL" || (incidentStatus === "ACTIVE" ? ["OPEN","ACKNOWLEDGED"].includes(item.status) : item.status === incidentStatus));
  const incidentRows = incidents.map(item => `<tr><td><button class="link-button" data-quality-detail="${item.id}" data-quality-kind="incident">${esc(item.table_name)}</button><span class="secondary-cell">${esc(human(item.anomaly_type))}</span></td><td>${badge(item.severity)}</td><td>${badge(item.status)}</td><td>${item.occurrence_count}</td><td>${when(item.last_observed_at)}</td><td>${item.status !== "RESOLVED" ? `<button class="row-action" data-quality-transition="${item.id}" data-quality-status="ACKNOWLEDGED">Acknowledge</button><button class="row-action" data-quality-transition="${item.id}" data-quality-status="RESOLVED">Resolve</button>` : esc(item.resolution_reason || "Recovered")}</td></tr>`);
  renderTable("quality-incidents-table", ["Table / control","Severity","Status","Occurrences","Last observed","Action"], incidentRows, "No quality incidents match this view");

  const observationStatus = $("#quality-observation-status")?.value || "ALL";
  const observations = state.qualityObservations.filter(item => observationStatus === "ALL" || item.status === observationStatus);
  const observationRows = observations.map(item => `<tr><td><button class="link-button" data-quality-detail="${item.id}" data-quality-kind="observation">${esc(item.table_name)}</button><span class="secondary-cell">${esc(item.analysis_run_id)}</span></td><td>${badge(item.status)}</td><td>${item.quality_score}</td><td>${item.anomaly_types.length ? item.anomaly_types.map(human).map(esc).join(", ") : "None"}</td><td>${item.baseline_profile_id ? "Historical profile" : "Baseline established"}</td><td>${when(item.created_at)}</td></tr>`);
  renderTable("quality-observations-table", ["Table / run","Status","Score","Anomalies","Comparison","Observed"], observationRows, "No quality observations match this view");
}

async function loadSchedule() {
  const sourceId = $("#schedule-source")?.value;
  if (!sourceId) return setHtml("schedule-status", '<p class="form-note">No source selected.</p>');
  try {
    const policy = await api(`/v1/datasources/${sourceId}/scan-policy`); const form = $("#schedule-form");
    form.elements.interval_minutes.value = policy.interval_minutes; form.elements.mode.value = policy.mode; form.elements.priority.value = policy.priority; form.elements.enabled.checked = policy.enabled;
    form.elements.maintenance_start_hour_utc.value = policy.maintenance_start_hour_utc ?? ""; form.elements.maintenance_end_hour_utc.value = policy.maintenance_end_hour_utc ?? "";
    setHtml("schedule-status", `<p class="form-note">Next run ${when(policy.next_run_at)} / last triggered ${when(policy.last_triggered_at)}</p>`);
  } catch (error) { setHtml("schedule-status", `<p class="form-note">${error.status === 404 ? "No scan policy configured." : esc(error.message)}</p>`); }
}

function showRecord(title, record) {
  $("#record-title").textContent = title;
  const entries = Object.entries(record || {}).map(([key,value]) => `<dt>${esc(human(key))}</dt><dd>${esc(typeof value === "object" && value !== null ? JSON.stringify(value, null, 2) : value ?? "Not recorded")}</dd>`).join("");
  setHtml("record-content", `<dl class="record-json">${entries}</dl>`); $("#record-dialog").showModal();
}

function openDecision(kind, id, decision="APPROVE") {
  let record;
  if (kind === "governance") record = state.reviews.find(item => item.id === id);
  else record = state.relationships.find(item => item.candidate_id === id);
  state.pendingDecision = {kind, id}; $("#decision-value").value = decision; $("#decision-reason").value = "";
  $("#decision-title").textContent = kind === "governance" ? "Review governed change" : "Review relationship suggestion";
  setHtml("decision-context", kind === "governance" ? `<strong>${esc(human(record?.object_type))} / ${esc(record?.requested_action)}</strong><span>Maker ${esc(record?.requested_by)} / object ${esc(record?.object_id)}</span>` : `<strong>${esc(record?.source_label)} to ${esc(record?.target_label)}</strong><span>${Math.round((record?.confidence || 0) * 100)}% confidence / metadata-only evidence</span>`);
  $("#decision-dialog").showModal();
}

async function refreshAfter(action, message, reload=true) {
  try { await action(); notify(message, true); if (reload) await loadOrganizationData(); }
  catch (error) { notify(error.message); }
}

async function submitDecision() {
  if (!state.pendingDecision) return;
  const decision = $("#decision-value").value; const reason = $("#decision-reason").value.trim();
  if (decision === "REJECT" && !reason) return notify("A rejection rationale is required.");
  const {kind,id} = state.pendingDecision;
  const path = kind === "governance" ? `/v1/governance/reviews/${id}/decision` : `/v1/relationship-candidates/${id}/decision`;
  $("#decision-dialog").close();
  await refreshAfter(() => api(path, {method:"POST", principal:"local-ui-checker", body:JSON.stringify({decision, reason:reason || null})}), `${human(decision)} decision recorded with independent checker evidence.`);
}


const NAV_INDEX = [
  {view:"home", label:"Home", hint:"Operating brief"},
  {view:"analyst", label:"AI analyst", hint:"Ask, plan, execute"},
  {view:"catalog", label:"Data catalog", hint:"Search technical metadata"},
  {view:"transformations", label:"Transformations", hint:"Transformation metadata adapters"},
  {view:"meaning", label:"Business meaning", hint:"Semantic inference workbench"},
  {view:"semantics", label:"Semantic layer", hint:"Versioned metrics"},
  {view:"tools", label:"Tool registry", hint:"Governed reusable tools"},
  {view:"relationships", label:"Knowledge graph", hint:"Relationship intelligence"},
  {view:"governance", label:"Review center", hint:"Maker-checker queue"},
  {view:"agents", label:"AI control center", hint:"Models, agents, evaluations"},
  {view:"sources", label:"Source fleet", hint:"Connections and scan policy"},
  {view:"quality", label:"Data quality", hint:"Baselines and incidents"},
  {view:"operations", label:"Operations", hint:"Runs, memory, event delivery"},
  {view:"administration", label:"Platform setup", hint:"Tenant onboarding"},
  {view:"audit", label:"Audit evidence", hint:"Attributable decision ledger"}
];

function viewVisible(view) {
  return view !== "transformations" || transformationMetadataSurfaceEnabled();
}

function visibleNavEntries() {
  return NAV_INDEX.filter(entry => viewVisible(entry.view));
}

function applyPersona() {
  const persona = state.persona || "all";
  $$(".nav-item[data-persona]").forEach(node => {
    const allowed = persona === "all" || node.dataset.persona.split(" ").includes(persona);
    node.classList.toggle("persona-hidden", !allowed);
  });
  if ($(".nav-item.active.persona-hidden, .nav-item.active.integration-hidden")) {
    const first = $(".nav-item[data-persona]:not(.persona-hidden):not(.integration-hidden)");
    if (first) showView(first.dataset.view);
  }
}

function paletteEntries() {
  const dynamic = [
    ...state.tables.map(item => ({type:"Table", label:item.name, hint:human(item.object_type), action: () => { showView("catalog"); showTable(item.id); }})),
    ...state.sources.map(item => ({type:"Source", label:item.name, hint:`${item.connector_type} / ${human(item.status)}`, action: () => showView("sources")})),
    ...state.tools.map(item => ({type:"Tool", label:item.name, hint:human(item.status), action: () => { showView("tools"); selectTool(item.id); }})),
    ...state.semanticModels.map(item => ({type:"Semantic model", label:item.name, hint:`v${item.version} / ${human(item.status)}`, action: () => { showView("semantics"); selectSemantic(item.id); }})),
    ...state.glossaryTerms.map(item => ({type:"Glossary term", label:item.display_name, hint:`${item.term_key} / ${human(item.status)}`, action: () => showView("meaning")})),
    ...(dbtEnabled() ? state.dbtProjects.map(item => ({type:"dbt project", label:item.display_name, hint:human(item.status), action: () => { showView("transformations"); selectDbtProject(item.id).catch(error => notify(error.message)); }})) : [])
  ];
  const views = visibleNavEntries().map(entry => ({type:"View", label:entry.label, hint:entry.hint, action: () => showView(entry.view)}));
  return [...views, ...dynamic];
}

function renderPaletteResults(query) {
  const q = query.trim().toLowerCase();
  const entries = paletteEntries();
  const visible = (q ? entries.filter(entry => `${entry.label} ${entry.type} ${entry.hint || ""}`.toLowerCase().includes(q)) : entries.filter(entry => entry.type === "View")).slice(0, 40);
  setHtml("palette-results", visible.length ? visible.map((entry,index) => `<button type="button" id="palette-option-${index}" role="option" aria-selected="${index === 0}" class="palette-row ${index === 0 ? "active" : ""}" data-palette-index="${index}"><span class="palette-type">${esc(entry.type)}</span><span class="palette-label">${esc(entry.label)}</span><span class="palette-hint">${esc(entry.hint || "")}</span></button>`).join("") : empty("No matches", "Try a different view, table, tool, source, semantic model, or transformation adapter name."));
  state.paletteEntries = visible;
  state.paletteActiveIndex = visible.length ? 0 : -1;
  $("#palette-input")?.setAttribute("aria-activedescendant", visible.length ? "palette-option-0" : "");
}

function openPalette() {
  const input = $("#palette-input");
  input.value = "";
  renderPaletteResults("");
  $("#command-palette").showModal();
  window.requestAnimationFrame(() => input.focus());
}

function movePaletteSelection(delta) {
  const rows = $$(".palette-row");
  if (!rows.length) return;
  state.paletteActiveIndex = (state.paletteActiveIndex + delta + rows.length) % rows.length;
  rows.forEach((row,index) => { const active = index === state.paletteActiveIndex; row.classList.toggle("active", active); row.setAttribute("aria-selected", String(active)); });
  rows[state.paletteActiveIndex].scrollIntoView({block:"nearest"});
  $("#palette-input")?.setAttribute("aria-activedescendant", `palette-option-${state.paletteActiveIndex}`);
}

function activatePaletteSelection() {
  const entry = state.paletteEntries[state.paletteActiveIndex];
  if (!entry) return;
  $("#command-palette").close();
  entry.action();
}

function bindPaletteEvents() {
  $("#persona-select").addEventListener("change", event => {
    state.persona = event.target.value;
    localStorage.setItem("aida-persona", state.persona);
    applyPersona();
  });
  $("#palette-trigger").addEventListener("click", openPalette);
  $("#palette-input").addEventListener("input", event => renderPaletteResults(event.target.value));
  $("#palette-input").addEventListener("keydown", event => {
    if (event.key === "ArrowDown") { event.preventDefault(); movePaletteSelection(1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); movePaletteSelection(-1); }
    else if (event.key === "Enter") { event.preventDefault(); activatePaletteSelection(); }
  });
  document.addEventListener("keydown", event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openPalette(); }
  });
  $("#command-palette").addEventListener("close", () => { $("#palette-trigger")?.focus(); });
}

function bindTabKeyboardNav() {
  document.addEventListener("keydown", event => {
    if (!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return;
    const tab = event.target.closest('[role="tab"]');
    if (!tab) return;
    const list = tab.closest('[role="tablist"]');
    if (!list) return;
    const tabs = [...list.querySelectorAll('[role="tab"]')];
    const currentIndex = tabs.indexOf(tab);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
    else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    if (nextIndex === currentIndex) return;
    event.preventDefault();
    tabs[nextIndex].focus();
    tabs[nextIndex].click();
  });
}

function prepareAccessibility() {
  $$("dialog").forEach(dialog => {
    if (dialog.hasAttribute("aria-label") || dialog.hasAttribute("aria-labelledby")) return;
    const title = dialog.querySelector("h2");
    if (!title) return;
    title.id ||= `${dialog.id}-title`;
    dialog.setAttribute("aria-labelledby", title.id);
  });
  ["stewardship-coverage","glossary-link-proposals","glossary-conflicts","bulk-stewardship-operations"].forEach(id => {
    document.getElementById(id)?.setAttribute("aria-live", "polite");
  });
}

function showView(name) {
  if (!viewVisible(name)) name = "administration";
  const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  $$(".view").forEach(node => node.classList.toggle("active", node.id === `${name}-view`));
  $$(".nav-item").forEach(node => {
    const active = node.dataset.view === name;
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
  });
  const titles = {home:"Home",analyst:"Ask Atlas",catalog:"All assets",transformations:"Transformation metadata",meaning:"Business meaning",semantics:"Semantic layer",tools:"Tool registry",relationships:"Knowledge graph",governance:"Review center",agents:"AI governance",sources:"Sources",quality:"Data quality",operations:"Operations",administration:"Administration",audit:"Audit evidence"};
  $("#page-title").textContent = titles[name] || human(name); history.replaceState(null, "", `#${name}`);
  if (name === "relationships") window.requestAnimationFrame(() => knowledgeGraphEngine?.resizeAndFit());
  if (name === "operations" && $("#ops-memory").classList.contains("active")) loadMemory().catch(error => notify(error.message));
  if (name === "operations" && $("#ops-outbox").classList.contains("active")) loadOutbox().catch(error => notify(error.message));
  document.body.classList.remove("nav-open");
  $("#nav-toggle")?.setAttribute("aria-expanded", "false");
  window.scrollTo({top:0, behavior: reducedMotion ? "auto" : "smooth"});
  window.requestAnimationFrame(() => $("#page-title")?.focus({preventScroll:true}));
}

window.AtlasUI.navigateTo = showView;

function openAssetDocumentationDialog() {
  if (!state.selectedTableId) return notify("Select an asset first.");
  const form = $("#asset-documentation-form");
  const documentation = state.selectedAssetDocumentation;
  form.elements.aliases.value = (documentation?.aliases || []).join(", ");
  form.elements.owner_principal.value = documentation?.owner_principal || "";
  form.elements.readme.value = documentation?.readme || "";
  $("#asset-documentation-dialog").showModal();
}

function openAssetTermDialog() {
  if (!state.selectedTableId) return notify("Select an asset first.");
  const linked = new Set(state.selectedAssetLinks.map(link => link.term_id));
  const approved = state.glossaryTerms.filter(term => term.status === "APPROVED" && !linked.has(term.term_id));
  preserveSelect("asset-term-select", selectOptions(approved.map(term => ({...term, id:term.term_id})), term => `${term.display_name} (${term.term_key})`, "Choose an approved term"));
  if (!approved.length) return notify("No additional approved glossary terms are available.");
  $("#asset-term-dialog").showModal();
}

function openBulkStewardshipDialog(operationType="ASSIGN_OWNERSHIP", subjectType="TABLE", subjectIds=[]) {
  const form = $("#stewardship-bulk-form");
  form.reset();
  form.elements.operation_type.value = operationType;
  form.elements.subject_type.value = subjectType;
  form.elements.subject_ids.value = subjectIds.join("\n");
  $("#stewardship-bulk-dialog").showModal();
}

function bindDirectEvents() {
  $("#nav-toggle").addEventListener("click", () => { const open = document.body.classList.toggle("nav-open"); $("#nav-toggle").setAttribute("aria-expanded", String(open)); });
  $("#refresh-button").addEventListener("click", () => loadOrganizationData().then(() => notify("Organization data refreshed.", true)).catch(error => notify(error.message)));
  $("#organization-select").addEventListener("change", async event => { state.organizationId = event.target.value; localStorage.setItem("aida-organization", state.organizationId); await loadOrganizationData(); });
  $("#preview-plan").addEventListener("click", previewPlan); $("#run-analysis").addEventListener("click", runAnalysis); $("#validate-sql").addEventListener("click", validateSql);
  $("#analyst-source").addEventListener("change", loadAgentRuns); $("#catalog-source").addEventListener("change", loadTables); $("#catalog-search").addEventListener("input", renderTables);
  $("#catalog-type-filter").addEventListener("change", renderTables); $("#catalog-status-filter").addEventListener("change", renderTables);
  $("#clear-catalog-filters").addEventListener("click", () => { $("#catalog-search").value = ""; $("#catalog-type-filter").value = "ALL"; $("#catalog-status-filter").value = "ALL"; renderTables(); });
  $("#new-glossary-term").addEventListener("click", () => $("#glossary-term-dialog").showModal());
  $("#new-glossary-category").addEventListener("click", () => $("#glossary-category-dialog").showModal());
  $("#new-ownership-rule").addEventListener("click", () => $("#ownership-rule-dialog").showModal());
  $("#open-stewardship-actions").addEventListener("click", () => openBulkStewardshipDialog());
  $("#snapshot-coverage").addEventListener("click", () => refreshAfter(() => api(`/v1/organizations/${state.organizationId}/stewardship/coverage/snapshots`, {method:"POST"}), "Coverage snapshot recorded with value-free evidence."));
  $("#generate-link-proposals").addEventListener("click", () => refreshAfter(() => api(`/v1/organizations/${state.organizationId}/glossary-link-proposals/generate`, {method:"POST", body:JSON.stringify({minimum_confidence:.75, limit:200})}), "Approved semantic annotations were matched to glossary language."));
  $("#detect-glossary-conflicts").addEventListener("click", () => refreshAfter(() => api(`/v1/organizations/${state.organizationId}/glossary-conflicts/detect`, {method:"POST"}), "Glossary conflict detection completed."));
  $("#glossary-term-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target);
    const synonyms = String(data.get("synonyms") || "").split(",").map(value => value.trim()).filter(Boolean);
    const body = {term_key:data.get("term_key"), display_name:data.get("display_name"), definition:data.get("definition"), category_id:data.get("category_id") || null, synonyms, owner_principal:data.get("owner_principal")?.trim() || null};
    try { await api(`/v1/organizations/${state.organizationId}/glossary-terms`, {method:"POST", body:JSON.stringify(body)}); $("#glossary-term-dialog").close(); event.target.reset(); notify("Glossary draft created. Submit it for independent review when ready.", true); await loadGlossary(); }
    catch (error) { notify(error.message); }
  });
  $("#glossary-category-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target);
    const body = {category_key:data.get("category_key"), display_name:data.get("display_name"), description:data.get("description"), parent_id:data.get("parent_id") || null};
    try { await api(`/v1/organizations/${state.organizationId}/glossary-categories`, {method:"POST", body:JSON.stringify(body)}); $("#glossary-category-dialog").close(); event.target.reset(); notify("Glossary category created.", true); await loadGlossary(); }
    catch (error) { notify(error.message); }
  });
  $("#ownership-rule-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target);
    const body = Object.fromEntries(["rule_key","display_name","match_field","match_pattern","owner_type","owner_principal"].map(key => [key,data.get(key)]));
    try { await api(`/v1/organizations/${state.organizationId}/ownership-rules`, {method:"POST", body:JSON.stringify(body)}); $("#ownership-rule-dialog").close(); event.target.reset(); notify("Ownership routing rule created. Apply it when ready.", true); await loadGlossary(); }
    catch (error) { notify(error.message); }
  });
  $("#stewardship-bulk-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target);
    const subjectIds = String(data.get("subject_ids") || "").split(/[\s,]+/).map(value => value.trim()).filter(Boolean);
    const expires = data.get("expires_at");
    const body = {operation_type:data.get("operation_type"), subject_type:data.get("subject_type"), subject_ids:subjectIds, owner_type:data.get("owner_principal")?.trim() ? data.get("owner_type") : null, owner_principal:data.get("owner_principal")?.trim() || null, term_id:data.get("term_id") || null, rationale:data.get("rationale")?.trim() || null, expires_at:expires ? new Date(expires).toISOString() : null, source_rule_id:null};
    try { await api(`/v1/organizations/${state.organizationId}/stewardship/bulk-operations`, {method:"POST", body:JSON.stringify(body)}); $("#stewardship-bulk-dialog").close(); event.target.reset(); notify("Bounded stewardship operation submitted for independent review.", true); await loadOrganizationData(); }
    catch (error) { notify(error.message); }
  });
  $("#conflict-resolution-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target); const conflictId = data.get("conflict_id");
    const body = {resolution:data.get("resolution"), resolved_definition:data.get("resolved_definition")?.trim() || null, rationale:data.get("rationale")};
    try { await api(`/v1/glossary-conflicts/${conflictId}/resolution`, {method:"POST", body:JSON.stringify(body)}); $("#conflict-resolution-dialog").close(); event.target.reset(); notify("Conflict resolution submitted for independent review.", true); await loadOrganizationData(); }
    catch (error) { notify(error.message); }
  });
  $("#asset-documentation-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target);
    const aliases = String(data.get("aliases") || "").split(",").map(value => value.trim()).filter(Boolean);
    const body = {aliases, readme:data.get("readme"), owner_principal:data.get("owner_principal")?.trim() || null};
    try { await api(`/v1/metadata/tables/${state.selectedTableId}/documentation-versions`, {method:"POST", body:JSON.stringify(body)}); $("#asset-documentation-dialog").close(); notify("Documentation draft created. Submit it for independent review.", true); await showTable(state.selectedTableId); }
    catch (error) { notify(error.message); }
  });
  $("#asset-term-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target);
    try { await api(`/v1/metadata/tables/${state.selectedTableId}/glossary-links`, {method:"POST", body:JSON.stringify({term_id:data.get("term_id")})}); $("#asset-term-dialog").close(); notify("Approved glossary term linked to the asset.", true); await showTable(state.selectedTableId); }
    catch (error) { notify(error.message); }
  });
  $("#semantic-project").addEventListener("change", async event => { populateProjectSources("metric-source", event.target.value); await loadSemanticModels(); });
  $("#transform-project").addEventListener("change", () => loadDbtProjects().catch(error => notify(error.message)));
  $("#integration-policy-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target;
    const body = {
      transformation_metadata_integrations: {
        dbt: form.elements.dbt.checked,
        openlineage: form.elements.openlineage.checked,
        airflow: form.elements.airflow.checked,
        generic_elt: form.elements.generic_elt.checked
      }
    };
    try {
      state.integrationPolicy = await api(`/v1/organizations/${state.organizationId}/integration-policy`, {method:"PUT", body:JSON.stringify(body)});
      renderIntegrationPolicy();
      applyIntegrationPolicyVisibility();
      await loadDbtProjects();
      notify("Organization integration policy saved.", true);
    } catch (error) { notify(error.message); }
  });
  $("#meaning-source").addEventListener("change", () => loadBusinessMeaning().catch(error => notify(error.message)));
  $("#run-semantic-inference").addEventListener("click", async event => {
    const sourceId = $("#meaning-source").value;
    if (!sourceId) return notify("Select a data source first.");
    const button = event.currentTarget; button.disabled = true; button.textContent = "Inferring metadata";
    try {
      const run = await api(`/v1/datasources/${sourceId}/semantic-inference-runs`, {method:"POST", body:JSON.stringify({max_tables:100, use_model:true})});
      notify(`Created ${run.proposal_count} governed proposals using ${human(run.engine_mode)}.`, true);
      await loadOrganizationData();
    } catch (error) { notify(error.message); }
    finally { button.disabled = false; button.textContent = "Infer business meaning"; }
  });
  $("#new-dbt-project").addEventListener("click", () => {
    if (!dbtEnabled()) return notify("Enable dbt in Administration before registering projects.");
    populateProjectSources("dbt-source", $("#transform-project").value);
    $("#dbt-project-dialog").showModal();
  });
  $("#import-dbt-manifest").addEventListener("click", () => {
    if (!dbtEnabled()) return notify("Enable dbt in Administration before importing manifests.");
    return state.dbtProjects.length ? $("#dbt-import-dialog").showModal() : notify("Register a dbt project before importing a manifest.");
  });
  $$("[data-dbt-dag-mode]").forEach(btn => {
    btn.addEventListener("click", event => {
      state.dbtDagMode = event.currentTarget.dataset.dbtDagMode;
      renderDbtArtifact();
    });
  });
  $("#dbt-dag-zoom-in")?.addEventListener("click", () => state.dbtGraphEngine?.zoomBy(1.25));
  $("#dbt-dag-zoom-out")?.addEventListener("click", () => state.dbtGraphEngine?.zoomBy(1 / 1.25));
  $("#dbt-dag-zoom-fit")?.addEventListener("click", () => state.dbtGraphEngine?.fit());
  $("#dbt-dag-search")?.addEventListener("input", event => {
    state.dbtDagSearch = event.target.value;
    if (state.dbtDagMode === "dag" && state.dbtGraphEngine) state.dbtGraphEngine.applySearch(state.dbtDagSearch);
    else renderDbtArtifact();
  });
  $("#dbt-resource-type").addEventListener("change", renderDbtArtifact); $("#dbt-match-filter").addEventListener("change", renderDbtArtifact);
  $("#refresh-dbt").addEventListener("click", () => loadDbtProjects().then(() => notify("Transformation evidence refreshed.", true)).catch(error => notify(error.message)));
  $("#tools-project").addEventListener("change", async event => { populateProjectSources("tool-author-source", event.target.value); await loadTools(); });
  $("#tool-status-filter").addEventListener("change", renderToolList); $("#new-tool-button").addEventListener("click", () => openToolAuthor());
  $("#new-semantic-button").addEventListener("click", () => $("#semantic-dialog").showModal());
  $("#metric-source").addEventListener("change", loadMetricTables); $("#metric-table").addEventListener("change", loadMetricColumns);
  $("#add-tool-parameter").addEventListener("click", () => addToolParameter());
  $("#relationship-source").addEventListener("change", () => loadRelationships().catch(error => notify(error.message))); $("#graph-edge-filter").addEventListener("change", renderGraph);
  $("#graph-search-form").addEventListener("submit", event => { event.preventDefault(); searchGraph().catch(error => notify(error.message)); });
  ["graph-depth","graph-direction"].forEach(id => $("#"+id).addEventListener("change", () => { const focusId = state.graph?.focus_node_id; if (focusId) loadRelationships({focusId}).catch(error => notify(error.message)); }));
  $("#graph-overview").addEventListener("click", () => { state.graphFocusHistory = []; loadRelationships().catch(error => notify(error.message)); });
  $("#graph-back").addEventListener("click", () => { const focusId = state.graphFocusHistory.pop(); if (focusId && focusId !== "OVERVIEW") loadRelationships({focusId}).catch(error => notify(error.message)); else loadRelationships().catch(error => notify(error.message)); });
  $("#graph-stage")?.addEventListener("keydown", event => { if (!["+","-","0"].includes(event.key)) return; event.preventDefault(); if (event.key === "0") knowledgeGraphEngine?.fit(); else knowledgeGraphEngine?.zoomBy(event.key === "+" ? 1.25 : 1 / 1.25); });
  $("#schedule-source").addEventListener("change", loadSchedule); $("#run-status-filter").addEventListener("change", () => renderRuns("runs-table", 500)); $("#run-source-filter").addEventListener("change", () => renderRuns("runs-table", 500));
  $("#certification-source").addEventListener("change", () => loadEnterpriseIngestion().catch(error => notify(error.message)));
  $("#ingestion-source").addEventListener("change", () => loadEnterpriseIngestion().catch(error => notify(error.message)));
  $("#batch-source").addEventListener("change", () => { state.selectedBatchId = null; loadEnterpriseIngestion().catch(error => notify(error.message)); });
  $("#batch-select").addEventListener("change", async event => { state.selectedBatchId = event.target.value || null; state.metadataBatchChunks = state.selectedBatchId ? await fetchAll(`/v1/metadata-ingestion-batches/${state.selectedBatchId}/chunks`) : []; renderEnterpriseIngestion(); });
  $("#refresh-ingestions").addEventListener("click", () => loadEnterpriseIngestion().then(() => notify("Ingestion evidence refreshed.", true)).catch(error => notify(error.message)));
  $("#refresh-batches").addEventListener("click", () => loadEnterpriseIngestion().then(() => notify("Durable batch evidence refreshed.", true)).catch(error => notify(error.message)));
  $("#run-connector-certification").addEventListener("click", async event => {
    const sourceId = $("#certification-source").value;
    if (!sourceId) return notify("Select a data source to certify.");
    const button = event.currentTarget; button.disabled = true; button.textContent = "Evaluating controls";
    try {
      const result = await api(`/v1/datasources/${sourceId}/connector-certifications`, {method:"POST"});
      notify(`Connector certification completed: ${human(result.status)} at ${result.score}/100.`, result.status === "CERTIFIED");
      await loadEnterpriseIngestion();
    } catch (error) { notify(error.message); }
    finally { button.disabled = false; button.textContent = "Run certification"; }
  });
  $("#quality-source").addEventListener("change", () => loadQuality().catch(error => notify(error.message))); $("#refresh-quality").addEventListener("click", () => loadQuality().then(() => notify("Quality evidence refreshed.", true)).catch(error => notify(error.message)));
  $("#quality-incident-status").addEventListener("change", renderQuality); $("#quality-observation-status").addEventListener("change", renderQuality);
  $("#review-type-filter").addEventListener("change", renderGovernance); $("#review-maker-filter").addEventListener("input", renderGovernance); $("#refresh-reviews").addEventListener("click", loadOrganizationData);
  $("#refresh-audit").addEventListener("click", () => loadAudit().catch(error => notify(error.message))); $("#refresh-memory").addEventListener("click", () => loadMemory().catch(error => notify(error.message))); $("#memory-status").addEventListener("change", () => loadMemory().catch(error => notify(error.message))); $("#memory-source").addEventListener("change", () => loadMemory().catch(error => notify(error.message)));
  $("#refresh-outbox").addEventListener("click", () => loadOutbox().catch(error => notify(error.message)));
  $("#decision-form").addEventListener("submit", event => { event.preventDefault(); submitDecision(); });
  $("#quality-transition-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.target; const data = new FormData(form); const incidentId = state.pendingQualityIncidentId;
    if (!incidentId) return;
    try { await api(`/v1/quality-incidents/${incidentId}/transition`, {method:"POST", body:JSON.stringify({status:data.get("status"), reason:data.get("reason")})}); $("#quality-transition-dialog").close(); form.reset(); notify("Quality incident lifecycle updated with audit evidence.", true); await loadQuality(); } catch (error) { notify(error.message); }
  });
  $("#tool-form").addEventListener("submit", event => { event.preventDefault(); executeSelectedTool(event.target); });
  window.addEventListener("resize", () => window.requestAnimationFrame(() => { knowledgeGraphEngine?.resizeAndFit(); state.dbtGraphEngine?.resizeAndFit(); }));

  $("#semantic-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.target; const data = new FormData(form); const projectId = $("#semantic-project").value;
    try { await api(`/v1/projects/${projectId}/semantic-model-versions`, {method:"POST", body:JSON.stringify({name:data.get("name"), change_summary:data.get("change_summary")})}); $("#semantic-dialog").close(); form.reset(); notify("Semantic draft created.", true); await loadOrganizationData(); } catch (error) { notify(error.message); }
  });
  $("#clone-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target); const model = state.selectedSemantic; if (!model) return;
    try { await api(`/v1/semantic-model-versions/${model.id}/clone`, {method:"POST", body:JSON.stringify({name:data.get("name"), change_summary:data.get("change_summary")})}); $("#clone-dialog").close(); event.target.reset(); notify("Cloned semantic draft created with metric versions.", true); await loadOrganizationData(); } catch (error) { notify(error.message); }
  });
  $("#metric-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target); const model = state.selectedSemantic; if (!model) return;
    const body = {slug:data.get("slug"), name:data.get("name"), description:data.get("description"), aggregation:data.get("aggregation"), grain:data.get("grain"), source_table_id:data.get("source_table_id"), measure_column_id:data.get("measure_column_id") || null, default_time_column_id:data.get("default_time_column_id") || null, allowed_dimension_column_ids:data.getAll("allowed_dimension_column_ids")};
    try { await api(`/v1/semantic-model-versions/${model.id}/metrics`, {method:"POST", body:JSON.stringify(body)}); $("#metric-dialog").close(); event.target.reset(); notify("Metric added to the governed draft.", true); await loadOrganizationData(); } catch (error) { notify(error.message); }
  });
  $("#tool-author-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target); const projectId = $("#tools-project").value;
    const body = {slug:data.get("slug"), name:data.get("name"), description:data.get("description"), datasource_id:data.get("datasource_id"), semantic_model_version_id:null, sql_template:data.get("sql_template"), parameters:collectToolParameters(), allowed_roles:String(data.get("allowed_roles")).split(",").map(value => value.trim()).filter(Boolean)};
    try { await api(`/v1/projects/${projectId}/tools`, {method:"POST", body:JSON.stringify(body)}); $("#tool-dialog").close(); notify("Governed tool draft created and SQL contract validated.", true); await loadOrganizationData(); } catch (error) { notify(error.message); }
  });
  $("#dbt-project-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.target; const data = new FormData(form); const projectId = $("#transform-project").value;
    const body = {project_key:data.get("project_key"), display_name:data.get("display_name"), datasource_id:data.get("datasource_id"), repository_url:String(data.get("repository_url") || "").trim() || null, target_name:data.get("target_name")};
    try { const created = await api(`/v1/projects/${projectId}/dbt-projects`, {method:"POST", body:JSON.stringify(body)}); state.selectedDbtProjectId = created.id; $("#dbt-project-dialog").close(); form.reset(); notify("dbt project registered to its governed warehouse source.", true); await loadDbtProjects(); } catch (error) { notify(error.message); }
  });
  $("#dbt-import-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.target; const data = new FormData(form);
    const manifestFile = $("#dbt-manifest-file")?.files[0];
    const catalogFile = $("#dbt-catalog-file")?.files[0];
    const runResultsFile = $("#dbt-run-results-file")?.files[0];
    if (!manifestFile) return notify("Choose a dbt manifest.json file.");
    if (manifestFile.size > 32 * 1024 * 1024) return notify("The manifest exceeds the 32 MiB ingestion limit.");
    const button = form.querySelector("button[type=submit]"); button.disabled = true; button.textContent = "Validating artifact";
    try {
      const manifest = JSON.parse(await manifestFile.text());
      let catalog = null;
      if (catalogFile) {
        try { catalog = JSON.parse(await catalogFile.text()); }
        catch { return notify("The catalog.json file is not valid JSON."); }
      }
      let runResults = null;
      if (runResultsFile) {
        try { runResults = JSON.parse(await runResultsFile.text()); }
        catch { return notify("The run_results.json file is not valid JSON."); }
      }
      const imported = await api(`/v1/dbt-projects/${data.get("dbt_project_id")}/artifact-imports`, {
        method: "POST",
        body: JSON.stringify({
          manifest,
          catalog,
          run_results: runResults
        })
      });
      state.selectedDbtProjectId = String(data.get("dbt_project_id"));
      state.selectedDbtImportId = imported.id;
      $("#dbt-import-dialog").close();
      form.reset();
      notify(`Imported ${imported.resource_count} dbt resources, ${imported.lineage_edge_count} lineage edges, and associated catalog/test metadata.`, true);
      await loadDbtProjects();
    } catch (error) {
      notify(error instanceof SyntaxError ? "The selected file is not valid JSON." : error.message);
    } finally {
      button.disabled = false; button.textContent = "Validate and import";
    }
  });
  $("#schedule-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.target; const data = new FormData(form); const sourceId = $("#schedule-source").value;
    const body = {enabled:form.elements.enabled.checked, interval_minutes:Number(data.get("interval_minutes")), mode:data.get("mode"), priority:Number(data.get("priority")), maintenance_start_hour_utc:asNumberOrNull(data.get("maintenance_start_hour_utc")), maintenance_end_hour_utc:asNumberOrNull(data.get("maintenance_end_hour_utc")), start_at:null};
    await refreshAfter(() => api(`/v1/datasources/${sourceId}/scan-policy`, {method:"PUT", body:JSON.stringify(body)}), "Durable scan policy saved.");
  });
  $("#metadata-ingestion-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.target; const data = new FormData(form); const sourceId = data.get("datasource_id");
    if (!sourceId) return notify("Select a data source for this metadata delivery.");
    let catalogs;
    try { catalogs = JSON.parse(String(data.get("catalogs"))); }
    catch { return notify("Catalog payload must be valid JSON."); }
    if (!Array.isArray(catalogs) || !catalogs.length) return notify("Catalog payload must be a non-empty JSON array.");
    if (data.get("snapshot_type") === "FULL" && !window.confirm("A full snapshot retires active metadata omitted from this payload. Continue?")) return;
    const body = {envelope_version:"1.0", idempotency_key:data.get("idempotency_key"), producer:data.get("producer"), transport:data.get("transport"), snapshot_type:data.get("snapshot_type"), emitted_at:new Date().toISOString(), catalogs};
    const button = form.querySelector("button[type=submit]"); button.disabled = true; button.textContent = "Validating contract";
    try {
      const result = await api(`/v1/datasources/${sourceId}/metadata-ingestions`, {method:"POST", body:JSON.stringify(body)});
      form.elements.idempotency_key.value = `ui:${new Date().toISOString().replaceAll(/[-:.TZ]/g, "").slice(0, 14)}:${crypto.randomUUID().slice(0, 8)}`;
      notify(`Metadata ingestion completed: ${result.object_counts.tables || 0} tables and ${result.object_counts.columns || 0} columns.`, true);
      await loadOrganizationData();
    } catch (error) { notify(error.message); }
    finally { button.disabled = false; button.textContent = "Validate and ingest"; }
  });
  $("#batch-create-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.target; const data = new FormData(form); const sourceId = data.get("datasource_id");
    if (!sourceId) return notify("Select a data source for this batch.");
    const body = {envelope_version:"1.0", batch_key:data.get("batch_key"), producer:data.get("producer"), snapshot_type:data.get("snapshot_type"), expected_chunks:Number(data.get("expected_chunks"))};
    const button = form.querySelector("button[type=submit]"); button.disabled = true; button.textContent = "Creating manifest";
    try { const batch = await api(`/v1/datasources/${sourceId}/metadata-ingestion-batches`, {method:"POST", body:JSON.stringify(body)}); state.selectedBatchId = batch.id; notify("Durable batch manifest created. Upload every numbered chunk before finalizing.", true); await loadEnterpriseIngestion(); }
    catch (error) { notify(error.message); }
    finally { button.disabled = false; button.textContent = "Create batch"; }
  });
  $("#batch-chunk-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.target; const data = new FormData(form); const batchId = data.get("batch_id");
    if (!batchId) return notify("Select an open batch.");
    let catalogs; try { catalogs = JSON.parse(String(data.get("catalogs"))); } catch { return notify("Chunk catalog payload must be valid JSON."); }
    if (!Array.isArray(catalogs) || !catalogs.length) return notify("Chunk payload must be a non-empty JSON array.");
    const body = {chunk_number:Number(data.get("chunk_number")), chunk_key:data.get("chunk_key"), emitted_at:new Date().toISOString(), catalogs};
    const button = form.querySelector("button[type=submit]"); button.disabled = true; button.textContent = "Checksumming chunk";
    try { const chunk = await api(`/v1/metadata-ingestion-batches/${batchId}/chunks`, {method:"POST", body:JSON.stringify(body)}); state.selectedBatchId = batchId; form.elements.chunk_number.value = String(chunk.chunk_number + 1); form.elements.chunk_key.value = ""; notify(`Chunk ${chunk.chunk_number} accepted with checksum evidence.`, true); await loadEnterpriseIngestion(); }
    catch (error) { notify(error.message); }
    finally { button.disabled = false; button.textContent = "Upload chunk"; }
  });
  $("#finalize-batch").addEventListener("click", async event => {
    const batchId = $("#batch-select").value; const batch = state.metadataBatches.find(item => item.id === batchId);
    if (!batch) return notify("Select an open batch to finalize.");
    if (batch.received_chunks !== batch.expected_chunks) return notify(`Upload all ${batch.expected_chunks} chunks before finalizing.`);
    if (batch.snapshot_type === "FULL" && !window.confirm("This full batch retires active metadata omitted from all chunks, but only after every chunk succeeds. Continue?")) return;
    const button = event.currentTarget; button.disabled = true; button.textContent = "Submitting workflow";
    try {
      let result = await api(`/v1/metadata-ingestion-batches/${batchId}/finalize`, {method:"POST"});
      notify("Batch queued in the durable workflow engine.", true);
      for (let attempt=0; attempt<120 && ["QUEUED","PROCESSING"].includes(result.status); attempt++) { await new Promise(resolve => window.setTimeout(resolve, 1000)); result = await api(`/v1/metadata-ingestion-batches/${batchId}`); if (attempt % 3 === 0) { state.selectedBatchId = batchId; await loadEnterpriseIngestion(); } }
      state.selectedBatchId = batchId; await loadOrganizationData();
      notify(result.status === "COMPLETED" ? `Batch completed: ${result.object_counts.tables || 0} tables and ${result.object_counts.columns || 0} columns.` : `Batch finished with status ${human(result.status)}.`, result.status === "COMPLETED");
    } catch (error) { notify(error.message); }
    finally { button.disabled = false; button.textContent = "Finalize and process"; }
  });
  $("#quality-policy-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.target; const data = new FormData(form); const sourceId = $("#quality-source").value;
    const body = {table_id:null, name:data.get("name"), enabled:form.elements.enabled.checked, volume_change_percent:Number(data.get("volume_change_percent")), null_rate_change_percent:Number(data.get("null_rate_change_percent")), schema_change_enabled:form.elements.schema_change_enabled.checked, metadata_scan_max_age_minutes:Number(data.get("metadata_scan_max_age_minutes"))};
    try { await api(`/v1/datasources/${sourceId}/quality-policies`, {method:"PUT", body:JSON.stringify(body)}); notify("Quality baseline policy saved with audit evidence.", true); await loadQuality(); } catch (error) { notify(error.message); }
  });
  $("#model-route-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.target; const data = new FormData(form); const reference = String(data.get("credential_reference") || "").trim();
    const body = {route_key:data.get("route_key"), display_name:data.get("display_name"), provider_type:data.get("provider_type"), model_id:data.get("model_id"), endpoint_alias:data.get("endpoint_alias"), credential_reference:reference || null, data_residency:data.get("data_residency"), retention_policy:data.get("retention_policy"), capabilities:data.getAll("capabilities"), max_input_tokens:Number(data.get("max_input_tokens")), max_output_tokens:Number(data.get("max_output_tokens")), timeout_seconds:Number(data.get("timeout_seconds"))};
    await refreshAfter(() => api(`/v1/organizations/${state.organizationId}/model-routes`, {method:"POST", body:JSON.stringify(body)}), "Governed model route draft created."); form.reset();
  });
  $("#organization-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target);
    try { const created = await api("/v1/organizations", {method:"POST", body:JSON.stringify({name:data.get("name"), slug:data.get("slug")})}); await loadOrganizations(created.id); localStorage.setItem("aida-organization", created.id); event.target.reset(); notify("Organization created and selected.", true); await loadOrganizationData(); } catch (error) { notify(error.message); }
  });
  $("#lob-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target); const orgId = data.get("organization_id");
    await refreshAfter(() => api(`/v1/organizations/${orgId}/lines-of-business`, {method:"POST", body:JSON.stringify({name:data.get("name"), code:data.get("code")})}), "Line of business created."); event.target.reset();
  });
  $("#project-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target);
    await refreshAfter(() => api(`/v1/lines-of-business/${data.get("lob_id")}/projects`, {method:"POST", body:JSON.stringify({name:data.get("name"), slug:data.get("slug")})}), "Project created."); event.target.reset();
  });
  $("#datasource-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.target); const projectId = data.get("project_id");
    const connectorType = data.get("connector_type") === "postgresql" ? "postgres" : data.get("connector_type");
    const body = {name:data.get("name"), connector_type:connectorType, dialect:data.get("dialect"), environment:data.get("environment"), network_zone:data.get("network_zone"), credential_reference:data.get("credential_reference"), max_concurrency:Number(data.get("max_concurrency"))};
    try { const source = await api(`/v1/projects/${projectId}/datasources`, {method:"POST", body:JSON.stringify(body)}); await api(`/v1/datasources/${source.id}/test`, {method:"POST"}); event.target.reset(); notify("Source registered and connectivity verified.", true); await loadOrganizationData(); } catch (error) { notify(error.message); }
  });
}

function bindDelegatedEvents() {
  document.addEventListener("click", async event => {
    const go = event.target.closest("[data-go]"); if (go) return showView(go.dataset.go);
    const nav = event.target.closest("[data-view]"); if (nav) return showView(nav.dataset.view);
    const assetTab = event.target.closest("[data-asset-tab]"); if (assetTab) { state.selectedAssetTab = assetTab.dataset.assetTab; $$("[data-asset-tab]").forEach(node => { const active = node === assetTab; node.classList.toggle("active", active); node.setAttribute("aria-selected", String(active)); node.tabIndex = active ? 0 : -1; }); $$("[data-asset-pane]").forEach(node => node.classList.toggle("active", node.dataset.assetPane === state.selectedAssetTab)); return; }
    const submitTerm = event.target.closest("[data-submit-term]"); if (submitTerm) return refreshAfter(() => api(`/v1/glossary-term-versions/${submitTerm.dataset.submitTerm}/submit`, {method:"POST"}), "Glossary term submitted for independent review.");
    const deprecateTerm = event.target.closest("[data-deprecate-term]"); if (deprecateTerm) return openBulkStewardshipDialog("DEPRECATE_TERM", "TERM", [deprecateTerm.dataset.deprecateTerm]);
    const submitLinkProposal = event.target.closest("[data-submit-link-proposal]"); if (submitLinkProposal) return refreshAfter(() => api(`/v1/glossary-link-proposals/${submitLinkProposal.dataset.submitLinkProposal}/submit`, {method:"POST"}), "Inferred glossary link submitted for independent review.");
    const resolveConflict = event.target.closest("[data-resolve-conflict]"); if (resolveConflict) { const form = $("#conflict-resolution-form"); form.reset(); form.elements.conflict_id.value = resolveConflict.dataset.resolveConflict; return $("#conflict-resolution-dialog").showModal(); }
    const applyOwnershipRule = event.target.closest("[data-apply-ownership-rule]"); if (applyOwnershipRule) return refreshAfter(() => api(`/v1/ownership-rules/${applyOwnershipRule.dataset.applyOwnershipRule}/apply`, {method:"POST"}), "Ownership rule matched a bounded asset set and was submitted for review.");
    const assignAssetOwner = event.target.closest("[data-assign-asset-owner]"); if (assignAssetOwner) return openBulkStewardshipDialog("ASSIGN_OWNERSHIP", "TABLE", [assignAssetOwner.dataset.assignAssetOwner]);
    const certifyAsset = event.target.closest("[data-certify-asset]"); if (certifyAsset) return openBulkStewardshipDialog("CERTIFY_ASSET", "TABLE", [certifyAsset.dataset.certifyAsset]);
    const editDocumentation = event.target.closest("[data-edit-documentation]"); if (editDocumentation) return openAssetDocumentationDialog();
    const submitDocumentation = event.target.closest("[data-submit-documentation]"); if (submitDocumentation) return refreshAfter(() => api(`/v1/asset-documentation-versions/${submitDocumentation.dataset.submitDocumentation}/submit`, {method:"POST"}), "Asset documentation submitted for independent review.");
    const linkTerm = event.target.closest("[data-link-term]"); if (linkTerm) return openAssetTermDialog();
    const removeTerm = event.target.closest("[data-remove-term]"); if (removeTerm) { if (!window.confirm("Remove this glossary term link from the asset?")) return; try { await api(`/v1/asset-term-links/${removeTerm.dataset.removeTerm}`, {method:"DELETE"}); notify("Glossary term unlinked.", true); return showTable(state.selectedTableId); } catch (error) { return notify(error.message); } }
    const paletteRow = event.target.closest("[data-palette-index]"); if (paletteRow) { state.paletteActiveIndex = Number(paletteRow.dataset.paletteIndex); return activatePaletteSelection(); }
    const close = event.target.closest("[data-close-dialog]"); if (close) return document.getElementById(close.dataset.closeDialog).close();
    const graphNode = event.target.closest("[data-graph-node]"); if (graphNode) return selectGraphNode(graphNode.dataset.graphNode).catch(error => notify(error.message));
    const graphSearchNode = event.target.closest("[data-graph-search-node]"); if (graphSearchNode) { $("#graph-search-results").classList.remove("active"); return loadRelationships({focusId:graphSearchNode.dataset.graphSearchNode, pushHistory:true}).catch(error => notify(error.message)); }
    const graphFocus = event.target.closest("[data-graph-focus]"); if (graphFocus) return loadRelationships({focusId:graphFocus.dataset.graphFocus, pushHistory:true}).catch(error => notify(error.message));
    const graphCatalog = event.target.closest("[data-graph-catalog]"); if (graphCatalog) { $("#catalog-source").value = state.graphDatasourceId; await loadTables(); showView("catalog"); return showTable(graphCatalog.dataset.graphCatalog); }
    const tableNode = event.target.closest("[data-table]"); if (tableNode) return showTable(tableNode.dataset.table);
    const model = event.target.closest("[data-model]"); if (model) return selectSemantic(model.dataset.model);
    const tool = event.target.closest("[data-tool]"); if (tool) return selectTool(tool.dataset.tool);
    const dbtProject = event.target.closest("[data-dbt-project]"); if (dbtProject) return selectDbtProject(dbtProject.dataset.dbtProject).catch(error => notify(error.message));
    const dbtImport = event.target.closest("[data-dbt-import]"); if (dbtImport) return loadDbtArtifact(dbtImport.dataset.dbtImport).catch(error => notify(error.message));
    const toggleCols = event.target.closest("[data-toggle-dbt-columns]");
    if (toggleCols) {
      const nodeId = toggleCols.dataset.toggleDbtColumns;
      if (state.dbtDagExpandedNodes.has(nodeId)) state.dbtDagExpandedNodes.delete(nodeId);
      else state.dbtDagExpandedNodes.add(nodeId);
      if (state.dbtDagMode === "dag" && state.dbtGraphEngine) { state.dbtGraphEngine.updateNodeData(nodeId, {expanded: state.dbtDagExpandedNodes.has(nodeId)}); return; }
      return renderDbtArtifact();
    }
    const dagNode = event.target.closest("[data-dbt-dag-node]");
    if (dagNode) {
      const nodeId = dagNode.dataset.dbtDagNode;
      state.dbtDagSelectedNodeId = state.dbtDagSelectedNodeId === nodeId ? null : nodeId;
      showDbtResource(nodeId);
      return renderDbtArtifact();
    }
    const dbtResource = event.target.closest("[data-dbt-resource]"); if (dbtResource) return showDbtResource(dbtResource.dataset.dbtResource);
    const proposalDetail = event.target.closest("[data-proposal-detail]"); if (proposalDetail) return showRecord("Business metadata proposal", state.enrichmentProposals.find(item => item.id === proposalDetail.dataset.proposalDetail));
    const annotationDetail = event.target.closest("[data-annotation-detail]"); if (annotationDetail) return showRecord("Approved business annotation", state.businessAnnotations.find(item => item.id === annotationDetail.dataset.annotationDetail));
    const promoteBlueprint = event.target.closest("[data-promote-blueprint]"); if (promoteBlueprint) return refreshAfter(() => api(`/v1/metadata-enrichment-proposals/${promoteBlueprint.dataset.promoteBlueprint}/promote-tool`, {method:"POST"}), "Approved blueprint promoted to a governed tool draft. Submit it separately for publication review.");
    const addMetric = event.target.closest("[data-add-metric]"); if (addMetric) return prepareMetricComposer();
    const clone = event.target.closest("[data-clone-model]"); if (clone) { $("#clone-form").elements.name.value = `${state.selectedSemantic.name} working copy`; return $("#clone-dialog").showModal(); }
    const submitModel = event.target.closest("[data-submit-model]"); if (submitModel) return refreshAfter(() => api(`/v1/semantic-model-versions/${submitModel.dataset.submitModel}/submit`, {method:"POST"}), "Semantic model submitted for independent review.");
    const submitTool = event.target.closest("[data-submit-tool]"); if (submitTool) return refreshAfter(() => api(`/v1/tool-versions/${submitTool.dataset.submitTool}/submit`, {method:"POST"}), "Tool version submitted for independent review.");
    const deprecate = event.target.closest("[data-deprecate-tool]"); if (deprecate) return refreshAfter(() => api(`/v1/tool-versions/${deprecate.dataset.deprecateTool}/deprecation-submit`, {method:"POST"}), "Tool deprecation submitted for independent review.");
    const newVersion = event.target.closest("[data-new-version]"); if (newVersion) return openToolAuthor(state.tools.find(item => item.id === newVersion.dataset.newVersion));
    const removeParameter = event.target.closest("[data-remove-parameter]"); if (removeParameter) return removeParameter.closest(".parameter-row").remove();
    const review = event.target.closest("[data-review]"); if (review) return openDecision("governance", review.dataset.review);
    const relationship = event.target.closest("[data-relationship]"); if (relationship) return openDecision("relationship", relationship.dataset.relationship, relationship.dataset.decision);
    const record = event.target.closest("[data-record]"); if (record) return showRecord(record.dataset.recordTitle || "Record detail", JSON.parse(record.dataset.record));
    const runDetail = event.target.closest("[data-run-detail]"); if (runDetail) return showRecord("Analysis run evidence", state.runs.find(item => item.id === runDetail.dataset.runDetail));
    const auditDetail = event.target.closest("[data-audit-detail]"); if (auditDetail) return showRecord("Audit event", state.audit.find(item => String(item.id) === auditDetail.dataset.auditDetail));
    const evaluation = event.target.closest("[data-evaluation]"); if (evaluation) return showRecord("Evaluation findings", state.evaluations.find(item => item.id === evaluation.dataset.evaluation));
    const trace = event.target.closest("[data-trace]"); if (trace) { const run = await api(`/v1/agent-runs/${trace.dataset.trace}`); renderTrace(run.step_trace); showView("analyst"); return; }
    const qualityDetail = event.target.closest("[data-quality-detail]"); if (qualityDetail) { const records = qualityDetail.dataset.qualityKind === "incident" ? state.qualityIncidents : state.qualityObservations; return showRecord(qualityDetail.dataset.qualityKind === "incident" ? "Quality incident evidence" : "Quality observation evidence", records.find(item => item.id === qualityDetail.dataset.qualityDetail)); }
    const certificationDetail = event.target.closest("[data-certification-detail]"); if (certificationDetail) return showRecord("Connector certification evidence", state.connectorCertifications.find(item => item.id === certificationDetail.dataset.certificationDetail));
    const ingestionDetail = event.target.closest("[data-ingestion-detail]"); if (ingestionDetail) return showRecord("Metadata ingestion evidence", state.metadataIngestions.find(item => item.id === ingestionDetail.dataset.ingestionDetail));
    const batchRow = event.target.closest("[data-batch-select-row]"); if (batchRow) { state.selectedBatchId = batchRow.dataset.batchSelectRow; state.metadataBatchChunks = await fetchAll(`/v1/metadata-ingestion-batches/${state.selectedBatchId}/chunks`); renderEnterpriseIngestion(); return showRecord("Durable batch evidence", state.metadataBatches.find(item => item.id === state.selectedBatchId)); }
    const chunkDetail = event.target.closest("[data-chunk-detail]"); if (chunkDetail) return showRecord("Checksum-addressed chunk evidence", state.metadataBatchChunks.find(item => item.id === chunkDetail.dataset.chunkDetail));
    const qualityTransition = event.target.closest("[data-quality-transition]"); if (qualityTransition) { const incident = state.qualityIncidents.find(item => item.id === qualityTransition.dataset.qualityTransition); state.pendingQualityIncidentId = incident.id; const form = $("#quality-transition-form"); form.elements.status.value = qualityTransition.dataset.qualityStatus; form.elements.reason.value = ""; setHtml("quality-transition-context", `<strong>${esc(incident.table_name)} / ${esc(human(incident.anomaly_type))}</strong><span>${esc(incident.summary)} Last observed ${when(incident.last_observed_at)}.</span>`); return $("#quality-transition-dialog").showModal(); }
    const feedback = event.target.closest("[data-feedback]"); if (feedback) return refreshAfter(() => api(`/v1/agent-runs/${feedback.dataset.feedback}/feedback`, {method:"PUT", body:JSON.stringify({rating:feedback.dataset.rating, comment:null})}), "Feedback recorded without retaining raw comments.");
    const sourceTest = event.target.closest("[data-test-source]"); if (sourceTest) return refreshAfter(() => api(`/v1/datasources/${sourceTest.dataset.testSource}/test`, {method:"POST"}), "Source connectivity verified.");
    const scan = event.target.closest("[data-scan]"); if (scan) return refreshAfter(() => api(`/v1/datasources/${scan.dataset.scan}/analysis-runs`, {method:"POST", body:JSON.stringify({mode:"INCREMENTAL"})}), "Durable metadata run submitted.");
    const toggle = event.target.closest("[data-toggle]"); if (toggle) { const enabling = toggle.dataset.enabled === "true"; if (!enabling && !window.confirm("Disable this source? Scheduled scans and governed analysis against it will stop until it is re-enabled.")) return; return refreshAfter(() => api(`/v1/datasources/${toggle.dataset.toggle}`, {method:"PATCH", body:JSON.stringify({enabled:enabling})}), `Source ${enabling ? "enabled" : "disabled"}.`); }
    const cancel = event.target.closest("[data-cancel-run]"); if (cancel) { if (!window.confirm("Cancel this running analysis?")) return; return refreshAfter(() => api(`/v1/analysis-runs/${cancel.dataset.cancelRun}/cancel`, {method:"POST"}), "Cancellation requested."); }
    const resume = event.target.closest("[data-resume-run]"); if (resume) return refreshAfter(() => api(`/v1/analysis-runs/${resume.dataset.resumeRun}/resume`, {method:"POST"}), "Replacement run submitted from prior scope.");
    const route = event.target.closest("[data-submit-route]"); if (route) return refreshAfter(() => api(`/v1/model-routes/${route.dataset.submitRoute}/submit`, {method:"POST"}), "Model route submitted for independent review.");
    const requeue = event.target.closest("[data-requeue]"); if (requeue) return refreshAfter(() => api(`/v1/outbox-events/${requeue.dataset.requeue}/requeue`, {method:"POST"}), "Dead-letter event requeued with audit evidence.");
  });
  $("#discover-relationships").addEventListener("click", () => { const id = $("#relationship-source").value; return refreshAfter(() => api(`/v1/datasources/${id}/relationship-candidates/discover`, {method:"POST", body:JSON.stringify({max_candidates:500})}), "Relationship discovery completed with value-free evidence."); });
  $("#run-evaluation").addEventListener("click", () => refreshAfter(() => api(`/v1/organizations/${state.organizationId}/agent-evaluations`, {method:"POST"}), "Agent control evaluation completed."));
  $$("[data-ops-tab]").forEach(button => button.addEventListener("click", () => { $$("[data-ops-tab]").forEach(item => { const active = item === button; item.classList.toggle("active", active); item.setAttribute("aria-selected", String(active)); item.tabIndex = active ? 0 : -1; }); $$(".ops-pane").forEach(pane => pane.classList.toggle("active", pane.id === `ops-${button.dataset.opsTab}`)); if (button.dataset.opsTab === "memory") loadMemory().catch(error => notify(error.message)); if (button.dataset.opsTab === "outbox") loadOutbox().catch(error => notify(error.message)); }));
}

async function initialize() {
  state.persona = localStorage.getItem("aida-persona") || "all";
  $("#persona-select").value = state.persona;
  applyPersona();
  prepareAccessibility();
  bindDirectEvents(); bindDelegatedEvents(); bindPaletteEvents(); bindTabKeyboardNav();
  const ingestionForm = $("#metadata-ingestion-form");
  if (ingestionForm) {
    ingestionForm.elements.snapshot_type.value = "INCREMENTAL";
    ingestionForm.elements.idempotency_key.value = `ui:${new Date().toISOString().replaceAll(/[-:.TZ]/g, "").slice(0, 14)}:${crypto.randomUUID().slice(0, 8)}`;
  }
  const sourceForm = $("#datasource-form");
  if (sourceForm?.elements.connector_type?.options.length) {
    sourceForm.elements.connector_type.options[0].value = "postgres";
    sourceForm.elements.connector_type.options[0].textContent = "PostgreSQL (native pull)";
  }
  try {
    await loadOrganizations();
    if (state.organizationId) await loadOrganizationData(); else { renderHierarchy(); showView("administration"); }
    const requested = location.hash.slice(1); if (document.getElementById(`${requested}-view`)) showView(requested);
  } catch (error) { notify(`Atlas could not load: ${error.message}`); }
}

initialize();
