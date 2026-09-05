import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ColumnDocumentationRead } from "../lib/_column_documentation_api";

const fetchColumns = vi.fn();
const fetchTable = vi.fn();
const requestWithdrawal = vi.fn();

vi.mock("../lib/_column_documentation_api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../lib/_column_documentation_api",
  );
  return {
    ...actual,
    fetchColumnDocumentation: (...args: unknown[]) => fetchColumns(...args),
    fetchTableDescription: (...args: unknown[]) => fetchTable(...args),
    requestDescriptionWithdrawal: (...args: unknown[]) => requestWithdrawal(...args),
  };
});

import { ColumnPanel } from "./ColumnPanel";

function column(overrides: Partial<ColumnDocumentationRead>): ColumnDocumentationRead {
  return {
    column_id: "c1",
    table_id: "t1",
    name: "customer_id",
    ordinal_position: 0,
    physical_type: "uuid",
    nullable: false,
    classification: "INTERNAL",
    classification_source: "RULE",
    source_description: null,
    business_description: null,
    description_version: null,
    description_approved_by: null,
    description_approved_at: null,
    source_claim_id: null,
    withdrawn_description: null,
    ...overrides,
  };
}

function tableDescription(overrides: Record<string, unknown> = {}) {
  return {
    table_id: "t1",
    name: "customers",
    source_description: null,
    readme: null,
    readme_version: null,
    approved_by: null,
    approved_at: null,
    withdrawn_readme: null,
    ...overrides,
  };
}

beforeEach(() => {
  fetchColumns.mockReset();
  // Undocumented by default, so a test that cares about the table row opts in
  // and the column tests are not competing with a second Withdraw control.
  fetchTable.mockReset().mockResolvedValue(tableDescription());
  requestWithdrawal.mockReset();
});

/** Wait for the dialog and fill in its reason, the way a steward would. */
async function fillDialog(reasonLabel: string, reason: string) {
  await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
  fireEvent.change(screen.getByLabelText(reasonLabel), { target: { value: reason } });
}

/* --------------------------------------------------------------------------
   Reading columns.
-------------------------------------------------------------------------- */

it("shows the authored description and the source comment as separate labelled claims", async () => {
  // The distinction is the point: one is overwritten by the next rediscovery
  // pass and one is not, so a pane that merged them would be lying about
  // durability.
  fetchColumns.mockResolvedValue([
    column({
      business_description: "Unique customer identifier across retail systems.",
      description_version: 2,
      description_approved_by: "checker@example.com",
      source_description: "pk",
    }),
  ]);

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() => expect(screen.getByText("Business description")).toBeInTheDocument());
  expect(
    screen.getByText("Unique customer identifier across retail systems."),
  ).toBeInTheDocument();
  expect(screen.getByText("Source comment")).toBeInTheDocument();
  expect(screen.getByText("pk")).toBeInTheDocument();
  expect(screen.getByText(/Approved v2 by checker@example\.com/)).toBeInTheDocument();
});

it("does not present a source comment as if it were an approved description", async () => {
  fetchColumns.mockResolvedValue([
    column({ business_description: null, source_description: "govt id number" }),
  ]);

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() => expect(screen.getByText("Source comment")).toBeInTheDocument());
  expect(screen.queryByText("Business description")).not.toBeInTheDocument();
});

it("says plainly when a column has no description at all", async () => {
  fetchColumns.mockResolvedValue([column({})]);

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() => expect(screen.getByText("No description.")).toBeInTheDocument());
});

it("counts how many columns are described", async () => {
  fetchColumns.mockResolvedValue([
    column({ column_id: "c1", business_description: "Described." }),
    column({ column_id: "c2", name: "ssn" }),
    column({ column_id: "c3", name: "opened_at" }),
  ]);

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() => expect(screen.getByText("1 of 3 described")).toBeInTheDocument());
});

it("collapses a wide table behind a show-more control", async () => {
  // A 40-column table would otherwise push the evidence items this pane leads
  // with off the screen.
  fetchColumns.mockResolvedValue(
    Array.from({ length: 12 }, (_, i) =>
      column({ column_id: `c${i}`, name: `col_${i}`, ordinal_position: i }),
    ),
  );

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() => expect(screen.getByText("col_0")).toBeInTheDocument());
  expect(screen.queryByText("col_11")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Show 4 more columns" }));
  expect(screen.getByText("col_11")).toBeInTheDocument();
});

