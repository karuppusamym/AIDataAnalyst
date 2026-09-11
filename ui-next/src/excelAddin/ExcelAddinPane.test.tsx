import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkbookHost } from "./officeHost";

/* ---------------------------------------------------------------------------
   The Excel task pane, against a fake Office host and a mocked API boundary.

   What these pin is the save-back contract, not the layout: the file goes to
   the source its own README names, saving publishes nothing, and a workbook
   with no Atlas identity is refused rather than guessed at.
--------------------------------------------------------------------------- */

const upload = vi.fn();
const submit = vi.fn();
const datasources = vi.fn();
const blob = vi.fn();

vi.mock("../lib/api/columnDocumentation", () => ({
  uploadModelWorkbook: (...args: unknown[]) => upload(...args),
  submitModelImport: (...args: unknown[]) => submit(...args),
}));
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, fetchOrgDatasources: (...args: unknown[]) => datasources(...args) };
});
vi.mock("../lib/api/transport", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/transport")>();
  return { ...actual, requestBlob: (...args: unknown[]) => blob(...args) };
});

import { ApiError } from "../lib/api";
import { ExcelAddinPane } from "./ExcelAddinPane";

const SOURCE = "11111111-2222-3333-4444-555555555555";
const ORG = "00000000-0000-0000-0000-000000000009";
const README = [
  ["Field", "Value"],
  ["Datasource", "warehouse"],
  ["Datasource id", SOURCE],
  ["Organization id", ORG],
];

function fakeHost(overrides: Partial<WorkbookHost> = {}): WorkbookHost {
  return {
    inExcel: true,
    readSheet: vi.fn().mockResolvedValue(README),
    readWorkbookFile: vi.fn().mockResolvedValue(new Uint8Array([80, 75, 3, 4])),
    openWorkbook: vi.fn().mockResolvedValue(undefined),
    fileName: () => "warehouse-model.xlsx",
    runDialog: vi.fn(),
    ...overrides,
  };
}

const BATCH = {
  id: "b1",
  organization_id: ORG,
  datasource_id: SOURCE,
  filename: "warehouse-model.xlsx",
  content_sha256: "x".repeat(64),
  status: "DRAFT",
  governance_review_id: null,
  change_count: 3,
  applied_count: 0,
  skipped_count: 0,
  rejected_row_count: 1,
  uploaded_by: "local-ui-admin",
  reviewed_by: null,
  reviewed_at: null,
};

beforeEach(() => {
  for (const mock of [upload, submit, datasources, blob]) mock.mockReset();
});

describe("ExcelAddinPane", () => {
  it("saves the open workbook to the source its own README names", async () => {
    upload.mockResolvedValue(BATCH);
    render(<ExcelAddinPane host={fakeHost()} />);

    fireEvent.click(await screen.findByRole("button", { name: "Save to Atlas" }));

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
    const [datasourceId, file] = upload.mock.calls[0] as [string, File];
    expect(datasourceId).toBe(SOURCE);
    expect(file.name).toBe("warehouse-model.xlsx");
    expect(file.size).toBe(4);
    const result = await screen.findByLabelText("Save result");
    expect(result).toHaveTextContent("3 changes ready for review");
    expect(result).toHaveTextContent("1 row could not be applied");
  });

  it("publishes nothing on save; submitting hands the decision to someone else", async () => {
    upload.mockResolvedValue(BATCH);
    submit.mockResolvedValue({ ...BATCH, status: "PENDING_REVIEW" });
    render(<ExcelAddinPane host={fakeHost()} />);

    fireEvent.click(await screen.findByRole("button", { name: "Save to Atlas" }));
    fireEvent.click(await screen.findByRole("button", { name: "Submit for review" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith("b1"));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Nothing is published until someone other than you approves it",
    );
    expect(screen.queryByRole("button", { name: "Submit for review" })).not.toBeInTheDocument();
  });

  it("refuses to guess where a workbook with no Atlas identity belongs", async () => {
    render(<ExcelAddinPane host={fakeHost({ readSheet: vi.fn().mockResolvedValue(null) })} />);

    expect(await screen.findByText(/did not come from Atlas/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save to Atlas" })).not.toBeInTheDocument();
  });

  it("shows the server's refusal as the server phrased it", async () => {
    upload.mockRejectedValue(
      new ApiError(422, "this workbook was exported from a different datasource (x)."),
    );
    render(<ExcelAddinPane host={fakeHost()} />);

    fireEvent.click(await screen.findByRole("button", { name: "Save to Atlas" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "this workbook was exported from a different datasource",
    );
  });

  it("outside Excel it says where to open it instead of failing", () => {
    render(<ExcelAddinPane host={fakeHost({ inExcel: false })} />);
    expect(screen.getByText(/Open it from the Atlas button/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("opens a model from Atlas as a new workbook", async () => {
    datasources.mockResolvedValue({ items: [{ id: SOURCE, name: "warehouse" }] });
    const bytes = new Uint8Array([1, 2, 3]);
    blob.mockResolvedValue({ blob: { arrayBuffer: async () => bytes.buffer }, response: {} });
    const host = fakeHost({ readSheet: vi.fn().mockResolvedValue(null) });
    render(<ExcelAddinPane host={host} />);

    const panel = await screen.findByLabelText("Open a model from Atlas");
    fireEvent.click(within(panel).getByRole("button", { name: "Choose a source" }));
    fireEvent.click(await within(panel).findByRole("button", { name: "Open in Excel" }));

    await waitFor(() => expect(host.openWorkbook).toHaveBeenCalledWith(bytes));
    expect(blob).toHaveBeenCalledWith(`/v1/datasources/${SOURCE}/model/export.xlsx`);
  });
});
