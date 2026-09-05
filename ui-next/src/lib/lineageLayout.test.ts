import { describe, expect, it } from "vitest";
import { layoutTopology } from "./lineageLayout";
import type { UnifiedLineageEdgeRead, UnifiedLineageNodeRead } from "./types";

const node = (id: string) => ({ id, label: id, node_kind: "TABLE", qualified_name: `public.${id}` } as UnifiedLineageNodeRead);
const edge = (from: string, to: string) => ({ source_node_id: from, target_node_id: to } as UnifiedLineageEdgeRead);
describe("lineage dependency layout", () => {
  it("separates connected tables into upstream, focus and downstream columns", () => {
    const layout = layoutTopology([node("raw"), node("model"), node("report")], [edge("raw", "model"), edge("model", "report")], "model");
    expect(layout.positions.get("raw")!.x).toBeLessThan(layout.positions.get("model")!.x);
    expect(layout.positions.get("report")!.x).toBeGreaterThan(layout.positions.get("model")!.x);
  });
  it("bounds cycles and excludes disconnected assets from the focused view", () => {
    const layout = layoutTopology([node("a"), node("b"), node("unrelated")], [edge("a", "b"), edge("b", "a")], "a");
    expect(layout.shown).toBe(2);
    expect(layout.omitted).toBe(1);
    expect(layout.positions.has("unrelated")).toBe(false);
  });
});
