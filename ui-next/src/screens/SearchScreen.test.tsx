import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { SearchPageRead, SearchQuery, SearchResultRead } from "../lib/api";
import { resetLocationCacheForTests } from "../lib/location";
import type { MeRead, SearchSuggestion } from "../lib/types";
import type { Session, SessionState } from "../lib/session";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";
import { SearchScreen } from "./SearchScreen";

/* ---------------------------------------------------------------------------
   Search (R11-AUD08) -- `GET /v1/search` and `/v1/search/suggest`.

   The properties, and why each was a real way to get this screen wrong:

     1. WHO IS ASKED. The five roles the routes admit search; a session known to
        hold none is asked NOTHING -- not the search, not the typeahead -- and is
        told search is not available to its roles; a session whose identity is
        still in flight is asked nothing YET.
     2. THE ANSWER IS SHOWN AS THE API GAVE IT: the total, the per-type facets,
        the ranked page grouped by type, the API's own score. Nothing derived.
     3. A COLUMN HAS NOWHERE TO GO, and the screen says so instead of inventing a
        table: the answer names a column and not its table, so five `customer_id`
        rows are five identical rows.
     4. A LINK IS OFFERED ONLY WHERE IT WORKS: a table opens in the Catalog, and a
        session the Catalog refuses gets the results without links and a sentence.
     5. THE QUERY IS IN THE URL and runs on submit; the typeahead is a `<datalist>`.
     6. EVERY STATE IS SAID: empty, failed, a page past the end, no query yet.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

const fetchSearchResults = vi.fn<
  (organizationId: string, query: SearchQuery, signal?: AbortSignal) => Promise<SearchPageRead>
>();
const fetchSearchSuggestions = vi.fn<
  (organizationId: string, q: string, limit?: number, signal?: AbortSignal) => Promise<SearchSuggestion[]>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchSearchResults: (organizationId: string, query: SearchQuery, signal?: AbortSignal) =>
      fetchSearchResults(organizationId, query, signal),
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

type HitInput = Pick<SearchResultRead, "object_type" | "object_id" | "display_name"> & Partial<SearchResultRead>;

/** A hit exactly as `GET /v1/search` shapes it: what a live probe of the running API returned. */
const hit = (input: HitInput): SearchResultRead => ({
  qualified_name: input.display_name,
  description: null,
  score: 1,
  evidence: {
    object_type: input.object_type,
    object_id: input.object_id,
    display_name: input.display_name,
    final_score: 1,
    fusion_method: "lexical",
    factors: [{ signal: "lexical", raw_score: 1, weight: 1, weighted_score: 1, rank: null }],
    graph_expansion_path: [],
    source_signals: ["lexical"],
    metadata: {},
  },
  datasource_id: input.object_type === "TABLE" ? "ds-1" : null,
  datasource_name: null,
  domain_name: null,
  ...input,
});

const TABLE = hit({ object_type: "TABLE", object_id: "t-1", display_name: "customer", datasource_id: "ds-1" });
const TABLE_2 = hit({ object_type: "TABLE", object_id: "t-2", display_name: "customer_risk_snapshot", datasource_id: "ds-2" });
const COLUMN = (id: string, name = "customer_id") => hit({ object_type: "COLUMN", object_id: id, display_name: name });

const page = (items: SearchResultRead[], overrides: Partial<SearchPageRead> = {}): SearchPageRead => {
  const counts = new Map<string, number>();
  for (const item of items) counts.set(item.object_type, (counts.get(item.object_type) ?? 0) + 1);
  return {
    items,
    facets: [...counts.entries()].sort().map(([value, count]) => ({ field: "object_type", value, count })),
    total: items.length,
    limit: 25,
    offset: 0,
    ...overrides,
  };
};

const suggestion = (name: string, id = `s-${name}`): SearchSuggestion => ({
  text: name, object_type: "TABLE", object_id: id, display_name: name, qualified_name: name, score: 1,
});

const NOT_AVAILABLE =
  "Search is not available to your roles: only sessions holding Analyst, DataAdmin, DataSteward, PlatformAdmin or Viewer can search tables and columns.";

function mount(url = "/?q=customer#/analyst/search") {
  window.history.replaceState(null, "", url);
  resetLocationCacheForTests();
  return render(<SearchScreen />);
}

// An `<input type="search" list=...>` is a combobox to assistive technology (HTML-AAM: a text-like input with
// a `list` attribute), which is what a typeahead is; the plain "searchbox" role is for one with no list.
const box = () => screen.getByRole("combobox", { name: "Search by name" });