it("renders an authorization failure rather than an empty column list", async () => {
  const { ApiError } = await import("../lib/api");
  fetchColumns.mockRejectedValue(new ApiError(403, "NO_BINDING_FOR_DATASOURCE"));

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() =>
    expect(
      screen.getByText("You are not authorized to view this table's columns."),
    ).toBeInTheDocument(),
  );
});

it("says the table has no columns rather than rendering an empty list", async () => {
  fetchColumns.mockResolvedValue([]);

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() =>
    expect(screen.getByText("This table has no active columns.")).toBeInTheDocument(),
  );
  expect(screen.getByText("none")).toBeInTheDocument();
});

it("distinguishes a retired description from a column nobody has described", async () => {
  // Materially different facts: "we looked and decided to say nothing" versus
  // "nobody has looked". Collapsing them would lose a real editorial decision.
  fetchColumns.mockResolvedValue([
    column({ business_description: null, withdrawn_description: "The retired text." }),
  ]);

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() => expect(screen.getByText("Withdrawn description")).toBeInTheDocument());
  expect(screen.getByText("The retired text.")).toBeInTheDocument();
  expect(screen.getByText(/this column reads as undescribed/)).toBeInTheDocument();
  expect(screen.queryByText("No description.")).not.toBeInTheDocument();
  expect(screen.queryByText("Business description")).not.toBeInTheDocument();
});

it("offers withdrawal only where there is an approved description to retire", async () => {
  fetchColumns.mockResolvedValue([
    column({ column_id: "c1", name: "described", business_description: "Live." }),
    column({ column_id: "c2", name: "bare" }),
  ]);

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() => expect(screen.getByText("Live.")).toBeInTheDocument());
  expect(screen.getAllByRole("button", { name: "Withdraw" })).toHaveLength(1);
});

/* --------------------------------------------------------------------------
   The dialog, which replaced a window.prompt.
-------------------------------------------------------------------------- */

it("shows the text being retired, not just a reason box", async () => {
  // Deciding to retire a paragraph without re-reading it is exactly how the
  // wrong one gets retired -- which is what the prompt allowed.
  fetchColumns.mockResolvedValue([column({ business_description: "The exact wording." })]);

  render(<ColumnPanel tableId="t1" />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));

  const dialog = await waitFor(() => screen.getByRole("dialog"));
  expect(within(dialog).getByText("The exact wording.")).toBeInTheDocument();
  expect(within(dialog).getByText(/Nothing is un-published now/)).toBeInTheDocument();
});

it("requests a withdrawal with the steward's reason and says a reviewer decides", async () => {
  fetchColumns.mockResolvedValue([column({ business_description: "Wrong text." })]);
  requestWithdrawal.mockResolvedValue({ id: "w1", status: "PENDING_REVIEW" });

  render(<ColumnPanel tableId="t1" />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));
  await fillDialog("Why should it be retired?", "describes the wrong column");
  fireEvent.click(screen.getByRole("button", { name: "Request withdrawal" }));

  await waitFor(() =>
    expect(requestWithdrawal).toHaveBeenCalledWith(
      "COLUMN",
      "c1",
      "describes the wrong column",
      "WITHDRAW",
    ),
  );
  expect(screen.getByText(/stays published until a reviewer approves/)).toBeInTheDocument();
});

it("refuses to send without a reason, and says so instead of failing silently", async () => {
  // `prompt` conflated "cancel" with "empty reason"; the difference matters
  // when the reason is what the reviewer decides on.
  fetchColumns.mockResolvedValue([column({ business_description: "Live." })]);

  render(<ColumnPanel tableId="t1" />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));
  await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Request withdrawal" }));

  await waitFor(() => expect(screen.getByText(/Give a reason/)).toBeInTheDocument());
  expect(requestWithdrawal).not.toHaveBeenCalled();
});

