import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReviewBatch } from "../lib/fixtures";

/* ---------------------------------------------------------------------------
   AT-D4: `PropagationLog`'s "Why orders_raw is currently blocked" narrative
   is hard-coded, not backed by any endpoint (see the comment above
   `PROPAGATION_LOG_ENABLED` in `ReviewQueueScreen.tsx`), so it must not be
   reachable by a real user until `VITE_ENABLE_PROPAGATION_LOG` is turned on
   -- which happens only once AT-11 (or an equivalent real read model) ships
   something to show. These assert the rendered DOM directly, the same way
   `App.test.tsx` proves persona gating: a hidden-but-mounted node would still
   be a fake one.
--------------------------------------------------------------------------- */

const fixtureBatch: ReviewBatch = {
  runLabel: "finance-revenue · semantic validation",
  finishedAgo: "9 minutes ago",
  passed: 44,
  threshold: 0.9,
  proposals: [],
};

const fetchReviewBatch = vi.fn<() => Promise<ReviewBatch>>();
vi.mock("../lib/fixtures", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/fixtures")>();
  return {
    ...actual,
    fetchReviewBatch: () => fetchReviewBatch(),
  };
});

async function loadScreen() {
  const { ReviewQueueScreen } = await import("./ReviewQueueScreen");
  return ReviewQueueScreen;
}

beforeEach(() => {
  fetchReviewBatch.mockReset();
  fetchReviewBatch.mockResolvedValue(fixtureBatch);
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllEnvs();
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
