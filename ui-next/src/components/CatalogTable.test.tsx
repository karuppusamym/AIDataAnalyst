import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CatalogTable } from "./CatalogTable";
import type { CatalogRowRead } from "../lib/ui-types";

/* ---------------------------------------------------------------------------
   P1-03: glossary chips on catalog rows. Server ships `row.glossary_terms:
   string[]` via `_glossary_terms_by_table`; the table renders up to 3
   inline chips and a "+N more" chip when the row has 4 or more terms.
   These tests exercise the render side only -- click behaviour is a
   `location.href` navigation asserted in the smoke test at the bottom.
--------------------------------------------------------------------------- */

function rowFactory(overrides: Partial<CatalogRowRead>): CatalogRowRead {
  return {
    id: "t_1",
    name: "orders_raw",
    schema_name: "public",
    datasource_name: "snowflake_prod",
    object_type: "TABLE",
    status: "ACTIVE",
    description: "raw orders from the source system",
    description_is_proposed: false,
    owner: "priya",
    certification: "NONE",
    certification_expires_at: null,
    certification_evidence_summary: null,
    quality: "PASSING",
    glossary_terms: [],
    row_count_estimate: 1000,
    updated_at: "2026-09-02T00:00:00Z",
    ...overrides,
  };
}

function renderTable(rows: CatalogRowRead[]) {
  return render(
    <CatalogTable
      rows={rows}
      totalCount={rows.length}
      selectedId={null}
      checked={new Set()}
      onSelect={() => {}}
      onToggleCheck={() => {}}
      onToggleAllVisible={() => {}}
      onReachEnd={() => {}}
      loadingMore={false}
    />,
  );
}

describe("CatalogTable glossary chips (P1-03)", () => {
  it("renders no chip container when the row has no glossary terms", () => {
    renderTable([rowFactory({ glossary_terms: [] })]);
    expect(screen.queryByRole("list", { name: /glossary term/ })).not.toBeInTheDocument();
  });

  it("renders one chip per term when there are 1..3", () => {
    renderTable([rowFactory({ glossary_terms: ["Revenue", "MRR", "ARR"] })]);
    const list = screen.getByRole("list", { name: /3 glossary terms/ });
    expect(list).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Revenue" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "MRR" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "ARR" })).toBeInTheDocument();
    // No overflow chip at 3.
    expect(screen.queryByText(/more/)).not.toBeInTheDocument();
  });

  it("renders the +N more chip when there are 4 or more glossary terms", () => {
    renderTable([rowFactory({ glossary_terms: ["A", "B", "C", "D", "E"] })]);
    // Only the first three individual term chips are rendered.
    expect(screen.getByRole("listitem", { name: "A" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "B" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "C" })).toBeInTheDocument();
    expect(
      screen.queryByRole("listitem", { name: "D" }),
    ).not.toBeInTheDocument();
    // ...but the +N more chip absorbs the tail.
    expect(screen.getByText("+2 more")).toBeInTheDocument();
  });
});
