import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  PortfolioAnalyticsSummaryRead,
  PortfolioAnalyticsTrendsRead,
  PortfolioTopProductRead,
  PortfolioTrendPointRead,
} from "../lib/types";
import { ApiError, fetchPortfolioAnalyticsSummary, fetchPortfolioAnalyticsTrends } from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./PortfolioAnalyticsScreen.css";

/* ---------------------------------------------------------------------------
   Portfolio analytics — the operator/analytics dashboard over the data
   product marketplace that `MarketplaceScreen` (the catalog itself) does not
   cover. Composed from two real, already-merged `product_marketplace_api.py`
   routes:

     1. summary   GET .../portfolio-analytics/summary   lifecycle counts,
                  the access-request funnel, usage, quality and review-queue
                  depth, plus a ranked `top_products` list — everything as of
                  one `generated_at` snapshot.
     2. trends    GET .../portfolio-analytics/trends     the same window
                  bucketed into `bucket_days`-wide points, oldest first.

   Both take `window_days`; the summary additionally takes
   `low_quality_threshold` and `top_products_limit`, which this screen fixes
   at their server-side defaults (80, 10) and simply displays rather than
   exposing as editable filters (module scope: one selector, not a filter
   builder). The two calls are independent and separately abortable — a
   failed trends fetch must not blank an already-loaded summary, and vice
   versa, so each gets its own loading/error state rather than one shared
   "the dashboard could not be loaded" fallback.
--------------------------------------------------------------------------- */

const WINDOW_OPTIONS = [7, 30, 90] as const;
const nf = new Intl.NumberFormat("en-US");

function fmtNum(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : nf.format(n);
}