beforeEach(() => {
  fetchSearchResults.mockReset();
  fetchSearchResults.mockResolvedValue(page([TABLE, TABLE_2, COLUMN("c-1"), COLUMN("c-2"), COLUMN("c-3", "customer_name")]));
  fetchSearchSuggestions.mockReset();
  fetchSearchSuggestions.mockResolvedValue([]);
  sessionMe = null;
  sessionState = "connected";
  window.history.replaceState(null, "", "/");
  resetLocationCacheForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("who is asked", () => {
  it.each(["Analyst", "DataAdmin", "DataSteward", "PlatformAdmin", "Viewer"])(
    "searches for %s, with the organization, the query and the API's own page size",
    async (role) => {
      sessionMe = asRoles(role);
      mount();

      expect(await screen.findByText(/5 matches for “customer”/)).toBeInTheDocument();
      expect(fetchSearchResults).toHaveBeenCalledWith(
        ORG,
        { q: "customer", objectType: null, limit: 25, offset: 0 },
        expect.any(AbortSignal),
      );
      expect(screen.queryByText(NOT_AVAILABLE)).not.toBeInTheDocument();
    },
  );

  it.each(["Auditor", "Reviewer", "AgentDeveloper", "ToolDeveloper", "MetadataAdmin", "SemanticAdmin", "Operations"])(
    "asks for nothing as %s, says search is not available, and offers no box to type in",
    async (role) => {
      sessionMe = asRoles(role);
      mount(); // the URL even carries a query: it is still not sent

      expect(await screen.findByText(NOT_AVAILABLE)).toBeInTheDocument();
      expect(fetchSearchResults).not.toHaveBeenCalled();
      expect(fetchSearchSuggestions).not.toHaveBeenCalled();
      expect(screen.queryByRole("combobox", { name: "Search by name" })).not.toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );

  it("does not send the typeahead either when a session that may not search types", async () => {
    sessionMe = asRoles("Auditor");
    mount("/#/analyst/search");
    await screen.findByText(NOT_AVAILABLE);

    await new Promise((resolve) => setTimeout(resolve, 350));

    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
  });

  it("holds everything while identity is in flight", async () => {
    sessionState = "connecting";
    mount();

    expect(await screen.findByText("Loading search…")).toBeInTheDocument();
    expect(fetchSearchResults).not.toHaveBeenCalled();
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
    expect(screen.queryByText(NOT_AVAILABLE)).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Search by name" })).not.toBeInTheDocument();
  });

  it("never sends the search when identity then says the session may not", async () => {
    sessionState = "connecting";
    window.history.replaceState(null, "", "/?q=customer#/analyst/search");
    resetLocationCacheForTests();
    const view = render(<SearchScreen />);
    await screen.findByText("Loading search…");

    sessionState = "connected";
    sessionMe = asRoles("Auditor");
    view.rerender(<SearchScreen />);

    expect(await screen.findByText(NOT_AVAILABLE)).toBeInTheDocument();
    expect(fetchSearchResults).not.toHaveBeenCalled();
  });

  it("sends the search once identity says the session may", async () => {
    sessionState = "connecting";
    window.history.replaceState(null, "", "/?q=customer#/analyst/search");
    resetLocationCacheForTests();
    const view = render(<SearchScreen />);
    await screen.findByText("Loading search…");

    sessionState = "connected";
    sessionMe = asRoles("Analyst");
    view.rerender(<SearchScreen />);

    expect(await screen.findByText(/5 matches for “customer”/)).toBeInTheDocument();
    expect(fetchSearchResults).toHaveBeenCalledTimes(1);
  });

  it("still searches when identity will not answer: the server stays the authority", async () => {
    sessionState = "disconnected";
    mount();

    expect(await screen.findByText(/5 matches for “customer”/)).toBeInTheDocument();
    expect(fetchSearchResults).toHaveBeenCalledTimes(1);
  });
});

describe("before there is a query", () => {
  it("says what to do, and asks for nothing", async () => {
    sessionMe = asRoles("Viewer");
    mount("/#/analyst/search");

    expect(await screen.findByText(/Type part of a table or column name and press Search/)).toBeInTheDocument();
    expect(fetchSearchResults).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
  });

  it("says what is searched: names, not descriptions", async () => {
    sessionMe = asRoles("Viewer");
    mount("/#/analyst/search");

    expect(await screen.findByText(/Find a table or a column by name across every source you can read/)).toBeInTheDocument();
    expect(screen.getByText(/descriptions are not searched/)).toBeInTheDocument();
  });
});

describe("the answer", () => {
  it("says how many matched and how they split by type, as the API counted them", async () => {
    sessionMe = asRoles("Viewer");
    mount();

    expect(await screen.findByText("5 matches for “customer”: 3 columns, 2 tables")).toBeInTheDocument();
  });

  it("groups the ranked page by type, headed with the API's counts", async () => {
    sessionMe = asRoles("Viewer");
    mount();

    const tables = (await screen.findByRole("heading", { name: /^Tables/ })).closest("section")!;
    expect(within(tables).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      expect.stringContaining("customer"),
      expect.stringContaining("customer_risk_snapshot"),
    ]);
    const columns = screen.getByRole("heading", { name: /^Columns/ }).closest("section")!;
    expect(within(columns).getAllByRole("listitem")).toHaveLength(3);
    // The tables come first because that is where the ranked page put them.
    const headings = screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);
    expect(headings).toEqual(["Tables 2", "Columns 3"]);
  });

  it("says how many of a type's hits a page holds when it does not hold them all", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockResolvedValue({
      ...page([TABLE, COLUMN("c-1"), COLUMN("c-2")]),
      total: 40,
      facets: [
        { field: "object_type", value: "COLUMN", count: 33 },
        { field: "object_type", value: "TABLE", count: 7 },
      ],
    });
    mount();

    expect(await screen.findByRole("heading", { name: "Tables 1 of 7 on this page" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Columns 2 of 33 on this page" })).toBeInTheDocument();
    expect(screen.getByText("40 matches for “customer”: 33 columns, 7 tables")).toBeInTheDocument();
  });

  it("shows the API's relevance as a number and never as a percentage of its own", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockResolvedValue(page([hit({ object_type: "TABLE", object_id: "t-1", display_name: "customer", score: 0.5 })]));
    mount();

    expect(await screen.findByText("relevance 0.50")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("shows a name, its qualified name when it differs, and the datasource name when the API gives one", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockResolvedValue(
      page([
        hit({
          object_type: "TABLE",
          object_id: "t-1",
          display_name: "customer",
          qualified_name: "core.customer",
          datasource_name: "snowflake_prod",
          description: "One row per customer",
        }),
      ]),
    );
    mount();

    const item = (await screen.findByRole("listitem")) as HTMLElement;
    expect(item).toHaveTextContent("core.customer");
    expect(item).toHaveTextContent("snowflake_prod");
    expect(item).toHaveTextContent("One row per customer");
  });

  it("shows a type it has never seen under its own heading rather than dropping it", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockResolvedValue(
      page([hit({ object_type: "GLOSSARY_TERM", object_id: "g-1", display_name: "Customer" })]),
    );
    mount();

    expect(await screen.findByRole("heading", { name: /^Glossary terms/ })).toBeInTheDocument();
    expect(screen.getByText("1 match for “customer”: 1 glossary term")).toBeInTheDocument();
  });
});

describe("where a result opens", () => {
  it("opens a table in the Catalog, selected, filtered to its name and its source", async () => {
    sessionMe = asRoles("Analyst");
    mount();

    fireEvent.click(await screen.findByRole("button", { name: "Open customer_risk_snapshot in the Catalog" }));

    expect(location.hash).toBe("#/analyst/catalog");
    const params = new URLSearchParams(location.search);
    expect(params.get("asset")).toBe("t-2");
    expect(params.get("q")).toBe("customer_risk_snapshot");
    expect(params.get("ds")).toBe("ds-2");
    // Nothing of this screen's own query leaks into the Catalog's.
    expect(params.has("offset")).toBe(false);
  });

  it("gives a column NO link, and says why once, in words -- it names a column and not its table", async () => {
    sessionMe = asRoles("Analyst");
    mount();

    const columns = (await screen.findByRole("heading", { name: /^Columns/ })).closest("section")!;
    expect(within(columns).queryByRole("button")).not.toBeInTheDocument();
    expect(
      screen.getAllByText(/The search names a column but does not say which table it belongs to, so a column result cannot open its table from here\./),
    ).toHaveLength(1);
    // Five identical names would be indistinguishable: the column's id tells them apart.
    expect(within(columns).getByText("c-1")).toBeInTheDocument();
    expect(within(columns).getByText("c-2")).toBeInTheDocument();
  });

  it("opens a column's table when the hit says which it is", async () => {
    sessionMe = asRoles("Analyst");
    const withTable = hit({ object_type: "COLUMN", object_id: "c-9", display_name: "customer_id", datasource_id: "ds-3" });
    withTable.evidence.metadata = { table_id: "t-9", column_id: "c-9" };
    fetchSearchResults.mockResolvedValue(page([withTable]));
    mount();

    fireEvent.click(await screen.findByRole("button", { name: "Open the table of column customer_id in the Catalog" }));

    expect(location.hash).toBe("#/analyst/catalog");
    expect(new URLSearchParams(location.search).get("asset")).toBe("t-9");
    expect(new URLSearchParams(location.search).get("ds")).toBe("ds-3");
    // ... and the sentence about a column with nowhere to go is not said, because this one has somewhere.
    expect(screen.queryByText(/cannot open its table from here/)).not.toBeInTheDocument();
  });

  it.each(["DataSteward", "DataAdmin"])(
    "lists the results for %s without links, and says why -- the Catalog would refuse them",
    async (role) => {
      // Search admits DataAdmin and DataSteward; the Catalog's rows read does not.
      sessionMe = asRoles(role);
      mount();

      expect(await screen.findByText(/5 matches/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /in the Catalog/ })).not.toBeInTheDocument();
      expect(screen.getByText("customer_risk_snapshot")).toBeInTheDocument();
      expect(
        screen.getByText(/Opening a table needs Analyst, MetadataAdmin, PlatformAdmin or Viewer, which your roles do not include, so these results are listed without links\./),
      ).toBeInTheDocument();
    },
  );

  it("offers the links when the session holds any role the Catalog admits, whatever else it holds", async () => {
    sessionMe = asRoles("DataSteward", "Analyst");
    mount();

    expect(await screen.findByRole("button", { name: "Open customer in the Catalog" })).toBeInTheDocument();
    expect(screen.queryByText(/listed without links/)).not.toBeInTheDocument();
  });
});

describe("running a search", () => {
  it("runs on submit, writes the trimmed query to the URL, and starts at the first page", async () => {
    sessionMe = asRoles("Viewer");
    mount("/?q=customer&offset=25#/analyst/search");
    await screen.findByText(/matches for/);
    fetchSearchResults.mockClear();

    fireEvent.change(box(), { target: { value: "  order  " } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() =>
      expect(fetchSearchResults).toHaveBeenLastCalledWith(
        ORG,
        { q: "order", objectType: null, limit: 25, offset: 0 },
        expect.any(AbortSignal),
      ),
    );
    const params = new URLSearchParams(location.search);
    expect(params.get("q")).toBe("order");
    expect(params.has("offset")).toBe(false);
  });

  it("does not search on every keystroke", async () => {
    sessionMe = asRoles("Viewer");
    mount("/#/analyst/search");

    fireEvent.change(box(), { target: { value: "ord" } });
    fireEvent.change(box(), { target: { value: "orde" } });
    await new Promise((resolve) => setTimeout(resolve, 350));

    expect(fetchSearchResults).not.toHaveBeenCalled();
  });

  it("looks again when the same words are submitted again", async () => {
    sessionMe = asRoles("Viewer");
    mount();
    await screen.findByText(/matches for/);
    expect(fetchSearchResults).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => expect(fetchSearchResults).toHaveBeenCalledTimes(2));
  });

  it("submits from the keyboard with Enter", async () => {
    sessionMe = asRoles("Viewer");
    mount("/#/analyst/search");

    fireEvent.change(box(), { target: { value: "ledger" } });
    fireEvent.submit(box().closest("form")!);

    await waitFor(() => expect(fetchSearchResults).toHaveBeenCalledTimes(1));
    expect(fetchSearchResults.mock.calls[0]![1]).toMatchObject({ q: "ledger" });
  });

  it("narrows to one type with the filter, and starts the paging over", async () => {
    sessionMe = asRoles("Viewer");
    mount("/?q=customer&offset=25#/analyst/search");
    await screen.findByText(/matches for/);

    fireEvent.change(screen.getByRole("combobox", { name: "Look in" }), { target: { value: "COLUMN" } });

    await waitFor(() =>
      expect(fetchSearchResults).toHaveBeenLastCalledWith(
        ORG,
        { q: "customer", objectType: "COLUMN", limit: 25, offset: 0 },
        expect.any(AbortSignal),
      ),
    );
    expect(new URLSearchParams(location.search).get("type")).toBe("COLUMN");
    expect(new URLSearchParams(location.search).has("offset")).toBe(false);
  });

  it("offers only the types the API searches: tables and columns, not annotations", async () => {
    sessionMe = asRoles("Viewer");
    mount("/#/analyst/search");

    const filter = await screen.findByRole("combobox", { name: "Look in" });
    expect(within(filter).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Tables and columns", "Tables", "Columns",
    ]);
  });

  it("ignores a type in the URL that the API does not search", async () => {
    sessionMe = asRoles("Viewer");
    mount("/?q=customer&type=ANNOTATION#/analyst/search");

    await screen.findByText(/matches for/);
    expect(fetchSearchResults.mock.calls[0]![1]).toMatchObject({ objectType: null });
  });

  it("follows the URL when it moves, so a link from the palette or Back shows what it searched", async () => {
    sessionMe = asRoles("Viewer");
    mount();
    await screen.findByText(/matches for “customer”/);

    window.history.replaceState(null, "", "/?q=ledger#/analyst/search");
    resetLocationCacheForTests();
    window.dispatchEvent(new PopStateEvent("popstate"));

    await waitFor(() => expect(box()).toHaveValue("ledger"));
    await waitFor(() =>
      expect(fetchSearchResults).toHaveBeenLastCalledWith(ORG, expect.objectContaining({ q: "ledger" }), expect.any(AbortSignal)),
    );
  });
});

describe("paging", () => {
  const MANY = Array.from({ length: 25 }, (_, index) => hit({ object_type: "TABLE", object_id: `t-${index}`, display_name: `orders_${index}` }));

  it("says where the page is in the total, and pages by the API's own limit", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockResolvedValue(page(MANY, { total: 60, facets: [{ field: "object_type", value: "TABLE", count: 60 }] }));
    mount("/?q=orders#/analyst/search");

    expect(await screen.findByText("Showing 1–25 of 60")).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Search result pages" });
    expect(within(nav).getByRole("button", { name: "Previous" })).toBeDisabled();

    fireEvent.click(within(nav).getByRole("button", { name: "Next" }));

    await waitFor(() =>
      expect(fetchSearchResults).toHaveBeenLastCalledWith(ORG, expect.objectContaining({ q: "orders", offset: 25 }), expect.any(AbortSignal)),
    );
    expect(new URLSearchParams(location.search).get("offset")).toBe("25");
  });

  it("goes back to the first page by dropping the offset rather than writing 0", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockResolvedValue(page(MANY, { total: 60, offset: 25 }));
    mount("/?q=orders&offset=25#/analyst/search");

    expect(await screen.findByText("Showing 26–50 of 60")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));

    await waitFor(() => expect(new URLSearchParams(location.search).has("offset")).toBe(false));
  });

  it("has no Next on the last page, and offers no pager at all when everything fits", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockResolvedValue(page(MANY.slice(0, 10), { total: 60, offset: 50 }));
    mount("/?q=orders&offset=50#/analyst/search");
    expect(await screen.findByText("Showing 51–60 of 60")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous" })).toBeEnabled();
  });

  it("offers no pager when the first page holds every match", async () => {
    sessionMe = asRoles("Viewer");
    mount();

    await screen.findByText(/5 matches/);
    expect(screen.queryByRole("navigation", { name: "Search result pages" })).not.toBeInTheDocument();
  });

  it("says a page past the end is empty, with the total, rather than showing nothing", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockResolvedValue({ items: [], facets: [{ field: "object_type", value: "TABLE", count: 60 }], total: 60, limit: 25, offset: 500 });
    mount("/?q=orders&offset=500#/analyst/search");

    expect(await screen.findByText("This page of results is empty")).toBeInTheDocument();
    expect(screen.getByText("There are 60 matches; this page starts past the last of them.")).toBeInTheDocument();
  });

  it("treats a garbage offset as the first page", async () => {
    sessionMe = asRoles("Viewer");
    mount("/?q=customer&offset=abc#/analyst/search");

    await screen.findByText(/matches for/);
    expect(fetchSearchResults.mock.calls[0]![1]).toMatchObject({ offset: 0 });
  });
});

