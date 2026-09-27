import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { OkfImportReview, OkfImportReviewChange } from "../lib/api/okfImport";
import { ApiError } from "../lib/http";
import { expectNoAxeViolations } from "../test/a11y";

/* ---------------------------------------------------------------------------
   R11-OKF03: the OKF import review preview. The API boundary
   (`../lib/api/okfImport`) is mocked, the pattern `ReviewChangePreview.test.tsx`
   uses. What is pinned: per document, the text before and after; a conflict is
   said in words, with the approved text now and what approving does instead; a
   decided change shows its row status; the decision is unlocked only by a
   preview of exactly this review, and re-locked while a page is unreadable;
   the attention filter and the pages are reachable and operable from the
   keyboard; axe finds no WCAG A/AA violation.
--------------------------------------------------------------------------- */

const { fetchReview } = vi.hoisted(() => ({ fetchReview: vi.fn() }));
vi.mock("../lib/api/okfImport", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/okfImport")>();
  return { ...actual, fetchOkfImportReview: fetchReview };
});

import { OkfImportReviewPreview } from "./OkfImportReviewPreview";

function change(overrides: Partial<OkfImportReviewChange> = {}): OkfImportReviewChange {
  return {
    change_id: "c-purpose",
    subject_type: "TABLE",
    subject_id: "t-orders",
    field: "purpose",
    label: "bank.sales.orders",
    before_value: "One row per completed order.",
    proposed_value: "One row per order that reached payment.",
    expected_version: 3,
    current_version: 3,
    current_value: null,
    status: "PENDING",
    skip_reason: null,
    state: "APPLIES",
    reason_code: null,
    target_active: true,
    approval_effect: "Approving publishes the proposed text as the new approved description.",
    ...overrides,
  };
}

