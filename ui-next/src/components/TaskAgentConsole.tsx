import { useCallback, useEffect, useState } from "react";
import type {
  TaskAgentOutcomeRead,
  TaskAgentRunItemRead,
  TaskAgentRunRead,
  TaskAgentStateRead,
} from "../lib/types";
import { ApiError, fetchTaskAgentState, runTaskAgent } from "../lib/api";
import type { TaskAgentKind } from "../lib/api";
import { useOrgId } from "../lib/org";
import { buildRelativeLink } from "../lib/routes";
import { Button, Empty, ErrorState, Field, Pill } from "./primitives";
import type { Tone } from "./primitives";
import "./TaskAgentConsole.css";

/* ---------------------------------------------------------------------------
   Task agent console — ADR-0029.

   Every task agent (steward, lineage, quality) works a backlog under its own
   contract and puts what it drafts in front of a human as its own request. It
   decides nothing and calls no model. They share one runtime server-side, so
   they share one control surface here; a screen supplies only the agent's
   name, its capabilities' labels and its skip reasons.

   Three panels, each loaded and erred independently:
     1. what the agent is in this organization — registered or not (and why
        not), the tier it runs at and what that tier lets it do, and whether a
        kill switch is stopping it;
     2. a bounded run, with a preview that opens nothing, listing every item it
        looked at, what it did, and why;
     3. how its proposals have fared with reviewers — "—" until a reviewer has
        decided something, because an empty sample is not a 0% agent.
--------------------------------------------------------------------------- */

const LIMITS = [5, 10, 25];

/* Operator-facing wording for the stable reason codes a run is refused with.
   The code is always shown beside it, so a code this table does not know is
   still visible rather than hidden behind a generic sentence. */
const REFUSALS: Record<string, string> = {
  agent_contract_missing: "This agent is not registered in this organization.",
  agent_version_not_approved: "This agent's AI asset version is not approved.",
  agent_contract_unresolved: "More than one approved contract names this agent.",
  agent_principal_reserved:
    "This agent is configured with another agent's identity, so it refuses to run.",
  agent_kill_switch_engaged:
    "A kill switch is engaged — the agent's own, its tier's, or the organization's.",
  agent_autonomy_withdrawn:
    "The agent's tier was lowered to T0 during the run, so nothing it did was kept.",
  agent_object_type_above_ceiling:
    "The agent tried to propose something above its tier ceiling and was stopped.",
};

const STOPS: Record<string, string> = {
  agent_review_backlog_full:
    "Stopped early: the agent's own proposals waiting for review reached the backlog bound.",
  agent_wall_clock_cap_exceeded: "Stopped early: the run reached its contract's wall-clock cap.",
};

function describeRefusal(code: string): string {
  const sentence = REFUSALS[code];
  return sentence ? `${sentence} (${code})` : code;
}

