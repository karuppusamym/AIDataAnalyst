// KG-3 (Level-of-detail rendering) proof.
//
// Exercises `computeClusterView` -- the pure function graph-engine.js's
// `AtlasGraph._refreshClusterState()` calls, whose output drives which real
// nodes/edges `_applyClusterPlan` hides (`display: none`, never removed)
// and which synthetic cluster nodes/edges it adds. This is a pure,
// client-side rendering decision over data `unified_lineage_api.py` already
// returns (the default grouping key, `defaultClusterKey`, is derived from
// `qualified_name`, which every node the API returns already carries) --
// the API's own request/response shape and bounded/truncated contract
// (ADR-0010) are untouched; see graph-engine.js's "Level-of-detail
// clustering (KG-3)" doc comment.
//
// Follows the exact harness LN-8 established in
// graph-engine.virtualization.test.mjs: no test runner exists for ui/ (a
// plain, un-bundled browser app -- see tests/test_ui_accessibility.py's
// source-assertion convention), so this needs nothing beyond Node's stdlib
// and is run directly: `node ui/scripts/graph-engine.clustering.test.mjs`.
// tests/test_ui_lineage_graph_clustering.py shells out to it so the bound
// is proven under `pytest` too, not just by hand.
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
const {
  computeClusterView,
  defaultClusterKey,
  DEFAULT_CLUSTER_ZOOM_THRESHOLD,
  DEFAULT_CLUSTER_MIN_SIZE,
  computeWindowedNodeIds,
  DEFAULT_HTML_WINDOW_CAP,
} = sandbox.window.AtlasUI;

assert.equal(typeof computeClusterView, "function", "graph-engine.js must export computeClusterView");
assert.equal(typeof defaultClusterKey, "function", "graph-engine.js must export defaultClusterKey");
assert.equal(typeof DEFAULT_CLUSTER_ZOOM_THRESHOLD, "number");
assert.equal(typeof DEFAULT_CLUSTER_MIN_SIZE, "number");

