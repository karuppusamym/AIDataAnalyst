import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  UnifiedLineageEdgeRead,
  UnifiedLineageGraphRead,
  UnifiedLineageImpactNodeRead,
  UnifiedLineageImpactRead,
  UnifiedLineageNodeRead,
} from "../lib/types";
import { ApiError, fetchLineageImpact, fetchUnifiedLineageGraph } from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { useDatasourcePicker } from "../lib/useDatasourcePicker";
import { useOrgId } from "../lib/org";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./UnifiedLineageScreen.css";

/* ---------------------------------------------------------------------------
   Unified lineage — the legacy portal's `unified-lineage` view
   (`ui/index.html#unified-lineage-view`,
   `ui/scripts/features/context-lineage-control-plane.js`'s
   `loadUnifiedLineage`/`renderLineageGraph`/`inspectImpact`), ported onto the
   real, already-merged `unified_lineage_api.py` routes:

     GET /v1/datasources/{id}/unified-lineage/graph
         (`get_unified_lineage_graph`, ~line 1181) -- the merged FK +
         suggested + dbt + OpenLineage + view/procedure graph for one
         datasource. `node_limit`/`edge_limit` (defaults 300/1500, real
         server bounds 5-2000 / 5-10000 -- this screen's number inputs use
         those bounds, not the legacy HTML's stale 4000/20000 `max`
         attributes) and `suggestion_status` (ALL/PENDING/APPROVED/REJECTED,
         default APPROVED) all pass straight through, exactly as legacy's own
         `#unified-lineage-node-limit`/`#unified-lineage-edge-limit` inputs
         do for the first two. `fetchUnifiedLineageGraph` (`lib/api.ts`) is a
         new call added alongside the already-existing `fetchLineageGraph` --
         that one hardcodes `node_limit=200&edge_limit=500` and has no
         `suggestion_status` param, so it doesn't cover this screen's own
         controls; see that function's own doc comment for why it was left
         untouched rather than edited in place.

     GET /v1/datasources/{id}/unified-lineage/impact/{node_id}
         (`get_unified_lineage_impact`, ~line 1216) -- bounded multi-hop
         upstream/downstream traversal from one selected node, exactly
         `depth=5&node_limit=200` the way legacy's `inspectImpact` calls it
         (no depth control here, matching legacy 1:1). Served by
         `fetchLineageImpact`, which `NarratedLineageScreen` (UX-20) already
         built and this screen reuses verbatim rather than duplicating.

   Deliberately out of scope (documented, not silently dropped):

     - Legacy's `graph-engine.js` (41KB, hand-rolled force-directed layout,
       clustering, DOM virtualization -- two dedicated test files prove real
       sophistication: `graph-engine.clustering.test.mjs`,
       `graph-engine.virtualization.test.mjs`). `ui-next/package.json`
       deliberately carries almost no dependencies and no charting/graph
       library, and reproducing a force-directed engine by hand is its own
       multi-week project, not a screen port. What ships instead: the
       "Estate topology" panel below groups the real returned nodes into
       columns by their real `node_kind` and draws real edges between them
       as straight lines -- an honest, deterministic, un-clustered layout,
       capped to a legible number of rendered nodes (see
       `TOPOLOGY_NODE_CAP`) -- plus full, unclipped "Nodes" and "Edges"
       tabs (`VirtualList`, same windowed-DOM component `RelationshipsScreen`
       uses) so nothing the API actually returned is ever hidden, only the
       *diagram* is capped.
     - Domain scope. Legacy's screen really does have a second mode (the
       `#unified-lineage-scope` select's "Domain (all sources)" option,
       hitting `GET /v1/data-domains/{id}/unified-lineage/graph` and, when a
       related domain is withheld, a whole cross-boundary-grant-request
       dialog, ADR-0017 SS4). Nothing in `ui-next` today -- no domain list
       fetch, no grant-request flow -- exists to build on for that; adding it
       here would mean building an entire second un-ported subsystem inside
       one screen's port. `DomainLineageGraphRead` (`lib/types.ts`) is
       present but unused for the same reason. Single-datasource scope only.
     - Legacy's free-text canvas search (`matchNode`, dims every non-matching
       node) is a capability of the retired canvas engine, not a separate
       feature to reproduce; the "Nodes" tab's `VirtualList` is browsable in
       full instead.
     - The layer legend (`ui/styles/context-lineage.css`'s `.lineage-legend`)
       is static color-key text in legacy -- clicking it does nothing. Here
       it is real, working filter chips (`aria-pressed`) over the same four
       categories plus a fifth for the two edge sources the legend never
       named (VIEW_DEFINITION/PROCEDURE_DEFINITION, LN-2) -- a small, honest
       improvement the list-based rendering makes easy, not a legacy feature.
     - `suggestion_status` itself has no legacy control at all (legacy always
       gets the server default, APPROVED); exposing it here is new, and
       documented as new rather than presented as a port.
--------------------------------------------------------------------------- */

