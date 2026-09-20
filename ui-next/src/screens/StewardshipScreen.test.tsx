import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  CatalogBulkActionRunRead,
  DataSourceRead,
  UnownedAssetBacklogRouteResult,
  UnownedAssetEscalationRead,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";

/* ---------------------------------------------------------------------------
   Stewardship: catalog bulk actions (tag/classify/own/certify) against the
   real `bulk_tag_tables`/`bulk_classify_tables`/`bulk_own_tables`/
   `bulk_certify_tables` (api.py) plus the unowned-asset backlog
   (`list_unowned_backlog`/`route_unowned_backlog`, stewardship_api.py).
   Mocks only the API boundary this screen calls, matching
   `QualityScreen.test.tsx`'s established pattern.
--------------------------------------------------------------------------- */

const listOrgDatasources = vi.fn<
  (organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>
>();
const bulkTagCatalogTables = vi.fn<
  (organizationId: string, body: unknown, signal?: AbortSignal) => Promise<CatalogBulkActionRunRead>
>();
const bulkClassifyCatalogColumns = vi.fn<
  (organizationId: string, body: unknown, signal?: AbortSignal) => Promise<CatalogBulkActionRunRead>
>();
const bulkAssignCatalogOwnership = vi.fn<
  (organizationId: string, body: unknown, signal?: AbortSignal) => Promise<CatalogBulkActionRunRead>
>();
const bulkCertifyCatalogTables = vi.fn<
  (organizationId: string, body: unknown, signal?: AbortSignal) => Promise<CatalogBulkActionRunRead>
>();
const fetchUnownedAssetBacklog = vi.fn<
  (organizationId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<UnownedAssetEscalationRead>>
>();
const routeUnownedAssetBacklog = vi.fn<
  (organizationId: string, body: unknown, signal?: AbortSignal) => Promise<UnownedAssetBacklogRouteResult>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    listOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      listOrgDatasources(organizationId, signal),
    bulkTagCatalogTables: (organizationId: string, body: unknown, signal?: AbortSignal) =>
      bulkTagCatalogTables(organizationId, body, signal),
    bulkClassifyCatalogColumns: (organizationId: string, body: unknown, signal?: AbortSignal) =>
      bulkClassifyCatalogColumns(organizationId, body, signal),
    bulkAssignCatalogOwnership: (organizationId: string, body: unknown, signal?: AbortSignal) =>
      bulkAssignCatalogOwnership(organizationId, body, signal),
    bulkCertifyCatalogTables: (organizationId: string, body: unknown, signal?: AbortSignal) =>
      bulkCertifyCatalogTables(organizationId, body, signal),
    fetchUnownedAssetBacklog: (organizationId: string, query: unknown, signal?: AbortSignal) =>
      fetchUnownedAssetBacklog(organizationId, query, signal),
    routeUnownedAssetBacklog: (organizationId: string, body: unknown, signal?: AbortSignal) =>
      routeUnownedAssetBacklog(organizationId, body, signal),
  };
});

