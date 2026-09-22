import { useEffect, useState } from "react";
import type { SchedulerPassStatusListRead, SchedulerPassStatusRead } from "../lib/types";
import { ApiError } from "../lib/api";
import { fetchSchedulerPasses } from "../lib/api/schedulerPasses";
import { readDecision } from "../lib/roles";
import { useSession } from "../lib/session";
import { ErrorState, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";

/* ---------------------------------------------------------------------------
   Scheduler passes (R11-VAL04) on the Operations screen.

   Round 12 stopped one failing maintenance pass from ending the scheduler: the
   pass is logged and counted and the others carry on. Both of those live in
   the scheduler process, so a pass that failed on every iteration was still
   invisible here. The leading replica now persists each pass's outcome, and
   this reads it back: failing and stale passes are listed, the healthy ones
   are counted.

   STALE means nothing has attempted the pass for longer than the server's
   bound -- the scheduler is down, every replica is standing by, or it is stuck
   in an earlier pass -- so an old "OK" is not shown as health. Only the
   exception's class is shown; its message is kept to the logs on purpose.
--------------------------------------------------------------------------- */

/** `GET /v1/operations/scheduler-passes`, from `Docs/50-security/surface-control-matrix.md`. */
const SCHEDULER_PASS_READ_ROLES = ["Operations", "PlatformAdmin"] as const;

const STATE_WORDS: Record<SchedulerPassStatusRead["state"], string> = {
  FAILING: "failing",
  STALE: "not attempted lately",
  NEVER_RUN: "never run",
  OK: "ok",
};

const STATE_TONE: Record<SchedulerPassStatusRead["state"], Tone> = {
  FAILING: "bad",
  STALE: "warn",
  NEVER_RUN: "mute",
  OK: "ok",
};

function when(iso: string | null): string {
  return iso ? iso.slice(0, 19).replace("T", " ") : "—";
}

export function SchedulerPasses() {
  const session = useSession();
  const decision = readDecision(session, SCHEDULER_PASS_READ_ROLES);
  const [data, setData] = useState<SchedulerPassStatusListRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (decision !== "ask") return;
    const ac = new AbortController();
    setError(null);
    fetchSchedulerPasses(ac.signal)
      .then(setData)
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      });
    return () => ac.abort();
  }, [decision, attempt]);

  if (decision === "skip") return null;

  const attention = (data?.items ?? []).filter((item) => item.state !== "OK");
  const healthy = (data?.items.length ?? 0) - attention.length;

  return (
    <section className="ops__sec ops__sec--small" aria-label="Scheduler passes">
      <div className="ops__sechead">
        <h2 className="ops__h2">
          Scheduler passes
          {data && data.failing > 0 ? <Pill tone="bad">{data.failing} failing</Pill> : null}
          {data && data.stale > 0 ? <Pill tone="warn">{data.stale} not attempted lately</Pill> : null}
        </h2>
      </div>
      {error ? (
        <ErrorState
          title="Scheduler passes could not be loaded"
          detail={error}
          onRetry={() => setAttempt((value) => value + 1)}
        />
      ) : !data ? (
        <div className="ops__skeleton" role="status" aria-live="polite">
          Loading scheduler passes…
        </div>
      ) : data.items.length === 0 ? (
        <p className="ops__gap">No scheduler pass has reported yet.</p>
      ) : (
        <>
          <p className="ops__gap">
            {attention.length === 0
              ? `All ${healthy} passes ran cleanly on their last attempt.`
              : `${healthy} of ${data.items.length} passes ran cleanly on their last attempt.`}{" "}
            A pass with no attempt for {Math.round(data.stale_after_seconds / 60)} minutes counts as
            not attempted lately.
          </p>
          {attention.length > 0 ? (
            <table className="ops__table">
              <thead>
                <tr>
                  <th scope="col">Pass</th>
                  <th scope="col">State</th>
                  <th scope="col">Failures in a row</th>
                  <th scope="col">Last failure</th>
                  <th scope="col">Last success</th>
                  <th scope="col">Last attempt</th>
                </tr>
              </thead>
              <tbody>
                {attention.map((item) => (
                  <tr key={item.pass_name}>
                    <th scope="row">
                      <code>{item.pass_name}</code>
                    </th>
                    <td>
                      <Pill tone={STATE_TONE[item.state]}>{STATE_WORDS[item.state]}</Pill>
                    </td>
                    <td className="tnum">{item.consecutive_failures}</td>
                    <td>
                      {when(item.last_failure_at)}
                      {item.last_error_class ? (
                        <span className="ops__sub"> — {item.last_error_class}</span>
                      ) : null}
                    </td>
                    <td>{when(item.last_success_at)}</td>
                    <td>{when(item.last_attempt_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </>
      )}
    </section>
  );
}
