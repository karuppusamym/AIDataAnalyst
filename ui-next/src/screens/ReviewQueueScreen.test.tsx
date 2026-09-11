import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { GovernanceReviewRead, MeRead, ReviewQueueRead } from "../lib/types";

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

    await waitFor(() => expect(screen.getByText(/term:mrr/)).toBeInTheDocument());
    expect(fetchReviewQueue).toHaveBeenCalledWith(
      expect.objectContaining({ status: "PENDING" }),
      expect.anything(),
    );
    expect(screen.getByText("pending review").previousSibling).toHaveTextContent("1");
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
    await waitFor(() => expect(screen.getByText(/term:mrr/)).toBeInTheDocument());

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

  it("keeps the loaded queue visible when an approval is refused", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();
    const { ApiError } = await import("../lib/http");
    decideGovernanceReview.mockRejectedValue(
      new ApiError(409, "maker-checker separation is required"),
    );
    render(<ReviewQueueScreen />);
    await waitFor(() => expect(screen.getByText(/term:mrr/)).toBeInTheDocument());

    screen.getByRole("button", { name: "Approve" }).click();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "maker-checker separation is required",
    );
    // The queue is still there. Scoped to the row's own title: the refusal
    // opens the detail on the contested review, so the subject legitimately
    // appears twice.
    expect(screen.getByText(/term:mrr/, { selector: ".prop__title" })).toBeInTheDocument();
    expect(screen.queryByText("The review queue could not be loaded")).not.toBeInTheDocument();
  });

  /* F05/T18: the decision service answers a lost claim with the review's
     refreshed state. The reviewer who lost must be told WHICH decision won --
     the old error slot printed the bare status line, because the structured
     `detail` was discarded by the decoder before it reached this screen. */
  it("shows the winning decision to the reviewer who lost the race", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();
    /* Imported AFTER the screen, and from `lib/http` rather than the mocked
       `lib/api` barrel: `vi.resetModules()` plus a memoized `importOriginal`
       can leave the barrel holding an older copy of the transport module, and
       an `ApiError` from that copy is not `instanceof` the one the screen
       graph loaded. That is a test-harness artifact, not a product one. */
    const { ApiError } = await import("../lib/http");
    decideGovernanceReview.mockRejectedValue(
      new ApiError(409, "governance review is already approved", {
        details: {
          message: "governance review is already approved",
          outcome: "CONFLICT",
          review: {
            review_id: "rq_1",
            status: "APPROVED",
            decided_by: "priya@tenant.example",
            decided_at: "2026-09-05T10:15:00Z",
            decision_reason: "Matches the published finance definition.",
          },
        },
      }),
    );
    render(<ReviewQueueScreen />);
    await waitFor(() => expect(screen.getByText(/term:mrr/)).toBeInTheDocument());

    screen.getByRole("button", { name: "Approve" }).click();

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("Another reviewer decided first");
    expect(banner).toHaveTextContent("priya@tenant.example");
    expect(banner).toHaveTextContent("Matches the published finance definition.");
    // The detail opens on the contested review, and the queue is re-read so
    // the row underneath stops claiming it is still pending.
    expect(new URLSearchParams(location.search).get("review")).toBe("rq_1");
    await waitFor(() => expect(fetchReviewQueue).toHaveBeenCalledTimes(2));
  });

  it("explains maker-checker separation instead of offering actions on your own proposal", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();
    const { SessionProvider } = await import("../lib/session");

    render(
      <SessionProvider
        fetchMe={() =>
          Promise.resolve({ principal_id: PENDING_PROPOSAL.requested_by } as MeRead)
        }
      >
        <ReviewQueueScreen />
      </SessionProvider>,
    );

    expect(
      await screen.findByText(
        "You proposed this change. Another reviewer must approve or reject it.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    expect(decideGovernanceReview).not.toHaveBeenCalled();
  });

  /* The rationale used to come from `window.prompt`, so "no reason" and "this
     browser blocks prompts" were the same value and the decision silently
     vanished. It now comes from a real dialog: the confirm button stays
     disabled until something is typed, and nothing reaches the endpoint
     before it (review 2026-09-05, F21). */
  it("will not submit a rejection until a rationale is typed into the dialog", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);
    await waitFor(() => expect(screen.getByText(/term:mrr/)).toBeInTheDocument());

    screen.getByRole("button", { name: "Reject" }).click();

    const dialog = await screen.findByRole("dialog", { name: "Reject this proposal" });
    expect(within(dialog).getByRole("button", { name: "Reject proposal" })).toBeDisabled();
    expect(decideGovernanceReview).not.toHaveBeenCalled();
  });

  it("sends the typed rationale to the real decision endpoint", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    decideGovernanceReview.mockResolvedValue({} as GovernanceReviewRead);
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);
    await waitFor(() => expect(screen.getByText(/term:mrr/)).toBeInTheDocument());

    screen.getByRole("button", { name: "Reject" }).click();
    const dialog = await screen.findByRole("dialog", { name: "Reject this proposal" });
    fireEvent.change(within(dialog).getByRole("textbox"), {
      target: { value: "Duplicates the approved definition." },
    });
    within(dialog).getByRole("button", { name: "Reject proposal" }).click();

    await waitFor(() =>
      expect(decideGovernanceReview).toHaveBeenCalledWith(
        "rq_1",
        { decision: "REJECT", reason: "Duplicates the approved definition." },
        undefined,
      ),
    );
  });

  it("traps focus inside the rejection dialog and gives it back on cancel", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);
    await waitFor(() => expect(screen.getByText(/term:mrr/)).toBeInTheDocument());

    const opener = screen.getByRole("button", { name: "Reject" });
    opener.focus();
    opener.click();

    const dialog = await screen.findByRole("dialog", { name: "Reject this proposal" });
    // Focus moved into the dialog, and the rest of the document was made
    // inert -- ARIA attributes alone never did either.
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(dialog).toHaveAttribute("aria-modal", "true");

    within(dialog).getByRole("button", { name: "Cancel" }).click();

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(opener);
  });

  it("opens a permalinkable detail panel for a focused proposal", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);
    await waitFor(() => expect(screen.getByText(/term:mrr/)).toBeInTheDocument());

    screen.getByRole("button", { name: /term:mrr/ }).click();

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
    expect(screen.queryByText(/How a quality incident propagates/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Quality propagation/)).not.toBeInTheDocument();
    expect(screen.queryByText(/inherits the incident/)).not.toBeInTheDocument();
  });

  it("renders no propagation narrative when VITE_ENABLE_PROPAGATION_LOG=0", async () => {
    vi.stubEnv("VITE_ENABLE_PROPAGATION_LOG", "0");
    const ReviewQueueScreen = await loadScreen();

    render(<ReviewQueueScreen />);

    await waitFor(() => expect(screen.getByText("Review queue")).toBeInTheDocument());
    expect(screen.queryByText(/How a quality incident propagates/)).not.toBeInTheDocument();
  });

  it("renders the propagation narrative once VITE_ENABLE_PROPAGATION_LOG=1 is set explicitly", async () => {
    vi.stubEnv("VITE_ENABLE_PROPAGATION_LOG", "1");
    const ReviewQueueScreen = await loadScreen();

    render(<ReviewQueueScreen />);

    await waitFor(() =>
      expect(screen.getByText(/How a quality incident propagates/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Quality propagation/)).toBeInTheDocument();
    /* D02: turning the gate on must not let the story pass for evidence. The
       label is on screen and in the section's accessible name. */
    expect(screen.getAllByText(/worked example/).length).toBeGreaterThan(0);
    expect(
      screen.getByLabelText(/Quality propagation.*worked example, not live evidence/),
    ).toBeInTheDocument();
  });
});


