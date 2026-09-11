import type { UnifiedLineageNodeRead } from "./types";

export type GraphQuestion = {
  direction: "upstream" | "downstream" | "both";
  depth: number;
  node: UnifiedLineageNodeRead;
};

/** Resolve a small, explicit query grammar against the authorized loaded graph.
 * No generated SQL/Cypher, fuzzy first-match selection, or new access scope. */
export function resolveGraphQuestion(text: string, nodes: UnifiedLineageNodeRead[]): GraphQuestion {
  let normalized = text.trim().replace(/\?$/, "").trim();
  const aliases: [RegExp, string][] = [
    [/^(?:what|which assets)\s+(?:depends? on|uses?)\s+(.+)$/i, "downstream of $1"],
    [/^(?:show\s+)?(?:the\s+)?impact of\s+(.+)$/i, "downstream of $1"],
    [/^what\s+feeds\s+(.+)$/i, "upstream of $1"],
    [/^where does\s+(.+?)\s+come from$/i, "upstream of $1"],
    [/^(?:show\s+)?(?:the\s+)?(?:dependencies|inputs) of\s+(.+)$/i, "upstream of $1"],
    [/^(?:show\s+)?(?:the\s+)?lineage of\s+(.+)$/i, "both of $1"],
  ];
  for (const [pattern, replacement] of aliases) {
    if (pattern.test(normalized)) { normalized = normalized.replace(pattern, replacement); break; }
  }
  const match = /^(?:show\s+)?(upstream|downstream|both)\s+of\s+(.+?)(?:\s+(?:within|up to)\s+([1-5])\s+hops?)?\s*$/i.exec(normalized);
  if (!match) throw new Error('Ask "what depends on asset?", "what feeds asset?", or "lineage of asset within 3 hops" (1–5 hops).');
  const name = match[2]!.replace(/^"(.*)"$/, "$1").trim().toLowerCase();
  const matches = nodes.filter(n => [n.label, n.qualified_name, n.id].some(v => v.toLowerCase() === name));
  if (!matches.length) throw new Error("Asset not found in the loaded graph. Use its exact name, or load the correct source and bounds.");
  if (matches.length > 1) throw new Error("Several assets match. Use a unique qualified name or node ID.");
  return { direction: match[1]!.toLowerCase() as GraphQuestion["direction"], depth: Number(match[3] ?? 3), node: matches[0]! };
}
