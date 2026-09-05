import { useCallback, useEffect, useState } from "react";
import type {
  DisagreementRateRead,
  DisagreementReportRead,
  ReviewAuditSampleRead,
  ReviewerAgentStateRead,
} from "../lib/types";
import {
  ApiError,
  fetchDisagreementRates,
  fetchReviewerAgentSamples,
  fetchReviewerAgentState,
  resolveAuditSample,
  resumeReviewerAgent,
  runReviewerAgent,
  runReviewerAgentPreReview,
  suspendReviewerAgent,
} from "../lib/api";
import type { PageOf } from "../lib/ui-types";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./ReviewerAgentScreen.css";

/* ---------------------------------------------------------------------------
   Reviewer agent console — ADR-0027.

   The backend has a fully-built autonomous reviewer agent: it pre-reviews
   every pending `GovernanceReview` item with a deterministic recommendation,
   auto-decides low-risk (T0/T1) ones on its own, and a human can suspend or
   resume it at any time and audit a sampled subset of its decisions. None of
   that had a screen before this one — a governance blind spot on an agent
   that takes real autonomous actions.

   Four panels, each loaded and erred independently so one failing fetch does
   not blank the other three:
     1. the agent's own state (enabled/suspended/tier/sampling rate) plus the
        one human action ADR-0027 condition (c) calls for — suspend/resume,
        effective immediately;
     2. manual triggers for pre-review and the actual auto-decide run, each
        showing the real counts the endpoint returned;
     3. the disagreement-rate report — ADR-0027's 5% revisit trigger, as a
        number per object type, reporting only, never suspending by itself;
     4. the sampled-decision audit queue, where a human resolves one sampled
        auto-decision at a time with a mandatory rationale.
--------------------------------------------------------------------------- */

const WINDOWS = [7, 30, 90];
const OUTCOMES = ["PENDING", "AGREED", "DISAGREED", "ALL"] as const;
type OutcomeFilter = (typeof OUTCOMES)[number];
const SAMPLES_LIMIT = 20;

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

function tierTone(tier: string): Tone {
  const t = tier.toUpperCase();
  if (t === "T0") return "mute";
  if (t === "T1") return "info";
  if (t === "T2") return "warn";
  return "bad";
}

function outcomeTone(outcome: string): Tone {
  if (outcome === "AGREED") return "ok";
  if (outcome === "DISAGREED") return "bad";
  return "mute";
}

function decisionTone(decision: string): Tone {
  if (decision === "APPROVED") return "ok";
  if (decision === "REJECTED") return "bad";
  return "mute";
}

function DisagreementRow({ row }: { row: DisagreementRateRead }) {
  return (
    <li className="revagent__row">
      <span className="revagent__rowhead">
        <strong>{row.object_type}</strong>
        {row.breaches_revisit_trigger && <Pill tone="bad">breaches revisit trigger</Pill>}
        {!row.sufficient_sample && <Pill tone="mute">insufficient sample</Pill>}
      </span>
      <span className="revagent__meta">
        <span>sampled {row.sampled}</span>
        <span>resolved {row.resolved}</span>
        <span>agreed {row.agreed}</span>
        <span>disagreed {row.disagreed}</span>
        <span>pending {row.pending}</span>
        <span>
          disagreement rate <b>{percent(row.disagreement_rate)}</b>
        </span>
      </span>
    </li>
  );
}

