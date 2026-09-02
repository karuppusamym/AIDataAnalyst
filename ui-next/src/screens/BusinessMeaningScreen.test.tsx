import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { DataSourceRead, MetadataBusinessAnnotationRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   UX-16: Business meaning on the Catalog pattern against the real
   `GET /v1/datasources/{id}/business-annotations` /
   `GET /v1/metadata/tables/{id}/business-annotation`
   (`semantic_intelligence_api.py`). Mocks the API boundary, matching
   `MarketplaceScreen.test.tsx`'s established pattern -- real payload shapes,
   asserting the exact endpoint/args called, not superficial snapshots.
--------------------------------------------------------------------------- */

const fetchOrgDatasources =
  vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>>();
const fetchBusinessAnnotations = vi.fn<
  (query: unknown, signal?: AbortSignal) => Promise<PageOf<MetadataBusinessAnnotationRead>>
>();
const fetchTableBusinessAnnotation = vi.fn<
  (tableId: string, signal?: AbortSignal) => Promise<MetadataBusinessAnnotationRead>
>();
const fetchBusinessMap = vi.fn<(query: unknown, signal?: AbortSignal) => Promise<unknown>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgDatasources(organizationId, signal),
    fetchBusinessAnnotations: (query: unknown, signal?: AbortSignal) =>
      fetchBusinessAnnotations(query, signal),
    fetchTableBusinessAnnotation: (tableId: string, signal?: AbortSignal) =>
      fetchTableBusinessAnnotation(tableId, signal),
    fetchBusinessMap: (query: unknown, signal?: AbortSignal) => fetchBusinessMap(query, signal),
  };
});

const DATASOURCE: DataSourceRead = {
  id: "ds_1", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  project_id: "proj1", name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake",
  environment: "PRODUCTION", credential_reference: "vault://x", status: "ACTIVE", capabilities: {},
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const ANNOTATION: MetadataBusinessAnnotationRead = {
  id: "ann_1", organization_id: "org1", datasource_id: "ds_1", table_id: "t_customer_dim",
  schema_name: "core", table_name: "customer_dim",
  domain_id: "dom_fin", domain_key: "finance", domain_name: "Finance",
  entity_id: "ent_customer", entity_key: "customer", entity_name: "Customer",
  source_proposal_id: "prop_1", version: 1,
  business_name: "Customer",
  business_description: "One row per customer the organization has a banking relationship with.",
  table_role: "DIMENSION", grain_statement: "One row per customer_id.",
  synonyms: ["client"], suggested_questions: ["How many active customers do we have?"],
  tags: ["pii"], confidence: 0.93,
  approved_by: "priya@tenant.example", approved_at: "2026-08-14T00:00:00Z",
  created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-14T00:00:00Z",
};

async function loadScreen() {
  const { BusinessMeaningScreen } = await import("./BusinessMeaningScreen");
  return BusinessMeaningScreen;
}

beforeEach(() => {
  fetchOrgDatasources.mockReset();
  fetchBusinessAnnotations.mockReset();
  fetchTableBusinessAnnotation.mockReset();
  fetchBusinessMap.mockReset();
  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  fetchBusinessAnnotations.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("BusinessMeaningScreen against the real UX-16 endpoints", () => {
  it("does not fetch business annotations before a datasource is selected", async () => {
    const BusinessMeaningScreen = await loadScreen();
    render(<BusinessMeaningScreen />);

    await waitFor(() => expect(fetchOrgDatasources).toHaveBeenCalled());
    expect(
      screen.getByText("Pick a datasource to see its business annotations"),
    ).toBeInTheDocument();
    expect(fetchBusinessAnnotations).not.toHaveBeenCalled();
  });

  it("picking a datasource loads its business annotations", async () => {
    fetchBusinessAnnotations.mockResolvedValue({ items: [ANNOTATION], limit: 100, offset: 0, total: 1 });
    const BusinessMeaningScreen = await loadScreen();
    render(<BusinessMeaningScreen />);
    await waitFor(() => expect(screen.getByRole("combobox")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: "ds_1" } });

    await waitFor(() =>
      expect(fetchBusinessAnnotations).toHaveBeenCalledWith(
        expect.objectContaining({ datasourceId: "ds_1" }),
        expect.anything(),
      ),
    );
    expect(await screen.findByRole("button", { name: /Customer/ })).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("ds")).toBe("ds_1");
  });

  it("selecting a row opens the evidence panel with a permalink URL param", async () => {
    fetchBusinessAnnotations.mockResolvedValue({ items: [ANNOTATION], limit: 100, offset: 0, total: 1 });
    fetchTableBusinessAnnotation.mockResolvedValue(ANNOTATION);
    history.replaceState(null, "", "/?ds=ds_1");
    const BusinessMeaningScreen = await loadScreen();
    render(<BusinessMeaningScreen />);

    await waitFor(() =>
      expect(fetchBusinessAnnotations).toHaveBeenCalledWith(
        expect.objectContaining({ datasourceId: "ds_1" }),
        expect.anything(),
      ),
    );
    const row = await screen.findByRole("button", { name: /Customer/ });
    fireEvent.click(row);

    expect(new URLSearchParams(location.search).get("asset")).toBe("t_customer_dim");
    await waitFor(() =>
      expect(fetchTableBusinessAnnotation).toHaveBeenCalledWith("t_customer_dim", expect.anything()),
    );
    const panel = await screen.findByLabelText("Business meaning for Customer");
    expect(
      within(panel).getByText("One row per customer the organization has a banking relationship with."),
    ).toBeInTheDocument();
    expect(within(panel).getByText("One row per customer_id.")).toBeInTheDocument();
  });

  it("surfaces a fetch error with a retry action", async () => {
    fetchBusinessAnnotations.mockRejectedValue(new ApiError(403, "policy_denied"));
    history.replaceState(null, "", "/?ds=ds_1");
    const BusinessMeaningScreen = await loadScreen();

    render(<BusinessMeaningScreen />);

    await waitFor(() => expect(screen.getByText("policy_denied")).toBeInTheDocument());
  });
});
