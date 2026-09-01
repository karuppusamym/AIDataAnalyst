"""KG-3: level-of-detail (clustering) rendering for the shared lineage/
knowledge-graph renderer.

`ui/scripts/graph-engine.js` (AtlasGraph, wrapping Cytoscape.js) is the same
single rendering surface LN-8 (large-DAG virtualization) instrumented. LN-8
windowed the HTML card layer -- bounding how many rich `<div>` cards mount at
once -- but deliberately left every real node in the Cytoscape model, so
canvas layout/hit-testing/minimap cost still scaled with the raw node count
at extreme zoom-out (up to `unified_lineage_api.py`'s full-graph route
`node_limit` of 4,000).

KG-3 adds a second, composed layer: below a zoom threshold, `graph-engine.js`
groups nodes by a caller-derived key (`defaultClusterKey`, derived from the
`qualified_name` every node the API already returns -- no new API field) and
collapses each group of `minClusterSize`+ members into a single synthetic
cluster node (centroid position, a member-count badge) via the pure,
independently-tested `computeClusterView`. Real member nodes/edges are only
hidden (`display: none`, never removed), so zooming back in recovers every
individual node instantly. This composes with LN-8's windowing rather than
duplicating it: `_computeWindowedIds()` only boxes up *visible* nodes, so a
cluster of a thousand hidden real nodes still costs exactly one HTML-card
window slot.

This is a pure client-side rendering decision over data the API already
returns -- `unified_lineage_api.py`'s and `intelligence_api.py::
get_knowledge_graph`'s request/response shape and bounded/truncated contract
(ADR-0010) are untouched; see the module-level test below that greps for it.

Follows tests/test_ui_accessibility.py's established convention for ui/ (a
plain, un-bundled browser app with no JS test runner) of asserting directly
against the source text, plus shells out to Node (no npm dependency
required -- see ui/scripts/graph-engine.clustering.test.mjs) to actually
execute the clustering function against synthetic graphs at platform scale
and prove the rendered-element-count reduction, the expand-past-threshold
recovery, and the windowing composition contract.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = REPO_ROOT / "ui"
GRAPH_ENGINE = UI_ROOT / "scripts" / "graph-engine.js"
CLUSTERING_TEST = UI_ROOT / "scripts" / "graph-engine.clustering.test.mjs"
UNIFIED_LINEAGE_API = REPO_ROOT / "src" / "aida" / "unified_lineage_api.py"
INTELLIGENCE_API = REPO_ROOT / "src" / "aida" / "intelligence_api.py"


def test_graph_engine_exports_the_pure_clustering_function_for_direct_testing() -> None:
    script = GRAPH_ENGINE.read_text(encoding="utf-8")
    assert "window.AtlasUI.computeClusterView = computeClusterView;" in script
    assert "window.AtlasUI.defaultClusterKey = defaultClusterKey;" in script
    assert "window.AtlasUI.DEFAULT_CLUSTER_ZOOM_THRESHOLD = DEFAULT_CLUSTER_ZOOM_THRESHOLD;" in script
    assert "window.AtlasUI.DEFAULT_CLUSTER_MIN_SIZE = DEFAULT_CLUSTER_MIN_SIZE;" in script
    assert "DEFAULT_CLUSTER_ZOOM_THRESHOLD = 0.45" in script
    assert "DEFAULT_CLUSTER_MIN_SIZE = 3" in script


def test_default_cluster_key_derives_from_qualified_name_no_new_api_field() -> None:
    """The default grouping key must come from data the API already returns."""
    script = GRAPH_ENGINE.read_text(encoding="utf-8")
    assert "function defaultClusterKey(nodeData)" in script
    assert "nodeData.qualified_name" in script


def test_clustering_composes_with_ln8_windowing_not_duplicating_it() -> None:
    """Clustering recomputes first in the same coalesced frame, and windowing
    only boxes up visible nodes -- so a real node hidden behind a cluster
    costs 0 window slots and its cluster costs exactly 1."""
    script = GRAPH_ENGINE.read_text(encoding="utf-8")
    assert "_refreshClusterState();" in script
    assert re.search(
        r"_scheduleWindowRefresh\(\)\s*\{[\s\S]*?_refreshClusterState\(\);\s*\n\s*this\._applyWindow\(this\._computeWindowedIds\(\)\);",
        script,
    ), "clustering must recompute before windowing in the same coalesced animation-frame pass"
    assert 'node.style("display") !== "none"' in script


def test_real_elements_are_hidden_never_removed_so_expansion_is_lossless() -> None:
    script = GRAPH_ENGINE.read_text(encoding="utf-8")
    assert "_applyClusterPlan(plan)" in script
    # Real nodes/edges are styled hidden, not removed -- `.remove()` is only
    # ever called on stale synthetic cluster nodes/edges from a prior frame.
    assert 'node.style("display", shouldHide ? "none" : "element")' in script
    assert 'edge.style("display", shouldHide ? "none" : "element")' in script
    assert "cn.remove()" in script
    assert "ce.remove()" in script


def test_pinned_selected_node_is_never_collapsed_into_a_cluster() -> None:
    script = GRAPH_ENGINE.read_text(encoding="utf-8")
    assert "pinnedIds" in script
    assert "const pinnedIds = this.selectedId ? [this.selectedId] : [];" in script


def test_graph_engine_stage_shows_a_cluster_status_readout() -> None:
    script = GRAPH_ENGINE.read_text(encoding="utf-8")
    assert 'data-ag="cluster-readout"' in script
    assert "cluster" in script.lower()
    css = (UI_ROOT / "styles" / "graph-engine.css").read_text(encoding="utf-8")
    assert ".atlas-graph-cluster-readout" in css
    assert ".atlas-cluster-card" in css


def test_api_boundary_is_untouched() -> None:
    """KG-3 is a pure client-side rendering technique: ADR-0010's bounded/
    truncated contract for the lineage/knowledge-graph endpoints must not
    change, and no new endpoint may be added for clustering."""
    unified_lineage_src = UNIFIED_LINEAGE_API.read_text(encoding="utf-8")
    intelligence_src = INTELLIGENCE_API.read_text(encoding="utf-8")
    for forbidden in ("cluster", "clustering", "level_of_detail", "level-of-detail", "lod"):
        assert forbidden not in unified_lineage_src.lower(), (
            f"unified_lineage_api.py must not gain clustering-related server logic (found {forbidden!r}) -- "
            "KG-3 is a pure client-side rendering decision"
        )
    assert "get_knowledge_graph" in intelligence_src  # sanity: still the same function name/route present
    for forbidden in ("cluster_node", "clustering", "level_of_detail"):
        assert forbidden not in intelligence_src.lower(), (
            f"intelligence_api.py::get_knowledge_graph must not gain clustering-related server logic (found {forbidden!r})"
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to execute the KG-3 clustering proof")
def test_clustering_reduces_rendered_elements_and_expansion_recovers_all_nodes() -> None:
    """Actually executes computeClusterView (no mocking) against a synthetic
    4,000-node graph (unified_lineage_api.py's own full-graph node_limit
    ceiling, the same scale LN-8's own proof uses) and proves: clustering is
    inactive at/above the zoom threshold; below it, the rendered node count
    drops far below the raw count; zooming back past the threshold recovers
    every individual node; a group under the minimum size and the pinned/
    selected node both stay individual; and a cluster costs exactly one
    LN-8 windowing slot regardless of how many real nodes it represents.
    """
    completed = subprocess.run(
        ["node", str(CLUSTERING_TEST)],
        cwd=str(UI_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"graph-engine.clustering.test.mjs failed:\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    results = payload["results"]

    inactive = results["inactive_at_threshold"]
    assert inactive["rendered"] == inactive["total"]

    clustered = results["clustered_at_node_limit"]
    assert clustered["total"] == 4000  # unified_lineage_api.py's full-graph node_limit ceiling
    assert clustered["rendered_nodes"] < clustered["total"]
    assert clustered["rendered_nodes"] == 100  # 4,000 / 40-member groups
    assert clustered["rendered_edges"] < clustered["raw_edges"]

    expand = results["expand_past_threshold_recovers_all_nodes"]
    assert expand["zoomed_out_rendered"] < expand["total"]
    assert expand["zoomed_in_rendered"] == expand["total"]

    small_group = results["small_group_stays_individual"]
    assert small_group["rendered"] == 3  # 1 cluster (40 members) + 2 individual solo nodes

    pinned = results["pinned_node_never_clustered"]
    assert pinned["pinned_individual"] is True

    window_slot = results["cluster_is_one_window_slot"]
    assert window_slot["clusters"] == 100
    assert window_slot["windowed"] == 100  # every cluster fits under the 220 HTML-card cap
    assert window_slot["windowed"] < window_slot["total_raw_nodes"]

    assert results["default_cluster_key_uses_qualified_name"]["ok"] is True
