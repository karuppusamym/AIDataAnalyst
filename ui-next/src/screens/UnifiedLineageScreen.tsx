import { layoutTopology } from "../lib/lineageLayout";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  UnifiedLineageEdgeRead,
  UnifiedLineageGraphRead,
  UnifiedLineageImpactNodeRead,
  UnifiedLineageImpactRead,
  UnifiedLineageNodeRead,
} from "../lib/types";
import { ApiError, fetchLineageImpact, fetchOrgDatasources, fetchUnifiedLineageGraph } from "../lib/api";
import {
  domainsWithDatasources,
  fetchDomainLineageGraph,
  fetchOrgDataDomains,
} from "../lib/_cross_source_api";
import { CrossBoundaryGrants } from "../components/CrossBoundaryGrants";
import type { DataDomainRead } from "../lib/types";
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
     - Domain scope. SHIPPED (2026-09-05), no longer deferred. The
       `Scope` select switches between one datasource
       (`GET /v1/datasources/{id}/unified-lineage/graph`) and every datasource
       in one data domain (`GET /v1/data-domains/{id}/unified-lineage/graph`),
       which is the only view in which a relationship spanning two systems
       renders as an edge at all. `DomainLineageGraphRead` is finally the type
       it was generated to be.

       The part that is not just a different URL: ADR-0017 SS4 / INV-5 make
       cross-domain visibility deny-by-default and never inherited, so the
       domain graph reports `withheld_cross_boundary_domain_ids` -- domains
       with candidates reaching in here that no ACTIVE grant covers. Those
       render as a banner naming each withheld domain, with the grant request
       one click away (`CrossBoundaryGrants`), rather than as a silently
       incomplete graph. Requesting is all this screen does; a grant goes
       ACTIVE only when a different principal approves its
       `CROSS_BOUNDARY_GRANT` review on the Review queue.

       Impact still resolves per datasource, because the impact endpoint is
       datasource-scoped. In domain scope a node id carries its owning
       datasource as a `{datasource_id}:` prefix (the merge step adds it to
       avoid false-merging two sources' same-named synthetic nodes), so
       selecting a node splits that prefix back off and asks the right
       datasource -- rather than disabling impact in the one view where
       cross-source questions actually arise.
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



const kindTone = (k: string): Tone => (k === "UNRESOLVED_DATASET" ? "mute" : "info");

const qualityTone = (q: string): Tone =>
  q === "PASSING" ? "ok" : q === "STALE" ? "warn" : q === "INCIDENT_OPEN" ? "bad" : "mute";

// Topology diagram is capped for legibility -- server-side node_limit alone
// can be up to 2000, which no straight-line column layout renders readably.
// The "Nodes"/"Edges" tabs below are never capped: this only bounds the
// *diagram*.
const COL_WIDTH = 208;
const MARGIN = 18;

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
  const [zoom, setZoom] = useState(1);
  const [detailsVisible, setDetailsVisible] = useState(true);
  const [maximized, setMaximized] = useState(false);
  const [neighborhood, setNeighborhood] = useState(true);
  const [params, setParams] = useUrlState();
  const ds = params.get("ds");
  // Scope lives in the URL alongside `ds`/`dom` so a domain-wide graph is as
  // shareable as a single-source one.
  const scopeKind = params.get("scope") === "domain" ? "domain" : "source";
  const dom = params.get("dom");
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

  // Domains that actually contain datasources. Listing every domain in the
  // organization would mostly offer empty graphs.
  const [domains, setDomains] = useState<DataDomainRead[]>([]);
  // Reported by the domain graph, never inferred here: domains with
  // candidates reaching into this one that no ACTIVE grant covers.
  const [withheldDomainIds, setWithheldDomainIds] = useState<string[]>([]);
  const [grantTargetDomainId, setGrantTargetDomainId] = useState<string | null>(null);

  const [impact, setImpact] = useState<UnifiedLineageImpactRead | null>(null);
  const [impactLoading, setImpactLoading] = useState(false);
  const [impactError, setImpactError] = useState<string | null>(null);

  const graphInflight = useRef<AbortController | null>(null);
  const graphSeq = useRef(0);
  const impactInflight = useRef<AbortController | null>(null);
  const impactSeq = useRef(0);

  useEffect(() => {
    const ac = new AbortController();
    let cancelled = false;
    void (async () => {
      try {
        const [allDomains, sources] = await Promise.all([
          fetchOrgDataDomains(ORG, ac.signal),
          fetchOrgDatasources(ORG, ac.signal),
        ]);
        if (cancelled) return;
        setDomains(domainsWithDatasources(allDomains, sources.items ?? []));
      } catch {
        // Degrades to an empty domain list: single-source scope keeps working
        // and only the domain option list is affected, matching how
        // `useDatasourcePicker` handles the same failure.
        if (!cancelled) setDomains([]);
      }
    })();
    return () => {
      cancelled = true;
      ac.abort();
    };
  }, [ORG]);

  const loadGraph = useCallback(async () => {
    graphInflight.current?.abort();
    const scopeId = scopeKind === "domain" ? dom : ds;
    if (!scopeId) {
      setGraph(null);
      setError(null);
      setWithheldDomainIds([]);
      setLoading(false);
      return;
    }
    const ac = new AbortController();
    graphInflight.current = ac;
    const seq = ++graphSeq.current;
    setLoading(true);
    setError(null);
    try {
      const options = {
        nodeLimit: Number(nodeLimit) || 300,
        edgeLimit: Number(edgeLimit) || 1500,
        suggestionStatus,
      };
      if (scopeKind === "domain") {
        const result = await fetchDomainLineageGraph(scopeId, options, ac.signal);
        if (seq !== graphSeq.current) return;
        // The two responses differ only in their scope key
        // (`data_domain_id` vs `datasource_id`); nodes, edges and counts are
        // the same shape, so everything downstream is untouched.
        setGraph({ ...result, datasource_id: "" } as unknown as UnifiedLineageGraphRead);
        setWithheldDomainIds(result.withheld_cross_boundary_domain_ids ?? []);
        return;
      }
      const result = await fetchUnifiedLineageGraph(scopeId, options, ac.signal);
      if (seq !== graphSeq.current) return;
      setGraph(result);
      setWithheldDomainIds([]);
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
  }, [ds, dom, scopeKind, nodeLimit, edgeLimit, suggestionStatus]);

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
  }, [ds, dom, scopeKind, suggestionStatus]);

  const loadImpact = useCallback(async () => {
    impactInflight.current?.abort();
    // The impact endpoint is datasource-scoped. In domain scope a node id
    // carries its owning datasource as a `{datasource_id}:` prefix (added by
    // the merge step so two sources' same-named synthetic nodes cannot false-
    // merge), so split it back off rather than disabling impact in the one
    // view where cross-source questions actually arise.
    const separator = selectedNodeId?.indexOf(":") ?? -1;
    const impactDatasourceId =
      scopeKind === "domain" && selectedNodeId && separator > 0
        ? selectedNodeId.slice(0, separator)
        : ds;
    const impactNodeId =
      scopeKind === "domain" && selectedNodeId && separator > 0
        ? selectedNodeId.slice(separator + 1)
        : selectedNodeId;
    if (!impactDatasourceId || !impactNodeId) {
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
      const result = await fetchLineageImpact(impactDatasourceId, impactNodeId, { depth: 5, nodeLimit: 200 }, ac.signal);
      if (seq !== impactSeq.current) return;
      setImpact(result);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== impactSeq.current) return;
      setImpactError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === impactSeq.current) setImpactLoading(false);
    }
  }, [ds, scopeKind, selectedNodeId]);

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

  const layout = useMemo(() => (graph ? layoutTopology(graph.nodes, filteredEdges, selectedNodeId, neighborhood) : null), [graph, filteredEdges, selectedNodeId, neighborhood]);

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
    <div className={`ult${maximized ? " ult--maximized" : ""}`}>
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
        <Field label="Scope">
          <select
            value={scopeKind}
            aria-label="Scope"
            onChange={(e) => setParams({ scope: e.target.value === "domain" ? "domain" : null, node: null })}
          >
            <option value="source">One data source</option>
            <option value="domain">Domain (all sources)</option>
          </select>
        </Field>
        {scopeKind === "domain" ? (
          <Field label="Data domain">
            <select
              value={dom ?? ""}
              aria-label="Data domain"
              onChange={(e) => setParams({ dom: e.target.value || null, node: null })}
            >
              <option value="">Select a domain…</option>
              {domains.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </Field>
        ) : (
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
        )}
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
        <Button
          variant="primary"
          disabled={(scopeKind === "domain" ? !dom : !ds) || loading}
          onClick={() => void loadGraph()}
        >
          {loading ? "Loading…" : "Load graph"}
        </Button>
      </div>

      <p className="ult__note">
        {scopeKind === "domain"
          ? "Every data source in one domain, merged — the only scope in which a relationship spanning two systems renders as an edge. Selecting a node still resolves impact against the source that owns it."
          : "One data source. Switch scope to Domain to see relationships that cross between sources."}
      </p>

      {/* Reported by the server, never inferred here. A domain with candidates
          reaching in but no ACTIVE grant is named rather than dropped, so an
          incomplete graph cannot pass for a complete one (ADR-0017 §4). */}
      {scopeKind === "domain" && withheldDomainIds.length > 0 ? (
        <div className="ult__withheld" role="status">
          <strong>Some edges are withheld.</strong>{" "}
          {withheldDomainIds.length === 1 ? "One domain has" : `${withheldDomainIds.length} domains have`}{" "}
          relationships reaching into this one that no active grant lets you see:{" "}
          {withheldDomainIds
            .map((id) => domains.find((d) => d.id === id)?.name ?? id)
            .join(", ")}
          .{" "}
          {/* Names the domain rather than saying "Request access": the panel
              below has its own general request control, and two identically
              labelled buttons would leave a steward guessing which one is
              about the problem they are looking at. */}
          <button
            className="ult__withheldlink"
            onClick={() => setGrantTargetDomainId(withheldDomainIds[0] ?? null)}
          >
            {`Request access to ${
              domains.find((d) => d.id === withheldDomainIds[0])?.name ?? withheldDomainIds[0]
            }`}
          </button>
        </div>
      ) : null}

      {scopeKind === "domain" && dom ? (
        <CrossBoundaryGrants
          domainId={dom}
          domains={domains}
          suggestedSourceDomainId={grantTargetDomainId}
          onGranted={() => void loadGraph()}
        />
      ) : null}

      {datasourcesError ? (
        <p className="ult__dserr" role="alert">
          {datasourcesError}
        </p>
      ) : null}

      <div className={`ult__layout${detailsVisible ? "" : " ult__layout--wide"}`}>
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

          {/* Scope-aware: in domain scope there is no `ds`, and gating the graph
              body on it alone left this pane showing "Pick a datasource" under
              a header already reporting the domain graph's node and edge
              counts. */}
          {!(scopeKind === "domain" ? dom : ds) ? (
            <Empty
              title={scopeKind === "domain" ? "Pick a data domain" : "Pick a datasource"}
              hint={
                scopeKind === "domain"
                  ? "A domain graph federates every data source inside one governance boundary."
                  : "The unified-lineage endpoints are scoped per datasource."
              }
            />
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
                    <div className="ult__viewtools">
                      <Button onClick={() => setZoom(Math.max(.5, zoom - .25))}>Zoom out</Button>
                      <Button onClick={() => setZoom(Math.min(4, zoom + .25))}>Zoom in</Button>
                      <Button onClick={() => setZoom(1)}>Fit graph</Button>
                      <Button onClick={() => setDetailsVisible(!detailsVisible)}>{detailsVisible ? "Hide details" : "Show details"}</Button>
                      <Button onClick={() => setMaximized(!maximized)}>{maximized ? "Exit expanded view" : "Expand graph"}</Button>
                      <label><input type="checkbox" checked={neighborhood} onChange={e => setNeighborhood(e.target.checked)} />Focus neighborhood (2 hops)</label>
                    </div>
                    <div className="ult__canvas">
                    <svg
                      className="ult__topo"
                      style={{ width: `${zoom * 100}%`, height: `${zoom * 100}%` }}
                      viewBox={`0 0 ${layout.width} ${layout.height}`}
                      role="img"
                      aria-label="Directed lineage, upstream to downstream"
                    >
                      <defs><marker id="lineage-direction" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10z" fill="currentColor" /></marker></defs>
                      {topologyEdges.map((e) => {
                        const s = layout.positions.get(e.source_node_id)!;
                        const t = layout.positions.get(e.target_node_id)!;
                        const layer = LAYER_DEFS.find((l) => l.key === layerOf(e.edge_source))!;
                        return (
                          <path
                            key={e.id}
                            fill="none"
                            markerEnd="url(#lineage-direction)"
                            d={`M ${s.x + 94} ${s.y} C ${s.x + 135} ${s.y}, ${t.x - 135} ${t.y}, ${t.x - 94} ${t.y}`}
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
                    </div>
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

        <aside hidden={!detailsVisible} className="ult__impact" aria-label="Impact">
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
