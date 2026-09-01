/*
 * Shared graph rendering engine for Atlas's four lineage/relationship
 * surfaces (Knowledge graph, Transformations DAG, Unified lineage, AI
 * dependency graph).
 *
 * Wraps Cytoscape.js (vendored in /vendor, no runtime CDN calls) with:
 *  - a dagre-based layered layout (falls back to cose if dagre is missing)
 *  - real pan / zoom / drag interaction instead of a fixed grid or ring
 *  - rich HTML node cards (via cytoscape-node-html-label) so existing
 *    card markup/behavior (data-* click targets) keeps working unchanged
 *  - styled, directional edges per relationship type
 *  - a lightweight built-in minimap and search dim/highlight
 *  - LN-8: viewport-based virtualization of the HTML card layer (see
 *    "Large-DAG virtualization" below)
 *
 * Each view supplies plain node/edge objects and an HTML template
 * function; this module owns layout, chrome, and interaction only.
 *
 * Large-DAG virtualization (LN-8)
 * --------------------------------
 * Cytoscape itself draws node shapes and edges on a <canvas>, which stays
 * cheap regardless of graph size (drawing pixels, not DOM). The actual
 * "full graph render" cost lives entirely in cytoscape-node-html-label:
 * every node gets one real `<div>` (the rich, clickable card) mounted
 * unconditionally, with no notion of viewport. At the platform's own
 * bounded maxima (`node_limit` up to 4,000 / `edge_limit` up to 20,000 on
 * `unified_lineage_api.py`'s full-graph route), that means thousands of
 * cards mounted at once -- including on first load, since `runLayout()`
 * fits the whole graph into view by default.
 *
 * This module keeps cytoscape-node-html-label's plugin but drives its
 * membership dynamically instead of statically: the label query is
 * `node[agWindowed]` (a boolean data flag cytoscape's own `[foo]`
 * selector treats as "truthy"), and `_computeWindowedIds()` recomputes,
 * on every pan/zoom/layout settle, which nodes are inside the current
 * viewport extent (plus an overscan margin for smooth panning) and mounts
 * an HTML card only for those -- capped at `_htmlWindowCap` regardless of
 * how many nodes are nominally "in view" (covers the fit-all-nodes case,
 * where the whole bounded graph is visible at once after a big load).
 * Nodes outside the window fall back to a plain, uniform canvas-drawn
 * rectangle (no DOM, no per-node styling decision) so pan/zoom still
 * shows the graph's shape while cards lazily mount as you approach them.
 * This is windowing of the render budget only -- it does not cluster,
 * aggregate, or otherwise simplify the graph (that is KG-3's still-open
 * level-of-detail work); every node keeps its real position and every
 * edge is still drawn.
 */