type LayerKey = "FK" | "SUGGESTED" | "DBT" | "OL" | "OTHER";

const LAYER_DEFS: { key: LayerKey; label: string; tone: Tone; sources: UnifiedLineageEdgeRead["edge_source"][] }[] = [
  { key: "FK", label: "FK", tone: "info", sources: ["FOREIGN_KEY"] },
  { key: "SUGGESTED", label: "Suggested", tone: "warn", sources: ["SUGGESTED_RELATIONSHIP"] },
  { key: "DBT", label: "dbt", tone: "accent", sources: ["DBT_DEPENDENCY"] },
  { key: "OL", label: "OpenLineage", tone: "ok", sources: ["OPENLINEAGE_ETL"] },
  { key: "OTHER", label: "View / procedure", tone: "mute", sources: ["VIEW_DEFINITION", "PROCEDURE_DEFINITION"] },
];

function layerOf(source: UnifiedLineageEdgeRead["edge_source"]): LayerKey {
  return LAYER_DEFS.find((l) => l.sources.includes(source))?.key ?? "OTHER";
}

const NODE_KIND_ORDER: UnifiedLineageNodeRead["node_kind"][] = [
  "TABLE",
  "DBT_SOURCE",
  "DBT_SEED",
  "DBT_MODEL",
  "DBT_SNAPSHOT",
  "UNRESOLVED_DATASET",
];

const kindTone = (k: string): Tone => (k === "UNRESOLVED_DATASET" ? "mute" : "info");

const qualityTone = (q: string): Tone =>
  q === "PASSING" ? "ok" : q === "STALE" ? "warn" : q === "INCIDENT_OPEN" ? "bad" : "mute";

// Topology diagram is capped for legibility -- server-side node_limit alone
// can be up to 2000, which no straight-line column layout renders readably.
// The "Nodes"/"Edges" tabs below are never capped: this only bounds the
// *diagram*.
const TOPOLOGY_NODE_CAP = 90;
const COL_WIDTH = 208;
const ROW_HEIGHT = 32;
const MARGIN = 18;
const HEADER_H = 26;

interface TopologyLayout {
  width: number;
  height: number;
  columns: { kind: string; x: number; nodes: UnifiedLineageNodeRead[] }[];
  positions: Map<string, { x: number; y: number }>;
  shown: number;
  omitted: number;
}

function layoutTopology(nodes: readonly UnifiedLineageNodeRead[]): TopologyLayout {
  const shownNodes = nodes.slice(0, TOPOLOGY_NODE_CAP);
  const byKind = new Map<string, UnifiedLineageNodeRead[]>();
  for (const n of shownNodes) {
    const list = byKind.get(n.node_kind) ?? [];
    list.push(n);
    byKind.set(n.node_kind, list);
  }
  const orderedKinds = [
    ...NODE_KIND_ORDER.filter((k) => byKind.has(k)),
    ...[...byKind.keys()].filter((k) => !NODE_KIND_ORDER.includes(k as UnifiedLineageNodeRead["node_kind"])),
  ];
  const positions = new Map<string, { x: number; y: number }>();
  const columns = orderedKinds.map((kind, ci) => {
    const x = MARGIN + ci * COL_WIDTH + COL_WIDTH / 2;
    const kindNodes = byKind.get(kind) ?? [];
    kindNodes.forEach((n, ri) => {
      positions.set(n.id, { x, y: MARGIN + HEADER_H + ri * ROW_HEIGHT + ROW_HEIGHT / 2 });
    });
    return { kind, x, nodes: kindNodes };
  });
  const maxRows = Math.max(1, ...columns.map((c) => c.nodes.length));
  return {
    width: Math.max(COL_WIDTH, MARGIN * 2 + orderedKinds.length * COL_WIDTH),
    height: MARGIN * 2 + HEADER_H + maxRows * ROW_HEIGHT,
    columns,
    positions,
    shown: shownNodes.length,
    omitted: nodes.length - shownNodes.length,
  };
}