describe("every state says what it is", () => {
  it("says nothing matched, and what the search does and does not look at", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockResolvedValue({ items: [], facets: [], total: 0, limit: 25, offset: 0 });
    mount("/?q=zzz#/analyst/search");

    expect(await screen.findByText("No tables or columns match “zzz”")).toBeInTheDocument();
    expect(screen.getByText(/Words shorter than two letters and common words such as “the” are ignored, and descriptions are not searched/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says it failed, in the server's words, and can try again", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockRejectedValueOnce(new ApiError(500, "search index unavailable"));
    mount();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The search could not be run");
    expect(alert).toHaveTextContent("search index unavailable");
    // A failure is not "no matches".
    expect(screen.queryByText(/No tables or columns match/)).not.toBeInTheDocument();

    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/5 matches/)).toBeInTheDocument();
    expect(fetchSearchResults).toHaveBeenCalledTimes(2);
  });

  it("shows a refusal of the query itself as the server phrased it", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockRejectedValue(new ApiError(422, "query.q: String should have at most 500 characters"));
    mount();

    expect(await screen.findByRole("alert")).toHaveTextContent("query.q: String should have at most 500 characters");
  });

  it("says it is loading while it waits", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchResults.mockReturnValue(new Promise(() => undefined));
    mount();

    expect(await screen.findByText("Loading results…")).toBeInTheDocument();
  });
});

