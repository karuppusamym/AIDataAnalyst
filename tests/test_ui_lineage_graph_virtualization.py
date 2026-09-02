"""LN-8: large-DAG virtualization for the shared lineage/knowledge-graph renderer.

`ui/scripts/graph-engine.js` (AtlasGraph, wrapping Cytoscape.js) is the single
rendering surface behind Knowledge graph, the dbt Transformations DAG, Unified
lineage, and the AI dependency graph. Cytoscape itself draws nodes/edges on a
<canvas> (cheap regardless of graph size); the actual "full graph render" cost
was cytoscape-node-html-label mounting one real HTML `<div>` card per node,
unconditionally, with no notion of viewport -- at the platform's own bounded
maxima (`node_limit` up to 4,000 on `unified_lineage_api.py`'s full-graph
route) that is thousands of DOM elements mounted at once, including on first
load (the engine fits the whole graph into view by default).

`ui/scripts/graph-engine.js` now drives that plugin's mounting dynamically
through a `node[agWindowed]` data flag, set by the pure, DOM/Cytoscape-free
`computeWindowedNodeIds` function: viewport-intersecting (plus overscan),
capped at a fixed size regardless of how many nodes are nominally in view
(covering the "fit all nodes" case), always including the selected/focused
node. This module follows tests/test_ui_accessibility.py's established
convention for ui/ (a plain, un-bundled browser app with no JS test runner)
of asserting directly against the source text, plus shells out to Node (no
npm dependency required -- see `ui/scripts/graph-engine.virtualization.test.mjs`)
to actually execute the windowing function against a synthetic large graph
and prove the rendered-element-count bound, the same thing UX-11's
CatalogTable test proves for the virtualized catalog grid, just with a
viewport-shaped window instead of a scroll-position-shaped one.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI_ROOT = Path(__file__).resolve().parents[1] / "ui"
GRAPH_ENGINE = UI_ROOT / "scripts" / "graph-engine.js"
VIRTUALIZATION_TEST = UI_ROOT / "scripts" / "graph-engine.virtualization.test.mjs"
NODE_BIN = shutil.which("node")


def test_graph_engine_html_card_mounting_is_gated_by_a_windowed_data_flag() -> None:
    script = GRAPH_ENGINE.read_text(encoding="utf-8")
    # The only query cytoscape-node-html-label is registered with -- this is
    # what actually decides whether a node gets a mounted HTML `<div>`.
    assert 'query: "node[agWindowed]"' in script
    # Every node still gets a canvas-only placeholder (no DOM cost) so
    # pan/zoom shows the graph's shape before cards lazily mount.
    assert '{ selector: "node[agWindowed]"' in script
    assert "_computeWindowedIds" in script
    assert "_applyWindow" in script
    assert "_scheduleWindowRefresh" in script
    assert "computeWindowedNodeIds" in script
    assert "DEFAULT_HTML_WINDOW_CAP = 220" in script


def test_graph_engine_windows_on_pan_zoom_layout_and_selection() -> None:
    script = GRAPH_ENGINE.read_text(encoding="utf-8")
    # Recompute on pan/zoom (continuous panning) and layoutstop (new/re-laid-
    # out data), not just once on load.
    assert '"pan zoom", () => { this._refreshMinimap(); this._scheduleWindowRefresh(); }' in script
    assert '"layoutstop", () => this._scheduleWindowRefresh()' in script
    # A freshly selected/focused node is pinned in even if off-screen.
    assert "always pins `this.selectedId`" in script


def test_graph_engine_exports_the_pure_windowing_function_for_direct_testing() -> None:
    script = GRAPH_ENGINE.read_text(encoding="utf-8")
    assert "window.AtlasUI.computeWindowedNodeIds = computeWindowedNodeIds;" in script
    assert "window.AtlasUI.DEFAULT_HTML_WINDOW_CAP = DEFAULT_HTML_WINDOW_CAP;" in script


def test_graph_engine_stage_shows_a_render_window_readout() -> None:
    """Mirrors the virtualized catalog table's "Showing X-Y of Z rows" readout."""
    script = GRAPH_ENGINE.read_text(encoding="utf-8")
    html = script  # the toolbar markup is a template literal inside graph-engine.js
    assert 'data-ag="window-readout"' in html
    assert "nodes rendered" in script
    css = (UI_ROOT / "styles" / "graph-engine.css").read_text(encoding="utf-8")
    assert ".atlas-graph-window-readout" in css


@pytest.mark.skipif(
    NODE_BIN is None, reason="node is required to execute the LN-8 virtualization proof"
)
def test_windowed_node_count_stays_bounded_at_the_platform_node_limit() -> None:
    """Actually executes computeWindowedNodeIds (no mocking) against synthetic
    graphs sized at/above the platform's own bounds (node_limit up to 4,000 on
    `unified_lineage_api.py`'s `GET /v1/datasources/{id}/unified-lineage`
    full-graph route) and proves the windowed/rendered set never exceeds the
    cap -- including the "whole graph fit into view" case that a naive
    mount-every-node renderer would lock up on.
    """
    assert NODE_BIN is not None  # narrows for mypy; skipif above already guarantees this
    completed = subprocess.run(  # noqa: S603 -- fixed script path, resolved trusted node binary
        [NODE_BIN, str(VIRTUALIZATION_TEST)],
        cwd=str(UI_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"graph-engine.virtualization.test.mjs failed:\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    results = payload["results"]

    fit_all = results["fit_all_at_node_limit"]
    assert fit_all["total"] == 4000  # unified_lineage_api.py's full-graph node_limit ceiling
    assert fit_all["windowed"] <= fit_all["cap"]
    assert fit_all["windowed"] > 0

    custom_cap = results["custom_cap_respected"]
    assert custom_cap["windowed"] <= custom_cap["cap"]

    panned = results["viewport_culls_far_nodes"]
    assert 0 < panned["windowed"] < panned["total"]

    pinned = results["pinned_node_survives_offscreen"]
    assert pinned["pinned_included"] is True

    assert results["empty_graph"]["windowed"] == 0