const DATASOURCE: DataSourceRead = {
  id: "ds_1", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  project_id: "proj1", name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake",
  environment: "PRODUCTION", credential_reference: "vault://x", status: "ACTIVE", capabilities: {},
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const ESCALATION: UnownedAssetEscalationRead = {
  id: "unowned_1", organization_id: "org1", table_id: "t_abc123",
  first_detected_unowned_at: "2026-08-01T00:00:00Z", status: "UNOWNED",
  candidate_owner: null, notification_rule_id: null, channel: null, recipients: [],
  dedup_key: "ds_1:t_abc123", routed_at: null, escalated_at: null, escalated_tier2_at: null,
  resolved_at: null, created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
};

function backlogPage(items: UnownedAssetEscalationRead[]): PageOf<UnownedAssetEscalationRead> {
  return { items, limit: 100, offset: 0, total: items.length };
}

function bulkRun(overrides: Partial<CatalogBulkActionRunRead> = {}): CatalogBulkActionRunRead {
  return {
    id: "bulkrun_1", organization_id: "org1", action: "BULK_TAG", selection_mode: "FILTER",
    parameters: {}, requested_count: 2, succeeded_count: 2, failed_count: 0,
    results: [
      { subject_id: "t_1", status: "SUCCEEDED", reason: null },
      { subject_id: "t_2", status: "SUCCEEDED", reason: null },
    ],
    requested_by: "local-ui-admin", created_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

/* R11-S13 (items 15/17): the old page's two panels are two views of the
   stewardship workspace now. Each case renders the view whose behaviour it
   asserts; the tab axis itself is `StewardshipWorkspace.test.tsx`'s. */
async function loadWorkQueue() {
  const { StewardshipWorkQueue } = await import("./StewardshipScreen");
  return StewardshipWorkQueue;
}

async function loadBulkActions() {
  const { StewardshipBulkActions } = await import("./StewardshipScreen");
  return StewardshipBulkActions;
}

beforeEach(() => {
  listOrgDatasources.mockReset();
  bulkTagCatalogTables.mockReset();
  bulkClassifyCatalogColumns.mockReset();
  bulkAssignCatalogOwnership.mockReset();
  bulkCertifyCatalogTables.mockReset();
  fetchUnownedAssetBacklog.mockReset();
  routeUnownedAssetBacklog.mockReset();

  listOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  fetchUnownedAssetBacklog.mockResolvedValue(backlogPage([ESCALATION]));
  bulkTagCatalogTables.mockResolvedValue(bulkRun());

  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("StewardshipScreen against the real catalog bulk-action + stewardship endpoints", () => {
  it("loads the datasource picker and the unowned backlog on mount", async () => {
    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);

    await waitFor(() => expect(fetchUnownedAssetBacklog).toHaveBeenCalledWith(
      "00000000-0000-0000-0000-000000000001",
      { status: null, limit: 100 },
      expect.anything(),
    ));
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());
    expect(screen.getAllByText("snowflake_prod").length).toBeGreaterThan(0);
  });

  it("submitting the default tag action posts the right filter body and renders the results panel", async () => {
    const StewardshipBulkActions = await loadBulkActions();
    render(<StewardshipBulkActions />);
    await waitFor(() => expect(screen.getAllByText("snowflake_prod").length).toBeGreaterThan(0));

    fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: "ds_1" } });
    fireEvent.change(screen.getByLabelText("Match pattern"), { target: { value: "raw_%" } });
    fireEvent.change(screen.getByLabelText("Tag key"), { target: { value: "pii-reviewed" } });

    fireEvent.click(screen.getByRole("button", { name: "Run tag tables" }));

    await waitFor(() =>
      expect(bulkTagCatalogTables).toHaveBeenCalledWith(
        "00000000-0000-0000-0000-000000000001",
        {
          filter: { datasource_id: "ds_1", match_field: "TABLE_NAME", match_pattern: "raw_%" },
          tag_key: "pii-reviewed",
          tag_value: null,
        },
        undefined,
      ),
    );
    await waitFor(() => expect(screen.getByLabelText("Bulk action result")).toBeInTheDocument());
    expect(screen.getByText("2 requested")).toBeInTheDocument();
    expect(screen.getByText("2 succeeded")).toBeInTheDocument();
  });

  it("switching the action to certify swaps in the rationale/expiry fields and keeps submit disabled until both are valid", async () => {
    const StewardshipBulkActions = await loadBulkActions();
    render(<StewardshipBulkActions />);
    await waitFor(() => expect(screen.getAllByText("snowflake_prod").length).toBeGreaterThan(0));

    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "certify" } });
    expect(screen.getByLabelText("Rationale")).toBeInTheDocument();
    expect(screen.getByLabelText("Expires at")).toBeInTheDocument();
    expect(screen.queryByLabelText("Tag key")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: "ds_1" } });
    fireEvent.change(screen.getByLabelText("Match pattern"), { target: { value: "raw_%" } });

    const submitButton = screen.getByRole("button", { name: "Run certify tables" });
    expect(submitButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Rationale"), {
      target: { value: "Quarterly certification review completed." },
    });
    expect(submitButton).not.toBeDisabled();

    bulkCertifyCatalogTables.mockResolvedValue(bulkRun({ action: "BULK_CERTIFY" }));
    fireEvent.click(submitButton);

    await waitFor(() => expect(bulkCertifyCatalogTables).toHaveBeenCalledTimes(1));
    const [, body] = bulkCertifyCatalogTables.mock.calls[0]!;
    expect((body as { rationale: string }).rationale).toBe("Quarterly certification review completed.");
  });

  /* The candidate-owner filter is the server's now (`candidate_owner`, exact and case-sensitive,
     applied before paging). These cases use a fake that filters the way the route does, so what
     the screen shows is what the request asked for -- and a screen that still narrowed the page
     itself would show a different set than the fake returns. */
  const OWNED_BY_RISK = "risk-data-stewards@tenant.example";
  const BACKLOG_ROWS: UnownedAssetEscalationRead[] = [
    ESCALATION,
    { ...ESCALATION, id: "unowned_2", table_id: "t_def456", candidate_owner: OWNED_BY_RISK },
    { ...ESCALATION, id: "unowned_3", table_id: "t_ghi789", candidate_owner: "Finance Data" },
    { ...ESCALATION, id: "unowned_4", table_id: "t_jkl012", candidate_owner: OWNED_BY_RISK, status: "ROUTED" },
  ];

  function serveBacklogLikeTheRoute() {
    fetchUnownedAssetBacklog.mockImplementation(async (_org, query) => {
      const { status, candidateOwner } = query as { status?: string | null; candidateOwner?: string | null };
      const rows = BACKLOG_ROWS.filter(
        (row) =>
          (!status || row.status === status) &&
          (!candidateOwner || row.candidate_owner === candidateOwner),
      );
      return { items: rows, limit: 100, offset: 0, total: rows.length };
    });
  }

  function lastBacklogQuery(): unknown {
    return fetchUnownedAssetBacklog.mock.calls.at(-1)![1];
  }

  async function applyCandidateOwner(value: string) {
    fireEvent.change(screen.getByLabelText("Candidate owner"), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filter" }));
  }

  it("asks the server for one candidate owner instead of narrowing the loaded page itself", async () => {
    serveBacklogLikeTheRoute();
    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());
    expect(lastBacklogQuery()).toEqual({ status: null, limit: 100 });
    expect(screen.getByText("4 total")).toBeInTheDocument();

    await applyCandidateOwner(OWNED_BY_RISK);

    await waitFor(() =>
      expect(lastBacklogQuery()).toEqual({ status: null, limit: 100, candidateOwner: OWNED_BY_RISK }),
    );
    await waitFor(() => expect(screen.queryByText("t_abc123")).not.toBeInTheDocument());
    expect(screen.getByText("t_def456")).toBeInTheDocument();
    expect(screen.getByText("t_jkl012")).toBeInTheDocument();
    expect(screen.queryByText("t_ghi789")).not.toBeInTheDocument();
    // The total is the server's count of the matches, not the loaded page's length.
    expect(screen.getByText("2 for this owner")).toBeInTheDocument();
  });

  it("shows every row the server returns, and never says the filter only narrows what is loaded", async () => {
    // The server's answer is the answer: a row it returns is shown even when its owner is not the
    // text that was typed (a client-side filter would hide it), and no note claims otherwise.
    fetchUnownedAssetBacklog.mockResolvedValue(backlogPage(BACKLOG_ROWS));
    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());

    await applyCandidateOwner("Finance Data");

    await waitFor(() => expect(fetchUnownedAssetBacklog).toHaveBeenCalledTimes(2));
    for (const table of ["t_abc123", "t_def456", "t_ghi789", "t_jkl012"]) {
      expect(screen.getByText(table)).toBeInTheDocument();
    }
    expect(screen.queryByText(/only narrows what is already loaded/)).not.toBeInTheDocument();
    expect(screen.queryByText(/loaded rows match/)).not.toBeInTheDocument();
  });

  it("does not ask until the steward applies the value: typing alone makes no request", async () => {
    serveBacklogLikeTheRoute();
    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Candidate owner"), { target: { value: "risk" } });
    fireEvent.change(screen.getByLabelText("Candidate owner"), { target: { value: "risk-data" } });

    expect(fetchUnownedAssetBacklog).toHaveBeenCalledTimes(1);
    expect(screen.getByText("t_abc123")).toBeInTheDocument();
  });

  it("applies on Enter, trims what was typed, and sends nothing else about the owner", async () => {
    serveBacklogLikeTheRoute();
    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());

    const input = screen.getByLabelText("Candidate owner");
    fireEvent.change(input, { target: { value: `  ${OWNED_BY_RISK}  ` } });
    fireEvent.submit(input.closest("form")!);

    await waitFor(() =>
      expect(lastBacklogQuery()).toEqual({ status: null, limit: 100, candidateOwner: OWNED_BY_RISK }),
    );
  });

  it("matches exactly: a different case finds nothing, and the empty state says why", async () => {
    serveBacklogLikeTheRoute();
    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());

    await applyCandidateOwner("FINANCE DATA");

    expect(await screen.findByText("No unowned assets for this candidate owner")).toBeInTheDocument();
    expect(screen.getByText(/matched exactly as stored, capital letters included/)).toBeInTheDocument();
    expect(screen.queryByText("t_ghi789")).not.toBeInTheDocument();
  });

  it("keeps the status filter beside the candidate owner, and asks again when either changes", async () => {
    serveBacklogLikeTheRoute();
    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());

    await applyCandidateOwner(OWNED_BY_RISK);
    await waitFor(() => expect(screen.queryByText("t_abc123")).not.toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "ROUTED" } });

    await waitFor(() =>
      expect(lastBacklogQuery()).toEqual({ status: "ROUTED", limit: 100, candidateOwner: OWNED_BY_RISK }),
    );
    await waitFor(() => expect(screen.queryByText("t_def456")).not.toBeInTheDocument());
    expect(screen.getByText("t_jkl012")).toBeInTheDocument();
  });

  it("Clear filter asks for the whole backlog again", async () => {
    serveBacklogLikeTheRoute();
    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());
    await applyCandidateOwner(OWNED_BY_RISK);
    await waitFor(() => expect(screen.queryByText("t_abc123")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Clear filter" }));

    await waitFor(() => expect(lastBacklogQuery()).toEqual({ status: null, limit: 100 }));
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());
    expect(screen.getByLabelText("Candidate owner")).toHaveValue("");
    expect(screen.queryByRole("button", { name: "Clear filter" })).not.toBeInTheDocument();
    expect(screen.getByText("4 total")).toBeInTheDocument();
  });

  it("offers the owners it has seen as suggestions, and keeps offering them once the page is narrowed", async () => {
    serveBacklogLikeTheRoute();
    const StewardshipWorkQueue = await loadWorkQueue();
    const { container } = render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());
    const offered = () =>
      [...container.querySelectorAll("#stew-candidate-owners option")].map((option) =>
        option.getAttribute("value"),
      );
    expect(offered()).toEqual(["Finance Data", OWNED_BY_RISK]);

    await applyCandidateOwner(OWNED_BY_RISK);
    await waitFor(() => expect(screen.queryByText("t_ghi789")).not.toBeInTheDocument());

    expect(offered()).toEqual(["Finance Data", OWNED_BY_RISK]);
  });

  it("does not offer Apply when the value is already the applied one", async () => {
    serveBacklogLikeTheRoute();
    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Apply filter" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Candidate owner"), { target: { value: "Finance Data" } });
    expect(screen.getByRole("button", { name: "Apply filter" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Apply filter" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Apply filter" })).toBeDisabled());
  });

  it("17B: an explicit `?ids=` selection replaces the filter fields and sends table_ids, never both", async () => {
    history.replaceState(null, "", "/?ids=t1,t2&action=certify");
    const StewardshipBulkActions = await loadBulkActions();
    render(<StewardshipBulkActions />);

    // The filter fields are gone; the selection is a fact, not a form.
    expect(screen.queryByLabelText("Datasource")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Match pattern")).not.toBeInTheDocument();
    expect(screen.getByText("2 tables selected in Catalog")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Rationale"), {
      target: { value: "Quarterly certification review completed." },
    });
    bulkCertifyCatalogTables.mockResolvedValue(bulkRun({ action: "BULK_CERTIFY", selection_mode: "EXPLICIT" }));
    fireEvent.click(screen.getByRole("button", { name: "Run certify tables" }));

    await waitFor(() =>
      expect(bulkCertifyCatalogTables).toHaveBeenCalledWith(
        "00000000-0000-0000-0000-000000000001",
        expect.objectContaining({ table_ids: ["t1", "t2"] }),
        undefined,
      ),
    );
    const [, body] = bulkCertifyCatalogTables.mock.calls[0]!;
    expect(body as object).not.toHaveProperty("filter");
  });

  it("17B: 'Use a filter instead' clears the selection and brings the filter fields back", async () => {
    history.replaceState(null, "", "/?ids=t1&action=tag");
    const StewardshipBulkActions = await loadBulkActions();
    render(<StewardshipBulkActions />);
    expect(screen.getByText("1 table selected in Catalog")).toBeInTheDocument();
    expect(screen.queryByLabelText("Datasource")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Use a filter instead" }));

    expect(new URLSearchParams(location.search).has("ids")).toBe(false);
    await waitFor(() => expect(screen.getByLabelText("Datasource")).toBeInTheDocument());
    expect(screen.queryByText(/selected in Catalog/)).not.toBeInTheDocument();
  });

  it("routing the backlog calls the route endpoint, shows the summary, and refetches the backlog", async () => {
    routeUnownedAssetBacklog.mockResolvedValue({
      organization_id: "org1",
      routed: [{ ...ESCALATION, status: "ROUTED" }],
      escalated: [],
      escalated_tier2: [],
      resolved_count: 0,
    });

    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());
    expect(fetchUnownedAssetBacklog).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Route backlog" }));

    await waitFor(() =>
      expect(routeUnownedAssetBacklog).toHaveBeenCalledWith(
        "00000000-0000-0000-0000-000000000001",
        { datasource_id: null },
        undefined,
      ),
    );
    const summary = await screen.findByLabelText("Route backlog result");
    expect(within(summary).getByText("1")).toBeInTheDocument();
    expect(within(summary).getByText("routed")).toBeInTheDocument();
    await waitFor(() => expect(fetchUnownedAssetBacklog).toHaveBeenCalledTimes(2));
  });

  it("scoping the route to a datasource sends that datasource_id, and the status filter re-fetches with the right query", async () => {
    const StewardshipWorkQueue = await loadWorkQueue();
    render(<StewardshipWorkQueue />);
    await waitFor(() => expect(screen.getAllByText("snowflake_prod").length).toBeGreaterThan(0));

    fireEvent.change(screen.getByLabelText("Route scope (optional)"), { target: { value: "ds_1" } });
    routeUnownedAssetBacklog.mockResolvedValue({
      organization_id: "org1", routed: [], escalated: [], escalated_tier2: [], resolved_count: 0,
    });
    fireEvent.click(screen.getByRole("button", { name: "Route backlog" }));
    await waitFor(() =>
      expect(routeUnownedAssetBacklog).toHaveBeenCalledWith(
        "00000000-0000-0000-0000-000000000001",
        { datasource_id: "ds_1" },
        undefined,
      ),
    );

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "RESOLVED" } });
    await waitFor(() =>
      expect(fetchUnownedAssetBacklog).toHaveBeenLastCalledWith(
        "00000000-0000-0000-0000-000000000001",
        { status: "RESOLVED", limit: 100 },
        expect.anything(),
      ),
    );
  });
});

