import { useMemo, useState } from "react";

import type { QueryExecutionResponse } from "../lib/types";
import { Pill } from "./primitives";
import "./QueryResultTable.css";

/* ---------------------------------------------------------------------------
   The result of a governed query (review 2026-09-05, F20 · T14).

   THE DEFECT this exists to remove: Ask Atlas rendered the row COUNT, the
   elapsed time, the tables touched, the SQL, the explanation and the
   evidence -- everything about the answer except the answer. The rows were
   sitting in `QueryExecutionResponse.rows`, already masked by the gateway,
   and the screen threw them away. A user asking "what were sales last
   quarter" was told that three rows exist.

   THE INVARIANT: what is shown here is exactly what the gateway returned for
   THIS execution, under the policy version named beside it, and it is not
   kept. `query_gateway.py` masks (`***MASKED***`) or tokenizes sensitive
   columns before the response is built, so this component never has an
   unmasked value to leak -- and it must never become a place that stores
   one. History deliberately holds no rows: reopening a past run re-reads the
   run record, which has none, and says so. Saving an analysis
   (`SaveAnalysisTool`) saves a value-free DEFINITION -- SQL with literals
   redacted, parameters declared -- never a result set. Rerunning it executes
   again under whatever policy is current then.

   WHAT THE RESPONSE DOES AND DOES NOT CARRY. It carries the rows, the row
   count, the elapsed time, the masked column names and `column_lineage`, so
   every column can say where it came from and whether it was derived. It
   carries no per-column unit or display format -- those live on the
   published semantic model, named above this panel as the version that
   grounded the answer -- and no explicit applied row limit, so truncation is
   inferred from the LIMIT in the executed SQL rather than asserted. Both
   gaps are stated on screen instead of being papered over with a plausible
   number.
--------------------------------------------------------------------------- */

/** Rows put into the DOM at once. A governed query can legitimately return
 *  thousands; a table that renders all of them freezes the tab, and nobody
 *  reads row 4,000 in an answer panel. The rest stay in the response object
 *  and the panel says how many are not shown. */
const MAX_DISPLAY_ROWS = 200;

/** What `query_gateway.py` substitutes for a redacted value. */
const MASK_SENTINEL = "***MASKED***";

interface ColumnMeta {
  readonly name: string;
  readonly masked: boolean;
  readonly derived: boolean;
  /** `table.column` sources from `column_lineage`, for the header tooltip. */
  readonly sources: readonly string[];
  readonly transformations: readonly string[];
  readonly numeric: boolean;
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/**
 * The LIMIT the executed SQL actually carried.
 *
 * The gateway applies a default/hard row limit (`row_limit_finding`) by
 * rewriting the statement, and records the applied limit in its audit
 * details -- not in the response. Reading it back off `normalized_sql` is the
 * only evidence the browser has, so it is used only to *ask* whether the
 * result stopped at the limit, never to claim a total.
 */
function limitFromSql(sql: string): number | null {
  const match = /\blimit\s+(\d+)\b/i.exec(sql);
  if (!match?.[1]) return null;
  const value = Number.parseInt(match[1], 10);
  return Number.isFinite(value) ? value : null;
}

function describeColumns(execution: QueryExecutionResponse): ColumnMeta[] {
  const first = execution.rows[0];
  if (!first) return [];

  // Masked columns arrive as bare column names (`query_gateway.py` keys them
  // off the row dict), but a caller may qualify them; match on the leaf so
  // both shapes mark the same column.
  const masked = new Set(
    execution.masked_columns.map((name) => (name.split(".").pop() ?? name).toLowerCase()),
  );

  const lineage = new Map<string, { derived: boolean; sources: string[]; transformations: string[] }>();
  for (const entry of execution.column_lineage) {
    const output = text(entry["output_column"]);
    if (!output) continue;
    const rawSources = Array.isArray(entry["source_columns"]) ? entry["source_columns"] : [];
    const sources: string[] = [];
    for (const source of rawSources) {
      if (!source || typeof source !== "object") continue;
      const record = source as Record<string, unknown>;
      const column = text(record["column"]);
      if (!column) continue;
      const table = text(record["table"]);
      sources.push(table ? `${table}.${column}` : column);
    }
    const rawTransformations = Array.isArray(entry["transformations"])
      ? entry["transformations"]
      : [];
    lineage.set(output.toLowerCase(), {
      derived: text(entry["lineage_type"]) === "DERIVED",
      sources,
      transformations: rawTransformations.filter((t): t is string => typeof t === "string"),
    });
  }

  return Object.keys(first).map((name) => {
    const info = lineage.get(name.toLowerCase());
    const isMasked = masked.has(name.toLowerCase());
    return {
      name,
      masked: isMasked,
      derived: info?.derived ?? false,
      sources: info?.sources ?? [],
      transformations: info?.transformations ?? [],
      // Alignment follows the data, not the column name: a numeric column is
      // right-aligned so digits line up and magnitudes are comparable.
      numeric:
        !isMasked &&
        execution.rows.slice(0, 25).some((row) => typeof row[name] === "number"),
    };
  });
}

const NUMBER = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });

