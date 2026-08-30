/* Data-product marketplace, contracts, AI registry, assessments, and trust factors. */
(function initializeProductAiControlPlane() {
  const { state, $, setHtml, esc, when, human, badge, empty, api, selectOptions, preserveSelect } = window.AtlasUI;
  const feature = {
    products: [],
    contracts: [],
    marketplace: [],
    access: [],
    aiAssets: [],
    aiGraph: null,
    aiGraphEngine: null,
    aiSelectedAsset: null,
    aiSelectedNodeId: null,
  };
  const split = value => String(value || "").split(",").map(item => item.trim()).filter(Boolean);

  function message(target, text, kind="neutral") {
    setHtml(target, text ? `<div class="feature-message ${kind}">${esc(text)}</div>` : "");
  }

  function populateProjectSelectors() {
    const options = selectOptions(state.projects, item => item.name, "No projects");
    preserveSelect("data-product-project", options);
    preserveSelect("data-product-project-filter", options);
  }

  function renderProducerProducts() {
    if (!feature.products.length) return setHtml("data-products-table", empty("No data products", "Create a candidate and publish its compatible contract first."));
    const rows = feature.products.map(item => `<tr><td><strong>${esc(item.name)}</strong><span class="secondary-cell">${esc(item.product_key)} / v${item.version}</span></td><td>${badge(item.status)}</td><td>${esc(item.domain_name)}</td><td>${esc(item.owner_principal)}</td><td>${item.quality_score ?? "Not scored"}</td><td>${item.lineage_coverage}%</td><td>${item.status === "DRAFT" ? `<button class="button small" data-product-submit="${item.id}">Submit</button>` : ""}</td></tr>`);
    setHtml("data-products-table", `<div class="result-scroll"><table class="data-table"><thead><tr><th>Product</th><th>Status</th><th>Domain</th><th>Owner</th><th>Quality</th><th>Lineage</th><th>Action</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`);
    preserveSelect("contract-data-product", feature.products.map(item => `<option value="${item.product_id}">${esc(item.name)} (${esc(item.status)})</option>`).join(""));
  }

  function renderContracts() {
    if (!feature.contracts.length) return setHtml("data-contracts-table", empty("No contract versions", "Define the first schema contract for the selected product."));
    setHtml("data-contracts-table", feature.contracts.map(item => `<div class="contract-row"><div><strong>Contract v${item.version}</strong><span>${esc(item.compatibility_mode)} / ${esc(item.compatibility_status)}</span></div>${badge(item.status)}${item.status === "DRAFT" ? `<button class="button small" data-contract-submit="${item.id}">Submit</button>` : ""}</div>`).join(""));
  }

  async function loadContracts() {
    const productId = $("#contract-data-product")?.value;
    if (!productId) { feature.contracts = []; return renderContracts(); }
    try {
      const page = await api(`/v1/data-products/${productId}/contracts?limit=100&offset=0`);
      feature.contracts = page.items || [];
      renderContracts();
    } catch (error) { message("marketplace-message", error.message, "error"); }
  }

  async function loadDataProducts() {
    populateProjectSelectors();
    const projectId = $("#data-product-project-filter")?.value || $("#data-product-project")?.value;
    if (!projectId) { feature.products = []; return renderProducerProducts(); }
    try {
      const page = await api(`/v1/projects/${projectId}/data-products?limit=200&offset=0`);
      feature.products = page.items || [];
      renderProducerProducts();
      await loadContracts();
    } catch (error) { message("marketplace-message", error.message, "error"); }
  }

  function renderMarketplace() {
    if (!feature.marketplace.length) return setHtml("marketplace-products-table", empty("No eligible products", "Published products appear only when discovery policy allows your roles."));
    const rows = feature.marketplace.map(item => `<tr><td><strong>${esc(item.name)}</strong><span class="secondary-cell">${esc(item.product_key)} / ${esc(item.domain_name)}</span></td><td>${badge(item.certification_status)}</td><td>${esc(item.classification)}</td><td>${item.quality_score ?? "--"}</td><td>${item.lineage_coverage}%</td><td>${badge(item.access_status)}</td></tr>`);
    setHtml("marketplace-products-table", `<div class="result-scroll"><table class="data-table"><thead><tr><th>Product</th><th>Certification</th><th>Class</th><th>Quality</th><th>Lineage</th><th>Access</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`);
    preserveSelect("marketplace-access-product", feature.marketplace.filter(item => item.access_status === "NOT_REQUESTED").map(item => `<option value="${item.id}">${esc(item.name)} v${item.version}</option>`).join(""));
  }

  function renderAccessRequests() {
    if (!feature.access.length) return setHtml("marketplace-access-table", empty("No access requests"));
    setHtml("marketplace-access-table", feature.access.slice(0, 8).map(item => `<div class="access-row"><div><strong>${esc(item.purpose)}</strong><span>${item.duration_days} days / ${when(item.expires_at)}</span></div>${badge(item.status)}</div>`).join(""));
  }

  async function loadMarketplace() {
    const q = encodeURIComponent($("#marketplace-search")?.value || "");
    try {
      const [products, access] = await Promise.all([
        api(`/v1/marketplace/products?limit=100&offset=0${q ? `&q=${q}` : ""}`),
        api("/v1/marketplace/access-requests?limit=100&offset=0"),
      ]);
      feature.marketplace = products.items || [];
      feature.access = access.items || [];
      renderMarketplace();
      renderAccessRequests();
    } catch (error) { message("marketplace-message", error.message, "error"); }
  }

  async function createDataProduct(form) {
    const values = new FormData(form), projectId = String(values.get("project_id") || "");
    const outputId = String(values.get("output_asset_id") || "").trim();
    const contextId = String(values.get("context_product_version_id") || "").trim();
    const quality = String(values.get("quality_score") || "").trim();
    const body = {
      product_key: values.get("product_key"), name: values.get("name"), description: values.get("description"),
      domain_name: values.get("domain_name"), owner_principal: values.get("owner_principal"), usage_terms: values.get("usage_terms"),
      classification: values.get("classification"), certification_status: "UNCERTIFIED",
      quality_score: quality ? Number(quality) : null, lineage_coverage: Number(values.get("lineage_coverage") || 0),
      context_product_version_id: contextId || null, discoverable_roles: split(values.get("discoverable_roles")), consumer_roles: split(values.get("consumer_roles")),
      ports: [{port_key:"primary_output", direction:"OUTPUT", name:"Primary output", description:"Governed primary data-product output", asset_type:"TABLE", asset_id:outputId}],
    };
    try {
      await api(`/v1/projects/${projectId}/data-products`, {method:"POST", body:JSON.stringify(body)});
      message("marketplace-message", "Candidate created. Publish a compatible contract before submitting the product.", "success");
      await loadDataProducts();
    } catch (error) { message("marketplace-message", error.message, "error"); }
  }

  async function createContract(form) {
    const values = new FormData(form), productId = String(values.get("product_id") || "");
    const schema = String(values.get("schema_fields") || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(line => {
      const [name, dataType, posture="optional"] = line.split(/[:|]/).map(value => value.trim());
      return {name, data_type:dataType, required:posture.toLowerCase() === "required"};
    });
    const body = {compatibility_mode:values.get("compatibility_mode"), schema_definition:schema, quality_rules:[], freshness_sla_minutes:Number(values.get("freshness_sla_minutes")), availability_sla_percent:Number(values.get("availability_sla_percent")), producer_principal:values.get("producer_principal"), consumer_roles:[]};
    try {
      await api(`/v1/data-products/${productId}/contracts`, {method:"POST", body:JSON.stringify(body)});
      message("marketplace-message", "Contract draft created with deterministic compatibility findings.", "success");
      await loadContracts();
    } catch (error) { message("marketplace-message", error.message, "error"); }
  }

  async function submitLifecycle(path, successText) {
    try { await api(path, {method:"POST"}); message("marketplace-message", successText, "success"); await Promise.all([loadDataProducts(), loadMarketplace()]); }
    catch (error) { message("marketplace-message", error.message, "error"); }
  }

  async function requestAccess(form) {
    const values = new FormData(form), versionId = String(values.get("version_id") || "");
    try {
      await api(`/v1/marketplace/products/${versionId}/access-requests`, {method:"POST", body:JSON.stringify({purpose:values.get("purpose"), duration_days:Number(values.get("duration_days") || 30)})});
      message("marketplace-message", "Access request entered independent review.", "success");
      form.reset(); await loadMarketplace();
    } catch (error) { message("marketplace-message", error.message, "error"); }
  }

  function aiNodeHtml(data, meta) {
    const classes = ["atlas-node-card", "compact"];
    if (meta.selected) classes.push("is-selected");
    if (data.is_root) classes.push("is-focus");
    if (data.agMatch) classes.push("is-match");
    if (data.agDim) classes.push("is-dim");
    const kindClass = String(data.kind || "").toLowerCase().replace(/_/g, "-");
    classes.push(`type-${kindClass}`);
    return `<div class="${classes.join(" ")}" data-ai-dependency-node="${esc(data.id)}" tabindex="0" role="button" aria-label="Inspect ${esc(data.label || data.reference || data.id)}">`
      + `<div class="ag-card-head"><span class="ag-type-tag">${esc(human(data.kind || "dependency"))}</span>${data.is_root ? '<span class="ag-pill matched">Selected</span>' : ""}</div>`
      + `<span class="ag-title">${esc(data.label || data.reference || data.id)}</span>`
      + `<span class="ag-sub">${esc(data.reference || data.status || "Governed dependency")}</span>`
      + `</div>`;
  }

  function resetAiDependencyView() {
    feature.aiGraph = null;
    feature.aiSelectedAsset = null;
    feature.aiSelectedNodeId = null;
    if (feature.aiGraphEngine) {
      try { feature.aiGraphEngine.destroy(); } catch (error) { /* detached instance */ }
      feature.aiGraphEngine = null;
    }
    setHtml("ai-dependency-canvas", empty("Select an AI asset", "Dependency topology will appear here."));
    setHtml("ai-dependency-detail", empty("Select an AI asset", "Version facts, dependency counts, and governance-safe references will appear here."));
  }

  function renderAiDependencyDetail(nodeId) {
    const node = feature.aiGraph?.nodes?.find(item => item.id === nodeId);
    if (!node) {
      setHtml("ai-dependency-detail", empty("Dependency unavailable", "Select another asset or refresh the registry."));
      return;
    }
    feature.aiSelectedNodeId = nodeId;
    feature.aiGraphEngine?.select(nodeId);
    const selectedAsset = feature.aiSelectedAsset;
    const incoming = feature.aiGraph.edges.filter(edge => edge.target === nodeId).length;
    const outgoing = feature.aiGraph.edges.filter(edge => edge.source === nodeId).length;
    const chips = [];
    if (node.kind === "POLICY_CONTROL") chips.push(`<span class="ai-chip">${esc(node.reference || "policy control")}</span>`);
    if (node.kind === "MODEL_ROUTE") chips.push(`<span class="ai-chip">Approved route reference</span>`);
    if (node.kind === "CONTEXT_PRODUCT_VERSION") chips.push(`<span class="ai-chip">Published context dependency</span>`);
    if (node.is_root && selectedAsset) {
      selectedAsset.policy_control_ids.forEach(item => chips.push(`<span class="ai-chip">${esc(item)}</span>`));
    }
    const narrative = node.is_root && selectedAsset
      ? `<p>${esc(selectedAsset.description)}</p><p class="form-note spaced">${esc(selectedAsset.intended_use)}</p>`
      : `<p>${esc(node.reference || "Value-free governed dependency reference.")}</p>`;
    const facts = node.is_root && selectedAsset
      ? [
          ["Status", badge(selectedAsset.status)],
          ["Risk tier", badge(selectedAsset.risk_tier)],
          ["Provider", esc(selectedAsset.provider_type)],
          ["Owner", esc(selectedAsset.owner_principal)],
          ["Dependencies", String(feature.aiGraph.edges.length)],
          ["Updated", esc(when(selectedAsset.updated_at))],
        ]
      : [
          ["Kind", esc(human(node.kind || "dependency"))],
          ["Inbound", String(incoming)],
          ["Outbound", String(outgoing)],
          ["Reference", esc(node.reference || node.id)],
        ];
    setHtml(
      "ai-dependency-detail",
      `<div class="panel-heading"><div><p class="eyebrow">DEPENDENCY EVIDENCE</p><h2>${esc(node.label || node.reference || node.id)}</h2></div>${node.is_root && selectedAsset ? badge(selectedAsset.asset_kind) : badge(node.kind || "DEPENDENCY")}</div>`
      + `${narrative}`
      + `<div class="ai-dependency-facts">${facts.map(([label, value]) => `<div><span>${label}</span><strong>${value}</strong></div>`).join("")}</div>`
      + (chips.length ? `<div class="ai-chip-list">${chips.join("")}</div>` : "")
      + (node.is_root && selectedAsset?.documentation_url ? `<p class="form-note spaced">Documentation: <a href="${esc(selectedAsset.documentation_url)}" target="_blank" rel="noreferrer">${esc(selectedAsset.documentation_url)}</a></p>` : ""),
    );
  }

  function renderAiDependencyGraph() {
    const graph = feature.aiGraph;
    if (!graph?.nodes?.length) return resetAiDependencyView();
    setHtml("ai-dependency-canvas", '<div class="lineage-summary" id="ai-dependency-summary"></div><div id="ai-dependency-stage"></div>');
    setHtml(
      "ai-dependency-summary",
      `<span>${graph.nodes.length} nodes</span><span>${graph.edges.length} edges</span><span>Value-free dependency evidence</span>`,
    );
    feature.aiGraphEngine = window.AtlasUI.AtlasGraph.mount(
      "ai-dependency-stage",
      {
        direction: "LR",
        nodeSep: 30,
        rankSep: 120,
        nodeHtml: aiNodeHtml,
        matchNode: (data, q) => `${data.label || ""} ${data.reference || ""} ${data.kind || ""}`.toLowerCase().includes(q),
        onNodeExpand: data => renderAiDependencyDetail(data.id),
      },
      feature,
      "aiGraphEngine",
    );
    if (!feature.aiGraphEngine) return;
    const nodes = graph.nodes.map(node => ({
      id: node.id,
      w: node.is_root ? 168 : 150,
      h: 68,
      data: {
        id: node.id,
        label: node.label,
        kind: node.kind,
        reference: node.reference,
        status: node.status,
        is_root: Boolean(node.is_root),
      },
    }));
    const edges = graph.edges.map((edge, index) => ({
      id: `ai-dependency-${index}`,
      source: edge.source,
      target: edge.target,
      classes: "declared",
      data: { relationship: edge.relationship },
    }));
    feature.aiGraphEngine.setData(nodes, edges, {
      emptyHtml: empty("No dependency graph", "This AI asset version has no governed dependencies."),
      selectId: feature.aiSelectedNodeId,
    });
  }

  async function loadAiDependencyGraph(versionId) {
    const selected = feature.aiAssets.find(item => item.id === versionId);
    if (!selected) return message("ai-registry-message", "AI asset version is no longer available.", "error");
    feature.aiSelectedAsset = selected;
    feature.aiSelectedNodeId = String(versionId);
    message("ai-registry-message", "Loading governed dependency graph...");
    try {
      const graph = await api(`/v1/ai-asset-versions/${versionId}/dependencies`);
      feature.aiGraph = {
        nodes: (graph.nodes || []).map(node => ({
          ...node,
          label: node.name || node.reference || node.id,
          is_root: node.id === String(versionId),
        })),
        edges: graph.edges || [],
      };
      renderAiDependencyGraph();
      renderAiDependencyDetail(String(versionId));
      message("ai-registry-message", "Dependency graph loaded from governed AI registry references.", "success");
    } catch (error) {
      message("ai-registry-message", error.message, "error");
    }
  }

  async function requestAiRetirement(assetId) {
    try {
      await api(`/v1/ai-assets/${assetId}/retire`, { method: "POST" });
      message("ai-registry-message", "AI asset retirement entered independent review.", "success");
      await loadAiRegistry();
    } catch (error) {
      message("ai-registry-message", error.message, "error");
    }
  }

  function renderAiAssets() {
    if (!feature.aiAssets.length) {
      resetAiDependencyView();
      return setHtml("ai-assets-table", empty("No AI assets", "Register an AI use case, model, or agent as an immutable draft."));
    }
    const rows = feature.aiAssets.map(item => {
      const actions = [
        `<button class="button small secondary" data-ai-graph="${item.id}">Graph</button>`,
        `<button class="button small secondary" data-ai-trust="${item.id}">Trust</button>`,
        item.status === "DRAFT" ? `<button class="button small" data-ai-submit="${item.id}">Submit</button>` : "",
        item.status !== "DRAFT" ? `<button class="button small danger" data-ai-retire="${item.asset_id}">Retire</button>` : "",
      ].join("");
      return `<tr><td><strong>${esc(item.name)}</strong><span class="secondary-cell">${esc(item.asset_key)} / ${human(item.asset_kind)} v${item.version}</span></td><td>${badge(item.status)}</td><td>${badge(item.risk_tier)}</td><td>${esc(item.owner_principal)}</td><td>${item.policy_control_ids.length}</td><td>${actions}</td></tr>`;
    });
    setHtml("ai-assets-table", `<div class="result-scroll"><table class="data-table"><thead><tr><th>AI asset</th><th>Status</th><th>Risk</th><th>Owner</th><th>Controls</th><th>Actions</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`);
    preserveSelect("assessment-ai-version", feature.aiAssets.map(item => `<option value="${item.id}">${esc(item.name)} v${item.version}</option>`).join(""));
  }

  async function loadAiRegistry() {
    if (!state.organizationId) { feature.aiAssets = []; return renderAiAssets(); }
    try {
      const page = await api(`/v1/organizations/${state.organizationId}/ai-assets?limit=200&offset=0`);
      feature.aiAssets = page.items || [];
      renderAiAssets();
      if (feature.aiSelectedAsset) {
        const current = feature.aiAssets.find(item => item.id === feature.aiSelectedAsset.id);
        if (current) {
          feature.aiSelectedAsset = current;
          await loadAiDependencyGraph(current.id);
        } else {
          resetAiDependencyView();
        }
      }
    } catch (error) { message("ai-registry-message", error.message, "error"); }
  }

  async function createAiAsset(form) {
    const values = new FormData(form);
    const body = {asset_key:values.get("asset_key"), asset_kind:values.get("asset_kind"), name:values.get("name"), description:values.get("description"), intended_use:values.get("intended_use"), owner_principal:values.get("owner_principal"), provider_type:values.get("provider_type"), risk_tier:values.get("risk_tier"), documentation_url:values.get("documentation_url") || null, context_product_version_ids:[], model_route_ids:[], policy_control_ids:split(values.get("policy_control_ids")), evaluation_evidence:{pass_rate:Number(values.get("evaluation_pass_rate") || 0) / 100, evidence_id:"ui-attested-evaluation"}, runtime_evidence:{success_rate:Number(values.get("runtime_success_rate") || 0) / 100, open_critical_incidents:Number(values.get("open_critical_incidents") || 0), evidence_id:"ui-attested-runtime"}};
    try { await api(`/v1/organizations/${state.organizationId}/ai-assets`, {method:"POST", body:JSON.stringify(body)}); message("ai-registry-message", "AI asset draft registered. Independent approval remains required.", "success"); await loadAiRegistry(); }
    catch (error) { message("ai-registry-message", error.message, "error"); }
  }

  async function assessAiAsset(form) {
    const values = new FormData(form), versionId = String(values.get("version_id") || "");
    const controls = String(values.get("controls") || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(line => { const [control_key, title, weight, outcome] = line.split("|").map(value => value.trim()); return {control_key, title, weight:Number(weight || 1), outcome}; });
    try { await api(`/v1/ai-asset-versions/${versionId}/assessments`, {method:"POST", body:JSON.stringify({framework:values.get("framework"), framework_version:values.get("framework_version"), control_results:controls})}); message("ai-registry-message", "Independent assessment evidence recorded.", "success"); await inspectTrust(versionId); }
    catch (error) { message("ai-registry-message", error.message, "error"); }
  }

  async function inspectTrust(versionId) {
    try {
      const trust = await api(`/v1/ai-asset-versions/${versionId}/trust`);
      const factors = trust.factors.map(item => `<article class="trust-factor"><div><span>${esc(human(item.factor))}</span><strong>${item.score} / ${item.maximum}</strong></div><div class="trust-meter"><i style="width:${Math.min(100, (item.score / item.maximum) * 100)}%"></i></div><p>${esc(item.reason)}</p></article>`).join("");
      setHtml("ai-trust-detail", `<div class="trust-score ${trust.grade.toLowerCase()}"><span>${badge(trust.grade)}</span><strong>${trust.score}</strong><small>of 100</small>${trust.blockers.length ? `<p>Blockers: ${esc(trust.blockers.join(", "))}</p>` : ""}</div>${factors}`);
    } catch (error) { message("ai-registry-message", error.message, "error"); }
  }

  document.addEventListener("submit", event => {
    const handlers = {"data-product-form":createDataProduct, "data-contract-form":createContract, "marketplace-access-form":requestAccess, "ai-asset-form":createAiAsset, "ai-assessment-form":assessAiAsset};
    const handler = handlers[event.target.id]; if (!handler) return; event.preventDefault(); handler(event.target);
  });
  document.addEventListener("change", event => {
    if (event.target.id === "data-product-project-filter") loadDataProducts();
    if (event.target.id === "contract-data-product") loadContracts();
    if (event.target.id === "organization-select") setTimeout(() => { populateProjectSelectors(); resetAiDependencyView(); loadAiRegistry(); }, 250);
  });
  document.addEventListener("click", event => {
    const target = event.target.closest("[data-product-submit], [data-contract-submit], [data-ai-submit], [data-ai-trust], [data-ai-graph], [data-ai-retire], [data-ai-dependency-node], #refresh-marketplace, #refresh-ai-registry"); if (!target) return;
    if (target.dataset.productSubmit) return submitLifecycle(`/v1/data-product-versions/${target.dataset.productSubmit}/submit`, "Product publication review requested.");
    if (target.dataset.contractSubmit) return submitLifecycle(`/v1/data-contract-versions/${target.dataset.contractSubmit}/submit`, "Contract review requested. Breaking findings require an explicit exception approval.");
    if (target.dataset.aiSubmit) return api(`/v1/ai-asset-versions/${target.dataset.aiSubmit}/submit`, {method:"POST"}).then(() => { message("ai-registry-message", "AI asset approval requested.", "success"); loadAiRegistry(); }).catch(error => message("ai-registry-message", error.message, "error"));
    if (target.dataset.aiTrust) return inspectTrust(target.dataset.aiTrust);
    if (target.dataset.aiGraph) return loadAiDependencyGraph(target.dataset.aiGraph);
    if (target.dataset.aiRetire) return requestAiRetirement(target.dataset.aiRetire);
    if (target.dataset.aiDependencyNode) return renderAiDependencyDetail(target.dataset.aiDependencyNode);
    if (target.id === "refresh-marketplace") return Promise.all([loadDataProducts(), loadMarketplace()]);
    if (target.id === "refresh-ai-registry") return loadAiRegistry();
  });
  document.addEventListener("input", event => { if (event.target.id === "marketplace-search") { clearTimeout(feature.searchTimer); feature.searchTimer = setTimeout(loadMarketplace, 250); } });
  document.addEventListener("click", event => {
    const view = event.target.closest("[data-view]")?.dataset.view;
    if (view === "marketplace") setTimeout(() => { populateProjectSelectors(); loadDataProducts(); loadMarketplace(); }, 0);
    if (view === "ai-registry") setTimeout(loadAiRegistry, 0);
  });
  Object.assign(window.AtlasUI, { loadDataProducts, loadMarketplace, loadAiRegistry });
})();
