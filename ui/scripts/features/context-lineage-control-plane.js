/* Governed Context Products and unified lineage operator surfaces. */
(function initializeContextLineageControlPlane() {
  const { state, $, setHtml, esc, human, badge, empty, api, selectOptions, preserveSelect } = window.AtlasUI;
  const feature = { products: [], graph: null, impact: null, engine: null };

  function message(target, text, kind="neutral") {
    setHtml(target, text ? `<div class="feature-message ${kind}">${esc(text)}</div>` : "");
  }

  function populateSelectors() {
    preserveSelect("context-product-project", selectOptions(state.projects, item => item.name, "No projects"));
    preserveSelect("context-product-filter-project", selectOptions(state.projects, item => item.name, "No projects"));
    preserveSelect("unified-lineage-source", selectOptions(state.sources, item => item.name, "No sources"));
  }

  function renderProducts() {
    if (!feature.products.length) {
      return setHtml("context-products-table", empty("No Context Products", "Create a bounded product from approved tables, semantics, terms, and tools."));
    }
    const rows = feature.products.map(product => {
      const version = product.latest_version;
      const actions = [
        version.status === "DRAFT" ? `<button class="button small" data-context-submit="${version.id}">Submit</button>` : "",
        version.status === "PUBLISHED" ? `<button class="button small secondary" data-context-deprecate="${version.id}">Deprecate</button>` : "",
        `<button class="button small secondary" data-context-compile="${version.id}">Compile</button>`,
      ].join("");
      return `<tr><td><strong>${esc(version.name)}</strong><span class="secondary-cell">${esc(product.product_key)} / v${version.version}</span></td><td>${badge(version.status)}</td><td>${esc(version.owner_principal)}</td><td>${esc(version.allowed_consumer_roles.join(", "))}</td><td><code>${esc(version.fingerprint.slice(0, 12))}</code></td><td>${actions}</td></tr>`;
    });
    setHtml("context-products-table", `<div class="result-scroll"><table class="data-table"><thead><tr><th>Product</th><th>Status</th><th>Owner</th><th>Consumers</th><th>Fingerprint</th><th>Actions</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`);
  }

  async function loadContextProducts() {
    populateSelectors();
    const projectId = $("#context-product-filter-project")?.value || $("#context-product-project")?.value;
    if (!projectId) return renderProducts();
    message("context-product-message", "Loading governed products...");
    try {
      const page = await api(`/v1/projects/${projectId}/context-products?limit=200&offset=0`);
      feature.products = page.items || [];
      renderProducts();
      message("context-product-message", `${page.total} governed product${page.total === 1 ? "" : "s"} in this project.`, "success");
    } catch (error) {
      message("context-product-message", error.message, "error");
    }
  }

  async function createContextProduct(form) {
    const values = new FormData(form);
    const split = name => String(values.get(name) || "").split(",").map(value => value.trim()).filter(Boolean);
    const projectId = String(values.get("project_id") || "");
    const body = {
      product_key: values.get("product_key"),
      name: values.get("name"),
      description: values.get("description"),
      purpose: values.get("purpose"),
      owner_principal: values.get("owner_principal"),
      table_ids: split("table_ids"),
      semantic_model_version_ids: split("semantic_model_version_ids"),
      glossary_term_version_ids: split("glossary_term_version_ids"),
      eligible_tool_version_ids: split("eligible_tool_version_ids"),
      allowed_consumer_roles: split("allowed_consumer_roles"),
      lineage_depth: Number(values.get("lineage_depth") || 2),
      quality_requirements: {
        minimum_score: Number(values.get("minimum_score") || 0),
        deny_on_critical_incident: values.get("deny_on_critical_incident") === "on",
      },
      policy_summary: { source_values: "GATEWAY_ONLY", retention: "NO_RAW_CONTEXT", permitted_actions: ["READ_CONTEXT", "INVOKE_ELIGIBLE_TOOLS"] },
    };
    message("context-product-message", "Validating governed references...");
    try {
      await api(`/v1/projects/${projectId}/context-products`, {method:"POST", body:JSON.stringify(body)});
      form.reset();
      populateSelectors();
      message("context-product-message", "Draft created. Submit it for independent review when ready.", "success");
      await loadContextProducts();
    } catch (error) {
      message("context-product-message", error.message, "error");
    }
  }

  async function transitionVersion(versionId, action) {
    try {
      await api(`/v1/context-product-versions/${versionId}/${action}`, {method:"POST"});
      message("context-product-message", action === "submit" ? "Publication review requested." : "Deprecation review requested.", "success");
      await loadContextProducts();
    } catch (error) {
      message("context-product-message", error.message, "error");
    }
  }

  async function compileVersion(versionId) {
    const target = $("#context-compiler-target")?.value || "MCP";
    message("context-product-message", `Compiling deterministic ${target} artifact...`);
    try {
      const artifact = await api(`/v1/context-product-versions/${versionId}/compile?target=${encodeURIComponent(target)}`);
      setHtml("context-compiler-output", `<div class="compiler-meta"><span>${badge(target)}</span><code>artifact ${esc(artifact.artifact_hash)}</code><code>source ${esc(artifact.source_fingerprint)}</code></div><pre>${esc(artifact.content)}</pre>`);
      message("context-product-message", "Artifact compiled. Repeating this request against the same version produces the same hash.", "success");
    } catch (error) {
      message("context-product-message", error.message, "error");
    }
  }

  function unifiedNodeHtml(data, meta) {
    const classes = ["atlas-node-card", "compact"];
    if (meta.selected) classes.push("is-selected");
    if (data.agMatch) classes.push("is-match");
    if (data.agDim) classes.push("is-dim");
    if (data.resolved === false) classes.push("is-dim");
    const kindLabel = String(data.node_kind || "").replace(/_/g, " ");
    return `<div class="${classes.join(" ")}" data-lineage-node="${esc(data.id)}" tabindex="0" role="button" aria-label="Inspect ${esc(data.qualified_name || data.label || data.id)}">`
      + `<div class="ag-card-head"><span class="ag-type-tag">${esc(kindLabel)}</span>${data.resolved === false ? '<span class="ag-pill">Unresolved</span>' : ""}</div>`
      + `<span class="ag-title">${esc(data.label || data.id)}</span>`
      + `<span class="ag-sub">${esc(data.qualified_name || "")}</span>`
      + `</div>`;
  }

  const UNIFIED_EDGE_CLASS = { FOREIGN_KEY: "declared", SUGGESTED_RELATIONSHIP: "suggested", DBT_DEPENDENCY: "dbt", OPENLINEAGE_ETL: "openlineage" };

  function renderLineageGraph() {
    const graph = feature.graph;
    if (!graph?.nodes?.length) {
      if (feature.engine) { try { feature.engine.destroy(); } catch (error) { /* already detached */ } feature.engine = null; }
      return setHtml("unified-lineage-canvas", empty("No lineage nodes", "Import catalog, dbt, or OpenLineage metadata first."));
    }
    setHtml("unified-lineage-canvas", '<div class="lineage-summary" id="unified-lineage-summary"></div><div id="unified-lineage-stage"></div>');
    setHtml("unified-lineage-summary", `<span>${graph.returned_node_count} nodes</span><span>${graph.returned_edge_count} edges</span><span>${graph.truncated ? "Bounded result" : "Complete result"}</span>`);

    feature.engine = window.AtlasUI.AtlasGraph.mount("unified-lineage-stage", {
      direction: "LR",
      layout: graph.nodes.length > 220 ? "cose" : undefined,
      nodeSep: 26, rankSep: 120,
      nodeHtml: unifiedNodeHtml,
      matchNode: (data, q) => `${data.label || ""} ${data.qualified_name || ""}`.toLowerCase().includes(q),
      onNodeExpand: data => inspectImpact(data.id)
    }, feature, "engine");
    if (!feature.engine) return;

    const nodes = graph.nodes.slice(0, 400);
    const nodeIds = new Set(nodes.map(node => node.id));
    const cyNodes = nodes.map(node => ({
      id: node.id, w: 150, h: 64,
      data: { label: node.label, node_kind: node.node_kind, qualified_name: node.qualified_name, resolved: node.resolved, depth: node.depth }
    }));
    const cyEdges = graph.edges.filter(edge => nodeIds.has(edge.source_node_id) && nodeIds.has(edge.target_node_id)).map(edge => ({
      id: edge.id, source: edge.source_node_id, target: edge.target_node_id, classes: UNIFIED_EDGE_CLASS[edge.edge_source] || ""
    }));
    feature.engine.setData(cyNodes, cyEdges, { emptyHtml: empty("No connected nodes in view") });
  }

  async function loadUnifiedLineage() {
    populateSelectors();
    const sourceId = $("#unified-lineage-source")?.value;
    if (!sourceId) return renderLineageGraph();
    message("unified-lineage-message", "Building bounded unified graph...");
    try {
      const nodeLimit = Number($("#unified-lineage-node-limit")?.value || 300);
      const edgeLimit = Number($("#unified-lineage-edge-limit")?.value || 1500);
      feature.graph = await api(`/v1/datasources/${sourceId}/unified-lineage/graph?node_limit=${nodeLimit}&edge_limit=${edgeLimit}`);
      renderLineageGraph();
      message("unified-lineage-message", feature.graph.truncated ? `Result bounded: ${feature.graph.truncation_reasons.join(", ")}.` : "Unified graph is within the requested budget.", feature.graph.truncated ? "warning" : "success");
    } catch (error) {
      message("unified-lineage-message", error.message, "error");
    }
  }

  async function inspectImpact(nodeId) {
    const sourceId = $("#unified-lineage-source")?.value;
    if (!sourceId) return;
    try {
      feature.impact = await api(`/v1/datasources/${sourceId}/unified-lineage/impact/${encodeURIComponent(nodeId)}?depth=5&node_limit=200`);
      const impact = feature.impact;
      const rows = [...impact.upstream.map(item => ["Upstream", item]), ...impact.downstream.map(item => ["Downstream", item])]
        .map(([direction, item]) => `<tr><td>${direction}</td><td><strong>${esc(item.label)}</strong><span class="secondary-cell">${esc(item.qualified_name)}</span></td><td>${item.depth}</td><td>${esc(item.contributing_edge_sources.join(", "))}</td></tr>`);
      setHtml("unified-lineage-impact", `<div class="panel-heading"><div><p class="eyebrow">TRANSITIVE IMPACT</p><h2>${esc(impact.focus_label)}</h2></div></div>${rows.length ? `<div class="result-scroll"><table class="data-table"><thead><tr><th>Direction</th><th>Asset</th><th>Depth</th><th>Evidence</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>` : empty("No connected impact")}`);
    } catch (error) {
      message("unified-lineage-message", error.message, "error");
    }
  }

  document.addEventListener("submit", event => {
    if (event.target.id !== "context-product-form") return;
    event.preventDefault();
    createContextProduct(event.target);
  });
  document.addEventListener("click", event => {
    const target = event.target.closest("[data-context-submit], [data-context-deprecate], [data-context-compile], [data-lineage-node], #refresh-context-products, #load-unified-lineage");
    if (!target) return;
    if (target.dataset.contextSubmit) return transitionVersion(target.dataset.contextSubmit, "submit");
    if (target.dataset.contextDeprecate) return transitionVersion(target.dataset.contextDeprecate, "deprecate");
    if (target.dataset.contextCompile) return compileVersion(target.dataset.contextCompile);
    if (target.dataset.lineageNode) return inspectImpact(target.dataset.lineageNode);
    if (target.id === "refresh-context-products") return loadContextProducts();
    if (target.id === "load-unified-lineage") return loadUnifiedLineage();
  });
  document.addEventListener("change", event => {
    if (event.target.id === "context-product-filter-project") loadContextProducts();
    if (event.target.id === "organization-select") setTimeout(populateSelectors, 300);
  });
  document.addEventListener("click", event => {
    const view = event.target.closest("[data-view]")?.dataset.view;
    if (view === "context-products") setTimeout(loadContextProducts, 0);
    if (view === "unified-lineage") setTimeout(() => { populateSelectors(); loadUnifiedLineage(); }, 0);
  });
  Object.assign(window.AtlasUI, { loadContextProducts, loadUnifiedLineage });
})();
