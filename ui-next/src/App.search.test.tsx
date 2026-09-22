import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MeRead, SearchSuggestion } from "./lib/types";
import { expectFocusStaysWithin } from "./test/a11y";

/* ---------------------------------------------------------------------------
   Search in the assembled shell (R11-AUD08): the sidebar entry, the route, and
   the Ctrl+K palette's second half -- the tables under the page names.

   `PaletteAssetResults.test.tsx` and `SearchScreen.test.tsx` prove the pieces
   in isolation. What only the shell can show is the joins: that Search is a
   screen a person can reach and share a link to, that the palette hands its
   query to it, that the palette's page list and focus trap survived having a
   second section, and that a session who may not search is asked nothing
   THROUGH THE REAL SESSION PROVIDER rather than through a stub of it.
--------------------------------------------------------------------------- */

const fetchMe = vi.fn<() => Promise<MeRead>>();
const fetchSearchSuggestions = vi.fn<
  (organizationId: string, q: string, limit?: number, signal?: AbortSignal) => Promise<SearchSuggestion[]>
>();
const fetchSearchResults = vi.fn();

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return {
    ...actual,
    fetchMe: () => fetchMe(),
    fetchSearchSuggestions: (organizationId: string, q: string, limit?: number, signal?: AbortSignal) =>
      fetchSearchSuggestions(organizationId, q, limit, signal),
    fetchSearchResults: (...args: unknown[]) => fetchSearchResults(...args),
  };
});

const me = (...roles: string[]): MeRead => ({
  principal_id: "dev-fixture-user",
  principal_type: "USER",
  organization_id: null,
  roles,
  persona: null,
  identity_provider: "DEVELOPMENT",
});

const suggestion = (name: string): SearchSuggestion => ({
  text: name, object_type: "TABLE", object_id: `t-${name}`, display_name: name, qualified_name: name, score: 1,
});

async function loadApp() {
  const { default: App } = await import("./App");
  return App;
}

async function mountAs(roles: string[], url = "/#/catalog") {
  history.replaceState(null, "", url);
  fetchMe.mockResolvedValue(me(...roles));
  const App = await loadApp();
  render(<App />);
  // Roles are known once the session has answered; the persona switcher renders from the same answer.
  await waitFor(() => expect(screen.getByTestId("persona-nav")).toBeInTheDocument());
}

function openPalette() {
  fireEvent.click(screen.getByRole("button", { name: /Jump to/ }));
  return screen.getByRole("textbox", { name: "Search pages" });
}

