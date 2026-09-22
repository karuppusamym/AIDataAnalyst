import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { CoverageScope, CoverageSnapshotRead } from "../lib/api";
import { resetLocationCacheForTests } from "../lib/location";
import type { DataSourceRead, MeRead, StewardshipCoverageRead } from "../lib/types";
import type { Session, SessionState } from "../lib/session";
import type { PageOf } from "../lib/ui-types";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";
import { StewardshipCoverage } from "./StewardshipCoverage";

/* ---------------------------------------------------------------------------
   Stewardship -> Coverage, the scorecard (R11-AUD08).

   The properties, and the way each was a real way to get a scorecard wrong:

     1. THE NUMBERS ARE THE API'S. Every percentage on screen is the `percentage`
        the server sent, not one this screen divided out -- proved with a payload
        whose percentage does NOT equal covered/total, because a screen that
        recomputed it would print a different number.
     2. NOTHING FROM NOTHING. A scope with no active tables is "nothing to score",
        not six empty bars and a 0%.
     3. WHO IS ASKED. Every role the reads admit sees the figures; a session known
        to hold none is asked NOTHING and told so; a session whose identity is
        still in flight is asked nothing YET (a request a session turns out not to
        be admitted to is a 403 per load).
     4. THE WRITE IS OFFERED ONLY TO A SESSION KNOWN TO HOLD A WRITE ROLE, is
        never sent on the click, says in the confirmation that a snapshot is
        STORED (and that the server recomputes it), shows a refusal in the
        server's own words, and re-reads both panels after a success rather than
        editing them.
     5. THE HISTORY IS THE SCOPE'S OWN, and its table is the whole record.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

const fetchStewardshipCoverage = vi.fn<
  (organizationId: string, scope?: CoverageScope, signal?: AbortSignal) => Promise<StewardshipCoverageRead>
>();
const fetchCoverageSnapshots = vi.fn<
  (
    organizationId: string,
    scope?: CoverageScope,
    page?: { limit?: number; offset?: number },
    signal?: AbortSignal,
  ) => Promise<PageOf<CoverageSnapshotRead>>
>();
const takeCoverageSnapshot = vi.fn<
  (organizationId: string, scope?: CoverageScope, signal?: AbortSignal) => Promise<StewardshipCoverageRead>
>();
const listOrgDatasources = vi.fn<
  (organizationId: string, signal?: AbortSignal, options?: unknown) => Promise<PageOf<DataSourceRead> & { truncated?: boolean }>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchStewardshipCoverage: (organizationId: string, scope?: CoverageScope, signal?: AbortSignal) =>
      fetchStewardshipCoverage(organizationId, scope, signal),
    fetchCoverageSnapshots: (
      organizationId: string,
      scope?: CoverageScope,
      page?: { limit?: number; offset?: number },
      signal?: AbortSignal,
    ) => fetchCoverageSnapshots(organizationId, scope, page, signal),
    takeCoverageSnapshot: (organizationId: string, scope?: CoverageScope, signal?: AbortSignal) =>
      takeCoverageSnapshot(organizationId, scope, signal),
    listOrgDatasources: (organizationId: string, signal?: AbortSignal, options?: unknown) =>
      listOrgDatasources(organizationId, signal, options),
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

const DIMENSIONS = ["documented", "owned", "classified", "certified", "quality_monitored", "semantically_mapped"] as const;

/** The exact shape `GET .../stewardship/coverage` answers -- the figures a live probe returned on 2026-09-21. */
const COVERAGE: StewardshipCoverageRead = {
  organization_id: ORG,
  datasource_id: null,
  domain_id: null,
  line_of_business_id: null,
  table_count: 19,
  overall_score: 9.65,
  dimensions: {
    documented: { covered: 0, total: 19, percentage: 0.0 },
    owned: { covered: 0, total: 19, percentage: 0.0 },
    classified: { covered: 2, total: 19, percentage: 10.53 },
    certified: { covered: 0, total: 19, percentage: 0.0 },
    quality_monitored: { covered: 0, total: 19, percentage: 0.0 },
    semantically_mapped: { covered: 9, total: 19, percentage: 47.37 },
  },
  unowned_table_ids: ["t-1", "t-2"],
  computed_at: "2026-09-21T14:52:56.323511Z",
};

