import type { CoverageSnapshotRead } from "../lib/api";
import { dimensionLabel, orderedDimensionKeys } from "../lib/coverageDimensions";
import "./StewardshipCoverage.css";

/* ---------------------------------------------------------------------------
   The stored coverage history (R11-AUD08) -- what `GET .../coverage/snapshots`
   returned, as a table and, when there are at least two rows, one small trend.

   THE TABLE IS THE RECORD; THE TREND IS A SUMMARY OF ONE COLUMN OF IT. Every
   snapshot is a row with its six percentages exactly as the server stored them,
   so a reader who cannot see the line, or does not trust it, has all of the
   data in a form a screen reader walks cell by cell. The line only ever draws
   `overall_score`.

   WHAT THE LINE DOES TO STAY HONEST:

     * A FIXED 0-100 SCALE. `overall_score` is a mean of percentages, so its
       whole range is 0 to 100, and that is the axis whatever the data does. A
       line scaled to fit its own minimum and maximum turns a move from 9.6 to
       10.1 into a cliff; on the true scale it is the small move it is.
     * ORDER, NOT SPACING. Points are one per stored snapshot, evenly spaced,
       oldest to newest. A gap of a week and a gap of a year look the same, which
       is why the table beside it carries the dates and the line says "one point
       per snapshot" rather than implying a time axis.
     * ONE NAME FOR ITS CONTENT. The graphic is a single `role="img"` whose label
       gives the count and the first and last values; it is not a set of focus
       stops with nothing to say.

   Nothing is derived from the rows but their order: no trend arrow, no "up 3%".
   A difference between two stored figures is one subtraction a reader can make
   from the table, and the API never said it was significant.
--------------------------------------------------------------------------- */

/** `2026-09-19T08:30:12Z` -> `2026-09-19 08:30 UTC`, the form the rest of the console uses. */
export const stamp = (iso: string): string => `${iso.slice(0, 16).replace("T", " ")} UTC`;

/** The API rounds every percentage to two places, so two places never changes a value. */
export const pct = (value: number): string => `${value.toFixed(2)}%`;

const W = 180;
const H = 44;
const PAD = 4;

/** The overall score across the stored snapshots, oldest to newest, on a 0-100 scale. */
function Trend({ newestFirst }: { newestFirst: readonly CoverageSnapshotRead[] }) {
  const points = [...newestFirst].reverse();
  const first = points[0]!;
  const last = points[points.length - 1]!;
  const step = (W - 2 * PAD) / (points.length - 1);
  const y = (score: number) => H - PAD - (Math.min(100, Math.max(0, score)) / 100) * (H - 2 * PAD);
  const path = points.map((snapshot, index) => `${PAD + index * step},${y(snapshot.overall_score)}`).join(" ");
  return (
    <figure className="stewcov__trend">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width={W}
        height={H}
        role="img"
        aria-label={`Overall coverage across ${points.length} snapshots, oldest to newest: ${pct(first.overall_score)} to ${pct(last.overall_score)}`}
      >
        <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} className="stewcov__trendbase" />
        <polyline points={path} className="stewcov__trendline" />
        {points.map((snapshot, index) => (
          <circle key={snapshot.id} cx={PAD + index * step} cy={y(snapshot.overall_score)} r={2.5} className="stewcov__trenddot" />
        ))}
      </svg>
      <figcaption>Overall score, one point per snapshot, oldest to newest, on a 0 to 100 scale.</figcaption>
    </figure>
  );
}

export function CoverageSnapshots({
  snapshots,
  total,
}: {
  /** Newest first, as the API returns them. */
  snapshots: readonly CoverageSnapshotRead[];
  /** Every snapshot stored for the scope, which may be more than `snapshots`. */
  total: number;
}) {
  const keys = orderedDimensionKeys([
    ...new Set(snapshots.flatMap((snapshot) => Object.keys(snapshot.dimensions))),
  ]);
  return (
    <div className="stewcov__history">
      {snapshots.length >= 2 ? <Trend newestFirst={snapshots} /> : null}
      <div className="stewcov__scroll" role="region" aria-label="Snapshot history table" tabIndex={0}>
        <table className="stewcov__table">
          <caption className="sr-only">
            Stored coverage snapshots, newest first. Each row is what the server computed when the snapshot was taken.
          </caption>
          <thead>
            <tr>
              <th scope="col">Taken</th>
              <th scope="col">Taken by</th>
              <th scope="col" className="stewcov__num">Tables</th>
              <th scope="col" className="stewcov__num">Overall</th>
              {keys.map((key) => (
                <th key={key} scope="col" className="stewcov__num">{dimensionLabel(key)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {snapshots.map((snapshot) => (
              <tr key={snapshot.id}>
                <th scope="row">
                  <time dateTime={snapshot.created_at}>{stamp(snapshot.created_at)}</time>
                </th>
                <td>{snapshot.computed_by}</td>
                <td className="stewcov__num">{snapshot.table_count}</td>
                <td className="stewcov__num">{pct(snapshot.overall_score)}</td>
                {keys.map((key) => {
                  const dimension = snapshot.dimensions[key];
                  return (
                    <td key={key} className="stewcov__num">
                      {dimension ? (
                        <>
                          {pct(dimension.percentage)}
                          <span className="sr-only">
                            {" "}({dimension.covered} of {dimension.total} tables)
                          </span>
                        </>
                      ) : (
                        <>
                          <span aria-hidden="true">—</span>
                          <span className="sr-only">not recorded</span>
                        </>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {total > snapshots.length ? (
        <p className="stew__note">
          Showing the {snapshots.length} most recent of {total} stored snapshots.
        </p>
      ) : null}
    </div>
  );
}
