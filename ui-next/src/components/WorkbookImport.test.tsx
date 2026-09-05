import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type {
  ModelImportBatchRead,
  ModelImportChangeRead,
} from "../lib/_column_documentation_api";

const upload = vi.fn();
const fetchChanges = vi.fn();
const submit = vi.fn();
const setExclusion = vi.fn();

vi.mock("../lib/_column_documentation_api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../lib/_column_documentation_api",
  );
  return {
    ...actual,
    uploadModelWorkbook: (...args: unknown[]) => upload(...args),
    fetchModelImportChanges: (...args: unknown[]) => fetchChanges(...args),
    submitModelImport: (...args: unknown[]) => submit(...args),
    setModelImportExclusion: (...args: unknown[]) => setExclusion(...args),
  };
});

import { WorkbookImport } from "./WorkbookImport";

beforeEach(() => {
  upload.mockReset();
  fetchChanges.mockReset();
  submit.mockReset();
  setExclusion.mockReset();
});

function batch(overrides: Partial<ModelImportBatchRead> = {}): ModelImportBatchRead {
  return {
    id: "b1",
    organization_id: "o1",
    datasource_id: "d1",
    filename: "warehouse-model.xlsx",
    content_sha256: "a".repeat(64),
    status: "DRAFT",
    governance_review_id: null,
    change_count: 1,
    applied_count: 0,
    skipped_count: 0,
    rejected_row_count: 0,
    uploaded_by: "maker@example.com",
    reviewed_by: null,
    reviewed_at: null,
    ...overrides,
  };
}

function change(overrides: Partial<ModelImportChangeRead> = {}): ModelImportChangeRead {
  return {
    id: "ch1",
    batch_id: "b1",
    sheet_name: "Columns",
    row_number: 2,
    subject_type: "COLUMN",
    subject_id: "c1",
    subject_label: "customers.customer_id",
    field: "business_description",
    old_value: null,
    new_value: "The customer's unique identifier.",
    expected_version: null,
    status: "PENDING",
    skip_reason: null,
    ...overrides,
  };
}

function selectFile(name = "warehouse-model.xlsx") {
  const input = screen.getByLabelText("Edited model workbook") as HTMLInputElement;
  const file = new File([new Uint8Array([80, 75])], name);
  fireEvent.change(input, { target: { files: [file] } });
  return file;
}

it("parses a chosen file and shows what it would change without publishing", async () => {
  upload.mockResolvedValue(batch());
  fetchChanges.mockResolvedValue([change()]);

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();

  await waitFor(() => expect(screen.getByText("customers.customer_id")).toBeInTheDocument());
  expect(screen.getByText("The customer's unique identifier.")).toBeInTheDocument();
  // Upload alone must never submit: that is the whole reason the two are
  // separate steps.
  expect(submit).not.toHaveBeenCalled();
  expect(
    screen.getByRole("button", { name: "Submit 1 change for review" }),
  ).toBeInTheDocument();
});

it("shows rows that could not be applied before the ones that could", async () => {
  // A steward whose upload half-failed needs to see that first; "312 changes
  // ready" above "88 rows could not be matched" is the truth in the wrong order.
  upload.mockResolvedValue(batch({ change_count: 1, rejected_row_count: 1 }));
  fetchChanges.mockResolvedValue([
    change({ id: "ok", row_number: 2, subject_label: "customers.customer_id" }),
    change({
      id: "bad",
      row_number: 9,
      subject_label: "customers.legacy_id",
      status: "REJECTED",
      skip_reason: "no active column with this id in this datasource",
    }),
  ]);

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();

  await waitFor(() => expect(screen.getByText("1 rows not applied")).toBeInTheDocument());
  const subjects = screen
    .getAllByTitle(/customers\./)
    .map((node) => node.textContent);
  expect(subjects[0]).toBe("customers.legacy_id");
  expect(
    screen.getByText("no active column with this id in this datasource"),
  ).toBeInTheDocument();
});

it("shows the value being replaced next to the new one", async () => {
  upload.mockResolvedValue(batch());
  fetchChanges.mockResolvedValue([
    change({ old_value: "Old wording.", new_value: "New wording." }),
  ]);

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();

  await waitFor(() => expect(screen.getByText("New wording.")).toBeInTheDocument());
  expect(screen.getByText("Old wording.")).toBeInTheDocument();
});

it("submits only when asked, and then says a reviewer still has to decide", async () => {
  upload.mockResolvedValue(batch());
  fetchChanges.mockResolvedValue([change()]);
  submit.mockResolvedValue(batch({ status: "PENDING_REVIEW", governance_review_id: "r1" }));

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();
  await waitFor(() => expect(screen.getByText("customers.customer_id")).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: "Submit 1 change for review" }));

  await waitFor(() => expect(submit).toHaveBeenCalledWith("b1"));
  expect(screen.getByText(/someone other than you has to approve it/)).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /Submit 1 change for review/ }),
  ).not.toBeInTheDocument();
});

