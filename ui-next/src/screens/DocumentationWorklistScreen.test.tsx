import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { DocumentationWorklistScreen } from "./DocumentationWorklistScreen";
import type { DocumentationWorklistEntryRead } from "../lib/types";

const fetchDocumentationWorklist = vi.fn();

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    fetchDocumentationWorklist: (...args: unknown[]) => fetchDocumentationWorklist(...args),
  };
});

const ORG = "00000000-0000-0000-0000-000000000001";

const ROW: DocumentationWorklistEntryRead = {
  table_id: "t_1",
  table_name: "customer_master",
  schema_name: "raw_sales",
  datasource_name: "snowflake_prod",
  rank: 1,
  query_execution_count: 500,
  consumption_read_count: 100,
  query_volume: 600,
  last_queried_at: new Date().toISOString(),
  last_consumed_at: null,
  description_is_proposed: false,
  score: 0.5,
  usage: 0.9,
  impact: 0.8,
  deficit: 0.7,
  downstream_count: 5,
  missing: ["description", "owner"],
};

describe("DocumentationWorklistScreen (AT-5 / SW-1)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    history.replaceState(null, "", "/");
    fetchDocumentationWorklist.mockResolvedValue({ items: [ROW], limit: 100, offset: 0, total: 1 });
  });

  it("loads with the priority ranking by default", async () => {
    render(<DocumentationWorklistScreen />);

    await waitFor(() => expect(screen.getByText("raw_sales.customer_master")).toBeInTheDocument());
    expect(fetchDocumentationWorklist).toHaveBeenCalledWith(
      ORG,
      expect.objectContaining({ ranking: "priority", includeZeroVolume: false }),
      expect.anything(),
    );
  });

  it("shows the score and the usage/impact/deficit factors", async () => {
    render(<DocumentationWorklistScreen />);

    await waitFor(() => expect(screen.getByText("0.500")).toBeInTheDocument());
    expect(screen.getByText("90%")).toBeInTheDocument();
    expect(screen.getByText("80%")).toBeInTheDocument();
    expect(screen.getByText("70%")).toBeInTheDocument();
  });

  it("names what's missing as pills", async () => {
    render(<DocumentationWorklistScreen />);

    await waitFor(() => expect(screen.getByText("missing description")).toBeInTheDocument());
    expect(screen.getByText("missing owner")).toBeInTheDocument();
  });

  it("switching ranking to query_volume re-fetches with the new ranking", async () => {
    render(<DocumentationWorklistScreen />);
    await waitFor(() => expect(fetchDocumentationWorklist).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("Ranking"), { target: { value: "query_volume" } });

    await waitFor(() => expect(fetchDocumentationWorklist).toHaveBeenCalledTimes(2));
    expect(fetchDocumentationWorklist.mock.calls[1]![1]).toEqual(
      expect.objectContaining({ ranking: "query_volume" }),
    );
    expect(location.search).toContain("ranking=query_volume");
  });

  it("toggling include-zero-volume re-fetches with includeZeroVolume true", async () => {
    render(<DocumentationWorklistScreen />);
    await waitFor(() => expect(fetchDocumentationWorklist).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByLabelText("Include never-queried tables"));

    await waitFor(() => expect(fetchDocumentationWorklist).toHaveBeenCalledTimes(2));
    expect(fetchDocumentationWorklist.mock.calls[1]![1]).toEqual(
      expect.objectContaining({ includeZeroVolume: true }),
    );
  });

  it("shows an empty state when nothing ranks above zero", async () => {
    fetchDocumentationWorklist.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
    render(<DocumentationWorklistScreen />);

    await waitFor(() => expect(screen.getByText("Nothing ranks above zero right now")).toBeInTheDocument());
  });

  it("renders an error state with retry on failure", async () => {
    fetchDocumentationWorklist.mockRejectedValue(new Error("boom"));
    render(<DocumentationWorklistScreen />);

    await waitFor(() =>
      expect(screen.getByText(/documentation worklist could not be loaded/i)).toBeInTheDocument(),
    );
  });
});
