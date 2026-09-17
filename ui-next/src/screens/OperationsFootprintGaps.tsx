import { useEffect, useState } from "react";
import type { FootprintGapDetailRead, FootprintGapsRead } from "../lib/types";
import { ApiError } from "../lib/api";
import { fetchFootprintGapObjects, fetchFootprintGaps } from "../lib/api/footprintGaps";
import { DefinitionHistoryPanel } from "../components/DefinitionHistoryPanel";
import { ErrorState } from "../components/primitives";

/* ---------------------------------------------------------------------------
   Knowledge gaps (R11-FP05/FP17) on the Operations screen.

   One table per source with anything outstanding: what kind of gap, how
   many, the one route that closes it and who acts. The server leaves out a
   source the caller may not read -- it is not shown as "0", because a zero
   would still say the source exists and was looked at.

   A count expands (R11-FP05) into the objects behind it, fetched when asked
   for rather than with the summary: the list is what a steward acts on, and
   most rows are read without ever needing it. The gap whose objects Atlas
   never saw expands to the server's sentence saying why there is nothing to
   name, rather than to an empty list that would read as "none".
--------------------------------------------------------------------------- */

const KIND_WORDS: Record<string, string> = {
  CODE_WITHHELD: "Code withheld by the source",
  CODE_TRUNCATED: "Code truncated by the source",
  CODE_QUARANTINED: "Code quarantined by screening",
  LINEAGE_AWAITING_PARSE: "Lineage waiting to be parsed",
  LINEAGE_UNPARSED_STATEMENTS: "Statements lineage cannot read",
  LINEAGE_AWAITING_REVIEW: "Lineage waiting for review",
  SOURCE_OBJECTS_INVISIBLE: "Objects this login may not see",
  // R11-FP02: counted per facet, not per object -- a refused read returns nothing, so
  // there is no object behind the number. The row's own note says so when it is expanded;
  // the label has to at least not read as a code.
  SOURCE_READS_REFUSED: "Reads refused by the source",
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

function GapRow({
  datasourceId,
  kind,
  count,
  resolution,
  owner,
  explanation,
}: {
  datasourceId: string;
  kind: string;
  count: number;
  resolution: string;
  owner: string;
  explanation: string;
}) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<FootprintGapDetailRead | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || detail) return;
    const ac = new AbortController();
    setError(null);
    fetchFootprintGapObjects(datasourceId, kind, ac.signal)
      .then(setDetail)
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      });
    return () => ac.abort();
  }, [open, detail, datasourceId, kind]);

  const label = KIND_WORDS[kind] ?? kind;
  return (
    <>
      <tr>
        <th scope="row">{label}</th>
        <td className="tnum">
          <button
            type="button"
            className="ops__linkbtn"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
          >
            {count}
          </button>
        </td>
        <td>{RESOLUTION_WORDS[resolution] ?? resolution}</td>
        <td>{owner}</td>
        <td>{explanation}</td>
      </tr>
      {open ? (
        <tr>
          <td colSpan={5}>
            {error ? (
              <span role="alert">These objects could not be loaded: {error}</span>
            ) : !detail ? (
              <span role="status">Loading the objects behind {label.toLowerCase()}…</span>
            ) : detail.note ? (
              <p className="ops__sub">{detail.note}</p>
            ) : detail.objects.length === 0 ? (
              <p className="ops__sub">Nothing left to show: these were closed since the count.</p>
            ) : (
              <>
                <ul className="ops__list">
                  {detail.objects.map((object) => (
                    <li key={`${object.object_type}:${object.object_id}`}>
                      <span className="ops__kind">{object.object_type}</span>{" "}
                      {object.qualified_name}
                      {object.detail ? <span className="ops__sub"> — {object.detail}</span> : null}
                      {/* R11-FP03: a steward who has just been told that this
                          procedure's body is withheld, truncated, quarantined
                          or unparsed asks the same next question every time --
                          "then what changed in it, and when?" -- and the
                          answer had no screen. It does not need the body to be
                          readable: the change class, the digests and the
                          derived table footprint are all value-free, which is
                          exactly why this belongs on the row that says the body
                          is not. Offered only for a ROUTINE, because
                          `metadata_routine_definition_version` is a routine's
                          history; a view's definition has no equivalent table
                          (see `RoutineDocumentationVersion`'s note on why). */}
                      {object.object_type === "ROUTINE" ? (
                        <DefinitionHistoryPanel
                          routineId={object.object_id}
                          qualifiedName={object.qualified_name}
                        />
                      ) : null}
                    </li>
                  ))}
                </ul>
                {detail.truncated ? (
                  <p className="ops__sub">
                    The first {detail.objects.length} of more than that. A source with this many is
                    closed by a grant or a narrower discovery selection, not one object at a time.
                  </p>
                ) : null}
              </>
            )}
          </td>
        </tr>
      ) : null}
    </>
  );
}

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
                    <GapRow
                      key={gap.kind}
                      datasourceId={source.datasource_id}
                      kind={gap.kind}
                      count={gap.count}
                      resolution={gap.resolution}
                      owner={gap.owner}
                      explanation={gap.explanation}
                    />
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