beforeEach(() => {
  fetchMe.mockReset();
  fetchSearchSuggestions.mockReset();
  fetchSearchSuggestions.mockResolvedValue([suggestion("customer"), suggestion("customer_risk_snapshot")]);
  fetchSearchResults.mockReset();
  fetchSearchResults.mockResolvedValue({ items: [], facets: [], total: 0, limit: 25, offset: 0 });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

describe("Search is a screen of the shell", () => {
  it("is in the sidebar, under Analyst, and opens at its canonical route", async () => {
    await mountAs(["Viewer"]);

    fireEvent.click(within(screen.getByRole("navigation", { name: "Analyst pages" })).getByRole("button", { name: "Search" }));

    expect(await screen.findByRole("region", { name: "Search" })).toBeInTheDocument();
    expect(location.hash).toBe("#/analyst/search");
    await waitFor(() => expect(screen.getByTestId("route-announcer")).toHaveTextContent("Search, Analyst"));
  });

  it("opens a shared link with its query running, and keeps `q` through the rewrite of a flat link", async () => {
    await mountAs(["Viewer"], "/?q=customer#/search");

    await waitFor(() => expect(location.hash).toBe("#/analyst/search"));
    expect(new URLSearchParams(location.search).get("q")).toBe("customer");
    await waitFor(() => expect(fetchSearchResults).toHaveBeenCalled());
    expect(fetchSearchResults.mock.calls[0]![1]).toMatchObject({ q: "customer" });
  });

  it("does not let an inherited datasource silently narrow a search that says it is global", async () => {
    await mountAs(["Viewer"], "/?q=customer&ds=ds_1#/search");

    await waitFor(() => expect(location.hash).toBe("#/analyst/search"));
    expect(new URLSearchParams(location.search).has("ds")).toBe(false);
    expect(new URLSearchParams(location.search).get("q")).toBe("customer");
  });

  it("is reachable from the palette as a page, by what a person would type", async () => {
    await mountAs(["Viewer"]);

    fireEvent.change(openPalette(), { target: { value: "typeahead" } });

    const dialog = within(screen.getByRole("dialog", { name: "Quick navigation" }));
    fireEvent.click(dialog.getByRole("button", { name: /^Search Analyst/ }));
    expect(location.hash).toBe("#/analyst/search");
  });
});

describe("the palette, past the page names", () => {
  it("offers matching tables and Search all under the pages, for a session that may search", async () => {
    await mountAs(["Viewer"]);

    fireEvent.change(openPalette(), { target: { value: "customer" } });

    const dialog = within(screen.getByRole("dialog", { name: "Quick navigation" }));
    // No page is called "customer", and the palette says so ...
    expect(dialog.getByText("No matching page")).toBeInTheDocument();
    // ... and then offers what the tables are called.
    expect(await dialog.findByRole("button", { name: /customer_risk_snapshot/ })).toBeInTheDocument();
    expect(fetchSearchSuggestions).toHaveBeenCalledWith(
      "00000000-0000-0000-0000-000000000001",
      "customer",
      6,
      expect.any(AbortSignal),
    );
    expect(dialog.getByRole("button", { name: /Search all tables and columns for “customer”/ })).toBeInTheDocument();
  });

  it("opens the Catalog on a table from the palette, and closes the palette", async () => {
    await mountAs(["Viewer"], "/#/analyst/semantics");

    fireEvent.change(openPalette(), { target: { value: "customer" } });
    const dialog = within(screen.getByRole("dialog", { name: "Quick navigation" }));
    fireEvent.click(await dialog.findByRole("button", { name: /customer_risk_snapshot/ }));

    await waitFor(() => expect(location.hash).toBe("#/analyst/catalog"));
    const params = new URLSearchParams(location.search);
    expect(params.get("asset")).toBe("t-customer_risk_snapshot");
    expect(params.get("q")).toBe("customer_risk_snapshot");
    expect(screen.queryByRole("dialog", { name: "Quick navigation" })).not.toBeInTheDocument();
  });

  it("opens the Search screen already running the query from Search all", async () => {
    await mountAs(["Viewer"]);

    fireEvent.change(openPalette(), { target: { value: "customer" } });
    const dialog = within(screen.getByRole("dialog", { name: "Quick navigation" }));
    fireEvent.click(await dialog.findByRole("button", { name: /Search all tables and columns for “customer”/ }));

    await waitFor(() => expect(location.hash).toBe("#/analyst/search"));
    expect(new URLSearchParams(location.search).get("q")).toBe("customer");
    expect(await screen.findByRole("combobox", { name: "Search by name" })).toHaveValue("customer");
    await waitFor(() => expect(fetchSearchResults).toHaveBeenCalled());
    expect(screen.queryByRole("dialog", { name: "Quick navigation" })).not.toBeInTheDocument();
  });

  it("still lists the pages a query matches, with the tables under them", async () => {
    await mountAs(["Viewer"]);

    fireEvent.change(openPalette(), { target: { value: "review queue" } });

    const dialog = within(screen.getByRole("dialog", { name: "Quick navigation" }));
    expect(dialog.getByRole("button", { name: /Review queue/ })).toBeInTheDocument();
    expect(dialog.queryByText("No matching page")).not.toBeInTheDocument();
    expect(await dialog.findByRole("button", { name: /Search all tables and columns for “review queue”/ })).toBeInTheDocument();
  });

  it("asks nothing of a session that may not search, and says so when no page matched either", async () => {
    await mountAs(["Auditor"]);

    fireEvent.change(openPalette(), { target: { value: "customer" } });

    const dialog = within(screen.getByRole("dialog", { name: "Quick navigation" }));
    expect(await dialog.findByText("Searching tables and columns is not available to your roles.")).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 350));
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
    expect(dialog.queryByRole("button", { name: /Search all/ })).not.toBeInTheDocument();
  });

  it("does not nag that same session when a page did match", async () => {
    await mountAs(["Auditor"]);

    fireEvent.change(openPalette(), { target: { value: "audit" } });

    const dialog = within(screen.getByRole("dialog", { name: "Quick navigation" }));
    expect(dialog.getByRole("button", { name: /Audit ledger/ })).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 350));
    expect(dialog.queryByText(/not available to your roles/)).not.toBeInTheDocument();
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
  });

  it("does not disturb the palette's keyboard behaviour: focus stays in the dialog through 60 Tab presses", async () => {
    await mountAs(["Viewer"]);
    const user = userEvent.setup();
    fireEvent.change(openPalette(), { target: { value: "customer" } });
    const dialog = screen.getByRole("dialog", { name: "Quick navigation" });
    await within(dialog).findByRole("button", { name: /customer_risk_snapshot/ });

    await expectFocusStaysWithin(user, dialog, 60);
  }, 20000);

  it("lets the keyboard reach a table row and activate it", async () => {
    await mountAs(["Viewer"]);
    const user = userEvent.setup();
    const input = openPalette();
    await user.type(input, "customer");
    const dialog = screen.getByRole("dialog", { name: "Quick navigation" });
    const row = await within(dialog).findByRole("button", { name: /customer\s+table/i });

    // Off the box, past no page (none matched), onto the first table.
    await user.tab();
    expect(document.activeElement).toBe(row);
    await user.keyboard("{Enter}");

    await waitFor(() => expect(location.hash).toBe("#/analyst/catalog"));
  });
});
