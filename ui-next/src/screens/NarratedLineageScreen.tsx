import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { UnifiedLineageGraphRead, UnifiedLineageImpactNodeRead, UnifiedLineageImpactRead } from "../lib/types";
import type { CatalogRowRead } from "../lib/ui-types";
import {
  ApiError,
  fetchCatalogRows,
  fetchLineageImpact,
  fetchLineageGraph,
} from "../lib/api";
import { Empty, ErrorState, Field, Pill } from "../components/primitives";
import { LineageGraph } from "../components/LineageGraph";
import { useDatasourcePicker } from "../lib/useDatasourcePicker";
import type { Tone } from "../components/primitives";
import "./NarratedLineageScreen.css";

/* ---------------------------------------------------------------------------
   UX-20: narrated lineage traversal as the PRIMARY lineage surface.

   Built against the real `GET /v1/datasources/{id}/unified-lineage/impact/{node}`
   (`unified_lineage_api.py::build_unified_lineage_impact_payload`), the same
   bounded multi-hop traversal the native MCP tool `atlas__get_lineage_impact`
   serves. Every hop's evidence is real, server-computed data, not invented
   narration text:
     - `depth`                      how many hops from the question's subject
     - `contributing_edge_sources`  which real lineage mechanism produced the
                                     hop -- a foreign key, a dbt dependency, an
                                     OpenLineage ETL edge, a view/procedure
                                     definition, or a steward-approved
                                     suggested relationship

   "Streams" here means client-side pacing over an already-fetched, already-
   bounded response (the endpoint has no server-sent-events mode) -- each hop
   appears in turn rather than the whole traversal materializing at once, so
   a long chain reads as a narrated sequence instead of a wall of text. The
   full graph endpoint (`.../unified-lineage/graph`) and its Cytospace-based
   canvas renderer (LN-8, `ui/scripts/graph-engine.js`) stay exactly where
   they are in the legacy shell -- UX-16 (retiring `ui/`) has not happened
   yet, and re-implementing LN-8's virtualized DAG canvas in `ui-next` would
   duplicate real, already-shipped, already-tested work rather than migrate
   it. What ships here instead as the "supporting view" is a lightweight,
   depth-swimlane rendering of the SAME impact response the narration reads
   -- real node placement (grouped by real `depth`), not a fabricated graph.
--------------------------------------------------------------------------- */

import { useOrgId } from "../lib/org";

const EDGE_SOURCE_LABEL: Record<string, string> = {
  FOREIGN_KEY: "a foreign key",
  SUGGESTED_RELATIONSHIP: "a steward-approved suggested relationship",
  DBT_DEPENDENCY: "a dbt dependency",
  OPENLINEAGE_ETL: "an OpenLineage ETL run",
  VIEW_DEFINITION: "a view definition",
  PROCEDURE_DEFINITION: "a procedure definition",
};

function humanizeSources(sources: readonly string[]): string {
  if (sources.length === 0) return "an unlabeled lineage edge";
  return sources.map((s) => EDGE_SOURCE_LABEL[s] ?? s.toLowerCase()).join(" and ");
}

const kindTone = (k: string): Tone => (k === "UNRESOLVED_DATASET" ? "mute" : "info");