const EMPTY_SCOPE: StewardshipCoverageRead = {
  ...COVERAGE,
  table_count: 0,
  overall_score: 0,
  dimensions: Object.fromEntries(DIMENSIONS.map((name) => [name, { covered: 0, total: 0, percentage: 0 }])),
  unowned_table_ids: [],
};

const snapshot = (overrides: Partial<CoverageSnapshotRead> = {}): CoverageSnapshotRead => ({
  id: "snap-1",
  organization_id: ORG,
  datasource_id: null,
  domain_id: null,
  line_of_business_id: null,
  table_count: 19,
  dimensions: COVERAGE.dimensions,
  overall_score: 9.65,
  computed_by: "dana.steward",
  created_at: "2026-09-19T08:30:12Z",
  ...overrides,
});

const historyPage = (items: CoverageSnapshotRead[], total = items.length): PageOf<CoverageSnapshotRead> => ({
  items, limit: 50, offset: 0, total,
});

const SOURCE: DataSourceRead = {
  id: "ds_1", organization_id: ORG, line_of_business_id: "lob1", data_domain_id: "dom1",
  project_id: "proj1", name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake",
  environment: "PRODUCTION", credential_reference: "vault://x", status: "ACTIVE", capabilities: {},
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const takeButton = () => screen.queryByRole("button", { name: "Take a snapshot" });
const READ_ONLY = /Only DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin can take a snapshot/;

function mount(url = "/?view=coverage#/steward/stewardship") {
  window.history.replaceState(null, "", url);
  resetLocationCacheForTests();
  return render(<StewardshipCoverage />);
}
beforeEach(() => {
  fetchStewardshipCoverage.mockReset();
  fetchStewardshipCoverage.mockResolvedValue(COVERAGE);
  fetchCoverageSnapshots.mockReset();
  fetchCoverageSnapshots.mockResolvedValue(historyPage([]));
  takeCoverageSnapshot.mockReset();
  listOrgDatasources.mockReset();
  listOrgDatasources.mockResolvedValue({ items: [SOURCE], limit: 500, offset: 0, total: 1, truncated: false });
  sessionMe = null;
  sessionState = "connected";
  window.history.replaceState(null, "", "/");
  resetLocationCacheForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("who is asked, and who may take a snapshot", () => {
  it.each(["Analyst", "Auditor", "DataAdmin", "Reviewer", "Viewer"])(
    "shows %s the figures and the history, and offers no snapshot",
    async (role) => {
      sessionMe = asRoles(role);
      mount();

      expect(await screen.findByText("9.65%")).toBeInTheDocument();
      expect(fetchStewardshipCoverage).toHaveBeenCalledWith(ORG, { datasourceId: null }, expect.any(AbortSignal));
      expect(fetchCoverageSnapshots).toHaveBeenCalledWith(ORG, { datasourceId: null }, { limit: 50 }, expect.any(AbortSignal));
      expect(takeButton()).not.toBeInTheDocument();
      // Told why, rather than left to wonder where the control is.
      expect(screen.getByText(READ_ONLY)).toBeInTheDocument();
    },
  );

  it.each(["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"])(
    "offers %s the snapshot, and does not say they cannot take one",
    async (role) => {
      sessionMe = asRoles(role);
      mount();

      expect(await screen.findByText("9.65%")).toBeInTheDocument();
      expect(takeButton()).toBeEnabled();
      expect(screen.queryByText(READ_ONLY)).not.toBeInTheDocument();
    },
  );

  it.each(["AgentDeveloper", "ToolDeveloper", "Operations", "OrganizationAdmin", "ProjectAdmin", "DataEngineer"])(
    "asks for nothing as %s, and says coverage is not available rather than showing a refusal",
    async (role) => {
      sessionMe = asRoles(role);
      mount();

      expect(
        await screen.findByText(
          /Not applicable to your roles: only sessions holding Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin or Viewer can read stewardship coverage/,
        ),
      ).toBeInTheDocument();
      expect(fetchStewardshipCoverage).not.toHaveBeenCalled();
      expect(fetchCoverageSnapshots).not.toHaveBeenCalled();
      expect(listOrgDatasources).not.toHaveBeenCalled();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(takeButton()).not.toBeInTheDocument();
      expect(screen.queryByText("9.65%")).not.toBeInTheDocument();
    },
  );

  it("holds every request while identity is in flight, and offers no write", async () => {
    // `/v1/me` has not answered: unknown, not "no roles".
    sessionState = "connecting";
    mount();

    expect(await screen.findByText("Loading coverage…")).toBeInTheDocument();
    expect(fetchStewardshipCoverage).not.toHaveBeenCalled();
    expect(fetchCoverageSnapshots).not.toHaveBeenCalled();
    expect(listOrgDatasources).not.toHaveBeenCalled();
    expect(takeButton()).not.toBeInTheDocument();
    // ... and it does not tell a steward-to-be that they cannot, or that it does not apply.
    expect(screen.queryByText(READ_ONLY)).not.toBeInTheDocument();
    expect(screen.queryByText(/Not applicable to your roles/)).not.toBeInTheDocument();
  });

  it("never sends the reads when identity then says the session may not have them", async () => {
    sessionState = "connecting";
    const view = render(<StewardshipCoverage />);
    expect(await screen.findByText("Loading coverage…")).toBeInTheDocument();

    sessionState = "connected";
    sessionMe = asRoles("Operations");
    view.rerender(<StewardshipCoverage />);

    expect(await screen.findByText(/Not applicable to your roles/)).toBeInTheDocument();
    expect(fetchStewardshipCoverage).not.toHaveBeenCalled();
    expect(fetchCoverageSnapshots).not.toHaveBeenCalled();
  });

  it("sends the reads, and the write control, once identity says the session may", async () => {
    sessionState = "connecting";
    const view = render(<StewardshipCoverage />);
    expect(fetchStewardshipCoverage).not.toHaveBeenCalled();

    sessionState = "connected";
    sessionMe = asRoles("DataSteward");
    view.rerender(<StewardshipCoverage />);

    expect(await screen.findByText("9.65%")).toBeInTheDocument();
    expect(fetchStewardshipCoverage).toHaveBeenCalledTimes(1);
    expect(fetchCoverageSnapshots).toHaveBeenCalledTimes(1);
    expect(takeButton()).toBeEnabled();
  });

  it("still reads when identity will not answer -- the server stays the authority -- but offers no write", async () => {
    sessionState = "disconnected";
    mount();

    expect(await screen.findByText("9.65%")).toBeInTheDocument();
    expect(fetchStewardshipCoverage).toHaveBeenCalledTimes(1);
    // Fail closed: a write is never offered on a guess, and "only X can" is not said about a session unknown.
    expect(takeButton()).not.toBeInTheDocument();
    expect(screen.queryByText(READ_ONLY)).not.toBeInTheDocument();
  });

  it("does not ask for the datasource list of a session the list refuses, and does of one it admits", async () => {
    sessionMe = asRoles("DataSteward"); // admitted to coverage, NOT to the organization's datasource list
    const steward = mount();
    expect(await screen.findByText("9.65%")).toBeInTheDocument();
    expect(listOrgDatasources).not.toHaveBeenCalled();
    steward.unmount();

    sessionMe = asRoles("Analyst");
    mount();
    expect(await screen.findByText("9.65%")).toBeInTheDocument();
    await waitFor(() => expect(listOrgDatasources).toHaveBeenCalled());
  });
});

describe("the figures", () => {
  it("shows the API's numbers as returned: the overall score, each dimension's counts and percentage, when they were computed", async () => {
    sessionMe = asRoles("Viewer");
    mount();

    expect(await screen.findByText("9.65%")).toBeInTheDocument();
    expect(screen.getByText(/overall for the whole organization — the average of the dimension percentages below, across 19 active tables/)).toBeInTheDocument();

    const table = screen.getByRole("table", { name: /Coverage of the whole organization by dimension/ });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))).toEqual([
      ["0 of 19 tables", "0.00%"],
      ["0 of 19 tables", "0.00%"],
      ["2 of 19 tables", "10.53%"],
      ["0 of 19 tables", "0.00%"],
      ["0 of 19 tables", "0.00%"],
      ["9 of 19 tables", "47.37%"],
    ]);
    expect(rows.map((row) => within(row).getByRole("rowheader").querySelector(".stewcov__dimname")?.textContent)).toEqual([
      "Documented", "Owned", "Classified", "Certified", "Quality monitored", "Semantically mapped",
    ]);
    // Worked out on open and not stored: said in words, with the time.
    expect(screen.getByText(/Computed 2026-09-21 14:52 UTC\. These figures are worked out when this view opens and are not stored until someone takes a snapshot\./)).toBeInTheDocument();
  });

  it("says what each dimension counts, beside it", async () => {
    sessionMe = asRoles("Viewer");
    mount();
    await screen.findByText("9.65%");

    const certified = screen.getByRole("rowheader", { name: /Certified/ });
    expect(certified).toHaveTextContent("A certified column does not make its table certified.");
    expect(certified).toHaveTextContent("has not expired");
  });

  it("prints the percentage the API sent, never one it worked out itself", async () => {
    // 1 of 4 is 25%; the payload says 50 and 60. A screen that divided would print 25.00% and mean 25.00%.
    sessionMe = asRoles("Viewer");
    fetchStewardshipCoverage.mockResolvedValue({
      ...COVERAGE,
      table_count: 4,
      overall_score: 60,
      dimensions: { documented: { covered: 1, total: 4, percentage: 50 }, owned: { covered: 1, total: 4, percentage: 70 } },
    });
    mount();

    expect(await screen.findByText("60.00%")).toBeInTheDocument();
    expect(screen.getByText("50.00%")).toBeInTheDocument();
    expect(screen.getByText("70.00%")).toBeInTheDocument();
    expect(screen.queryByText("25.00%")).not.toBeInTheDocument();
  });

  it("shows a dimension the server added since, without a definition, rather than dropping it", async () => {
    sessionMe = asRoles("Viewer");
    fetchStewardshipCoverage.mockResolvedValue({
      ...COVERAGE,
      dimensions: { ...COVERAGE.dimensions, lineage_traced: { covered: 3, total: 19, percentage: 15.79 } },
    });
    mount();

    expect(await screen.findByText("Lineage traced")).toBeInTheDocument();
    expect(screen.getByText("15.79%")).toBeInTheDocument();
  });

  it("says a scope with no active tables has nothing to score, and draws no percentage", async () => {
    sessionMe = asRoles("Viewer");
    fetchStewardshipCoverage.mockResolvedValue(EMPTY_SCOPE);
    mount();

    expect(await screen.findByText("No active tables in the whole organization")).toBeInTheDocument();
    expect(screen.getByText(/There is nothing to score yet, so no percentage is shown/)).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: /Coverage of/ })).not.toBeInTheDocument();
    expect(screen.queryByText("0.00%")).not.toBeInTheDocument();
    expect(screen.queryByText(/overall for/)).not.toBeInTheDocument();
  });

  it("says it could not load the figures, with the server's reason, and can try again", async () => {
    sessionMe = asRoles("Viewer");
    fetchStewardshipCoverage.mockRejectedValueOnce(new ApiError(503, "database unavailable"));
    mount();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Coverage could not be loaded");
    expect(alert).toHaveTextContent("database unavailable");
    // A failed read is not a zero.
    expect(screen.queryByText("0.00%")).not.toBeInTheDocument();

    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("9.65%")).toBeInTheDocument();
    expect(fetchStewardshipCoverage).toHaveBeenCalledTimes(2);
  });
});

