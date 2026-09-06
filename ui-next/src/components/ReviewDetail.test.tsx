import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError, decodeError } from "../lib/http";
import { ReviewDetailShell, conflictFromError } from "./ReviewDetail";

/* ---------------------------------------------------------------------------
   T18 / F05 — one review detail, and a lost race shown as the other decision.

   The property under test is end-to-end: the decision service answers a lost
   claim with a structured 409 whose `detail` is an object, the transport
   decoder preserves that object, and the shell renders the decision that won.
   Before this, `messageFromDetail` returned nothing for an object detail, so
   the payload was dropped at the decoder and the reviewer saw the bare status
   line -- which is why the first test builds its error out of a real
   `Response` rather than constructing an `ApiError` by hand.
--------------------------------------------------------------------------- */

function conflictResponse(body: unknown, status = 409): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const LOST_CLAIM = {
  detail: {
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
};

describe("the structured 409 survives the transport decoder", () => {
  it("keeps the outcome and the refreshed review the decision service sent", async () => {
    const error = await decodeError(conflictResponse(LOST_CLAIM));

    expect(error.status).toBe(409);
    // The sentence, not "409 Conflict".
    expect(error.detail).toBe("governance review is already approved");
    expect(error.details).toMatchObject({ outcome: "CONFLICT" });

    const conflict = conflictFromError(error);
    expect(conflict).not.toBeNull();
    expect(conflict?.outcome).toBe("CONFLICT");
    expect(conflict?.winning).toEqual({
      reviewId: "rq_1",
      status: "APPROVED",
      decidedBy: "priya@tenant.example",
      decidedAt: "2026-09-05T10:15:00Z",
      decisionReason: "Matches the published finance definition.",
    });
  });

  it("reports a 409 with no refreshed review as a refusal with no winner claimed", () => {
    const conflict = conflictFromError(
      new ApiError(409, "maker-checker separation is required"),
    );
    expect(conflict?.message).toBe("maker-checker separation is required");
    // Unknown, never invented: the endpoint reported no decision.
    expect(conflict?.winning).toBeNull();
    expect(conflict?.outcome).toBeNull();
  });

  it("is not a conflict when the request failed for any other reason", () => {
    expect(conflictFromError(new ApiError(500, "boom"))).toBeNull();
    expect(conflictFromError(new ApiError(403, "policy_denied"))).toBeNull();
    expect(conflictFromError(new Error("network"))).toBeNull();
  });
});

const IDENTITY = {
  subject: "Term \"MRR\"",
  target: "GLOSSARY_TERM_VERSION · UPDATE",
  status: "PENDING",
  raisedBy: "semantic_inference_agent",
  raisedAt: "2026-09-01T00:00:00Z",
  confidence: 0.82,
};

describe("ReviewDetailShell", () => {
  it("shows the decision that won, not a generic conflict message", async () => {
    const conflict = conflictFromError(await decodeError(conflictResponse(LOST_CLAIM)));

    render(<ReviewDetailShell identity={IDENTITY} conflict={conflict} />);

    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("Another reviewer decided first");
    expect(banner).toHaveTextContent("approved");
    expect(banner).toHaveTextContent("priya@tenant.example");
    expect(banner).toHaveTextContent("Matches the published finance definition.");
  });

  it("does not claim a winner when the endpoint reported none", () => {
    render(
      <ReviewDetailShell
        identity={IDENTITY}
        conflict={conflictFromError(new ApiError(409, "parsed lineage edge is already approved"))}
      />,
    );

    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("This decision was refused");
    expect(banner).toHaveTextContent("parsed lineage edge is already approved");
    expect(banner).not.toHaveTextContent("Another reviewer decided first");
    // No winner is asserted inside the banner. ("Decided by" still appears in
    // the Assignment section below, where it reads "awaiting a checker".)
    expect(within(banner).queryByText("Decided by")).not.toBeInTheDocument();
    expect(within(banner).queryByText("Recorded decision")).not.toBeInTheDocument();
  });

  it("keeps the four sections every review type owes a reviewer", () => {
    render(<ReviewDetailShell identity={IDENTITY} evidence={<p>a claim</p>} />);

    expect(screen.getByLabelText("Proposed change")).toBeInTheDocument();
    expect(screen.getByLabelText("Impact")).toBeInTheDocument();
    expect(screen.getByLabelText("Assignment")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Evidence")).getByText("a claim")).toBeInTheDocument();
    // Identity/assignment facts, not a bare object id.
    expect(screen.getByText("semantic_inference_agent")).toBeInTheDocument();
  });

  it("explains why a decision cannot be made instead of showing dead controls", () => {
    render(
      <ReviewDetailShell
        identity={IDENTITY}
        assignment={{ blockedReason: "You proposed this change." }}
        decision={{ busy: false, reasonRequiredFor: ["REJECT"], onDecide: vi.fn() }}
      />,
    );

    expect(screen.getByText("You proposed this change.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("takes a rationale in a real dialog for the verdicts that require one", async () => {
    const onDecide = vi.fn();
    render(
      <ReviewDetailShell
        identity={IDENTITY}
        decision={{ busy: false, reasonRequiredFor: ["REJECT"], onDecide }}
      />,
    );

    // Approve needs no rationale at this endpoint, so it goes straight through.
    screen.getByRole("button", { name: "Approve" }).click();
    expect(onDecide).toHaveBeenCalledWith("APPROVE", null);

    screen.getByRole("button", { name: "Reject" }).click();
    const dialog = await screen.findByRole("dialog", { name: "Reject this review" });
    expect(within(dialog).getByRole("button", { name: "Reject" })).toBeDisabled();

    fireEvent.change(within(dialog).getByRole("textbox"), {
      target: { value: "Duplicates the approved definition." },
    });
    within(dialog).getByRole("button", { name: "Reject" }).click();

    await waitFor(() =>
      expect(onDecide).toHaveBeenLastCalledWith("REJECT", "Duplicates the approved definition."),
    );
  });
});