// The pure clustering decision must compose with, not fight, LN-8's
// windowing -- structurally pin the composition point: _refreshClusterState
// runs before _computeWindowedIds in the same coalesced frame, and
// _computeWindowedIds only boxes up *visible* nodes (so a hidden, absorbed
// real node costs 0 slots and its one cluster node costs exactly 1).
assert.match(source, /_scheduleWindowRefresh\(\)\s*\{[\s\S]*?_refreshClusterState\(\);\s*\n\s*this\._applyWindow\(this\._computeWindowedIds\(\)\);/, "clustering must recompute before windowing in the same coalesced pass");
assert.match(source, /_computeWindowedIds\(\)\s*\{[\s\S]*?filter\(node => node\.style\("display"\) !== "none"\)/, "windowing must only box up visible nodes so a hidden clustered-away node costs 0 window slots");

function makeGroupedBoxes(count, { groupSize = 40, spacing = 260, perRow = 50 } = {}) {
  const boxes = [];
  for (let i = 0; i < count; i += 1) {
    const groupIndex = Math.floor(i / groupSize);
    boxes.push({
      id: `n${i}`,
      groupKey: `schema_${groupIndex}`,
      x: (i % perRow) * spacing,
      y: Math.floor(i / perRow) * spacing,
      w: 190,
      h: 92,
    });
  }
  return boxes;
}

function chainEdges(count) {
  const edges = [];
  for (let i = 0; i < count - 1; i += 1) edges.push({ id: `e${i}`, source: `n${i}`, target: `n${i + 1}` });
  return edges;
}

const results = {};

// 1) At/above the zoom threshold, clustering is inactive: the raw graph
// passes through completely unchanged (every node individual, every edge
// its original id/endpoints).
{
  const TOTAL = 200;
  const boxes = makeGroupedBoxes(TOTAL);
  const edges = chainEdges(TOTAL);
  const plan = computeClusterView(boxes, edges, DEFAULT_CLUSTER_ZOOM_THRESHOLD, {});
  assert.equal(plan.active, false, "at the threshold itself, clustering must be inactive (>=, not >)");
  assert.equal(plan.nodes.length, TOTAL, "inactive clustering must render every raw node");
  assert.ok(plan.nodes.every(n => !n.clustered), "no node should be marked clustered when inactive");
  assert.equal(plan.edges.length, edges.length, "inactive clustering must render every raw edge");
  assert.ok(plan.edges.every(e => e.original), "every edge must keep its original id/endpoints when inactive");
  results.inactive_at_threshold = { total: TOTAL, rendered: plan.nodes.length };
}

// 2) Below the threshold, at platform scale (mirrors LN-8's own 4,000-node
// unified_lineage_api.py full-graph node_limit ceiling), clustering
// activates and the rendered node count drops far below the raw count --
// this is the actual level-of-detail win: fewer rendered elements than the
// raw node count, on top of whatever LN-8's windowing does to the HTML
// card layer.
{
  const TOTAL = 4000;
  const boxes = makeGroupedBoxes(TOTAL, { groupSize: 40 }); // 100 groups of 40
  const edges = chainEdges(TOTAL);
  const zoom = DEFAULT_CLUSTER_ZOOM_THRESHOLD - 0.1; // zoomed out past the threshold
  const plan = computeClusterView(boxes, edges, zoom, {});
  assert.equal(plan.active, true);
  assert.ok(plan.nodes.length < TOTAL, `clustered rendered node count (${plan.nodes.length}) must be less than the raw count (${TOTAL})`);
  const EXPECTED_GROUPS = TOTAL / 40; // every group is >= DEFAULT_CLUSTER_MIN_SIZE, so each collapses to exactly one cluster node
  assert.equal(plan.nodes.length, EXPECTED_GROUPS, "every 40-member group must collapse to exactly one cluster node");
  assert.ok(plan.nodes.every(n => n.clustered), "every rendered node must be a cluster (no group fell below the minimum size)");
  plan.nodes.forEach(n => assert.equal(n.count, 40));
  const totalMembers = plan.nodes.reduce((sum, n) => sum + n.memberIds.length, 0);
  assert.equal(totalMembers, TOTAL, "every raw node must be accounted for by exactly one cluster");
  assert.ok(plan.edges.length < edges.length, "aggregated edge count must also drop below the raw edge count");
  results.clustered_at_node_limit = { total: TOTAL, rendered_nodes: plan.nodes.length, raw_edges: edges.length, rendered_edges: plan.edges.length };
}

// 3) Zooming back in past the threshold recovers every individual node --
// re-running the same input at a zoom at/above threshold must reproduce
// case (1)'s full-fidelity, unclustered output exactly (nothing "sticky"
// carries over from having been clustered a moment ago, since the function
// is pure and stateless).
{
  const TOTAL = 4000;
  const boxes = makeGroupedBoxes(TOTAL, { groupSize: 40 });
  const edges = chainEdges(TOTAL);
  const zoomedOut = computeClusterView(boxes, edges, DEFAULT_CLUSTER_ZOOM_THRESHOLD - 0.1, {});
  const zoomedIn = computeClusterView(boxes, edges, DEFAULT_CLUSTER_ZOOM_THRESHOLD + 0.2, {});
  assert.ok(zoomedOut.active && zoomedOut.nodes.length < TOTAL, "sanity: the same input must actually be clustered when zoomed out");
  assert.equal(zoomedIn.active, false, "zooming back in past the threshold must deactivate clustering");
  assert.equal(zoomedIn.nodes.length, TOTAL, "zooming back in must recover every individual node");
  const recoveredIds = new Set(zoomedIn.nodes.map(n => n.id));
  boxes.forEach(b => assert.ok(recoveredIds.has(b.id), `expanded view must recover node ${b.id}`));
  results.expand_past_threshold_recovers_all_nodes = { total: TOTAL, zoomed_out_rendered: zoomedOut.nodes.length, zoomed_in_rendered: zoomedIn.nodes.length };
}

// 4) A group smaller than the minimum cluster size stays individual --
// collapsing a lone pair into a "cluster of 2" saves nothing and would
// just be confusing.
{
  const boxes = [
    ...makeGroupedBoxes(40, { groupSize: 40 }), // one big group, well over the minimum
    { id: "solo_a", groupKey: "tiny_schema", x: 20000, y: 0, w: 190, h: 92 },
    { id: "solo_b", groupKey: "tiny_schema", x: 20200, y: 0, w: 190, h: 92 },
  ];
  const plan = computeClusterView(boxes, [], DEFAULT_CLUSTER_ZOOM_THRESHOLD - 0.1, { minClusterSize: 3 });
  assert.equal(plan.active, true);
  const soloA = plan.nodes.find(n => n.id === "solo_a");
  const soloB = plan.nodes.find(n => n.id === "solo_b");
  assert.ok(soloA && !soloA.clustered, "a 2-member group below minClusterSize must stay an individual node");
  assert.ok(soloB && !soloB.clustered, "a 2-member group below minClusterSize must stay an individual node");
  const bigGroupCluster = plan.nodes.find(n => n.clustered);
  assert.ok(bigGroupCluster && bigGroupCluster.count === 40, "the 40-member group must still collapse");
  results.small_group_stays_individual = { rendered: plan.nodes.length };
}

// 5) The selected/focused node is pinned -- never absorbed into a cluster,
// even while every other member of its group collapses. Mirrors LN-8's own
// pinned-node behavior for windowing.
{
  const boxes = makeGroupedBoxes(80, { groupSize: 80 }); // one group of 80
  const pinnedId = "n40";
  const plan = computeClusterView(boxes, [], DEFAULT_CLUSTER_ZOOM_THRESHOLD - 0.1, { pinnedIds: [pinnedId] });
  assert.equal(plan.active, true);
  assert.equal(plan.memberToRenderId[pinnedId], pinnedId, "a pinned node must render as itself, never a cluster id");
  const pinnedNode = plan.nodes.find(n => n.id === pinnedId);
  assert.ok(pinnedNode && !pinnedNode.clustered, "the pinned node must appear individually in the rendered node list");
  const otherCluster = plan.nodes.find(n => n.clustered);
  assert.ok(otherCluster && otherCluster.count === 79, "the remaining 79 members must still collapse into one cluster");
  results.pinned_node_never_clustered = { rendered: plan.nodes.length, pinned_individual: !pinnedNode.clustered };
}

// 6) A cluster node is exactly one box handed to LN-8's windowing function
// -- proving the composition contract in the task description ("a cluster
// node counts as one window slot, not N"), not just asserting it by source
// inspection. Simulate _computeWindowedIds' own box-building: after
// clustering, only render-list entries (never the raw per-member nodes)
// are boxed up for computeWindowedNodeIds.
{
  const TOTAL = 4000;
  const boxes = makeGroupedBoxes(TOTAL, { groupSize: 40 }); // 100 clusters
  const plan = computeClusterView(boxes, [], DEFAULT_CLUSTER_ZOOM_THRESHOLD - 0.1, {});
  assert.equal(plan.nodes.length, 100, "100 groups of 40 must collapse to exactly 100 cluster nodes");
  const windowBoxes = plan.nodes.map(n => ({ id: n.id, x: n.x, y: n.y, w: n.w, h: n.h }));
  const xs = windowBoxes.map(b => b.x), ys = windowBoxes.map(b => b.y);
  const extent = { x1: Math.min(...xs) - 500, y1: Math.min(...ys) - 500, x2: Math.max(...xs) + 500, y2: Math.max(...ys) + 500 };
  extent.w = extent.x2 - extent.x1; extent.h = extent.y2 - extent.y1;
  const windowed = computeWindowedNodeIds(windowBoxes, extent, { cap: DEFAULT_HTML_WINDOW_CAP });
  // All 100 clusters fit comfortably under the 220 cap -- unlike windowing
  // 4,000 raw nodes (LN-8's own test 1, which hits the cap at 220), so this
  // also proves clustering strictly reduces window pressure, not just
  // canvas node count.
  assert.equal(windowed.size, 100, "every cluster must fit in the HTML card window (100 boxes, cap 220) -- 4,000 raw nodes would have hit the 220 cap instead");
  results.cluster_is_one_window_slot = { total_raw_nodes: TOTAL, clusters: plan.nodes.length, windowed: windowed.size, html_window_cap: DEFAULT_HTML_WINDOW_CAP };
}

// 7) defaultClusterKey derives the grouping key from `qualified_name` alone
// -- the field every node the API already returns carries (see
// unified_lineage_api.py's `f"{catalog.name}.{schema.name}.{table.name}"`
// for TABLE nodes) -- with no new API field required.
{
  assert.equal(defaultClusterKey({ qualified_name: "analytics.public.orders" }), "analytics.public");
  assert.equal(defaultClusterKey({ qualified_name: "orders", node_kind: "TABLE" }), "TABLE", "a qualified_name with no '.' must fall back to node_kind");
  assert.equal(defaultClusterKey({ qualified_name: "orders", object_type: "TABLE" }), "TABLE", "node_kind absent must fall back to object_type");
  assert.equal(defaultClusterKey({}), "ungrouped", "no qualified_name and no node_kind/object_type must fall back to a single catch-all bucket");
  results.default_cluster_key_uses_qualified_name = { ok: true };
}

console.log(JSON.stringify({ ok: true, results }, null, 2));