/* ---------------------------------------------------------------------------
   P1-03: glossary-specific row renderers. GLOSSARY_LINK_PROPOSAL rows now
   show the term display_name in the header and the confidence in the
   subtitle; GLOSSARY_TERM_VERSION rows show the term name and the diff
   below is unchanged (it already surfaces the definition delta from
   `diff.entries`).
--------------------------------------------------------------------------- */

const LINK_PROPOSAL: ReviewQueueRead["proposals"][number] = {
  review_id: "rq_link_1",
  organization_id: "org1",
  object_type: "GLOSSARY_LINK_PROPOSAL",
  object_id: "prop_9f",
  requested_action: "APPROVE",
  status: "PENDING",
  requested_by: "glossary_link_agent",
  decided_by: null,
  decision_reason: null,
  decided_at: null,
  created_at: "2026-09-01T00:00:00Z",
  confidence: 0.88,
  evidence: [
    { category: "GLOSSARY_LINK_PROPOSAL", claim: "term_display_name: Monthly Recurring Revenue", source: "glossary_link_proposal:prop_9f.evidence" },
    { category: "GLOSSARY_LINK_PROPOSAL", claim: "table_name: finance.mrr_daily", source: "glossary_link_proposal:prop_9f.evidence" },
    { category: "GLOSSARY_LINK_PROPOSAL", claim: "summary: name matches term synonym", source: "glossary_link_proposal:prop_9f.evidence" },
  ],
  diff: {
    review_id: "rq_link_1",
    object_type: "GLOSSARY_LINK_PROPOSAL",
    object_id: "prop_9f",
    diffable: false,
    message: "Link proposals have no field-level diff.",
  },
};

const TERM_VERSION: ReviewQueueRead["proposals"][number] = {
  review_id: "rq_ver_1",
  organization_id: "org1",
  object_type: "GLOSSARY_TERM_VERSION",
  object_id: "term:revenue",
  requested_action: "UPDATE",
  status: "PENDING",
  requested_by: "priya@tenant.example",
  decided_by: null,
  decision_reason: null,
  decided_at: null,
  created_at: "2026-09-01T00:00:00Z",
  confidence: null,
  evidence: [
    { category: "GLOSSARY_TERM_VERSION", claim: "display_name: Revenue", source: "glossary_term_version:v2" },
    { category: "GLOSSARY_TERM_VERSION", claim: "reason: reconcile with finance policy", source: "glossary_term_version:v2" },
  ],
  diff: {
    review_id: "rq_ver_1",
    object_type: "GLOSSARY_TERM_VERSION",
    object_id: "term:revenue",
    diffable: true,
    entries: [
      { field: "definition", change: "changed", before: "any inflow", after: "net inflow attributable to sales" },
    ],
  },
};

