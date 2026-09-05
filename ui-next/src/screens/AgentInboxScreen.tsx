import { useCallback, useEffect, useMemo, useState } from "react";
/* The generated names are `InboxAgent`/`InboxPendingItem` (the server's own
   `InboxAgent`/`InboxPendingItem` schemas, reached through `AgentInboxRead`).
   They were hand-renamed inside the generated `types.ts`, which is why the
   `ui-types-diff` gate failed; aliasing at the import keeps this file's own
   reading unchanged without editing a generated file. */
import type {
  InboxAgent as AgentInboxAgent,
  InboxPendingItem as AgentInboxPendingItem,
  AgentInboxRead,
} from "../lib/types";
import { ApiError, engageAgentKillSwitch, fetchAgentInbox } from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Field, Pill, StateDot } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./AgentInboxScreen.css";

/* ---------------------------------------------------------------------------
   Agent inbox — UX-21 (AG-10 / ADR-0027).

   The screen no competitor has, because none of them separates agent
   proposals from human decisions structurally. It answers two questions in
   one place: what did my agents do, and what is waiting on me.

   One call: `GET /v1/organizations/{org}/agent-inbox`, composed server-side
   in a fixed number of queries. The panels below are all views of that
   single payload, so the screen cannot show a summary that disagrees with
   the list underneath it.

   Ordering of the pending list is the server's (blast radius desc, then
   confidence desc) and is deliberately not re-sorted here — "why is this
   first" must have one answer, and it is the one in the API.
--------------------------------------------------------------------------- */

const TIER_TONE: Record<string, Tone> = {
  T0: "mute",
  T1: "info",
  T2: "warn",
  T3: "bad",
};

const RECOMMENDATION_TONE: Record<string, Tone> = {
  APPROVE: "ok",
  REJECT: "bad",
  NONE: "mute",
};

const PERSONAS = ["ANALYST", "CONSUMER", "STEWARD", "REVIEWER", "OPERATOR", "AUDITOR"];

