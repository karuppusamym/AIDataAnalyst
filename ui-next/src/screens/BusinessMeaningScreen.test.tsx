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

const fetchCatalogRows = vi.fn();
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
    fetchCatalogRows: (query: unknown, signal?: AbortSignal) => fetchCatalogRows(query, signal),
  };
});

/* P1-03: mock the glossary wrappers landed in `_api_append.ts` so the
 *  Glossary tab can be exercised without hitting `fetch`. Same pattern as
 *  the primary `../lib/api` mock above. */
const listGlossaryTerms = vi.fn();
const createGlossaryTerm = vi.fn();
const submitGlossaryTermVersion = vi.fn();
const linkTermToTable = vi.fn();

vi.mock("../lib/_api_append", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/_api_append")>();
  return {
    ...actual,
    // Spread-forwarded so each spy records exactly the arguments the caller
    // passed. Re-passing named parameters appended an explicit `undefined`
    // for every omitted optional, so a two-argument call was recorded as
    // three and every `toHaveBeenCalledWith` on it failed.
    listGlossaryTerms: (...args: unknown[]) =>
      (listGlossaryTerms as (...a: unknown[]) => unknown)(...args),
    createGlossaryTerm: (...args: unknown[]) =>
      (createGlossaryTerm as (...a: unknown[]) => unknown)(...args),
    submitGlossaryTermVersion: (...args: unknown[]) =>
      (submitGlossaryTermVersion as (...a: unknown[]) => unknown)(...args),
    linkTermToTable: (...args: unknown[]) =>
      (linkTermToTable as (...a: unknown[]) => unknown)(...args),
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
  fetchCatalogRows.mockReset();
  listGlossaryTerms.mockReset();
  createGlossaryTerm.mockReset();
  submitGlossaryTermVersion.mockReset();
  linkTermToTable.mockReset();
  listGlossaryTerms.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
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


/* ---------------------------------------------------------------------------
   P1-03: BusinessMeaning Glossary tab. Backed by the wrappers in
   `_api_append.ts`; mocked above. Covers list-scoped-by-business-node,
   create-term (auto-submits for review), and link-to-asset.
--------------------------------------------------------------------------- */

const APPROVED_TERM = {
  id: "ver_1",
  organization_id: "org1",
  term_id: "term_1",
  term_key: "mrr",
  category_id: "node_finance",
  lifecycle_status: "ACTIVE",
  version: 1,
  status: "APPROVED",
  display_name: "Monthly Recurring Revenue",
  definition: "Recurring revenue normalized to a monthly cadence.",
  synonyms: ["MRR", "recurring revenue"],
  owner_principal: "priya@tenant.example",
  created_by: "priya@tenant.example",
  approved_by: "priya@tenant.example",
  approved_at: "2026-08-14T00:00:00Z",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-14T00:00:00Z",
};

const DRAFT_TERM = { ...APPROVED_TERM, id: "ver_2", term_id: "term_2", term_key: "arr", display_name: "Annual Recurring Revenue", status: "DRAFT" };

describe("BusinessMeaningScreen Glossary tab (P1-03)", () => {
  it("lists glossary terms scoped by business_node when ?node= is set", async () => {
    listGlossaryTerms.mockResolvedValue({ items: [APPROVED_TERM], limit: 200, offset: 0, total: 1 });
    history.replaceState(null, "", "/?view=glossary&node=node_finance");
    const BusinessMeaningScreen = await loadScreen();

    render(<BusinessMeaningScreen />);

    await waitFor(() =>
      expect(listGlossaryTerms).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({ businessNodeId: "node_finance", limit: 200 }),
      ),
    );
    expect(await screen.findByText("Monthly Recurring Revenue")).toBeInTheDocument();
    expect(screen.getByText("mrr")).toBeInTheDocument();
  });

  it("create-term flow calls createGlossaryTerm then submitGlossaryTermVersion", async () => {
    listGlossaryTerms.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
    createGlossaryTerm.mockResolvedValue({ ...DRAFT_TERM, id: "ver_new" });
    submitGlossaryTermVersion.mockResolvedValue({ id: "gr_1" });
    history.replaceState(null, "", "/?view=glossary&node=node_finance");
    const BusinessMeaningScreen = await loadScreen();
    render(<BusinessMeaningScreen />);

    await waitFor(() => expect(listGlossaryTerms).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Create term" }));

    const dialog = await screen.findByRole("dialog", { name: "Create glossary term" });
    fireEvent.change(within(dialog).getByLabelText("Display name"), { target: { value: "Annual Recurring Revenue" } });
    fireEvent.change(within(dialog).getByLabelText("Term key"), { target: { value: "arr" } });
    fireEvent.change(within(dialog).getByLabelText("Definition"), {
      target: { value: "Recurring revenue normalized to an annual cadence." },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /Create and submit/ }));

    await waitFor(() => expect(createGlossaryTerm).toHaveBeenCalledTimes(1));
    expect(createGlossaryTerm).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        term_key: "arr",
        display_name: "Annual Recurring Revenue",
        business_node_id: "node_finance",
      }),
    );
    await waitFor(() => expect(submitGlossaryTermVersion).toHaveBeenCalledWith("ver_new"));
  });

  it("link-to-asset flow calls linkTermToTable with the selected table id and term_id", async () => {
    listGlossaryTerms.mockResolvedValue({ items: [APPROVED_TERM], limit: 200, offset: 0, total: 1 });
    fetchCatalogRows.mockResolvedValue({
      items: [{ id: "t_1", name: "mrr_daily", schema_name: "finance", datasource_name: "snowflake_prod", object_type: "TABLE", status: "ACTIVE", description: null, description_is_proposed: false, owner: null, certification: "NONE", certification_expires_at: null, quality: "PASSING", glossary_terms: [], row_count_estimate: 100, updated_at: "2026-09-01T00:00:00Z" }],
      limit: 25, offset: 0, total: 1, next_cursor: null,
    });
    linkTermToTable.mockResolvedValue({ id: "link_1", term_id: "term_1", table_id: "t_1" });
    history.replaceState(null, "", "/?view=glossary");
    const BusinessMeaningScreen = await loadScreen();
    render(<BusinessMeaningScreen />);

    await waitFor(() => expect(screen.getByText("Monthly Recurring Revenue")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Link to asset/ }));

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Search asset by name"), { target: { value: "mrr" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Search" }));
    await waitFor(() => expect(fetchCatalogRows).toHaveBeenCalled());
    fireEvent.click(await within(dialog).findByRole("option", { name: /finance\.mrr_daily/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: /Link to finance\.mrr_daily/ }));

    await waitFor(() =>
      // Four arguments, not five: the screen omits the optional signal, and
      // the mock now forwards exactly what it was given rather than padding.
      expect(linkTermToTable).toHaveBeenCalledWith(
        expect.any(String),
        "t_1",
        "term_1",
        expect.any(Object),
      ),
    );
  });
});
