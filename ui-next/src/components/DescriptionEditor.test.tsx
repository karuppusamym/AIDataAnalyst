import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { AssetDescriptionDraftRead } from "../lib/types";

const put = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", () => ({ putJson: (...args: unknown[]) => put(...args), postJson: (...args: unknown[]) => post(...args) }));
import { DescriptionEditor } from "./DescriptionEditor";
beforeEach(() => { put.mockReset(); post.mockReset(); });
it("saves edited draft with the original text for stale-update protection, without publishing", async () => {
  put.mockResolvedValue({});
  render(<DescriptionEditor tableId="table" draft={{ id: "draft", drafted_text: "Original metadata text" } as AssetDescriptionDraftRead} />);
  fireEvent.click(screen.getByText("Edit generated draft"));
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Human reviewed description text" } });
  fireEvent.click(screen.getByRole("button", { name: "Save description revision" }));
  await waitFor(() => expect(put).toHaveBeenCalledWith("/v1/asset-description-drafts/draft", { drafted_text: "Human reviewed description text", expected_text: "Original metadata text" }));
  expect(post).not.toHaveBeenCalled();
});
