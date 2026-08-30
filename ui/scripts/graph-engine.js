/*
 * Shared graph rendering engine for Atlas's three lineage/relationship
 * surfaces (Knowledge graph, Transformations DAG, Unified lineage).
 *
 * Wraps Cytoscape.js (vendored in /vendor, no runtime CDN calls) with:
 *  - a dagre-based layered layout (falls back to cose if dagre is missing)
 *  - real pan / zoom / drag interaction instead of a fixed grid or ring
 *  - rich HTML node cards (via cytoscape-node-html-label) so existing
 *    card markup/behavior (data-* click targets) keeps working unchanged
 *  - styled, directional edges per relationship type
 *  - a lightweight built-in minimap and search dim/highlight
 *
 * Each view supplies plain node/edge objects and an HTML template
 * function; this module owns layout, chrome, and interaction only.
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

  class AtlasGraph {
    constructor(mountEl, opts = {}) {
      this.el = typeof mountEl === "string" ? document.getElementById(mountEl) : mountEl;
      this.opts = opts;
      this.cy = null;
      this._built = false;
      this.selectedId = null;
      this.searchQuery = "";
      this._mmTransform = null;
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
        </div>
        <div class="atlas-graph-canvas" data-ag="canvas"></div>
        <canvas class="atlas-graph-minimap" data-ag="minimap" width="176" height="118" aria-hidden="true"></canvas>
        <div class="atlas-graph-empty" data-ag="empty" hidden></div>
      `;
      this.canvasEl = this.el.querySelector('[data-ag="canvas"]');
      this.minimapEl = this.el.querySelector('[data-ag="minimap"]');
      this.zoomReadout = this.el.querySelector('[data-ag="zoom-readout"]');
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
        { selector: "node", style: {
            "shape": "round-rectangle",
            "width": "data(w)", "height": "data(h)",
            "background-opacity": 0, "border-width": 0, "label": ""
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
      if (options.selectId) this.select(options.selectId);
    }

    _applyHtmlLabels() {
      if (!this.cy || typeof this.cy.nodeHtmlLabel !== "function") return;
      const self = this;
      this.cy.nodeHtmlLabel([{
        query: "node",
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
      this.cy.on("pan zoom", () => this._refreshMinimap());
      this.cy.on("zoom", () => { if (this.zoomReadout) this.zoomReadout.textContent = `${Math.round(this.cy.zoom() * 100)}%`; });
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
})();
