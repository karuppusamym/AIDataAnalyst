import { useCallback, useEffect, useMemo, useState } from "react";

import {
  createAnalysisRun,
  fetchAgentRuns,
  fetchCatalogRows,
  fetchDatasourceAnalysisRuns,
  listOrgDatasources,
  fetchOrgWorkspaces,
} from "../lib/api";
import { ApiError } from "../lib/http";
import { useOrgId } from "../lib/org";
import { useSession } from "../lib/session";
import { useUrlState } from "../lib/useUrlState";
import type { AnalysisRunRead, DataSourceRead } from "../lib/types";
import { Button, Pill } from "./primitives";
import { useAsyncResource, useSubmitAction } from "./screenState";
import "./FirstSourceSetup.css";

/* ---------------------------------------------------------------------------
   First-source setup, from server state (review 2026-09-05, T15 · Journey A).

   THE DEFECT this removes: the only "setup progress" this app had was
   `OnboardingWizard`'s per-persona checklist, whose ticks are checkboxes in
   `localStorage`. A person could tick every box on an installation with no
   datasource, no scan and an empty catalog, and the app would agree that setup
   was finished. The review is explicit about this: "Base completion on actual
   resources and successful operations; manual local checkboxes are a personal
   convenience, not setup certification."

   THE INVARIANT: every step below is a QUESTION ANSWERED BY THE SERVER --
   does a datasource exist, did a scan run and how did it end, did the catalog
   receive rows, has anyone consumed it. Nothing here is remembered locally.
   Closing the browser and coming back re-asks all four questions and lands on
   whichever step is genuinely next; `localStorage` is consulted for exactly
   one thing, whether the panel was collapsed, and a collapsed panel is not a
   completed one.

   THE SECOND INVARIANT: a signal this principal cannot read is UNKNOWN, never
   zero and never "done". A 403 on scan history means "you may not see whether
   this was scanned", which is a different sentence from "it was never
   scanned", and only one of them is true. Configured is not healthy; a
   completed scan that returned nothing is a failure path with its own
   wording, not a step still in progress.
--------------------------------------------------------------------------- */

/** A server answer, or an honest account of why there isn't one. */
export type Signal<T> =
  | { readonly kind: "ok"; readonly value: T }
  | { readonly kind: "denied"; readonly message: string }
  | { readonly kind: "unavailable"; readonly message: string };

/**
 * Run one read and classify its failure instead of throwing it away.
 *
 * A rejected resource collapses to a string in `useAsyncResource`, which is
 * right for a whole-screen load but wrong here: this panel has to tell a 403
 * (unknown, and say who to ask) apart from a 500 (unknown, and retry) apart
 * from a real empty answer (known, and act). So the read never rejects, and
 * the classification is part of the value.
 */
export async function readSignal<T>(load: () => Promise<T>): Promise<Signal<T>> {
  try {
    return { kind: "ok", value: await load() };
  } catch (reason) {
    if ((reason as Error | null)?.name === "AbortError") throw reason;
    if (reason instanceof ApiError && (reason.status === 403 || reason.status === 401)) {
      return { kind: "denied", message: reason.detail };
    }
    return {
      kind: "unavailable",
      message: reason instanceof ApiError ? reason.detail : (reason as Error).message,
    };
  }
}

/**
 * A step's state.
 *
 * `failed` and `unknown` are deliberately not folded into `todo`. A scan that
 * failed is not a scan that has not happened yet, and a signal nobody may read
 * is not a signal that came back empty -- collapsing either is exactly the
 * "unknown becomes zero" the review names.
 */
export type SetupStepState = "done" | "todo" | "running" | "failed" | "unknown";

export interface SetupStepAction {
  readonly label: string;
  /** A screen to open, with the context that makes it useful. */
  readonly navId?: string;
  readonly params?: Record<string, string>;
  /** An action taken here rather than elsewhere (starting the first scan). */
  readonly kind?: "start-scan";
}

export interface SetupStep {
  readonly id: "workspace" | "source" | "scan" | "catalog" | "consume";
  readonly title: string;
  readonly state: SetupStepState;
  /** What is true right now, in the server's terms. */
  readonly detail: string;
  /** What must already be true for the primary action to be possible. */
  readonly prerequisite?: string;
  readonly action?: SetupStepAction;
  /** The link onward. Always present once the step can be acted on. */
  readonly onward?: SetupStepAction;
}