function relative(iso: string): string {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (!Number.isFinite(minutes)) return "";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function percent(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function BudgetBar({ agent }: { agent: AgentInboxAgent }) {
  const { daily_token_cap: cap, daily_tokens_estimated: used } = agent.budget;
  if (cap === null) return <span className="aiinbox__muted">no cap</span>;
  if (used === null) {
    // Not "no usage" — no run today reached a model call at all. Saying zero
    // would read as "this agent is idle" when it may have been busy on
    // query-memory hits that never cost a token.
    return <span className="aiinbox__muted">{cap.toLocaleString()} cap · no model call today</span>;
  }
  const share = Math.min(used / cap, 1);
  // "≈" is load-bearing: no provider adapter reports usage, so this is the
  // gateway's own estimate — the same one the cap is enforced against, which
  // is what makes the comparison meaningful rather than decorative.
  return (
    <span
      className="aiinbox__budget"
      title={`≈${used.toLocaleString()} of ${cap.toLocaleString()} today (estimated, not provider-reported)`}
    >
      <span className="aiinbox__budgetbar">
        <span className="aiinbox__budgetfill" style={{ width: `${share * 100}%` }} />
      </span>
      <span className="aiinbox__muted">≈{Math.round(share * 100)}% of today's cap</span>
    </span>
  );
}

function PendingRow({
  item,
  onOpen,
}: {
  item: AgentInboxPendingItem;
  onOpen: (reviewId: string) => void;
}) {
  return (
    <li className="aiinbox__pending">
      <button
        type="button"
        className="aiinbox__pendingbtn"
        onClick={() => onOpen(item.review_id)}
        aria-label={`Open review ${item.title}`}
      >
        <span className="aiinbox__pendinghead">
          <StateDot tone={TIER_TONE[item.risk_tier] ?? "mute"} title={`Risk tier ${item.risk_tier}`} />
          <Pill tone={TIER_TONE[item.risk_tier] ?? "mute"}>{item.risk_tier}</Pill>
          <Pill tone={item.proposed_by_kind === "AGENT" ? "accent" : "mute"}>
            {item.proposed_by_kind}
          </Pill>
          <strong>{item.title}</strong>
        </span>
        <span className="aiinbox__meta">
          <span>by {item.proposed_by}</span>
          <span>blast radius {item.blast_radius ?? "—"}</span>
          <span>confidence {percent(item.confidence)}</span>
          {item.negative_knowledge_hits > 0 && (
            <span className="aiinbox__negative">
              rejected before ×{item.negative_knowledge_hits}
            </span>
          )}
          <span>{relative(item.created_at)}</span>
        </span>
        {item.recommendation !== "NONE" && (
          <span className="aiinbox__recommendation">
            <Pill tone={RECOMMENDATION_TONE[item.recommendation] ?? "mute"}>
              {item.recommendation}
            </Pill>
            <span className="aiinbox__muted">recommended by reviewer agent</span>
          </span>
        )}
      </button>
    </li>
  );
}

export function AgentInboxScreen({
  persona: shellPersona,
  onNavigate,
}: {
  persona?: string;
  onNavigate?: (view: string, params?: Record<string, string>) => void;
}) {
  const organizationId = useOrgId();
  const [params, setParams] = useUrlState();
  const initialPersona = (params.get("persona") ?? shellPersona ?? "STEWARD").toUpperCase();
  const [persona, setPersona] = useState(initialPersona);
  const [inbox, setInbox] = useState<AgentInboxRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(
    (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      fetchAgentInbox(organizationId, persona, signal)
        .then((data) => {
          setInbox(data);
          setLoading(false);
        })
        .catch((err: unknown) => {
          if (signal?.aborted) return;
          setError(err instanceof ApiError ? err.message : String(err));
          setLoading(false);
        });
    },
    [organizationId, persona],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const onPersona = useCallback(
    (next: string) => {
      setPersona(next);
      setParams({ persona: next });
    },
    [setParams],
  );

  const openReview = useCallback(
    (reviewId: string) => {
      onNavigate?.("governance", { review: reviewId });
    },
    [onNavigate],
  );

  const onKill = useCallback(
    async (agent: AgentInboxAgent) => {
      if (agent.version_id === null) return;
      const reason = window.prompt(
        `Engage the kill switch for "${agent.name}"?\n\n` +
          `Scope: ${agent.kill_scope}. It takes effect on this agent's very next run.\n` +
          `Give a reason (recorded in the audit ledger):`,
      );
      if (reason === null || reason.trim() === "") return;
      try {
        await engageAgentKillSwitch(organizationId, agent.version_id, reason.trim());
        setNotice(`Kill switch engaged for ${agent.name}.`);
        load();
      } catch (err: unknown) {
        setNotice(err instanceof Error ? err.message : String(err));
      }
    },
    [organizationId, load],
  );

  const summary = inbox?.summary;
  const tiles = useMemo(
    () =>
      summary
        ? [
            { label: "Waiting on you", value: summary.pending_decisions, tone: "info" as Tone, icon: "⧉" },
            { label: "Auto-applied", value: summary.auto_applied_since, tone: "ok" as Tone, icon: "✓" },
            { label: "Sampled for audit", value: summary.sampled_for_audit, tone: "warn" as Tone, icon: "◎" },
            { label: "Agents active", value: summary.agents_active, tone: "accent" as Tone, icon: "⌬" },
          ]
        : [],
    [summary],
  );

  if (error) {
    return (
      <section className="aiinbox">
        <ErrorState
          title="The agent inbox could not be loaded"
          detail={error}
          onRetry={() => load()}
        />
      </section>
    );
  }

  return (
    <section className="aiinbox">
      <header className="aiinbox__head">
        <div>
          <h1>Agent inbox</h1>
          <p className="aiinbox__sub">What your agents did, and what is waiting on you — in one place.</p>
        </div>
        <Field label="Persona">
          <select
            value={persona}
            onChange={(event) => onPersona(event.target.value)}
            aria-label="Inbox persona"
          >
            {PERSONAS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </Field>
      </header>

      {summary?.kill_switch_engaged && (
        <p className="aiinbox__killbanner" role="status">
          <span aria-hidden="true">⛔</span>
          A kill switch is engaged. Affected agents are not running.
        </p>
      )}

      {notice && (
        <p className="aiinbox__notice" role="status">
          {notice}
        </p>
      )}

      <ul className="aiinbox__kpis">
        {tiles.map((tile) => (
          <li key={tile.label} className={`aiinbox__kpi aiinbox__kpi--${tile.tone}`}>
            <span className="aiinbox__kpiicon" aria-hidden="true">{tile.icon}</span>
            <span>
              <span className="aiinbox__kpivalue">{tile.value}</span>
              <span className="aiinbox__kpilabel">{tile.label}</span>
            </span>
          </li>
        ))}
      </ul>

      <section className="aiinbox__panel">
        <header className="aiinbox__panelhead">
          <h2>Waiting for your decision</h2>
          {inbox && <span className="aiinbox__count">{inbox.pending.length}</span>}
        </header>
        <div className="aiinbox__panelbody">
          {loading && !inbox ? (
            <p role="status" className="aiinbox__loading">Loading the agent inbox…</p>
          ) : inbox && inbox.pending.length > 0 ? (
            <ul className="aiinbox__list">
              {inbox.pending.map((item) => (
                <PendingRow key={item.review_id} item={item} onOpen={openReview} />
              ))}
            </ul>
          ) : (
            <Empty title="Nothing is waiting on you." />
          )}
        </div>
      </section>

      <section className="aiinbox__panel">
        <header className="aiinbox__panelhead">
          <h2>Auto-applied by agents</h2>
          {inbox && <span className="aiinbox__count">{inbox.auto_applied.length}</span>}
        </header>
        <div className="aiinbox__panelbody">
          {inbox && inbox.auto_applied.length > 0 ? (
            <ul className="aiinbox__list">
              {inbox.auto_applied.map((task) => (
                <li key={task.task_id} className="aiinbox__row">
                  <span className="aiinbox__rowhead">
                    <strong>{task.agent_name}</strong>
                    <span className="aiinbox__muted">{task.action} on {task.object_type}</span>
                  </span>
                  <span className="aiinbox__meta">
                    <span>{relative(task.applied_at)}</span>
                    {task.sampled_for_audit && <Pill tone="warn">sampled for audit</Pill>}
                    {task.audit_outcome && <Pill tone="mute">{task.audit_outcome}</Pill>}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty title="No agent has applied anything in this window." />
          )}
        </div>
      </section>

      <section className="aiinbox__panel">
        <header className="aiinbox__panelhead">
          <h2>Your agents</h2>
          {inbox && <span className="aiinbox__count">{inbox.agents.length}</span>}
        </header>
        <div className="aiinbox__panelbody">
          {inbox && inbox.agents.length > 0 ? (
            <ul className="aiinbox__list">
              {inbox.agents.map((agent) => (
                <li key={agent.ai_asset_id} className="aiinbox__row">
                  <span className="aiinbox__rowhead">
                    <StateDot tone={TIER_TONE[agent.autonomy_tier] ?? "mute"} title={`Autonomy ${agent.autonomy_tier}`} />
                    <strong>{agent.name}</strong>
                    <Pill tone={TIER_TONE[agent.autonomy_tier] ?? "mute"}>
                      {agent.autonomy_tier}
                    </Pill>
                    {agent.kill_engaged && <Pill tone="bad">kill engaged</Pill>}
                  </span>
                  <span className="aiinbox__meta">
                    <span>{agent.runs_recent} runs</span>
                    <span>success {percent(agent.success_rate)}</span>
                    <BudgetBar agent={agent} />
                    <span>scope {agent.kill_scope}</span>
                    {agent.supervisor_persona && <span>supervised by {agent.supervisor_persona}</span>}
                  </span>
                  {!agent.kill_engaged && (
                    <Button onClick={() => void onKill(agent)} title="Stops this agent on its very next run">
                      Engage kill switch
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <Empty title="No agent has a contract in this organization yet." hint="Publish an agent contract to give an agent an identity and an envelope." />
          )}
        </div>
      </section>

      <section className="aiinbox__panel">
        <header className="aiinbox__panelhead">
          <h2>Recent agent activity</h2>
          {inbox && <span className="aiinbox__count">{inbox.recent_tasks.length}</span>}
        </header>
        <div className="aiinbox__panelbody">
          {inbox && inbox.recent_tasks.length > 0 ? (
            <ul className="aiinbox__list">
              {inbox.recent_tasks.map((task) => (
                <li key={task.task_id} className="aiinbox__row">
                  <span className="aiinbox__rowhead">
                    <strong>{task.agent_name}</strong>
                    <span className="aiinbox__muted">{task.intent}</span>
                  </span>
                  <span className="aiinbox__meta">
                    <Pill tone={task.status === "REJECTED" ? "bad" : "mute"}>{task.status}</Pill>
                    <span>{relative(task.started_at)}</span>
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty title="No agent activity recorded." />
          )}
        </div>
      </section>
    </section>
  );
}
