import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { MeRead, SearchSuggestion } from "../lib/types";
import type { Session, SessionState } from "../lib/session";
import { expectNoAxeViolations } from "../test/a11y";
import { PaletteAssetResults } from "./PaletteAssetResults";

/* ---------------------------------------------------------------------------
   The table results under the Ctrl+K palette (R11-AUD08).

   What is proved here is what a person and a session would each notice:

     1. IT ASKS LITTLE AND LATE. Nothing under two characters, nothing over the
        route's 200, one request for a run of keystrokes, and a superseded request
        is aborted; while the debounce is pending the previous answer is NOT shown
        as though it answered this query.
     2. IT ASKS ONLY WHO MAY BE ASKED. A session known to hold none of the five
        search roles is sent nothing, and told so only when no page matched either;
        one whose identity is in flight is sent nothing yet; one that can search
        but cannot open the Catalog gets "Search all" and no table rows that would
        end in a refusal.
     3. IT SAYS WHAT IT FOUND, INCLUDING NOTHING AND FAILURE.
     4. WHAT A ROW DOES is `searchTargetFor`'s answer, handed to the shell's own
        `navigate`.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

const fetchSearchSuggestions = vi.fn<
  (organizationId: string, q: string, limit?: number, signal?: AbortSignal) => Promise<SearchSuggestion[]>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchSearchSuggestions: (organizationId: string, q: string, limit?: number, signal?: AbortSignal) =>
      fetchSearchSuggestions(organizationId, q, limit, signal),
  };
});

let sessionMe: MeRead | null = null;
let sessionState: SessionState = "connected";
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: sessionState,
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

const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});

const suggestion = (name: string, id = `t-${name}`, type = "TABLE"): SearchSuggestion => ({
  text: name, object_type: type, object_id: id, display_name: name, qualified_name: name, score: 1,
});

const onOpen = vi.fn<(screen: string, params?: Record<string, string>) => void>();

const palette = (query: string, pagesMatched = false) => (
  <PaletteAssetResults query={query} pagesMatched={pagesMatched} onOpen={onOpen} />
);

const SEARCH_ALL = /Search all tables and columns for/;
const NOT_AVAILABLE = "Searching tables and columns is not available to your roles.";

/** Long enough for the 250 ms debounce, in real time: the component owns its own timer. */
const pause = () => new Promise((resolve) => setTimeout(resolve, 320));