export interface SetupSignals {
  readonly workspaces: Signal<{ total: number; active: number }> | null;
  readonly datasources: Signal<readonly DataSourceRead[]> | null;
  /** `null` when there is no source to ask about yet -- not "no runs". */
  readonly runs: Signal<readonly AnalysisRunRead[]> | null;
  readonly catalog: Signal<{ rows: number; total: number | null }> | null;
  readonly consumption: Signal<{ runs: number }> | null;
  /** The datasource the last three steps are about, when one has been chosen. */
  readonly subject: DataSourceRead | null;
}

/* The scan vocabulary, from `AnalysisRun.status` in `models.py` and the
 * statuses `workflows/activities.py` actually writes. `SUCCEEDED` is not one
 * the backend writes -- the bundled fixtures use it -- so both spellings are
 * accepted for success rather than letting demo data read as a failed scan. */
const RUN_SUCCEEDED = new Set(["COMPLETED", "SUCCEEDED"]);
const RUN_IN_FLIGHT = new Set(["QUEUED", "RUNNING", "PROFILING", "CANCELLATION_REQUESTED"]);
const RUN_FAILED = new Set(["FAILED", "SUBMISSION_FAILED", "CANCELLED"]);

const unknownFrom = <T,>(signal: Signal<T>, subject: string): string =>
  signal.kind === "denied"
    ? `You do not have permission to read ${subject} (${signal.message}). Its state is unknown — not "none".`
    : `${subject} could not be read (${(signal as { message: string }).message}). Its state is unknown — not "none".`;

/**
 * The whole readiness derivation, as a pure function of what the server said.
 *
 * Separated from the component on purpose: "the step state is recomputed from
 * server data" is the property worth testing, and it is only testable if the
 * derivation does not need a React tree, a browser or a clock.
 */
