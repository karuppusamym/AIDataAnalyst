import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ParsedLineageEdgeReviewQueueItemRead } from "../lib/types";

/* ---------------------------------------------------------------------------
   T18 — the parsed lineage queue decides inside the same shell as the
   governance queue, and keeps its own edge-type evidence.

   What is asserted here is the part that was missing rather than merely
   restyled: there was no detail pane at all, so the parser reference, the
   confidence coercion and the edge type were only ever visible as table cells
   with no decision beside them, and a refusal about an edge's own state was
   written into the screen's LOAD-error strip ("Could not load queue") where a
   reviewer reads it as the queue being broken.
--------------------------------------------------------------------------- */

const listParsedLineageReviewQueue = vi.fn();
const decideParsedLineageEdge = vi.fn();
const bulkDecideParsedLineageEdges = vi.fn();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    listParsedLineageReviewQueue: (query: unknown, signal?: AbortSignal) =>
      listParsedLineageReviewQueue(query, signal),
    decideParsedLineageEdge: (edgeId: string, body: unknown) =>
      decideParsedLineageEdge(edgeId, body),
    bulkDecideParsedLineageEdges: (body: unknown) => bulkDecideParsedLineageEdges(body),
  };
});

const EDGE: ParsedLineageEdgeReviewQueueItemRead = {
  edge_id: "edge_1",
  edge_type: "DBT",
  organization_id: "org1",
  created_at: "2026-09-01T00:00:00Z",
  created_by: "dbt_parser",
  confidence: "PARTIAL",
  source_label: "raw.orders.amount",
  target_label: "mart.revenue.amount",
  transformation_type: "SUM",
  source_sql_reference: { kind: "DBT_MODEL", model: "mart_revenue" },
};

async function loadScreen() {
  const { ParsedLineageReviewScreen } = await import("./ParsedLineageReviewScreen");
  return ParsedLineageReviewScreen;
}