describe("the history", () => {
  it("lists every stored snapshot with the six percentages, newest first, and the record is a table", async () => {
    sessionMe = asRoles("Auditor");
    fetchCoverageSnapshots.mockResolvedValue(
      historyPage([
        snapshot({ id: "s3", created_at: "2026-09-19T08:30:12Z", computed_by: "dana.steward", overall_score: 9.65, table_count: 19 }),
        snapshot({
          id: "s2",
          created_at: "2026-09-01T06:00:00Z",
          computed_by: "omar.steward",
          overall_score: 5,
          table_count: 17,
          dimensions: { ...COVERAGE.dimensions, classified: { covered: 1, total: 17, percentage: 5.88 } },
        }),
      ]),
    );
    mount();

    const table = await screen.findByRole("table", { name: /Stored coverage snapshots, newest first/ });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByRole("rowheader")).toHaveTextContent("2026-09-19 08:30 UTC");
    expect(within(rows[0]!).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
      "dana.steward", "19", "9.65%",
      "0.00% (0 of 19 tables)", "0.00% (0 of 19 tables)", "10.53% (2 of 19 tables)",
      "0.00% (0 of 19 tables)", "0.00% (0 of 19 tables)", "47.37% (9 of 19 tables)",
    ]);
    expect(within(rows[1]!).getByRole("rowheader")).toHaveTextContent("2026-09-01 06:00 UTC");
    expect(within(rows[1]!).getAllByRole("cell")[5]).toHaveTextContent("5.88% (1 of 17 tables)");
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      "Taken", "Taken by", "Tables", "Overall",
      "Documented", "Owned", "Classified", "Certified", "Quality monitored", "Semantically mapped",
    ]);
    expect(screen.getByText("2 stored")).toBeInTheDocument();
  });

  it("draws the trend from two snapshots up, on the fixed scale, and names it for a screen reader", async () => {
    sessionMe = asRoles("Auditor");
    fetchCoverageSnapshots.mockResolvedValue(
      historyPage([snapshot({ id: "s3", overall_score: 12 }), snapshot({ id: "s2", overall_score: 9.65 }), snapshot({ id: "s1", overall_score: 4.5 })]),
    );
    mount();

    // Oldest to newest, so the label runs from the LAST row of the table to the first.
    const trend = await screen.findByRole("img", {
      name: "Overall coverage across 3 snapshots, oldest to newest: 4.50% to 12.00%",
    });
    expect(trend.querySelectorAll("circle")).toHaveLength(3);
    expect(screen.getByText(/one point per snapshot, oldest to newest, on a 0 to 100 scale/)).toBeInTheDocument();
    // The line is on the true 0-100 axis: 4.5 sits near the bottom, 12 barely above it -- not stretched to fill the box.
    const ys = [...trend.querySelectorAll("circle")].map((circle) => Number(circle.getAttribute("cy")));
    expect(Math.max(...ys)).toBeGreaterThan(30); // the oldest, 4.5
    expect(Math.min(...ys)).toBeGreaterThan(30); // even the newest, 12, is near the floor of a 44px box
  });

  it("draws no trend from a single snapshot, and the table is still there", async () => {
    sessionMe = asRoles("Auditor");
    fetchCoverageSnapshots.mockResolvedValue(historyPage([snapshot()]));
    mount();

    expect(await screen.findByRole("table", { name: /Stored coverage snapshots/ })).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("says when the table is not the whole history", async () => {
    sessionMe = asRoles("Auditor");
    fetchCoverageSnapshots.mockResolvedValue(historyPage([snapshot({ id: "a" }), snapshot({ id: "b" })], 120));
    mount();

    expect(await screen.findByText("Showing the 2 most recent of 120 stored snapshots.")).toBeInTheDocument();
    expect(screen.getByText("120 stored")).toBeInTheDocument();
  });

  it("says there are none, and tells a steward how to start one", async () => {
    sessionMe = asRoles("DataSteward");
    mount();

    expect(await screen.findByText("No snapshots have been stored for the whole organization")).toBeInTheDocument();
    expect(screen.getByText("Take a snapshot to start a history you can compare later figures against.")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: /Stored coverage snapshots/ })).not.toBeInTheDocument();
  });

  it("says there are none without telling a reader to do what they cannot", async () => {
    sessionMe = asRoles("Viewer");
    mount();

    expect(await screen.findByText("No snapshots have been stored for the whole organization")).toBeInTheDocument();
    expect(screen.getByText("Someone who can take a snapshot has not yet stored one for this scope.")).toBeInTheDocument();
    expect(screen.queryByText(/Take a snapshot to start a history/)).not.toBeInTheDocument();
  });

  it("says the history could not be loaded, in the server's words, while the figures still show", async () => {
    sessionMe = asRoles("Viewer");
    fetchCoverageSnapshots.mockRejectedValueOnce(new ApiError(500, "snapshot store unavailable"));
    mount();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The snapshot history could not be loaded");
    expect(alert).toHaveTextContent("snapshot store unavailable");
    expect(screen.getByText("9.65%")).toBeInTheDocument();

    fetchCoverageSnapshots.mockResolvedValueOnce(historyPage([snapshot()]));
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table", { name: /Stored coverage snapshots/ })).toBeInTheDocument();
  });
});