/* ---------------------------------------------------------------------------
   R11-S13 (items 15/17) — an unrun bulk action is unsaved work.

   On the old page nothing could unmount the bulk form: it shared the screen
   with the backlog, and leaving the screen was the only way to lose it. As a
   workspace view it can be unmounted by a tab switch, which is a `patchQuery`
   and so bypasses the shell's own navigation guard. The workspace's tab bar
   asks `lib/unsavedChanges` -- and that only protects anything if the form
   reports into it. These cases pin the reporting; the tab bar's side is in
   `StewardshipWorkspace.test.tsx`.

   The registry is imported AFTER `vi.resetModules()` (in `beforeEach`), in
   the same test as the component, so both talk to one module instance.
--------------------------------------------------------------------------- */
describe("the bulk form reports unsaved work", () => {
  async function loadRegistry() {
    return import("../lib/unsavedChanges");
  }

  it("reports an edited action field, and stops once that action has run", async () => {
    const { pendingUnsavedWarning } = await loadRegistry();
    const { StewardshipBulkActions, BULK_UNSAVED_MESSAGE } = await import("./StewardshipScreen");
    render(<StewardshipBulkActions />);
    await waitFor(() => expect(screen.getAllByText("snowflake_prod").length).toBeGreaterThan(0));

    expect(pendingUnsavedWarning()).toBeNull();

    fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: "ds_1" } });
    fireEvent.change(screen.getByLabelText("Match pattern"), { target: { value: "raw_%" } });
    // The filter is in the URL, which a tab switch keeps -- so it is not
    // unsaved work, and a prompt about it would be a false alarm.
    expect(pendingUnsavedWarning()).toBeNull();

    fireEvent.change(screen.getByLabelText("Tag key"), { target: { value: "pii-reviewed" } });
    await waitFor(() => expect(pendingUnsavedWarning()).toBe(BULK_UNSAVED_MESSAGE));

    fireEvent.click(screen.getByRole("button", { name: "Run tag tables" }));
    await waitFor(() => expect(bulkTagCatalogTables).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(pendingUnsavedWarning()).toBeNull());
  });

  it("keeps reporting when the run fails, because the values are still unfinished work", async () => {
    bulkTagCatalogTables.mockRejectedValue(new Error("403: one of these roles is required"));
    const { pendingUnsavedWarning } = await loadRegistry();
    const { StewardshipBulkActions, BULK_UNSAVED_MESSAGE } = await import("./StewardshipScreen");
    render(<StewardshipBulkActions />);
    await waitFor(() => expect(screen.getAllByText("snowflake_prod").length).toBeGreaterThan(0));

    fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: "ds_1" } });
    fireEvent.change(screen.getByLabelText("Match pattern"), { target: { value: "raw_%" } });
    fireEvent.change(screen.getByLabelText("Tag key"), { target: { value: "pii-reviewed" } });
    fireEvent.click(screen.getByRole("button", { name: "Run tag tables" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("one of these roles is required");
    expect(pendingUnsavedWarning()).toBe(BULK_UNSAVED_MESSAGE);
  });
});
