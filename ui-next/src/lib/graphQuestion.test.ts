import { describe, expect, it } from "vitest";
import { resolveGraphQuestion } from "./graphQuestion";
import type { UnifiedLineageNodeRead } from "./types";
const node: UnifiedLineageNodeRead = { id: "a", label: "orders", qualified_name: "sales.orders", node_kind: "TABLE" };
describe("bounded graph questions", () => {
  it.each([
    ["what depends on orders?", "downstream"],
    ["which assets use sales.orders?", "downstream"],
    ["impact of orders", "downstream"],
    ["what feeds orders?", "upstream"],
    ["where does orders come from?", "upstream"],
    ["show dependencies of orders", "upstream"],
    ["show lineage of orders", "both"],
  ])("resolves %s as a typed plan", (question, direction) => {
    expect(resolveGraphQuestion(question, [node])).toEqual({direction, depth: 3, node});
  });
  it("keeps hop limits when using natural-language aliases", () => {
    expect(resolveGraphQuestion("what depends on orders within 2 hops?", [node]).depth).toBe(2);
    expect(() => resolveGraphQuestion("what depends on orders within 8 hops?", [node])).toThrow();
    expect(() => resolveGraphQuestion("what feeds orders; DELETE all", [node])).toThrow();
  });
  it("resolves direction, exact asset and bounded depth", () => {
    expect(resolveGraphQuestion("Show downstream of sales.orders within 2 hops", [node]))
      .toEqual({ direction: "downstream", depth: 2, node });
  });
  it("refuses ambiguous names instead of choosing the first asset", () => {
    expect(() => resolveGraphQuestion("upstream of orders", [node, { ...node, id: "b", qualified_name: "other.orders" }])).toThrow("Several");
  });
  it("does not accept arbitrary database statements or out-of-range depths", () => {
    expect(() => resolveGraphQuestion("MATCH (n) DELETE n", [node])).toThrow();
    expect(() => resolveGraphQuestion("downstream of orders within 99 hops", [node])).toThrow();
  });
  it("does not resolve an asset outside the loaded authorized graph", () => {
    expect(() => resolveGraphQuestion("upstream of secret", [node])).toThrow("not found");
  });
});
