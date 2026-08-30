/* dbt and OpenLineage workbench rendering and data loading. */
(function initializeTransformationWorkbench() {
  const { state, $, setHtml, esc, when, human, badge, empty, selectOptions, preserveSelect, populateProjectSources, api, fetchAll, renderTable, integrationFlags, dbtEnabled, renderTransformationOverview, renderDbtDisabledState } = window.AtlasUI;

function renderDbtProjects() {
  const rows = state.dbtProjects.map(item => `<tr><td><button class="link-button" data-dbt-project="${item.id}">${esc(item.display_name)}</button><span class="secondary-cell">${esc(item.project_key)}</span></td><td>${badge(item.status)}</td><td>${esc(item.target_name)}</td><td>${esc(state.sources.find(source => source.id === item.datasource_id)?.name || item.datasource_id)}</td></tr>`);
  renderTable("dbt-projects-table", ["Project","Status","Target","Warehouse source"], rows, "No dbt projects registered for this delivery project");
  const options = selectOptions(state.dbtProjects, item => `${item.display_name} / ${item.target_name}`, state.dbtProjects.length ? "" : "No dbt projects");
  preserveSelect("dbt-import-project", options);
  if (state.selectedDbtProjectId && state.dbtProjects.some(item => item.id === state.selectedDbtProjectId)) $("#dbt-import-project").value = state.selectedDbtProjectId;
}

function renderOpenLineageHistory() {
  if (!integrationFlags().openlineage) {
    setHtml("openlineage-history", empty("OpenLineage is disabled", "Enable it in Administration to ingest runtime lineage events."));
    return;
  }
  const rows = state.openlineageEvents.map(item => `<tr><td><button class="link-button" data-openlineage-event="${item.id}">${esc(item.job_name)}</button><span class="secondary-cell">${esc(item.job_namespace)} / ${esc(item.run_id)}</span></td><td>${badge(item.event_type)}</td><td>${item.input_dataset_count} in / ${item.output_dataset_count} out</td><td>${item.table_edge_count} table / ${item.column_edge_count} column</td><td>${item.unresolved_dataset_count}</td><td>${when(item.event_time)}</td></tr>`);
  renderTable("openlineage-history", ["Job run","Event","Datasets","Edges","Unresolved","Observed"], rows, "No OpenLineage events have been ingested for this source");
}

function renderDbtImports() {
  const rows = state.dbtImports.map(item => `<tr><td><button class="link-button" data-dbt-import="${item.id}">${when(item.generated_at || item.created_at)}</button><span class="secondary-cell">dbt ${esc(item.dbt_version || "unknown")}</span></td><td>${badge(item.status)}</td><td>${item.model_count} / ${item.source_count} / ${item.test_count}</td><td>${item.lineage_edge_count}</td><td>${item.matched_resource_count} matched / ${item.unmatched_resource_count} open</td></tr>`);
  renderTable("dbt-imports-table", ["Artifact","Status","Models / sources / tests","Edges","Catalog coverage"], rows, "No manifest imports yet");
}

function dbtNodeHtml(data, meta) {
  const rType = (data.resource_type || "MODEL").toLowerCase().replace(/_/g, "-");
  const classes = ["atlas-node-card", `type-${rType}`];
  if (meta.selected) classes.push("is-selected");
  if (data.agMatch) classes.push("is-match");
  if (data.agDim) classes.push("is-dim");
  let statusPill = "";
  if (data.test_status) {
    const t = String(data.test_status).toLowerCase();
    statusPill = `<span class="ag-pill ${t === "pass" ? "pass" : t === "fail" ? "fail" : ""}">${esc(data.test_status)}${data.test_failures ? ` (${data.test_failures})` : ""}</span>`;
  } else if (data.matched_table_id) {
    statusPill = '<span class="ag-pill matched">Matched</span>';
  }
  const columnNames = data.column_names || [];
  const isExpanded = Boolean(data.expanded);
  const popover = (isExpanded && columnNames.length) ? `<div class="atlas-col-popover">${columnNames.slice(0, 40).map(name => `<div class="col-row"><span class="col-name">${esc(name)}</span>${data.column_types && data.column_types[name] ? `<span class="col-type">${esc(data.column_types[name])}</span>` : ""}</div>`).join("")}${columnNames.length > 40 ? `<div class="col-row"><span class="col-name">+${columnNames.length - 40} more</span></div>` : ""}</div>` : "";
  return `<div class="${classes.join(" ")}" data-dbt-dag-node="${data.id}">`
    + `<div class="ag-card-head"><span class="ag-type-tag">${esc(data.resource_type || "MODEL")}</span>${statusPill}</div>`
    + `<span class="ag-title">${esc(data.label || data.id)}</span>`
    + (data.materialization ? `<span class="ag-sub">${esc(data.materialization)}</span>` : "")
    + (columnNames.length ? `<button type="button" class="ag-expand" data-toggle-dbt-columns="${data.id}">${columnNames.length} columns ${isExpanded ? "\u25b2" : "\u25bc"}</button>` : "")
    + popover
    + `</div>`;
}

function dbtMatchNode(data, q) {
  if (String(data.label || "").toLowerCase().includes(q)) return true;
  if ((data.column_names || []).some(c => c.toLowerCase().includes(q))) return true;
  if ((data.tags || []).some(t => t.toLowerCase().includes(q))) return true;
  return false;
}

function renderDbtLineageDAG(artifact) {
  const nodes = state.dbtLineage?.nodes || [];
  const edges = state.dbtLineage?.edges || [];
  if (!nodes.length) {
    if (state.dbtGraphEngine) { try { state.dbtGraphEngine.destroy(); } catch (error) { /* already detached */ } state.dbtGraphEngine = null; }
    setHtml("dbt-lineage", empty("No lineage graph", "This artifact has no declared resources or dependency nodes."));
    return;
  }
  const engine = window.AtlasUI.AtlasGraph.mount("dbt-lineage", {
    direction: "LR", nodeSep: 30, rankSep: 150,
    nodeHtml: dbtNodeHtml, matchNode: dbtMatchNode,
    onNodeExpand: data => showDbtResource(data.id)
  }, state, "dbtGraphEngine");
  if (!engine) return;

  const resourceMap = new Map(state.dbtResources.map(r => [r.id, r]));
  const nodeIds = new Set(nodes.map(n => n.id));
  const cyNodes = nodes.map(node => {
    const resource = resourceMap.get(node.id) || {};
    return {
      id: node.id, w: 210, h: 98,
      data: {
        label: node.label, resource_type: node.resource_type, materialization: node.materialization,
        matched_table_id: node.matched_table_id, test_status: resource.test_status, test_failures: resource.test_failures,
        column_names: resource.column_names || [], column_types: resource.column_types || {}, tags: resource.tags || [],
        expanded: state.dbtDagExpandedNodes.has(node.id)
      }
    };
  });
  const cyEdges = edges.filter(edge => nodeIds.has(edge.source_resource_id) && nodeIds.has(edge.target_resource_id)).map(edge => ({
    id: `${edge.source_resource_id}->${edge.target_resource_id}`,
    source: edge.source_resource_id, target: edge.target_resource_id, classes: "dbt"
  }));

  engine.setData(cyNodes, cyEdges, {
    selectId: state.dbtDagSelectedNodeId,
    emptyHtml: empty("No dependency graph", "This artifact has no declared node dependencies.")
  });
  engine.applySearch(state.dbtDagSearch || "");
}

function renderDbtColumnFlows(artifact) {
  const resources = state.dbtResources || [];
  const edges = state.dbtLineage?.edges || [];
  const resMap = new Map(resources.map(r => [r.id, r]));
  const searchQuery = (state.dbtDagSearch || "").trim().toLowerCase();

  const flows = [];
  edges.slice(0, 100).forEach(edge => {
    const src = resMap.get(edge.source_resource_id);
    const tgt = resMap.get(edge.target_resource_id);
    if (!src || !tgt) return;
    const commonCols = src.column_names.filter(c => tgt.column_names.includes(c));
    if (!commonCols.length) {
      flows.push({
        srcName: src.name,
        srcType: src.resource_type,
        tgtName: tgt.name,
        tgtType: tgt.resource_type,
        srcCol: "(relation link)",
        tgtCol: "(relation link)",
        srcDataType: "",
        tgtDataType: "",
        desc: tgt.description || ""
      });
    } else {
      commonCols.forEach(col => {
        if (searchQuery && !col.toLowerCase().includes(searchQuery) && !src.name.toLowerCase().includes(searchQuery) && !tgt.name.toLowerCase().includes(searchQuery)) return;
        flows.push({
          srcName: src.name,
          srcType: src.resource_type,
          tgtName: tgt.name,
          tgtType: tgt.resource_type,
          srcCol: col,
          tgtCol: col,
          srcDataType: src.column_types?.[col] || "",
          tgtDataType: tgt.column_types?.[col] || "",
          desc: tgt.column_descriptions?.[col] || src.column_descriptions?.[col] || ""
        });
      });
    }
  });

  if (!flows.length) {
    setHtml("dbt-lineage", empty("No column flows found", "Trace columns across upstream and downstream dbt models."));
    return;
  }

  const cards = flows.slice(0, 80).map(flow => `
    <div class="dbt-col-flow-card">
      <div class="dbt-col-flow-side">
        <span class="dbt-col-flow-node-name">${esc(flow.srcType)} · ${esc(flow.srcName)}</span>
        <span class="dbt-col-flow-col-name">${esc(flow.srcCol)} ${flow.srcDataType ? `<small class="dbt-col-type">${esc(flow.srcDataType)}</small>` : ''}</span>
      </div>
      <div class="dbt-col-flow-arrow">&rarr;</div>
      <div class="dbt-col-flow-side">
        <span class="dbt-col-flow-node-name">${esc(flow.tgtType)} · ${esc(flow.tgtName)}</span>
        <span class="dbt-col-flow-col-name">${esc(flow.tgtCol)} ${flow.tgtDataType ? `<small class="dbt-col-type">${esc(flow.tgtDataType)}</small>` : ''}</span>
        ${flow.desc ? `<small class="secondary-cell">${esc(flow.desc)}</small>` : ''}
      </div>
    </div>
  `).join("");

  setHtml("dbt-lineage", `<div class="dbt-col-flow-list">${cards}${flows.length > 80 ? `<p class="form-note">Showing first 80 of ${flows.length} column flows.</p>` : ''}</div>`);
}

function renderDbtEdgeList(artifact) {
  const nodes = new Map((state.dbtLineage?.nodes || []).map(node => [node.id, node]));
  const edges = (state.dbtLineage?.edges || []).slice(0, 100).map(edge => {
    const source = nodes.get(edge.source_resource_id), target = nodes.get(edge.target_resource_id);
    if (!source || !target) return "";
    return `<div class="lineage-edge"><div class="lineage-node"><strong>${esc(source.label)}</strong><small>${esc(human(source.resource_type))}${source.matched_table_id ? " / catalog linked" : ""}</small></div><b>&rarr;</b><div class="lineage-node target"><strong>${esc(target.label)}</strong><small>${esc(human(target.resource_type))}${target.materialization ? ` / ${esc(target.materialization)}` : ""}</small></div></div>`;
  }).join("");
  setHtml("dbt-lineage", edges ? `<div class="lineage-list">${edges}</div>${artifact.lineage_edge_count > 100 ? `<p class="form-note">Showing first 100 of ${artifact.lineage_edge_count} edges.</p>` : ""}` : empty("No dependencies declared"));
}

function renderDbtArtifact() {
  if (!dbtEnabled()) {
    renderDbtDisabledState();
    return;
  }
  const artifact = state.dbtImports.find(item => item.id === state.selectedDbtImportId);
  if (!artifact) {
    setHtml("dbt-metrics", [["Registered projects",state.dbtProjects.length,"Selected delivery scope"],["Artifact imports",0,"Import manifest.json to begin"],["Catalog matches",0,"No artifact selected"],["Lineage edges",0,"No artifact selected"]].map(([a,b,c]) => `<div class="metric"><p>${a}</p><strong>${b}</strong><small>${c}</small></div>`).join(""));
    setHtml("dbt-resources-table", empty("No dbt artifact selected", "Import a manifest or select an immutable artifact."));
    setHtml("dbt-lineage", empty("No lineage available"));
    setHtml("dbt-lineage-status", badge("NOT_CONFIGURED"));
    return;
  }
  setHtml("dbt-metrics", [
    ["Models",artifact.model_count,"Compiled transformation nodes"],
    ["Sources",artifact.source_count,"Declared upstream relations"],
    ["Catalog matches",artifact.matched_resource_count,`${artifact.unmatched_resource_count} relation mappings need attention`],
    ["Lineage edges",artifact.lineage_edge_count,`${artifact.test_count} test nodes included`]
  ].map(([a,b,c]) => `<div class="metric"><p>${a}</p><strong>${b}</strong><small>${c}</small></div>`).join(""));

  const type = $("#dbt-resource-type")?.value || "ALL";
  const match = $("#dbt-match-filter")?.value || "ALL";
  const visible = state.dbtResources.filter(item => (type === "ALL" || item.resource_type === type) && (match === "ALL" || (match === "MATCHED") === Boolean(item.matched_table_id)));
  const rows = visible.map(item => {
    const testPill = item.test_status ? `<span class="dbt-test-pill ${item.test_status.toLowerCase()}">${esc(item.test_status)}${item.test_failures ? ` (${item.test_failures})` : ""}</span>` : "";
    return `<tr><td><button class="link-button" data-dbt-resource="${item.id}">${esc(item.name)}</button><span class="secondary-cell">${esc(item.package_name)} / ${esc(item.unique_id)}</span></td><td>${badge(item.resource_type)} ${testPill}</td><td>${esc(item.materialization || "Not applicable")}</td><td>${badge(item.matched_table_id ? "MATCHED" : "UNMATCHED")}</td><td>${badge(item.sql_parse_status)}</td><td>${item.column_names.length}</td></tr>`;
  });
  renderTable("dbt-resources-table", ["Resource","Type","Materialization","Catalog","SQL evidence","Columns"], rows, "No resources match these filters");

  // Update view mode button active state
  $$("[data-dbt-dag-mode]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.dbtDagMode === state.dbtDagMode);
  });

  if (state.dbtDagMode === "columns") {
    renderDbtColumnFlows(artifact);
  } else if (state.dbtDagMode === "edges") {
    renderDbtEdgeList(artifact);
  } else {
    renderDbtLineageDAG(artifact);
  }
  setHtml("dbt-lineage-status", badge("IMPORTED"));
}

async function loadDbtArtifact(artifactId) {
  state.selectedDbtImportId = artifactId;
  if (!artifactId) { state.dbtResources = []; state.dbtLineage = null; renderDbtArtifact(); return; }
  [state.dbtResources, state.dbtLineage] = await Promise.all([
    fetchAll(`/v1/dbt-artifact-imports/${artifactId}/resources`, 2000),
    api(`/v1/dbt-artifact-imports/${artifactId}/lineage?limit=2000`)
  ]);
  renderDbtArtifact();
}

async function selectDbtProject(dbtProjectId) {
  state.selectedDbtProjectId = dbtProjectId;
  state.dbtImports = dbtProjectId ? await fetchAll(`/v1/dbt-projects/${dbtProjectId}/artifact-imports`) : [];
  renderDbtProjects(); renderDbtImports();
  const preferred = state.dbtImports.some(item => item.id === state.selectedDbtImportId) ? state.selectedDbtImportId : state.dbtImports[0]?.id || null;
  await loadDbtArtifact(preferred);
}

async function loadDbtProjects() {
  renderTransformationOverview();
  if (!dbtEnabled()) {
    Object.assign(state, {
      dbtProjects: [], dbtImports: [], dbtResources: [], dbtLineage: null,
      selectedDbtProjectId: null, selectedDbtImportId: null
    });
    renderDbtProjects();
    renderDbtImports();
    renderDbtDisabledState();
    return;
  }
  const projectId = $("#transform-project")?.value;
  populateProjectSources("dbt-source", projectId);
  state.dbtProjects = projectId ? await fetchAll(`/v1/projects/${projectId}/dbt-projects`) : [];
  const preferred = state.dbtProjects.some(item => item.id === state.selectedDbtProjectId) ? state.selectedDbtProjectId : state.dbtProjects[0]?.id || null;
  await selectDbtProject(preferred);
}

async function loadOpenLineage() {
  renderTransformationOverview();
  if (!integrationFlags().openlineage) {
    state.openlineageEvents = [];
    renderOpenLineageHistory();
    return;
  }
  const sourceId = $("#openlineage-source")?.value;
  populateProjectSources("openlineage-source", $("#transform-project")?.value);
  if (!sourceId) {
    state.openlineageEvents = [];
    setHtml("openlineage-history", empty("No source selected", "Choose a warehouse source to inspect OpenLineage evidence."));
    return;
  }
  state.openlineageEvents = await fetchAll(`/v1/datasources/${sourceId}/openlineage-events`);
  renderOpenLineageHistory();
}

function showDbtResource(resourceId) {
  const resource = state.dbtResources.find(item => item.id === resourceId); if (!resource) return;
  $("#record-title").textContent = `${human(resource.resource_type)} / ${resource.name}`;

  let testBanner = "";
  if (resource.test_status) {
    testBanner = `
      <div class="boundary-callout">
        <div>
          <strong>Test Execution Health: ${esc(resource.test_status)}</strong>
          <p>${resource.test_failures !== null && resource.test_failures !== undefined ? `Failures observed: ${resource.test_failures} rows.` : 'Assertions executed with 0 failures.'} ${resource.test_execution_time ? `Execution time: ${resource.test_execution_time.toFixed(2)}s.` : ''}</p>
        </div>
      </div>
    `;
  }

  let colSection = "";
  if (resource.column_names && resource.column_names.length) {
    const colRows = resource.column_names.map(col => {
      const dtype = resource.column_types?.[col] || "Not resolved";
      const desc = resource.column_descriptions?.[col] || "—";
      return `<tr><td><strong>${esc(col)}</strong></td><td><code>${esc(dtype)}</code></td><td>${esc(desc)}</td></tr>`;
    }).join("");
    colSection = `
      <h3>Columns & Physical Schema Types</h3>
      <table class="data-table">
        <thead><tr><th>Column Name</th><th>Physical Type</th><th>Documentation</th></tr></thead>
        <tbody>${colRows}</tbody>
      </table>
    `;
  }

  let exposureSection = "";
  if (resource.extra_metadata && Object.keys(resource.extra_metadata).length) {
    const entries = Object.entries(resource.extra_metadata).map(([k, v]) => `<dt>${esc(human(k))}</dt><dd>${esc(String(v))}</dd>`).join("");
    exposureSection = `<h3>Downstream & Exposure Metadata</h3><dl class="record-json">${entries}</dl>`;
  }

  const details = `
    ${testBanner}
    <dl class="record-json">
      <dt>Unique ID</dt><dd>${esc(resource.unique_id)}</dd>
      <dt>Relation</dt><dd>${esc(resource.relation_name || "Not a warehouse relation")}</dd>
      <dt>Materialization</dt><dd>${esc(resource.materialization || "Not applicable")}</dd>
      <dt>Catalog mapping</dt><dd>${esc(resource.matched_table_id || "Unmatched")}</dd>
      <dt>Source file</dt><dd>${esc(resource.original_file_path || "Not recorded")}</dd>
      <dt>Tags</dt><dd>${esc(resource.tags.join(", ") || "None")}</dd>
      <dt>SQL fingerprint</dt><dd>${esc(resource.compiled_sql_hash || "No compiled SQL")}</dd>
    </dl>
    ${colSection}
    ${exposureSection}
    ${resource.compiled_sql_redacted ? `<h3>Literal-redacted compiled SQL</h3><pre class="sql-preview">${esc(resource.compiled_sql_redacted)}</pre>` : `<p class="form-note">Compiled SQL was not present or could not be safely normalized; only its fingerprint is retained.</p>`}
  `;
  setHtml("record-content", details); $("#record-dialog").showModal();
}
  Object.assign(window.AtlasUI, { renderDbtProjects, renderOpenLineageHistory, renderDbtImports, renderDbtLineageDAG, renderDbtColumnFlows, renderDbtEdgeList, renderDbtArtifact, loadDbtArtifact, selectDbtProject, loadDbtProjects, loadOpenLineage, showDbtResource });
})();
