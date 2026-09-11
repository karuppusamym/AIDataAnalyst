import { useCallback, useEffect, useState } from "react";
import type {
  StewardAgentOutcomeRead,
  StewardAgentRunItemRead,
  StewardAgentRunRead,
  StewardAgentStateRead,
} from "../lib/types";
import { ApiError, fetchStewardAgentState, runStewardAgent } from "../lib/api";
import { useOrgId } from "../lib/org";
import { buildRelativeLink } from "../lib/routes";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./StewardAgentScreen.css";

/* ---------------------------------------------------------------------------
   Steward agent console — ADR-0029.

   The steward agent works the documentation backlog under its own contract:
   it drafts table descriptions and glossary links from catalog evidence and
   puts each one in the review queue as its own request. It decides nothing,
   and it calls no model.

   Three panels, each loaded and erred independently:
     1. what the agent is in this organization — registered or not (and why
        not), the tier it runs at and what that tier lets it do, and whether a
        kill switch is stopping it;
     2. a bounded run, with a preview that opens nothing, listing every table
        or link it looked at, what it did, and why;
     3. how its proposals have fared with reviewers — shown as "—" until a
        reviewer has decided something, because an empty sample is not a 0%
        agent.
--------------------------------------------------------------------------- */

type Capability = "TABLE_DESCRIPTION" | "GLOSSARY_LINK";

const CAPABILITY_LABELS: Record<Capability, string> = {
  TABLE_DESCRIPTION: "Table descriptions",
  GLOSSARY_LINK: "Glossary links",
};
const ALL_CAPABILITIES: Capability[] = ["TABLE_DESCRIPTION", "GLOSSARY_LINK"];
const LIMITS = [5, 10, 25];

/* Operator-facing wording for the stable reason codes the run endpoint answers
   409 with. The code is always shown beside it, so a code this table does not
   know is still visible rather than hidden behind a generic sentence. */
const REFUSALS: Record<string, string> = {
  agent_contract_missing: "The steward agent is not registered in this organization.",
  steward_agent_version_not_approved: "The steward agent's AI asset version is not approved.",
  agent_contract_unresolved: "More than one approved contract names the steward agent.",
  steward_agent_principal_is_reviewer:
    "The steward agent is configured with the reviewer agent's identity, so it refuses to run.",
  agent_kill_switch_engaged:
    "A kill switch is engaged — the agent's own, its tier's, or the organization's.",
  steward_agent_autonomy_withdrawn:
    "The agent's tier was lowered to T0 during the run, so nothing it did was kept.",
  steward_agent_object_type_above_ceiling:
    "The agent tried to propose something above its tier ceiling and was stopped.",
};

const STOPS: Record<string, string> = {
  steward_agent_review_backlog_full:
    "Stopped early: the agent's own proposals waiting for review reached the backlog bound.",
  agent_wall_clock_cap_exceeded: "Stopped early: the run reached its contract's wall-clock cap.",
};

