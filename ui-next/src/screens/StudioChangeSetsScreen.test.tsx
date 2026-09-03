import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { StudioChangeItemRead, StudioChangeSetRead, StudioDiffRead, StudioImpactPreview } from "../lib/types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   UX-15: Studio change sets against the real, already-merged `studio_api.py`
   (module 19 / ST-A7) -- not a stub. Mocks the API boundary the same way
   every other UX-15 screen test does.
--------------------------------------------------------------------------- */

const fetchStudioChangeSets = vi.fn<(query: unknown, signal?: AbortSignal) => Promise<StudioChangeSetRead[]>>();
const fetchStudioChangeSetItems = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioChangeItemRead[]>>();
const fetchStudioDiff = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioDiffRead>>();
const fetchStudioImpact = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioImpactPreview>>();
const submitStudioChangeSet = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioChangeSetRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchStudioChangeSets: (query: unknown, signal?: AbortSignal) => fetchStudioChangeSets(query, signal),
    fetchStudioChangeSetItems: (id: string, signal?: AbortSignal) => fetchStudioChangeSetItems(id, signal),
    fetchStudioDiff: (id: string, signal?: AbortSignal) => fetchStudioDiff(id, signal),
    fetchStudioImpact: (id: string, signal?: AbortSignal) => fetchStudioImpact(id, signal),
    submitStudioChangeSet: (id: string, signal?: AbortSignal) => submitStudioChangeSet(id, signal),
  };
});

const CHANGE_SET: StudioChangeSetRead = {
  id: "cs_1", organization_id: "org1", name: "Exclude intercompany transfers", author: "priya",
  status: "TESTING", base_version_hash: "0".repeat(64), conflict_status: "CLEAN",
  created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T09:00:00Z",
};

async function loadScreen() {
  const { StudioChangeSetsScreen } = await import("./StudioChangeSetsScreen");
  return StudioChangeSetsScreen;
}

beforeEach(() => {
  fetchStudioChangeSets.mockReset();
  fetchStudioChangeSetItems.mockReset();
  fetchStudioDiff.mockReset();
  fetchStudioImpact.mockReset();
  submitStudioChangeSet.mockReset();
  fetchStudioChangeSets.mockResolvedValue([]);
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("StudioChangeSetsScreen against the real studio_api.py", () => {
  it("lists change sets from the real endpoint", async () => {
    fetchStudioChangeSets.mockResolvedValue([CHANGE_SET]);
    const StudioChangeSetsScreen = await loadScreen();

    render(<StudioChangeSetsScreen />);

    await waitFor(() => expect(screen.getByText("Exclude intercompany transfers")).toBeInTheDocument());
    expect(fetchStudioChangeSets).toHaveBeenCalledWith({ status: null, limit: 200 }, expect.anything());
  });

  it("selecting a change set loads its real items, diff and impact together", async () => {
    fetchStudioChangeSets.mockResolvedValue([CHANGE_SET]);
    fetchStudioChangeSetItems.mockResolvedValue([
      {
        id: "item_1", organization_id: "org1", change_set_id: "cs_1", object_type: "METRIC",
        object_id: "metric:revenue", operation: "UPDATE", before_snapshot: null, after_snapshot: null,
        diff: null, test_status: "PASSED", created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
      },
    ]);
    fetchStudioDiff.mockResolvedValue({ change_set_id: "cs_1", items: [] });
    fetchStudioImpact.mockResolvedValue({ change_set_id: "cs_1", affected_object_count: 2, affected_objects: [{ object_id: "metric:revenue" }] });
    const StudioChangeSetsScreen = await loadScreen();
    render(<StudioChangeSetsScreen />);
    await waitFor(() => expect(screen.getByText("Exclude intercompany transfers")).toBeInTheDocument());

    screen.getByRole("button", { name: /Exclude intercompany transfers/ }).click();

    await waitFor(() => expect(fetchStudioChangeSetItems).toHaveBeenCalledWith("cs_1", expect.anything()));
    expect(fetchStudioDiff).toHaveBeenCalledWith("cs_1", expect.anything());
    expect(fetchStudioImpact).toHaveBeenCalledWith("cs_1", expect.anything());
    expect(await screen.findByText("Items (1)")).toBeInTheDocument();
    expect(screen.getByText("Impact (2 affected)")).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("cs")).toBe("cs_1");
  });

  it("submits through the real test-gated endpoint and refetches on success", async () => {
    fetchStudioChangeSets.mockResolvedValue([CHANGE_SET]);
    fetchStudioChangeSetItems.mockResolvedValue([]);
    fetchStudioDiff.mockResolvedValue({ change_set_id: "cs_1", items: [] });
    fetchStudioImpact.mockResolvedValue({ change_set_id: "cs_1", affected_object_count: 0, affected_objects: [] });
    submitStudioChangeSet.mockResolvedValue({ ...CHANGE_SET, status: "SUBMITTED" });
    const StudioChangeSetsScreen = await loadScreen();
    render(<StudioChangeSetsScreen />);
    await waitFor(() => expect(screen.getByText("Exclude intercompany transfers")).toBeInTheDocument());
    screen.getByRole("button", { name: /Exclude intercompany transfers/ }).click();
    await screen.findByText("Items (0)");

    screen.getByRole("button", { name: "Submit for review" }).click();

    await waitFor(() => expect(submitStudioChangeSet).toHaveBeenCalledWith("cs_1", undefined));
    await waitFor(() => expect(fetchStudioChangeSets).toHaveBeenCalledTimes(2));
  });

  it("shows the real 409 test-gate failure without changing status client-side", async () => {
    fetchStudioChangeSets.mockResolvedValue([CHANGE_SET]);
    fetchStudioChangeSetItems.mockResolvedValue([]);
    fetchStudioDiff.mockResolvedValue({ change_set_id: "cs_1", items: [] });
    fetchStudioImpact.mockResolvedValue({ change_set_id: "cs_1", affected_object_count: 0, affected_objects: [] });
    submitStudioChangeSet.mockRejectedValue(new ApiError(409, "1 item(s) have not passed testing"));
    const StudioChangeSetsScreen = await loadScreen();
    render(<StudioChangeSetsScreen />);
    await waitFor(() => expect(screen.getByText("Exclude intercompany transfers")).toBeInTheDocument());
    screen.getByRole("button", { name: /Exclude intercompany transfers/ }).click();
    await screen.findByText("Items (0)");

    screen.getByRole("button", { name: "Submit for review" }).click();

    expect(await screen.findByText("1 item(s) have not passed testing")).toBeInTheDocument();
  });
});