function SampleRow({
  sample,
  onResolve,
}: {
  sample: ReviewAuditSampleRead;
  onResolve: (sample: ReviewAuditSampleRead, outcome: "AGREED" | "DISAGREED") => void;
}) {
  return (
    <li className="revagent__row">
      <span className="revagent__rowhead">
        <Pill tone={tierTone(sample.risk_tier)}>{sample.risk_tier}</Pill>
        <strong>{sample.object_type}</strong>
        <Pill tone={decisionTone(sample.decision)}>{sample.decision}</Pill>
        <Pill tone={outcomeTone(sample.human_outcome)}>{sample.human_outcome}</Pill>
      </span>
      <span className="revagent__meta">
        <span>by {sample.agent_principal_id}</span>
        <span>{relative(sample.sampled_at)}</span>
        {sample.human_rationale && <span className="revagent__muted">{sample.human_rationale}</span>}
      </span>
      {sample.human_outcome === "PENDING" && (
        <span className="revagent__actions">
          <Button onClick={() => onResolve(sample, "AGREED")}>Agree</Button>
          <Button onClick={() => onResolve(sample, "DISAGREED")}>Disagree</Button>
        </span>
      )}
    </li>
  );
}

export function ReviewerAgentScreen() {
  const organizationId = useOrgId();
  const [params, setParams] = useUrlState();
  const windowDays = Number(params.get("window") ?? 30);
  const outcome = (params.get("outcome") ?? "PENDING") as OutcomeFilter;
  const offset = Number(params.get("offset") ?? 0);

  const [state, setState] = useState<ReviewerAgentStateRead | null>(null);
  const [stateLoading, setStateLoading] = useState(true);
  const [stateError, setStateError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);

  const [triggerBusy, setTriggerBusy] = useState<"pre-review" | "run" | null>(null);
  const [triggerNotice, setTriggerNotice] = useState<string | null>(null);

  const [disagreement, setDisagreement] = useState<DisagreementReportRead | null>(null);
  const [disagreementLoading, setDisagreementLoading] = useState(true);
  const [disagreementError, setDisagreementError] = useState<string | null>(null);

  const [samples, setSamples] = useState<PageOf<ReviewAuditSampleRead> | null>(null);
  const [samplesLoading, setSamplesLoading] = useState(true);
  const [samplesError, setSamplesError] = useState<string | null>(null);

  const loadState = useCallback(
    (signal?: AbortSignal) => {
      setStateLoading(true);
      setStateError(null);
      fetchReviewerAgentState(organizationId, signal)
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

  const loadDisagreement = useCallback(
    (signal?: AbortSignal) => {
      setDisagreementLoading(true);
      setDisagreementError(null);
      fetchDisagreementRates(organizationId, windowDays, signal)
        .then((data) => {
          setDisagreement(data);
          setDisagreementLoading(false);
        })
        .catch((err: unknown) => {
          if (signal?.aborted) return;
          setDisagreementError(err instanceof ApiError ? err.message : String(err));
          setDisagreementLoading(false);
        });
    },
    [organizationId, windowDays],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadDisagreement(controller.signal);
    return () => controller.abort();
  }, [loadDisagreement]);

  const loadSamples = useCallback(
    (signal?: AbortSignal) => {
      setSamplesLoading(true);
      setSamplesError(null);
      fetchReviewerAgentSamples(organizationId, { outcome, limit: SAMPLES_LIMIT, offset }, signal)
        .then((data) => {
          setSamples(data);
          setSamplesLoading(false);
        })
        .catch((err: unknown) => {
          if (signal?.aborted) return;
          setSamplesError(err instanceof ApiError ? err.message : String(err));
          setSamplesLoading(false);
        });
    },
    [organizationId, outcome, offset],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadSamples(controller.signal);
    return () => controller.abort();
  }, [loadSamples]);

  const onSuspendResume = useCallback(async () => {
    if (!state) return;
    const wantSuspend = !state.suspended;
    const reason = window.prompt(
      wantSuspend
        ? "Suspend the reviewer agent?\n\nADR-0027 condition (c): one human action, effective immediately. Give a reason (recorded in the audit ledger):"
        : "Resume the reviewer agent?\n\nGive a reason (recorded in the audit ledger):",
    );
    if (reason === null || reason.trim() === "") return;
    try {
      const updated = wantSuspend
        ? await suspendReviewerAgent(organizationId, reason.trim())
        : await resumeReviewerAgent(organizationId, reason.trim());
      setState(updated);
      setActionNotice(wantSuspend ? "Reviewer agent suspended." : "Reviewer agent resumed.");
    } catch (err: unknown) {
      setActionNotice(err instanceof Error ? err.message : String(err));
    }
  }, [state, organizationId]);

  const onPreReview = useCallback(async () => {
    setTriggerBusy("pre-review");
    setTriggerNotice(null);
    try {
      const result = await runReviewerAgentPreReview(organizationId);
      setTriggerNotice(`${result.pre_reviewed} pre-reviewed`);
    } catch (err: unknown) {
      setTriggerNotice(err instanceof ApiError ? err.message : String(err));
    } finally {
      setTriggerBusy(null);
    }
  }, [organizationId]);

  const onRun = useCallback(async () => {
    setTriggerBusy("run");
    setTriggerNotice(null);
    try {
      const result = await runReviewerAgent(organizationId);
      setTriggerNotice(
        `${result.decided} decided — ${result.approved} approved, ${result.rejected} rejected, ${result.sampled_for_audit} sampled for audit`,
      );
    } catch (err: unknown) {
      setTriggerNotice(err instanceof ApiError ? err.message : String(err));
    } finally {
      setTriggerBusy(null);
    }
  }, [organizationId]);

  const onResolve = useCallback(
    async (sample: ReviewAuditSampleRead, humanOutcome: "AGREED" | "DISAGREED") => {
      const rationale = window.prompt(
        `Give a rationale for marking this sampled decision as ${humanOutcome.toLowerCase()} (recorded in the audit ledger):`,
      );
      if (rationale === null || rationale.trim() === "") return;
      try {
        await resolveAuditSample(organizationId, sample.sample_id, {
          human_outcome: humanOutcome,
          rationale: rationale.trim(),
        });
        setActionNotice(`Marked the ${sample.object_type} sample as ${humanOutcome.toLowerCase()}.`);
        loadSamples();
      } catch (err: unknown) {
        setActionNotice(err instanceof Error ? err.message : String(err));
      }
    },
    [organizationId, loadSamples],
  );

  return (
    <section className="revagent">
      <header className="revagent__head">
        <div>
          <h1>Reviewer agent</h1>
          <p className="revagent__sub">
            ADR-0027's autonomous pre-reviewer — pre-reviews every pending item, auto-decides
            low-risk (T0/T1) ones, and can be suspended by a human at any time.
          </p>
        </div>
      </header>

      {actionNotice && (
        <p className="revagent__notice" role="status">
          {actionNotice}
        </p>
      )}

      <section className="revagent__panel">
        <header className="revagent__panelhead">
          <h2>Agent state</h2>
        </header>
        <div className="revagent__panelbody">
          {stateError ? (
            <ErrorState
              title="The reviewer agent's state could not be loaded"
              detail={stateError}
              onRetry={() => loadState()}
            />
          ) : stateLoading && !state ? (
            <p role="status" className="revagent__loading">
              Loading the reviewer agent's state…
            </p>
          ) : state ? (
            <div className="revagent__state">
              <div className="revagent__statepills">
                <Pill tone={state.enabled ? "ok" : "mute"}>{state.enabled ? "enabled" : "disabled"}</Pill>
                <Pill tone={state.suspended ? "bad" : "ok"}>
                  {state.suspended ? "suspended" : "active"}
                </Pill>
                <Pill tone={tierTone(state.max_tier)}>max tier {state.max_tier}</Pill>
              </div>
              <dl className="revagent__facts">
                <div>
                  <dt>Sampling rate</dt>
                  <dd>{percent(state.sampling_rate)}</dd>
                </div>
                <div>
                  <dt>Acts as</dt>
                  <dd className="revagent__mono">{state.agent_principal_id}</dd>
                </div>
              </dl>
              <div className="revagent__actions">
                {state.suspended ? (
                  <Button variant="primary" onClick={() => void onSuspendResume()}>
                    Resume
                  </Button>
                ) : (
                  <Button variant="primary" onClick={() => void onSuspendResume()}>
                    Suspend
                  </Button>
                )}
              </div>
            </div>
          ) : null}
        </div>
      </section>

      <section className="revagent__panel">
        <header className="revagent__panelhead">
          <h2>Manual triggers</h2>
        </header>
        <div className="revagent__panelbody">
          <div className="revagent__actions">
            <Button onClick={() => void onPreReview()} disabled={triggerBusy !== null}>
              Run pre-review
            </Button>
            <Button onClick={() => void onRun()} disabled={triggerBusy !== null}>
              Run reviewer agent
            </Button>
          </div>
          {triggerNotice && (
            <p className="revagent__notice" role="status">
              {triggerNotice}
            </p>
          )}
        </div>
      </section>

      <section className="revagent__panel">
        <header className="revagent__panelhead">
          <h2>Disagreement rate</h2>
          <Field label="Window">
            <select
              value={String(windowDays)}
              onChange={(event) => setParams({ window: event.target.value })}
              aria-label="Disagreement window"
            >
              {WINDOWS.map((days) => (
                <option key={days} value={days}>
                  {days} days
                </option>
              ))}
            </select>
          </Field>
        </header>
        <div className="revagent__panelbody">
          {disagreementError ? (
            <ErrorState
              title="Disagreement rates could not be loaded"
              detail={disagreementError}
              onRetry={() => loadDisagreement()}
            />
          ) : disagreementLoading && !disagreement ? (
            <p role="status" className="revagent__loading">
              Loading disagreement rates…
            </p>
          ) : disagreement && !disagreement.measured ? (
            <Empty
              title="Nothing has been resolved in this window yet."
              hint="This is not evidence the reviewer agent is performing well — it means no sampled decision has a human verdict yet."
            />
          ) : disagreement ? (
            <ul className="revagent__list">
              {disagreement.by_object_type.map((row) => (
                <DisagreementRow key={row.object_type} row={row} />
              ))}
            </ul>
          ) : null}
        </div>
      </section>

      <section className="revagent__panel">
        <header className="revagent__panelhead">
          <h2>Sampled decisions</h2>
          <Field label="Outcome">
            <select
              value={outcome}
              onChange={(event) => setParams({ outcome: event.target.value, offset: null })}
              aria-label="Sample outcome"
            >
              {OUTCOMES.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </Field>
        </header>
        <div className="revagent__panelbody">
          {samplesError ? (
            <ErrorState
              title="Sampled decisions could not be loaded"
              detail={samplesError}
              onRetry={() => loadSamples()}
            />
          ) : samplesLoading && !samples ? (
            <p role="status" className="revagent__loading">
              Loading sampled decisions…
            </p>
          ) : samples && samples.items.length > 0 ? (
            <>
              <ul className="revagent__list">
                {samples.items.map((sample) => (
                  <SampleRow key={sample.sample_id} sample={sample} onResolve={onResolve} />
                ))}
              </ul>
              <div className="revagent__pagination">
                <Button
                  onClick={() => setParams({ offset: String(Math.max(0, offset - SAMPLES_LIMIT)) })}
                  disabled={offset === 0}
                >
                  Previous
                </Button>
                <span className="revagent__muted">
                  {offset + 1}–{Math.min(offset + SAMPLES_LIMIT, samples.total)} of {samples.total}
                </span>
                <Button
                  onClick={() => setParams({ offset: String(offset + SAMPLES_LIMIT) })}
                  disabled={offset + SAMPLES_LIMIT >= samples.total}
                >
                  Next
                </Button>
              </div>
            </>
          ) : (
            <Empty title="No sampled decisions match this filter." />
          )}
        </div>
      </section>
    </section>
  );
}