const SKIPS: Record<string, string> = {
  open_draft_exists: "a draft is already open",
  identical_text_rejected: "identical text was rejected before",
  below_evidence_bar: "too little evidence to submit",
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

function summarize(run: StewardAgentRunRead): string {
  const parts =
    run.mode === "OBSERVE"
      ? [`${run.would_propose} would be proposed — nothing was opened`]
      : [`${run.proposed} proposed for review`];
  parts.push(`${run.skipped} skipped`);
  if (run.failed) parts.push(`${run.failed} failed`);
  return parts.join(", ");
}

function RunItem({ item }: { item: StewardAgentRunItemRead }) {
  const capability = CAPABILITY_LABELS[item.capability as Capability] ?? item.capability;
  return (
    <li className="stewagent__row">
      <span className="stewagent__rowhead">
        <Pill tone={actionTone(item.action)}>{item.action.replace("_", " ").toLowerCase()}</Pill>
        <strong>{item.table_name}</strong>
        {item.term_name ? <span>→ {item.term_name}</span> : null}
      </span>
      <span className="stewagent__meta">
        <span>{capability}</span>
        {item.worklist_rank != null ? <span>worklist #{item.worklist_rank}</span> : null}
        {item.confidence != null ? <span>evidence {item.confidence.toFixed(2)}</span> : null}
        {item.reason ? <span>{SKIPS[item.reason] ?? item.reason}</span> : null}
        {item.review_id ? (
          <a href={buildRelativeLink({ screen: "governance", params: { review: item.review_id } })}>
            Open in review queue
          </a>
        ) : null}
      </span>
    </li>
  );
}

function OutcomeRow({ row }: { row: StewardAgentOutcomeRead }) {
  return (
    <li className="stewagent__row">
      <span className="stewagent__rowhead">
        <strong>{row.object_type}</strong>
      </span>
      <span className="stewagent__meta">
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

export function StewardAgentScreen() {
  const organizationId = useOrgId();

  const [state, setState] = useState<StewardAgentStateRead | null>(null);
  const [stateLoading, setStateLoading] = useState(true);
  const [stateError, setStateError] = useState<string | null>(null);

  const [capabilities, setCapabilities] = useState<Capability[]>(ALL_CAPABILITIES);
  const [limit, setLimit] = useState(10);
  const [busy, setBusy] = useState<"preview" | "run" | null>(null);
  const [run, setRun] = useState<StewardAgentRunRead | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  const loadState = useCallback(
    (signal?: AbortSignal) => {
      setStateLoading(true);
      setStateError(null);
      fetchStewardAgentState(organizationId, signal)
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
    [organizationId],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadState(controller.signal);
    return () => controller.abort();
  }, [loadState]);

  const toggleCapability = useCallback((capability: Capability) => {
    setCapabilities((current) =>
      current.includes(capability)
        ? current.filter((value) => value !== capability)
        : ALL_CAPABILITIES.filter((value) => value === capability || current.includes(value)),
    );
  }, []);

  const start = useCallback(
    async (dryRun: boolean) => {
      setBusy(dryRun ? "preview" : "run");
      setRunError(null);
      try {
        const result = await runStewardAgent(organizationId, {
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
    [organizationId, capabilities, limit, loadState],
  );

  const blocked = !state?.registered || Boolean(state?.blocking_reason);
  const canStart = !blocked && busy === null && capabilities.length > 0;

  return (
    <section className="stewagent">
      <header className="stewagent__head">
        <h1>Steward agent</h1>
        <p className="stewagent__sub">
          Works the documentation backlog in the order the worklist ranks it: drafts table
          descriptions and glossary links from catalog evidence and puts each one in the review
          queue as its own request. It decides nothing, and it calls no model.
        </p>
      </header>

      <section className="stewagent__panel">
        <header className="stewagent__panelhead">
          <h2>Agent state</h2>
        </header>
        <div className="stewagent__panelbody">
          {stateError ? (
            <ErrorState
              title="The steward agent's state could not be loaded"
              detail={stateError}
              onRetry={() => loadState()}
            />
          ) : stateLoading && !state ? (
            <p role="status" className="stewagent__loading">
              Loading the steward agent's state…
            </p>
          ) : state && !state.registered ? (
            <>
              <div className="stewagent__statepills">
                <Pill tone="mute">not registered</Pill>
              </div>
              <p className="stewagent__muted">{describeRefusal(state.refusal_reason ?? "")}</p>
              <p className="stewagent__muted">
                To register it, add an AGENT-kind asset in the AI registry, approve a version, and
                give that version a contract whose agent principal is{" "}
                <code>{state.agent_principal_id}</code> and whose supervisor persona is STEWARD. A T0
                contract lets it preview; T1 lets it propose.
              </p>
            </>
          ) : state ? (
            <>
              <div className="stewagent__statepills">
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
              <dl className="stewagent__facts">
                <div>
                  <dt>Acts as</dt>
                  <dd className="stewagent__mono">{state.agent_principal_id}</dd>
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
              <ul className="stewagent__list" aria-label="What it proposes">
                {state.capabilities.map((capability) => (
                  <li key={capability.capability} className="stewagent__row">
                    <span className="stewagent__rowhead">
                      <Pill tone={tierTone(capability.risk_tier)}>{capability.risk_tier}</Pill>
                      <strong>
                        {CAPABILITY_LABELS[capability.capability as Capability] ??
                          capability.capability}
                      </strong>
                      <span className="stewagent__muted">as {capability.object_type}</span>
                    </span>
                    <span className="stewagent__meta">{capability.producer}</span>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      </section>

      <section className="stewagent__panel">
        <header className="stewagent__panelhead">
          <h2>Run</h2>
        </header>
        <div className="stewagent__panelbody">
          <div className="stewagent__controls">
            <fieldset className="stewagent__checks">
              <legend>Propose</legend>
              {ALL_CAPABILITIES.map((capability) => (
                <label key={capability}>
                  <input
                    type="checkbox"
                    checked={capabilities.includes(capability)}
                    onChange={() => toggleCapability(capability)}
                  />
                  {CAPABILITY_LABELS[capability]}
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
            <div className="stewagent__actions">
              <Button onClick={() => void start(true)} disabled={!canStart}>
                Preview
              </Button>
              <Button variant="primary" onClick={() => void start(false)} disabled={!canStart}>
                Run steward agent
              </Button>
            </div>
          </div>

          {runError ? (
            <p className="stewagent__notice" role="alert">
              {runError}
            </p>
          ) : null}
          {run ? (
            <>
              <p className="stewagent__notice" role="status">
                {summarize(run)}
              </p>
              {run.stopped_reason ? (
                <p className="stewagent__muted">
                  {STOPS[run.stopped_reason] ?? `Stopped early: ${run.stopped_reason}`}
                </p>
              ) : null}
              {run.items.length > 0 ? (
                <ul className="stewagent__list" aria-label="What the run looked at">
                  {run.items.map((item) => (
                    <RunItem key={`${item.capability}:${item.table_id}:${item.term_id ?? ""}`} item={item} />
                  ))}
                </ul>
              ) : (
                <Empty
                  title="Nothing to do."
                  hint="Every undocumented table on the worklist is already in review, or there is no exact label match waiting for a link."
                />
              )}
            </>
          ) : null}
        </div>
      </section>

      <section className="stewagent__panel">
        <header className="stewagent__panelhead">
          <h2>How its proposals fared</h2>
        </header>
        <div className="stewagent__panelbody">
          {state && state.outcomes.length > 0 ? (
            <>
              <ul className="stewagent__list">
                {state.outcomes.map((row) => (
                  <OutcomeRow key={row.object_type} row={row} />
                ))}
              </ul>
              <p className="stewagent__muted">
                Acceptance is approved ÷ decided. It reads "—" until a reviewer has decided at
                least one proposal of that kind.
              </p>
            </>
          ) : state ? (
            <Empty title="The steward agent has not proposed anything yet." />
          ) : null}
        </div>
      </section>
    </section>
  );
}
