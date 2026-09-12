import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";

import type { StudioChangeItemRead, StudioChangeSetRead, StudioDiffRead, StudioImpactPreview } from "../lib/types";

/* ---------------------------------------------------------------------------
   Back/Forward on the screens that carried their own copy of the URL-state
   hook (review 2026-09-05, F09 · R07).

   THE DEFECT: `CatalogScreen`, `NarratedLineageScreen`, `ReviewQueueScreen`,
   `MarketplaceScreen`, `LineageRefusalScreen` and `StudioChangeSetsScreen`
   each carried a verbatim copy of the old hook -- a `useState` seeded once
   from `location.search`, subscribed to nothing. Once mounted, the copy and
   the address bar could disagree forever: Back changed the URL and the screen
   kept rendering the previous selection, because nothing listened to
   `popstate`; a same-screen link changed only the query, so the shell (keyed
   on the route id) did not remount and the private state was never re-read.

   Two assertions, because each catches a different regression:

     1. STRUCTURAL. None of the six declares a local `useUrlState` any more,
        and each imports the shared one. Re-inlining it would silently
        reintroduce all six copies of the bug -- and only one of them would
        have a behavioural test to catch it.
     2. BEHAVIOURAL. Back actually re-renders one of them. This is the
        property the structural check is a proxy for.
--------------------------------------------------------------------------- */

const DEDUPLICATED = {
  CatalogScreen: () => import("./CatalogScreen.tsx?raw"),
  NarratedLineageScreen: () => import("./NarratedLineageScreen.tsx?raw"),
  ReviewQueueScreen: () => import("./ReviewQueueScreen.tsx?raw"),
  MarketplaceScreen: () => import("./MarketplaceScreen.tsx?raw"),
  LineageRefusalScreen: () => import("./LineageRefusalScreen.tsx?raw"),
  StudioChangeSetsScreen: () => import("./StudioChangeSetsScreen.tsx?raw"),
} as const;

describe("the six screens that had inlined the URL-state hook", () => {
  for (const [name, load] of Object.entries(DEDUPLICATED)) {
    it(`${name} uses the shared hook rather than a private copy`, async () => {
      const source = (await load()).default;

      expect(source).not.toMatch(/function useUrlState\s*\(/);
      expect(source).not.toContain("new URLSearchParams(location.search)");
      expect(source).toContain('from "../lib/useUrlState"');
    });
  }
});

/* --------------------------------------------------------------------------
   The behavioural half, on the simplest of the six.
-------------------------------------------------------------------------- */

const fetchStudioChangeSets =
  vi.fn<(query: unknown, signal?: AbortSignal) => Promise<StudioChangeSetRead[]>>();
const fetchStudioChangeSetItems =
  vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioChangeItemRead[]>>();
const fetchStudioDiff = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioDiffRead>>();
const fetchStudioImpact = vi.fn<(id: string, signal?: AbortSignal) => Promise<StudioImpactPreview>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchStudioChangeSets: (query: unknown, signal?: AbortSignal) =>
      fetchStudioChangeSets(query, signal),
    fetchStudioChangeSetItems: (id: string, signal?: AbortSignal) =>
      fetchStudioChangeSetItems(id, signal),
    fetchStudioDiff: (id: string, signal?: AbortSignal) => fetchStudioDiff(id, signal),
    fetchStudioImpact: (id: string, signal?: AbortSignal) => fetchStudioImpact(id, signal),
  };
});

function changeSet(id: string, name: string): StudioChangeSetRead {
  return {
    id,
    organization_id: "org1",
    name,
    author: "priya",
    status: "TESTING",
    base_version_hash: "0".repeat(64),
    conflict_status: "CLEAN",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T09:00:00Z",
  };
}

beforeEach(() => {
  fetchStudioChangeSets.mockReset();
  fetchStudioChangeSetItems.mockReset().mockResolvedValue([]);
  fetchStudioDiff.mockReset().mockResolvedValue({ change_set_id: "cs_1", items: [] });
  fetchStudioImpact
    .mockReset()
    .mockResolvedValue({ change_set_id: "cs_1", affected_object_count: 0, affected_objects: [] });
  vi.resetModules();
  history.replaceState(null, "", "/#/studio");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Back/Forward on a de-duplicated screen", () => {
  it("re-renders the selection named by the URL after a same-screen push and Back", async () => {
    fetchStudioChangeSets.mockResolvedValue([changeSet("cs_1", "Exclude intercompany transfers")]);
    const { StudioChangeSetsScreen } = await import("./StudioChangeSetsScreen");
    const { resetLocationCacheForTests } = await import("../lib/location");

    render(<StudioChangeSetsScreen />);
    await waitFor(() =>
      expect(screen.getByText("Exclude intercompany transfers")).toBeInTheDocument(),
    );
    expect(screen.queryByLabelText(/Detail for Exclude intercompany transfers/)).not.toBeInTheDocument();

    // A same-screen drilldown: only the query changes, so the shell does not
    // remount. The old private hook never noticed.
    act(() => {
      history.pushState(null, "", "/?cs=cs_1#/studio");
      resetLocationCacheForTests();
    });
    await waitFor(() => expect(screen.getByLabelText(/Detail for Exclude intercompany transfers/)).toBeInTheDocument());

    // Back. `popstate` fires; the screen must follow the address bar.
    act(() => {
      history.replaceState(null, "", "/#/studio");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    await waitFor(() => expect(screen.queryByLabelText(/Detail for Exclude intercompany transfers/)).not.toBeInTheDocument());
  });
});

/* --------------------------------------------------------------------------
   The permalink round-trip (review 2026-09-05, F08 - T11).

   The copy actions built `origin + pathname + '?' + id` and left out the
   `#/screen` that selects the screen, so pasting a "link to the row I am
   looking at" into a fresh tab landed on the persona default with the id in
   the URL and nobody reading it. This asserts the whole loop: copy on one
   mount, open the copied URL on the next, land on the same object.
-------------------------------------------------------------------------- */

describe("permalink round-trip", () => {
  it("copies a link that reopens the same change set in a fresh mount", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });

    fetchStudioChangeSets.mockResolvedValue([changeSet("cs_1", "Exclude intercompany transfers")]);
    const { StudioChangeSetsScreen } = await import("./StudioChangeSetsScreen");
    const { resetLocationCacheForTests } = await import("../lib/location");

    history.replaceState(null, "", "/?cs=cs_1#/studio");
    resetLocationCacheForTests();
    const first = render(<StudioChangeSetsScreen />);
    const pane = await screen.findByLabelText(/Detail for Exclude intercompany transfers/);

    within(pane).getByRole("button", { name: "Copy link" }).click();
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    const copied = new URL(String(writeText.mock.calls[0]![0]));
    expect(copied.hash).toBe("#/steward/studio");
    expect(copied.searchParams.get("cs")).toBe("cs_1");

    first.unmount();

    // A fresh tab: everything the reader has is the pasted URL.
    act(() => {
      history.replaceState(null, "", `${copied.pathname}${copied.search}${copied.hash}`);
      resetLocationCacheForTests();
    });
    render(<StudioChangeSetsScreen />);

    await waitFor(() =>
      expect(screen.getByLabelText(/Detail for Exclude intercompany transfers/)).toBeInTheDocument(),
    );
  });
});
