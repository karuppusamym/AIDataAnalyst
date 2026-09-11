import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
const { fetchDiff } = vi.hoisted(() => ({fetchDiff: vi.fn()}));
vi.mock("../lib/api/governance", () => ({fetchGovernanceReviewDiff: fetchDiff}));
import { ReviewChangePreview } from "./ReviewChangePreview";
beforeEach(() => fetchDiff.mockReset());
it("does not mark a failed or unavailable diff ready and supports retry", async () => {
  fetchDiff.mockRejectedValueOnce(new Error("Not available"));
  fetchDiff.mockResolvedValueOnce({review_id: "r1", diffable: true, entries: [{field: "purpose", before: "Old", after: "New"}]});
  const ready = vi.fn();
  render(<ReviewChangePreview reviewId="r1" onReady={ready} />);
  await screen.findByText("Not available");
  expect(ready).not.toHaveBeenCalledWith("r1");
  fireEvent.click(screen.getByRole("button", {name: "Retry review preview"}));
  await screen.findByText("purpose");
  expect(ready).toHaveBeenCalledWith("r1");
});
it("rejects a response belonging to a different review", async () => {
  fetchDiff.mockResolvedValue({review_id: "other", diffable: true, entries: []});
  const ready = vi.fn();
  render(<ReviewChangePreview reviewId="r1" onReady={ready} />);
  await screen.findByText("A complete preview is unavailable for this review.");
  expect(ready).not.toHaveBeenCalledWith("r1");
});
