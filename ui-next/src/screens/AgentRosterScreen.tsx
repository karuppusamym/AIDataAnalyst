import { useCallback, useEffect, useState } from "react";
import type { AgentRosterEntryRead, AgentRunOutcomeRead } from "../lib/types";
import { ApiError, fetchAgentRoster } from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./AgentRosterScreen.css";

/* ---------------------------------------------------------------------------
   Agent roster — UX-19.

   "Each agent's method is inspectable before its output is trusted." One
   call (`GET /v1/organizations/{org}/ai-agents/roster`) returns, for every
   registered AGENT-kind AI asset: its published purpose (the AI registry
   entry a steward wrote and approved), an aggregated method summary (what
   this organization's governed agent runtime has actually been doing lately
   — strategy mix, confidence, tool-first rate), a bounded window of recent
   live results, and an honest auto-apply determination.

   The method summary and recent-results window are organization-wide, not
   private to one registered entity (`AgentRun` carries no per-agent foreign
   key today) — the server says so once, and this screen repeats that note
   rather than implying a precision the data does not have.
--------------------------------------------------------------------------- */

const WINDOWS = [7, 30, 90];

function riskTone(tier: string): Tone {
  const t = tier.toUpperCase();
  return t === "LOW" ? "ok" : t === "MEDIUM" ? "warn" : t === "CRITICAL" || t === "HIGH" ? "bad" : "mute";
}

function statusTone(status: string): Tone {
  const s = status.toUpperCase();
  if (s === "APPROVED") return "ok";
  if (s === "REVIEW_REQUIRED") return "warn";
  if (s === "DRAFT") return "mute";
  return "bad";
}

function runStatusTone(status: string): Tone {
  if (status === "COMPLETED") return "ok";
  if (status === "REJECTED" || status === "FAILED") return "bad";
  return "mute";
}

