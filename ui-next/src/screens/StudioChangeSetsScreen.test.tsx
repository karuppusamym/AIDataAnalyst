import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { MeRead, StudioChangeItemRead, StudioChangeSetRead, StudioDiffRead, StudioImpactPreview } from "../lib/types";
import type { Session } from "../lib/session";
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

/* R11-AUD08: Submit is offered only to a session KNOWN to hold a Studio write role
   (`roleHolds`, fail-closed), where it used to be offered to everyone. These tests are about
   submitting, so they run as a DataSteward; who else sees the button is `StudioAuthoring.test.tsx`. */
let sessionMe: MeRead | null = null;
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: "connected",
      me: sessionMe,
      lapsed: false,
      lastSuccessAt: null,
      error: null,
      dataMode: "live",
      authMode: "development",
      authModeInferred: false,
      reload: () => undefined,
    }),
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
  sessionMe = {
    principal_id: "priya", principal_type: "USER", organization_id: null, roles: ["DataSteward"],
    persona: null, identity_provider: "DEVELOPMENT",
  };
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

  /* Submitting is one-way: it moves the change set to SUBMITTED, opens a review for any context
     product item, and nothing moves it back. So the button asks first, and only the confirmation
     sends the request. */
  async function openSubmitConfirmation(items: StudioChangeItemRead[] = []) {
    fetchStudioChangeSets.mockResolvedValue([CHANGE_SET]);
    fetchStudioChangeSetItems.mockResolvedValue(items);
    fetchStudioDiff.mockResolvedValue({ change_set_id: "cs_1", items: [] });
    fetchStudioImpact.mockResolvedValue({ change_set_id: "cs_1", affected_object_count: 0, affected_objects: [] });
    const StudioChangeSetsScreen = await loadScreen();
    render(<StudioChangeSetsScreen />);
    await waitFor(() => expect(screen.getByText("Exclude intercompany transfers")).toBeInTheDocument());
    screen.getByRole("button", { name: /Exclude intercompany transfers/ }).click();
    await screen.findByText(`Items (${items.length})`);

    fireEvent.click(screen.getByRole("button", { name: "Submit for review" }));
    return await screen.findByRole("dialog", { name: "Submit for review?" });
  }

  it("asks before submitting, says the submission cannot be undone, and sends nothing on the first click", async () => {
    const dialog = await openSubmitConfirmation();

    expect(dialog).toHaveTextContent("moves to SUBMITTED");
    expect(dialog).toHaveTextContent("items can no longer be added, removed or tested, and it cannot be submitted again");
    expect(dialog).toHaveTextContent("This screen has no way to withdraw a submission.");
    expect(dialog).toHaveTextContent("It has no context product item, so no governance review is opened.");
    expect(dialog).toHaveTextContent("recorded in the audit ledger");
    expect(submitStudioChangeSet).not.toHaveBeenCalled();
  });

  it("says how many reviews a submission opens, counted from the change set's context product items", async () => {
    const cp = (id: string): StudioChangeItemRead => ({
      id, organization_id: "org1", change_set_id: "cs_1", object_type: "CONTEXT_PRODUCT",
      object_id: `cp:${id}`, operation: "CREATE", before_snapshot: null, after_snapshot: null,
      diff: null, test_status: "PASSED", created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
    });
    const dialog = await openSubmitConfirmation([cp("a"), cp("b")]);

    expect(dialog).toHaveTextContent("Its 2 context product items are opened as governance reviews in the Review queue.");
  });

  it("cancelling the confirmation sends nothing and leaves the change set as it was", async () => {
    const dialog = await openSubmitConfirmation();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(submitStudioChangeSet).not.toHaveBeenCalled();
    expect(fetchStudioChangeSets).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Submit for review" })).toBeEnabled();
  });

  it("submits through the real test-gated endpoint once confirmed, and refetches on success", async () => {
    submitStudioChangeSet.mockResolvedValue({ ...CHANGE_SET, status: "SUBMITTED" });
    const dialog = await openSubmitConfirmation();

    fireEvent.click(within(dialog).getByRole("button", { name: "Submit for review" }));

    await waitFor(() => expect(submitStudioChangeSet).toHaveBeenCalledWith("cs_1", undefined));
    expect(submitStudioChangeSet).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(fetchStudioChangeSets).toHaveBeenCalledTimes(2));
  });

  it("shows the real 409 test-gate failure in the confirmation without changing status client-side", async () => {
    submitStudioChangeSet.mockRejectedValue(new ApiError(409, "1 item(s) have not passed testing"));
    const dialog = await openSubmitConfirmation();

    fireEvent.click(within(dialog).getByRole("button", { name: "Submit for review" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^1 item\(s\) have not passed testing$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(fetchStudioChangeSets).toHaveBeenCalledTimes(1);
  });
});
