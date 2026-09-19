import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type {
  QueryExecutionResponse,
  SqlDraftReceiptRead,
  SqlDraftResponse,
  SqlDraftRunResponse,
} from "../lib/types";
import { ApiError } from "../lib/http";

/* ---------------------------------------------------------------------------
   R11-SQL01: the review-first path. The API boundary (`../lib/api/sqlWorkspace`)
   is mocked with the route's real payload shapes; the error mapping is the
   real one, so a refusal renders exactly as it would against the server.
--------------------------------------------------------------------------- */

const createSqlDraft =
  vi.fn<(datasourceId: string, body: unknown, signal?: AbortSignal) => Promise<SqlDraftResponse>>();
const runSqlDraft =
  vi.fn<(receiptId: string, body: unknown, signal?: AbortSignal) => Promise<SqlDraftRunResponse>>();
const listSqlDrafts =
  vi.fn<(datasourceId: string, signal?: AbortSignal) => Promise<SqlDraftReceiptRead[]>>();

vi.mock("../lib/api/sqlWorkspace", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/sqlWorkspace")>();
  return {
    ...actual,
    createSqlDraft: (datasourceId: string, body: unknown, signal?: AbortSignal) =>
      createSqlDraft(datasourceId, body, signal),
    runSqlDraft: (receiptId: string, body: unknown, signal?: AbortSignal) =>
      runSqlDraft(receiptId, body, signal),
    listSqlDrafts: (datasourceId: string, signal?: AbortSignal) =>
      listSqlDrafts(datasourceId, signal),
  };
});

const { SqlWorkspace, receiptState } = await import("./SqlWorkspace");

const GENERATED = "SELECT o.order_id FROM retail.orders AS o";

function receipt(overrides: Partial<SqlDraftReceiptRead> = {}): SqlDraftReceiptRead {
  return {
    id: "rcpt-1",
    origin: "GENERATED",
    status: "VALIDATED",
    statement_digest: "d".repeat(64),
    referenced_tables: ["retail.orders"],
    created_at: "2026-09-18T12:00:00Z",
    expires_at: "2026-09-18T12:15:00Z",
    ...overrides,
  };
}

function drafted(overrides: Partial<SqlDraftResponse> = {}): SqlDraftResponse {
  return {
    origin: "GENERATED",
    sql: GENERATED,
    agent_run_id: "run-1",
    validation: {
      valid: true,
      dialect: "postgres",
      findings: [],
      referenced_tables: ["retail.orders"],
      referenced_columns: ["retail.orders.order_id"],
      column_lineage: [],
      estimate: { plan_cost: 1, kind: "EXPLAIN" },
    },
    receipt: receipt(),
    ...overrides,
  };
}

const EXECUTION: QueryExecutionResponse = {
  execution_id: "exec-1",
  status: "SUCCEEDED",
  normalized_sql: GENERATED,
  referenced_tables: ["retail.orders"],
  referenced_columns: ["retail.orders.order_id"],
  column_lineage: [],
  plan_cost: 1,
  warehouse_query_id: null,
  row_count: 1,
  elapsed_ms: 4,
  masked_columns: [],
  rows: [{ order_id: "O-1" }],
};

function renderWorkspace(question = "which orders are there", productKey: string | null = null) {
  return render(<SqlWorkspace datasourceId="ds-1" productKey={productKey} question={question} />);
}

function sqlBox(): HTMLTextAreaElement {
  return screen.getByLabelText("SQL statement") as HTMLTextAreaElement;
}

function runButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Run" }) as HTMLButtonElement;
}

beforeEach(() => {
  createSqlDraft.mockReset();
  runSqlDraft.mockReset();
  listSqlDrafts.mockReset();
  listSqlDrafts.mockResolvedValue([]);
});

