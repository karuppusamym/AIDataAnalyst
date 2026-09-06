import { useCallback, useEffect, useRef, useState } from "react";
import type { DependencyList, ReactNode } from "react";

import { ApiError } from "../lib/http";
import "./screenState.css";

/* ---------------------------------------------------------------------------
   The four controllers every governed-object screen was re-deriving
   (review 2026-09-05, R06).

   THE DEFECT these exist to remove: nine screens each hand-rolled the same
   read, the same write, and the same message strip, and they did not agree.
   `ContextProductsScreen`, `ToolRegistryScreen` and `AdministrationScreen`
   guarded a slow response against a fast one with a monotonic ticket;
   `TransformationsScreen`'s three loaders and `WorkspaceAccessScreen`'s
   summary loader did not, and relied on `AbortController` alone -- which
   cancels the request but does not order the two `setState` calls when the
   abort loses the race. The same list therefore recovered from a
   project switch on some screens and not on others, and nothing in the code
   said which behaviour was intended.

   The nine create forms diverged the same way. Each held its own
   `submitting`/`error`/`created` triple, each re-wrote
   `e instanceof ApiError ? e.detail : (e as Error).message`, and each
   re-entrancy check read `submitting` out of the closure that had just been
   captured -- so a second submit dispatched in the same tick as the first
   was not actually blocked by it.

   THE INVARIANT: a screen states *what* it reads and *what* it writes. When
   a request is superseded, how a failure becomes a sentence, and whether a
   second submit is admitted are decided once, here.

   These are controllers, not visual primitives, which is why they live
   beside `primitives.tsx` rather than inside it.
--------------------------------------------------------------------------- */

const isAbort = (reason: unknown): boolean =>
  (reason as Error | null)?.name === "AbortError";

/**
 * The server's own words for a failure.
 *
 * Deliberately NOT `describeError` from `primitives.tsx`, which is the right
 * choice for a whole-surface load failure: it replaces a 403/404/409 with
 * guidance ("ask its owner for access"). These screens report policy
 * decisions -- "dbt integration is disabled for this organization", "a
 * different reviewer must approve this binding" -- into a status strip a
 * steward reads as the platform's answer, and paraphrasing the server there
 * loses the reason the request was refused.
 */
export function failureText(reason: unknown): string {
  if (reason instanceof ApiError) return reason.detail;
  if (reason instanceof Error) return reason.message;
  return "The request failed.";
}

export interface AsyncResource<T> {
  /** `undefined` until the first response, and while the resource is disabled. */
  readonly data: T | undefined;
  readonly loading: boolean;
  readonly error: string | null;
  /** Discard and re-issue the request. */
  readonly reload: () => void;
  /** Local edit of the loaded value -- an append after a create, a filter
   *  after a decision. A subsequent `reload` overwrites it with the server's
   *  answer, which is the point: an optimistic edit is never authoritative. */
  readonly setData: (update: (previous: T | undefined) => T | undefined) => void;
}

/**
 * One read, with its own abort and its own ordering.
 *
 * `load` is re-created on every render, so it is intentionally not a
 * dependency: `deps` declares what it closes over, exactly as it did when
 * each screen wrote this out by hand with `useCallback`.
 *
 * Two guards, not one. `AbortController` stops a superseded request from
 * finishing; the monotonic ticket stops a superseded request that already
 * finished from being written to state. Both are needed -- abort is a
 * request to stop, not a promise that nothing arrives.
 */
export function useAsyncResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  options: { enabled?: boolean } = {},
): AsyncResource<T> {
  const enabled = options.enabled ?? true;
  const [data, setDataState] = useState<T | undefined>(undefined);
  // Seeded from `enabled` so an enabled resource never renders its empty
  // state for one frame before the request it is about to make.
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);
  const inflight = useRef<AbortController | null>(null);
  const ticket = useRef(0);

  const run = useCallback(
    () => {
      inflight.current?.abort();
      if (!enabled) {
        ticket.current += 1;
        inflight.current = null;
        setDataState(undefined);
        setLoading(false);
        setError(null);
        return;
      }
      const controller = new AbortController();
      inflight.current = controller;
      const mine = (ticket.current += 1);
      setLoading(true);
      setError(null);
      void load(controller.signal).then(
        (value) => {
          if (mine !== ticket.current) return;
          setDataState(value);
          setLoading(false);
        },
        (reason: unknown) => {
          if (mine !== ticket.current || isAbort(reason)) return;
          setError(failureText(reason));
          setLoading(false);
        },
      );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [enabled, ...deps],
  );

  useEffect(() => {
    run();
    return () => inflight.current?.abort();
  }, [run]);

  const setData = useCallback(
    (update: (previous: T | undefined) => T | undefined) => setDataState(update),
    [],
  );

  return { data, loading, error, reload: run, setData };
}

export interface SubmitAction<T> {
  readonly submitting: boolean;
  readonly error: string | null;
  /** The last successful result, for the form's own confirmation line. */
  readonly result: T | null;
  /** Runs `perform` once; resolves to its value, or `null` if it failed. */
  readonly run: (perform: () => Promise<T>) => Promise<T | null>;
  readonly fail: (message: string) => void;
  readonly reset: () => void;
}

