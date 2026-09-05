import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ColumnDocumentationRead } from "../lib/_column_documentation_api";

const fetchColumns = vi.fn();
const requestWithdrawal = vi.fn();
vi.mock("../lib/_column_documentation_api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../lib/_column_documentation_api",
  );
  return {
    ...actual,
    fetchColumnDocumentation: (...args: unknown[]) => fetchColumns(...args),
    requestDescriptionWithdrawal: (...args: unknown[]) => requestWithdrawal(...args),
  };
});

import { ColumnPanel } from "./ColumnPanel";

beforeEach(() => {
  fetchColumns.mockReset();
  requestWithdrawal.mockReset();
});

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
  expect(screen.getByText("Unique customer identifier across retail systems.")).toBeInTheDocument();
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
  // A 40-column table would otherwise push the table-level evidence this pane
  // leads with off the screen.
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


/* ---------------------------------------------------------------------------
   Withdrawal (2026-09-05). Publishing a description was governed from the
   start; retiring one was not possible at all.
--------------------------------------------------------------------------- */

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
  // Not the generic empty state.
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

it("requests a withdrawal with the steward's reason and says a reviewer decides", async () => {
  fetchColumns.mockResolvedValue([column({ business_description: "Wrong text." })]);
  requestWithdrawal.mockResolvedValue({ id: "w1", status: "PENDING_REVIEW" });
  const prompt = vi.spyOn(window, "prompt").mockReturnValue("describes the wrong column");

  render(<ColumnPanel tableId="t1" />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));

  await waitFor(() =>
    expect(requestWithdrawal).toHaveBeenCalledWith("COLUMN", "c1", "describes the wrong column"),
  );
  expect(
    screen.getByText(/stays published until a reviewer approves it/),
  ).toBeInTheDocument();
  prompt.mockRestore();
});

it("does not request a withdrawal when the reason is cancelled or too short", async () => {
  // The server requires a reason, and a reviewer deciding a retraction needs
  // one -- the text itself cannot say what was wrong with it.
  fetchColumns.mockResolvedValue([column({ business_description: "Live." })]);
  const prompt = vi.spyOn(window, "prompt").mockReturnValue(null);

  render(<ColumnPanel tableId="t1" />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));
  expect(requestWithdrawal).not.toHaveBeenCalled();

  prompt.mockReturnValue("no");
  fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));
  expect(requestWithdrawal).not.toHaveBeenCalled();
  prompt.mockRestore();
});

it("surfaces a refused withdrawal instead of implying it worked", async () => {
  const { ApiError } = await import("../lib/api");
  fetchColumns.mockResolvedValue([column({ business_description: "Live." })]);
  requestWithdrawal.mockRejectedValue(
    new ApiError(409, "a withdrawal for this asset is already awaiting review"),
  );
  const prompt = vi.spyOn(window, "prompt").mockReturnValue("a good reason");

  render(<ColumnPanel tableId="t1" />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));

  await waitFor(() =>
    expect(
      screen.getByText("a withdrawal for this asset is already awaiting review"),
    ).toBeInTheDocument(),
  );
  prompt.mockRestore();
});
