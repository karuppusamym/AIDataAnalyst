/* Governed Context Products and unified lineage operator surfaces. */
(function initializeContextLineageControlPlane() {
  const { state, $, setHtml, esc, human, badge, empty, api, selectOptions, preserveSelect } = window.AtlasUI;
  const feature = { products: [], graph: null, impact: null };

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

  function renderLineageGraph() {
    const graph = feature.graph;
    if (!graph?.nodes?.length) return setHtml("unified-lineage-canvas", empty("No lineage nodes", "Import catalog, dbt, or OpenLineage metadata first."));
    const nodes = graph.nodes.slice(0, 80);
    const width = 960, height = 540, centerX = width / 2, centerY = height / 2;
    const positions = new Map(nodes.map((node, index) => {
      const ring = index < 16 ? 150 : 225;
      const angle = (Math.PI * 2 * index) / nodes.length - Math.PI / 2;
      return [node.id, {x:centerX + Math.cos(angle) * ring, y:centerY + Math.sin(angle) * ring}];
    }));
    const edges = graph.edges.filter(edge => positions.has(edge.source_node_id) && positions.has(edge.target_node_id));
    const edgeMarkup = edges.map(edge => {
      const start = positions.get(edge.source_node_id), end = positions.get(edge.target_node_id);
      return `<line x1="${start.x}" y1="${start.y}" x2="${end.x}" y2="${end.y}" class="lineage-edge lineage-${edge.edge_source.toLowerCase()}" />`;
    }).join("");
    const nodeMarkup = nodes.map(node => {
      const point = positions.get(node.id);
      return `<g class="lineage-node" data-lineage-node="${esc(node.id)}" tabindex="0" role="button" aria-label="Inspect ${esc(node.qualified_name)}"><circle cx="${point.x}" cy="${point.y}" r="28"></circle><text x="${point.x}" y="${point.y + 4}" text-anchor="middle">${esc(node.node_kind.slice(0, 3))}</text><title>${esc(node.qualified_name)}</title></g>`;
    }).join("");
    setHtml("unified-lineage-canvas", `<div class="lineage-summary"><span>${graph.returned_node_count} nodes</span><span>${graph.returned_edge_count} edges</span><span>${graph.truncated ? "Bounded result" : "Complete result"}</span></div><div class="lineage-svg-wrap"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Unified lineage graph">${edgeMarkup}${nodeMarkup}</svg></div>`);
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
    const target = event.target.closest("[data-context-submit], [data-context-deprecate], [data-lineage-node], #refresh-context-products, #load-unified-lineage");
    if (!target) return;
    if (target.dataset.contextSubmit) return transitionVersion(target.dataset.contextSubmit, "submit");
    if (target.dataset.contextDeprecate) return transitionVersion(target.dataset.contextDeprecate, "deprecate");
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