/**
 * One write, admitted once.
 *
 * The re-entrancy guard is a ref, not the `submitting` state: two submits
 * dispatched in the same tick both read `submitting === false` out of the
 * render that captured them, so the state check let the second through and
 * the form posted twice.
 */
export function useSubmitAction<T>(): SubmitAction<T> {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<T | null>(null);
  const running = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(async (perform: () => Promise<T>): Promise<T | null> => {
    if (running.current) return null;
    running.current = true;
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const value = await perform();
      if (mounted.current) setResult(value);
      return value;
    } catch (reason) {
      if (mounted.current) setError(failureText(reason));
      return null;
    } finally {
      running.current = false;
      if (mounted.current) setSubmitting(false);
    }
  }, []);

  const fail = useCallback((message: string) => {
    setResult(null);
    setError(message);
  }, []);

  const reset = useCallback(() => {
    setError(null);
    setResult(null);
  }, []);

  return { submitting, error, result, run, fail, reset };
}

/** A write that failed, in the server's own words. */
export function FormError({ detail }: { detail: string }) {
  return (
    <p className="formfeedback formfeedback--err" role="alert">
      {detail}
    </p>
  );
}

/** A write that succeeded, naming what it created. */
export function FormSuccess({ children }: { children: ReactNode }) {
  return (
    <p className="formfeedback formfeedback--ok" role="status">
      {children}
    </p>
  );
}

export type StatusKind = "info" | "success" | "error";

export interface StatusMessage {
  readonly text: string;
  readonly kind: StatusKind;
}

export interface StatusChannel {
  readonly status: StatusMessage | null;
  readonly info: (text: string) => void;
  readonly success: (text: string) => void;
  readonly failure: (reason: unknown) => void;
  readonly clear: () => void;
}

/**
 * The single message strip a registry screen reports every action through.
 *
 * Deliberately persistent, unlike `useToast`: these messages are the outcome
 * of a governance decision ("deprecation review requested"), and a steward
 * must still be able to read one after coming back to the tab.
 */
export function useStatusChannel(): StatusChannel {
  const [status, setStatus] = useState<StatusMessage | null>(null);
  const info = useCallback((text: string) => setStatus({ text, kind: "info" }), []);
  const success = useCallback((text: string) => setStatus({ text, kind: "success" }), []);
  const failure = useCallback(
    (reason: unknown) => setStatus({ text: failureText(reason), kind: "error" }),
    [],
  );
  const clear = useCallback(() => setStatus(null), []);
  return { status, info, success, failure, clear };
}

export function StatusStrip({ status }: { status: StatusMessage | null }) {
  if (!status) return null;
  return (
    <div className={`statusstrip statusstrip--${status.kind}`} role="status">
      {status.text}
    </div>
  );
}

/** The "this list is being fetched" placeholder, shared by every registry. */
export function LoadingPanel({ label }: { label: string }) {
  return (
    <div className="loadingpanel" role="status" aria-live="polite">
      {label}
    </div>
  );
}

export interface VersionLifecycle {
  /** The version a lifecycle request is currently in flight for. */
  readonly busyVersionId: string | null;
  readonly run: (
    versionId: string,
    step: {
      /** Present tense, for the strip while the request is in flight. */
      readonly pending: string;
      /** Past tense, for the strip once the reviewer has been asked. */
      readonly done: string;
      readonly action: (versionId: string) => Promise<unknown>;
    },
  ) => Promise<void>;
}

/**
 * Submit for review, or request deprecation.
 *
 * Both are the same operation from the screen's side: ask an independent
 * reviewer, then re-read the registry, because the version's status changed
 * on the server and the row on screen now describes a state that no longer
 * exists. `ContextProductsScreen` and `ToolRegistryScreen` each had their own
 * `runVersionAction` doing exactly this; the drift between them was that one
 * reloaded and one did not clear its busy id on an early return.
 */
export function useVersionLifecycle(channel: StatusChannel, reload: () => void): VersionLifecycle {
  const [busyVersionId, setBusyVersionId] = useState<string | null>(null);
  const reloadRef = useRef(reload);
  useEffect(() => {
    reloadRef.current = reload;
  }, [reload]);

  const run = useCallback(
    async (
      versionId: string,
      step: { pending: string; done: string; action: (versionId: string) => Promise<unknown> },
    ) => {
      setBusyVersionId(versionId);
      channel.info(`${step.pending}…`);
      try {
        await step.action(versionId);
        channel.success(step.done);
        reloadRef.current();
      } catch (reason) {
        channel.failure(reason);
      } finally {
        setBusyVersionId(null);
      }
    },
    [channel],
  );

  return { busyVersionId, run };
}

/**
 * A comma-separated list of identifiers, as the API wants it.
 *
 * One implementation: `ContextProductsScreen` and `ToolRegistryScreen` each
 * carried a byte-identical `splitIds`, and both feed the same kind of
 * `allowed_*` array on a governed version.
 */
export const splitList = (value: string): string[] =>
  value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
