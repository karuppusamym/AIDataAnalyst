// LN-8 (Large-DAG virtualization) proof.
//
// Exercises `computeWindowedNodeIds` -- the pure function graph-engine.js's
// `AtlasGraph._computeWindowedIds()` calls, whose output drives the
// `node[agWindowed]` data flag that is the *only* thing gating whether
// cytoscape-node-html-label mounts a real HTML `<div>` card for a node (see
// `_applyHtmlLabels`'s `query: "node[agWindowed]"` and `_stylesheet()`'s
// `node[agWindowed]` style rule in graph-engine.js). Proving this function's
// output stays bounded is proving the rendered-DOM-element-count bound --
// mirrors how UX-11's CatalogTable test proves @tanstack/react-virtual's
// rendered row count stays bounded regardless of total row count.
//
// No test runner exists for ui/ (a plain, un-bundled browser app -- see
// tests/test_ui_accessibility.py for the established source-level-assertion
// convention). This file needs nothing beyond Node's stdlib (`node:assert`,
// `node:vm`) and is run directly: `node ui/scripts/graph-engine.virtualization.test.mjs`.
// tests/test_ui_lineage_graph_virtualization.py shells out to it so the
// bound is proven by `uv run pytest` too, not just by hand.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, "graph-engine.js"), "utf8");

// graph-engine.js is a plain browser IIFE (no `require`/`module.exports`);
// running it against a bare `{ AtlasUI: {} }` "window" is enough to capture
// its exports, since nothing at module-load time (only inside method
// bodies, never invoked here) touches `document` or `cytoscape`.
const sandbox = { window: { AtlasUI: {} }, console };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "graph-engine.js" });
const { computeWindowedNodeIds, DEFAULT_HTML_WINDOW_CAP } = sandbox.window.AtlasUI;

assert.equal(typeof computeWindowedNodeIds, "function", "graph-engine.js must export computeWindowedNodeIds");
assert.equal(typeof DEFAULT_HTML_WINDOW_CAP, "number");