export function deriveSetupSteps(signals: SetupSignals): SetupStep[] {
  const steps: SetupStep[] = [];

  // 1. Somewhere for the work to live.
  const workspaces = signals.workspaces;
  steps.push(
    workspaces === null || workspaces.kind !== "ok"
      ? {
          id: "workspace",
          title: "Create a workspace",
          state: "unknown",
          detail: workspaces
            ? unknownFrom(workspaces, "this organization's workspaces")
            : "Waiting for the organization's workspaces.",
          action: { label: "Open administration", navId: "administration" },
        }
      : workspaces.value.active > 0
        ? {
            id: "workspace",
            title: "Create a workspace",
            state: "done",
            detail: `${workspaces.value.active} active workspace${workspaces.value.active === 1 ? "" : "s"} in this organization.`,
            onward: { label: "Manage access", navId: "workspace-access" },
          }
        : workspaces.value.total > 0
          ? {
              id: "workspace",
              title: "Create a workspace",
              state: "failed",
              // Configured is not active, and neither is healthy.
              detail: `${workspaces.value.total} workspace${workspaces.value.total === 1 ? " exists" : "s exist"} but none is ACTIVE. Work cannot be scoped to an inactive workspace.`,
              action: { label: "Open administration", navId: "administration" },
            }
          : {
              id: "workspace",
              title: "Create a workspace",
              state: "todo",
              detail: "This organization has no workspace yet.",
              prerequisite: "An organization you may administer.",
              action: { label: "Create a workspace", navId: "administration" },
            },
  );

  // 2. Something to read data from.
  const datasources = signals.datasources;
  const subject = signals.subject;
  steps.push(
    datasources === null || datasources.kind !== "ok"
      ? {
          id: "source",
          title: "Register a source",
          state: "unknown",
          detail: datasources
            ? unknownFrom(datasources, "this organization's data sources")
            : "Waiting for the organization's data sources.",
          action: { label: "Open sources", navId: "sources" },
        }
      : datasources.value.length === 0
        ? {
            id: "source",
            title: "Register a source",
            state: "todo",
            detail: "No data source is registered in this organization.",
            prerequisite: "A project and a stored credential reference for the connector.",
            action: { label: "Register a source", navId: "administration" },
          }
        : subject && subject.status !== "ACTIVE"
          ? {
              id: "source",
              title: "Register a source",
              state: "failed",
              detail: `${subject.name} is registered but its status is ${subject.status}. A source that is not ACTIVE is never scanned.`,
              action: { label: "Open sources", navId: "sources", params: { source: subject.id } },
            }
          : {
              id: "source",
              title: "Register a source",
              state: "done",
              detail: subject
                ? `${subject.name} (${subject.connector_type}) is registered and ACTIVE.`
                : `${datasources.value.length} source${datasources.value.length === 1 ? "" : "s"} registered.`,
              onward: subject
                ? { label: "Open this source", navId: "sources", params: { source: subject.id } }
                : { label: "Open sources", navId: "sources" },
            },
  );

  // 3. A scan, and how it ended.
  const runs = signals.runs;
  const latest = runs && runs.kind === "ok" ? (runs.value[0] ?? null) : null;
  const scanState: SetupStepState =
    runs === null
      ? "todo"
      : runs.kind !== "ok"
        ? "unknown"
        : latest === null
          ? "todo"
          : RUN_SUCCEEDED.has(latest.status)
            ? "done"
            : RUN_IN_FLIGHT.has(latest.status)
              ? "running"
              : RUN_FAILED.has(latest.status)
                ? "failed"
                : "unknown";
  steps.push({
    id: "scan",
    title: "Scan the source",
    state: scanState,
    detail:
      runs === null
        ? /* Not asked yet -- because no source has been chosen, or because the
             read is still in flight. Neither is "never scanned". */
          subject
          ? "Reading this source's scan history…"
          : "No source has been chosen to scan yet."
        : runs.kind !== "ok"
          ? unknownFrom(runs, "this source's scan history")
          : latest === null
            ? "This source has never been scanned."
            : RUN_SUCCEEDED.has(latest.status)
              ? `Last scan ${latest.status.toLowerCase()}, discovering ${latest.discovered_tables} table${latest.discovered_tables === 1 ? "" : "s"} (${latest.created_objects} new, ${latest.changed_objects} changed).`
              : RUN_IN_FLIGHT.has(latest.status)
                ? `A scan is ${latest.status.toLowerCase()}. Nothing is complete until it ends.`
                : RUN_FAILED.has(latest.status)
                  ? /* Named as a failure, with the server's own reason. A failed
                       scan used to be indistinguishable from one that had not
                       started. */
                    `The last scan ${latest.status.toLowerCase()}${latest.error_class ? ` — ${latest.error_class}` : ""}${latest.error_message ? `: ${latest.error_message}` : "."}`
                  : `The last scan reports an unrecognised status (${latest.status}). Treat it as unknown.`,
    prerequisite: "An ACTIVE source and a principal with MetadataAdmin or DataAdmin.",
    action:
      subject && (scanState === "todo" || scanState === "failed")
        ? { label: latest === null ? "Start the first scan" : "Run the scan again", kind: "start-scan" }
        : undefined,
    onward: subject
      ? { label: "Watch it in Operations", navId: "operations", params: { ds: subject.id } }
      : undefined,
  });

  // 4. What actually arrived.
  const catalog = signals.catalog;
  const scanFinished = scanState === "done";
  steps.push({
    id: "catalog",
    title: "See what arrived",
    state:
      catalog === null
        ? "todo"
        : catalog.kind !== "ok"
          ? "unknown"
          : catalog.value.rows > 0
            ? "done"
            : scanFinished
              ? /* The connected-but-empty path. A scan that succeeded and left
                   the catalog empty is a real, reportable outcome -- a scope
                   that matched nothing, or a principal the connector can see
                   nothing with -- and it is not "still waiting". */
                "failed"
              : "todo",
    detail:
      catalog === null
        ? subject
          ? "Reading this source's catalog rows…"
          : "No source has been chosen yet."
        : catalog.kind !== "ok"
          ? unknownFrom(catalog, "this source's catalog rows")
            : catalog.value.rows > 0
              ? catalog.value.total !== null
                ? `The catalog holds ${catalog.value.total} asset${catalog.value.total === 1 ? "" : "s"} from this source.`
                : // The row endpoint is cursor-paged and does not always report
                  // a total. "At least one" is what was actually observed.
                  "The catalog holds at least one asset from this source."
            : scanFinished
              ? "The scan finished and the catalog received no rows from this source. Check the scan's scope and what the connector's credential is permitted to see."
              : "Nothing has arrived from this source yet.",
    prerequisite: "A scan that completed.",
    onward: subject
      ? { label: "Browse this source in the catalog", navId: "catalog", params: { ds: subject.id } }
      : undefined,
  });

  // 5. Somebody using it. The step the review calls "first consumer workflow".
  const consumption = signals.consumption;
  const catalogReady = steps[3]?.state === "done";
  steps.push({
    id: "consume",
    title: "Answer a first question",
    state:
      consumption === null
        ? "todo"
        : consumption.kind !== "ok"
          ? "unknown"
          : consumption.value.runs > 0
            ? "done"
            : "todo",
    detail:
      consumption === null
        ? subject
          ? "Reading this source's question history…"
          : "No source has been chosen yet."
        : consumption.kind !== "ok"
          ? unknownFrom(consumption, "this source's question history")
          : consumption.value.runs > 0
            ? `${consumption.value.runs} question${consumption.value.runs === 1 ? " has" : "s have"} been answered against this source.`
            : "No question has been asked against this source yet.",
    prerequisite: catalogReady
      ? undefined
      : "Assets from this source visible in the catalog.",
    action:
      subject && catalogReady
        ? { label: "Ask a question", navId: "analyst", params: { ds: subject.id } }
        : undefined,
    onward: subject
      ? { label: "Browse the catalog", navId: "catalog", params: { ds: subject.id } }
      : { label: "Browse the catalog", navId: "catalog" },
  });

  return steps;
}

