import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
const queue = vi.fn();
const bulk = vi.fn();
vi.mock("../lib/api", () => ({ listParsedLineageReviewQueue: (...args: unknown[]) => queue(...args), bulkDecideParsedLineageEdges: (...args: unknown[]) => bulk(...args), decideParsedLineageEdge: vi.fn() }));
import { ParsedLineageReviewScreen } from "./ParsedLineageReviewScreen";
beforeEach(() => {
  queue.mockReset(); bulk.mockReset();
  queue.mockResolvedValue({ items: [{ edge_id:"edge-1", edge_type:"VIEW", source_label:"orders", target_label:"revenue", confidence:1, source_sql_reference:{}, created_by:"author" }], total:101 });
});
it("fetches beyond the first 100 edges", async () => {
  render(<ParsedLineageReviewScreen />);
  await screen.findByText("orders");
  fireEvent.click(screen.getByRole("button", {name:"Next page"}));
  await waitFor(() => expect(queue).toHaveBeenLastCalledWith(expect.objectContaining({offset:100}), expect.anything()));
});
it("requires a reason and sends only selected edges, reporting partial failures", async () => {
  bulk.mockResolvedValue({succeeded_count:0, failed_count:1, results:[{edge_type:"VIEW",edge_id:"edge-1",status:"FAILED",reason:"Maker-checker refusal"}]});
  render(<ParsedLineageReviewScreen />);
  await screen.findByText("orders");
  fireEvent.click(screen.getByRole("checkbox", {name:"Select orders to revenue"}));
  expect(screen.getByRole("button", {name:"Approve selected (1)"})).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", {name:"Decision reason"}), {target:{value:"Reviewed source SQL"}});
  fireEvent.click(screen.getByRole("button", {name:"Approve selected (1)"}));
  await waitFor(() => expect(bulk).toHaveBeenCalledWith({items:[{edge_id:"edge-1",edge_type:"VIEW"}],decision:"APPROVED",reason:"Reviewed source SQL"}));
  expect(await screen.findByText(/Maker-checker refusal/)).toBeInTheDocument();
});