describe("the scope", () => {
  it("reads and stores the organization by default, and says so", async () => {
    sessionMe = asRoles("Analyst");
    mount();

    expect(await screen.findByText(/overall for the whole organization/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Scope" })).toHaveValue("");
  });

  it("reads one datasource, for both the figures and the history, when the URL names it", async () => {
    sessionMe = asRoles("Analyst");
    fetchStewardshipCoverage.mockResolvedValue({ ...COVERAGE, datasource_id: "ds_1", table_count: 4 });
    mount("/?view=coverage&ds=ds_1#/steward/stewardship");

    expect(await screen.findByText(/overall for snowflake_prod/)).toBeInTheDocument();
    expect(fetchStewardshipCoverage).toHaveBeenCalledWith(ORG, { datasourceId: "ds_1" }, expect.any(AbortSignal));
    expect(fetchCoverageSnapshots).toHaveBeenCalledWith(ORG, { datasourceId: "ds_1" }, { limit: 50 }, expect.any(AbortSignal));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Scope" })).toHaveValue("ds_1"));
    expect(screen.getByText("No snapshots have been stored for snowflake_prod")).toBeInTheDocument();
  });

  it("re-reads both panels for the new scope when the steward picks a source, and writes it to the URL", async () => {
    sessionMe = asRoles("Analyst");
    mount();
    await screen.findByText("9.65%");
    await waitFor(() => expect(screen.getByRole("option", { name: "snowflake_prod" })).toBeInTheDocument());

    fireEvent.change(screen.getByRole("combobox", { name: "Scope" }), { target: { value: "ds_1" } });

    await waitFor(() =>
      expect(fetchStewardshipCoverage).toHaveBeenLastCalledWith(ORG, { datasourceId: "ds_1" }, expect.any(AbortSignal)),
    );
    expect(fetchCoverageSnapshots).toHaveBeenLastCalledWith(ORG, { datasourceId: "ds_1" }, { limit: 50 }, expect.any(AbortSignal));
    expect(new URLSearchParams(location.search).get("ds")).toBe("ds_1");
    // ... and back to the organization drops the field rather than writing an empty one.
    fireEvent.change(screen.getByRole("combobox", { name: "Scope" }), { target: { value: "" } });
    await waitFor(() => expect(new URLSearchParams(location.search).has("ds")).toBe(false));
    await waitFor(() =>
      expect(fetchStewardshipCoverage).toHaveBeenLastCalledWith(ORG, { datasourceId: null }, expect.any(AbortSignal)),
    );
  });

  it("keeps a scope the URL names even when the source list cannot name it, rather than showing another", async () => {
    sessionMe = asRoles("DataSteward"); // the datasource list is not asked for
    mount("/?view=coverage&ds=ds_unknown#/steward/stewardship");

    expect(await screen.findByText(/overall for the selected datasource/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Scope" })).toHaveValue("ds_unknown");
    expect(screen.getByRole("option", { name: "Selected datasource" })).toBeInTheDocument();
  });

  it("says the source list could not be loaded, and still offers the organization", async () => {
    sessionMe = asRoles("Analyst");
    listOrgDatasources.mockRejectedValue(new ApiError(500, "source registry unavailable"));
    mount();

    expect(await screen.findByText("The datasource list could not be loaded: source registry unavailable")).toBeInTheDocument();
    expect(await screen.findByText("9.65%")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Whole organization" })).toBeInTheDocument();
  });
});

describe("taking a snapshot", () => {
  // PlatformAdmin holds a write role AND is admitted to the datasource list, so a datasource's NAME
  // resolves; a DataSteward-only session is the case the scope tests above cover (the list is not asked).
  async function openConfirmation(scopeUrl?: string) {
    sessionMe = asRoles("PlatformAdmin");
    mount(scopeUrl);
    await screen.findByText("9.65%");
    const trigger = screen.getByRole("button", { name: "Take a snapshot" });
    trigger.focus();
    fireEvent.click(trigger);
    return await screen.findByRole("dialog", { name: "Take a coverage snapshot?" });
  }

  it("says a snapshot is STORED, where and what, and sends nothing on the click", async () => {
    const dialog = await openConfirmation();

    expect(dialog).toHaveTextContent("This stores a snapshot in the coverage history for the whole organization");
    expect(dialog).toHaveTextContent("the number of active tables, the six percentages and the overall score, with your name and the time");
    expect(dialog).toHaveTextContent("recorded in the audit ledger");
    expect(dialog).toHaveTextContent("has no way to remove it");
    // The one thing that is easy to miss: what is stored is computed again, not copied from the screen.
    expect(dialog).toHaveTextContent("The server works the figures out again when you confirm");
    expect(dialog).toHaveTextContent("No table, owner or certification changes.");
    expect(within(dialog).getByRole("button", { name: "Take snapshot" })).toBeEnabled();
    expect(takeCoverageSnapshot).not.toHaveBeenCalled();
  });

  it("names the datasource it will store the snapshot under", async () => {
    const dialog = await openConfirmation("/?view=coverage&ds=ds_1#/steward/stewardship");

    await waitFor(() => expect(dialog).toHaveTextContent("coverage history for snowflake_prod"));
  });

  it("sends nothing when the steward cancels", async () => {
    const dialog = await openConfirmation();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(takeCoverageSnapshot).not.toHaveBeenCalled();
    expect(fetchStewardshipCoverage).toHaveBeenCalledTimes(1);
  });

  it("takes it for the scope on screen, says what was stored, then re-reads both panels rather than editing them", async () => {
    const stored: StewardshipCoverageRead = { ...COVERAGE, overall_score: 9.65 };
    takeCoverageSnapshot.mockResolvedValue(stored);
    fetchCoverageSnapshots.mockResolvedValueOnce(historyPage([])).mockResolvedValueOnce(historyPage([snapshot({ id: "new" })]));
    const dialog = await openConfirmation();

    fireEvent.click(within(dialog).getByRole("button", { name: "Take snapshot" }));

    await waitFor(() => expect(takeCoverageSnapshot).toHaveBeenCalledWith(ORG, { datasourceId: null }, undefined));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(
      await screen.findByText("Snapshot stored for the whole organization: 19 active tables, overall 9.65%."),
    ).toBeInTheDocument();
    // What the panels show now is the SERVER's answer to a second read of each.
    expect(await screen.findByRole("table", { name: /Stored coverage snapshots/ })).toBeInTheDocument();
    expect(fetchStewardshipCoverage).toHaveBeenCalledTimes(2);
    expect(fetchCoverageSnapshots).toHaveBeenCalledTimes(2);
  });

  it("stores the snapshot under the datasource on screen", async () => {
    takeCoverageSnapshot.mockResolvedValue({ ...COVERAGE, datasource_id: "ds_1", table_count: 1 });
    const dialog = await openConfirmation("/?view=coverage&ds=ds_1#/steward/stewardship");
    await waitFor(() => expect(dialog).toHaveTextContent("for snowflake_prod"));

    fireEvent.click(within(dialog).getByRole("button", { name: "Take snapshot" }));

    await waitFor(() => expect(takeCoverageSnapshot).toHaveBeenCalledWith(ORG, { datasourceId: "ds_1" }, undefined));
    expect(await screen.findByText(/Snapshot stored for snowflake_prod: 1 active table, overall 9.65%\./)).toBeInTheDocument();
  });

  it("keeps the dialog open on a refusal and shows the server's own words, claiming nothing", async () => {
    takeCoverageSnapshot.mockRejectedValue(new ApiError(403, "requires one of: DataSteward, MetadataAdmin"));
    const dialog = await openConfirmation();

    fireEvent.click(within(dialog).getByRole("button", { name: "Take snapshot" }));

    const refusal = await within(dialog).findByRole("alert");
    expect(refusal).toHaveTextContent(/^requires one of: DataSteward, MetadataAdmin$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // Nothing was stored, so nothing is claimed and nothing behind the dialog is re-read.
    expect(screen.queryByText(/Snapshot stored/)).not.toBeInTheDocument();
    expect(fetchStewardshipCoverage).toHaveBeenCalledTimes(1);
    expect(fetchCoverageSnapshots).toHaveBeenCalledTimes(1);
    // The steward can retry from the same dialog.
    expect(within(dialog).getByRole("button", { name: "Take snapshot" })).toBeEnabled();
  });

  it("shows a not-found for the scope in the server's words too", async () => {
    takeCoverageSnapshot.mockRejectedValue(new ApiError(404, "data source not found"));
    const dialog = await openConfirmation();

    fireEvent.click(within(dialog).getByRole("button", { name: "Take snapshot" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("data source not found");
  });

  it("admits one snapshot at a time and shows the dialog busy meanwhile", async () => {
    let settle: (coverage: StewardshipCoverageRead) => void = () => undefined;
    takeCoverageSnapshot.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const dialog = await openConfirmation();

    fireEvent.click(within(dialog).getByRole("button", { name: "Take snapshot" }));
    const working = await within(dialog).findByRole("button", { name: "Working…" });
    expect(working).toBeDisabled();
    fireEvent.click(working);
    fireEvent.click(working);

    expect(takeCoverageSnapshot).toHaveBeenCalledTimes(1);
    settle(COVERAGE);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("does not carry a refusal or a notice over to another scope", async () => {
    takeCoverageSnapshot.mockRejectedValueOnce(new ApiError(403, "no"));
    const dialog = await openConfirmation();
    fireEvent.click(within(dialog).getByRole("button", { name: "Take snapshot" }));
    await within(dialog).findByRole("alert");

    // Backing out to another scope (Back, or the picker) closes a confirmation made for the first.
    window.history.replaceState(null, "", "/?view=coverage&ds=ds_1#/steward/stewardship");
    resetLocationCacheForTests();
    window.dispatchEvent(new PopStateEvent("popstate"));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});

describe("accessibility", () => {
  it("has no detectable WCAG A/AA violation when populated, and names every control", async () => {
    sessionMe = asRoles("DataSteward");
    fetchCoverageSnapshots.mockResolvedValue(
      historyPage([snapshot({ id: "s2", overall_score: 12 }), snapshot({ id: "s1", overall_score: 9.65 })]),
    );
    const { container } = mount();
    await screen.findByRole("table", { name: /Stored coverage snapshots/ });

    await expectNoAxeViolations(container);
    expect(unnamedFocusableElements(container)).toEqual([]);
  });

  it("lets the keyboard reach the scrolling history table", async () => {
    sessionMe = asRoles("Viewer");
    fetchCoverageSnapshots.mockResolvedValue(historyPage([snapshot()]));
    mount();

    const region = await screen.findByRole("region", { name: "Snapshot history table" });
    expect(region).toHaveAttribute("tabindex", "0");
  });
});