function NodeRow({
  node,
  selected,
  onSelect,
}: {
  node: UnifiedLineageNodeRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button className={`ult__noderow${selected ? " ult__noderow--sel" : ""}`} onClick={onSelect}>
      <Pill tone={kindTone(node.node_kind)}>{node.node_kind.toLowerCase().replace(/_/g, " ")}</Pill>
      <span className="ult__nodename">
        {node.label}
        {node.resolved === false ? <span className="ult__unresolved"> · unresolved</span> : null}
      </span>
      <span className="ult__nodeqn">{node.qualified_name}</span>
      <span className="ult__nodecounts tnum">
        {node.inbound_edge_count}↑ {node.outbound_edge_count}↓
      </span>
    </button>
  );
}

function EdgeRow({ edge }: { edge: UnifiedLineageEdgeRead }) {
  const layer = LAYER_DEFS.find((l) => l.key === layerOf(edge.edge_source))!;
  const sourceColumns = edge.source_columns ?? [];
  const targetColumns = edge.target_columns ?? [];
  return (
    <div className="ult__edgerow">
      <Pill tone={layer.tone}>{layer.label}</Pill>
      <span className="ult__edgelabel">
        {edge.source_label} <span aria-hidden="true">→</span> {edge.target_label}
      </span>
      <span className="ult__edgemeta">
        {edge.status.toLowerCase()} · {Math.round(edge.confidence * 100)}%
      </span>
      {sourceColumns.length || targetColumns.length ? (
        <span className="ult__edgecols">
          {sourceColumns.join(", ")}
          {sourceColumns.length && targetColumns.length ? " → " : ""}
          {targetColumns.join(", ")}
        </span>
      ) : null}
    </div>
  );
}

function ImpactRow({ direction, item }: { direction: "Upstream" | "Downstream"; item: UnifiedLineageImpactNodeRead }) {
  const qualityState = item.quality_state ?? "UNKNOWN";
  return (
    <tr>
      <td>{direction}</td>
      <td>
        <div className="ult__impactasset">{item.label}</div>
        <div className="ult__impactqn">{item.qualified_name}</div>
      </td>
      <td className="tnum">{item.depth}</td>
      <td>{item.contributing_edge_sources.map((s) => s.toLowerCase().replace(/_/g, " ")).join(", ")}</td>
      <td>
        <Pill tone={qualityTone(qualityState)}>{qualityState.toLowerCase().replace(/_/g, " ")}</Pill>
      </td>
    </tr>
  );
}

