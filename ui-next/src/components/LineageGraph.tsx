import { useMemo, useState } from "react";
import type { UnifiedLineageEdgeRead, UnifiedLineageGraphRead, UnifiedLineageNodeRead } from "../lib/types";
import "./LineageGraph.css";

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

export function LineageGraph({ graph, focusNodeId, onSelectNode }: {
  graph: UnifiedLineageGraphRead;
  focusNodeId?: string | null;
  onSelectNode: (nodeId: string) => void;
}) {
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
        <div className="lgraph__zoom" aria-label="Graph zoom">
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
      <div className="lgraph__viewport">
        <svg
          className="lgraph__svg"
          style={{ width: `${zoom * 100}%` }}
          viewBox={`0 0 ${placed.width} ${placed.height}`}
          role="img"
          aria-label="Lineage graph grouped by hop distance and evidence"
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
                onClick={() => onSelectNode(node.id)}
                onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") onSelectNode(node.id); }}
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
      {graph.truncated ? <p className="lgraph__truncated">The server bounded this view. Narrow the source or inspect a selected asset for its local impact.</p> : null}
      <p className="lgraph__hint">Select any asset to focus its narrated upstream and downstream impact.</p>
    </section>
  );
}
