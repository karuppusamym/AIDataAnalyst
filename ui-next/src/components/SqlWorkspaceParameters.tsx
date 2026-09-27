import type { SqlDraftParameter } from "../lib/types";
import { Button } from "./primitives";

/**
 * R11-SQL01: named, typed parameter values for reviewed SQL.
 *
 * The statement names a value as `:name`; the value is typed here and sent beside the text,
 * where the server binds it into the parsed statement as one typed literal -- the governed-tool
 * binding -- so nothing typed here can change what the statement does. The checks below mirror
 * the server's (`aida/sql_workspace.py`) so a person sees the problem before sending; the server
 * decides regardless, and answers with `PARAMETER_*` findings when a value does not bind.
 */

export type SqlParameterType = SqlDraftParameter["parameter_type"];

export const PARAMETER_TYPES: readonly SqlParameterType[] = [
  "STRING",
  "INTEGER",
  "NUMBER",
  "BOOLEAN",
  "DATE",
];

/** The server's own limit on a text value (`PARAMETER_VALUE_MAX_LENGTH`). */
export const PARAMETER_VALUE_MAX_LENGTH = 4000;

const NAME = /^[a-z][a-z0-9_]{0,63}$/;
const INTEGER = /^[+-]?\d+$/;
const NUMBER = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
const DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** One parameter as the person is editing it: the value is still the text they typed. */
export interface ParameterRow {
  key: number;
  name: string;
  parameterType: SqlParameterType;
  value: string;
}

export interface ParameterProblem {
  name?: string;
  value?: string;
}

/**
 * The `:name` placeholders a statement uses, in order, once each. Advisory: string literals,
 * quoted identifiers and comments are skipped and `::` casts are not placeholders, but the
 * server's parser is what decides.
 */
export function placeholdersIn(sql: string): string[] {
  const code = sql
    .replace(/'(?:[^']|'')*'/g, "''")
    .replace(/"(?:[^"]|"")*"/g, '""')
    .replace(/--[^\n]*/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "");
  const found: string[] = [];
  for (const match of code.matchAll(/(^|[^:\w]):([a-z][a-z0-9_]{0,63})(?![a-z0-9_])/g)) {
    const name = match[2] ?? "";
    if (!found.includes(name)) found.push(name);
  }
  return found;
}

function validDate(value: string): boolean {
  const parts = DATE.exec(value);
  if (parts === null) return false;
  const [year, month, day] = [Number(parts[1]), Number(parts[2]), Number(parts[3])];
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
  );
}

function valueProblem(row: ParameterRow): string | undefined {
  const text = row.value.trim();
  switch (row.parameterType) {
    case "STRING":
      return row.value.length > PARAMETER_VALUE_MAX_LENGTH
        ? `At most ${PARAMETER_VALUE_MAX_LENGTH.toLocaleString("en-US")} characters.`
        : undefined;
    case "INTEGER":
      return INTEGER.test(text) && Number.isSafeInteger(Number(text))
        ? undefined
        : "Enter a whole number.";
    case "NUMBER":
      return NUMBER.test(text) && Number.isFinite(Number(text)) ? undefined : "Enter a number.";
    case "BOOLEAN":
      return row.value === "true" || row.value === "false" ? undefined : "Choose true or false.";
    case "DATE":
      return validDate(text) ? undefined : "Enter a date as YYYY-MM-DD.";
  }
}

/** What stops each row being sent, keyed by row. A row with nothing wrong is absent. */
export function parameterProblems(rows: readonly ParameterRow[]): Map<number, ParameterProblem> {
  const problems = new Map<number, ParameterProblem>();
  const seen = new Map<string, number>();
  for (const row of rows) seen.set(row.name, (seen.get(row.name) ?? 0) + 1);
  for (const row of rows) {
    const problem: ParameterProblem = {};
    if (!NAME.test(row.name)) {
      problem.name = "Use lower-case letters, digits and _, starting with a letter.";
    } else if ((seen.get(row.name) ?? 0) > 1) {
      problem.name = "Declared more than once.";
    }
    const value = valueProblem(row);
    if (value !== undefined) problem.value = value;
    if (problem.name !== undefined || problem.value !== undefined) problems.set(row.key, problem);
  }
  return problems;
}

/** The rows as the API takes them, each value in its declared type's JSON form. Only for rows
 *  `parameterProblems` passes: an INTEGER row's text is already known to be a whole number. */
export function toSqlDraftParameters(rows: readonly ParameterRow[]): SqlDraftParameter[] {
  return rows.map((row) => {
    const text = row.value.trim();
    const value: SqlDraftParameter["value"] =
      row.parameterType === "INTEGER" || row.parameterType === "NUMBER"
        ? Number(text)
        : row.parameterType === "BOOLEAN"
          ? row.value === "true"
          : row.parameterType === "DATE"
            ? text
            : row.value;
    return { name: row.name, parameter_type: row.parameterType, value };
  });
}

