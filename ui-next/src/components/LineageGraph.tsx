import { useMemo, useState } from "react";
import type { UnifiedLineageEdgeRead, UnifiedLineageGraphRead, UnifiedLineageNodeRead } from "../lib/types";
import "./LineageGraph.css";

/* ---------------------------------------------------------------------------
   Lineage, twice (review 2026-09-05, F21 / UX section 7).

   THE DEFECT this removes: lineage existed ONLY as an SVG. The nodes were
   focusable, but the edges -- which are the entire content of a lineage view,
   the claim that A feeds B and on what evidence -- were `path` elements with a
   `title`, reachable by a mouse pointer and by nothing else. A screen reader
   user, and anyone on a narrow window where a 760px-minimum canvas cannot be
   read, had no way to learn what the graph said. The SVG also carried
   `role="img"`, which tells assistive technology to treat its contents as one
   flat image -- including the buttons inside it.

   THE INVARIANT: every relationship shown in the diagram is also available as
   a row. The table is not a summary or a fallback stub; it is the same edges
   and the same evidence, in a form that can be read linearly, searched by the
   browser, and operated by a keyboard.
--------------------------------------------------------------------------- */

const EDGE_LABELS: Record<string, string> = {
  FOREIGN_KEY: "Foreign key",
  SUGGESTED_RELATIONSHIP: "Inferred / approved",
  DBT_DEPENDENCY: "dbt dependency",
  OPENLINEAGE_ETL: "OpenLineage job",
  VIEW_DEFINITION: "View definition",
  PROCEDURE_DEFINITION: "Procedure",
};

interface PlacedNode extends UnifiedLineageNodeRead {
  x: number;
  y: number;
}

function placeNodes(graph: UnifiedLineageGraphRead): { nodes: PlacedNode[]; width: number; height: number } {
  const ids = new Set(graph.nodes.map((node) => node.id));
  const incoming = new Map(graph.nodes.map((node) => [node.id, 0]));
  const outgoing = new Map<string, string[]>();
  for (const edge of graph.edges) {
    if (!ids.has(edge.source_node_id) || !ids.has(edge.target_node_id)) continue;
    incoming.set(edge.target_node_id, (incoming.get(edge.target_node_id) ?? 0) + 1);
    outgoing.set(edge.source_node_id, [...(outgoing.get(edge.source_node_id) ?? []), edge.target_node_id]);
  }
  const layer = new Map<string, number>();
  const queue = graph.nodes.filter((node) => incoming.get(node.id) === 0).map((node) => node.id);
  if (queue.length === 0 && graph.nodes[0]) queue.push(graph.nodes[0].id);
  for (const id of queue) layer.set(id, 0);
  while (queue.length) {
    const id = queue.shift()!;
    for (const target of outgoing.get(id) ?? []) {
      layer.set(target, Math.max(layer.get(target) ?? 0, (layer.get(id) ?? 0) + 1));
      incoming.set(target, Math.max(0, (incoming.get(target) ?? 0) - 1));
      if (incoming.get(target) === 0) queue.push(target);
    }
  }
  for (const node of graph.nodes) if (!layer.has(node.id)) layer.set(node.id, 0);

  const grouped = new Map<number, UnifiedLineageNodeRead[]>();
  for (const node of graph.nodes) grouped.set(layer.get(node.id) ?? 0, [...(grouped.get(layer.get(node.id) ?? 0) ?? []), node]);
  const maxLayer = Math.max(0, ...grouped.keys());
  const maxRows = Math.max(1, ...[...grouped.values()].map((items) => items.length));
  const width = Math.max(760, 90 + (maxLayer + 1) * 245);
  const height = Math.max(360, 90 + maxRows * 105);
  const nodes: PlacedNode[] = [];
  for (const [column, items] of grouped) {
    const contentHeight = (items.length - 1) * 105;
    const start = (height - contentHeight) / 2;
    items.sort((a, b) => a.label.localeCompare(b.label)).forEach((node, index) => {
      nodes.push({ ...node, x: 50 + column * 245, y: start + index * 105 - 34 });
    });
  }
  return { nodes, width, height };
}

function edgeTitle(edge: UnifiedLineageEdgeRead): string {
  const confidence = Number.isFinite(edge.confidence) ? ` · ${Math.round(edge.confidence * 100)}% confidence` : "";
  return `${EDGE_LABELS[edge.edge_source] ?? edge.edge_source} · ${edge.status.toLowerCase()}${confidence}`;
}