beforeEach(() => {
  listParsedLineageReviewQueue.mockReset();
  decideParsedLineageEdge.mockReset();
  bulkDecideParsedLineageEdges.mockReset();
  listParsedLineageReviewQueue.mockResolvedValue({ items: [EDGE], total: 1 });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ParsedLineageReviewScreen detail", () => {
  it("opens the shared review detail with this edge type's own evidence", async () => {
    const ParsedLineageReviewScreen = await loadScreen();
    render(<ParsedLineageReviewScreen />);

    await waitFor(() => expect(screen.getByText("raw.orders.amount")).toBeInTheDocument());
    screen.getByRole("button", { name: /raw\.orders\.amount/ }).click();

    const pane = await screen.findByLabelText("Parsed lineage edge detail");
    expect(within(pane).getByText(/raw\.orders\.amount → mart\.revenue\.amount/)).toBeInTheDocument();
    // The type-specific slot: the parser's own reference back to the SQL, and
    // the string confidence with the float it is coerced to for filtering.
    const evidence = within(pane).getByLabelText("Evidence");
    expect(within(evidence).getByText("mart_revenue")).toBeInTheDocument();
    expect(within(evidence).getByText(/PARTIAL \(coerced to 0\.60\)/)).toBeInTheDocument();
    // Selection is in the URL, so the pane is shareable.
    expect(new URLSearchParams(location.search).get("review")).toBe("DBT:edge_1");
  });

  it("requires a written rationale for both verdicts before calling the endpoint", async () => {
    const ParsedLineageReviewScreen = await loadScreen();
    render(<ParsedLineageReviewScreen />);
    await waitFor(() => expect(screen.getByText("raw.orders.amount")).toBeInTheDocument());
    screen.getByRole("button", { name: /raw\.orders\.amount/ }).click();
    const pane = await screen.findByLabelText("Parsed lineage edge detail");

    within(pane).getByRole("button", { name: "Approve edge" }).click();
    const dialog = await screen.findByRole("dialog", { name: "Approve this review" });
    expect(within(dialog).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(decideParsedLineageEdge).not.toHaveBeenCalled();

    fireEvent.change(within(dialog).getByRole("textbox"), {
      target: { value: "Matches the dbt manifest." },
    });
    within(dialog).getByRole("button", { name: "Approve" }).click();

    await waitFor(() =>
      expect(decideParsedLineageEdge).toHaveBeenCalledWith("edge_1", {
        edge_type: "DBT",
        decision: "APPROVED",
        reason: "Matches the dbt manifest.",
      }),
    );
  });

  it("shows an already-decided edge as a refusal on the edge, not as a load failure", async () => {
    const ParsedLineageReviewScreen = await loadScreen();
    const { ApiError } = await import("../lib/http");
    decideParsedLineageEdge.mockRejectedValue(
      new ApiError(409, "parsed lineage edge is already approved"),
    );
    render(<ParsedLineageReviewScreen />);
    await waitFor(() => expect(screen.getByText("raw.orders.amount")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Decision reason"), {
      target: { value: "Looks right." },
    });
    screen.getAllByRole("button", { name: "Approve" })[0]?.click();

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("parsed lineage edge is already approved");
    // This endpoint sends no refreshed snapshot, so no winner is claimed.
    expect(banner).toHaveTextContent("This decision was refused");
    expect(screen.queryByText("Could not load queue")).not.toBeInTheDocument();
  });
});

/* --------------------------------------------------------------------------
   P1-05's original queue-level guarantees, unchanged in intent: the table is
   still the queue, and the bulk path still refuses without a rationale, sends
   only what was selected, and reports per-item failures. The detail shell
   above is an addition beside them, not a replacement for them.
-------------------------------------------------------------------------- */

const VIEW_EDGE = {
  edge_id: "edge-1",
  edge_type: "VIEW",
  source_label: "orders",
  target_label: "revenue",
  confidence: 1,
  source_sql_reference: {},
  created_by: "author",
} as unknown as ParsedLineageEdgeReviewQueueItemRead;

describe("ParsedLineageReviewScreen queue", () => {
  it("fetches beyond the first 100 edges", async () => {
    listParsedLineageReviewQueue.mockResolvedValue({ items: [VIEW_EDGE], total: 101 });
    const ParsedLineageReviewScreen = await loadScreen();
    render(<ParsedLineageReviewScreen />);
    await screen.findByText("orders");

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));

    await waitFor(() =>
      expect(listParsedLineageReviewQueue).toHaveBeenLastCalledWith(
        expect.objectContaining({ offset: 100 }),
        expect.anything(),
      ),
    );
  });

  it("requires a reason and sends only selected edges, reporting partial failures", async () => {
    listParsedLineageReviewQueue.mockResolvedValue({ items: [VIEW_EDGE], total: 101 });
    bulkDecideParsedLineageEdges.mockResolvedValue({
      succeeded_count: 0,
      failed_count: 1,
      results: [
        { edge_type: "VIEW", edge_id: "edge-1", status: "FAILED", reason: "Maker-checker refusal" },
      ],
    });
    const ParsedLineageReviewScreen = await loadScreen();
    render(<ParsedLineageReviewScreen />);
    await screen.findByText("orders");

    fireEvent.click(screen.getByRole("checkbox", { name: "Select orders to revenue" }));
    expect(screen.getByRole("button", { name: "Approve selected (1)" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Decision reason" }), {
      target: { value: "Reviewed source SQL" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve selected (1)" }));

    await waitFor(() =>
      expect(bulkDecideParsedLineageEdges).toHaveBeenCalledWith({
        items: [{ edge_id: "edge-1", edge_type: "VIEW" }],
        decision: "APPROVED",
        reason: "Reviewed source SQL",
      }),
    );
    expect(await screen.findByText(/Maker-checker refusal/)).toBeInTheDocument();
  });
});