function review(overrides: Partial<OkfImportReview> = {}): OkfImportReview {
  return {
    review_id: "r1",
    object_type: "OKF_IMPORT_BATCH",
    object_id: "b1",
    review_status: "PENDING",
    requested_by: "steward",
    proposal_status: "PENDING_REVIEW",
    datasource_id: "d1",
    filename: "okf-bundle.zip",
    archive_sha256: "a".repeat(64),
    counts: { documents: 2, changes: 3, applies: 2, conflicts: 1, target_unavailable: 0, decided: 0 },
    offset: 0,
    limit: 25,
    total_documents: 2,
    authority: "Approving publishes every change marked APPLIES.",
    documents: [
      {
        document_id: "t-orders",
        label: "bank.sales.orders",
        object_type: "BASE_TABLE",
        conflicts: 1,
        changes: [
          change({
            state: "CONFLICT",
            reason_code: "SOURCE_CHANGED_SINCE_EXPORT",
            current_version: 4,
            current_value: "Approved while the import waited.",
            approval_effect:
              "Approving skips this change (SKIPPED_STALE): the approved description moved from v3 to v4.",
          }),
          change({
            change_id: "c-channel",
            subject_type: "COLUMN",
            field: "column:channel",
            label: "bank.sales.orders.channel",
            before_value: null,
            proposed_value: "The sales channel the order came through.",
            expected_version: null,
            current_version: null,
          }),
        ],
      },
      {
        document_id: "t-revenue",
        label: "bank.sales.revenue_daily",
        object_type: "BASE_TABLE",
        conflicts: 0,
        changes: [
          change({
            change_id: "c-revenue",
            subject_id: "t-revenue",
            label: "bank.sales.revenue_daily",
            before_value: null,
            expected_version: 2,
            proposed_value: "Daily revenue per channel.",
          }),
        ],
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  fetchReview.mockReset();
});

describe("OkfImportReviewPreview", () => {
  it("shows each document with its before and after text and a conflict in words", async () => {
    fetchReview.mockResolvedValue(review());
    const ready = vi.fn();
    render(<OkfImportReviewPreview reviewId="r1" onReady={ready} />);

    const documents = await screen.findByRole("list", { name: "Documents in this import" });
    const orders = within(documents).getByRole("region", { name: "bank.sales.orders" });
    const purpose = within(orders).getByRole("article", { name: "Purpose" });
    expect(purpose).toHaveTextContent("Before · approved v3 when exported");
    expect(purpose).toHaveTextContent("One row per completed order.");
    expect(purpose).toHaveTextContent("Proposed");
    expect(purpose).toHaveTextContent("One row per order that reached payment.");
    expect(purpose).toHaveTextContent("conflict");
    expect(purpose).toHaveTextContent("Approved now · v4");
    expect(purpose).toHaveTextContent("Approved while the import waited.");
    expect(purpose).toHaveTextContent("SKIPPED_STALE");

    const channel = within(orders).getByRole("article", { name: "Column channel" });
    expect(channel).toHaveTextContent("No approved text when the file was exported.");
    expect(channel).toHaveTextContent("applies");
    expect(channel).not.toHaveTextContent("Approved now");

    // A null "before" on a versioned field is screening, not absence.
    const revenue = within(documents).getByRole("region", { name: "bank.sales.revenue_daily" });
    expect(revenue).toHaveTextContent("Withheld by export screening.");

    expect(screen.getByText(/1 change will not be published as proposed/)).toBeInTheDocument();
    expect(fetchReview).toHaveBeenCalledWith("r1", { offset: 0, limit: 25 }, expect.anything());
    expect(ready).toHaveBeenLastCalledWith("r1");
  });

  it("shows a decided change with its own row status", async () => {
    fetchReview.mockResolvedValue(
      review({
        review_status: "APPROVED",
        proposal_status: "APPLIED",
        counts: { documents: 1, changes: 1, applies: 0, conflicts: 0, target_unavailable: 0, decided: 1 },
        total_documents: 1,
        documents: [
          {
            document_id: "t-orders",
            label: "bank.sales.orders",
            object_type: "BASE_TABLE",
            conflicts: 0,
            changes: [
              change({
                state: "DECIDED",
                status: "SKIPPED_STALE",
                skip_reason: "someone published newer documentation for this table",
                approval_effect: "Already decided: skipped stale.",
              }),
            ],
          },
        ],
      }),
    );
    render(<OkfImportReviewPreview reviewId="r1" onReady={vi.fn()} />);
    const purpose = await screen.findByRole("article", { name: "Purpose" });
    expect(purpose).toHaveTextContent("decided");
    expect(purpose).toHaveTextContent("skipped stale");
    expect(purpose).toHaveTextContent("someone published newer documentation for this table");
  });

  it("never unlocks the decision on a failed load or another review's preview, and retries", async () => {
    fetchReview.mockRejectedValueOnce(
      new ApiError(403, "Forbidden", {
        details: {
          reason_code: "DATASOURCE_NOT_AUTHORIZED",
          detail: "you may not read the model of the source this import edits",
        },
      }),
    );
    const ready = vi.fn();
    render(<OkfImportReviewPreview reviewId="r1" onReady={ready} />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("DATASOURCE_NOT_AUTHORIZED");
    expect(ready).not.toHaveBeenCalledWith("r1");

    fetchReview.mockResolvedValueOnce(review({ review_id: "someone-else" }));
    await userEvent.click(screen.getByRole("button", { name: "Retry import preview" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("belongs to a different review");
    expect(ready).not.toHaveBeenCalledWith("r1");

    fetchReview.mockResolvedValueOnce(review());
    await userEvent.click(screen.getByRole("button", { name: "Retry import preview" }));
    await screen.findByRole("list", { name: "Documents in this import" });
    expect(ready).toHaveBeenLastCalledWith("r1");
  });

  it("filters to the changes that need attention and pages from the keyboard", async () => {
    const user = userEvent.setup();
    fetchReview.mockResolvedValueOnce(review({ total_documents: 30 }));
    const ready = vi.fn();
    render(<OkfImportReviewPreview reviewId="r1" onReady={ready} />);
    await screen.findByRole("list", { name: "Documents in this import" });
    expect(screen.getByText("Documents 1–25 of 30")).toBeInTheDocument();

    // The filter is a native radio group: Tab reaches it, an arrow key moves the choice.
    const all = screen.getByRole("radio", { name: "All changes" });
    all.focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("radio", { name: "Only changes that need attention" })).toBeChecked();
    expect(screen.queryByRole("region", { name: "bank.sales.revenue_daily" })).toBeNull();
    const orders = screen.getByRole("region", { name: "bank.sales.orders" });
    expect(within(orders).queryByRole("article", { name: "Column channel" })).toBeNull();
    expect(within(orders).getByRole("article", { name: "Purpose" })).toBeInTheDocument();

    // Next page: the decision locks while the page loads, and unlocks once it has.
    let resolve: (value: OkfImportReview) => void = () => undefined;
    fetchReview.mockReturnValueOnce(new Promise<OkfImportReview>((done) => (resolve = done)));
    const next = screen.getByRole("button", { name: "Next documents" });
    next.focus();
    await user.keyboard("{Enter}");
    expect(fetchReview).toHaveBeenLastCalledWith("r1", { offset: 25, limit: 25 }, expect.anything());
    expect(ready).toHaveBeenLastCalledWith(null);
    resolve(review({ offset: 25, total_documents: 30, documents: review().documents.slice(1) }));
    expect(await screen.findByText("Documents 26–30 of 30")).toBeInTheDocument();
    expect(ready).toHaveBeenLastCalledWith("r1");
  });

  it("has no WCAG A/AA violations with a conflict and pages on screen", async () => {
    fetchReview.mockResolvedValue(review({ total_documents: 30 }));
    const { container } = render(<OkfImportReviewPreview reviewId="r1" onReady={vi.fn()} />);
    await screen.findByRole("list", { name: "Documents in this import" });
    await expectNoAxeViolations(container);
  });
});
