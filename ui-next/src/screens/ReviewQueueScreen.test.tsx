import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { GovernanceReviewRead, ReviewQueueRead } from "../lib/types";

/* ---------------------------------------------------------------------------
   UX-15: this screen was rewired off `fetchReviewBatch` (a fixture standing
   in for a read model that had not shipped) onto UX-17's real
   `GET /v1/governance/reviews/queue` -- so these tests now mock the API
   boundary (`../lib/api`), the same pattern `EvidencePane.test.tsx`/
   `App.test.tsx` already establish, rather than `../lib/fixtures`.

   AT-D4: `PropagationLog`'s "Why orders_raw is currently blocked" narrative
   is hard-coded, not backed by any endpoint (see the comment above
   `PROPAGATION_LOG_ENABLED` in `ReviewQueueScreen.tsx`), so it must not be
   reachable by a real user until `VITE_ENABLE_PROPAGATION_LOG` is turned on.
   Those three cases are preserved byte-for-byte in intent from the pre-UX-15
   version of this file.
--------------------------------------------------------------------------- */

const fetchReviewQueue = vi.fn<
  (query: unknown, signal?: AbortSignal) => Promise<ReviewQueueRead>
>();
const decideGovernanceReview = vi.fn<
  (reviewId: string, body: unknown, signal?: AbortSignal) => Promise<GovernanceReviewRead>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchReviewQueue: (query: unknown, signal?: AbortSignal) => fetchReviewQueue(query, signal),
    decideGovernanceReview: (reviewId: string, body: unknown, signal?: AbortSignal) =>
      decideGovernanceReview(reviewId, body, signal),
  };
});

function queueOf(proposals: ReviewQueueRead["proposals"]): ReviewQueueRead {
  const by_status: Record<string, number> = {};
  for (const p of proposals) by_status[p.status] = (by_status[p.status] ?? 0) + 1;
  return {
    organization_id: "org1",
    status_filter: "PENDING",
    object_type_filter: null,
    inference_run_id_filter: null,
    generated_at: "2026-09-02T00:00:00Z",
    proposals,
    total_proposals: proposals.length,
    by_status,
    by_object_type: {},
    diffable_count: proposals.filter((p) => p.diff.diffable).length,
  };
}

const PENDING_PROPOSAL: ReviewQueueRead["proposals"][number] = {
  review_id: "rq_1",
  organization_id: "org1",
  object_type: "GLOSSARY_TERM_VERSION",
  object_id: "term:mrr",
  requested_action: "UPDATE",
  status: "PENDING",
  requested_by: "semantic_inference_agent",
  decided_by: null,
  decision_reason: null,
  decided_at: null,
  created_at: "2026-09-01T00:00:00Z",
  confidence: 0.82,
  evidence: [{ category: "BUSINESS_MEANING", claim: "Two domains disagree", source: "semantic_diff.py" }],
  diff: {
    review_id: "rq_1",
    object_type: "GLOSSARY_TERM_VERSION",
    object_id: "term:mrr",
    diffable: true,
    entries: [{ field: "scope", change: "added", before: null, after: "finance.mrr" }],
  },
};

async function loadScreen() {
  const { ReviewQueueScreen } = await import("./ReviewQueueScreen");
  return ReviewQueueScreen;
}

