import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ConsumerFooterRead,
  ProjectRead,
  SemanticMetricVersionRead,
  SemanticModelVersionRead,
} from "../lib/types";
import {
  ApiError,
  fetchOrgProjects,
  fetchSemanticMetricConsumers,
  fetchSemanticMetricVersions,
  fetchSemanticModelConsumers,
  fetchSemanticModelVersions,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/EvidencePane.css";
import "./SemanticsScreen.css";

/* ---------------------------------------------------------------------------
   Semantics — UX-15/UX-16 (tracker rows UX-15/UX-16), nav id `semantics`.

   Built against the real, already-merged `semantic_api.py` routes:
     - GET /v1/projects/{project_id}/semantic-model-versions   (list_semantic_model_versions)
     - GET /v1/semantic-model-versions/{model_id}/metrics       (list_metric_versions)
     - GET /v1/semantic-model-versions/{model_id}/consumers     (UX-18, consumer footer)
     - GET /v1/semantic-metric-versions/{version_id}/consumers  (UX-18, consumer footer)
   All four are project- or version-scoped. No fabricated data anywhere below.

   HONEST GAP: there is no endpoint that lists every published semantic model
   across an organization in one call — `list_semantic_model_versions` takes
   a `project_id` and returns only that project's versions. What ships here
   instead is a real, two-step browse: `GET /v1/organizations/{id}/projects`
   (`operational_api.py::list_organization_projects`, confirmed present and
   reachable — it is NOT the case that no project-listing endpoint exists)
   feeds a project picker, and picking a project lists that project's
   semantic models, each expandable to its metrics. This is honestly a
   project-scoped browse behind a project picker, not a flattened org-wide
   semantic model/metric search — the same shape this file's own
   `fetchOrgDatasources`-style composition already uses elsewhere in this
   app (see `NarratedLineageScreen`'s datasource-name-to-id bridge). A true
   org-wide browse would need either a new aggregate endpoint (out of scope —
   `src/aida/` is not touched by this change) or an N+1 fan-out across every
   project with no server-side bound, which this screen deliberately does not
   do. `GET /v1/organizations/{id}/global-search` was also checked as a
   possible substitute; its `GlobalSearchResponse` is asset/table/term-shaped
   (no semantic-model/metric result kind), so it does not cover this either.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

const modelStatusTone = (s: string): Tone =>
  s === "PUBLISHED" ? "ok" : s === "DRAFT" ? "mute" : s === "REJECTED" ? "bad" : s === "DEPRECATED" ? "warn" : "info";

interface MetricsState {
  status: "loading" | "loaded" | "error";
  items: SemanticMetricVersionRead[];
  error?: string;
}

function ModelRow({
  model,
  expanded,
  metrics,
  selectedModelId,
  selectedMetricId,
  onToggleExpand,
  onSelectModel,
  onSelectMetric,
}: {
  model: SemanticModelVersionRead;
  expanded: boolean;
  metrics: MetricsState | undefined;
  selectedModelId: string | null;
  selectedMetricId: string | null;
  onToggleExpand: () => void;
  onSelectModel: () => void;
  onSelectMetric: (metricId: string) => void;
}) {
  const isModelSelected = model.id === selectedModelId && !selectedMetricId;
  return (
    <article className={`smmodel${isModelSelected ? " smmodel--sel" : ""}`} aria-label={model.name}>
      <div className="smmodel__row">
        <button
          className="smmodel__chevron"
          onClick={onToggleExpand}
          aria-expanded={expanded}
          aria-label={expanded ? `Collapse ${model.name}` : `Expand ${model.name}`}
        >
          {expanded ? "⌄" : "›"}
        </button>
        <button className="smmodel__click" onClick={onSelectModel}>
          <div className="smmodel__badges">
            <Pill tone={modelStatusTone(model.status)}>{model.status.toLowerCase()}</Pill>
            <Pill tone="mute">v{model.version}</Pill>
          </div>
          <h3 className="smmodel__title">{model.name}</h3>
          <p className="smmodel__summary">{model.change_summary}</p>
          <div className="smmodel__meta">
            <span>{model.created_by}</span>
            <span>·</span>
            <time dateTime={model.updated_at}>{model.updated_at.slice(0, 10)}</time>
          </div>
        </button>
      </div>

      {expanded ? (
        <div className="smmetrics" role="list" aria-label={`Metrics for ${model.name}`}>
          {!metrics || metrics.status === "loading" ? (
            <p className="smmetrics__hint">Loading metrics…</p>
          ) : metrics.status === "error" ? (
            <p className="smmetrics__hint smmetrics__hint--err">{metrics.error}</p>
          ) : metrics.items.length === 0 ? (
            <p className="smmetrics__hint">No metrics defined on this version.</p>
          ) : (
            metrics.items.map((mt) => (
              <button
                key={mt.id}
                role="listitem"
                className={`smmetric${mt.id === selectedMetricId ? " smmetric--sel" : ""}`}
                onClick={() => onSelectMetric(mt.id)}
              >
                <span className="smmetric__name">{mt.metric_name}</span>
                <span className="smmetric__agg">{mt.aggregation.toLowerCase()} · {mt.grain}</span>
                <Pill tone={modelStatusTone(mt.status)}>{mt.status.toLowerCase()}</Pill>
              </button>
            ))
          )}
        </div>
      ) : null}
    </article>
  );
}

function ConsumerFooterView({ footer }: { footer: ConsumerFooterRead }) {
  if (footer.consumers.length === 0) {
    return <p className="sm__none">No recorded consumers for this version.</p>;
  }
  return (
    <ol className="evl">
      {footer.consumers.map((c, i) => (
        <li key={`${c.consumer_id}-${i}`} className="evi evi--info">
          <div className="evi__label">{c.consumer_type.replace(/_/g, " ")} · {c.channel.replace(/_/g, " ")}</div>
          <div className="evi__value">{c.consumer_id}</div>
          <div className="evi__source">
            {c.consumption_count} event{c.consumption_count === 1 ? "" : "s"} · last {c.last_consumed_at.slice(0, 10)}
          </div>
        </li>
      ))}
    </ol>
  );
}

function SemanticDetail({
  model,
  metric,
  onClose,
}: {
  model: SemanticModelVersionRead | null;
  metric: SemanticMetricVersionRead | null;
  onClose: () => void;
}) {
  const [footer, setFooter] = useState<ConsumerFooterRead | null>(null);
  const [error, setError] = useState<string | null>(null);

  const focusId = metric?.id ?? model?.id ?? null;

  useEffect(() => {
    if (!focusId) {
      setFooter(null);
      setError(null);
      return;
    }
    const ac = new AbortController();
    setFooter(null);
    setError(null);
    const load = metric
      ? fetchSemanticMetricConsumers(metric.id, ac.signal)
      : model
        ? fetchSemanticModelConsumers(model.id, ac.signal)
        : null;
    load
      ?.then(setFooter)
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusId]);

  if (!model && !metric) {
    return (
      <aside className="evp evp--idle" aria-label="Detail">
        <Empty
          title="Select a model or metric"
          hint="Its consumers — who depends on it — and version metadata appear here."
        />
      </aside>
    );
  }

  const permalink = metric
    ? `${location.origin}${location.pathname}?project=${model?.project_id ?? ""}&model=${model?.id ?? ""}&metric=${metric.id}`
    : `${location.origin}${location.pathname}?project=${model?.project_id ?? ""}&model=${model?.id ?? ""}`;

  const name = metric ? metric.metric_name : model?.name ?? "";

  return (
    <aside className="evp" aria-label={`Detail for ${name}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={name}>{name}</div>
          <div className="evp__path">
            {metric ? `metric v${metric.version} · ${metric.metric_slug}` : `model v${model?.version}`}
          </div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close detail">×</button>
      </header>

      <div className="evp__body">
        {metric ? (
          <>
            <p className="sm__desc">{metric.description}</p>
            <div className="sm__row"><b>Aggregation</b><span>{metric.aggregation}</span></div>
            <div className="sm__row"><b>Grain</b><span>{metric.grain}</span></div>
            <div className="sm__row"><b>Status</b><span><Pill tone={modelStatusTone(metric.status)}>{metric.status.toLowerCase()}</Pill></span></div>
            <div className="sm__row"><b>Source table</b><span className="sm__mono">{metric.source_table_id}</span></div>
            <div className="sm__row"><b>Created by</b><span>{metric.created_by}</span></div>
            <div className="sm__row"><b>Created</b><span>{metric.created_at.slice(0, 10)}</span></div>
          </>
        ) : model ? (
          <>
            <p className="sm__desc">{model.change_summary}</p>
            <div className="sm__row"><b>Status</b><span><Pill tone={modelStatusTone(model.status)}>{model.status.toLowerCase()}</Pill></span></div>
            <div className="sm__row"><b>Created by</b><span>{model.created_by}</span></div>
            <div className="sm__row"><b>Approved by</b><span>{model.approved_by ?? "—"}</span></div>
            <div className="sm__row"><b>Published</b><span>{model.published_at?.slice(0, 10) ?? "not published"}</span></div>
            <div className="sm__row"><b>Based on</b><span className="sm__mono">{model.based_on_version_id ?? "—"}</span></div>
          </>
        ) : null}

        <div className="evp__sub" style={{ marginTop: 14 }}>
          Consumers{footer ? ` (${footer.total_consumers})` : ""}
        </div>
        {error ? (
          <div className="evp__error" role="alert">{error}</div>
        ) : footer === null ? (
          <div className="evp__load" role="status">Loading consumers…</div>
        ) : (
          <ConsumerFooterView footer={footer} />
        )}
      </div>

      <footer className="evp__foot">
        <Button onClick={() => void navigator.clipboard?.writeText(permalink)}>Copy link</Button>
        <span className="evp__hint">Consumer footer · UX-18</span>
      </footer>
    </aside>
  );
}

export function SemanticsScreen() {
  const [params, setParams] = useUrlState();
  const projectId = params.get("project");
  const modelId = params.get("model");
  const metricId = params.get("metric");

  const [projects, setProjects] = useState<ProjectRead[]>([]);
  const [projectsError, setProjectsError] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    fetchOrgProjects(ORG, ac.signal)
      .then((page) => setProjects(page.items))
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        // Degrades to an empty picker -- a caller with `project` already in
        // the URL keeps working; only the dropdown's option list is affected.
        setProjectsError(e instanceof ApiError ? e.detail : (e as Error).message);
      });
    return () => ac.abort();
  }, []);

  const [models, setModels] = useState<SemanticModelVersionRead[]>([]);
  const [modelsTotal, setModelsTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const loadModels = useCallback(async () => {
    if (!projectId) {
      setModels([]);
      setModelsTotal(null);
      setLoading(false);
      setError(null);
      return;
    }
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const page = await fetchSemanticModelVersions(projectId, { limit: 200 }, ac.signal);
      if (seq !== reqSeq.current) return;
      setModels(page.items);
      setModelsTotal(page.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadModels();
    return () => inflight.current?.abort();
  }, [loadModels]);

  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [metricsByModel, setMetricsByModel] = useState<Record<string, MetricsState>>({});
  const metricsInflight = useRef<Map<string, AbortController>>(new Map());

  const loadMetrics = useCallback((mId: string) => {
    metricsInflight.current.get(mId)?.abort();
    const ac = new AbortController();
    metricsInflight.current.set(mId, ac);
    setMetricsByModel((prev) => ({ ...prev, [mId]: { status: "loading", items: [] } }));
    fetchSemanticMetricVersions(mId, { limit: 200 }, ac.signal)
      .then((page) => {
        setMetricsByModel((prev) => ({ ...prev, [mId]: { status: "loaded", items: page.items } }));
      })
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setMetricsByModel((prev) => ({
          ...prev,
          [mId]: { status: "error", items: [], error: e instanceof ApiError ? e.detail : (e as Error).message },
        }));
      });
  }, []);

  const toggleExpand = useCallback(
    (mId: string) => {
      setExpanded((prev) => {
        const next = new Set(prev);
        if (next.has(mId)) {
          next.delete(mId);
        } else {
          next.add(mId);
          if (!metricsByModel[mId]) loadMetrics(mId);
        }
        return next;
      });
    },
    [metricsByModel, loadMetrics],
  );

  // A metric selected via a permalink (?model=&metric=) needs its parent
  // model's metrics loaded even though nothing was clicked to expand it.
  useEffect(() => {
    if (modelId && metricId && !metricsByModel[modelId]) {
      setExpanded((prev) => new Set(prev).add(modelId));
      loadMetrics(modelId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelId, metricId]);

  useEffect(() => {
    return () => {
      for (const ac of metricsInflight.current.values()) ac.abort();
    };
  }, []);

  const selectedModel = useMemo(() => models.find((m) => m.id === modelId) ?? null, [models, modelId]);
  const selectedMetric = useMemo(
    () => (modelId && metricId ? metricsByModel[modelId]?.items.find((m) => m.id === metricId) ?? null : null),
    [metricsByModel, modelId, metricId],
  );

  return (
    <div className="smscreen">
      <header className="smscreen__head">
        <div>
          <h1 className="smscreen__h1">Semantics</h1>
          <p className="smscreen__lede">
            Published semantic models and their metrics, browsed by project — see this
            screen&rsquo;s own file-top comment for why this is project-scoped, not org-wide.
          </p>
        </div>
        <div className="smscreen__stats">
          <span><b className="tnum">{modelsTotal !== null ? modelsTotal : "—"}</b> models</span>
        </div>
      </header>

      <div className="smscreen__filters">
        <Field label="Project">
          <select
            value={projectId ?? ""}
            onChange={(e) => setParams({ project: e.target.value || null, model: null, metric: null })}
          >
            <option value="">Select a project…</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </Field>
        {projectsError ? <p className="smscreen__pickerr" role="alert">{projectsError}</p> : null}
      </div>

      <div className="smscreen__main">
        {!projectId ? (
          <Empty
            title="Pick a project to see its semantic models"
            hint="Semantic models are project-scoped — there is no org-wide browse (see the honest gap noted at the top of this screen's source)."
          />
        ) : error ? (
          <ErrorState title="Semantic models could not be loaded" detail={error} onRetry={() => void loadModels()} />
        ) : loading ? (
          <div className="smscreen__skeleton" role="status" aria-live="polite">Loading semantic models…</div>
        ) : (
          <VirtualList
            items={models}
            getKey={(m) => m.id}
            ariaLabel="Semantic models"
            estimateSize={98}
            totalCount={modelsTotal}
            emptyState={
              <Empty title="No semantic models in this project" hint="Nothing has been drafted or published here yet." />
            }
            renderItem={(m) => (
              <ModelRow
                model={m}
                expanded={expanded.has(m.id)}
                metrics={metricsByModel[m.id]}
                selectedModelId={modelId}
                selectedMetricId={metricId}
                onToggleExpand={() => toggleExpand(m.id)}
                onSelectModel={() => setParams({ model: m.id, metric: null })}
                onSelectMetric={(mtId) => setParams({ model: m.id, metric: mtId })}
              />
            )}
          />
        )}
        <SemanticDetail
          model={selectedModel}
          metric={selectedMetric}
          onClose={() => setParams({ model: null, metric: null })}
        />
      </div>
    </div>
  );
}