/** The step a returning user should be looking at: the first that is not done. */
export function currentStepOf(steps: readonly SetupStep[]): SetupStep | null {
  return steps.find((step) => step.state !== "done") ?? null;
}

const STATE_LABEL: Record<SetupStepState, string> = {
  done: "done",
  todo: "not started",
  running: "in progress",
  failed: "needs attention",
  unknown: "unknown",
};

const STATE_TONE = {
  done: "ok",
  todo: "mute",
  running: "info",
  failed: "bad",
  unknown: "warn",
} as const;

/* Collapse state is keyed by organization AND principal, the convention F22
 * already established for onboarding: one browser is shared by people and by
 * tenants, and a panel dismissed for one of them is not dismissed for another.
 * This is the ONLY thing stored, and it never decides whether a step is done. */
const COLLAPSE_PREFIX = "atlas.setup";
const collapseKey = (orgId: string, principalId: string) =>
  `${COLLAPSE_PREFIX}.${orgId}.${principalId}.collapsed`;

function readCollapsed(key: string): boolean {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false; // storage blocked -- show the panel rather than hide progress
  }
}

function writeCollapsed(key: string, collapsed: boolean): void {
  try {
    localStorage.setItem(key, collapsed ? "1" : "0");
  } catch {
    /* best effort; the panel simply reappears next time */
  }
}