describe("SqlWorkspace (R11-SQL01)", () => {
  it("drafts from the question without running it, then runs the validated text on Run", async () => {
    createSqlDraft.mockResolvedValue(drafted());
    runSqlDraft.mockResolvedValue({ receipt: receipt({ status: "EXECUTED" }), execution: EXECUTION });
    renderWorkspace();

    fireEvent.click(screen.getByRole("button", { name: "Draft from question" }));

    await waitFor(() => expect(sqlBox().value).toBe(GENERATED));
    expect(createSqlDraft).toHaveBeenCalledWith(
      "ds-1",
      { question: "which orders are there", context_product_key: null },
      expect.any(AbortSignal),
    );
    expect(screen.getByText("Valid — not run")).toBeTruthy();
    expect(screen.getByText("Drafted by the model")).toBeTruthy();
    expect(runSqlDraft).not.toHaveBeenCalled();

    fireEvent.click(runButton());

    await waitFor(() => expect(runSqlDraft).toHaveBeenCalledTimes(1));
    expect(runSqlDraft).toHaveBeenCalledWith(
      "rcpt-1",
      { sql: GENERATED, context_product_key: null },
      expect.any(AbortSignal),
    );
    await waitFor(() => expect(screen.getByText("O-1")).toBeTruthy());
    expect(runButton().disabled).toBe(true);
    // Found in the deployed browser journey: the badge kept saying "not run" after the run.
    expect(screen.queryByText("Valid — not run")).toBeNull();
    expect(screen.getByText("Ran once")).toBeTruthy();
    expect(screen.getByText("Validate again to run it again.")).toBeTruthy();
  });

  it("disables Run once the validated text is edited, and validates the edit as pasted SQL", async () => {
    createSqlDraft.mockResolvedValueOnce(drafted());
    renderWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Draft from question" }));
    await waitFor(() => expect(runButton().disabled).toBe(false));

    const edited = `${GENERATED} WHERE o.order_id = 'O-2'`;
    fireEvent.change(sqlBox(), { target: { value: edited } });

    expect(runButton().disabled).toBe(true);
    expect(screen.getByText(/Edited since it was validated/)).toBeTruthy();

    createSqlDraft.mockResolvedValueOnce(
      drafted({ origin: "PASTED", sql: null, receipt: receipt({ id: "rcpt-2", origin: "PASTED" }) }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));

    await waitFor(() => expect(runButton().disabled).toBe(false));
    expect(createSqlDraft).toHaveBeenLastCalledWith(
      "ds-1",
      { sql: edited, context_product_key: null },
      expect.any(AbortSignal),
    );
    expect(sqlBox().value).toBe(edited);
  });

  it("shows findings and offers no Run for a statement that cannot run", async () => {
    createSqlDraft.mockResolvedValue(
      drafted({
        origin: "PASTED",
        sql: null,
        receipt: null,
        validation: {
          valid: false,
          dialect: "postgres",
          findings: [
            {
              code: "UNKNOWN_OR_UNAUTHORIZED_TABLE",
              severity: "ERROR",
              ref: "retail.secret_ledger",
              hint: "Use a table the catalog lists for you.",
            },
          ],
          referenced_tables: ["retail.secret_ledger"],
          referenced_columns: [],
          column_lineage: [],
          estimate: {},
        },
      }),
    );
    renderWorkspace();
    fireEvent.change(sqlBox(), { target: { value: "SELECT * FROM retail.secret_ledger" } });

    fireEvent.click(screen.getByRole("button", { name: "Validate" }));

    await waitFor(() => expect(screen.getByText("Cannot run")).toBeTruthy());
    expect(screen.getByText("UNKNOWN_OR_UNAUTHORIZED_TABLE")).toBeTruthy();
    expect(screen.getByText("Use a table the catalog lists for you.")).toBeTruthy();
    expect(runButton().disabled).toBe(true);
  });

  it("says why a Run was refused and asks for a fresh validation", async () => {
    createSqlDraft.mockResolvedValue(drafted());
    runSqlDraft.mockRejectedValue(
      new ApiError(409, "Conflict", { details: { code: "RECEIPT_EXPIRED", execution_id: null } }),
    );
    renderWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Draft from question" }));
    await waitFor(() => expect(runButton().disabled).toBe(false));

    fireEvent.click(runButton());

    await waitFor(() => expect(screen.getByText("The validation expired")).toBeTruthy());
    expect(runButton().disabled).toBe(true);
  });

  it("hands out no SQL when an approved governed tool answers the question", async () => {
    createSqlDraft.mockResolvedValue({
      origin: "GENERATED",
      sql: null,
      agent_run_id: "run-2",
      reason: "GOVERNED_TOOL_ANSWERS",
      selected_tool_version_id: "tool-v1",
      validation: null,
      receipt: null,
    });
    renderWorkspace("order lookup");

    fireEvent.click(screen.getByRole("button", { name: "Draft from question" }));

    await waitFor(() =>
      expect(screen.getByText("An approved governed tool answers this question")).toBeTruthy(),
    );
    expect(sqlBox().value).toBe("");
    expect(runButton().disabled).toBe(true);
  });

  it("sends the context product with the draft and with the run", async () => {
    createSqlDraft.mockResolvedValue(drafted());
    runSqlDraft.mockResolvedValue({ receipt: receipt({ status: "EXECUTED" }), execution: EXECUTION });
    renderWorkspace("which orders are there", "orders-context");

    fireEvent.click(screen.getByRole("button", { name: "Draft from question" }));
    await waitFor(() => expect(runButton().disabled).toBe(false));
    fireEvent.click(runButton());

    await waitFor(() => expect(runSqlDraft).toHaveBeenCalledTimes(1));
    expect(createSqlDraft.mock.calls[0]?.[1]).toEqual({
      question: "which orders are there",
      context_product_key: "orders-context",
    });
    expect(runSqlDraft.mock.calls[0]?.[1]).toEqual({
      sql: GENERATED,
      context_product_key: "orders-context",
    });
  });

  it("does not offer drafting before there is a question to draft from", () => {
    renderWorkspace("");

    const draft = screen.getByRole("button", { name: "Draft from question" }) as HTMLButtonElement;
    expect(draft.disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Validate" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });
});


describe("SqlWorkspace history (R11-SQL01)", () => {
  const FUTURE = "2999-01-01T00:00:00Z";

  it("lists the caller's recent reviewed SQL as value-free shapes with their state", async () => {
    listSqlDrafts.mockResolvedValue([
      receipt({
        id: "r-ran",
        origin: "PASTED",
        status: "EXECUTED",
        redacted_sql: "SELECT a.status FROM customer.account AS a WHERE a.status <> :redacted",
        expires_at: FUTURE,
      }),
      receipt({ id: "r-open", status: "VALIDATED", redacted_sql: null, expires_at: FUTURE }),
    ]);
    renderWorkspace();

    await waitFor(() => expect(screen.getByText("Ran")).toBeTruthy());
    expect(listSqlDrafts).toHaveBeenCalledWith("ds-1", expect.any(AbortSignal));
    expect(screen.getByText("Validated, not run")).toBeTruthy();
    expect(screen.getByText(/:redacted/)).toBeTruthy();
    expect(screen.getByText("Shape withheld: it could not be redacted safely.")).toBeTruthy();
    expect(screen.getByText(/your SQL/)).toBeTruthy();
    expect(screen.getByText(/drafted by the model/)).toBeTruthy();
  });

  it("reads the history again after a validation and after a run", async () => {
    createSqlDraft.mockResolvedValue(drafted());
    runSqlDraft.mockResolvedValue({ receipt: receipt({ status: "EXECUTED" }), execution: EXECUTION });
    renderWorkspace();
    await waitFor(() => expect(listSqlDrafts).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Draft from question" }));
    await waitFor(() => expect(listSqlDrafts).toHaveBeenCalledTimes(2));
    fireEvent.click(runButton());
    await waitFor(() => expect(listSqlDrafts).toHaveBeenCalledTimes(3));
  });

  it("keeps working when the history cannot be read", async () => {
    listSqlDrafts.mockRejectedValue(new Error("history unavailable"));
    createSqlDraft.mockResolvedValue(drafted());
    renderWorkspace();

    await waitFor(() => expect(screen.getByText(/could not be read/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Draft from question" }));
    await waitFor(() => expect(runButton().disabled).toBe(false));
  });

  it("says in the history that parameter values are never kept", async () => {
    renderWorkspace();
    await waitFor(() => expect(listSqlDrafts).toHaveBeenCalled());
    expect(screen.getByText(/parameter values and results are never kept/)).toBeTruthy();
  });

  it("calls an unrun validation past its expiry expired", () => {
    const now = new Date("2026-09-18T13:00:00Z");
    expect(receiptState(receipt({ expires_at: "2026-09-18T12:15:00Z" }), now).label).toBe(
      "Expired",
    );
    expect(receiptState(receipt({ expires_at: FUTURE }), now).label).toBe("Validated, not run");
    expect(receiptState(receipt({ status: "FAILED" }), now).label).toBe("Refused at run");
    expect(receiptState(receipt({ status: "EXECUTED" }), now).label).toBe("Ran");
  });
});

describe("SqlWorkspace parameters (R11-SQL01)", () => {
  const TEMPLATE = "SELECT o.order_id FROM retail.orders AS o WHERE o.order_id = :order_id";

  function pasted(overrides: Partial<SqlDraftResponse> = {}): SqlDraftResponse {
    return drafted({
      origin: "PASTED",
      sql: null,
      receipt: receipt({ id: "rcpt-p", origin: "PASTED" }),
      ...overrides,
    });
  }

  function validateButton(): HTMLButtonElement {
    return screen.getByRole("button", { name: "Validate" }) as HTMLButtonElement;
  }

  function field(label: string): HTMLInputElement | HTMLSelectElement {
    return screen.getByLabelText(label) as HTMLInputElement | HTMLSelectElement;
  }

  async function declaredOrderId(type: string, value: string) {
    renderWorkspace();
    fireEvent.change(sqlBox(), { target: { value: TEMPLATE } });
    fireEvent.click(screen.getByRole("button", { name: "Declare :order_id" }));
    expect(field("Name of parameter 1").value).toBe("order_id");
    fireEvent.change(field("Type of parameter 1"), { target: { value: type } });
    fireEvent.change(field("Value of parameter 1"), { target: { value } });
  }

  it("sends typed values beside the SQL, and runs exactly what was validated", async () => {
    createSqlDraft.mockResolvedValue(pasted());
    runSqlDraft.mockResolvedValue({
      receipt: receipt({ id: "rcpt-p", status: "EXECUTED" }),
      execution: EXECUTION,
    });
    await declaredOrderId("INTEGER", "42");

    fireEvent.click(validateButton());

    await waitFor(() => expect(runButton().disabled).toBe(false));
    const bound = [{ name: "order_id", parameter_type: "INTEGER", value: 42 }];
    expect(createSqlDraft).toHaveBeenCalledWith(
      "ds-1",
      { sql: TEMPLATE, parameters: bound, context_product_key: null },
      expect.any(AbortSignal),
    );

    fireEvent.click(runButton());

    await waitFor(() => expect(runSqlDraft).toHaveBeenCalledTimes(1));
    expect(runSqlDraft).toHaveBeenCalledWith(
      "rcpt-p",
      { sql: TEMPLATE, parameters: bound, context_product_key: null },
      expect.any(AbortSignal),
    );
  });

  it("says what is wrong with a value and will not validate until it fits its type", async () => {
    await declaredOrderId("INTEGER", "4.5");

    expect(screen.getByText("Enter a whole number.")).toBeTruthy();
    expect(field("Value of parameter 1").getAttribute("aria-invalid")).toBe("true");
    expect(validateButton().disabled).toBe(true);
    expect(screen.getByText("Fix the parameters above to validate.")).toBeTruthy();

    fireEvent.change(field("Type of parameter 1"), { target: { value: "DATE" } });
    fireEvent.change(field("Value of parameter 1"), { target: { value: "2024-02-30" } });
    expect(screen.getByText("Enter a date as YYYY-MM-DD.")).toBeTruthy();

    fireEvent.change(field("Value of parameter 1"), { target: { value: "2024-02-29" } });
    expect(screen.queryByText("Enter a date as YYYY-MM-DD.")).toBeNull();
    expect(validateButton().disabled).toBe(false);
    expect(createSqlDraft).not.toHaveBeenCalled();
  });

  it("locks Run when a value changes after validating, and says to validate again", async () => {
    createSqlDraft.mockResolvedValue(pasted());
    await declaredOrderId("STRING", "O-1");
    fireEvent.click(validateButton());
    await waitFor(() => expect(runButton().disabled).toBe(false));

    fireEvent.change(field("Value of parameter 1"), { target: { value: "O-2" } });

    expect(runButton().disabled).toBe(true);
    expect(
      screen.getByText(
        "Parameter values changed since it was validated — validate again to run it.",
      ),
    ).toBeTruthy();

    // The validated value again is the validated statement again.
    fireEvent.change(field("Value of parameter 1"), { target: { value: "O-1" } });
    expect(runButton().disabled).toBe(false);

    // Another type for the same text is another binding.
    fireEvent.change(field("Type of parameter 1"), { target: { value: "INTEGER" } });
    fireEvent.change(field("Value of parameter 1"), { target: { value: "1" } });
    expect(runButton().disabled).toBe(true);
    expect(runSqlDraft).not.toHaveBeenCalled();
  });

  it("shows the server's parameter findings and offers no Run", async () => {
    createSqlDraft.mockResolvedValue(
      pasted({
        receipt: null,
        validation: {
          valid: false,
          dialect: "postgres",
          findings: [
            {
              code: "PARAMETER_TYPE_MISMATCH",
              severity: "ERROR",
              ref: "order_id",
              hint: "the value does not match the declared type",
              detail: { parameter_type: "INTEGER" },
            },
          ],
          referenced_tables: [],
          referenced_columns: [],
          column_lineage: [],
          estimate: {},
        },
      }),
    );
    await declaredOrderId("STRING", "O-1");

    fireEvent.click(validateButton());

    await waitFor(() => expect(screen.getByText("Cannot run")).toBeTruthy());
    expect(screen.getByText("PARAMETER_TYPE_MISMATCH")).toBeTruthy();
    expect(screen.getByText(/order_id/, { selector: ".sqlws__ref" })).toBeTruthy();
    expect(runButton().disabled).toBe(true);
  });

  it("notes a declared value the SQL does not use, and removes a parameter", async () => {
    renderWorkspace();
    fireEvent.change(sqlBox(), { target: { value: "SELECT o.order_id FROM retail.orders AS o" } });
    fireEvent.click(screen.getByRole("button", { name: "Add parameter" }));
    fireEvent.change(field("Name of parameter 1"), { target: { value: "region" } });

    expect(screen.getByText("The SQL has no :region for this value.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.queryByLabelText("Name of parameter 1")).toBeNull();
  });

  it("sends no parameters for SQL that has none", async () => {
    createSqlDraft.mockResolvedValue(pasted());
    renderWorkspace();
    fireEvent.change(sqlBox(), { target: { value: GENERATED } });

    fireEvent.click(validateButton());

    await waitFor(() => expect(createSqlDraft).toHaveBeenCalledTimes(1));
    expect(createSqlDraft.mock.calls[0]?.[1]).toEqual({ sql: GENERATED, context_product_key: null });
  });
});