function fmtPct(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${Number.isInteger(n) ? n : n.toFixed(1)}%`;
}

function fmtDate(iso: string): string {
  return iso.slice(0, 10);
}

const certificationTone = (status: string): Tone => {
  const s = status.toUpperCase();
  if (s.includes("CERTIFIED") && !s.includes("UN")) return "ok";
  if (s.includes("PENDING") || s.includes("REQUIRED")) return "warn";
  if (s.includes("UNCERTIFIED") || s.includes("REJECTED")) return "bad";
  return "mute";
};

interface Stat {
  label: string;
  value: string;
}

function StatGrid({ stats }: { stats: Stat[] }) {
  return (
    <div className="pfa__stats">
      {stats.map((s) => (
        <div key={s.label} className="pfa__stat">
          <div className="pfa__stat_v tnum">{s.value}</div>
          <div className="pfa__stat_l">{s.label}</div>
        </div>
      ))}
    </div>
  );
}

function LifecyclePanel({ lifecycle }: { lifecycle: PortfolioAnalyticsSummaryRead["lifecycle"] }) {
  return (
    <section className="pfa__panel" aria-labelledby="pfa-lifecycle-h">
      <h2 className="pfa__h2" id="pfa-lifecycle-h">Lifecycle</h2>
      <div className="pfa__group">
        <div className="pfa__grouplabel">Data products</div>
        <StatGrid
          stats={[
            { label: "total", value: fmtNum(lifecycle.data_products_total) },
            { label: "active", value: fmtNum(lifecycle.data_products_active) },
            { label: "candidate", value: fmtNum(lifecycle.data_products_candidate) },
            { label: "retired", value: fmtNum(lifecycle.data_products_retired) },
          ]}
        />
      </div>
      <div className="pfa__group">
        <div className="pfa__grouplabel">Data product versions</div>
        <StatGrid
          stats={[
            { label: "draft", value: fmtNum(lifecycle.data_product_versions_draft) },
            { label: "review required", value: fmtNum(lifecycle.data_product_versions_review_required) },
            { label: "published", value: fmtNum(lifecycle.data_product_versions_published) },
            { label: "retired", value: fmtNum(lifecycle.data_product_versions_retired) },
          ]}
        />
      </div>
      <div className="pfa__group">
        <div className="pfa__grouplabel">Data contract versions</div>
        <StatGrid
          stats={[
            { label: "draft", value: fmtNum(lifecycle.data_contract_versions_draft) },
            { label: "review required", value: fmtNum(lifecycle.data_contract_versions_review_required) },
            { label: "published", value: fmtNum(lifecycle.data_contract_versions_published) },
          ]}
        />
      </div>
      <div className="pfa__group">
        <div className="pfa__grouplabel">Context products ({fmtNum(lifecycle.context_products_total)} total)</div>
        <StatGrid
          stats={[
            { label: "draft", value: fmtNum(lifecycle.context_product_versions_draft) },
            { label: "review required", value: fmtNum(lifecycle.context_product_versions_review_required) },
            { label: "published", value: fmtNum(lifecycle.context_product_versions_published) },
            { label: "deprecated", value: fmtNum(lifecycle.context_product_versions_deprecated) },
          ]}
        />
      </div>
    </section>
  );
}

function AccessPanel({ access }: { access: PortfolioAnalyticsSummaryRead["access"] }) {
  return (
    <section className="pfa__panel" aria-labelledby="pfa-access-h">
      <h2 className="pfa__h2" id="pfa-access-h">Access</h2>
      <div className="pfa__group">
        <div className="pfa__grouplabel">Requests</div>
        <StatGrid
          stats={[
            { label: "created", value: fmtNum(access.requests_created) },
            { label: "pending", value: fmtNum(access.requests_pending) },
            { label: "approved", value: fmtNum(access.requests_approved) },
            { label: "rejected", value: fmtNum(access.requests_rejected) },
            { label: "revoked", value: fmtNum(access.requests_revoked) },
            { label: "expired", value: fmtNum(access.requests_expired) },
          ]}
        />
      </div>
      <div className="pfa__group">
        <div className="pfa__grouplabel">Grants &amp; fulfillment</div>
        <StatGrid
          stats={[
            { label: "active grants", value: fmtNum(access.active_grants) },
            { label: "expiring within 30 days", value: fmtNum(access.grants_expiring_within_30_days) },
            { label: "fulfillment pending", value: fmtNum(access.fulfillment_pending) },
            { label: "fulfillment provisioned", value: fmtNum(access.fulfillment_provisioned) },
            { label: "fulfillment failed", value: fmtNum(access.fulfillment_failed) },
            { label: "fulfillment revoked", value: fmtNum(access.fulfillment_revoked) },
          ]}
        />
      </div>
    </section>
  );
}

function UsagePanel({ usage }: { usage: PortfolioAnalyticsSummaryRead["usage"] }) {
  return (
    <section className="pfa__panel" aria-labelledby="pfa-usage-h">
      <h2 className="pfa__h2" id="pfa-usage-h">Usage</h2>
      <div className="pfa__group">
        <div className="pfa__grouplabel">Unique consumers</div>
        <StatGrid
          stats={[
            { label: "context consumers", value: fmtNum(usage.unique_context_consumers) },
            { label: "MCP consumers", value: fmtNum(usage.unique_mcp_consumers) },
            { label: "agent principals", value: fmtNum(usage.unique_agent_principals) },
          ]}
        />
      </div>
      <div className="pfa__group">
        <div className="pfa__grouplabel">Reads &amp; operations</div>
        <StatGrid
          stats={[
            { label: "context product reads", value: fmtNum(usage.context_product_reads) },
            { label: "MCP operations", value: fmtNum(usage.mcp_operations) },
            { label: "MCP resource reads", value: fmtNum(usage.mcp_resource_reads) },
            { label: "MCP prompt reads", value: fmtNum(usage.mcp_prompt_reads) },
            { label: "MCP tool calls", value: fmtNum(usage.mcp_tool_calls) },
            { label: "MCP control operations", value: fmtNum(usage.mcp_control_operations) },
          ]}
        />
      </div>
      <div className="pfa__group">
        <div className="pfa__grouplabel">Agent runs</div>
        <StatGrid
          stats={[
            { label: "total", value: fmtNum(usage.agent_runs) },
            { label: "governed tool", value: fmtNum(usage.governed_tool_agent_runs) },
            { label: "model gateway", value: fmtNum(usage.model_gateway_agent_runs) },
            { label: "development override", value: fmtNum(usage.development_override_agent_runs) },
            { label: "policy blocked", value: fmtNum(usage.policy_blocked_agent_runs) },
          ]}
        />
      </div>
      <div className="pfa__group">
        <div className="pfa__grouplabel">Query &amp; tool executions</div>
        <StatGrid
          stats={[
            { label: "query executions", value: fmtNum(usage.query_executions) },
            { label: "governed tool executions", value: fmtNum(usage.governed_tool_executions) },
          ]}
        />
      </div>
    </section>
  );
}

function QualityPanel({ quality }: { quality: PortfolioAnalyticsSummaryRead["quality"] }) {
  return (
    <section className="pfa__panel" aria-labelledby="pfa-quality-h">
      <h2 className="pfa__h2" id="pfa-quality-h">Quality</h2>
      <StatGrid
        stats={[
          { label: "published products", value: fmtNum(quality.published_products) },
          { label: "scored products", value: fmtNum(quality.scored_products) },
          { label: "average quality score", value: fmtPct(quality.average_quality_score) },
          { label: "low quality products", value: fmtNum(quality.low_quality_products) },
          { label: "certified products", value: fmtNum(quality.certified_products) },
          { label: "uncertified products", value: fmtNum(quality.uncertified_products) },
          { label: "average lineage coverage", value: fmtPct(quality.average_lineage_coverage) },
        ]}
      />
    </section>
  );
}

function QueuesPanel({ queues }: { queues: PortfolioAnalyticsSummaryRead["queues"] }) {
  return (
    <section className="pfa__panel" aria-labelledby="pfa-queues-h">
      <h2 className="pfa__h2" id="pfa-queues-h">Review queues</h2>
      <StatGrid
        stats={[
          { label: "data product versions", value: fmtNum(queues.review_required_data_product_versions) },
          { label: "data contract versions", value: fmtNum(queues.review_required_data_contract_versions) },
          { label: "context product versions", value: fmtNum(queues.review_required_context_product_versions) },
          { label: "pending access requests", value: fmtNum(queues.pending_marketplace_access_requests) },
        ]}
      />
    </section>
  );
}

function TopProductsTable({ products }: { products: PortfolioTopProductRead[] }) {
  if (products.length === 0) {
    return <Empty title="No published products yet" hint="Top products appear once a version is published." />;
  }
  return (
    <div className="pfa__tablewrap">
      <table className="pfa__table" aria-label="Top products">
        <thead>
          <tr>
            <th>Name</th>
            <th>Domain</th>
            <th>Certification</th>
            <th className="pfa__num">Quality</th>
            <th className="pfa__num">Lineage</th>
            <th className="pfa__num">Access requests</th>
            <th className="pfa__num">Approved</th>
            <th className="pfa__num">Context reads</th>
          </tr>
        </thead>
        <tbody>
          {products.map((p) => (
            <tr key={p.data_product_version_id}>
              <td>
                <div className="pfa__pname">{p.name}</div>
                <div className="pfa__pkey">{p.product_key}</div>
              </td>
              <td>{p.domain_name}</td>
              <td>
                <Pill tone={certificationTone(p.certification_status)}>
                  {p.certification_status.toLowerCase().replace(/_/g, " ")}
                </Pill>
              </td>
              <td className="pfa__num tnum">{fmtPct(p.quality_score)}</td>
              <td className="pfa__num tnum">{fmtPct(p.lineage_coverage)}</td>
              <td className="pfa__num tnum">{fmtNum(p.access_request_count)}</td>
              <td className="pfa__num tnum">{fmtNum(p.approved_access_count)}</td>
              <td className="pfa__num tnum">{fmtNum(p.context_read_count)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const TREND_COLUMNS: { key: keyof PortfolioTrendPointRead; label: string }[] = [
  { key: "access_requests", label: "Access requests" },
  { key: "context_reads", label: "Context reads" },
  { key: "mcp_operations", label: "MCP operations" },
  { key: "mcp_tool_calls", label: "MCP tool calls" },
  { key: "agent_runs", label: "Agent runs" },
  { key: "governed_tool_runs", label: "Governed tool runs" },
  { key: "model_gateway_runs", label: "Model gateway runs" },
  { key: "query_executions", label: "Query executions" },
];

function TrendsTable({ points }: { points: PortfolioTrendPointRead[] }) {
  if (points.length === 0) {
    return <Empty title="No trend buckets in this window" />;
  }
  return (
    <div className="pfa__tablewrap">
      <table className="pfa__table" aria-label="Trends">
        <thead>
          <tr>
            <th>Bucket</th>
            {TREND_COLUMNS.map((c) => (
              <th key={c.key} className="pfa__num">{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {points.map((pt) => (
            <tr key={pt.bucket_start}>
              <td className="pfa__bucket">
                {fmtDate(pt.bucket_start)} – {fmtDate(pt.bucket_end)}
              </td>
              {TREND_COLUMNS.map((c) => (
                <td key={c.key} className="pfa__num tnum">{fmtNum(pt[c.key] as number)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PortfolioAnalyticsScreen() {
  const organizationId = useOrgId();
  const [params, setParams] = useUrlState();
  const windowDays = Number(params.get("window") ?? "30") || 30;

  const [summary, setSummary] = useState<PortfolioAnalyticsSummaryRead | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const summaryInflight = useRef<AbortController | null>(null);
  const summarySeq = useRef(0);

  const loadSummary = useCallback(async () => {
    summaryInflight.current?.abort();
    const ac = new AbortController();
    summaryInflight.current = ac;
    const seq = ++summarySeq.current;

    setSummaryLoading(true);
    setSummaryError(null);
    try {
      const s = await fetchPortfolioAnalyticsSummary({ organizationId, windowDays }, ac.signal);
      if (seq !== summarySeq.current) return;
      setSummary(s);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== summarySeq.current) return;
      setSummaryError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === summarySeq.current) setSummaryLoading(false);
    }
  }, [organizationId, windowDays]);

  useEffect(() => {
    void loadSummary();
    return () => summaryInflight.current?.abort();
  }, [loadSummary]);

  const [trends, setTrends] = useState<PortfolioAnalyticsTrendsRead | null>(null);
  const [trendsLoading, setTrendsLoading] = useState(true);
  const [trendsError, setTrendsError] = useState<string | null>(null);
  const trendsInflight = useRef<AbortController | null>(null);
  const trendsSeq = useRef(0);

  const loadTrends = useCallback(async () => {
    trendsInflight.current?.abort();
    const ac = new AbortController();
    trendsInflight.current = ac;
    const seq = ++trendsSeq.current;

    setTrendsLoading(true);
    setTrendsError(null);
    try {
      const t = await fetchPortfolioAnalyticsTrends({ organizationId, windowDays }, ac.signal);
      if (seq !== trendsSeq.current) return;
      setTrends(t);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== trendsSeq.current) return;
      setTrendsError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === trendsSeq.current) setTrendsLoading(false);
    }
  }, [organizationId, windowDays]);

  useEffect(() => {
    void loadTrends();
    return () => trendsInflight.current?.abort();
  }, [loadTrends]);

  const topProducts = useMemo(() => summary?.top_products ?? [], [summary]);

  return (
    <div className="pfa">
      <header className="pfa__head">
        <div>
          <h1 className="pfa__h1">Portfolio analytics</h1>
          <p className="pfa__lede">
            Org-wide operator view over the data product marketplace — lifecycle, access,
            usage, quality and review-queue depth, plus which products are actually being
            requested and read.
          </p>
        </div>
        <div className="pfa__controls">
          <Field label="Window">
            <select
              value={String(windowDays)}
              onChange={(e) => setParams({ window: e.target.value })}
            >
              {WINDOW_OPTIONS.map((d) => (
                <option key={d} value={d}>
                  {d} days
                </option>
              ))}
            </select>
          </Field>
          {summary ? (
            <div className="pfa__meta">
              <span>low-quality threshold: <b className="tnum">{summary.low_quality_threshold}%</b></span>
              <span className="pfa__generated">as of {summary.generated_at.slice(0, 16).replace("T", " ")}</span>
            </div>
          ) : null}
        </div>
      </header>

      {summaryError ? (
        <ErrorState
          title="Portfolio summary could not be loaded"
          detail={summaryError}
          onRetry={() => void loadSummary()}
        />
      ) : summaryLoading || !summary ? (
        <div className="pfa__skeleton" role="status" aria-live="polite">
          Loading portfolio summary…
        </div>
      ) : (
        <>
          <div className="pfa__grid">
            <LifecyclePanel lifecycle={summary.lifecycle} />
            <AccessPanel access={summary.access} />
            <UsagePanel usage={summary.usage} />
            <QualityPanel quality={summary.quality} />
            <QueuesPanel queues={summary.queues} />
          </div>

          <section className="pfa__panel pfa__panel--wide">
            <h2 className="pfa__h2">Top products</h2>
            <TopProductsTable products={topProducts} />
          </section>
        </>
      )}

      <section className="pfa__panel pfa__panel--wide">
        <h2 className="pfa__h2">Trends</h2>
        {trendsError ? (
          <ErrorState
            title="Trends could not be loaded"
            detail={trendsError}
            onRetry={() => void loadTrends()}
          />
        ) : trendsLoading || !trends ? (
          <div className="pfa__skeleton" role="status" aria-live="polite">
            Loading trends…
          </div>
        ) : (
          <TrendsTable points={trends.points} />
        )}
      </section>
    </div>
  );
}
