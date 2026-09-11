import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { DataDictionariesScreen } from "./DataDictionariesScreen";
import type { DocumentRead } from "../lib/types";
import type { DocumentClaimRead, DocumentMappingRead, DocumentSectionRead } from "../lib/ui-types";

const api = vi.hoisted(() => ({
  fetchProjectDocuments: vi.fn(),
  uploadDataDictionary: vi.fn(),
  fetchDocumentSections: vi.fn(),
  fetchDocumentMappings: vi.fn(),
  fetchDocumentClaims: vi.fn(),
  mapDocument: vi.fn(),
  extractDocumentClaims: vi.fn(),
}));
const scope = vi.hoisted(() => ({ projectId: "project-1" }));

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, ...api };
});

vi.mock("../lib/scope", async () => {
  const actual = await vi.importActual<typeof import("../lib/scope")>("../lib/scope");
  return {
    ...actual,
    useScopeSelection: () =>
      scope.projectId
        ? {
            projectId: scope.projectId,
            projects: [{ id: scope.projectId, name: "Governed Analytics" }],
            ready: true,
          }
        : null,
  };
});

function page<T>(items: T[]) {
  return { items, limit: 500, offset: 0, total: items.length };
}

function documentRow(overrides: Partial<DocumentRead> = {}): DocumentRead {
  return {
    id: "doc-1",
    organization_id: "org-1",
    project_id: "project-1",
    filename: "dictionary.csv",
    media_type: "CSV",
    sha256: "0".repeat(64),
    status: "PARSED",
    section_count: 2,
    parse_error_count: 1,
    uploaded_by: "steward@bank",
    created_at: "2026-09-11T10:00:00Z",
    updated_at: "2026-09-11T10:00:00Z",
    ...overrides,
  };
}

const SECTIONS: DocumentSectionRead[] = [
  {
    id: "s-1",
    document_id: "doc-1",
    ordinal: 0,
    raw_schema_name: "public",
    raw_table_name: "payments",
    raw_column_name: "amount_ccy",
    raw_description: "ISO 4217 currency of the payment amount.",
  },
  {
    id: "s-2",
    document_id: "doc-1",
    ordinal: 1,
    raw_schema_name: "legacy",
    raw_table_name: "gl_postings",
    raw_column_name: "gl_code",
    raw_description: "General-ledger account the posting was booked to.",
  },
];

const MAPPINGS: DocumentMappingRead[] = [
  {
    id: "m-1",
    document_section_id: "s-1",
    subject_type: "COLUMN",
    subject_id: "column-1",
    mapping_kind: "STRUCTURAL",
    confidence: 1,
  },
  {
    id: "m-2",
    document_section_id: "s-2",
    subject_type: "COLUMN",
    subject_id: null,
    mapping_kind: "UNMATCHED",
    confidence: 0,
  },
];

const CLAIMS: DocumentClaimRead[] = [
  {
    id: "c-1",
    document_section_id: "s-1",
    subject_type: "COLUMN",
    subject_id: "column-1",
    predicate: "DESCRIBES",
    object_value: "ISO 4217 currency of the payment amount.",
    confidence: 1,
    status: "PENDING",
    governance_review_id: "review-1",
    created_by: "steward@bank",
    reviewed_by: null,
    reviewed_at: null,
  },
];

/** jsdom's File has no `text()`; every browser the app supports does. */
function withText(file: File, content: string): File {
  if (typeof file.text !== "function") {
    Object.defineProperty(file, "text", { value: () => Promise.resolve(content) });
  }
  return file;
}

function chooseFile(file: File) {
  fireEvent.change(screen.getByLabelText("Data dictionary (CSV)"), { target: { files: [file] } });
  fireEvent.click(screen.getByRole("button", { name: "Upload" }));
}

