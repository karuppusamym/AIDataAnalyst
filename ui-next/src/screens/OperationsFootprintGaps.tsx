import { useEffect, useState } from "react";
import type { FootprintGapsRead } from "../lib/types";
import { ApiError } from "../lib/api";
import { fetchFootprintGaps } from "../lib/api/footprintGaps";
import { ErrorState } from "../components/primitives";

/* ---------------------------------------------------------------------------
   Knowledge gaps (R11-FP05/FP17) on the Operations screen.

   One table per source with anything outstanding: what kind of gap, how
   many, the one route that closes it and who acts. The server leaves out a
   source the caller may not read -- it is not shown as "0", because a zero
   would still say the source exists and was looked at.
--------------------------------------------------------------------------- */

const KIND_WORDS: Record<string, string> = {
  CODE_WITHHELD: "Code withheld by the source",
  CODE_TRUNCATED: "Code truncated by the source",
  CODE_QUARANTINED: "Code quarantined by screening",
  LINEAGE_AWAITING_PARSE: "Lineage waiting to be parsed",
  LINEAGE_UNPARSED_STATEMENTS: "Statements lineage cannot read",
  LINEAGE_AWAITING_REVIEW: "Lineage waiting for review",
  SOURCE_OBJECTS_INVISIBLE: "Objects this login may not see",
  SOURCE_CHANGE_HOLDS: "Tables held after a source change",
  CHANGE_SIGNALS_PENDING: "Source changes not yet processed",
};

const RESOLUTION_WORDS: Record<string, string> = {
  AGENT: "task agent",
  HUMAN_REVIEW: "review queue",
  SOURCE_ACCESS: "source access",
  OPERATIONS: "operations",
  EXPLAINED: "explained, not retried",
};

export function FootprintGaps({ organizationId }: { organizationId: string }) {
  const [data, setData] = useState<FootprintGapsRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const ac = new AbortController();
    setError(null);
    fetchFootprintGaps(organizationId, ac.signal)
      .then(setData)
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      });
    return () => ac.abort();
  }, [organizationId, attempt]);

  const withGaps = data?.datasources.filter((source) => source.gaps.length > 0) ?? [];

  return (
    <section className="ops__sec" aria-labelledby="footprint-gaps-heading">
      <div className="ops__sechead">
        <h2 className="ops__h2" id="footprint-gaps-heading">
          Knowledge gaps
        </h2>
      </div>
      <p className="ops__sub">
        What Atlas does not yet know about each source you can read, why, and who can close it.
        Sources you cannot read are left out, not counted.
      </p>
      {error ? (
        <ErrorState
          title="Knowledge gaps could not be loaded"
          detail={error}
          onRetry={() => setAttempt((value) => value + 1)}
        />
      ) : !data ? (
        <div role="status" aria-live="polite">
          Loading knowledge gaps…
        </div>
      ) : withGaps.length === 0 ? (
        <p className="ops__sub">No recorded gaps in the sources you can read.</p>
      ) : (
        withGaps.map((source) => (
          <article key={source.datasource_id} aria-label={`Gaps in ${source.datasource_name}`}>
            <h3 className="ops__h3">{source.datasource_name}</h3>
            {source.oldest_pending_signal_minutes != null ? (
              <p className="ops__sub">
                Oldest unprocessed source change has waited {source.oldest_pending_signal_minutes} min.
              </p>
            ) : null}
            <div className="ops__tablewrap">
              <table className="ops__table">
                <thead>
                  <tr>
                    <th scope="col">Gap</th>
                    <th scope="col">Count</th>
                    <th scope="col">Closed by</th>
                    <th scope="col">Who acts</th>
                    <th scope="col">Why</th>
                  </tr>
                </thead>
                <tbody>
                  {source.gaps.map((gap) => (
                    <tr key={gap.kind}>
                      <th scope="row">{KIND_WORDS[gap.kind] ?? gap.kind}</th>
                      <td className="tnum">{gap.count}</td>
                      <td>{RESOLUTION_WORDS[gap.resolution] ?? gap.resolution}</td>
                      <td>{gap.owner}</td>
                      <td>{gap.explanation}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </article>
        ))
      )}
    </section>
  );
}