export function UnifiedLineageScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const ds = params.get("ds");
  const selectedNodeId = params.get("node");
  const tab = params.get("tab") === "nodes" || params.get("tab") === "edges" ? params.get("tab")! : "topology";

  const { datasources, error: datasourcesError } = useDatasourcePicker(ORG);

  const [nodeLimit, setNodeLimit] = useState("300");
  const [edgeLimit, setEdgeLimit] = useState("1500");
  const [suggestionStatus, setSuggestionStatus] = useState<"ALL" | "PENDING" | "APPROVED" | "REJECTED">("APPROVED");
  const [activeLayers, setActiveLayers] = useState<ReadonlySet<LayerKey>>(
    () => new Set(LAYER_DEFS.map((l) => l.key)),
  );

  const [graph, setGraph] = useState<UnifiedLineageGraphRead | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [impact, setImpact] = useState<UnifiedLineageImpactRead | null>(null);
  const [impactLoading, setImpactLoading] = useState(false);
  const [impactError, setImpactError] = useState<string | null>(null);

  const graphInflight = useRef<AbortController | null>(null);
  const graphSeq = useRef(0);
  const impactInflight = useRef<AbortController | null>(null);
  const impactSeq = useRef(0);

  const loadGraph = useCallback(async () => {
    graphInflight.current?.abort();
    if (!ds) {
      setGraph(null);
      setError(null);
      setLoading(false);
      return;
    }
    const ac = new AbortController();
    graphInflight.current = ac;
    const seq = ++graphSeq.current;
    setLoading(true);
    setError(null);
    try {
      const result = await fetchUnifiedLineageGraph(
        ds,
        { nodeLimit: Number(nodeLimit) || 300, edgeLimit: Number(edgeLimit) || 1500, suggestionStatus },
        ac.signal,
      );
      if (seq !== graphSeq.current) return;
      setGraph(result);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== graphSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === graphSeq.current) setLoading(false);
    }
    // node/edge limit are read here at call time only -- typing into those
    // inputs does not itself refetch, matching legacy's own
    // `Number($("#unified-lineage-node-limit")?.value ...)` read-at-click-time
    // behaviour for the same two controls.
  }, [ds, nodeLimit, edgeLimit, suggestionStatus]);

  // Auto-load once a datasource is selected (or already present in the URL
  // on mount) and whenever suggestion_status changes -- a cheap toggle, safe
  // to refire automatically. Switching datasource still requires "Load
  // graph" would match legacy's own missing `change` handler on
  // `#unified-lineage-source` exactly, but reads as a bug rather than a
  // feature in a picker component every other ui-next screen auto-loads on
  // change (`RelationshipsScreen`, `NarratedLineageScreen`) -- this screen
  // follows that established ui-next convention instead.
  useEffect(() => {
    void loadGraph();
    return () => graphInflight.current?.abort();
  }, [ds, suggestionStatus]);

  const loadImpact = useCallback(async () => {
    impactInflight.current?.abort();
    if (!ds || !selectedNodeId) {
      setImpact(null);
      setImpactError(null);
      setImpactLoading(false);
      return;
    }
    const ac = new AbortController();
    impactInflight.current = ac;
    const seq = ++impactSeq.current;
    setImpactLoading(true);
    setImpactError(null);
    try {
      const result = await fetchLineageImpact(ds, selectedNodeId, { depth: 5, nodeLimit: 200 }, ac.signal);
      if (seq !== impactSeq.current) return;
      setImpact(result);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== impactSeq.current) return;
      setImpactError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === impactSeq.current) setImpactLoading(false);
    }
  }, [ds, selectedNodeId]);

  useEffect(() => {
    void loadImpact();
    return () => impactInflight.current?.abort();
  }, [loadImpact]);

  const toggleLayer = useCallback((key: LayerKey) => {
    setActiveLayers((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const filteredEdges = useMemo(
    () => (graph ? graph.edges.filter((e) => activeLayers.has(layerOf(e.edge_source))) : []),
    [graph, activeLayers],
  );

  const layout = useMemo(() => (graph ? layoutTopology(graph.nodes) : null), [graph]);

  const topologyEdges = useMemo(() => {
    if (!layout) return [];
    return filteredEdges.filter((e) => layout.positions.has(e.source_node_id) && layout.positions.has(e.target_node_id));
  }, [filteredEdges, layout]);

  const selectNode = useCallback((id: string) => setParams({ node: id }), [setParams]);

  const impactRows = useMemo(() => {
    if (!impact) return [];
    return [
      ...[...impact.upstream].sort((a, b) => a.depth - b.depth).map((item) => ({ direction: "Upstream" as const, item })),
      ...[...impact.downstream].sort((a, b) => a.depth - b.depth).map((item) => ({ direction: "Downstream" as const, item })),
    ];
  }, [impact]);

  return (
    <div className="ult">
      <header className="ult__head">
        <div>
          <h1 className="ult__h1">Unified lineage</h1>
          <p className="ult__lede">
            Declared constraints, approved relationships, dbt dependencies, and OpenLineage runs, merged into one
            bounded, value-free graph — pick a node to see its bounded upstream/downstream impact.
          </p>
        </div>
      </header>

      <div className="ult__controls">
        <Field label="Data source">
          <select value={ds ?? ""} onChange={(e) => setParams({ ds: e.target.value || null, node: null })}>
            <option value="">Select a datasource…</option>
            {datasources.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Nodes">
          <input
            type="number"
            min={5}
            max={2000}
            value={nodeLimit}
            onChange={(e) => setNodeLimit(e.target.value)}
          />
        </Field>
        <Field label="Edges">
          <input
            type="number"
            min={5}
            max={10000}
            value={edgeLimit}
            onChange={(e) => setEdgeLimit(e.target.value)}
          />
        </Field>
        <Field label="Suggestions">
          <select
            value={suggestionStatus}
            onChange={(e) => setSuggestionStatus(e.target.value as typeof suggestionStatus)}
          >
            <option value="APPROVED">Approved</option>
            <option value="ALL">All</option>
            <option value="PENDING">Pending</option>
            <option value="REJECTED">Rejected</option>
          </select>
        </Field>
        <Button variant="primary" disabled={!ds || loading} onClick={() => void loadGraph()}>
          {loading ? "Loading…" : "Load graph"}
        </Button>
      </div>

      <p className="ult__note">
        Single-datasource scope only — legacy's federated "Domain (all sources)" mode, with its cross-boundary grant
        request flow (ADR-0017 §4), is deferred; see this file's header comment.
      </p>

      {datasourcesError ? (
        <p className="ult__dserr" role="alert">
          {datasourcesError}
        </p>
      ) : null}

      <div className="ult__layout">
        <article className="ult__main">
          <div className="ult__panelhead">
            <div>
              <p className="ult__eyebrow">VALUE-FREE GRAPH</p>
              <h2 className="ult__h2">Estate topology</h2>
            </div>
            {graph ? (
              <div className="ult__summary">
                <span>{graph.returned_node_count} nodes</span>
                <span>{graph.returned_edge_count} edges</span>
                <Pill tone={graph.truncated ? "warn" : "ok"}>{graph.truncated ? "Bounded result" : "Complete result"}</Pill>
              </div>
            ) : null}
          </div>

          <div className="ult__legend" role="group" aria-label="Filter by lineage source">
            {LAYER_DEFS.map((l) => (
              <button
                key={l.key}
                className={`ult__chip ult__chip--${l.tone}${activeLayers.has(l.key) ? " ult__chip--on" : ""}`}
                aria-pressed={activeLayers.has(l.key)}
                onClick={() => toggleLayer(l.key)}
              >
                {l.label}
              </button>
            ))}
          </div>

          {!ds ? (
            <Empty title="Pick a datasource" hint="The unified-lineage endpoints are scoped per datasource." />
          ) : error ? (
            <ErrorState title="Unified lineage graph could not be loaded" detail={error} onRetry={() => void loadGraph()} />
          ) : loading || !graph ? (
            <div className="ult__skeleton" role="status" aria-live="polite">
              Building bounded unified graph…
            </div>
          ) : graph.nodes.length === 0 ? (
            <Empty title="No lineage nodes" hint="Import catalog, dbt, or OpenLineage metadata first." />
          ) : (
            <>
              {graph.truncated ? (
                <p className="ult__trunc">Result bounded: {(graph.truncation_reasons ?? ["server limit reached"]).join(", ")}.</p>
              ) : null}

              <div className="ult__tabs" role="tablist">
                {(["topology", "nodes", "edges"] as const).map((t) => (
                  <button
                    key={t}
                    role="tab"
                    aria-selected={tab === t}
                    className={`ult__tab${tab === t ? " ult__tab--active" : ""}`}
                    onClick={() => setParams({ tab: t === "topology" ? null : t })}
                  >
                    {t === "topology" ? "Topology" : t === "nodes" ? `Nodes (${graph.nodes.length})` : `Edges (${filteredEdges.length})`}
                  </button>
                ))}
              </div>

              {tab === "topology" ? (
                layout && layout.columns.length > 0 ? (
                  <div className="ult__topowrap">
                    <svg
                      className="ult__topo"
                      width={layout.width}
                      height={layout.height}
                      viewBox={`0 0 ${layout.width} ${layout.height}`}
                      role="img"
                      aria-label="Estate topology, nodes grouped by kind"
                    >
                      {topologyEdges.map((e) => {
                        const s = layout.positions.get(e.source_node_id)!;
                        const t = layout.positions.get(e.target_node_id)!;
                        const layer = LAYER_DEFS.find((l) => l.key === layerOf(e.edge_source))!;
                        return (
                          <line
                            key={e.id}
                            x1={s.x}
                            y1={s.y}
                            x2={t.x}
                            y2={t.y}
                            className={`ult__topoedge ult__topoedge--${layer.tone}`}
                          />
                        );
                      })}
                      {layout.columns.map((col) => (
                        <text key={col.kind} x={col.x} y={MARGIN + 14} textAnchor="middle" className="ult__topocolhead">
                          {col.kind.toLowerCase().replace(/_/g, " ")} ({col.nodes.length})
                        </text>
                      ))}
                      {layout.columns.flatMap((col) =>
                        col.nodes.map((n) => {
                          const p = layout.positions.get(n.id)!;
                          const selected = n.id === selectedNodeId;
                          return (
                            <g
                              key={n.id}
                              transform={`translate(${p.x},${p.y})`}
                              className={`ult__toponode${selected ? " ult__toponode--sel" : ""}`}
                              role="button"
                              tabIndex={0}
                              aria-label={`Select ${n.qualified_name}`}
                              onClick={() => selectNode(n.id)}
                              onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && selectNode(n.id)}
                            >
                              <rect x={-COL_WIDTH / 2 + 10} y={-13} width={COL_WIDTH - 20} height={26} rx={5} />
                              <text x={0} y={4} textAnchor="middle">
                                {n.label.length > 24 ? `${n.label.slice(0, 23)}…` : n.label}
                              </text>
                            </g>
                          );
                        }),
                      )}
                    </svg>
                    {layout.omitted > 0 ? (
                      <p className="ult__topocap">
                        Showing {layout.shown} of {graph.nodes.length} nodes in the diagram — the "Nodes" tab lists all of
                        them.
                      </p>
                    ) : null}
                  </div>
                ) : (
                  <Empty title="No connected nodes in view" hint="Every returned node was filtered out by the active layer chips." />
                )
              ) : tab === "nodes" ? (
                <div className="ult__listwrap">
                  <VirtualList
                    items={graph.nodes}
                    getKey={(n) => n.id}
                    ariaLabel="Unified lineage nodes"
                    estimateSize={40}
                    renderItem={(n) => <NodeRow node={n} selected={n.id === selectedNodeId} onSelect={() => selectNode(n.id)} />}
                  />
                </div>
              ) : (
                <div className="ult__listwrap">
                  <VirtualList
                    items={filteredEdges}
                    getKey={(e) => e.id}
                    ariaLabel="Unified lineage edges"
                    estimateSize={40}
                    emptyState={<Empty title="No edges in the active layers" hint="Turn on a layer chip above to see its edges." />}
                    renderItem={(e) => <EdgeRow edge={e} />}
                  />
                </div>
              )}
            </>
          )}
        </article>

        <aside className="ult__impact" aria-label="Impact">
          {!selectedNodeId ? (
            <Empty title="Select a graph node" hint="Bounded upstream and downstream impact will appear here." />
          ) : impactError ? (
            <ErrorState title="Impact could not be loaded" detail={impactError} onRetry={() => void loadImpact()} />
          ) : impactLoading || !impact ? (
            <div className="ult__skeleton" role="status" aria-live="polite">
              Tracing impact…
            </div>
          ) : (
            <>
              <div className="ult__panelhead">
                <div>
                  <p className="ult__eyebrow">TRANSITIVE IMPACT</p>
                  <h2 className="ult__h2">{impact.focus_label}</h2>
                </div>
                <Pill tone={kindTone(impact.focus_node_kind)}>{impact.focus_node_kind.toLowerCase().replace(/_/g, " ")}</Pill>
              </div>
              {impactRows.length === 0 ? (
                <Empty title="No connected impact" />
              ) : (
                <div className="ult__impactscroll">
                  <table className="ult__impacttable">
                    <thead>
                      <tr>
                        <th>Direction</th>
                        <th>Asset</th>
                        <th>Depth</th>
                        <th>Evidence</th>
                        <th>Quality</th>
                      </tr>
                    </thead>
                    <tbody>
                      {impactRows.map(({ direction, item }) => (
                        <ImpactRow key={`${direction}-${item.node_id}`} direction={direction} item={item} />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {impact.upstream_truncated || impact.downstream_truncated ? (
                <p className="ult__trunc">Truncated at this depth/node limit — narrower search shows more of the true chain.</p>
              ) : null}
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
