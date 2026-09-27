import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ColumnDocumentationRead } from "../lib/api/columnDocumentation";
const { save } = vi.hoisted(() => ({save: vi.fn()}));
vi.mock("../lib/api/columnDocumentation", async importOriginal => ({...await importOriginal<object>(), saveColumnWorksheet: save}));
vi.mock("./WorkbookImport", () => ({WorkbookImport: ({initialBatch}: {initialBatch: {id: string}}) => <div>Preview batch {initialBatch.id}</div>}));
import { ColumnWorksheet } from "./ColumnWorksheet";
const column = {column_id: "c1", table_id: "t1", name: "customer_id", physical_type: "uuid", business_description: "Original", description_version: 2, source_description: "Source comment"} as ColumnDocumentationRead;
beforeEach(() => save.mockReset());

it("saves only edited descriptions with their pinned base versions and opens preview", async () => {
  save.mockResolvedValue({id: "batch1", datasource_id: "ds1"});
  render(<ColumnWorksheet tableId="t1" columns={[column]} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Description for customer_id"), {target: {value: "Edited definition"}});
  fireEvent.click(screen.getByRole("button", {name: "Save and preview changes"}));
  await screen.findByText("Preview batch batch1");
  expect(save).toHaveBeenCalledWith("t1", [{column_id: "c1", description: "Edited definition", expected_version: 2}]);
});
it("keeps edits on a failed save and permits retry", async () => {
  save.mockRejectedValueOnce(new Error("Save unavailable"));
  save.mockResolvedValueOnce({id: "batch2", datasource_id: "ds1"});
  render(<ColumnWorksheet tableId="t1" columns={[column]} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Description for customer_id"), {target: {value: "My edit"}});
  fireEvent.click(screen.getByRole("button", {name: "Save and preview changes"}));
  await screen.findByText("Save unavailable");
  expect(screen.getByLabelText("Description for customer_id")).toHaveValue("My edit");
  fireEvent.click(screen.getByRole("button", {name: "Save and preview changes"}));
  await screen.findByText("Preview batch batch2");
});
it("does not silently discard unsaved edits or clear approved descriptions", async () => {
  const close = vi.fn();
  render(<ColumnWorksheet tableId="t1" columns={[column]} onClose={close} />);
  fireEvent.change(screen.getByLabelText("Description for customer_id"), {target: {value: ""}});
  fireEvent.click(screen.getByRole("button", {name: "Save and preview changes"}));
  expect(save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name: "Close Column worksheet"}));
  await waitFor(() => expect(screen.getByText("Discard unsaved edits?")).toBeInTheDocument());
  expect(close).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name: "Discard edits"}));
  expect(close).toHaveBeenCalledOnce();
});
