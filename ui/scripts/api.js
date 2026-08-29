/* HTTP transport is isolated so feature modules share one authentication contract. */
(function initializeAtlasApi() {
  const { state, roles } = window.AtlasUI;

  function baseHeaders(principal="local-ui-admin") {
    return {"X-Principal-Id": principal, "X-Roles": roles, ...(state.organizationId ? {"X-Organization-Id": state.organizationId} : {})};
  }

  async function api(path, options={}) {
    const response = await fetch(`/api${path}`, {
      ...options,
      headers: {...baseHeaders(options.principal), ...(options.body ? {"Content-Type":"application/json"} : {}), ...(options.headers || {})}
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map(item => item.msg || JSON.stringify(item)).join("; ") : data.detail;
      const error = new Error(detail || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async function fetchAll(path, maximum=10000, pageLimit=100) {
    const items = [];
    for (let offset=0; offset<maximum; offset+=pageLimit) {
      const join = path.includes("?") ? "&" : "?";
      const page = await api(`${path}${join}limit=${pageLimit}&offset=${offset}`);
      items.push(...page.items);
      if (items.length >= page.total || !page.items.length) break;
    }
    return items;
  }

  Object.assign(window.AtlasUI, { baseHeaders, api, fetchAll });
})();