type LineageView = "graph" | "table";

/** The same edges as the diagram, as rows. */
function LineageTable({
  graph,
  focusNodeId,
  onSelectNode,
}: {
  graph: UnifiedLineageGraphRead;
  focusNodeId?: string | null;
  onSelectNode: (nodeId: string) => void;
}) {
  const byId = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph.nodes]);
  const connected = useMemo(() => {
    const seen = new Set<string>();
    for (const edge of graph.edges) {
      seen.add(edge.source_node_id);
      seen.add(edge.target_node_id);
    }
    return seen;
  }, [graph.edges]);
  const isolated = graph.nodes.filter((node) => !connected.has(node.id));

  return (
    <div className="lgtable">
      <div className="lgtable__scroll">
        <table>
          <caption className="sr-only">
            Every relationship in this lineage view: what feeds what, on which evidence, and
            whether that evidence is active or still proposed.
          </caption>
          <thead>
            <tr>
              <th scope="col">Upstream</th>
              <th scope="col">Downstream</th>
              <th scope="col">Evidence</th>
              <th scope="col">Status</th>
              <th scope="col">Confidence</th>
            </tr>
          </thead>
          <tbody>
            {graph.edges.map((edge) => {
              const source = byId.get(edge.source_node_id);
              const target = byId.get(edge.target_node_id);
              if (!source || !target) return null;
              return (
                <tr key={edge.id}>
                  <td>
                    <button
                      type="button"
                      className={`lgtable__node${source.id === focusNodeId ? " is-focus" : ""}`}
                      onClick={() => onSelectNode(source.id)}
                      title={source.qualified_name}
                    >
                      {source.label}
                    </button>
                  </td>
                  <td>
                    <button
                      type="button"
                      className={`lgtable__node${target.id === focusNodeId ? " is-focus" : ""}`}
                      onClick={() => onSelectNode(target.id)}
                      title={target.qualified_name}
                    >
                      {target.label}
                    </button>
                  </td>
                  <td>{EDGE_LABELS[edge.edge_source] ?? edge.edge_source}</td>
                  <td>{edge.status.toLowerCase()}</td>
                  <td className="lgtable__num">
                    {Number.isFinite(edge.confidence) ? `${Math.round(edge.confidence * 100)}%` : "—"}
                  </td>
                </tr>
              );
            })}
            {graph.edges.length === 0 ? (
              <tr>
                <td colSpan={5}>No relationships in this view.</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      {isolated.length > 0 ? (
        <p className="lgtable__isolated">
          {/* An asset with no edges is invisible in a table of edges. Saying so
              is the difference between "no lineage" and "not listed". */}
          {isolated.length} asset{isolated.length === 1 ? "" : "s"} in this view have no recorded
          relationship: {isolated.map((node) => node.label).join(", ")}.
        </p>
      ) : null}
    </div>
  );
}

export function LineageGraph({ graph, focusNodeId, onSelectNode }: {
  graph: UnifiedLineageGraphRead;
  focusNodeId?: string | null;
  onSelectNode: (nodeId: string) => void;
}) {
  const [view, setView] = useState<LineageView>("graph");
  const [zoom, setZoom] = useState(1);
  const placed = useMemo(() => placeNodes(graph), [graph]);
  const byId = useMemo(() => new Map(placed.nodes.map((node) => [node.id, node])), [placed.nodes]);
  const counts = Object.entries(graph.counts_by_source).filter(([, count]) => count > 0);

  return (
    <section className="lgraph" aria-label="Interactive lineage graph">
      <header className="lgraph__toolbar">
        <div>
          <strong>{graph.nodes.length} assets</strong>
          <span>{graph.edges.length} evidence-backed links</span>
        </div>
        <div className="lgraph__views" role="group" aria-label="Lineage view">
          <button type="button" aria-pressed={view === "graph"} onClick={() => setView("graph")}>
            Diagram
          </button>
          <button type="button" aria-pressed={view === "table"} onClick={() => setView("table")}>
            Table
          </button>
        </div>
        <div className="lgraph__zoom" aria-label="Graph zoom" hidden={view !== "graph"}>
          <button onClick={() => setZoom((value) => Math.max(.7, value - .15))} aria-label="Zoom out">−</button>
          <output>{Math.round(zoom * 100)}%</output>
          <button onClick={() => setZoom((value) => Math.min(1.8, value + .15))} aria-label="Zoom in">+</button>
          <button onClick={() => setZoom(1)}>Fit</button>
        </div>
      </header>
      <div className="lgraph__legend" aria-label="Relationship evidence legend">
        {counts.map(([source, count]) => (
          <span key={source} data-edge={source}><i />{EDGE_LABELS[source] ?? source} <b>{count}</b></span>
        ))}
      </div>
      {view === "table" ? (
        <LineageTable graph={graph} focusNodeId={focusNodeId} onSelectNode={onSelectNode} />
      ) : (
      <div className="lgraph__viewport">
        <svg
          className="lgraph__svg"
          style={{ width: `${zoom * 100}%` }}
          viewBox={`0 0 ${placed.width} ${placed.height}`}
          /* Not an image role: the nodes inside are real buttons, and role="img"
             would hide every one of them from assistive technology. */
          role="group"
          aria-label="Lineage diagram grouped by hop distance and evidence. The Table view lists the same relationships as rows."
        >
          <defs>
            {Object.keys(EDGE_LABELS).map((source) => (
              <marker key={source} id={`arrow-${source}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" className={`lgraph__arrow lgraph__arrow--${source}`} />
              </marker>
            ))}
          </defs>
          <g className="lgraph__edges">
            {graph.edges.map((edge) => {
              const source = byId.get(edge.source_node_id);
              const target = byId.get(edge.target_node_id);
              if (!source || !target) return null;
              const sx = source.x + 180;
              const sy = source.y + 34;
              const tx = target.x;
              const ty = target.y + 34;
              const bend = Math.max(42, (tx - sx) * .5);
              return (
                <path
                  key={edge.id}
                  d={`M ${sx} ${sy} C ${sx + bend} ${sy}, ${tx - bend} ${ty}, ${tx} ${ty}`}
                  className={`lgraph__edge lgraph__edge--${edge.edge_source}${edge.status !== "ACTIVE" ? " lgraph__edge--pending" : ""}`}
                  markerEnd={`url(#arrow-${edge.edge_source})`}
                >
                  <title>{edgeTitle(edge)}</title>
                </path>
              );
            })}
          </g>
          <g className="lgraph__nodes">
            {placed.nodes.map((node) => (
              <g
                key={node.id}
                className={`lgraph__node${node.id === focusNodeId ? " lgraph__node--focus" : ""}${node.resolved === false ? " lgraph__node--unresolved" : ""}`}
                transform={`translate(${node.x} ${node.y})`}
                role="button"
                tabIndex={0}
                /* R11-C2: an explicit name, matching `UnifiedLineageScreen`'s
                   equivalent node. Leaving it to the `<title>` and the three
                   `<text>` children made the announced name the whole card
                   read out as one run-on string -- kind, truncated label and
                   an ellipsised path -- which is not what a person needs to
                   hear to decide whether to open it. Neither axe's
                   `button-name` nor its `link-name` covers a focusable
                   `role="button"` on an SVG `<g>`, so nothing was reporting
                   this. */
                aria-label={`Select ${node.qualified_name}`}
                onClick={() => onSelectNode(node.id)}
                /* Space must be swallowed as well as handled: on a non-button
                   element the browser's default for Space is to scroll the
                   page, so activating a node from the keyboard also jumped
                   the diagram out from under the user. */
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelectNode(node.id);
                  }
                }}
              >
                <rect width="180" height="68" rx="9" />
                <text className="lgraph__kind" x="13" y="19">{node.node_kind.replace(/_/g, " ")}</text>
                <text className="lgraph__label" x="13" y="40">{node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label}</text>
                <text className="lgraph__path" x="13" y="56">{node.qualified_name.length > 29 ? `…${node.qualified_name.slice(-28)}` : node.qualified_name}</text>
                <title>{node.qualified_name}</title>
              </g>
            ))}
          </g>
        </svg>
      </div>
      )}
      {graph.truncated ? <p className="lgraph__truncated">The server bounded this view. Narrow the source or inspect a selected asset for its local impact.</p> : null}
      <p className="lgraph__hint">
        Select any asset to focus its narrated upstream and downstream impact.
        {view === "graph"
          ? " Switch to Table for the same relationships as rows."
          : " Switch to Diagram for the same relationships as a picture."}
      </p>
    </section>
  );
}