function Cell({ value, masked }: { value: unknown; masked: boolean }) {
  if (masked || value === MASK_SENTINEL) {
    return (
      <span className="qrt__masked" title="Redacted by data policy before it left the server">
        masked
      </span>
    );
  }
  if (value === null || value === undefined) return <span className="qrt__null">null</span>;
  if (typeof value === "number") return <>{NUMBER.format(value)}</>;
  if (typeof value === "boolean") return <>{value ? "true" : "false"}</>;
  if (typeof value === "string") return <>{value}</>;
  return <code className="qrt__json">{JSON.stringify(value)}</code>;
}

export function QueryResultTable({
  execution,
  semanticVersion,
  policyVersion,
  executedAt,
}: {
  execution: QueryExecutionResponse;
  /** The published semantic model version that defines these columns. */
  semanticVersion: string | null;
  policyVersion: string | null;
  /** When this execution ran. The freshness of the ANSWER, which is not the
   *  same claim as the freshness of the underlying tables. */
  executedAt: Date;
}) {
  const [expanded, setExpanded] = useState(false);
  const columns = useMemo(() => describeColumns(execution), [execution]);

  const limit = limitFromSql(execution.normalized_sql);
  const truncatedByPolicy = limit !== null && execution.row_count >= limit;
  const shown = expanded
    ? execution.rows
    : execution.rows.slice(0, MAX_DISPLAY_ROWS);
  const hiddenForDisplay = execution.rows.length - shown.length;

  if (execution.rows.length === 0) {
    return (
      <section className="qrt" aria-label="Result">
        <div className="qrt__head">
          <span className="qrt__title">Result</span>
          <Pill tone="mute">no rows</Pill>
        </div>
        <p className="qrt__note">
          The query ran successfully against {execution.referenced_tables.join(", ") || "the source"}{" "}
          and matched no rows. That is an answer, not a failure — the filters in the generated
          query are shown under “Executed query”.
        </p>
      </section>
    );
  }

  return (
    <section className="qrt" aria-label="Result">
      <div className="qrt__head">
        <span className="qrt__title">Result</span>
        <Pill tone="info">
          {execution.row_count} {execution.row_count === 1 ? "row" : "rows"}
        </Pill>
        {execution.masked_columns.length > 0 ? (
          <Pill tone="warn">{execution.masked_columns.length} masked</Pill>
        ) : null}
        {truncatedByPolicy ? <Pill tone="warn">row limit reached</Pill> : null}
      </div>

      <p className="qrt__meta">
        Ran {executedAt.toLocaleTimeString()} in {execution.elapsed_ms} ms
        {policyVersion ? ` · policy ${policyVersion}` : ""}
        {semanticVersion ? ` · semantic model ${semanticVersion}` : " · raw technical metadata"}
      </p>

      {truncatedByPolicy ? (
        <p className="qrt__warn" role="status">
          This result stopped at the governed row limit of {limit}. There may be more matching
          rows — narrow the question or aggregate rather than assuming this is the whole set.
        </p>
      ) : null}

      <div className="qrt__scroll" tabIndex={0} role="group" aria-label="Result rows">
        <table className="qrt__table">
          <caption className="sr-only">
            {execution.row_count} rows returned by the governed query, with masked columns
            redacted before the response left the server.
          </caption>
          <thead>
            <tr>
              {columns.map((column) => (
                <th
                  key={column.name}
                  scope="col"
                  className={column.numeric ? "qrt__num" : undefined}
                  title={
                    column.sources.length > 0
                      ? `${column.derived ? "Derived from" : "From"} ${column.sources.join(", ")}${
                          column.transformations.length > 0
                            ? ` via ${column.transformations.join(", ")}`
                            : ""
                        }`
                      : undefined
                  }
                >
                  <span className="qrt__colname">{column.name}</span>
                  {/* Provenance per column, from `column_lineage`. Colour is
                      not the only carrier: each tag is a word. */}
                  {column.masked ? <span className="qrt__tag qrt__tag--mask">masked</span> : null}
                  {column.derived ? <span className="qrt__tag">derived</span> : null}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, index) => (
              <tr key={index}>
                {columns.map((column) => (
                  <td
                    key={column.name}
                    className={column.numeric ? "qrt__num" : undefined}
                  >
                    <Cell value={row[column.name]} masked={column.masked} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {hiddenForDisplay > 0 ? (
        <p className="qrt__note">
          Showing the first {MAX_DISPLAY_ROWS} of {execution.rows.length} returned rows.{" "}
          <button type="button" className="qrt__more" onClick={() => setExpanded(true)}>
            Show all {execution.rows.length}
          </button>
        </p>
      ) : null}

      <p className="qrt__note">
        These rows are this execution's answer and are not stored: closing or reopening this run
        shows its evidence, not its values. Save the analysis to keep the value-free definition and
        rerun it under the policy in force at that time. Column units and display formats come from
        the semantic model named above — the execution response does not carry them.
      </p>
    </section>
  );
}