(function initializeAtlasGraphEngine() {
  function cytoAvailable() {
    return typeof window.cytoscape === "function";
  }

  function dagreAvailable() {
    try {
      return cytoAvailable() && Boolean(window.cytoscape("layout", "dagre"));
    } catch (error) {
      return false;
    }
  }

  const EDGE_STYLES = [
    { selector: "edge", style: {
        "curve-style": "bezier",
        "width": 1.6,
        "line-color": "#93a4b8",
        "target-arrow-color": "#93a4b8",
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.85,
        "opacity": 0.85
    }},
    { selector: "edge.declared, edge.fk, edge.approved", style: {
        "line-color": "#3f6fa8", "target-arrow-color": "#3f6fa8", "width": 2.1
    }},
    { selector: "edge.suggested, edge.pending", style: {
        "line-color": "#c78626", "target-arrow-color": "#c78626", "line-style": "dashed", "width": 1.7
    }},
    { selector: "edge.rejected", style: {
        "line-color": "#c3cad3", "target-arrow-color": "#c3cad3", "line-style": "dotted", "opacity": 0.45
    }},
    { selector: "edge.dbt", style: {
        "line-color": "#157c65", "target-arrow-color": "#157c65", "width": 2.1
    }},
    { selector: "edge.openlineage", style: {
        "line-color": "#245fbd", "target-arrow-color": "#245fbd", "width": 2.1
    }},
    { selector: "edge.ag-selected", style: { "width": 3.6, "opacity": 1, "z-index": 50 } },
    { selector: "edge.ag-dim", style: { "opacity": 0.06 } }
  ];

  // LN-8: default cap on simultaneously-mounted HTML node cards, independent
  // of total graph size or zoom level -- bounds the fit-all-nodes case, not
  // just off-screen panning. Callers may override via opts.htmlWindowCap.
  const DEFAULT_HTML_WINDOW_CAP = 220;
  // Extra viewport margin (as a fraction of the current viewport's own
  // width/height) kept "windowed in" beyond the visible extent, so cards
  // are already mounted just before they pan into view.
  const WINDOW_OVERSCAN_RATIO = 0.35;

  // LN-8: pure windowing decision, deliberately free of any Cytoscape/DOM
  // dependency so it can run (and be unit-tested, see
  // graph-engine.virtualization.test.mjs) outside a browser.
  //
  // `nodeBoxes` is a plain array of `{id, x, y, w, h}` (model-space center
  // position and size, the same units `cy.extent()`/`node.position()` use).
  // `extent` is `{x1, y1, x2, y2, w, h}`, the current viewport rectangle in
  // those same model-space units. Returns the `Set` of node ids that should
  // have an HTML card mounted: those whose bounding box intersects the
  // extent (padded by `overscanRatio` of the viewport's own size), plus
  // `pinnedId` unconditionally -- capped at `cap` total, nearest-to-center
  // first, so a graph far larger than the cap (including "every node is
  // nominally in view" after a full-graph fit) never mounts more than `cap`
  // cards at once.
  function computeWindowedNodeIds(nodeBoxes, extent, options = {}) {
    const cap = Math.max(1, options.cap || DEFAULT_HTML_WINDOW_CAP);
    const overscanRatio = options.overscanRatio == null ? WINDOW_OVERSCAN_RATIO : options.overscanRatio;
    const pinnedId = options.pinnedId || null;
    const result = new Set();
    if (!nodeBoxes || !nodeBoxes.length) return result;

    const padX = Math.max(1, extent.w) * overscanRatio;
    const padY = Math.max(1, extent.h) * overscanRatio;
    const bx1 = extent.x1 - padX, bx2 = extent.x2 + padX;
    const by1 = extent.y1 - padY, by2 = extent.y2 + padY;
    const cx = (extent.x1 + extent.x2) / 2, cyMid = (extent.y1 + extent.y2) / 2;

    const candidates = [];
    nodeBoxes.forEach(n => {
      const isPinned = n.id === pinnedId;
      const intersects = (n.x + n.w / 2) >= bx1 && (n.x - n.w / 2) <= bx2
        && (n.y + n.h / 2) >= by1 && (n.y - n.h / 2) <= by2;
      if (!intersects && !isPinned) return;
      const dx = n.x - cx, dy = n.y - cyMid;
      candidates.push({ id: n.id, pinned: isPinned, dist: dx * dx + dy * dy });
    });

    candidates.sort((a, b) => (b.pinned - a.pinned) || (a.dist - b.dist));
    candidates.slice(0, cap).forEach(c => result.add(c.id));
    return result;
  }

  class AtlasGraph {
    constructor(mountEl, opts = {}) {
      this.el = typeof mountEl === "string" ? document.getElementById(mountEl) : mountEl;
      this.opts = opts;
      this.cy = null;
      this._built = false;
      this.selectedId = null;
      this.searchQuery = "";
      this._mmTransform = null;
      this._htmlWindowCap = Math.max(20, opts.htmlWindowCap || DEFAULT_HTML_WINDOW_CAP);
      this._windowedIds = new Set();
      this._windowRaf = null;
      if (this.el) this._buildChrome();
    }

    _buildChrome() {
      this.el.classList.add("atlas-graph-stage");
      this.el.innerHTML = `
        <div class="atlas-graph-toolbar" role="toolbar" aria-label="Graph view controls">
          <button type="button" class="atlas-graph-btn" data-ag="zoom-out" aria-label="Zoom out">&minus;</button>
          <span class="atlas-graph-zoom-readout" data-ag="zoom-readout">100%</span>
          <button type="button" class="atlas-graph-btn" data-ag="zoom-in" aria-label="Zoom in">+</button>
          <span class="atlas-graph-sep"></span>
          <button type="button" class="atlas-graph-btn" data-ag="fit">Fit</button>
          <button type="button" class="atlas-graph-btn" data-ag="relayout">Re-layout</button>
          <span class="atlas-graph-sep"></span>
          <span class="atlas-graph-window-readout" data-ag="window-readout" role="status" aria-live="polite"></span>
        </div>
        <div class="atlas-graph-canvas" data-ag="canvas"></div>
        <canvas class="atlas-graph-minimap" data-ag="minimap" width="176" height="118" aria-hidden="true"></canvas>
        <div class="atlas-graph-empty" data-ag="empty" hidden></div>
      `;
      this.canvasEl = this.el.querySelector('[data-ag="canvas"]');
      this.minimapEl = this.el.querySelector('[data-ag="minimap"]');
      this.zoomReadout = this.el.querySelector('[data-ag="zoom-readout"]');
      this.windowReadout = this.el.querySelector('[data-ag="window-readout"]');
      this.emptyEl = this.el.querySelector('[data-ag="empty"]');
      this.el.querySelectorAll("button[data-ag]").forEach(btn => {
        btn.addEventListener("click", () => this._toolbarAction(btn.dataset.ag));
      });
      this.minimapEl.addEventListener("click", event => this._minimapClick(event));
    }

    _toolbarAction(action) {
      if (!this.cy) return;
      if (action === "zoom-in") this.zoomBy(1.25);
      else if (action === "zoom-out") this.zoomBy(1 / 1.25);
      else if (action === "fit") this.fit();
      else if (action === "relayout") this.runLayout();
    }

    zoomBy(factor) {
      if (!this.cy || !this.canvasEl) return;
      const level = Math.max(0.12, Math.min(2.5, this.cy.zoom() * factor));
      this.cy.zoom({ level, renderedPosition: { x: this.canvasEl.clientWidth / 2, y: this.canvasEl.clientHeight / 2 } });
    }

    fit() {
      if (!this.cy || !this.cy.elements().length) return;
      this.cy.animate({ fit: { eles: this.cy.elements(), padding: 34 } }, { duration: 220, easing: "ease-out" });
    }

    runLayout() {
      if (!this.cy) return;
      let options = this._layoutOptions();
      try {
        this.cy.layout(options).run();
      } catch (error) {
        this.cy.layout({ name: "cose", fit: true, padding: 40, animate: false }).run();
      }
    }

    _layoutOptions() {
      const direction = this.opts.direction || "LR";
      if (dagreAvailable() && this.opts.layout !== "cose") {
        return {
          name: "dagre", rankDir: direction,
          nodeSep: this.opts.nodeSep || 46, rankSep: this.opts.rankSep || 140, edgeSep: 24,
          fit: true, padding: 36, animate: this._built, animationDuration: 260
        };
      }
      return {
        name: "cose", fit: true, padding: 40, nodeRepulsion: 9000, idealEdgeLength: 150,
        animate: this._built, animationDuration: 300, randomize: !this._built
      };
    }

    _stylesheet() {
      return [
        // LN-8: every node always gets this cheap canvas-only placeholder
        // (no DOM cost regardless of graph size) so pan/zoom shows the
        // graph's real shape immediately; `node[agWindowed]` (see
        // _computeWindowedIds/_applyWindow) then hides it in favor of the
        // rich HTML card cytoscape-node-html-label mounts for that node.
        { selector: "node", style: {
            "shape": "round-rectangle",
            "width": "data(w)", "height": "data(h)",
            "background-color": "#c7d2e0", "background-opacity": 0.55,
            "border-width": 1, "border-color": "#93a4b8", "border-opacity": 0.6,
            "label": ""
        }},
        { selector: "node[agWindowed]", style: {
            "background-opacity": 0, "border-width": 0
        }},
        ...EDGE_STYLES
      ];
    }

    setData(nodes, edges, options = {}) {
      if (!this.el) return;
      if (!cytoAvailable()) {
        this.el.innerHTML = '<div class="atlas-graph-empty"><div class="empty-state"><strong>Graph library unavailable</strong><span>Vendored Cytoscape assets did not load. Check /vendor script tags.</span></div></div>';
        return;
      }
      if (!nodes.length) {
        if (this.cy) this.cy.elements().remove();
        if (this.emptyEl) { this.emptyEl.hidden = false; this.emptyEl.innerHTML = options.emptyHtml || ""; }
        this._refreshMinimap();
        this._windowedIds = new Set();
        if (this.windowReadout) this.windowReadout.textContent = "";
        return;
      }
      if (this.emptyEl) this.emptyEl.hidden = true;

      const elements = [
        ...nodes.map(node => ({ group: "nodes", data: Object.assign({ id: node.id, w: node.w || 190, h: node.h || 92 }, node.data || {}) })),
        ...edges.map(edge => ({ group: "edges", data: Object.assign({ id: edge.id, source: edge.source, target: edge.target }, edge.data || {}), classes: edge.classes || "" }))
      ];

      if (!this.cy) {
        this.cy = window.cytoscape({
          container: this.canvasEl, elements,
          minZoom: 0.12, maxZoom: 2.5, wheelSensitivity: 0.28,
          boxSelectionEnabled: false, autoungrabify: false,
          style: this._stylesheet()
        });
        this._wireEvents();
        this._applyHtmlLabels();
      } else {
        this.cy.elements().remove();
        this.cy.add(elements);
      }
      this.runLayout();
      this._built = true;
      this.searchQuery = "";
      window.requestAnimationFrame(() => this._refreshMinimap());
      this._scheduleWindowRefresh();
      if (options.selectId) this.select(options.selectId);
    }

    _applyHtmlLabels() {
      if (!this.cy || typeof this.cy.nodeHtmlLabel !== "function") return;
      const self = this;
      this.cy.nodeHtmlLabel([{
        // LN-8: only nodes flagged `agWindowed` (current viewport window,
        // see _computeWindowedIds) get a mounted HTML card; the plugin
        // itself adds/removes the DOM element as this data flag flips.
        query: "node[agWindowed]",
        halign: "center", valign: "center", halignBox: "center", valignBox: "center",
        tpl: data => {
          if (typeof self.opts.nodeHtml === "function") {
            try { return self.opts.nodeHtml(data, { selected: data.id === self.selectedId }); }
            catch (error) { console.error("AtlasGraph: nodeHtml template threw, falling back to a plain card", error); }
          }
          const label = (window.AtlasUI && window.AtlasUI.esc ? window.AtlasUI.esc(data.label || data.id) : String(data.label || data.id));
          return `<div class="atlas-node-card">${label}</div>`;
        }
      }]);
    }

    _wireEvents() {
      this.cy.on("pan zoom", () => { this._refreshMinimap(); this._scheduleWindowRefresh(); });
      this.cy.on("zoom", () => { if (this.zoomReadout) this.zoomReadout.textContent = `${Math.round(this.cy.zoom() * 100)}%`; });
      this.cy.on("layoutstop", () => this._scheduleWindowRefresh());
      this.cy.on("dbltap", "node", event => { if (typeof this.opts.onNodeExpand === "function") this.opts.onNodeExpand(event.target.data()); });
    }

    select(nodeId) {
      const previous = this.selectedId;
      this.selectedId = nodeId || null;
      if (!this.cy) return;
      this.cy.edges().removeClass("ag-selected");
      if (this.selectedId) {
        const node = this.cy.getElementById(this.selectedId);
        if (node && node.length) node.connectedEdges().addClass("ag-selected");
      }
      // Nudge just the two affected nodes' data so cytoscape-node-html-label
      // re-renders their card via its own data-change listener, picking up
      // the new `meta.selected` from the tpl closure. Calling _applyHtmlLabels()
      // here would re-register the whole label config and stack a duplicate
      // DOM layer on top of the existing one instead of updating in place.
      [previous, this.selectedId].forEach(id => {
        if (!id) return;
        const node = this.cy.getElementById(id);
        if (node && node.length) node.data("_agTouch", Date.now());
      });
      // LN-8: a freshly selected/focused node must get its HTML card even
      // if it sits outside the current viewport window -- _computeWindowedIds
      // always pins `this.selectedId` in ahead of the distance-ranked cap.
      this._scheduleWindowRefresh();
    }

    // LN-8: which nodes should have a mounted HTML card right now. Pulls
    // plain {id, x, y, w, h} boxes from the live Cytoscape graph and hands
    // them to the pure, independently-tested `computeWindowedNodeIds`
    // (module scope, above) -- this method is the only place that touches
    // `this.cy`; the actual windowing decision has no Cytoscape dependency.
    _computeWindowedIds() {
      if (!this.cy) return new Set();
      const nodes = this.cy.nodes();
      if (!nodes.length) return new Set();
      const boxes = [];
      nodes.forEach(node => {
        const pos = node.position();
        boxes.push({ id: node.id(), x: pos.x, y: pos.y, w: node.width() || 190, h: node.height() || 92 });
      });
      return computeWindowedNodeIds(boxes, this.cy.extent(), { cap: this._htmlWindowCap, pinnedId: this.selectedId });
    }

    // LN-8: mount/unmount HTML cards to match `idsSet`, driving
    // cytoscape-node-html-label purely through the `agWindowed` data flag
    // (see _applyHtmlLabels's `node[agWindowed]` query) -- never touches
    // the DOM directly here.
    _applyWindow(idsSet) {
      if (!this.cy) return;
      this.cy.nodes().forEach(node => {
        const shouldShow = idsSet.has(node.id());
        if (shouldShow !== Boolean(node.data("agWindowed"))) node.data("agWindowed", shouldShow);
      });
      this._windowedIds = idsSet;
      if (this.windowReadout) {
        const total = this.cy.nodes().length;
        this.windowReadout.textContent = total ? `${idsSet.size} of ${total} nodes rendered` : "";
      }
    }

    // LN-8: coalesce bursts of pan/zoom/layout events (a drag fires many
    // per second) into at most one recompute per animation frame.
    _scheduleWindowRefresh() {
      if (this._windowRaf || !this.cy) return;
      this._windowRaf = window.requestAnimationFrame(() => {
        this._windowRaf = null;
        this._applyWindow(this._computeWindowedIds());
      });
    }

    resizeAndFit() {
      if (!this.cy) return;
      this.cy.resize();
      this.fit();
    }

    updateNodeData(nodeId, patch) {
      if (!this.cy) return;
      const node = this.cy.getElementById(nodeId);
      if (node && node.length) node.data(patch);
    }

    panToNode(nodeId) {
      if (!this.cy) return;
      const node = this.cy.getElementById(nodeId);
      if (!node || !node.length) return;
      this.cy.animate({ center: { eles: node }, zoom: Math.max(this.cy.zoom(), 0.9) }, { duration: 260 });
    }

    applySearch(query) {
      this.searchQuery = (query || "").trim().toLowerCase();
      if (!this.cy) return;
      if (!this.searchQuery) {
        this.cy.nodes().forEach(node => node.data({ agMatch: false, agDim: false }));
        this.cy.edges().removeClass("ag-dim");
      } else {
        const matchFn = this.opts.matchNode || ((data, q) => String(data.label || "").toLowerCase().includes(q));
        this.cy.nodes().forEach(node => {
          const isMatch = matchFn(node.data(), this.searchQuery);
          node.data({ agMatch: isMatch, agDim: !isMatch });
        });
        this.cy.edges().forEach(edge => {
          const touches = edge.source().data("agMatch") || edge.target().data("agMatch");
          edge.toggleClass("ag-dim", !touches);
        });
      }
      // node.data({agMatch, agDim}) above already triggers cytoscape-node-html-label's
      // own per-node re-render (see the comment in select()) — no full re-init here.
    }

    _refreshMinimap() {
      if (!this.cy || !this.minimapEl) return;
      const ctx = this.minimapEl.getContext("2d");
      const mw = this.minimapEl.width, mh = this.minimapEl.height;
      ctx.clearRect(0, 0, mw, mh);
      const nodes = this.cy.nodes();
      if (!nodes.length) { this._mmTransform = null; return; }
      const bb = nodes.boundingBox();
      const pad = 8;
      const w = Math.max(1, bb.w), h = Math.max(1, bb.h);
      const scale = Math.min((mw - pad * 2) / w, (mh - pad * 2) / h);
      const ox = pad - bb.x1 * scale + Math.max(0, (mw - pad * 2 - w * scale)) / 2;
      const oy = pad - bb.y1 * scale + Math.max(0, (mh - pad * 2 - h * scale)) / 2;
      this._mmTransform = { scale, ox, oy };
      ctx.fillStyle = "rgba(63, 111, 168, .55)";
      nodes.forEach(node => {
        const p = node.position();
        ctx.fillRect(ox + p.x * scale - 1.5, oy + p.y * scale - 1.5, 3, 3);
      });
      if (this.selectedId) {
        const sel = this.cy.getElementById(this.selectedId);
        if (sel && sel.length) {
          const p = sel.position();
          ctx.fillStyle = "#155eef";
          ctx.beginPath(); ctx.arc(ox + p.x * scale, oy + p.y * scale, 3, 0, Math.PI * 2); ctx.fill();
        }
      }
      const zoom = this.cy.zoom(), pan = this.cy.pan();
      const cw = this.canvasEl.clientWidth || 1, ch = this.canvasEl.clientHeight || 1;
      const x1 = -pan.x / zoom, y1 = -pan.y / zoom, x2 = (cw - pan.x) / zoom, y2 = (ch - pan.y) / zoom;
      ctx.strokeStyle = "#155eef"; ctx.lineWidth = 1.4;
      ctx.strokeRect(ox + x1 * scale, oy + y1 * scale, (x2 - x1) * scale, (y2 - y1) * scale);
    }

    _minimapClick(event) {
      if (!this.cy || !this._mmTransform) return;
      const rect = this.minimapEl.getBoundingClientRect();
      const mx = (event.clientX - rect.left) * (this.minimapEl.width / rect.width);
      const my = (event.clientY - rect.top) * (this.minimapEl.height / rect.height);
      const { scale, ox, oy } = this._mmTransform;
      const modelX = (mx - ox) / scale, modelY = (my - oy) / scale;
      const zoom = this.cy.zoom();
      const cw = this.canvasEl.clientWidth, ch = this.canvasEl.clientHeight;
      this.cy.pan({ x: cw / 2 - modelX * zoom, y: ch / 2 - modelY * zoom });
    }

    destroy() {
      if (this._windowRaf) { window.cancelAnimationFrame(this._windowRaf); this._windowRaf = null; }
      if (this.cy) { this.cy.destroy(); this.cy = null; }
    }
  }

  // Returns a cached instance keyed on registry[key], recreating it if the
  // mount element was wiped by an unrelated setHtml() call elsewhere (e.g. a
  // sibling view mode that replaces the same container's innerHTML) or is
  // not yet in the document.
  AtlasGraph.mount = function (containerId, opts, registry, key) {
    const mount = document.getElementById(containerId);
    const existing = registry[key];
    if (existing && (!mount || !mount.contains(existing.canvasEl))) {
      try { existing.destroy(); } catch (error) { /* detached instance, nothing to clean up */ }
      registry[key] = null;
    }
    if (!registry[key] && mount) registry[key] = new AtlasGraph(containerId, opts);
    return registry[key];
  };

  window.AtlasUI = window.AtlasUI || {};
  window.AtlasUI.AtlasGraph = AtlasGraph;
  // LN-8: exported for graph-engine.virtualization.test.mjs (pure function,
  // no Cytoscape/DOM needed to exercise it).
  window.AtlasUI.computeWindowedNodeIds = computeWindowedNodeIds;
  window.AtlasUI.DEFAULT_HTML_WINDOW_CAP = DEFAULT_HTML_WINDOW_CAP;
})();