function percent(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function relative(iso: string): string {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (!Number.isFinite(minutes)) return "";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function StrategyBar({ label, count, total }: { label: string; count: number; total: number }) {
  const share = total > 0 ? Math.round((count / total) * 100) : 0;
  return (
    <li className="roster__strategy">
      <span className="roster__strategylabel">{label.replace(/_/g, " ").toLowerCase()}</span>
      <span className="roster__strategytrack">
        <span className="roster__strategyfill" style={{ width: `${share}%` }} />
      </span>
      <span className="roster__strategyvalue">
        {count} <span className="roster__muted">({share}%)</span>
      </span>
    </li>
  );
}

function RecentResultRow({ run }: { run: AgentRunOutcomeRead }) {
  return (
    <li className="roster__run">
      <Pill tone={runStatusTone(run.status)}>{run.status.toLowerCase()}</Pill>
      <span className="roster__runmeta">
        {run.strategy && <span>{run.strategy.replace(/_/g, " ").toLowerCase()}</span>}
        <span>confidence {percent(run.confidence)}</span>
        <span className="roster__muted">{run.generation_source}</span>
        {run.failure_reason && <span className="roster__negative">{run.failure_reason}</span>}
      </span>
      <span className="roster__muted">{relative(run.created_at)}</span>
    </li>
  );
}

function AgentCard({ entry }: { entry: AgentRosterEntryRead }) {
  const { purpose, method, recent_results, recent_results_total, auto_apply } = entry;
  const [expanded, setExpanded] = useState(false);
  const totalStrategies = Object.values(method.by_strategy).reduce((a, b) => a + b, 0);

  return (
    <article className="roster__card">
      <header className="roster__cardhead">
        <div className="roster__title">
          <h2>{purpose.name}</h2>
          <span className="roster__key">
            {purpose.asset_key} · v{purpose.version}
          </span>
        </div>
        <div className="roster__badges">
          <Pill tone={statusTone(purpose.status)}>{purpose.status.toLowerCase().replace(/_/g, " ")}</Pill>
          <Pill tone={riskTone(purpose.risk_tier)}>{purpose.risk_tier.toLowerCase()} risk</Pill>
          <Pill tone="mute">{purpose.provider_type.toLowerCase().replace(/_/g, " ")}</Pill>
        </div>
      </header>

      <p className="roster__description">{purpose.description}</p>

      <dl className="roster__facts">
        <div>
          <dt>Intended use</dt>
          <dd>{purpose.intended_use}</dd>
        </div>
        <div>
          <dt>Owner</dt>
          <dd>{purpose.owner_principal}</dd>
        </div>
        {purpose.documentation_url && (
          <div>
            <dt>Documentation</dt>
            <dd>
              <a href={purpose.documentation_url} target="_blank" rel="noreferrer">
                {purpose.documentation_url}
              </a>
            </dd>
          </div>
        )}
      </dl>

      <section className="roster__section">
        <h3>Method — last {method.window_days} days ({method.sampled_runs} runs sampled)</h3>
        {totalStrategies > 0 ? (
          <ul className="roster__strategies">
            {Object.entries(method.by_strategy)
              .sort(([, a], [, b]) => b - a)
              .map(([strategy, count]) => (
                <StrategyBar key={strategy} label={strategy} count={count} total={totalStrategies} />
              ))}
          </ul>
        ) : (
          <p className="roster__muted">No completed runs in this window yet.</p>
        )}
        <div className="roster__stats">
          <span>
            Average confidence <b>{percent(method.average_confidence)}</b>
          </span>
          <span>
            Tool-first rate <b>{percent(method.tool_first.rate)}</b>
            <Pill tone={method.tool_first.meets_target ? "ok" : "warn"}>
              target {percent(method.tool_first.target_rate)}
            </Pill>
          </span>
        </div>
      </section>

      <section className="roster__section">
        <h3>Auto-apply</h3>
        <div className="roster__autoapply">
          <Pill tone={auto_apply.has_auto_apply_branch ? "warn" : "ok"}>
            {auto_apply.has_auto_apply_branch ? "has an auto-apply branch" : "no auto-apply — human decides"}
          </Pill>
          {auto_apply.has_auto_apply_branch && auto_apply.threshold !== null && (
            <span className="roster__muted">
              threshold {auto_apply.threshold} ({auto_apply.threshold_source})
            </span>
          )}
        </div>
        <p className="roster__evidence">{auto_apply.evidence}</p>
      </section>

      <section className="roster__section">
        <button
          type="button"
          className="roster__sectiontoggle"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          <h3>
            Recent results <span className="roster__count">{recent_results.length} of {recent_results_total}</span>
          </h3>
          <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>
        </button>
        {expanded &&
          (recent_results.length > 0 ? (
            <ul className="roster__runs">
              {recent_results.map((run) => (
                <RecentResultRow key={run.run_id} run={run} />
              ))}
            </ul>
          ) : (
            <Empty title="No runs recorded in this window." />
          ))}
      </section>
    </article>
  );
}

export function AgentRosterScreen() {
  const organizationId = useOrgId();
  const [params, setParams] = useUrlState();
  const windowDays = Number(params.get("window") ?? 30);
  const [data, setData] = useState<Awaited<ReturnType<typeof fetchAgentRoster>> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      fetchAgentRoster(organizationId, { windowDays }, signal)
        .then((result) => {
          setData(result);
          setLoading(false);
        })
        .catch((err: unknown) => {
          if (signal?.aborted) return;
          setError(err instanceof ApiError ? err.message : String(err));
          setLoading(false);
        });
    },
    [organizationId, windowDays],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  if (error) {
    return (
      <section className="roster">
        <ErrorState title="The agent roster could not be loaded" detail={error} onRetry={() => load()} />
      </section>
    );
  }

  return (
    <section className="roster">
      <header className="roster__head">
        <div>
          <h1>Agent roster</h1>
          <p className="roster__sub">
            Every registered agent's published purpose, its actual recent method, and whether it can
            act without a human decision — inspect before you trust.
          </p>
        </div>
        <Field label="Window">
          <select
            value={String(windowDays)}
            onChange={(event) => setParams({ window: event.target.value })}
            aria-label="Roster window"
          >
            {WINDOWS.map((days) => (
              <option key={days} value={days}>
                {days} days
              </option>
            ))}
          </select>
        </Field>
      </header>

      {data && data.agents.length > 0 && (
        <p className="roster__scope" role="status">
          <span aria-hidden="true">ⓘ</span>
          {data.agents[0]!.method.note}
        </p>
      )}

      {loading && !data ? (
        <p role="status" className="roster__loading">Loading the agent roster…</p>
      ) : data && data.agents.length > 0 ? (
        <div className="roster__grid">
          {data.agents.map((entry) => (
            <AgentCard key={entry.purpose.asset_id} entry={entry} />
          ))}
        </div>
      ) : (
        <Empty
          title="No agent is registered in this organization yet."
          hint="Register an AGENT-kind asset in the AI registry to see its published purpose and method here."
        />
      )}
    </section>
  );
}
