/* Transformation adapter policy is a separate workspace concern. */
(function initializeIntegrationPolicyFeature() {
  const { state, $, setHtml, esc, badge, empty, api } = window.AtlasUI;

  const integrationFlags = () => state.integrationPolicy?.transformation_metadata_integrations || {};
  const dbtEnabled = () => Boolean(integrationFlags().dbt);
  const transformationMetadataSurfaceEnabled = () => Object.values(integrationFlags()).some(Boolean);

  function renderTransformationOverview() {
    const integrations = integrationFlags();
    const entries = [
      ["dbt", "dbt manifest", integrations.dbt, "Implemented workbench"],
      ["openlineage", "OpenLineage", integrations.openlineage, "Adapter reserved"],
      ["airflow", "Airflow lineage", integrations.airflow, "Adapter reserved"],
      ["generic_elt", "Generic ETL or ELT", integrations.generic_elt, "Adapter reserved"]
    ];
    setHtml("transformation-integration-summary", [
      ["Enabled adapters", entries.filter(([, , enabled]) => enabled).length, "Organization-scoped metadata surfaces"],
      ["Implemented adapters", entries.filter(([key, , enabled]) => enabled && ["dbt", "openlineage"].includes(key)).length, "Live ingestion or workbench coverage"],
      ["Reserved adapters", entries.filter(([key, , enabled]) => enabled && !["dbt", "openlineage"].includes(key)).length, "Future metadata adapters can bind here"],
      ["Execution boundary", "External", "Atlas ingests metadata evidence only"]
    ].map(([label, value, detail]) => `<div class="metric"><p>${label}</p><strong>${value}</strong><small>${detail}</small></div>`).join(""));
    setHtml("transformation-adapters", entries.map(([key, label, enabled, detail]) => `<div class="estate-row"><div><strong>${esc(label)}</strong><small>${esc(key)}</small></div><div>${badge(enabled ? (["dbt", "openlineage"].includes(key) ? "IMPLEMENTED" : "PLANNED") : "NOT_CONFIGURED")}</div><div><small>${esc(enabled ? detail : "Disabled for this organization")}</small></div></div>`).join(""));
    setHtml("transformation-workbenches", [
      `<div class="estate-row"><div><strong>dbt workbench</strong><small>Manifest import, catalog matching, DAG lineage</small></div><div>${badge(integrations.dbt ? "READY" : "DISABLED")}</div><div><small>${esc(integrations.dbt ? "Available in this workspace" : "Enable dbt in Administration to expose it")}</small></div></div>`,
      `<div class="estate-row"><div><strong>OpenLineage intake</strong><small>Run events, jobs, inputs, outputs, column lineage</small></div><div>${badge(integrations.openlineage ? "READY" : "NOT_CONFIGURED")}</div><div><small>${esc(integrations.openlineage ? "API and UI ingestion are live" : "Disabled for this organization")}</small></div></div>`,
      `<div class="estate-row"><div><strong>Airflow lineage</strong><small>Scheduler or DAG metadata mapped into lineage</small></div><div>${badge(integrations.airflow ? "PLANNED" : "NOT_CONFIGURED")}</div><div><small>${esc(integrations.airflow ? "Reserved policy slot; adapter not implemented yet" : "Disabled for this organization")}</small></div></div>`,
      `<div class="estate-row"><div><strong>Generic ELT adapter</strong><small>External transformation metadata normalized into Atlas evidence</small></div><div>${badge(integrations.generic_elt ? "PLANNED" : "NOT_CONFIGURED")}</div><div><small>${esc(integrations.generic_elt ? "Reserved policy slot; normalization contract comes next" : "Disabled for this organization")}</small></div></div>`
    ].join(""));
  }

  function renderDbtDisabledState() {
    setHtml("dbt-metrics", [["dbt workbench", "Disabled", "Enable dbt in Administration to ingest manifest metadata"]].map(([label, value, detail]) => `<div class="metric"><p>${label}</p><strong>${value}</strong><small>${detail}</small></div>`).join(""));
    setHtml("dbt-projects-table", empty("dbt integration is disabled", "This workspace hides dbt project registration until an administrator enables it."));
    setHtml("dbt-imports-table", empty("No dbt imports available", "Enable dbt first if you want to ingest manifest metadata."));
    setHtml("dbt-resources-table", empty("dbt workbench is disabled"));
    setHtml("dbt-lineage", empty("No dbt lineage available", "This section activates only when dbt integration is enabled for the current organization."));
    setHtml("dbt-lineage-status", badge("DISABLED"));
  }

  function renderIntegrationPolicy() {
    const form = $("#integration-policy-form");
    if (!form) return;
    const integrations = integrationFlags();
    form.elements.dbt.checked = Boolean(integrations.dbt);
    form.elements.openlineage.checked = Boolean(integrations.openlineage);
    form.elements.airflow.checked = Boolean(integrations.airflow);
    form.elements.generic_elt.checked = Boolean(integrations.generic_elt);
    setHtml("integration-policy-status", badge(Object.values(integrations).some(Boolean) ? "ACTIVE" : "NOT_CONFIGURED"));
    renderTransformationOverview();
  }

  function applyIntegrationPolicyVisibility() {
    const enabled = transformationMetadataSurfaceEnabled();
    $(".nav-item[data-view='transformations']")?.classList.toggle("integration-hidden", !enabled);
    if (!enabled && location.hash.slice(1) === "transformations") history.replaceState(null, "", "#administration");
    if (!enabled && $("#transformations-view")?.classList.contains("active")) window.AtlasUI.navigateTo?.("administration");
  }

  async function loadIntegrationPolicy() {
    if (!state.organizationId) {
      state.integrationPolicy = null;
    } else {
      state.integrationPolicy = await api(`/v1/organizations/${state.organizationId}/integration-policy`);
    }
    renderIntegrationPolicy();
    applyIntegrationPolicyVisibility();
  }

  Object.assign(window.AtlasUI, {
    integrationFlags, dbtEnabled, transformationMetadataSurfaceEnabled,
    renderTransformationOverview, renderDbtDisabledState, renderIntegrationPolicy,
    applyIntegrationPolicyVisibility, loadIntegrationPolicy
  });
})();
