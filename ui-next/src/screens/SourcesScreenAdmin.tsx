import { useCallback, useMemo, useState } from "react";

import {
  ApiError,
  createAnalysisRun,
  fetchDatasourceAnalysisRuns,
  fetchScanPolicy,
  resumeAnalysisRun,
  testDatasourceConnection,
  upsertScanPolicy,
} from "../lib/api";
import type { AnalysisRunRead, DataSourceRead, ScanPolicyRead, ScanPolicyUpsert } from "../lib/types";
import { useSession } from "../lib/session";
import { Button, ConfirmDialog, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import { LoadingPanel, useAsyncResource, useSubmitAction } from "../components/screenState";

/* ---------------------------------------------------------------------------
   Source administration — operating a source that ALREADY EXISTS (R11-B7).

   THE GAP this closes: the rescan/schedule/retry backend has been merged for
   some time and nothing in this client could reach it. `FirstSourceSetup`
   (review 2026-09-05, T15) starts the FIRST scan of a source that has never
   been scanned, and that was the only caller. A source past its first scan
   could not be re-scanned, its schedule could not be read let alone changed,
   and a failed run could only be retried by calling the API by hand.

   FOUR ENDPOINTS, each already merged, none composed or invented:

     POST /v1/datasources/{id}/test         connection probe (424 on failure)
     GET  /v1/datasources/{id}/scan-policy  the schedule (404 = never scheduled)
     PUT  /v1/datasources/{id}/scan-policy  create/replace the whole schedule
     POST /v1/datasources/{id}/analysis-runs   run a scan now (409 on admission)
     POST /v1/analysis-runs/{run_id}/resume    retry an interrupted run (409)

   THE INVARIANT, and the reason this panel is worth its size: EVERY FACT ON
   SCREEN IS THE SERVER'S. `next_run_at`, `last_triggered_at`, the run's
   status and its counts are read back from the server after every action, and
   the row is never edited in place to say what the click hoped for. A 202 is
   an accepted request, not a finished scan; a resume returns a DIFFERENT run
   id from the one retried (`resumed_from_run_id`), so mutating the failed row
   would be a lie twice over.

   THE SECOND INVARIANT: a refusal is an answer. Every one of these endpoints
   has a state machine behind it that says no in words -- "only interrupted or
   failed runs can resume", whatever run admission says when a scan is already
   in flight, "datasource connection test failed" with the source's status
   committed to CONNECTION_FAILED. `failureText` (not `describeError`) is used
   throughout so the server's own sentence reaches the dialog the operator is
   looking at, rather than being paraphrased into "something went wrong".

   Split out of `SourcesScreen.tsx` rather than added to it: the detail pane
   was already a health read model, and this is a write surface with its own
   confirmation, validation and re-read cycle.
--------------------------------------------------------------------------- */

/** Roles `test_datasource` accepts (`connectivity/router.py`, surface matrix). */
const CONNECTION_ROLES = ["PlatformAdmin", "DataAdmin"];
/** Roles the scan, resume and scan-policy writes accept. */
const SCAN_ROLES = ["PlatformAdmin", "MetadataAdmin", "DataAdmin"];

/** The server's own retry gate, copied from `aida.api.resume_analysis_run`.
 *  A status outside this set is refused with a 409 — so the button is not
 *  offered for it, and the 409 is still rendered if the run moved underneath. */
const RESUMABLE = new Set(["FAILED", "CANCELLED", "CANCELLATION_REQUESTED", "SUBMISSION_FAILED"]);

const RUN_SUCCEEDED = new Set(["COMPLETED", "SUCCEEDED"]);
const RUN_IN_FLIGHT = new Set(["QUEUED", "PENDING", "RUNNING", "PROFILING", "CANCELLATION_REQUESTED"]);

const runTone = (status: string): Tone =>
  RUN_SUCCEEDED.has(status) ? "ok" : RUN_IN_FLIGHT.has(status) ? "info" : "bad";

const connectionTone = (status: string): Tone =>
  status === "ACTIVE" || status === "CONNECTION_VERIFIED"
    ? "ok"
    : status === "CONNECTION_FAILED" || status === "DISABLED"
      ? "bad"
      : "mute";

const stamp = (iso: string | null): string =>
  iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "never";

/** "every 6h", "every 90m" — the interval as an operator states it. */
function intervalWords(minutes: number): string {
  if (minutes % 1440 === 0) return `every ${minutes / 1440}d`;
  if (minutes % 60 === 0) return `every ${minutes / 60}h`;
  return `every ${minutes}m`;
}

function windowWords(start: number | null, end: number | null): string {
  if (start === null || end === null) return "any time of day";
  const pad = (h: number) => `${String(h).padStart(2, "0")}:00`;
  return `${pad(start)}–${pad(end)} UTC`;
}

/* ---------------------------------------------------------------------------
   The policy editor's form state.

   `PUT scan-policy` is an UPSERT of the WHOLE document, not a patch: a field
   the caller omits falls back to the schema default rather than keeping what
   the server holds. So the form is always seeded from the current policy (or
   from the schema's own defaults when there is none), and every field is sent
   on every save -- editing the interval must not silently clear a maintenance
   window somebody else configured.
--------------------------------------------------------------------------- */

export interface PolicyFormState {
  enabled: boolean;
  intervalMinutes: string;
  mode: "FULL" | "INCREMENTAL";
  priority: string;
  usageBoostEnabled: boolean;
  windowStart: string;
  windowEnd: string;
  /** A `datetime-local` value, or empty for "leave the next run where it is". */
  startAt: string;
}

/** Schema defaults from `ScanPolicyUpsert` (`profiling/schemas.py`), so a
 *  source with no policy opens on what the server would itself have used. */
export const POLICY_DEFAULTS: PolicyFormState = {
  enabled: true,
  intervalMinutes: "1440",
  mode: "INCREMENTAL",
  priority: "50",
  usageBoostEnabled: false,
  windowStart: "",
  windowEnd: "",
  startAt: "",
};

export function policyToForm(policy: ScanPolicyRead | null): PolicyFormState {
  if (!policy) return POLICY_DEFAULTS;
  return {
    enabled: policy.enabled,
    intervalMinutes: String(policy.interval_minutes),
    mode: policy.mode === "FULL" ? "FULL" : "INCREMENTAL",
    // The admin's own choice, never the scheduler-visible boosted value: the
    // server keeps them apart as `base_priority`/`priority` precisely so an
    // edit does not compound a previous usage boost (ADR-0017 SS8).
    priority: String(policy.base_priority),
    usageBoostEnabled: policy.usage_boost_enabled,
    windowStart: policy.maintenance_start_hour_utc === null ? "" : String(policy.maintenance_start_hour_utc),
    windowEnd: policy.maintenance_end_hour_utc === null ? "" : String(policy.maintenance_end_hour_utc),
    startAt: "",
  };
}

/**
 * The server's own validation rules, checked before the request.
 *
 * Not a replacement for the server's answer -- a 422 is still rendered if one
 * gets through -- but a maintenance window is the one field pair whose rule
 * ("both or neither, and not equal") is invisible in the UI, and discovering
 * it as a rejected save after typing one hour is a worse way to learn it.
 * Returns `null` when the form is sendable.
 */
export function validatePolicyForm(form: PolicyFormState): string | null {
  const interval = Number(form.intervalMinutes);
  if (!Number.isInteger(interval) || interval < 5 || interval > 525_600) {
    return "Interval must be a whole number of minutes between 5 and 525600 (one year).";
  }
  const priority = Number(form.priority);
  if (!Number.isInteger(priority) || priority < 0 || priority > 100) {
    return "Priority must be a whole number between 0 and 100.";
  }
  const hasStart = form.windowStart !== "";
  const hasEnd = form.windowEnd !== "";
  if (hasStart !== hasEnd) return "Give both maintenance-window hours, or neither.";
  if (hasStart && hasEnd) {
    const start = Number(form.windowStart);
    const end = Number(form.windowEnd);
    if (!Number.isInteger(start) || start < 0 || start > 23 || !Number.isInteger(end) || end < 0 || end > 23) {
      return "Maintenance-window hours are whole hours from 0 to 23, in UTC.";
    }
    if (start === end) return "The maintenance window cannot start and end in the same hour.";
  }
  if (form.startAt !== "" && Number.isNaN(new Date(form.startAt).getTime())) {
    return "The next-run time could not be read as a date.";
  }
  return null;
}

/** The form as the endpoint wants it. `start_at` must carry a timezone — the
 *  handler 422s a naive datetime — and `toISOString()` is always UTC-suffixed,
 *  which is why the `datetime-local` value is converted rather than sent. */
export function policyFormToBody(form: PolicyFormState): ScanPolicyUpsert {
  const hasWindow = form.windowStart !== "" && form.windowEnd !== "";
  return {
    enabled: form.enabled,
    interval_minutes: Number(form.intervalMinutes),
    mode: form.mode,
    priority: Number(form.priority),
    usage_boost_enabled: form.usageBoostEnabled,
    maintenance_start_hour_utc: hasWindow ? Number(form.windowStart) : null,
    maintenance_end_hour_utc: hasWindow ? Number(form.windowEnd) : null,
    ...(form.startAt === "" ? {} : { start_at: new Date(form.startAt).toISOString() }),
  };
}

/** What the operator is being asked to confirm. Every state-changing action on
 *  this panel goes through one of these — none of them fires on the click. */
type Pending =
  | { kind: "test" }
  | { kind: "scan"; mode: "FULL" | "INCREMENTAL" }
  | { kind: "policy"; body: ScanPolicyUpsert; creating: boolean }
  | { kind: "retry"; run: AnalysisRunRead };

export function SourceAdministration({
  source,
  onSourceChanged,
}: {
  source: DataSourceRead;
  /** The fleet list holds this source's status; a connection test changes it,
   *  so the list is re-read rather than patched from the response here. */
  onSourceChanged: () => void;
}) {
  const roles = useSession().me?.roles;
  // `undefined` is "the session has not answered yet", not "no roles". Matching
  // the workbook gate this pane already uses: offer the action and let the
  // server's 403 be the authority, rather than hiding it on a guess.
  const allowed = useCallback(
    (accepted: string[]) => roles === undefined || roles.some((role) => accepted.includes(role)),
    [roles],
  );
  const mayTest = allowed(CONNECTION_ROLES);
  const mayScan = allowed(SCAN_ROLES);

  /* The schedule. A 404 here is an ANSWER — "this source has never been
     scheduled" — and is the state that offers to create a policy, so it is
     classified into `null` rather than left to the resource's error path. */
  const policy = useAsyncResource<ScanPolicyRead | null>(
    async (signal) => {
      try {
        return await fetchScanPolicy(source.id, signal);
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 404) return null;
        throw reason;
      }
    },
    [source.id],
  );

  const runs = useAsyncResource<AnalysisRunRead[]>(
    async (signal) => (await fetchDatasourceAnalysisRuns(source.id, { limit: 5 }, signal)).items,
    [source.id],
  );

  const [pending, setPending] = useState<Pending | null>(null);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<PolicyFormState>(POLICY_DEFAULTS);
  const [formError, setFormError] = useState<string | null>(null);
  const [scanMode, setScanMode] = useState<"FULL" | "INCREMENTAL">("INCREMENTAL");
  const [notice, setNotice] = useState<string | null>(null);
  const action = useSubmitAction<unknown>();

  const setField = useCallback(<K extends keyof PolicyFormState>(key: K, value: PolicyFormState[K]) => {
    setForm((previous) => ({ ...previous, [key]: value }));
  }, []);

  const openEditor = useCallback(() => {
    setForm(policyToForm(policy.data ?? null));
    setFormError(null);
    setEditing(true);
  }, [policy.data]);

  const askToSave = useCallback(() => {
    const invalid = validatePolicyForm(form);
    if (invalid) {
      setFormError(invalid);
      return;
    }
    setFormError(null);
    setPending({ kind: "policy", body: policyFormToBody(form), creating: !policy.data });
  }, [form, policy.data]);

  const latest = runs.data?.[0] ?? null;
  /* The most recent run the SERVER would accept a resume for. Offering retry
     on an older failed run when a newer one succeeded would ask to re-run work
     that has since been redone. */
  const retryable = useMemo(
    () => (latest && RESUMABLE.has(latest.status) ? latest : null),
    [latest],
  );

  const confirmed = useCallback(async () => {
    if (!pending) return;
    setNotice(null);
    if (pending.kind === "test") {
      const tested = await action.run(() => testDatasourceConnection(source.id));
      if (!tested) return;
      setPending(null);
      // The server's status for this source, not "it worked": a probe that
      // succeeds on an unverified source lands on CONNECTION_VERIFIED, and an
      // already-ACTIVE one stays ACTIVE.
      setNotice(`Connection verified. This source now reports ${(tested as DataSourceRead).status}.`);
      onSourceChanged();
      return;
    }
    if (pending.kind === "scan") {
      const created = await action.run(() => createAnalysisRun(source.id, { mode: pending.mode }));
      if (!created) return;
      setPending(null);
      setNotice(
        `Scan accepted (${pending.mode.toLowerCase()}). A 202 is an accepted request, not a finished scan — the run below is read back from the server.`,
      );
      runs.reload();
      return;
    }
    if (pending.kind === "retry") {
      const resumed = await action.run(() => resumeAnalysisRun(pending.run.id));
      if (!resumed) return;
      setPending(null);
      // A resume reserves a NEW run; saying "retried" without saying that
      // would leave the operator looking for the old id to change status.
      setNotice(
        `Retry accepted. The server started a new run resumed from ${pending.run.id.slice(0, 8)}; the failed run stays as it is.`,
      );
      runs.reload();
      return;
    }
    const saved = await action.run(() => upsertScanPolicy(source.id, pending.body));
    if (!saved) return;
    setPending(null);
    setEditing(false);
    setNotice(pending.creating ? "Scan policy created." : "Scan policy updated.");
    policy.reload();
  }, [action, onSourceChanged, pending, policy, runs, source.id]);

  const confirmTitle =
    pending?.kind === "test"
      ? "Test this connection"
      : pending?.kind === "scan"
        ? "Start a scan now"
        : pending?.kind === "retry"
          ? "Retry this run"
          : pending?.kind === "policy"
            ? pending.creating
              ? "Create this scan policy"
              : "Replace this scan policy"
            : "";

  const confirmDescription =
    pending?.kind === "test"
      ? `Opens a live connection to ${source.name} using its stored credential and rewrites its status: CONNECTION_VERIFIED if the probe succeeds, CONNECTION_FAILED if it does not. A failing source is not scanned.`
      : pending?.kind === "scan"
        ? pending.mode === "FULL"
          ? `Re-reads every object in ${source.name} from scratch. A full scan costs the connector more than an incremental one and may be refused while another run is in flight.`
          : `Scans ${source.name} for what has changed since its last successful run. May be refused while another run is in flight.`
        : pending?.kind === "retry"
          ? `Starts a NEW run resumed from the ${pending.run.status.toLowerCase()} one, with the same mode and priority. The failed run is kept as it is — nothing is rewritten.`
          : pending?.kind === "policy"
            ? pending.creating
              ? `Schedules ${source.name} to be scanned ${intervalWords(pending.body.interval_minutes)} from now on.`
              : `Replaces the whole schedule for ${source.name}. Every field below is sent, including the ones you did not change.`
            : undefined;

  return (
    <section className="srcadmin" aria-labelledby="srcadmin-heading">
      <div className="evp__sub" id="srcadmin-heading">Source administration</div>

      {/* ------------------------------------------------ connection -------- */}
      <div className="srcadmin__block">
        <div className="srcadmin__blockhead">
          <h3 className="srcadmin__h3">Connection</h3>
          <Pill tone={connectionTone(source.status)}>{source.status.toLowerCase().replace(/_/g, " ")}</Pill>
        </div>
        <p className="srcadmin__note">
          The status beside this heading is what the fleet endpoint reports, not the result of the
          last button pressed here.
        </p>
        <div className="srcadmin__actions">
          <Button disabled={!mayTest || action.submitting} onClick={() => setPending({ kind: "test" })}>
            Test connection
          </Button>
          {mayTest ? null : (
            <span className="srcadmin__denied">
              Testing a connection requires Data Admin or Platform Admin.
            </span>
          )}
        </div>
      </div>

      {/* ------------------------------------------------ scan policy ------- */}
      <div className="srcadmin__block">
        <div className="srcadmin__blockhead">
          <h3 className="srcadmin__h3">Scan policy</h3>
          {policy.data ? (
            <Pill tone={policy.data.enabled ? "ok" : "mute"}>
              {policy.data.enabled ? "scheduled" : "paused"}
            </Pill>
          ) : null}
        </div>

        {policy.error ? (
          <ErrorState
            title="Scan policy could not be read"
            detail={policy.error}
            onRetry={() => policy.reload()}
          />
        ) : policy.loading || policy.data === undefined ? (
          /* `undefined` is "no answer yet" and `null` is "the server answered
             404" — two different sentences, and only one of them is "this
             source is never scanned on a schedule". */
          <LoadingPanel label="Reading the scan policy…" />
        ) : policy.data === null ? (
          <Empty
            title="This source has no scan policy"
            hint="It is never scanned on a schedule. Anyone can still start a scan by hand below."
          />
        ) : (
          <dl className="srcadmin__facts">
            <div>
              <dt>Interval</dt>
              <dd>
                {intervalWords(policy.data.interval_minutes)} · {policy.data.mode.toLowerCase()}
              </dd>
            </div>
            <div>
              <dt>Maintenance window</dt>
              <dd>{windowWords(policy.data.maintenance_start_hour_utc, policy.data.maintenance_end_hour_utc)}</dd>
            </div>
            <div>
              <dt>Priority</dt>
              <dd>
                {policy.data.base_priority} as set
                {policy.data.usage_boost_enabled
                  ? ` · ${policy.data.priority} after a +${policy.data.computed_usage_boost} usage boost`
                  : " · usage boost off"}
              </dd>
            </div>
            <div>
              <dt>Next run</dt>
              <dd>{stamp(policy.data.next_run_at)}</dd>
            </div>
            <div>
              <dt>Last triggered</dt>
              <dd>{stamp(policy.data.last_triggered_at)}</dd>
            </div>
          </dl>
        )}

        {editing ? (
          <div className="srcadmin__form" role="group" aria-label="Scan policy">
            <div className="srcadmin__grid">
              <Field label="Interval (minutes)">
                <input
                  type="number"
                  min={5}
                  max={525600}
                  value={form.intervalMinutes}
                  onChange={(e) => setField("intervalMinutes", e.target.value)}
                />
              </Field>
              <Field label="Mode">
                <select
                  value={form.mode}
                  onChange={(e) => setField("mode", e.target.value as "FULL" | "INCREMENTAL")}
                >
                  <option value="INCREMENTAL">incremental</option>
                  <option value="FULL">full</option>
                </select>
              </Field>
              <Field label="Priority (0–100)">
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={form.priority}
                  onChange={(e) => setField("priority", e.target.value)}
                />
              </Field>
              <Field label="Window start hour (UTC)">
                <input
                  type="number"
                  min={0}
                  max={23}
                  placeholder="any"
                  value={form.windowStart}
                  onChange={(e) => setField("windowStart", e.target.value)}
                />
              </Field>
              <Field label="Window end hour (UTC)">
                <input
                  type="number"
                  min={0}
                  max={23}
                  placeholder="any"
                  value={form.windowEnd}
                  onChange={(e) => setField("windowEnd", e.target.value)}
                />
              </Field>
              <Field label="Next run at (optional)">
                <input
                  type="datetime-local"
                  value={form.startAt}
                  onChange={(e) => setField("startAt", e.target.value)}
                />
              </Field>
            </div>
            <label className="srcadmin__check">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => setField("enabled", e.target.checked)}
              />
              <span>Scheduled — the scheduler may start runs for this source</span>
            </label>
            <label className="srcadmin__check">
              <input
                type="checkbox"
                checked={form.usageBoostEnabled}
                onChange={(e) => setField("usageBoostEnabled", e.target.checked)}
              />
              <span>Let usage raise this source&rsquo;s priority above the value set here</span>
            </label>
            <p className="srcadmin__note">
              Saving replaces the whole policy — every field above is sent, including the ones you
              did not touch. Leave &ldquo;next run at&rdquo; empty to keep the schedule where it is.
            </p>
            {formError ? (
              <p className="srcadmin__err" role="alert">
                {formError}
              </p>
            ) : null}
            <div className="srcadmin__actions">
              <Button variant="primary" disabled={action.submitting} onClick={askToSave}>
                {policy.data ? "Save scan policy" : "Create scan policy"}
              </Button>
              <Button
                disabled={action.submitting}
                onClick={() => {
                  setEditing(false);
                  setFormError(null);
                }}
              >
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <div className="srcadmin__actions">
            <Button disabled={!mayScan || policy.loading} onClick={openEditor}>
              {policy.data ? "Edit scan policy" : "Create a scan policy"}
            </Button>
            {mayScan ? null : (
              <span className="srcadmin__denied">
                Changing a schedule requires Data Admin, Metadata Admin or Platform Admin.
              </span>
            )}
          </div>
        )}
      </div>

      {/* ------------------------------------------------ runs -------------- */}
      <div className="srcadmin__block">
        <div className="srcadmin__blockhead">
          <h3 className="srcadmin__h3">Scans</h3>
        </div>

        <div className="srcadmin__actions">
          <Field label="Mode">
            <select
              value={scanMode}
              onChange={(e) => setScanMode(e.target.value as "FULL" | "INCREMENTAL")}
            >
              <option value="INCREMENTAL">incremental</option>
              <option value="FULL">full</option>
            </select>
          </Field>
          <Button
            variant="primary"
            disabled={!mayScan || action.submitting}
            onClick={() => setPending({ kind: "scan", mode: scanMode })}
          >
            Re-scan now
          </Button>
          {retryable ? (
            <Button
              disabled={!mayScan || action.submitting}
              onClick={() => setPending({ kind: "retry", run: retryable })}
              title={`Resume the ${retryable.status.toLowerCase()} run from ${stamp(retryable.updated_at)}`}
            >
              Retry the {retryable.status.toLowerCase()} run
            </Button>
          ) : null}
        </div>

        {runs.error ? (
          <ErrorState
            title="Scan history could not be read"
            detail={runs.error}
            onRetry={() => runs.reload()}
          />
        ) : runs.loading ? (
          <LoadingPanel label="Reading scan history…" />
        ) : (runs.data?.length ?? 0) === 0 ? (
          <Empty
            title="This source has never been scanned"
            hint="Start one above; nothing reaches the catalog until a scan completes."
          />
        ) : (
          <ol className="srcadmin__runs">
            {runs.data!.map((run) => (
              <li key={run.id} className="srcadmin__run" aria-label={`${run.mode} run ${run.id}`}>
                <div className="srcadmin__runhead">
                  <Pill tone={runTone(run.status)}>{run.status.toLowerCase().replace(/_/g, " ")}</Pill>
                  <Pill tone="mute">{run.mode.toLowerCase()}</Pill>
                  <Pill tone="mute">{run.trigger_type.toLowerCase()}</Pill>
                  <span className="srcadmin__runtime">{stamp(run.updated_at)}</span>
                </div>
                <div className="srcadmin__runmeta">
                  {run.discovered_tables} tables discovered · {run.created_objects} created ·{" "}
                  {run.changed_objects} changed
                  {run.resumed_from_run_id ? ` · resumed from ${run.resumed_from_run_id.slice(0, 8)}` : ""}
                </div>
                {run.error_message ? (
                  <div className="srcadmin__runerr" role="alert">
                    {run.error_class ? <b>{run.error_class}: </b> : null}
                    {run.error_message}
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
        )}
      </div>

      {notice ? (
        <p className="srcadmin__notice" role="status">
          {notice}
        </p>
      ) : null}

      {pending ? (
        <ConfirmDialog
          title={confirmTitle}
          description={confirmDescription}
          confirmLabel={
            pending.kind === "test"
              ? "Run the test"
              : pending.kind === "scan"
                ? "Start the scan"
                : pending.kind === "retry"
                  ? "Retry the run"
                  : pending.creating
                    ? "Create policy"
                    : "Replace policy"
          }
          destructive={pending.kind !== "test"}
          busy={action.submitting}
          /* The dialog stays open on a refusal, holding the server's own
             sentence, because that sentence IS the answer: "only interrupted
             or failed runs can resume" and a run-admission rejection send an
             operator to two different places. */
          error={action.error}
          onCancel={() => {
            setPending(null);
            action.reset();
          }}
          onConfirm={() => void confirmed()}
        />
      ) : null}
    </section>
  );
}