// The literal gating query graph-engine.js registers with
// cytoscape-node-html-label -- if this string ever drifts from
// "node[agWindowed]" the DOM-mounting behavior this test proves would no
// longer match what's actually wired up, so pin it structurally too.
assert.match(source, /query:\s*"node\[agWindowed\]"/, 'HTML label plugin must be gated by query: "node[agWindowed]"');
assert.match(source, /_computeWindowedIds\(\)\s*\{[\s\S]*?computeWindowedNodeIds\(/, "_computeWindowedIds must delegate to the pure computeWindowedNodeIds function under test");

function makeGrid(count, { spacing = 260, perRow = 50 } = {}) {
  const boxes = [];
  for (let i = 0; i < count; i += 1) {
    boxes.push({
      id: `n${i}`,
      x: (i % perRow) * spacing,
      y: Math.floor(i / perRow) * spacing,
      w: 190,
      h: 92,
    });
  }
  return boxes;
}

function extentOf(boxes) {
  const xs = boxes.map(b => b.x), ys = boxes.map(b => b.y);
  const x1 = Math.min(...xs) - 100, x2 = Math.max(...xs) + 100;
  const y1 = Math.min(...ys) - 100, y2 = Math.max(...ys) + 100;
  return { x1, y1, x2, y2, w: x2 - x1, h: y2 - y1 };
}

const results = {};

// 1) The platform's own bounded maximum: unified_lineage_api.py's
// GET /v1/datasources/{id}/unified-lineage full-graph route allows
// node_limit up to 4,000 (edge_limit up to 20,000). Simulate the graph
// engine's default post-load view -- "Fit" fits every returned node into
// the viewport (see AtlasGraph.runLayout/_layoutOptions: `fit: true`) --
// so the whole 4,000-node graph is nominally "in view" at once. This is
// exactly the scenario that would lock up a naive "mount every node's
// HTML card" renderer.
{
  const TOTAL = 4000;
  const boxes = makeGrid(TOTAL);
  const extent = extentOf(boxes); // covers every node, like a post-Fit view
  const windowed = computeWindowedNodeIds(boxes, extent, { cap: DEFAULT_HTML_WINDOW_CAP });
  assert.ok(windowed.size <= DEFAULT_HTML_WINDOW_CAP, `windowed set (${windowed.size}) must not exceed the cap (${DEFAULT_HTML_WINDOW_CAP}) even when the whole ${TOTAL}-node graph is in view`);
  assert.ok(windowed.size > 0, "some nodes must still be windowed in (the graph isn't empty)");
  results.fit_all_at_node_limit = { total: TOTAL, cap: DEFAULT_HTML_WINDOW_CAP, windowed: windowed.size };
}

// 2) Edge_limit's own upper bound (20,000) still isn't a per-node-card
// concern (edges never get an HTML card -- cytoscape draws them natively
// on <canvas>), but node_limit's own ceiling (2,000 on the neighborhood
// route, 4,000 on the full-graph route) both stay well under a hard cap
// regardless of how the cap value itself is tuned per surface.
{
  const TOTAL = 2000;
  const boxes = makeGrid(TOTAL);
  const extent = extentOf(boxes);
  const cap = 150; // a surface-tuned cap, e.g. a denser compact-card view
  const windowed = computeWindowedNodeIds(boxes, extent, { cap });
  assert.ok(windowed.size <= cap, `windowed set (${windowed.size}) must respect a caller-supplied cap (${cap})`);
  results.custom_cap_respected = { total: TOTAL, cap, windowed: windowed.size };
}

// 3) Panning to a small region of a large, spread-out graph only windows
// in nodes near that region -- proving this is real viewport culling, not
// just "always return the first `cap` nodes" regardless of where the user
// is looking.
{
  const TOTAL = 3000;
  const boxes = makeGrid(TOTAL, { spacing: 300, perRow: 60 });
  // A viewport near the grid's origin corner, far smaller than the whole
  // layout (which spans roughly 18,000 x 15,000 model units).
  const smallExtent = { x1: -50, y1: -50, x2: 900, y2: 700, w: 950, h: 750 };
  const windowed = computeWindowedNodeIds(boxes, smallExtent, { cap: DEFAULT_HTML_WINDOW_CAP });
  assert.ok(windowed.size > 0, "at least the nodes actually in the small viewport must be windowed in");
  assert.ok(windowed.size < TOTAL, "panned-in view must not window in the entire graph");
  const farAwayNode = "n2999"; // bottom-right corner of the grid, nowhere near smallExtent
  assert.ok(!windowed.has(farAwayNode), "a node far outside the viewport (and unpinned) must be culled");
  results.viewport_culls_far_nodes = { total: TOTAL, windowed: windowed.size, excluded_far_node: farAwayNode };
}

// 4) A pinned (selected/focused) node stays windowed in even when it sits
// far outside the current viewport -- selecting a node (or "Focus and
// expand") must not make its own card disappear.
{
  const TOTAL = 3000;
  const boxes = makeGrid(TOTAL, { spacing: 300, perRow: 60 });
  const smallExtent = { x1: -50, y1: -50, x2: 900, y2: 700, w: 950, h: 750 };
  const pinnedId = "n2999"; // same far-away node as above
  const windowed = computeWindowedNodeIds(boxes, smallExtent, { cap: DEFAULT_HTML_WINDOW_CAP, pinnedId });
  assert.ok(windowed.has(pinnedId), "the pinned/selected node must be windowed in regardless of viewport position");
  assert.ok(windowed.size <= DEFAULT_HTML_WINDOW_CAP, "pinning a node must not blow through the cap");
  results.pinned_node_survives_offscreen = { windowed: windowed.size, pinned_included: windowed.has(pinnedId) };
}

// 5) An empty graph windows in nothing (no crash on `extent.w === 0`
// degenerate cases either).
{
  const windowed = computeWindowedNodeIds([], { x1: 0, y1: 0, x2: 0, y2: 0, w: 0, h: 0 });
  assert.equal(windowed.size, 0);
  results.empty_graph = { windowed: 0 };
}

console.log(JSON.stringify({ ok: true, results }, null, 2));
