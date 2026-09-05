import type { UnifiedLineageEdgeRead, UnifiedLineageNodeRead } from "./types";

/** Arrange a bounded neighborhood by data-flow direction, not asset kind. */
export function layoutTopology(nodes: readonly UnifiedLineageNodeRead[], edges: readonly UnifiedLineageEdgeRead[], focusId?: string | null, neighborhood = true) {
  const focus = nodes.find(n => n.id === focusId) ?? nodes.find(n => n.inbound_edge_count === 0 && (n.outbound_edge_count ?? 0) > 0) ?? nodes[0];
  const levels = new Map<string, number>();
  if (focus) {
    levels.set(focus.id, 0);
    for (const direction of [-1, 1]) {
      const visited = new Set([focus.id]);
      let frontier = [focus.id];
      for (let depth = 1; depth <= (neighborhood ? 2 : 5); depth++) {
        const next: string[] = [];
        for (const id of frontier) for (const edge of edges) {
          const from = direction === 1 ? edge.source_node_id : edge.target_node_id;
          const to = direction === 1 ? edge.target_node_id : edge.source_node_id;
          if (from === id && !visited.has(to)) { visited.add(to); next.push(to); if (!levels.has(to)) levels.set(to, depth * direction); }
        }
        frontier = next;
      }
    }
  }
  const candidates = neighborhood ? nodes.filter(n => levels.has(n.id)) : [...nodes];
  const shownNodes = [...candidates].sort((a, b) => Math.abs(levels.get(a.id) ?? 6) - Math.abs(levels.get(b.id) ?? 6) || a.label.localeCompare(b.label)).slice(0, 90);
  const groups = new Map<number, UnifiedLineageNodeRead[]>();
  for (const node of shownNodes) { const rank = levels.get(node.id) ?? 6; groups.set(rank, [...(groups.get(rank) ?? []), node]); }
  const positions = new Map<string, { x: number; y: number }>();
  const columns = [...groups.entries()].sort(([a], [b]) => a - b).map(([rank, group], ci) => {
    const x = 18 + ci * 208 + 104;
    group.forEach((node, i) => positions.set(node.id, { x, y: 60 + i * 48 }));
    return { kind: rank === 0 ? "Focus" : rank === 6 ? "Other assets" : `${rank < 0 ? "Upstream" : "Downstream"} ${Math.abs(rank)}`, x, nodes: group };
  });
  return { width: Math.max(244, 36 + columns.length * 208), height: Math.max(160, 100 + Math.max(0, ...columns.map(c => c.nodes.length)) * 48), columns, positions, shown: shownNodes.length, omitted: nodes.length - shownNodes.length };
}
