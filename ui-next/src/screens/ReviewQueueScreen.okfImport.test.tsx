import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import type { ReviewQueueRead } from "../lib/types";
import type { OkfImportReview } from "../lib/api/okfImport";
import { expectNoAxeViolations } from "../test/a11y";

/* ---------------------------------------------------------------------------
   R11-OKF03: the review queue mounts the OKF import preview for the two review
   types an import raises, and holds the decision until it has loaded -- the
   gate every full-preview type has. The API boundary is mocked, as in
   `ReviewQueueScreen.test.tsx`.
--------------------------------------------------------------------------- */

const fetchReviewQueue = vi.fn<(query: unknown, signal?: AbortSignal) => Promise<ReviewQueueRead>>();
const decideGovernanceReview = vi.fn();
const { fetchReview } = vi.hoisted(() => ({ fetchReview: vi.fn() }));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchReviewQueue: (query: unknown, signal?: AbortSignal) => fetchReviewQueue(query, signal),
    decideGovernanceReview: (...args: unknown[]) => decideGovernanceReview(...args),
  };
});
vi.mock("../lib/api/okfImport", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/okfImport")>();
  return { ...actual, fetchOkfImportReview: fetchReview };
});

function proposal(objectType: string): ReviewQueueRead["proposals"][number] {
  return {
    review_id: "okf_1",
    organization_id: "org1",
    object_type: objectType,
    object_id: "batch-1",
    requested_action: "APPLY_OKF_IMPORT",
    status: "PENDING",
    requested_by: "steward",
    decided_by: null,
    decision_reason: null,
    decided_at: null,
    created_at: "2026-09-19T00:00:00Z",
    confidence: null,
    evidence: [],
    diff: {
      review_id: "okf_1",
      object_type: objectType,
      object_id: "batch-1",
      diffable: false,
      entries: [],
    },
  };
}

function queueOf(proposals: ReviewQueueRead["proposals"]): ReviewQueueRead {
  return {
    organization_id: "org1",
    status_filter: "PENDING",
    object_type_filter: null,
    inference_run_id_filter: null,
    generated_at: "2026-09-19T00:00:00Z",
    proposals,
    total_proposals: proposals.length,
    by_status: { PENDING: proposals.length },
    by_object_type: {},
    diffable_count: 0,
  };
}

function preview(objectType: string): OkfImportReview {
  const routine = objectType === "OKF_IMPORT_ROUTINE_DESCRIPTION";
  return {
    review_id: "okf_1",
    object_type: objectType,
    object_id: "batch-1",
    review_status: "PENDING",
    requested_by: "steward",
    proposal_status: routine ? "PENDING_APPROVAL" : "PENDING_REVIEW",
    datasource_id: "d1",
    filename: routine ? null : "okf-bundle.zip",
    archive_sha256: "b".repeat(64),
    counts: { documents: 1, changes: 1, applies: 1, conflicts: 0, target_unavailable: 0, decided: 0 },
    offset: 0,
    limit: 25,
    total_documents: 1,
    authority: "Approving publishes every change marked APPLIES.",
    documents: [
      {
        document_id: "x1",
        label: routine ? "bank.sales.rebuild_totals(integer)" : "bank.sales.orders",
        object_type: routine ? "PROCEDURE" : "BASE_TABLE",
        conflicts: 0,
        changes: [
          {
            change_id: "c1",
            subject_type: routine ? "ROUTINE" : "TABLE",
            subject_id: "x1",
            field: "purpose",
            label: "bank.sales.orders",
            before_value: "Old text.",
            proposed_value: "New text.",
            expected_version: 2,
            current_version: 2,
            current_value: null,
            status: routine ? "PENDING_APPROVAL" : "PENDING",
            skip_reason: null,
            state: "APPLIES",
            reason_code: null,
            target_active: true,
            approval_effect: "Approving publishes the proposed text.",
          },
        ],
      },
    ],
  };
}

beforeEach(() => {
  fetchReviewQueue.mockReset();
  decideGovernanceReview.mockReset();
  fetchReview.mockReset();
  vi.resetModules();
  history.replaceState(null, "", "/?review=okf_1");
});

describe("ReviewQueueScreen with an OKF import review", () => {
  it.each(["OKF_IMPORT_BATCH", "OKF_IMPORT_ROUTINE_DESCRIPTION"])(
    "mounts the import preview for %s and holds the decision until it loads",
    async (objectType) => {
      fetchReviewQueue.mockResolvedValue(queueOf([proposal(objectType)]));
      let resolve: (value: OkfImportReview) => void = () => undefined;
      fetchReview.mockReturnValue(new Promise<OkfImportReview>((done) => (resolve = done)));
      const { ReviewQueueScreen } = await import("./ReviewQueueScreen");
      const { container } = render(<ReviewQueueScreen />);

      const detail = await screen.findByLabelText("Proposal detail");
      expect(within(detail).getByText("Load the full change preview before deciding.")).toBeInTheDocument();
      expect(within(detail).queryByRole("button", { name: "Approve" })).toBeNull();
      expect(fetchReview).toHaveBeenCalledWith("okf_1", { offset: 0, limit: 25 }, expect.anything());

      resolve(preview(objectType));
      await within(detail).findByRole("list", { name: "Documents in this import" });
      expect(within(detail).getByRole("article", { name: /purpose/i })).toHaveTextContent("New text.");
      expect(await within(detail).findByRole("button", { name: "Approve" })).toBeInTheDocument();
      // The generic diff is not what this review shows.
      expect(screen.queryByText("Loading full review preview…")).toBeNull();
      expect(decideGovernanceReview).not.toHaveBeenCalled();
      await expectNoAxeViolations(container);
    },
  );

  it("names an import review in the queue and offers it as a type filter", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([proposal("OKF_IMPORT_BATCH")]));
    fetchReview.mockResolvedValue(preview("OKF_IMPORT_BATCH"));
    const { ReviewQueueScreen } = await import("./ReviewQueueScreen");
    render(<ReviewQueueScreen />);
    expect(
      await screen.findByRole("button", { name: "Descriptions imported from an edited OKF bundle" }),
    ).toBeInTheDocument();
    const filter = screen.getByLabelText("Object type");
    expect(within(filter).getByRole("option", { name: "okf import batch" })).toBeInTheDocument();
    expect(
      within(filter).getByRole("option", { name: "okf import routine description" }),
    ).toBeInTheDocument();
  });
});