describe("DataDictionariesScreen (N8 document ingestion)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    scope.projectId = "project-1";
    history.replaceState(null, "", "/#/data-dictionaries");
    api.fetchProjectDocuments.mockResolvedValue(page([documentRow()]));
    api.fetchDocumentSections.mockResolvedValue(page(SECTIONS));
    api.fetchDocumentMappings.mockResolvedValue(page([]));
    api.fetchDocumentClaims.mockResolvedValue(page([]));
  });

  it("asks for a project first, and loads nothing without one", () => {
    scope.projectId = "";
    render(<DataDictionariesScreen />);

    expect(screen.getByText("Choose a project")).toBeInTheDocument();
    expect(api.fetchProjectDocuments).not.toHaveBeenCalled();
  });

  it("uploads the chosen CSV as text and opens it", async () => {
    const content = "table,description\npayments,One row per settled payment.\n";
    api.fetchProjectDocuments.mockResolvedValue(page([]));
    api.uploadDataDictionary.mockImplementation(async (_project: string, body: { filename: string }) =>
      documentRow({ id: "doc-9", filename: body.filename, section_count: 1, parse_error_count: 0 }),
    );
    render(<DataDictionariesScreen />);
    await screen.findByText("None yet");

    chooseFile(withText(new File([content], "dictionary.csv", { type: "text/csv" }), content));

    await waitFor(() =>
      expect(api.uploadDataDictionary).toHaveBeenCalledWith("project-1", {
        filename: "dictionary.csv",
        content,
      }),
    );
    expect(await screen.findByText(/Uploaded dictionary\.csv: 1 row kept/)).toBeInTheDocument();
    await waitFor(() => expect(window.location.search).toContain("document=doc-9"));
  });

  it("refuses a file over 1 MB without sending it", async () => {
    render(<DataDictionariesScreen />);
    await screen.findByRole("list", { name: "Uploaded data dictionaries" });

    chooseFile(new File(["x".repeat(1_000_001)], "everything.csv", { type: "text/csv" }));

    expect(await screen.findByText(/over 1 MB/)).toBeInTheDocument();
    expect(api.uploadDataDictionary).not.toHaveBeenCalled();
  });

  it("matches an uploaded dictionary, then offers to propose only the matched rows", async () => {
    history.replaceState(null, "", "/?document=doc-1#/data-dictionaries");
    api.mapDocument.mockImplementation(async () => {
      api.fetchProjectDocuments.mockResolvedValue(page([documentRow({ status: "MAPPED" })]));
      api.fetchDocumentMappings.mockResolvedValue(page(MAPPINGS));
      return { document_id: "doc-1", matched_count: 1, unmatched_count: 1 };
    });
    render(<DataDictionariesScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Match rows to the catalog" }));

    await waitFor(() => expect(api.mapDocument).toHaveBeenCalledWith("doc-1"));
    expect(await screen.findByRole("button", { name: "Propose 1 row for review" })).toBeInTheDocument();
    const rows = screen.getByRole("table", { name: "Rows in dictionary.csv" });
    expect(within(rows).getByText("Matched")).toBeInTheDocument();
    expect(within(rows).getByText("No match")).toBeInTheDocument();
  });

  it("proposes once and links each proposal to its own review", async () => {
    history.replaceState(null, "", "/?document=doc-1#/data-dictionaries");
    api.fetchProjectDocuments.mockResolvedValue(page([documentRow({ status: "MAPPED" })]));
    api.fetchDocumentMappings.mockResolvedValue(page(MAPPINGS));
    api.extractDocumentClaims.mockImplementation(async () => {
      api.fetchDocumentClaims.mockResolvedValue(page(CLAIMS));
      return page(CLAIMS);
    });
    render(<DataDictionariesScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Propose 1 row for review" }));

    const link = await screen.findByRole("link", { name: "Open in review queue" });
    expect(link.getAttribute("href")).toContain("#/governance");
    expect(link.getAttribute("href")).toContain("review=review-1");
    expect(api.extractDocumentClaims).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Propose/ })).not.toBeInTheDocument(),
    );
  });

  it("does not offer to propose rows that are already in review", async () => {
    history.replaceState(null, "", "/?document=doc-1#/data-dictionaries");
    api.fetchProjectDocuments.mockResolvedValue(page([documentRow({ status: "MAPPED" })]));
    api.fetchDocumentMappings.mockResolvedValue(page(MAPPINGS));
    api.fetchDocumentClaims.mockResolvedValue(page(CLAIMS));
    render(<DataDictionariesScreen />);

    expect(await screen.findByText("In review")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Propose/ })).not.toBeInTheDocument();
  });
});
