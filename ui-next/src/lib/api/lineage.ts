/* ---------------------------------------------------------------------------
   Lineage — how an asset reached its current state and what depends on it.

   The AI-decision refusal feed and one run's decisions, the impact traversal
   with its per-hop evidence, the graph a screen draws, and the unified
   cross-source graph.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr } from "./transport";

/** `GET /v1/ai-decisions/refusals` (LN-3, `ai_decision_lineage_api.py`) —
 *  every `REFUSAL`-kind AI decision for the organization: an agent run
 *  declining to use or act on an asset, gated behind `PlatformAdmin`/
 *  `DataAdmin` in production (the endpoint's own `require_roles`). */
export function fetchLineageRefusals(
  organizationId: string,
  opts: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<PageOf<AiDecisionRead>> {
  return demoOr(
    async () => makeFixtureRefusals(opts),
    async () => {
      const params = new URLSearchParams({ organization_id: organizationId });
      params.set("limit", String(opts.limit ?? 50));
      params.set("offset", String(opts.offset ?? 0));
      return get<PageOf<AiDecisionRead>>(`/v1/ai-decisions/refusals?${params}`, signal);
    },
  );
}

/** `GET /v1/ai-decisions/{run_id}` — every decision (not only refusals) the
 *  named agent run made, for the evidence pane behind one refusal: what the
 *  run considered and rejected before it refused. */
export function fetchRunDecisions(
  runId: string,
  organizationId: string,
  signal?: AbortSignal,
): Promise<AiDecisionRead[]> {
  return demoOr(
    async () => makeFixtureRunDecisions(runId),
    async () => {
      const params = new URLSearchParams({ organization_id: organizationId });
      return get<AiDecisionRead[]>(`/v1/ai-decisions/${runId}?${params}`, signal);
    },
  );
}

export interface LineageImpactQuery {
  depth?: number;
  nodeLimit?: number;
}

/** `GET /v1/datasources/{datasourceId}/unified-lineage/impact/{nodeId}`
 *  (`unified_lineage_api.py::build_unified_lineage_impact_payload`) — the
 *  bounded multi-hop upstream/downstream traversal UX-20 narrates. Each
 *  returned node's `depth` and `contributing_edge_sources` is the evidence
 *  per hop: how far the hop is from the question's subject, and which real
 *  lineage source (foreign key, dbt, OpenLineage, a view/procedure
 *  definition, or a steward-approved suggestion) contributed it — not
 *  narration text invented client-side. */
export function fetchLineageImpact(
  datasourceId: string,
  nodeId: string,
  query: LineageImpactQuery = {},
  signal?: AbortSignal,
): Promise<UnifiedLineageImpactRead> {
  return demoOr(
    async () => makeFixtureLineageImpact(datasourceId, nodeId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("depth", String(query.depth ?? 5));
      params.set("node_limit", String(query.nodeLimit ?? 200));
      return get<UnifiedLineageImpactRead>(
        `/v1/datasources/${datasourceId}/unified-lineage/impact/${encodeURIComponent(nodeId)}?${params}`,
        signal,
      );
    },
  );
}

/** Complete, server-bounded lineage graph for a datasource. Edges retain
 * their source, confidence and approval status so inferred relationships are
 * visually distinguishable from declared foreign keys. */
export function fetchLineageGraph(
  datasourceId: string,
  signal?: AbortSignal,
): Promise<UnifiedLineageGraphRead> {
  return demoOr(
    async () => makeFixtureLineageGraph(datasourceId),
    async () => {
      return get<UnifiedLineageGraphRead>(
        `/v1/datasources/${datasourceId}/unified-lineage/graph?node_limit=200&edge_limit=500`,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   Unified lineage -- nav id `unified-lineage`, `UnifiedLineageScreen`'s own
   routes. See that screen's file-top comment for the full endpoint list and
   what was deliberately left out of scope (domain scope / cross-boundary
   grants, the legacy force-directed graph engine).
--------------------------------------------------------------------------- */

export interface UnifiedLineageGraphQuery {
  nodeLimit?: number;
  edgeLimit?: number;
  suggestionStatus?: "ALL" | "PENDING" | "APPROVED" | "REJECTED";
}

/** `GET /v1/datasources/{datasourceId}/unified-lineage/graph`
 *  (`unified_lineage_api.py::get_unified_lineage_graph`, ~line 1181) -- the
 *  merged FK + suggested + dbt + OpenLineage + view/procedure graph for one
 *  datasource, with the real, configurable `node_limit`/`edge_limit`/
 *  `suggestion_status` query params `UnifiedLineageScreen`'s own controls
 *  need. `fetchLineageGraph` (above) already exists but hardcodes
 *  `node_limit=200&edge_limit=500` with no `suggestion_status` param -- it
 *  was built for a different, narrower purpose and is left exactly as it
 *  landed rather than edited in place, matching this file's own established
 *  convention of adding a new function alongside an earlier one instead of
 *  changing a shipped call site's behaviour out from under it. */
export function fetchUnifiedLineageGraph(
  datasourceId: string,
  query: UnifiedLineageGraphQuery = {},
  signal?: AbortSignal,
): Promise<UnifiedLineageGraphRead> {
  return demoOr(
    async () => makeFixtureUnifiedLineageGraph(datasourceId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("node_limit", String(query.nodeLimit ?? 300));
      params.set("edge_limit", String(query.edgeLimit ?? 1500));
      params.set("suggestion_status", query.suggestionStatus ?? "APPROVED");
      return get<UnifiedLineageGraphRead>(
        `/v1/datasources/${datasourceId}/unified-lineage/graph?${params}`,
        signal,
      );
    },
  );
}