/** Equal exactly when two sets of parameters bind the same statement the same way. */
export function bindingKey(parameters: readonly SqlDraftParameter[]): string {
  return JSON.stringify(
    [...parameters]
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((parameter) => [parameter.name, parameter.parameter_type, parameter.value]),
  );
}

export function SqlParameterEditor({
  rows,
  problems,
  sql,
  onChange,
  onAdd,
  onRemove,
}: {
  rows: readonly ParameterRow[];
  problems: ReadonlyMap<number, ParameterProblem>;
  sql: string;
  onChange: (key: number, change: Partial<Omit<ParameterRow, "key">>) => void;
  onAdd: (name: string) => void;
  onRemove: (key: number) => void;
}) {
  const used = placeholdersIn(sql);
  const declared = new Set(rows.map((row) => row.name));
  const undeclared = used.filter((name) => !declared.has(name));
  return (
    <fieldset className="sqlws__params">
      <legend className="sqlws__h3">Parameters</legend>
      <p className="sqlws__note">
        Write <code>:name</code> in the SQL and give its value here. The value is sent separately
        and bound as one typed value — never pasted into the statement — and it is not stored.
      </p>
      {rows.length > 0 ? (
        <ul className="sqlws__paramlist">
          {rows.map((row, index) => {
            const problem = problems.get(row.key);
            const label = `parameter ${index + 1}`;
            const nameError = `sqlws-param-${row.key}-name`;
            const valueError = `sqlws-param-${row.key}-value`;
            const unused = NAME.test(row.name) && !used.includes(row.name);
            return (
              <li key={row.key} className="sqlws__param">
                <div className="sqlws__paramrow">
                  <input
                    className="sqlws__paramname"
                    aria-label={`Name of ${label}`}
                    aria-invalid={problem?.name !== undefined}
                    aria-describedby={problem?.name !== undefined ? nameError : undefined}
                    value={row.name}
                    onChange={(e) => onChange(row.key, { name: e.target.value })}
                    placeholder="name"
                    spellCheck={false}
                    autoComplete="off"
                  />
                  <select
                    className="sqlws__paramtype"
                    aria-label={`Type of ${label}`}
                    value={row.parameterType}
                    onChange={(e) =>
                      onChange(row.key, { parameterType: e.target.value as SqlParameterType })
                    }
                  >
                    {PARAMETER_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {type}
                      </option>
                    ))}
                  </select>
                  {row.parameterType === "BOOLEAN" ? (
                    <select
                      className="sqlws__paramvalue"
                      aria-label={`Value of ${label}`}
                      aria-invalid={problem?.value !== undefined}
                      aria-describedby={problem?.value !== undefined ? valueError : undefined}
                      value={row.value}
                      onChange={(e) => onChange(row.key, { value: e.target.value })}
                    >
                      <option value="">Choose…</option>
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                  ) : (
                    <input
                      className="sqlws__paramvalue"
                      aria-label={`Value of ${label}`}
                      aria-invalid={problem?.value !== undefined}
                      aria-describedby={problem?.value !== undefined ? valueError : undefined}
                      value={row.value}
                      onChange={(e) => onChange(row.key, { value: e.target.value })}
                      placeholder={
                        row.parameterType === "DATE"
                          ? "YYYY-MM-DD"
                          : row.parameterType === "STRING"
                            ? "text"
                            : "number"
                      }
                      inputMode={
                        row.parameterType === "INTEGER" || row.parameterType === "NUMBER"
                          ? "decimal"
                          : undefined
                      }
                      spellCheck={false}
                      autoComplete="off"
                    />
                  )}
                  <Button onClick={() => onRemove(row.key)} title={`Remove ${label}`}>
                    Remove
                  </Button>
                </div>
                {problem?.name !== undefined ? (
                  <div id={nameError} className="sqlws__paramproblem">
                    {problem.name}
                  </div>
                ) : null}
                {problem?.value !== undefined ? (
                  <div id={valueError} className="sqlws__paramproblem">
                    {problem.value}
                  </div>
                ) : null}
                {unused && problem?.name === undefined ? (
                  <div className="sqlws__note">The SQL has no :{row.name} for this value.</div>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      <div className="sqlws__paramactions">
        {undeclared.map((name) => (
          <Button key={name} onClick={() => onAdd(name)} title={`The SQL uses :${name}`}>
            Declare :{name}
          </Button>
        ))}
        <Button onClick={() => onAdd("")}>Add parameter</Button>
      </div>
    </fieldset>
  );
}