describe("the typeahead", () => {
  it("offers matching table names as the person types, after a pause, as a native list", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchSuggestions.mockResolvedValue([suggestion("customer"), suggestion("customer_risk_snapshot")]);
    mount("/#/analyst/search");

    fireEvent.change(box(), { target: { value: "cust" } });

    await waitFor(() => expect(fetchSearchSuggestions).toHaveBeenCalledWith(ORG, "cust", 8, expect.any(AbortSignal)));
    const list = box().getAttribute("list")!;
    await waitFor(() =>
      expect([...document.getElementById(list)!.querySelectorAll("option")].map((option) => option.getAttribute("value"))).toEqual([
        "customer",
        "customer_risk_snapshot",
      ]),
    );
    // A suggestion fills the box; it does not run a search by itself.
    expect(fetchSearchResults).not.toHaveBeenCalled();
  });

  it("sends one request for a run of keystrokes", async () => {
    sessionMe = asRoles("Viewer");
    mount("/#/analyst/search");

    for (const value of ["cu", "cus", "cust"]) fireEvent.change(box(), { target: { value } });

    await waitFor(() => expect(fetchSearchSuggestions).toHaveBeenCalled());
    expect(fetchSearchSuggestions).toHaveBeenCalledTimes(1);
    expect(fetchSearchSuggestions.mock.calls[0]![1]).toBe("cust");
  });

  it("does not ask for a single character, or for more than the route accepts", async () => {
    sessionMe = asRoles("Viewer");
    mount("/#/analyst/search");

    fireEvent.change(box(), { target: { value: "c" } });
    await new Promise((resolve) => setTimeout(resolve, 350));
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();

    fireEvent.change(box(), { target: { value: "x".repeat(201) } });
    await new Promise((resolve) => setTimeout(resolve, 350));
    expect(fetchSearchSuggestions).not.toHaveBeenCalled();
  });

  it("lists a name once when several tables share it", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchSuggestions.mockResolvedValue([suggestion("customer", "a"), suggestion("customer", "b")]);
    mount("/#/analyst/search");

    fireEvent.change(box(), { target: { value: "cust" } });

    const list = box().getAttribute("list")!;
    await waitFor(() => expect(document.getElementById(list)!.querySelectorAll("option")).toHaveLength(1));
  });

  it("says suggestions are unavailable when they fail, without disturbing the search", async () => {
    sessionMe = asRoles("Viewer");
    fetchSearchSuggestions.mockRejectedValue(new ApiError(503, "suggest index warming up"));
    mount();

    fireEvent.change(box(), { target: { value: "cust" } });

    expect(await screen.findByText("Suggestions are unavailable: suggest index warming up")).toBeInTheDocument();
    // The results still stand.
    expect(screen.getByText(/5 matches for “customer”/)).toBeInTheDocument();
  });
});

describe("accessibility", () => {
  it("has no detectable WCAG A/AA violation when populated, and names every control", async () => {
    sessionMe = asRoles("Analyst");
    fetchSearchResults.mockResolvedValue(
      page([TABLE, TABLE_2, COLUMN("c-1")], { total: 60, facets: [{ field: "object_type", value: "COLUMN", count: 40 }, { field: "object_type", value: "TABLE", count: 20 }] }),
    );
    const { container } = mount();
    await screen.findByText(/matches for/);

    await expectNoAxeViolations(container);
    expect(unnamedFocusableElements(container)).toEqual([]);
  });

  it("is a search landmark, named", async () => {
    sessionMe = asRoles("Viewer");
    mount("/#/analyst/search");

    expect(await screen.findByRole("search", { name: "Search tables and columns" })).toBeInTheDocument();
  });
});
