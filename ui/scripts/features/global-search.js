/* Global search with server-side typeahead, facets, and recent searches (RT-5 / UX-2). */
(function initializeGlobalSearch() {
  const { state, $, $$, setHtml, esc, api, fetchAll } = window.AtlasUI;

  const RECENT_KEY = "aida-recent-searches";
  const MAX_RECENT = 8;
  const DEBOUNCE_MS = 250;

  let debounceTimer = null;
  let activeFacet = "all";
  let serverResults = [];
  let lastQuery = "";

  /* ---- Recent searches (localStorage with try/catch) ---- */

  function loadRecent() {
    try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]").slice(0, MAX_RECENT); }
    catch { return []; }
  }

  function saveRecent(query) {
    if (!query || !query.trim()) return;
    const trimmed = query.trim();
    try {
      const list = loadRecent().filter(item => item !== trimmed);
      list.unshift(trimmed);
      localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, MAX_RECENT)));
    } catch { /* storage unavailable */ }
  }

  function clearRecent() {
    try { localStorage.removeItem(RECENT_KEY); } catch { /* ok */ }
  }

  /* ---- Search API wrappers ---- */

  async function searchGlobal(query, limit = 40, offset = 0) {
    if (!state.organizationId) return { items: [], total: 0 };
    try {
      return await api(`/v1/organizations/${state.organizationId}/search?q=${encodeURIComponent(query)}&limit=${limit}&offset=${offset}`);
    } catch {
      return { items: [], total: 0 };
    }
  }

  async function searchSuggest(query, limit = 10) {
    if (!state.organizationId) return { items: [] };
    try {
      return await api(`/v1/organizations/${state.organizationId}/search/suggest?q=${encodeURIComponent(query)}&limit=${limit}`);
    } catch {
      return { items: [] };
    }
  }

  /* ---- Facet computation ---- */

  function computeFacets(entries) {
    const counts = {};
    entries.forEach(entry => {
      const type = (entry.type || "other").toLowerCase();
      counts[type] = (counts[type] || 0) + 1;
    });
    return counts;
  }

  function renderFacets(entries) {
    const counts = computeFacets(entries);
    const types = Object.keys(counts).sort();
    if (!types.length) return "";
    const allCount = entries.length;
    let html = '<div class="palette-facets" role="tablist" aria-label="Result facets">';
    html += `<button type="button" class="palette-facet ${activeFacet === "all" ? "active" : ""}" role="tab" aria-selected="${activeFacet === "all"}" data-facet="all">All<span class="palette-facet-count">${allCount}</span></button>`;
    types.forEach(type => {
      const active = activeFacet === type;
      html += `<button type="button" class="palette-facet ${active ? "active" : ""}" role="tab" aria-selected="${active}" data-facet="${esc(type)}">${esc(type.charAt(0).toUpperCase() + type.slice(1))}<span class="palette-facet-count">${counts[type]}</span></button>`;
    });
    html += '</div>';
    return html;
  }

  function applyFacetFilter(entries) {
    if (activeFacet === "all") return entries;
    return entries.filter(entry => (entry.type || "other").toLowerCase() === activeFacet);
  }

  /* ---- Highlight matches ---- */

  function highlight(text, query) {
    if (!query) return esc(text);
    const escaped = esc(text);
    const q = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return escaped.replace(new RegExp(`(${q})`, "gi"), "<mark>$1</mark>");
  }

  /* ---- Rendering ---- */

  function renderResults(entries, query) {
    const filtered = applyFacetFilter(entries);
    state.paletteEntries = filtered;
    state.paletteActiveIndex = filtered.length ? 0 : -1;

    const facetHtml = renderFacets(entries);
    const listHtml = filtered.length
      ? filtered.map((entry, index) =>
          `<button type="button" id="palette-option-${index}" role="option" aria-selected="${index === 0}" class="palette-row ${index === 0 ? "active" : ""}" data-palette-index="${index}"><span class="palette-type">${esc(entry.type)}</span><span class="palette-label">${highlight(entry.label, query)}</span><span class="palette-hint">${esc(entry.hint || "")}</span></button>`
        ).join("")
      : '<div class="palette-empty"><strong>No matches</strong>Try a different search term, view, table, tool, source, or model name.</div>';

    const container = $("#palette-results");
    if (!container) return;

    /* Facets go before the listbox */
    const wrapper = container.closest(".command-palette") || container.parentNode;
    let facetContainer = wrapper.querySelector(".palette-facets");
    if (facetContainer) facetContainer.remove();
    if (facetHtml) {
      container.insertAdjacentHTML("beforebegin", facetHtml);
    }
    container.innerHTML = listHtml;
    $("#palette-input")?.setAttribute("aria-activedescendant", filtered.length ? "palette-option-0" : "");
  }

  function renderRecentSearches() {
    const recent = loadRecent();
    if (!recent.length) {
      return;
    }
    let html = '<div class="palette-section-title">Recent searches<button type="button" class="palette-recent-clear" data-action="clear-recent">Clear</button></div>';
    recent.forEach(term => {
      html += `<button type="button" class="palette-row" data-recent-query="${esc(term)}"><span class="palette-type">Recent</span><span class="palette-label">${esc(term)}</span><span class="palette-hint"></span></button>`;
    });
    const container = $("#palette-results");
    if (container) container.innerHTML = html;
  }

  /* ---- Server-backed search with debounce ---- */

  function scheduleServerSearch(query) {
    if (debounceTimer) clearTimeout(debounceTimer);
    if (!query.trim()) return;

    const container = $("#palette-results");
    if (container && !container.querySelector(".palette-loading")) {
      container.insertAdjacentHTML("beforeend", '<div class="palette-loading">Searching...</div>');
    }

    debounceTimer = setTimeout(async () => {
      lastQuery = query.trim();
      const response = await searchSuggest(lastQuery);
      if (response.items && response.items.length) {
        serverResults = response.items.map(item => ({
          type: item.resource_type || item.type || "Result",
          label: item.name || item.display_name || item.label || "Unknown",
          hint: item.description || item.hint || "",
          action: item.action_uri ? () => navigateToResult(item) : null,
          _raw: item
        }));
      } else {
        serverResults = [];
      }
    }, DEBOUNCE_MS);
  }

  function navigateToResult(item) {
    /* Best-effort navigation based on resource type */
    const type = (item.resource_type || item.type || "").toLowerCase();
    if (type === "table" || type === "metadata_table") {
      if (typeof window.showView === "function") window.showView("catalog");
      if (typeof window.showTable === "function" && item.id) window.showTable(item.id);
    } else if (type === "source" || type === "datasource") {
      if (typeof window.showView === "function") window.showView("sources");
    } else if (type === "tool" || type === "governed_tool") {
      if (typeof window.showView === "function") window.showView("tools");
      if (typeof window.selectTool === "function" && item.id) window.selectTool(item.id);
    } else if (type === "semantic_model") {
      if (typeof window.showView === "function") window.showView("semantics");
      if (typeof window.selectSemantic === "function" && item.id) window.selectSemantic(item.id);
    } else if (type === "glossary_term") {
      if (typeof window.showView === "function") window.showView("meaning");
    } else if (type === "dbt_project") {
      if (typeof window.showView === "function") window.showView("transformations");
    }
  }

  /* ---- Event wiring ---- */

  function bindGlobalSearchEvents() {
    /* Facet clicks */
    document.addEventListener("click", event => {
      const facetButton = event.target.closest("[data-facet]");
      if (facetButton) {
        activeFacet = facetButton.dataset.facet;
        const query = $("#palette-input")?.value || "";
        /* Re-render with current entries */
        renderResults(state.paletteEntries.length ? getAllCurrentEntries() : [], query);
        return;
      }

      /* Recent search click */
      const recentButton = event.target.closest("[data-recent-query]");
      if (recentButton) {
        const term = recentButton.dataset.recentQuery;
        const input = $("#palette-input");
        if (input) { input.value = term; input.dispatchEvent(new Event("input")); }
        return;
      }

      /* Clear recent */
      const clearButton = event.target.closest("[data-action='clear-recent']");
      if (clearButton) {
        clearRecent();
        renderRecentSearches();
        return;
      }
    });

    /* Record search term when palette closes with a selection */
    const palette = $("#command-palette");
    if (palette) {
      palette.addEventListener("close", () => {
        const query = $("#palette-input")?.value;
        if (query && query.trim()) {
          saveRecent(query.trim());
        }
        activeFacet = "all";
        serverResults = [];
      });
    }

    /* Show recent searches when palette opens empty */
    const trigger = $("#palette-trigger");
    if (trigger) {
      const originalClick = trigger.onclick;
      trigger.addEventListener("click", () => {
        window.requestAnimationFrame(() => {
          const input = $("#palette-input");
          if (input && !input.value.trim()) {
            renderRecentSearches();
          }
        });
      });
    }

    /* Debounced server search on input */
    const input = $("#palette-input");
    if (input) {
      input.addEventListener("input", () => {
        const query = input.value;
        if (query.trim().length >= 2) {
          scheduleServerSearch(query);
        }
      });
    }
  }

  function getAllCurrentEntries() {
    /* Collect the full, unfiltered entry set */
    return state._allPaletteEntries || state.paletteEntries || [];
  }

  /* ---- Exports ---- */

  Object.assign(window.AtlasUI, {
    searchGlobal,
    searchSuggest,
    bindGlobalSearchEvents,
    renderRecentSearches,
    saveRecentSearch: saveRecent,
    clearRecentSearches: clearRecent,
  });

  /* Auto-bind on DOMContentLoaded */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindGlobalSearchEvents);
  } else {
    bindGlobalSearchEvents();
  }
})();