it("offers no submit control when the workbook changes nothing", async () => {
  upload.mockResolvedValue(batch({ change_count: 0 }));
  fetchChanges.mockResolvedValue([]);

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();

  await waitFor(() =>
    expect(
      screen.getByText(/matches the current model — there is nothing to submit/),
    ).toBeInTheDocument(),
  );
  expect(screen.queryByRole("button", { name: /Submit/ })).not.toBeInTheDocument();
});

it("reports a refused upload instead of an empty diff", async () => {
  const { ApiError } = await import("../lib/api");
  upload.mockRejectedValue(
    new ApiError(422, "the workbook has no 'Tables' or 'Columns' sheet."),
  );

  render(<WorkbookImport datasourceId="d1" />);
  selectFile("holiday-photos.xlsx");

  await waitFor(() =>
    expect(
      screen.getByText("the workbook has no 'Tables' or 'Columns' sheet."),
    ).toBeInTheDocument(),
  );
  expect(screen.queryByRole("button", { name: /Submit/ })).not.toBeInTheDocument();
});

it("labels a superseded change as superseded, not as an error", async () => {
  upload.mockResolvedValue(batch({ status: "APPLIED", applied_count: 0, skipped_count: 1 }));
  fetchChanges.mockResolvedValue([
    change({
      status: "SKIPPED_STALE",
      skip_reason: "someone published a newer description after the workbook was exported",
    }),
  ]);

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();

  await waitFor(() => expect(screen.getByText("superseded")).toBeInTheDocument());
  expect(screen.getByText("1 skipped")).toBeInTheDocument();
});


/* ---------------------------------------------------------------------------
   Excluding rows (2026-09-05). One wrong row used to force rejecting the whole
   file and re-uploading a corrected one.
--------------------------------------------------------------------------- */

it("drops a row from the batch without rejecting the file", async () => {
  upload.mockResolvedValue(batch({ change_count: 2 }));
  fetchChanges.mockResolvedValue([
    change({ id: "keep", subject_label: "customers.customer_id" }),
    change({ id: "drop", row_number: 3, subject_label: "customers.segment_code" }),
  ]);
  setExclusion.mockResolvedValue(batch({ change_count: 1 }));

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();
  await waitFor(() => expect(screen.getByText("customers.segment_code")).toBeInTheDocument());

  fireEvent.click(
    screen.getByLabelText("Include customers.segment_code business_description"),
  );

  await waitFor(() => expect(setExclusion).toHaveBeenCalledWith("b1", ["drop"], true));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Submit 1 change for review" })).toBeInTheDocument(),
  );
});

it("keeps an excluded row visible, marked, rather than hiding the decision", async () => {
  upload.mockResolvedValue(batch({ change_count: 1 }));
  fetchChanges.mockResolvedValue([
    change({ id: "keep" }),
    change({ id: "drop", row_number: 3, subject_label: "customers.segment_code", status: "EXCLUDED" }),
  ]);

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();

  await waitFor(() => expect(screen.getByText("customers.segment_code")).toBeInTheDocument());
  expect(screen.getByText("excluded")).toBeInTheDocument();
  expect(screen.getByText("1 excluded")).toBeInTheDocument();
});

it("puts an excluded row back when it is re-checked", async () => {
  upload.mockResolvedValue(batch({ change_count: 0 }));
  fetchChanges.mockResolvedValue([change({ id: "drop", status: "EXCLUDED" })]);
  setExclusion.mockResolvedValue(batch({ change_count: 1 }));

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();
  await waitFor(() => expect(screen.getByText("excluded")).toBeInTheDocument());

  fireEvent.click(screen.getByLabelText("Include customers.customer_id business_description"));

  await waitFor(() => expect(setExclusion).toHaveBeenCalledWith("b1", ["drop"], false));
});

it("stops offering exclusion once the batch is submitted", async () => {
  // What a reviewer is asked to decide has to be fixed the moment it is
  // submitted, or "approve this batch" would not name a stable thing.
  upload.mockResolvedValue(batch({ status: "PENDING_REVIEW", governance_review_id: "r1" }));
  fetchChanges.mockResolvedValue([change()]);

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();

  await waitFor(() => expect(screen.getByText("customers.customer_id")).toBeInTheDocument());
  expect(
    screen.queryByLabelText("Include customers.customer_id business_description"),
  ).not.toBeInTheDocument();
});

it("offers no exclusion control on a row the diff already rejected", async () => {
  // Those were never going to apply; a checkbox would imply they could.
  upload.mockResolvedValue(batch({ change_count: 0, rejected_row_count: 1 }));
  fetchChanges.mockResolvedValue([
    change({ id: "bad", status: "REJECTED", skip_reason: "no active column with this id" }),
  ]);

  render(<WorkbookImport datasourceId="d1" />);
  selectFile();

  await waitFor(() => expect(screen.getByText("not applied")).toBeInTheDocument());
  expect(
    screen.queryByLabelText("Include customers.customer_id business_description"),
  ).not.toBeInTheDocument();
});