function useUrlState() {
  const [params, setParams] = useState(() => new URLSearchParams(location.search));
  const update = useCallback((patch: Record<string, string | null>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === "") next.delete(k);
        else next.set(k, v);
      }
      const query = next.toString();
      history.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}${location.hash}`);
      return next;
    });
  }, []);
  return [params, update] as const;
}

interface Hop {
  direction: "upstream" | "downstream";
  node: UnifiedLineageImpactNodeRead;
}

function orderedHops(impact: UnifiedLineageImpactRead): Hop[] {
  const upstream = [...impact.upstream].sort((a, b) => b.depth - a.depth); // farthest first, walking in toward the focus
  const downstream = [...impact.downstream].sort((a, b) => a.depth - b.depth); // nearest first, walking away from the focus
  return [
    ...upstream.map((node) => ({ direction: "upstream" as const, node })),
    ...downstream.map((node) => ({ direction: "downstream" as const, node })),
  ];
}

function NarratedStep({ hop, index, focusLabel }: { hop: Hop; index: number; focusLabel: string }) {
  const { node, direction } = hop;
  const relation = direction === "upstream" ? "feeds into" : "is fed by";
  const target = direction === "upstream" ? focusLabel : node.qualified_name;
  const source = direction === "upstream" ? node.qualified_name : focusLabel;
  return (
    <li className="narr__step" style={{ animationDelay: `${index * 60}ms` }}>
      <div className="narr__hopnum">{node.depth}</div>
      <div className="narr__text">
        <p className="narr__sentence">
          <b>{source}</b> {relation} <b>{target}</b>, contributed by {humanizeSources(node.contributing_edge_sources)}.
        </p>
        <div className="narr__badges">
          <Pill tone={kindTone(node.node_kind)}>{node.node_kind.toLowerCase().replace(/_/g, " ")}</Pill>
          <Pill tone={direction === "upstream" ? "warn" : "accent"}>
            {node.depth} hop{node.depth === 1 ? "" : "s"} {direction}
          </Pill>
        </div>
      </div>
    </li>
  );
}

export function Swimlanes({ impact }: { impact: UnifiedLineageImpactRead }) {
  const maxDepth = Math.max(1, ...impact.upstream.map((n) => n.depth), ...impact.downstream.map((n) => n.depth));
  const lanesUpstream = Array.from({ length: maxDepth }, (_, i) => maxDepth - i).map((d) =>
    impact.upstream.filter((n) => n.depth === d),
  );
  const lanesDownstream = Array.from({ length: maxDepth }, (_, i) => i + 1).map((d) =>
    impact.downstream.filter((n) => n.depth === d),
  );
  return (
    <div className="swim" role="img" aria-label="Lineage impact, grouped by hop distance from the focus node">
      <div className="swim__lanes">
        {lanesUpstream.map((nodes, i) => (
          <div key={`u${i}`} className="swim__lane">
            <div className="swim__lanehead">upstream · {maxDepth - i} hop{maxDepth - i === 1 ? "" : "s"}</div>
            {nodes.map((n) => (
              <div key={n.node_id} className="swim__node" title={n.qualified_name}>
                {n.label}
              </div>
            ))}
          </div>
        ))}
        <div className="swim__lane swim__lane--focus">
          <div className="swim__lanehead">focus</div>
          <div className="swim__node swim__node--focus" title={impact.focus_label}>
            {impact.focus_label}
          </div>
        </div>
        {lanesDownstream.map((nodes, i) => (
          <div key={`d${i}`} className="swim__lane">
            <div className="swim__lanehead">downstream · {i + 1} hop{i === 0 ? "" : "s"}</div>
            {nodes.map((n) => (
              <div key={n.node_id} className="swim__node" title={n.qualified_name}>
                {n.label}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function NarratedLineageScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const { datasources, preferredDatasourceId } = useDatasourcePicker(ORG);
  const dsId = params.get("ds") ?? preferredDatasourceId;
  const nodeId = params.get("node");
  const depth = Number(params.get("depth") ?? "5");
  const view = params.get("view") === "graph" ? "graph" : "narrated";

  const [search, setSearch] = useState("");
  const [candidates, setCandidates] = useState<CatalogRowRead[]>([]);
  const [searching, setSearching] = useState(false);

  const [impact, setImpact] = useState<UnifiedLineageImpactRead | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [visibleSteps, setVisibleSteps] = useState(0);
  const [graph, setGraph] = useState<UnifiedLineageGraphRead | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [graphRevision, setGraphRevision] = useState(0);

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const selectedDatasourceName = datasources.find((d) => d.id === dsId)?.name ?? null;

  useEffect(() => {
    if (!dsId) {
      setGraph(null);
      return;
    }
    const ac = new AbortController();
    setGraphLoading(true);
    setGraphError(null);
    fetchLineageGraph(dsId, ac.signal)
      .then((result) => { if (!ac.signal.aborted) setGraph(result); })
      .catch((reason: unknown) => {
        if (!ac.signal.aborted) {
          setGraphError(reason instanceof ApiError ? reason.detail : (reason as Error).message);
        }
      })
      .finally(() => { if (!ac.signal.aborted) setGraphLoading(false); });
    return () => ac.abort();
  }, [dsId, graphRevision]);

  const runSearch = useCallback(async () => {
    if (!search.trim()) {
      setCandidates([]);
      return;
    }
    setSearching(true);
    try {
      const page = await fetchCatalogRows({ organizationId: ORG, q: search, limit: 25 });
      setCandidates(
        selectedDatasourceName
          ? page.items.filter((r) => r.datasource_name === selectedDatasourceName)
          : page.items,
      );
    } finally {
      setSearching(false);
    }
  }, [search, selectedDatasourceName]);

  useEffect(() => {
    const t = setTimeout(() => void runSearch(), 250);
    return () => clearTimeout(t);
  }, [runSearch]);

  const load = useCallback(async () => {
    if (!dsId || !nodeId) {
      setImpact(null);
      return;
    }
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    setVisibleSteps(0);
    try {
      const result = await fetchLineageImpact(dsId, nodeId, { depth, nodeLimit: 200 }, ac.signal);
      if (seq !== reqSeq.current) return;
      setImpact(result);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [dsId, nodeId, depth]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const hops = useMemo(() => (impact ? orderedHops(impact) : []), [impact]);

  // Streaming reveal: one hop appears at a time rather than the whole
  // traversal materializing instantly. Bounded to `hops.length` steps
  // (the endpoint's own `node_limit`/`depth` bound this, so this can never
  // free-run) and cleared whenever a new impact result loads.
  useEffect(() => {
    if (hops.length === 0) return;
    if (visibleSteps >= hops.length) return;
    const t = setTimeout(() => setVisibleSteps((n) => Math.min(n + 1, hops.length)), 140);
    return () => clearTimeout(t);
  }, [hops.length, visibleSteps]);

  return (
    <div className="narrscreen">
      <header className="narrscreen__head">
        <h1 className="narrscreen__h1">Lineage explorer</h1>
        <p className="narrscreen__lede">
          Ask a root-cause question about one asset — every hop is real traversal
          evidence, not a hand-written story. The relationship graph distinguishes
          declared keys, inferred links, transformation dependencies and procedures.
        </p>
      </header>

      <div className="narrscreen__setup">
        <Field label="Datasource">
          <select
            value={dsId ?? ""}
            onChange={(e) => setParams({ ds: e.target.value || null, node: null })}
          >
            <option value="">Select a datasource…</option>
            {datasources.map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </select>
        </Field>
        <Field label="Trace from asset">
          <input
            type="search"
            value={search}
            placeholder="table or view name…"
            onChange={(e) => setSearch(e.target.value)}
            disabled={!dsId}
          />
        </Field>
        <Field label="Depth">
          <select value={String(depth)} onChange={(e) => setParams({ depth: e.target.value })}>
            {[1, 2, 3, 5, 8].map((d) => (
              <option key={d} value={d}>{d} hop{d === 1 ? "" : "s"}</option>
            ))}
          </select>
        </Field>
      </div>

      {dsId && !nodeId && search.trim() ? (
        <div className="narrscreen__candidates">
          {searching ? (
            <p className="narrscreen__hint">Searching…</p>
          ) : candidates.length === 0 ? (
            <p className="narrscreen__hint">No matching assets in this datasource.</p>
          ) : (
            <ul className="narrscreen__candlist">
              {candidates.map((c) => (
                <li key={c.id}>
                  <button className="narrscreen__cand" onClick={() => setParams({ node: c.id })}>
                    <span className="narrscreen__candname">{c.name}</span>
                    <span className="narrscreen__candpath">{c.schema_name}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}

      {dsId ? (
        <div className="narrscreen__tabs" role="tablist">
          <button
            role="tab"
            aria-selected={view === "graph"}
            className={`narrscreen__tab${view === "graph" ? " narrscreen__tab--active" : ""}`}
            onClick={() => setParams({ view: "graph" })}
          >
            Graph (supporting view)
          </button>
          <button
            role="tab"
            aria-selected={view === "narrated"}
            className={`narrscreen__tab${view === "narrated" ? " narrscreen__tab--active" : ""}`}
            onClick={() => setParams({ view: null })}
          >
            Narrated
          </button>
        </div>
      ) : null}

      {!dsId ? (
        <Empty title="Pick a datasource to begin" hint="The unified-lineage endpoints are scoped per datasource." />
      ) : view === "graph" ? (
        graphError ? (
          <ErrorState title="The lineage graph could not be loaded" detail={graphError} onRetry={() => setGraphRevision((value) => value + 1)} />
        ) : graphLoading || !graph ? (
          <div className="narrscreen__skeleton" role="status" aria-live="polite">Building the bounded lineage graphâ€¦</div>
        ) : graph.nodes.length === 0 ? (
          <Empty title="No graph evidence yet" hint="Ingest constraints, dbt artifacts or OpenLineage events to connect this source's assets." />
        ) : (
          <LineageGraph
            graph={graph}
            focusNodeId={nodeId}
            onSelectNode={(id) => setParams({ node: id, view: null })}
          />
        )
      ) : !nodeId ? (
        <Empty title="Choose an asset for impact" hint="Search above, or open the relationship graph and select any table or model." />
      ) : error ? (
        <ErrorState title="Lineage impact could not be loaded" detail={error} onRetry={() => void load()} />
      ) : loading || !impact ? (
        <div className="narrscreen__skeleton" role="status" aria-live="polite">Tracing lineage…</div>
      ) : (
        <>
          {hops.length === 0 ? (
            <Empty title="No traversal from this asset" hint="This node has no upstream or downstream edges within the selected depth." />
          ) : (
            <>
              <ol className="narr" aria-live="polite" aria-label={`Traversal from ${impact.focus_label}`}>
                {hops.slice(0, visibleSteps || 1).map((hop, i) => (
                  <NarratedStep key={`${hop.direction}-${hop.node.node_id}`} hop={hop} index={i} focusLabel={impact.focus_label} />
                ))}
              </ol>
            </>
          )}

          {impact.upstream_truncated || impact.downstream_truncated ? (
            <p className="narrscreen__trunc">
              Truncated at this depth/node limit — narrower search or a smaller depth shows more of the true chain.
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}