export function FirstSourceSetup({
  onNavigate,
}: {
  onNavigate: (navId: string, params?: Record<string, string>) => void;
}) {
  const organizationId = useOrgId();
  const principalId = useSession().me?.principal_id ?? "anonymous";
  const [params] = useUrlState();
  const requestedDatasourceId = params.get("ds");

  const workspaces = useAsyncResource(
    (signal) =>
      readSignal(async () => {
        const page = await fetchOrgWorkspaces(organizationId, signal);
        return {
          total: page.total ?? page.items.length,
          active: page.items.filter((workspace) => workspace.status === "ACTIVE").length,
        };
      }),
    [organizationId],
  );

  const datasources = useAsyncResource(
    (signal) =>
      readSignal(async () => {
        const page = await listOrgDatasources(organizationId, signal);
        return page.items as readonly DataSourceRead[];
      }),
    [organizationId],
  );

  /* The source the last three steps are about. A `?ds=` already in the URL
   * wins, so arriving from a source link keeps talking about that source;
   * otherwise the first ACTIVE one, and only then the first registered one --
   * which is how a source registered but left DISABLED becomes visible as a
   * failure instead of being skipped over. */
  const subject = useMemo<DataSourceRead | null>(() => {
    if (datasources.data?.kind !== "ok") return null;
    const items = datasources.data.value;
    return (
      items.find((item) => item.id === requestedDatasourceId) ??
      items.find((item) => item.status === "ACTIVE") ??
      items[0] ??
      null
    );
  }, [datasources.data, requestedDatasourceId]);

  const subjectId = subject?.id ?? null;

  const runs = useAsyncResource(
    (signal) =>
      readSignal(async () => {
        const page = await fetchDatasourceAnalysisRuns(subjectId!, { limit: 5 }, signal);
        return page.items as readonly AnalysisRunRead[];
      }),
    [subjectId],
    { enabled: subjectId !== null },
  );

  const catalog = useAsyncResource(
    (signal) =>
      readSignal(async () => {
        const page = await fetchCatalogRows(
          { organizationId, datasourceId: subjectId!, limit: 1 },
          signal,
        );
        return { rows: page.items.length, total: page.total ?? null };
      }),
    [organizationId, subjectId],
    { enabled: subjectId !== null },
  );

  const consumption = useAsyncResource(
    (signal) =>
      readSignal(async () => {
        const page = await fetchAgentRuns(subjectId!, { limit: 1 }, signal);
        return { runs: page.total ?? page.items.length };
      }),
    [subjectId],
    { enabled: subjectId !== null },
  );

  const scan = useSubmitAction<AnalysisRunRead>();
  const startScan = useCallback(async () => {
    if (!subjectId) return;
    const created = await scan.run(() => createAnalysisRun(subjectId, { mode: "FULL" }));
    // A 202 is an accepted request, never a finished scan: re-read the run
    // list so the step reports what the server now says rather than what this
    // click hoped for.
    if (created) runs.reload();
  }, [runs, scan, subjectId]);

  const steps = useMemo(
    () =>
      deriveSetupSteps({
        workspaces: workspaces.data ?? null,
        datasources: datasources.data ?? null,
        runs: subjectId === null ? null : (runs.data ?? null),
        catalog: subjectId === null ? null : (catalog.data ?? null),
        consumption: subjectId === null ? null : (consumption.data ?? null),
        subject,
      }),
    [workspaces.data, datasources.data, runs.data, catalog.data, consumption.data, subject, subjectId],
  );

  const current = currentStepOf(steps);
  const complete = current === null;
  const loading =
    workspaces.loading || datasources.loading || runs.loading || catalog.loading || consumption.loading;

  const key = collapseKey(organizationId, principalId);
  const [collapsed, setCollapsed] = useState(() => readCollapsed(key));
  useEffect(() => {
    setCollapsed(readCollapsed(key));
  }, [key]);
  const toggle = () => {
    setCollapsed((previous) => {
      writeCollapsed(key, !previous);
      return !previous;
    });
  };

  const take = (action: SetupStepAction) => {
    if (action.kind === "start-scan") {
      void startScan();
      return;
    }
    if (action.navId) onNavigate(action.navId, action.params);
  };

  return (
    <section className="fss" aria-label="First source setup">
      <header className="fss__head">
        <div>
          <span className="fss__eyebrow">Setup</span>
          <h2 className="fss__h1">Make a source usable</h2>
          <p className="fss__lede">
            {complete
              ? "Every step below is confirmed by the platform, not by a checkbox."
              : current
                ? `Next: ${current.title}.`
                : "Reading the workspace…"}{" "}
            {loading ? "Refreshing…" : null}
          </p>
        </div>
        <button className="fss__toggle" onClick={toggle} aria-expanded={!collapsed}>
          {collapsed ? "Show steps" : "Hide steps"}
        </button>
      </header>

      {collapsed ? null : (
        <ol className="fss__list">
          {steps.map((step, index) => (
            <li key={step.id} className={`fss__step fss__step--${step.state}`}>
              <span className="fss__n" aria-hidden="true">
                {index + 1}
              </span>
              <div className="fss__body">
                <div className="fss__title">
                  <span>{step.title}</span>
                  <Pill tone={STATE_TONE[step.state]}>{STATE_LABEL[step.state]}</Pill>
                </div>
                <p className="fss__detail">{step.detail}</p>
                {step.prerequisite && step.state !== "done" ? (
                  <p className="fss__prereq">Needs: {step.prerequisite}</p>
                ) : null}
                {step.id === "scan" && scan.error ? (
                  <p className="fss__err" role="alert">
                    {scan.error}
                  </p>
                ) : null}
              </div>
              <div className="fss__act">
                {step.action ? (
                  <Button
                    variant={step.id === current?.id ? "primary" : undefined}
                    disabled={step.action.kind === "start-scan" && scan.submitting}
                    onClick={() => take(step.action!)}
                  >
                    {step.action.kind === "start-scan" && scan.submitting
                      ? "Starting…"
                      : step.action.label}
                  </Button>
                ) : null}
                {step.onward ? (
                  <button className="fss__onward" onClick={() => take(step.onward!)}>
                    {step.onward.label} <span aria-hidden="true">→</span>
                  </button>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