it("cancels without sending anything", async () => {
  fetchColumns.mockResolvedValue([column({ business_description: "Live." })]);

  render(<ColumnPanel tableId="t1" />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));
  await fillDialog("Why should it be retired?", "a good reason");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(requestWithdrawal).not.toHaveBeenCalled();
});

it("keeps the dialog open and shows why when the server refuses", async () => {
  const { ApiError } = await import("../lib/api");
  fetchColumns.mockResolvedValue([column({ business_description: "Live." })]);
  requestWithdrawal.mockRejectedValue(
    new ApiError(409, "a withdrawal for this asset is already awaiting review"),
  );

  render(<ColumnPanel tableId="t1" />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));
  await fillDialog("Why should it be retired?", "a good reason");
  fireEvent.click(screen.getByRole("button", { name: "Request withdrawal" }));

  await waitFor(() =>
    expect(
      screen.getByText("a withdrawal for this asset is already awaiting review"),
    ).toBeInTheDocument(),
  );
  // Still open, so the steward can correct and retry rather than start over.
  expect(screen.getByRole("dialog")).toBeInTheDocument();
});

/* --------------------------------------------------------------------------
   Reinstatement, and the table-level control.
-------------------------------------------------------------------------- */

it("offers reinstatement on a retired description, and explains it republishes", async () => {
  fetchColumns.mockResolvedValue([
    column({ business_description: null, withdrawn_description: "The retired text." }),
  ]);
  requestWithdrawal.mockResolvedValue({ id: "w2", status: "PENDING_REVIEW" });

  render(<ColumnPanel tableId="t1" />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Reinstate" })).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Reinstate" }));

  const dialog = await waitFor(() => screen.getByRole("dialog"));
  expect(within(dialog).getByText("The retired text.")).toBeInTheDocument();
  // The property that makes reinstatement safe: it does not rewrite history.
  expect(within(dialog).getByText(/the withdrawn one stays withdrawn/)).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Why should it come back?"), {
    target: { value: "withdrawn by mistake" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Request reinstatement" }));

  await waitFor(() =>
    expect(requestWithdrawal).toHaveBeenCalledWith(
      "COLUMN",
      "c1",
      "withdrawn by mistake",
      "REINSTATE",
    ),
  );
});

it("offers no reinstatement on a column that is currently described", async () => {
  fetchColumns.mockResolvedValue([column({ business_description: "Live." })]);

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument(),
  );
  expect(screen.queryByRole("button", { name: "Reinstate" })).not.toBeInTheDocument();
});

it("renders the table's own documentation with its own withdraw control", async () => {
  fetchColumns.mockResolvedValue([column({})]);
  fetchTable.mockResolvedValue(
    tableDescription({ readme: "One row per retail customer.", readme_version: 3 }),
  );
  requestWithdrawal.mockResolvedValue({ id: "w3", status: "PENDING_REVIEW" });

  render(<ColumnPanel tableId="t1" />);
  await waitFor(() => expect(screen.getByText("Table description")).toBeInTheDocument());
  expect(screen.getByText("One row per retail customer.")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));
  await fillDialog("Why should it be retired?", "superseded by the data contract");
  fireEvent.click(screen.getByRole("button", { name: "Request withdrawal" }));

  await waitFor(() =>
    expect(requestWithdrawal).toHaveBeenCalledWith(
      "TABLE",
      "t1",
      "superseded by the data contract",
      "WITHDRAW",
    ),
  );
});

it("shows a retired table description as retired, with reinstatement offered", async () => {
  fetchColumns.mockResolvedValue([column({})]);
  fetchTable.mockResolvedValue(tableDescription({ withdrawn_readme: "The retired readme." }));

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() => expect(screen.getByText("The retired readme.")).toBeInTheDocument());
  expect(screen.getByText(/this table reads as undocumented/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reinstate" })).toBeInTheDocument();
});

it("shows no table section when the table has never been documented", async () => {
  fetchColumns.mockResolvedValue([column({})]);

  render(<ColumnPanel tableId="t1" />);

  await waitFor(() => expect(screen.getByText("No description.")).toBeInTheDocument());
  expect(screen.queryByText("Table description")).not.toBeInTheDocument();
});
