import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ColumnDescriptionDraftRead } from "../lib/types";

/* ---------------------------------------------------------------------------
   The column-draft section: what it lets a steward do, and what it refuses to
   let them believe. The API module is mocked at its boundary, the pattern
   `ColumnPanel.test.tsx` uses for its own API.
--------------------------------------------------------------------------- */

const list = vi.fn();
const generate = vi.fn();
const edit = vi.fn();
const submitOne = vi.fn();
const submitAll = vi.fn();

vi.mock("../lib/api/columnDescriptionDrafts", () => ({
  listTableColumnDescriptionDrafts: (...args: unknown[]) => list(...args),
  generateColumnDescriptionDrafts: (...args: unknown[]) => generate(...args),
  editColumnDescriptionDraft: (...args: unknown[]) => edit(...args),
  submitColumnDescriptionDraft: (...args: unknown[]) => submitOne(...args),
  submitTableColumnDescriptionDrafts: (...args: unknown[]) => submitAll(...args),
}));

import { ColumnDescriptionDrafts, describeGeneration } from "./ColumnDescriptionDrafts";

function draft(overrides: Partial<ColumnDescriptionDraftRead>): ColumnDescriptionDraftRead {
  return {
    id: "d1",
    organization_id: "org",
    table_id: "t1",
    table_name: "orders",
    column_id: "c1",
    column_name: "customer_id",
    drafted_text: "customer_id is a column of sales.orders (uuid, not null).",
    accuracy_score: 1,
    clarity_score: 1,
    style_score: 1,
    completeness_score: 0.6,
    overall_score: 0.9,
    reviewable: true,
    evidence: {},
    status: "DRAFT",
    base_description_version: null,
    governance_review_id: null,
    published_version_id: null,
    created_by: "steward",
    reviewed_by: null,
    reviewed_at: null,
    created_at: "2026-09-10T00:00:00Z",
    updated_at: "2026-09-10T00:00:00Z",
    ...overrides,
  };
}

const CUSTOMER = draft({});
const THIN = draft({
  id: "d2",
  column_id: "c2",
  column_name: "amt_ccy",
  drafted_text: "amt_ccy is a column of sales.orders (varchar(3), nullable).",
  overall_score: 0.15,
  reviewable: false,
});

beforeEach(() => {
  for (const mock of [list, generate, edit, submitOne, submitAll]) mock.mockReset();
});

describe("ColumnDescriptionDrafts", () => {
  it("offers to submit only the drafts that clear the evidence bar", async () => {
    list.mockResolvedValue([CUSTOMER, THIN]);
    render(<ColumnDescriptionDrafts tableId="t1" />);

    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Submit 1 for review" })).toBeInTheDocument();

    const thin = items[1]!;
    expect(within(thin).getByRole("button", { name: "Submit for review" })).toBeDisabled();
    expect(within(thin).getByText(/Rewording does not change that/)).toBeInTheDocument();
  });

  it("reports what generation did, including what it deliberately skipped", async () => {
    list.mockResolvedValue([]);
    generate.mockResolvedValue({
      drafts: [],
      created: 4,
      skipped_open: 0,
      skipped_described: 2,
      skipped_duplicate_rejected: 1,
      below_review_threshold: 2,
      tables_skipped: 0,
    });
    render(<ColumnDescriptionDrafts tableId="t1" />);
    await screen.findByText("No open drafts for this table.");

    fireEvent.click(screen.getByRole("button", { name: "Draft undescribed columns" }));

    await waitFor(() => expect(generate).toHaveBeenCalledWith(expect.any(String), ["t1"]));
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Drafted 4 columns.");
    expect(notice).toHaveTextContent("2 already described or deliberately retired.");
    expect(notice).toHaveTextContent("1 would repeat a draft a reviewer already rejected.");
    expect(notice).toHaveTextContent(/write those in the source model workbook/);
    // The list is re-read, not patched: the server decides what exists.
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("sends the text the steward started from, so a concurrent edit is refused not overwritten", async () => {
    list.mockResolvedValue([CUSTOMER]);
    edit.mockResolvedValue({ ...CUSTOMER, drafted_text: "The customer who placed the order." });
    render(<ColumnDescriptionDrafts tableId="t1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Draft text for customer_id"), {
      target: { value: "The customer who placed the order." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() =>
      expect(edit).toHaveBeenCalledWith(
        "d1",
        "The customer who placed the order.",
        CUSTOMER.drafted_text,
      ),
    );
  });

  it("says an editor cannot approve their own edit before the queue does", async () => {
    list.mockResolvedValue([draft({ evidence: { editors: ["steward-b"] } })]);
    render(<ColumnDescriptionDrafts tableId="t1" />);
    expect(
      await screen.findByText("Edited by steward-b. An editor cannot approve their own edits."),
    ).toBeInTheDocument();
  });

  it("submits a table's reviewable drafts in one action and says approval is someone else's", async () => {
    list.mockResolvedValue([CUSTOMER, THIN]);
    submitAll.mockResolvedValue({ submitted_review_ids: ["r1"], skipped_below_threshold: 1 });
    render(<ColumnDescriptionDrafts tableId="t1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Submit 1 for review" }));

    await waitFor(() => expect(submitAll).toHaveBeenCalledWith("t1"));
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Sent 1 draft to the review queue.");
    expect(notice).toHaveTextContent("Someone other than you has to approve each one");
    expect(notice).toHaveTextContent("1 stayed as drafts");
  });

  it("shows a load failure with a way to retry instead of an empty section", async () => {
    list.mockRejectedValueOnce(new Error("network down")).mockResolvedValue([]);
    render(<ColumnDescriptionDrafts tableId="t1" />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Drafts could not be loaded: network down");
    expect(screen.queryByText("No open drafts for this table.")).not.toBeInTheDocument();

    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No open drafts for this table.")).toBeInTheDocument();
  });
});

describe("describeGeneration", () => {
  it("does not claim success for a table it could not draft", () => {
    expect(
      describeGeneration({
        drafts: [],
        created: 0,
        skipped_open: 0,
        skipped_described: 0,
        skipped_duplicate_rejected: 0,
        below_review_threshold: 0,
        tables_skipped: 1,
      }),
    ).toMatch(/could not be drafted/);
  });
});