describe("ReviewQueueScreen when the queue cannot be loaded", () => {
  it("keeps a stale detail readable but blocks decisions after a refresh fails", async () => {
    history.replaceState(null, "", "/?review=rq_1");
    fetchReviewQueue.mockResolvedValueOnce(queueOf([PENDING_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);
    await screen.findByLabelText("Proposal detail");
    fetchReviewQueue.mockRejectedValue(new Error("refresh unavailable"));
    act(() => {
      history.pushState(null, "", "/?review=rq_1&status=ALL");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await screen.findByText("The review queue could not be loaded");
    expect(screen.getByText("Refresh the review queue successfully before deciding this proposal.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(decideGovernanceReview).not.toHaveBeenCalled();
  });

  /* The load-failure journey had no test at all, and it shipped a defect the
     decision-path tests could not see: the three tiles read
     `data?.byStatus[...] ?? 0`, so a first failure rendered "0 pending review"
     directly beside "The review queue could not be loaded", and a later
     failure rendered the previous load's counts with nothing marking them
     stale. "Nothing is waiting for you" and "we could not find out" are
     different answers and only one of them is safe to act on. */

  it("does not report zero pending when it could not find out", async () => {
    fetchReviewQueue.mockRejectedValue(new Error("upstream unavailable"));
    const ReviewQueueScreen = await loadScreen();

    render(<ReviewQueueScreen />);

    await screen.findByText("The review queue could not be loaded");
    expect(screen.getByText("upstream unavailable")).toBeInTheDocument();
    expect(screen.getByText("pending review").previousSibling).toHaveTextContent("—");
    expect(screen.getByText("approved").previousSibling).toHaveTextContent("—");
    expect(screen.getByText("rejected").previousSibling).toHaveTextContent("—");
  });

  it("does not keep showing the last good counts when a refresh fails", async () => {
    fetchReviewQueue.mockResolvedValueOnce(queueOf([PENDING_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();
    const { rerender } = render(<ReviewQueueScreen />);
    await waitFor(() =>
      expect(screen.getByText("pending review").previousSibling).toHaveTextContent("1"),
    );

    fetchReviewQueue.mockRejectedValue(new Error("upstream unavailable"));
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "ALL" } });
    rerender(<ReviewQueueScreen />);

    await screen.findByText("The review queue could not be loaded");
    expect(screen.getByText("pending review").previousSibling).toHaveTextContent("—");
  });

  it("retries the load, and restores the counts when it succeeds", async () => {
    fetchReviewQueue.mockRejectedValueOnce(new Error("upstream unavailable"));
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);
    await screen.findByText("The review queue could not be loaded");

    fetchReviewQueue.mockResolvedValue(queueOf([PENDING_PROPOSAL]));
    fireEvent.click(screen.getByRole("button", { name: /Retry|Try again/i }));

    await waitFor(() => expect(screen.getByText(/term:mrr/)).toBeInTheDocument());
    expect(screen.getByText("pending review").previousSibling).toHaveTextContent("1");
    expect(
      screen.queryByText("The review queue could not be loaded"),
    ).not.toBeInTheDocument();
  });
});

describe("ReviewQueueScreen glossary row renderers (P1-03)", () => {
  it("GLOSSARY_LINK_PROPOSAL row shows the term display_name in the title and confidence in the subtitle", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([LINK_PROPOSAL]));
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);

    // Title carries the term name, not the raw proposal uuid. Scoped to the
    // row's title button: a single-item queue auto-opens its detail panel, so
    // the name legitimately appears twice.
    await waitFor(() =>
      expect(
        screen.getByText(/Monthly Recurring Revenue/, { selector: ".prop__title" }),
      ).toBeInTheDocument(),
    );
    // Subtitle carries the target asset and the confidence percentage. Same
    // duplication as the title: the row and the auto-opened panel both show
    // them, so this asserts the row's own subtitle.
    expect(
      screen.getByText(/finance\.mrr_daily/, { selector: ".prop__extra" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/confidence 88%/, { selector: ".prop__extra" })).toBeInTheDocument();
  });

  it("GLOSSARY_TERM_VERSION row shows the term name in the title and the definition diff below", async () => {
    fetchReviewQueue.mockResolvedValue(queueOf([TERM_VERSION]));
    const ReviewQueueScreen = await loadScreen();
    render(<ReviewQueueScreen />);

    await waitFor(() =>
      expect(screen.getByText(/Revenue/, { selector: ".prop__title" })).toBeInTheDocument(),
    );
    // The diff row is emitted by the existing DiffEntries component.
    expect(screen.getByText(/definition/)).toBeInTheDocument();
    expect(screen.getByText(/net inflow attributable to sales/)).toBeInTheDocument();
  });
});
