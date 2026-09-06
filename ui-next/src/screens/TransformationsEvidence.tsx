import type { DbtResourceRead } from "../lib/api";
import { CopyLinkButton, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/EvidencePane.css";

/* ---------------------------------------------------------------------------
   One dbt resource, as a row and as evidence.

   These two views ship together because they must agree. The row's badges and
   the detail pane's evidence list are the same three judgements -- did its
   tests pass, is it mapped to a catalogued relation, what kind of node is it
   -- rendered at two levels of detail, and they read the same `testTone`/
   `matchTone` to decide. Split apart, a resource could be amber in the list
   and green in the pane, and neither would be wrong on its own terms.

   What the pane shows is evidence, not data: column names and their physical
   types, the catalog mapping, the source file, and compiled SQL with every
   literal redacted by the importer. The fingerprint is retained even when the
   SQL is not, so two imports can be compared without either being readable.
--------------------------------------------------------------------------- */

export const testTone = (status: string | null): Tone =>
  status === "PASS"
    ? "ok"
    : status === "FAIL" || status === "ERROR"
      ? "bad"
      : status === "SKIPPED"
        ? "mute"
        : "warn";

export const matchTone = (matched: boolean): Tone => (matched ? "ok" : "warn");

export function ResourceRow({
  resource,
  selected,
  onSelect,
}: {
  resource: DbtResourceRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className={`txres${selected ? " txres--sel" : ""}`} aria-label={resource.name}>
      <button className="txres__click" onClick={onSelect}>
        <div className="txres__head">
          <span className="txres__name" title={resource.name}>
            {resource.name}
          </span>
          <div className="txres__badges">
            <Pill tone="mute">{resource.resource_type.toLowerCase()}</Pill>
            {resource.test_status ? (
              <Pill tone={testTone(resource.test_status)}>
                {resource.test_status.toLowerCase()}
                {resource.test_failures ? ` (${resource.test_failures})` : ""}
              </Pill>
            ) : null}
          </div>
        </div>
        <div className="txres__meta">
          <span>{resource.package_name}</span>
          <span>&middot;</span>
          <span className="txres__uid">{resource.unique_id}</span>
        </div>
        <div className="txres__meta">
          <span>{resource.materialization ?? "not applicable"}</span>
          <span>&middot;</span>
          <Pill tone={matchTone(Boolean(resource.matched_table_id))}>
            {resource.matched_table_id ? "matched" : "unmatched"}
          </Pill>
          <span>&middot;</span>
          <span>{resource.column_names.length} columns</span>
        </div>
      </button>
    </article>
  );
}

export function ResourceDetailPane({
  resource,
  onClose,
  context,
}: {
  resource: DbtResourceRead;
  onClose: () => void;
  /** The project and dbt project the resource id is only meaningful inside.
   *  Passed explicitly rather than scraped off `location.search`, which also
   *  swept up this screen's type/match filters and shipped them to whoever
   *  the link was sent to. */
  context: { project: string | null; dbtProject: string | null };
}) {
  return (
    <aside className="evp" aria-label={`Detail for ${resource.name}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={resource.name}>
            {resource.name}
          </div>
          <div className="evp__path">
            {resource.resource_type.toLowerCase()} &middot; {resource.unique_id}
          </div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close resource detail">
          &times;
        </button>
      </header>
      <div className="evp__body">
        {resource.test_status ? (
          <div className={`txtestbanner txtestbanner--${testTone(resource.test_status)}`} role="status">
            <strong>Test execution: {resource.test_status.toLowerCase()}</strong>
            <p>
              {resource.test_failures !== null && resource.test_failures !== undefined
                ? `${resource.test_failures} failing row${resource.test_failures === 1 ? "" : "s"} observed.`
                : "Assertion executed with no recorded failure count."}
              {resource.test_execution_time !== null && resource.test_execution_time !== undefined
                ? ` Execution time ${resource.test_execution_time.toFixed(2)}s.`
                : ""}
            </p>
          </div>
        ) : null}

        <ol className="evl">
          <li className="evi evi--info">
            <div className="evi__label">Relation</div>
            <div className="evi__value">{resource.relation_name ?? "Not a warehouse relation"}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Materialization</div>
            <div className="evi__value">{resource.materialization ?? "Not applicable"}</div>
          </li>
          <li className={`evi evi--${resource.matched_table_id ? "ok" : "warn"}`}>
            <div className="evi__label">Catalog mapping</div>
            <div className="evi__value">{resource.matched_table_id ?? "Unmatched"}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Source file</div>
            <div className="evi__value">{resource.original_file_path ?? "Not recorded"}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Tags</div>
            <div className="evi__value">{resource.tags.join(", ") || "None"}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">SQL fingerprint</div>
            <div className="evi__value">{resource.compiled_sql_hash ?? "No compiled SQL"}</div>
          </li>
        </ol>

        {resource.column_names.length > 0 ? (
          <>
            <div className="evp__sub" style={{ marginTop: 14 }}>
              Columns &amp; physical schema types
            </div>
            <div className="txcolwrap">
              <table className="txcoltable">
                <thead>
                  <tr>
                    <th>Column</th>
                    <th>Physical type</th>
                    <th>Documentation</th>
                  </tr>
                </thead>
                <tbody>
                  {resource.column_names.map((col) => (
                    <tr key={col}>
                      <td>
                        <strong>{col}</strong>
                      </td>
                      <td>
                        <code>{resource.column_types[col] ?? "Not resolved"}</code>
                      </td>
                      <td>{resource.column_descriptions[col] ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}

        {Object.keys(resource.extra_metadata).length > 0 ? (
          <>
            <div className="evp__sub" style={{ marginTop: 14 }}>
              Downstream &amp; exposure metadata
            </div>
            <ol className="evl">
              {Object.entries(resource.extra_metadata).map(([k, v]) => (
                <li key={k} className="evi evi--info">
                  <div className="evi__label">{k.replace(/_/g, " ")}</div>
                  <div className="evi__value">{String(v)}</div>
                </li>
              ))}
            </ol>
          </>
        ) : null}

        <div className="evp__sub" style={{ marginTop: 14 }}>
          Literal-redacted compiled SQL
        </div>
        {resource.compiled_sql_redacted ? (
          <pre className="txsql">{resource.compiled_sql_redacted}</pre>
        ) : (
          <p className="txnone">
            Compiled SQL was not present or could not be safely normalized; only its fingerprint is retained.
          </p>
        )}
      </div>
      <footer className="evp__foot">
        {/* The copied link names the screen that resolves this selection.
            Built as `origin + pathname + '?' + id` it carried no `#/transformations`,
            so a fresh tab landed on the persona default and the id was read by
            nobody (review 2026-09-05, F08). */}
        <CopyLinkButton
          target={{
            screen: "transformations",
            params: {
              project: context.project,
              dbtProject: context.dbtProject,
              resource: resource.id,
            },
          }}
          label="Copy resource link"
        />
        <span className="evp__hint">Evidence, not source values &mdash; literals are redacted</span>
      </footer>
    </aside>
  );
}