function percent(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

function tierTone(tier: string): Tone {
  const t = tier.toUpperCase();
  if (t === "T0") return "mute";
  if (t === "T1") return "info";
  if (t === "T2") return "warn";
  return "bad";
}

function actionTone(action: string): Tone {
  if (action === "PROPOSED") return "ok";
  if (action === "WOULD_PROPOSE") return "info";
  if (action === "FAILED") return "bad";
  return "mute";
}

function summarize(run: TaskAgentRunRead): string {
  const parts =
    run.mode === "OBSERVE"
      ? [`${run.would_propose} would be proposed — nothing was opened`]
      : [`${run.proposed} proposed for review`];
  parts.push(`${run.skipped} skipped`);
  if (run.failed) parts.push(`${run.failed} failed`);
  return parts.join(", ");
}

/** Where a proposal is decided. Most go to the shared review queue; an agent
 *  whose proposals have their own queue passes its own link. */
export type ReviewLink = (item: TaskAgentRunItemRead) => string | null;

const governanceReviewLink: ReviewLink = (item) =>
  item.review_id ? buildRelativeLink({ screen: "governance", params: { review: item.review_id } }) : null;

export interface TaskAgentConsoleProps {
  kind: TaskAgentKind;
  title: string;
  description: string;
  /** Capability key -> what a person calls it. */
  capabilityLabels: Record<string, string>;
  /** Skip reason code -> the phrase shown beside a skipped item. */
  skipLabels: Record<string, string>;
  /** The persona a registration's contract names as supervisor. */
  supervisorPersona: string;
  /** Shown when a run found nothing to do. */
  emptyRunHint: string;
  reviewLink?: ReviewLink;
}

function RunItem({
  item,
  capabilityLabels,
  skipLabels,
  reviewLink,
}: {
  item: TaskAgentRunItemRead;
  capabilityLabels: Record<string, string>;
  skipLabels: Record<string, string>;
  reviewLink: ReviewLink;
}) {
  const href = reviewLink(item);
  return (
    <li className="taskagent__row">
      <span className="taskagent__rowhead">
        <Pill tone={actionTone(item.action)}>{item.action.replace("_", " ").toLowerCase()}</Pill>
        <strong>{item.subject_name}</strong>
        {item.related_name ? <span>→ {item.related_name}</span> : null}
      </span>
      <span className="taskagent__meta">
        <span>{capabilityLabels[item.capability] ?? item.capability}</span>
        {item.rank != null ? <span>worklist #{item.rank}</span> : null}
        {item.confidence != null ? <span>evidence {item.confidence.toFixed(2)}</span> : null}
        {item.reason ? <span>{skipLabels[item.reason] ?? item.reason}</span> : null}
        {href ? <a href={href}>Open in review queue</a> : null}
      </span>
    </li>
  );
}

function OutcomeRow({ row }: { row: TaskAgentOutcomeRead }) {
  return (
    <li className="taskagent__row">
      <span className="taskagent__rowhead">
        <strong>{row.object_type}</strong>
      </span>
      <span className="taskagent__meta">
        <span>pending {row.pending}</span>
        <span>approved {row.approved}</span>
        <span>rejected {row.rejected}</span>
        {row.other ? <span>other {row.other}</span> : null}
        <span>
          acceptance <b>{percent(row.acceptance_rate)}</b>
        </span>
      </span>
    </li>
  );
}

export function TaskAgentConsole({
  kind,
  title,
  description,
  capabilityLabels,
  skipLabels,
  supervisorPersona,
  emptyRunHint,
  reviewLink = governanceReviewLink,
}: TaskAgentConsoleProps) {
  const organizationId = useOrgId();
  const allCapabilities = Object.keys(capabilityLabels);

  const [state, setState] = useState<TaskAgentStateRead | null>(null);
  const [stateLoading, setStateLoading] = useState(true);
  const [stateError, setStateError] = useState<string | null>(null);

  const [capabilities, setCapabilities] = useState<string[]>(allCapabilities);
  const [limit, setLimit] = useState(10);
  const [busy, setBusy] = useState<"preview" | "run" | null>(null);
  const [run, setRun] = useState<TaskAgentRunRead | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  const loadState = useCallback(
    (signal?: AbortSignal) => {
      setStateLoading(true);
      setStateError(null);
      fetchTaskAgentState(organizationId, kind, signal)
        .then((data) => {
          setState(data);
          setStateLoading(false);
        })
        .catch((err: unknown) => {
          if (signal?.aborted) return;
          setStateError(err instanceof ApiError ? err.message : String(err));
          setStateLoading(false);
        });
    },
    [organizationId, kind],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadState(controller.signal);
    return () => controller.abort();
  }, [loadState]);

  const toggleCapability = useCallback(
    (capability: string) => {
      setCapabilities((current) =>
        current.includes(capability)
          ? current.filter((value) => value !== capability)
          : allCapabilities.filter((value) => value === capability || current.includes(value)),
      );
    },
    // `allCapabilities` is derived from a prop object a screen defines once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [allCapabilities.join("|")],
  );

  const start = useCallback(
    async (dryRun: boolean) => {
      setBusy(dryRun ? "preview" : "run");
      setRunError(null);
      try {
        const result = await runTaskAgent(organizationId, kind, {
          capabilities,
          limit,
          datasource_id: null,
          dry_run: dryRun,
        });
        setRun(result);
        // Pending count and outcomes moved; show the agent as it is now.
        loadState();
      } catch (err: unknown) {
        setRun(null);
        setRunError(
          err instanceof ApiError ? describeRefusal(err.detail || err.message) : String(err),
        );
      } finally {
        setBusy(null);
      }
    },
    [organizationId, kind, capabilities, limit, loadState],
  );

  const blocked = !state?.registered || Boolean(state?.blocking_reason);
  const canStart = !blocked && busy === null && capabilities.length > 0;

  return (
    <section className="taskagent">
      <header className="taskagent__head">
        <h1>{title}</h1>
        <p className="taskagent__sub">{description}</p>
      </header>

      <section className="taskagent__panel">
        <header className="taskagent__panelhead">
          <h2>Agent state</h2>
        </header>
        <div className="taskagent__panelbody">
          {stateError ? (
            <ErrorState
              title={`The ${title.toLowerCase()}'s state could not be loaded`}
              detail={stateError}
              onRetry={() => loadState()}
            />
          ) : stateLoading && !state ? (
            <p role="status" className="taskagent__loading">
              Loading the {title.toLowerCase()}'s state…
            </p>
          ) : state && !state.registered ? (
            <>
              <div className="taskagent__statepills">
                <Pill tone="mute">not registered</Pill>
              </div>
              <p className="taskagent__muted">{describeRefusal(state.refusal_reason ?? "")}</p>
              <p className="taskagent__muted">
                To register it, add an AGENT-kind asset in the AI registry, approve a version, and
                give that version a contract whose agent principal is{" "}
                <code>{state.agent_principal_id}</code> and whose supervisor persona is{" "}
                {supervisorPersona}. A T0 contract lets it preview; T1 lets it propose.
              </p>
            </>
          ) : state ? (
            <>
              <div className="taskagent__statepills">
                <Pill tone="ok">registered</Pill>
                <Pill tone={state.mode === "PROPOSE" ? "info" : "mute"}>
                  {state.mode === "PROPOSE" ? "proposes for review" : "observes only"}
                </Pill>
                {state.autonomy_tier ? (
                  <Pill tone={tierTone(state.autonomy_tier)}>tier {state.autonomy_tier}</Pill>
                ) : null}
                {state.blocking_reason ? <Pill tone="bad">stopped by a kill switch</Pill> : null}
                <Pill tone="mute">deterministic — no model</Pill>
              </div>
              <dl className="taskagent__facts">
                <div>
                  <dt>Acts as</dt>
                  <dd className="taskagent__mono">{state.agent_principal_id}</dd>
                </div>
                <div>
                  <dt>Waiting for review</dt>
                  <dd>
                    {state.max_pending_proposals
                      ? `${state.pending_proposals} of ${state.max_pending_proposals}`
                      : state.pending_proposals}
                  </dd>
                </div>
                <div>
                  <dt>Most per run</dt>
                  <dd>{state.max_proposals_per_run} of each kind</dd>
                </div>
                <div>
                  <dt>Wall-clock cap</dt>
                  <dd>{state.wall_clock_seconds_cap ? `${state.wall_clock_seconds_cap}s` : "none"}</dd>
                </div>
              </dl>
              <ul className="taskagent__list" aria-label="What it proposes">
                {state.capabilities.map((capability) => (
                  <li key={capability.capability} className="taskagent__row">
                    <span className="taskagent__rowhead">
                      <Pill tone={tierTone(capability.risk_tier)}>{capability.risk_tier}</Pill>
                      <strong>{capabilityLabels[capability.capability] ?? capability.capability}</strong>
                      <span className="taskagent__muted">as {capability.object_type}</span>
                    </span>
                    <span className="taskagent__meta">{capability.producer}</span>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      </section>

      <section className="taskagent__panel">
        <header className="taskagent__panelhead">
          <h2>Run</h2>
        </header>
        <div className="taskagent__panelbody">
          <div className="taskagent__controls">
            <fieldset className="taskagent__checks">
              <legend>Propose</legend>
              {allCapabilities.map((capability) => (
                <label key={capability}>
                  <input
                    type="checkbox"
                    checked={capabilities.includes(capability)}
                    onChange={() => toggleCapability(capability)}
                  />
                  {capabilityLabels[capability]}
                </label>
              ))}
            </fieldset>
            <Field label="Up to">
              <select
                value={String(limit)}
                onChange={(event) => setLimit(Number(event.target.value))}
                aria-label="Proposals of each kind"
              >
                {LIMITS.map((value) => (
                  <option key={value} value={value}>
                    {value} of each kind
                  </option>
                ))}
              </select>
            </Field>
            <div className="taskagent__actions">
              <Button onClick={() => void start(true)} disabled={!canStart}>
                Preview
              </Button>
              <Button variant="primary" onClick={() => void start(false)} disabled={!canStart}>
                Run {title.toLowerCase()}
              </Button>
            </div>
          </div>

          {runError ? (
            <p className="taskagent__notice" role="alert">
              {runError}
            </p>
          ) : null}
          {run ? (
            <>
              <p className="taskagent__notice" role="status">
                {summarize(run)}
              </p>
              {run.stopped_reason ? (
                <p className="taskagent__muted">
                  {STOPS[run.stopped_reason] ?? `Stopped early: ${run.stopped_reason}`}
                </p>
              ) : null}
              {run.items.length > 0 ? (
                <ul className="taskagent__list" aria-label="What the run looked at">
                  {run.items.map((item) => (
                    <RunItem
                      key={`${item.capability}:${item.subject_id}:${item.related_id ?? ""}`}
                      item={item}
                      capabilityLabels={capabilityLabels}
                      skipLabels={skipLabels}
                      reviewLink={reviewLink}
                    />
                  ))}
                </ul>
              ) : (
                <Empty title="Nothing to do." hint={emptyRunHint} />
              )}
            </>
          ) : null}
        </div>
      </section>

      <section className="taskagent__panel">
        <header className="taskagent__panelhead">
          <h2>How its proposals fared</h2>
        </header>
        <div className="taskagent__panelbody">
          {state && state.outcomes.length > 0 ? (
            <>
              <ul className="taskagent__list">
                {state.outcomes.map((row) => (
                  <OutcomeRow key={row.object_type} row={row} />
                ))}
              </ul>
              <p className="taskagent__muted">
                Acceptance is approved ÷ decided. It reads "—" until a reviewer has decided at
                least one proposal of that kind.
              </p>
            </>
          ) : state ? (
            <Empty title={`The ${title.toLowerCase()} has not proposed anything yet.`} />
          ) : null}
        </div>
      </section>
    </section>
  );
}
