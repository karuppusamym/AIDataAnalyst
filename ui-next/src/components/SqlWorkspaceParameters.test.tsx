import { describe, expect, it } from "vitest";
import {
  bindingKey,
  parameterProblems,
  placeholdersIn,
  toSqlDraftParameters,
  type ParameterRow,
  type SqlParameterType,
} from "./SqlWorkspaceParameters";

/* ---------------------------------------------------------------------------
   R11-SQL01: the parameter checks the workspace makes before sending. They
   mirror the server's binder (`aida/sql_workspace.py`), which decides anyway.
--------------------------------------------------------------------------- */

function row(parameterType: SqlParameterType, value: string, name = "p", key = 1): ParameterRow {
  return { key, name, parameterType, value };
}

describe("placeholdersIn", () => {
  it("finds each :name once, in order", () => {
    expect(placeholdersIn("SELECT a FROM t WHERE b = :b AND c > :c AND d = :b")).toEqual([
      "b",
      "c",
    ]);
  });

  it("ignores casts, string literals, quoted names and comments", () => {
    const sql = [
      "SELECT a::int, '12:30' AS t, \"x:y\" FROM s.t AS t",
      "-- :commented",
      "/* :blocked */ WHERE t.a = :real AND t.r[1:n] IS NOT NULL",
    ].join("\n");
    expect(placeholdersIn(sql)).toEqual(["real"]);
  });
});

describe("parameterProblems", () => {
  it.each<[SqlParameterType, string]>([
    ["INTEGER", "42"],
    ["INTEGER", "-7"],
    ["NUMBER", "12.5"],
    ["NUMBER", "-1e3"],
    ["BOOLEAN", "false"],
    ["DATE", "2024-02-29"],
    ["STRING", ""],
    ["STRING", "O'Brien; DROP TABLE x"],
  ])("accepts %s %j", (type, value) => {
    expect(parameterProblems([row(type, value)]).size).toBe(0);
  });

  it.each<[SqlParameterType, string, string]>([
    ["INTEGER", "4.5", "Enter a whole number."],
    ["INTEGER", "", "Enter a whole number."],
    // Number() reads both as whole numbers; a person reading the field would not.
    ["INTEGER", "5.", "Enter a whole number."],
    ["INTEGER", "1e3", "Enter a whole number."],
    ["INTEGER", "9007199254740993", "Enter a whole number."],
    ["NUMBER", "abc", "Enter a number."],
    ["NUMBER", "0x10", "Enter a number."],
    ["NUMBER", "", "Enter a number."],
    ["BOOLEAN", "", "Choose true or false."],
    ["DATE", "2024-02-30", "Enter a date as YYYY-MM-DD."],
    ["DATE", "05/01/2024", "Enter a date as YYYY-MM-DD."],
    ["STRING", "x".repeat(4001), "At most 4,000 characters."],
  ])("refuses %s %j", (type, value, message) => {
    expect(parameterProblems([row(type, value)]).get(1)).toEqual({ value: message });
  });

  it("names a bad or repeated name", () => {
    const problems = parameterProblems([
      row("STRING", "a", "Order-Id", 1),
      row("STRING", "a", "dup", 2),
      row("STRING", "b", "dup", 3),
    ]);
    expect(problems.get(1)?.name).toMatch(/lower-case letters/);
    expect(problems.get(2)?.name).toBe("Declared more than once.");
    expect(problems.get(3)?.name).toBe("Declared more than once.");
  });
});

describe("toSqlDraftParameters", () => {
  it("sends each value in its declared type's JSON form", () => {
    expect(
      toSqlDraftParameters([
        row("STRING", " padded ", "s"),
        row("INTEGER", " 42 ", "i"),
        row("NUMBER", "12.5", "n"),
        row("BOOLEAN", "false", "b"),
        row("DATE", "2024-01-05", "d"),
      ]),
    ).toEqual([
      { name: "s", parameter_type: "STRING", value: " padded " },
      { name: "i", parameter_type: "INTEGER", value: 42 },
      { name: "n", parameter_type: "NUMBER", value: 12.5 },
      { name: "b", parameter_type: "BOOLEAN", value: false },
      { name: "d", parameter_type: "DATE", value: "2024-01-05" },
    ]);
  });
});

describe("bindingKey", () => {
  it("ignores declaration order but not a type or a value", () => {
    const a = { name: "a", parameter_type: "INTEGER" as const, value: 5 };
    const b = { name: "b", parameter_type: "STRING" as const, value: "x" };
    expect(bindingKey([a, b])).toBe(bindingKey([b, a]));
    expect(bindingKey([a])).not.toBe(bindingKey([{ ...a, parameter_type: "NUMBER" }]));
    expect(bindingKey([a])).not.toBe(bindingKey([{ ...a, value: 6 }]));
  });
});