beforeEach(() => {
  fetchSearchSuggestions.mockReset();
  fetchSearchSuggestions.mockResolvedValue([suggestion("customer"), suggestion("customer_risk_snapshot")]);
  onOpen.mockReset();
  sessionMe = asRoles("Viewer");
  sessionState = "connected";
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("what it asks, and when", () => {
  it("asks for the tables under what was typed, after a pause, with a small limit", async () => {
    render(palette("cust"));

    expect(await screen.findByRole("button", { name: /customer_risk_snapshot/ })).toBeInTheDocument();
    expect(fetchSearchSuggestions).toHaveBeenCalledTimes(1);
    expect(fetchSearchSuggestions).toHaveBeenCalledWith(ORG, "cust", 6, expect.any(AbortSignal));
  });

  it("shows nothing at all, and asks nothing, under two characters", async () => {
    const { container } = render(palette("c"));

    await pause();

    expect(container).toBeEmptyDOMElement();
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
  });

  it("shows nothing for an empty box, or one of spaces", async () => {
    const { container, rerender } = render(palette(""));
    rerender(palette("   "));

    await pause();

    expect(container).toBeEmptyDOMElement();
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
  });

  it("does not ask for a query the route would refuse", async () => {
    const { container } = render(palette("x".repeat(201)));

    await pause();

    expect(container).toBeEmptyDOMElement();
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
  });

  it("sends one request for a run of keystrokes, and none for the prefixes", async () => {
    const { rerender } = render(palette("cu"));
    rerender(palette("cus"));
    rerender(palette("cust"));

    await screen.findByRole("button", { name: /customer_risk_snapshot/ });

    expect(fetchSearchSuggestions).toHaveBeenCalledTimes(1);
    expect(fetchSearchSuggestions.mock.calls[0]![1]).toBe("cust");
  });

  it("waits for a pause: keystrokes 80 ms apart are one request, sent only after the last", async () => {
    // Synchronous re-renders (above) collapse under ANY timer, even a zero-delay one; real gaps between
    // keystrokes are what a debounce is for, so this one types at the pace of a person.
    const gap = () => new Promise((resolve) => setTimeout(resolve, 80));
    const { rerender } = render(palette("cu"));
    await gap();
    rerender(palette("cus"));
    await gap();
    rerender(palette("cust"));
    await gap();

    // 240 ms after the first keystroke and 80 after the last: nothing has been asked yet.
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();

    await screen.findByRole("button", { name: /customer_risk_snapshot/ });
    expect(fetchSearchSuggestions).toHaveBeenCalledTimes(1);
    expect(fetchSearchSuggestions.mock.calls[0]![1]).toBe("cust");
  });

  it("trims what was typed, for the request and for Search all", async () => {
    render(palette("  cust  "));

    // The request goes out after the pause; wait for it, then take Search all.
    await screen.findByRole("button", { name: /customer_risk_snapshot/ });
    expect(fetchSearchSuggestions.mock.calls[0]![1]).toBe("cust");
    fireEvent.click(screen.getByRole("button", { name: SEARCH_ALL }));
    expect(onOpen).toHaveBeenCalledWith("search", { q: "cust" });
  });

  it("aborts a request that a newer query has superseded", async () => {
    fetchSearchSuggestions.mockReturnValue(new Promise(() => undefined));
    const { rerender } = render(palette("cust"));
    await waitFor(() => expect(fetchSearchSuggestions).toHaveBeenCalledTimes(1));
    const first = fetchSearchSuggestions.mock.calls[0]![3]!;
    expect(first.aborted).toBe(false);

    rerender(palette("custom"));

    await waitFor(() => expect(first.aborted).toBe(true));
  });

  it("does not show the last answer as though it answered the new query", async () => {
    const { rerender } = render(palette("cust"));
    await screen.findByRole("button", { name: /customer_risk_snapshot/ });

    rerender(palette("order"));

    // Inside the debounce the rows for "cust" are gone, not left standing under "order".
    expect(screen.queryByRole("button", { name: /customer_risk_snapshot/ })).not.toBeInTheDocument();
    expect(screen.getByText("Searching tables…")).toBeInTheDocument();
  });
});

describe("who is asked", () => {
  it.each(["Analyst", "PlatformAdmin", "Viewer"])(
    "offers %s the tables and Search all",
    async (role) => {
      sessionMe = asRoles(role);
      render(palette("cust"));

      expect(await screen.findByRole("button", { name: /customer\s+table/i })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: SEARCH_ALL })).toBeInTheDocument();
      expect(fetchSearchSuggestions).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["Auditor", "Reviewer", "AgentDeveloper", "MetadataAdmin", "SemanticAdmin", "Operations"])(
    "asks for nothing as %s, and says searching is not available when no page matched either",
    async (role) => {
      sessionMe = asRoles(role);
      render(palette("cust", false));

      expect(await screen.findByText(NOT_AVAILABLE)).toBeInTheDocument();
      await pause();
      expect(fetchSearchSuggestions).not.toHaveBeenCalled();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    },
  );

  it("does not nag a session that may not search when a page did match", async () => {
    sessionMe = asRoles("Auditor");
    const { container } = render(palette("audit", true));

    await pause();

    expect(container).toBeEmptyDOMElement();
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
  });

  it("holds every request, and says nothing, while identity is in flight", async () => {
    sessionState = "connecting";
    sessionMe = null;
    const { container } = render(palette("cust"));

    await pause();

    expect(container).toBeEmptyDOMElement();
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
  });

  it("never asks when identity then says the session may not search", async () => {
    sessionState = "connecting";
    sessionMe = null;
    const { rerender } = render(palette("cust"));
    await pause();

    sessionState = "connected";
    sessionMe = asRoles("Auditor");
    rerender(palette("cust"));

    expect(await screen.findByText(NOT_AVAILABLE)).toBeInTheDocument();
    await pause();
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
  });

  it("asks once identity says the session may", async () => {
    sessionState = "connecting";
    sessionMe = null;
    const { rerender } = render(palette("cust"));
    await pause();
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();

    sessionState = "connected";
    sessionMe = asRoles("Viewer");
    rerender(palette("cust"));

    expect(await screen.findByRole("button", { name: /customer_risk_snapshot/ })).toBeInTheDocument();
    expect(fetchSearchSuggestions).toHaveBeenCalledTimes(1);
  });

  it("still asks when identity will not answer: the server stays the authority", async () => {
    sessionState = "disconnected";
    sessionMe = null;
    render(palette("cust"));

    expect(await screen.findByRole("button", { name: /customer_risk_snapshot/ })).toBeInTheDocument();
  });

  it.each(["DataSteward", "DataAdmin"])(
    "gives %s Search all and no table rows: they may search and the Catalog would refuse them",
    async (role) => {
      sessionMe = asRoles(role);
      render(palette("cust"));

      expect(await screen.findByRole("button", { name: SEARCH_ALL })).toBeInTheDocument();
      await pause();
      expect(fetchSearchSuggestions).not.toHaveBeenCalled();
      expect(screen.queryByText(/opens in the Catalog/)).not.toBeInTheDocument();
      expect(screen.queryByText("Searching tables…")).not.toBeInTheDocument();
    },
  );
});

describe("what it says it found", () => {
  it("lists each table under its name, saying where it opens", async () => {
    render(palette("cust"));

    await screen.findByRole("button", { name: /customer_risk_snapshot/ });
    const group = screen.getByRole("group", { name: "Tables and columns" });
    const rows = within(group).getAllByRole("button");
    expect(rows.map((row) => row.textContent)).toEqual([
      "▦customer table · opens in the Catalog→",
      "▦customer_risk_snapshot table · opens in the Catalog→",
      "⌕Search all tables and columns for “cust” Opens Search→",
    ]);
  });

  it("says while it is searching", async () => {
    fetchSearchSuggestions.mockReturnValue(new Promise(() => undefined));
    render(palette("cust"));

    expect(await screen.findByText("Searching tables…")).toBeInTheDocument();
    // Search all does not wait for an answer it does not depend on.
    expect(screen.getByRole("button", { name: SEARCH_ALL })).toBeInTheDocument();
  });

  it("says no table matches, and still offers Search all", async () => {
    fetchSearchSuggestions.mockResolvedValue([]);
    render(palette("zzzz"));

    expect(await screen.findByText("No tables match “zzzz”.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: SEARCH_ALL })).toBeInTheDocument();
  });

  it("says the search failed, and why -- a failure is not 'no tables'", async () => {
    fetchSearchSuggestions.mockRejectedValue(new ApiError(503, "suggest index warming up"));
    render(palette("cust"));

    expect(await screen.findByText("Table search failed: suggest index warming up")).toBeInTheDocument();
    expect(screen.queryByText(/No tables match/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: SEARCH_ALL })).toBeInTheDocument();
  });

  it("leaves out a suggestion that has nowhere to open, rather than offering a dead row", async () => {
    fetchSearchSuggestions.mockResolvedValue([suggestion("customer"), suggestion("customer_id", "c-1", "COLUMN")]);
    render(palette("cust"));

    await screen.findByRole("button", { name: /customer\s+table/i });
    expect(screen.queryByRole("button", { name: /customer_id/ })).not.toBeInTheDocument();
  });
});

describe("what a row does", () => {
  it("opens a table in the Catalog on that table, through the shell's own navigate", async () => {
    render(palette("cust"));

    fireEvent.click(await screen.findByRole("button", { name: /customer_risk_snapshot/ }));

    // A suggestion carries no datasource, so the link carries none.
    expect(onOpen).toHaveBeenCalledWith("catalog", { asset: "t-customer_risk_snapshot", q: "customer_risk_snapshot" });
  });

  it("opens the Search screen already running the query", async () => {
    render(palette("cust order"));

    fireEvent.click(await screen.findByRole("button", { name: SEARCH_ALL }));

    expect(onOpen).toHaveBeenCalledWith("search", { q: "cust order" });
  });
});

describe("accessibility", () => {
  it("has no detectable WCAG A/AA violation, and the group is named", async () => {
    const { container } = render(palette("cust"));
    await screen.findByRole("button", { name: /customer_risk_snapshot/ });

    await expectNoAxeViolations(container);
    expect(screen.getByRole("group", { name: "Tables and columns" })).toBeInTheDocument();
  });
});