beforeEach(() => {
  fetchReviewQueue.mockReset();
  decideGovernanceReview.mockReset();
  fetchReviewQueue.mockResolvedValue(queueOf([]));
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("ReviewQueueScreen against the real UX-17 read model", () => {
  it("fetches PENDING by default and renders the pending tile count", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();

    render(<ReviewQueueScreen />);

    await waitFor(() => expect(screen.getByText("term:mrr")).toBeInTheDocument());
    expect(fetchReviewQueue).toHaveBeenCalledWith(
      expect.objectContaining({ status: "PENDING" }),
      expect.anything(),
    );
    expect(screen.getByText("pending your judgment").previousSibling).toHaveTextContent("1");
  });

  it("re-fetches with the new status when the URL-held filter changes", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([]));
    const ReviewQueueScreen = await loadScreen();
    const { rerender } = render(<ReviewQueueScreen />);
    await waitFor(() => expect(fetchReviewQueue).toHaveBeenCalledTimes(1));

    const select = screen.getByLabelText("Status");
    fireEvent.change(select, { target: { value: "ALL" } });
    rerender(<ReviewQueueScreen />);

    await waitFor(() =>
      expect(fetchReviewQueue).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: null }),
        expect.anything(),
      ),
    );
    expect(new URLSearchParams(location.search).get("status")).toBe("ALL");
  });

  it("calls the real decision endpoint on approve and refetches", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    decideGovernanceReview.mockResolvedValue({
      id: "rq_1",
      organization_id: "org1",
      object_type: "GLOSSARY_TERM_VERSION",
      object_id: "term:mrr",
      requested_action: "UPDATE",
      status: "APPROVED",
      requested_by: "semantic_inference_agent",
      decided_by: "dev-fixture-user",
      decision_reason: null,
      decided_at: "2026-09-02T00:00:00Z",
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-02T00:00:00Z",
    });
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);
    await waitFor(() => expect(screen.getByText("term:mrr")).toBeInTheDocument());

    screen.getByRole("button", { name: "Approve" }).click();

    await waitFor(() =>
      expect(decideGovernanceReview).toHaveBeenCalledWith(
        "rq_1",
        { decision: "APPROVE", reason: null },
        undefined,
      ),
    );
    await waitFor(() => expect(fetchReviewQueue).toHaveBeenCalledTimes(2));
  });

  it("requires a reason before calling the endpoint on reject, and skips the call if none is given", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    vi.spyOn(window, "prompt").mockReturnValue(null);
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);
    await waitFor(() => expect(screen.getByText("term:mrr")).toBeInTheDocument());

    screen.getByRole("button", { name: "Reject" }).click();

    expect(window.prompt).toHaveBeenCalled();
    expect(decideGovernanceReview).not.toHaveBeenCalled();
  });

  it("opens a permalinkable detail panel for a focused proposal", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);
    await waitFor(() => expect(screen.getByText("term:mrr")).toBeInTheDocument());

    screen.getByRole("button", { name: "term:mrr" }).click();

    const panel = await screen.findByLabelText("Proposal detail");
    expect(within(panel).getByText("Two domains disagree")).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("review")).toBe("rq_1");
  });
});

describe("ReviewQueueScreen's PropagationLog gate (AT-D4, default off)", () => {
  it("renders no propagation narrative when VITE_ENABLE_PROPAGATION_LOG is unset", async () => {
    const ReviewQueueScreen = await loadScreen();

    render(<ReviewQueueScreen />);

    await waitFor(() => expect(screen.getByText("Review queue")).toBeInTheDocument());
    expect(
      screen.queryByText("Why orders_raw is currently blocked"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Quality propagation/)).not.toBeInTheDocument();
    expect(screen.queryByText(/inherits the incident/)).not.toBeInTheDocument();
  });

  it("renders no propagation narrative when VITE_ENABLE_PROPAGATION_LOG=0", async () => {
    vi.stubEnv("VITE_ENABLE_PROPAGATION_LOG", "0");
    const ReviewQueueScreen = await loadScreen();

    render(<ReviewQueueScreen />);

    await waitFor(() => expect(screen.getByText("Review queue")).toBeInTheDocument());
    expect(
      screen.queryByText("Why orders_raw is currently blocked"),
    ).not.toBeInTheDocument();
  });

  it("renders the propagation narrative once VITE_ENABLE_PROPAGATION_LOG=1 is set explicitly", async () => {
    vi.stubEnv("VITE_ENABLE_PROPAGATION_LOG", "1");
    const ReviewQueueScreen = await loadScreen();

    render(<ReviewQueueScreen />);

    await waitFor(() =>
      expect(screen.getByText("Why orders_raw is currently blocked")).toBeInTheDocument(),
    );
    expect(screen.getByText(/Quality propagation/)).toBeInTheDocument();
  });
});
