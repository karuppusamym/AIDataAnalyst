import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { CatalogRowRead, CursorPage } from "../lib/ui-types";
import type { AssetDescriptionDraftRead } from "../lib/types";

/* ---------------------------------------------------------------------------
   P1-04: only the description-draft wiring is covered here. The catalog
   fetch/filter/pagination behaviour is exercised by the shared useUrlState
   hook tests and by the CatalogScreen fixture path -- this file just proves
   that the "Generate description draft" and bulk actions call
   generateAssetDescriptionDrafts with the right table_ids.
--------------------------------------------------------------------------- */

const fetchCatalogRows = vi.fn<
  (query: unknown, signal?: AbortSignal) => Promise<CursorPage<CatalogRowRead>>
>();
const generateAssetDescriptionDrafts = vi.fn<
  (organizationId: string, tableIds: string[], signal?: AbortSignal) => Promise<{
    drafts: AssetDescriptionDraftRead[];
    limit: number;
    offset: number;
    total: number;
  }>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchCatalogRows: (query: unknown, signal?: AbortSignal) => fetchCatalogRows(query, signal),
    // Spread-forwarded so the spy records exactly what the caller passed;
    // re-passing named parameters appended an explicit `undefined` for the
    // omitted signal and broke every two-argument assertion.
    generateAssetDescriptionDrafts: (...args: unknown[]) =>
      (generateAssetDescriptionDrafts as (...a: unknown[]) => unknown)(...args),
  };
});

// Virtualizer needs some measurable geometry; jsdom returns 0 without help.
vi.mock("../components/CatalogTable", async () => {
  const React = await import("react");
  return {
    CatalogTable: ({
      rows,
      onToggleCheck,
      onSelect,
    }: {
      rows: CatalogRowRead[];
      onToggleCheck: (id: string) => void;
      onSelect: (row: CatalogRowRead) => void;
    }) =>
      React.createElement(
        "div",
        { "data-testid": "catalog-table-stub" },
        rows.map((r) =>
          React.createElement(
            "div",
            { key: r.id },
            React.createElement(
              "button",
              { onClick: () => onSelect(r), "data-testid": `select-${r.id}` },
              r.name,
            ),
            React.createElement(
              "button",
              { onClick: () => onToggleCheck(r.id), "data-testid": `check-${r.id}` },
              `check ${r.id}`,
            ),
          ),
        ),
      ),
  };
});

// EvidencePane fetches its own data; stub it out for isolation.
vi.mock("../components/EvidencePane", async () => {
  const React = await import("react");
  return {
    EvidencePane: ({ tableId }: { tableId: string | null }) =>
      tableId
        ? React.createElement("div", { "data-testid": "evidence-pane" }, `evp:${tableId}`)
        : null,
  };
});

const ROW: CatalogRowRead = {
  id: "t1",
  name: "orders_raw",
  schema_name: "public",
  datasource_id: "ds_snowflake_prod",
  datasource_name: "snowflake_prod",
  object_type: "TABLE",
  status: "ACTIVE",
  description: null,
  description_is_proposed: false,
  owner: null,
  certification: "NONE",
  certification_expires_at: null,
  quality: "PASSING",
  certification_evidence_summary: null,
  glossary_terms: [],
  row_count_estimate: 1000,
  updated_at: "2026-09-02T00:00:00Z",
};

beforeEach(() => {
  fetchCatalogRows.mockReset();
  generateAssetDescriptionDrafts.mockReset();
  fetchCatalogRows.mockResolvedValue({
    items: [ROW, { ...ROW, id: "t2", name: "sessions_daily" }],
    limit: 100,
    offset: 0,
    total: 2,
    next_cursor: null,
  });
  generateAssetDescriptionDrafts.mockResolvedValue({
    drafts: [
      {
        id: "d1",
        organization_id: "org1",
        table_id: "t1",
        table_name: "orders_raw",
        drafted_text: "…",
        accuracy_score: 0.8,
        clarity_score: 0.8,
        style_score: 0.8,
        completeness_score: 0.8,
        overall_score: 0.8,
        evidence: {},
        status: "DRAFT",
        governance_review_id: null,
        published_version_id: null,
        created_by: "me",
        reviewed_by: null,
        reviewed_at: null,
        created_at: "2026-09-02T00:00:00Z",
        updated_at: "2026-09-02T00:00:00Z",
      },
    ],
    limit: 100,
    offset: 0,
    total: 1,
  });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CatalogScreen -- P1-04 draft generation wiring", () => {
  it("single-asset 'Generate description draft' action calls the API with just that table id", async () => {
    const { CatalogScreen } = await import("./CatalogScreen");
    render(<CatalogScreen />);

    // Select a row -- opens the EvidencePane and reveals the per-row action.
    fireEvent.click(await screen.findByTestId("select-t1"));

    const btn = await screen.findByRole("button", { name: /generate description draft$/i });
    fireEvent.click(btn);

    await waitFor(() =>
      expect(generateAssetDescriptionDrafts).toHaveBeenCalledWith(
        expect.any(String),
        ["t1"],
      ),
    );

    // Success banner offers the deep link.
    expect(await screen.findByText(/1 draft generated/i)).toBeInTheDocument();
  });

  it("bulk 'Generate description drafts' calls the API with the checked table ids", async () => {
    const { CatalogScreen } = await import("./CatalogScreen");
    render(<CatalogScreen />);

    fireEvent.click(await screen.findByTestId("check-t1"));
    fireEvent.click(await screen.findByTestId("check-t2"));

    fireEvent.click(await screen.findByRole("button", { name: /generate description drafts$/i }));

    await waitFor(() =>
      expect(generateAssetDescriptionDrafts).toHaveBeenCalledWith(
        expect.any(String),
        expect.arrayContaining(["t1", "t2"]),
      ),
    );
    const [, ids] = generateAssetDescriptionDrafts.mock.calls[0]!;
    expect((ids as string[]).length).toBe(2);
  });
});
