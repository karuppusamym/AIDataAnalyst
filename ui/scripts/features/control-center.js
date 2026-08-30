/* Enterprise controls for completed tracker capabilities that previously had no UI. */
(function initializeControlCenter() {
  const { state, $, $$, setHtml, esc, when, human, badge, empty, table, selectOptions, preserveSelect, api } = window.AtlasUI;

  const controlState = {
    bulkRuns: [], unowned: [], workspaces: [], members: [], bindings: [],
    policies: [], decisions: [], slos: [], notificationRules: [], archive: null,
    packs: [], refusals: [], changeSets: [], biConnections: [], activePlanId: null,
  };

  function message(text, success = false) {
    setHtml("control-message", `<div class="alert ${success ? "success" : ""}">${esc(text)}</div>`);
    window.setTimeout(() => setHtml("control-message", ""), 6000);
  }

  function parseJson(value, label) {
    try { return JSON.parse(String(value || "{}").trim() || "{}"); }
    catch { throw new Error(`${label} must be valid JSON.`); }
  }

  function csv(value) {
    return String(value || "").split(",").map(item => item.trim()).filter(Boolean);
  }

  function isoOrNull(value) {
    return value ? new Date(value).toISOString() : null;
  }

  function items(result) {
    return Array.isArray(result) ? result : result?.items || [];
  }

  async function collection(path) {
    return items(await api(path));
  }

  async function settled(path, fallback = []) {
    try { return await collection(path); }
    catch { return fallback; }
  }

  function recordOutput(id, record) {
    setHtml(id, `<pre class="control-output">${esc(JSON.stringify(record, null, 2))}</pre>`);
  }

  function localDateTime(value) {
    const shifted = new Date(value.getTime() - value.getTimezoneOffset() * 60000);
    return shifted.toISOString().slice(0, 16);
  }

  function enhanceCompletedIngestionSurface() {
    const ingestionSource = $("#ingestion-source")?.closest("label");
    if (ingestionSource && !$("#ingestion-envelope-version")) {
      ingestionSource.insertAdjacentHTML("afterend", '<label>Envelope<select id="ingestion-envelope-version" name="envelope_version"><option value="1.1">1.1 — views, routines, comments, grants</option><option value="1.0">1.0 — catalog compatibility</option></select></label>');
      const badgeNode = $("#metadata-ingestion-form .panel-heading .status");
      if (badgeNode) badgeNode.textContent = "ENVELOPE 1.1 READY";
      const privacy = $("#metadata-ingestion-form .privacy-note");
      if (privacy) privacy.textContent = "Envelope 1.1 accepts value-free view definitions, routines, source comments, and grants. Sample rows, secrets, credentials, and value-bearing attributes remain rejected.";
    }
    const batchSource = $("#batch-source")?.closest("label");
    if (batchSource && !$("#batch-envelope-version")) batchSource.insertAdjacentHTML("afterend", '<label>Envelope<select id="batch-envelope-version" name="envelope_version"><option value="1.1">1.1</option><option value="1.0">1.0</option></select></label>');
    const now = new Date();
    const periodStart = new Date(now); periodStart.setDate(periodStart.getDate() - 30);
    const certExpiry = new Date(now); certExpiry.setFullYear(certExpiry.getFullYear() + 1);
    const packForm = $("#compliance-pack-form");
    if (packForm) { packForm.elements.period_start.value ||= localDateTime(periodStart); packForm.elements.period_end.value ||= localDateTime(now); }
    const bulkForm = $("#catalog-bulk-form");
    if (bulkForm) bulkForm.elements.expires_at.value ||= localDateTime(certExpiry);
  }

  function configureSelectors() {
    const sourceOptions = selectOptions(state.sources, source => `${source.name} / ${human(source.connector_type)}`, "No sources");
    ["control-catalog-source", "binding-source", "bi-source"].forEach(id => preserveSelect(id, sourceOptions));
    preserveSelect("bi-project", selectOptions(state.projects, project => `${project.name} / ${project.lobName}`, "No projects"));
    const workspaceOptions = selectOptions(controlState.workspaces, workspace => workspace.name, "No workspaces");
    preserveSelect("control-workspace", workspaceOptions);
    const connectionOptions = selectOptions(controlState.biConnections, connection => connection.display_name, "No Tableau connections");
    preserveSelect("bi-connection", connectionOptions);
  }

  function renderCatalog() {
    const runRows = controlState.bulkRuns.map(run => `<tr><td>${esc(human(run.action_type || run.operation || "Action"))}</td><td>${badge(run.status)}</td><td>${esc(run.matched_count ?? run.total_count ?? 0)}</td><td>${esc(run.succeeded_count ?? run.success_count ?? 0)}</td><td>${esc(run.failed_count ?? 0)}</td><td>${when(run.created_at)}</td></tr>`);
    setHtml("catalog-bulk-runs", table(["Action", "Status", "Matched", "Succeeded", "Failed", "Started"], runRows, "No catalog bulk runs yet"));
    const backlog = controlState.unowned.slice(0, 100).map(item => `<div><strong>${esc(item.asset_name || item.display_name || item.subject_id || item.asset_id)}</strong>${badge(item.status || "UNOWNED")}<small>${esc(item.asset_type || item.subject_type || "Asset")} / ${esc(item.datasource_name || item.datasource_id || "Organization scope")}</small></div>`).join("");
    setHtml("unowned-backlog", `<div class="control-summary">${backlog || empty("No unowned assets", "Ownership coverage is clear for the current scope.")}</div>`);
  }

  function renderAccess() {
    configureSelectors();
    const workspaceRows = controlState.workspaces.map(workspace => `<tr><td>${esc(workspace.name)}</td><td>${esc(workspace.slug)}</td><td>${badge(workspace.status)}</td><td>${esc(workspace.monthly_cost_ceiling ?? "Not set")}</td><td><button class="row-action" type="button" data-workspace-select="${workspace.id}">Open</button></td></tr>`);
    setHtml("workspace-list", table(["Workspace", "Slug", "Status", "Cost ceiling", "Action"], workspaceRows, "No workspaces configured"));
    const memberRows = controlState.members.map(member => `<tr><td>${esc(member.principal_id)}</td><td>${esc(human(member.principal_kind))}</td><td>${esc(human(member.role))}</td><td>${when(member.expires_at)}</td></tr>`);
    const bindingRows = controlState.bindings.map(binding => `<tr><td>${esc(state.sources.find(source => source.id === binding.datasource_id)?.name || binding.datasource_id)}</td><td>${badge(binding.status)}</td><td>${esc(binding.masking_profile || "DEFAULT")}</td><td>${when(binding.expires_at)}</td><td>${binding.status === "PENDING" ? `<button class="row-action" type="button" data-binding-decision="${binding.id}" data-decision="APPROVE">Approve</button><button class="row-action" type="button" data-binding-decision="${binding.id}" data-decision="REJECT">Reject</button>` : ""}</td></tr>`);
    setHtml("workspace-detail", `<h3>Members</h3>${table(["Principal", "Kind", "Role", "Expires"], memberRows, "No members")}<h3 class="spaced">Source bindings</h3>${table(["Source", "Status", "Masking", "Expires", "Action"], bindingRows, "No source bindings")}`);
  }

  function renderPolicy() {
    const policyRows = controlState.policies.map(policy => `<tr><td>${esc(policy.name)}</td><td>${esc(policy.policy_key)}</td><td>${badge(policy.effect)}</td><td>${esc(policy.priority)}</td><td>${badge(policy.status || "ACTIVE")}</td></tr>`);
    const decisionRows = controlState.decisions.slice(0, 100).map(decision => `<tr><td>${badge(decision.decision || decision.effect)}</td><td>${esc(decision.subject_id || decision.principal_id || "Attribute request")}</td><td>${esc(decision.resource_id || decision.resource_type || "Resource")}</td><td>${esc((decision.matched_policy_keys || decision.policy_keys || []).join?.(", ") || decision.policy_key || "Default")}</td><td>${when(decision.created_at || decision.decided_at)}</td></tr>`);
    setHtml("abac-policy-list", `<h3>Policies</h3>${table(["Name", "Key", "Effect", "Priority", "Status"], policyRows, "No ABAC policies")}`);
    setHtml("abac-decision-list", `<h3>Decision log</h3>${table(["Decision", "Subject", "Resource", "Policy", "Time"], decisionRows, "No ABAC decisions")}`);
  }

  function renderReliability() {
    const archive = controlState.archive || {};
    setHtml("archive-posture", [["Archive status", human(archive.status || "NO_DATA"), `${archive.total_archives || 0} WORM archives`], ["Events archived", archive.total_events_archived || 0, "Checksum-addressed evidence"], ["Legal holds", archive.legal_hold_count || 0, archive.latest_archive_id ? `Latest ${archive.latest_archive_id}` : "No archive yet"]].map(([label, value, detail]) => `<div class="metric"><p>${esc(label)}</p><strong>${esc(value)}</strong><small>${esc(detail)}</small></div>`).join(""));
    const sloRows = controlState.slos.map(slo => `<tr><td>${esc(slo.name)}</td><td>${esc(slo.target)}%</td><td>${esc(slo.threshold)}%</td><td>${esc(slo.window_days)} days</td><td><button class="row-action" type="button" data-slo-budget="${slo.id}">Budget</button></td></tr>`);
    setHtml("slo-list", table(["SLO", "Target", "Threshold", "Window", "Evidence"], sloRows, "No SLO definitions"));
    const ruleRows = controlState.notificationRules.map(rule => `<tr><td>${esc(rule.name)}</td><td>${badge(rule.channel)}</td><td>${esc((rule.recipients || []).join(", "))}</td><td>${badge(rule.enabled ? "ACTIVE" : "DISABLED")}</td></tr>`);
    setHtml("notification-rule-list", table(["Rule", "Channel", "Recipients", "Status"], ruleRows, "No notification rules"));
  }

  function renderCompliance() {
    const packRows = controlState.packs.map(pack => `<tr><td>${esc(pack.name)}</td><td>${esc(human(pack.framework))}</td><td>${badge(pack.status)}</td><td>${when(pack.generated_at)}</td><td><button class="row-action" type="button" data-pack-detail="${pack.id}">Inspect</button></td></tr>`);
    setHtml("compliance-pack-list", table(["Pack", "Framework", "Status", "Generated", "Evidence"], packRows, "No compliance packs"));
    const refusalRows = controlState.refusals.slice(0, 100).map(refusal => `<div><strong>${esc(refusal.decision_reason || refusal.reason_code || "Governed refusal")}</strong>${badge(refusal.decision_type || "REFUSAL")}<small>${esc(refusal.run_id || refusal.id)} / ${when(refusal.created_at)}</small></div>`).join("");
    setHtml("ai-refusal-list", `<div class="control-summary">${refusalRows || empty("No AI refusals", "No refusal decisions were recorded in this organization.")}</div>`);
  }

  function renderStudio() {
    preserveSelect("studio-change-set", selectOptions(controlState.changeSets, changeSet => `${changeSet.name} / ${human(changeSet.status)}`, "No change sets"));
    const rows = controlState.changeSets.map(changeSet => `<tr><td>${esc(changeSet.name)}</td><td>${badge(changeSet.status)}</td><td>${badge(changeSet.conflict_status)}</td><td>${esc(changeSet.author)}</td><td>${when(changeSet.created_at)}</td><td><button class="row-action" type="button" data-studio-select="${changeSet.id}">Open</button></td></tr>`);
    setHtml("studio-list", table(["Change set", "Status", "Conflicts", "Author", "Created", "Action"], rows, "No Studio change sets"));
  }

  function renderBi() {
    configureSelectors();
    const rows = controlState.biConnections.map(connection => `<tr><td>${esc(connection.display_name)}</td><td>${badge(connection.bi_tool)}</td><td>${esc(connection.connection_key)}</td><td>${esc(state.sources.find(source => source.id === connection.datasource_id)?.name || connection.datasource_id)}</td><td>${badge(connection.status)}</td><td><button class="row-action" type="button" data-bi-select="${connection.id}">Use</button></td></tr>`);
    setHtml("bi-connection-list", table(["Connection", "Tool", "Key", "Catalog source", "Status", "Action"], rows, "No BI connections for this project"));
  }

  async function loadWorkspaceDetail() {
    const workspaceId = $("#control-workspace")?.value;
    if (!workspaceId) { controlState.members = []; controlState.bindings = []; return renderAccess(); }
    [controlState.members, controlState.bindings] = await Promise.all([
      settled(`/v1/workspaces/${workspaceId}/members?limit=200&offset=0`),
      settled(`/v1/workspaces/${workspaceId}/source-bindings?limit=200&offset=0`),
    ]);
    renderAccess();
  }

  async function loadBiConnections() {
    const projectId = $("#bi-project")?.value || state.projects[0]?.id;
    controlState.biConnections = projectId ? await settled(`/v1/projects/${projectId}/bi-connections?limit=200&offset=0`) : [];
    renderBi();
  }

  async function loadControlCenter() {
    if (!state.organizationId) return;
    configureSelectors();
    const org = encodeURIComponent(state.organizationId);
    const [bulkRuns, unowned, workspaces, policies, decisions, slos, notificationRules, archive, packs, refusals, changeSets] = await Promise.all([
      settled(`/v1/organizations/${org}/catalog-bulk-actions?limit=100&offset=0`),
      settled(`/v1/organizations/${org}/stewardship/unowned-backlog?limit=100&offset=0`),
      settled(`/v1/organizations/${org}/workspaces?limit=200&offset=0`),
      settled(`/v1/abac/policies?organization_id=${org}&limit=200&offset=0`),
      settled(`/v1/abac/decisions?organization_id=${org}&limit=100&offset=0`),
      settled("/v1/observability/slo?limit=200&offset=0"),
      settled("/v1/notification-rules?limit=200&offset=0"),
      api("/v1/observability/archive/status").catch(() => null),
      settled("/v1/compliance/packs?limit=100&offset=0"),
      settled(`/v1/ai-decisions/refusals?organization_id=${org}&limit=100&offset=0`),
      settled("/v1/studio/change-sets?limit=200&offset=0"),
    ]);
    Object.assign(controlState, { bulkRuns, unowned, workspaces, policies, decisions, slos, notificationRules, archive, packs, refusals, changeSets });
    renderCatalog(); renderAccess(); renderPolicy(); renderReliability(); renderCompliance(); renderStudio();
    await Promise.all([loadWorkspaceDetail(), loadBiConnections()]);
  }

  async function submit(form, action, successText) {
    try { await action(new FormData(form)); message(successText, true); await loadControlCenter(); }
    catch (error) { message(error.message); }
  }

  function bindEvents() {
    enhanceCompletedIngestionSurface();
    $$("[data-control-tab]").forEach(button => button.addEventListener("click", () => {
      $$("[data-control-tab]").forEach(tab => { const active = tab === button; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1; });
      $$(".control-pane").forEach(pane => pane.classList.toggle("active", pane.id === `control-${button.dataset.controlTab}`));
    }));
    $("#control-refresh")?.addEventListener("click", () => loadControlCenter().then(() => message("Control evidence refreshed.", true)).catch(error => message(error.message)));
    $("#control-workspace")?.addEventListener("change", () => loadWorkspaceDetail().catch(error => message(error.message)));
    $("#bi-project")?.addEventListener("change", () => loadBiConnections().catch(error => message(error.message)));

    $("#catalog-bulk-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => {
      const action = data.get("action");
      const filter = { datasource_id: data.get("datasource_id"), match_field: data.get("match_field"), match_pattern: data.get("match_pattern") };
      const bodies = {
        "bulk-tag": { filter, tag_key: data.get("tag_key"), tag_value: data.get("tag_value") || null },
        "bulk-classify": { filter, column_name_pattern: data.get("column_name_pattern") || "*", classification: data.get("classification") },
        "bulk-own": { filter, owner_type: data.get("owner_type"), owner_principal: data.get("owner_principal") },
        "bulk-certify": { filter, rationale: data.get("rationale"), expires_at: isoOrNull(data.get("expires_at")) },
      };
      await api(`/v1/organizations/${state.organizationId}/tables/${action}`, { method: "POST", body: JSON.stringify(bodies[action]) });
    }, "Catalog bulk action completed with per-item evidence."); });
    $("#route-unowned")?.addEventListener("click", () => submit($("#catalog-bulk-form"), async data => api(`/v1/organizations/${state.organizationId}/stewardship/unowned-backlog/route`, { method: "POST", body: JSON.stringify({ datasource_id: data.get("datasource_id") || null }) }), "Unowned assets routed through notification and escalation controls."));

    $("#workspace-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api(`/v1/organizations/${state.organizationId}/workspaces`, { method: "POST", body: JSON.stringify({ name: data.get("name"), slug: data.get("slug"), purpose: data.get("purpose") || "", isolation_boundary_id: null, monthly_cost_ceiling: data.get("monthly_cost_ceiling") === "" ? null : Number(data.get("monthly_cost_ceiling")) }) }), "Workspace created."); });
    $("#workspace-member-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api(`/v1/workspaces/${$("#control-workspace").value}/members`, { method: "POST", body: JSON.stringify({ principal_id: data.get("principal_id"), principal_kind: data.get("principal_kind"), role: data.get("role"), expires_at: isoOrNull(data.get("expires_at")) }) }), "Workspace member added."); });
    $("#source-binding-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api(`/v1/workspaces/${$("#control-workspace").value}/source-bindings`, { method: "POST", body: JSON.stringify({ datasource_id: data.get("datasource_id"), purpose: data.get("purpose"), schema_scope: csv(data.get("schema_scope")), permitted_classifications: csv(data.get("permitted_classifications")), masking_profile: data.get("masking_profile") || "DEFAULT", max_query_cost: data.get("max_query_cost") === "" ? null : Number(data.get("max_query_cost")) }) }), "Source binding submitted for independent review."); });

    $("#abac-policy-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api("/v1/abac/policies", { method: "POST", body: JSON.stringify({ policy_key: data.get("policy_key"), name: data.get("name"), description: data.get("description"), effect: data.get("effect"), subject_conditions: parseJson(data.get("subject_conditions"), "Subject conditions"), resource_conditions: parseJson(data.get("resource_conditions"), "Resource conditions"), environment_conditions: parseJson(data.get("environment_conditions"), "Environment conditions"), priority: Number(data.get("priority")) }) }), "ABAC policy created."); });
    $("#abac-simulate-form")?.addEventListener("submit", async event => { event.preventDefault(); const data = new FormData(event.target); try { const result = await api("/v1/abac/simulate", { method: "POST", body: JSON.stringify({ subject_attributes: parseJson(data.get("subject_attributes"), "Subject attributes"), resource_attributes: parseJson(data.get("resource_attributes"), "Resource attributes"), environment_attributes: parseJson(data.get("environment_attributes"), "Environment attributes"), vary_subject_attributes: [] }) }); recordOutput("abac-simulation-result", result); message("Policy simulation completed without changing access.", true); } catch (error) { message(error.message); } });

    $("#slo-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api("/v1/observability/slo", { method: "POST", body: JSON.stringify({ slo_key: data.get("slo_key"), name: data.get("name"), target: Number(data.get("target")), window_days: Number(data.get("window_days")), threshold: Number(data.get("threshold")) }) }), "SLO definition created."); });
    $("#notification-rule-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api("/v1/notification-rules", { method: "POST", body: JSON.stringify({ name: data.get("name"), conditions: parseJson(data.get("conditions"), "Conditions"), channel: data.get("channel"), recipients: csv(data.get("recipients")), escalation_after_minutes: data.get("escalation_after_minutes") === "" ? null : Number(data.get("escalation_after_minutes")), enabled: data.get("enabled") === "on" }) }), "Notification rule created."); });
    $("#contract-inspect-form")?.addEventListener("submit", async event => { event.preventDefault(); const id = new FormData(event.target).get("contract_id"); try { const [evaluation, sla, violations] = await Promise.all([api(`/v1/data-contracts/${id}/evaluate`, { method: "POST" }), api(`/v1/data-contracts/${id}/sla-status`), api(`/v1/data-contracts/${id}/violations?limit=100&offset=0`)]); recordOutput("contract-evidence", { evaluation, sla, violations }); message("Contract evaluation completed.", true); } catch (error) { message(error.message); } });

    $("#compliance-pack-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api("/v1/compliance/packs/generate", { method: "POST", body: JSON.stringify({ framework: data.get("framework"), period_start: isoOrNull(data.get("period_start")), period_end: isoOrNull(data.get("period_end")), name: data.get("name") || null }) }), "Compliance pack generated and archived."); });
    $("#studio-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api("/v1/studio/change-sets", { method: "POST", body: JSON.stringify({ name: data.get("name") }) }), "Studio change set created."); });
    $("#studio-item-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api(`/v1/studio/change-sets/${$("#studio-change-set").value}/items`, { method: "POST", body: JSON.stringify({ object_type: data.get("object_type"), object_id: data.get("object_id"), operation: data.get("operation"), before_snapshot: parseJson(data.get("before_snapshot"), "Before snapshot"), after_snapshot: parseJson(data.get("after_snapshot"), "After snapshot") }) }), "Change item added."); });

    $("#tool-plan-form")?.addEventListener("submit", async event => { event.preventDefault(); const data = new FormData(event.target); try { const plan = await api("/v1/tool-plans", { method: "POST", body: JSON.stringify({ name: data.get("name"), steps: [{ sequence: 1, tool_id: data.get("tool_id"), tool_version: data.get("tool_version"), parameters: parseJson(data.get("parameters"), "Parameters"), dependencies: [], timeout_seconds: Number(data.get("timeout_seconds")), expected_cost: Number(data.get("expected_cost")) }], budget: { max_steps: 20, max_time_seconds: Number(data.get("max_time_seconds")), max_tokens: 100000, max_cost_units: Number(data.get("max_cost_units")) } }) }); controlState.activePlanId = plan.id; recordOutput("tool-plan-result", plan); message("Tool plan created. Validate it before execution.", true); } catch (error) { message(error.message); } });

    $("#bi-connection-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api(`/v1/projects/${data.get("project_id")}/bi-connections`, { method: "POST", body: JSON.stringify({ datasource_id: data.get("datasource_id"), bi_tool: "TABLEAU", connection_key: data.get("connection_key"), display_name: data.get("display_name"), site_or_workspace: data.get("site_or_workspace") || null }) }), "Tableau metadata connection registered."); });
    $("#bi-import-form")?.addEventListener("submit", event => { event.preventDefault(); submit(event.target, async data => api(`/v1/bi-connections/${data.get("connection_id")}/artifact-imports`, { method: "POST", body: JSON.stringify({ bi_tool: "TABLEAU", artifact: parseJson(data.get("artifact"), "Artifact") }) }), "BI lineage artifact imported."); });

    document.addEventListener("click", async event => {
      const workspace = event.target.closest("[data-workspace-select]"); if (workspace) { $("#control-workspace").value = workspace.dataset.workspaceSelect; return loadWorkspaceDetail(); }
      const binding = event.target.closest("[data-binding-decision]"); if (binding) { try { await api(`/v1/source-bindings/${binding.dataset.bindingDecision}/decision`, { method: "POST", principal: "local-ui-checker", body: JSON.stringify({ decision: binding.dataset.decision, valid_for_days: 365, rationale: "Independent workspace source review" }) }); message(`${human(binding.dataset.decision)} decision recorded.`, true); await loadWorkspaceDetail(); } catch (error) { message(error.message); } return; }
      const budget = event.target.closest("[data-slo-budget]"); if (budget) { try { recordOutput("slo-list", await api(`/v1/observability/slo/${budget.dataset.sloBudget}/budget`)); } catch (error) { message(error.message); } return; }
      const pack = event.target.closest("[data-pack-detail]"); if (pack) { try { recordOutput("compliance-pack-list", await api(`/v1/compliance/packs/${pack.dataset.packDetail}/download`)); } catch (error) { message(error.message); } return; }
      const studioSelect = event.target.closest("[data-studio-select]"); if (studioSelect) { $("#studio-change-set").value = studioSelect.dataset.studioSelect; return; }
      const studioAction = event.target.closest("[data-studio-action]"); if (studioAction) { const id = $("#studio-change-set").value; if (!id) return message("Choose a change set first."); const action = studioAction.dataset.studioAction; const method = ["diff", "impact"].includes(action) ? "GET" : "POST"; try { const result = await api(`/v1/studio/change-sets/${id}/${action}`, { method }); recordOutput("studio-evidence", result); message(`${human(action)} completed.`, true); if (["test", "submit"].includes(action)) await loadControlCenter(); } catch (error) { message(error.message); } return; }
      const bi = event.target.closest("[data-bi-select]"); if (bi) { $("#bi-connection").value = bi.dataset.biSelect; return; }
    });

    [["plan-validate", "validate", "POST"], ["plan-execute", "execute", "POST"], ["plan-evidence", "evidence", "GET"], ["plan-cancel", "cancel", "POST"]].forEach(([id, action, method]) => $("#" + id)?.addEventListener("click", async () => { if (!controlState.activePlanId) return message("Create a plan first."); try { const result = await api(`/v1/tool-plans/${controlState.activePlanId}/${action}`, { method }); recordOutput("tool-plan-result", result); message(`Plan ${action} completed.`, true); } catch (error) { message(error.message); } }));
  }

  Object.assign(window.AtlasUI, { loadControlCenter });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bindEvents); else bindEvents();
})();
