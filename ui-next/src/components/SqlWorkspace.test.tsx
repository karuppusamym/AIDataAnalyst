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

vi.mock("../lib/api/sqlWorkspace", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/sqlWorkspace")>();
  return {
    ...actual,
    createSqlDraft: (datasourceId: string, body: unknown, signal?: AbortSignal) =>
      createSqlDraft(datasourceId, body, signal),
    runSqlDraft: (receiptId: string, body: unknown, signal?: AbortSignal) =>
      runSqlDraft(receiptId, body, signal),
  };
});

const { SqlWorkspace } = await import("./SqlWorkspace");

const GENERATED = "SELECT o.order_id FROM retail.orders AS o";

function receipt(overrides: Partial<SqlDraftReceiptRead> = {}): SqlDraftReceiptRead {
  return {
    id: "rcpt-1",
    origin: "GENERATED",
    status: "VALIDATED",
    statement_digest: "d".repeat(64),
    referenced_tables: ["retail.orders"],
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
