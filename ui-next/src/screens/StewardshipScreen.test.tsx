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

const fetchOrgDatasources = vi.fn<
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
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgDatasources(organizationId, signal),
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

async function loadScreen() {
  const { StewardshipScreen } = await import("./StewardshipScreen");
  return StewardshipScreen;
}

beforeEach(() => {
  fetchOrgDatasources.mockReset();
  bulkTagCatalogTables.mockReset();
  bulkClassifyCatalogColumns.mockReset();
  bulkAssignCatalogOwnership.mockReset();
  bulkCertifyCatalogTables.mockReset();
  fetchUnownedAssetBacklog.mockReset();
  routeUnownedAssetBacklog.mockReset();

  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
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
    const StewardshipScreen = await loadScreen();
    render(<StewardshipScreen />);

    await waitFor(() => expect(fetchUnownedAssetBacklog).toHaveBeenCalledWith(
      "00000000-0000-0000-0000-000000000001",
      { status: null, limit: 100 },
      expect.anything(),
    ));
    await waitFor(() => expect(screen.getByText("t_abc123")).toBeInTheDocument());
    expect(screen.getAllByText("snowflake_prod").length).toBeGreaterThan(0);
  });

  it("submitting the default tag action posts the right filter body and renders the results panel", async () => {
    const StewardshipScreen = await loadScreen();
    render(<StewardshipScreen />);
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
    const StewardshipScreen = await loadScreen();
    render(<StewardshipScreen />);
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

  it("routing the backlog calls the route endpoint, shows the summary, and refetches the backlog", async () => {
    routeUnownedAssetBacklog.mockResolvedValue({
      organization_id: "org1",
      routed: [{ ...ESCALATION, status: "ROUTED" }],
      escalated: [],
      escalated_tier2: [],
      resolved_count: 0,
    });

    const StewardshipScreen = await loadScreen();
    render(<StewardshipScreen />);
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
    const StewardshipScreen = await loadScreen();
    render(<StewardshipScreen />);
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
