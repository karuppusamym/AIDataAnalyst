import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {
  OkfBundleRead,
  OkfContextRead,
  OkfContextRequest,
  OkfDocumentRead,
  OkfObjectKnowledgeRead,
  OkfPublicationHistoryRead,
  OkfPublicationRead,
} from "../lib/types";
import type { OkfSourceContextRead, OkfSourcePublicationHistoryRead } from "../lib/api/knowledge";
import { ApiError } from "../lib/http";

/* ---------------------------------------------------------------------------
   R11-OKF02. The knowledge view inside Context Products, its section inside
   Catalog object details, and the document renderer both use. The rules these
   tests hold in place:

   1. every read after the manifest is PINNED to the publication the manifest
      described, and so is the download -- a rebuild landing mid-read cannot
      splice a newer document into the bundle on screen;
   2. a bundle link inside a document opens its target in place (the wiki
      reading), and a link to anything outside the bundle is inert text;
   3. document text is never markup: a raw tag in approved text renders as the
      characters, and no rendering path uses innerHTML;
   4. the Catalog section issues no request until it is opened, and says "none
      you may read" rather than counting what it may not show;
   5. version changes show what each publication changed and how much it
      carried unchanged.
--------------------------------------------------------------------------- */

const fetchOkfBundle = vi.fn<(versionId: string, signal?: AbortSignal) => Promise<OkfBundleRead>>();
const fetchOkfDocument =
  vi.fn<
    (versionId: string, path: string, publicationId: string | null, signal?: AbortSignal) => Promise<OkfDocumentRead>
  >();
const fetchOkfPublications =
  vi.fn<(versionId: string, signal?: AbortSignal) => Promise<OkfPublicationHistoryRead>>();
const fetchObjectKnowledge =
  vi.fn<(tableId: string, signal?: AbortSignal) => Promise<OkfObjectKnowledgeRead>>();
const downloadOkfBundle = vi.fn<(versionId: string, publicationId: string) => Promise<void>>();
const selectOkfContext =
  vi.fn<(versionId: string, body: OkfContextRequest, signal?: AbortSignal) => Promise<OkfContextRead>>();
/* R11-OKF02 source bundles: the same five reads against one datasource. */
const fetchSourceOkfBundle = vi.fn<(datasourceId: string, signal?: AbortSignal) => Promise<OkfBundleRead>>();
const fetchSourceOkfDocument =
  vi.fn<
    (datasourceId: string, path: string, publicationId: string | null, signal?: AbortSignal) => Promise<OkfDocumentRead>
  >();
const fetchSourceOkfPublications =
  vi.fn<(datasourceId: string, signal?: AbortSignal) => Promise<OkfSourcePublicationHistoryRead>>();
const downloadSourceOkfBundle = vi.fn<(datasourceId: string, publicationId: string) => Promise<void>>();
const selectSourceOkfContext =
  vi.fn<
    (datasourceId: string, body: OkfContextRequest, signal?: AbortSignal) => Promise<OkfSourceContextRead>
  >();

vi.mock("../lib/api/knowledge", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/knowledge")>();
  return {
    ...actual,
    fetchSourceOkfBundle: (datasourceId: string, signal?: AbortSignal) => fetchSourceOkfBundle(datasourceId, signal),
    fetchSourceOkfDocument: (datasourceId: string, path: string, publicationId: string | null, signal?: AbortSignal) =>
      fetchSourceOkfDocument(datasourceId, path, publicationId, signal),
    fetchSourceOkfPublications: (datasourceId: string, signal?: AbortSignal) =>
      fetchSourceOkfPublications(datasourceId, signal),
    downloadSourceOkfBundle: (datasourceId: string, publicationId: string) =>
      downloadSourceOkfBundle(datasourceId, publicationId),
    selectSourceOkfContext: (datasourceId: string, body: OkfContextRequest, signal?: AbortSignal) =>
      selectSourceOkfContext(datasourceId, body, signal),
    fetchOkfBundle: (versionId: string, signal?: AbortSignal) => fetchOkfBundle(versionId, signal),
    fetchOkfDocument: (versionId: string, path: string, publicationId: string | null, signal?: AbortSignal) =>
      fetchOkfDocument(versionId, path, publicationId, signal),
    fetchOkfPublications: (versionId: string, signal?: AbortSignal) => fetchOkfPublications(versionId, signal),
    fetchObjectKnowledge: (tableId: string, signal?: AbortSignal) => fetchObjectKnowledge(tableId, signal),
    downloadOkfBundle: (versionId: string, publicationId: string) => downloadOkfBundle(versionId, publicationId),
    selectOkfContext: (versionId: string, body: OkfContextRequest, signal?: AbortSignal) =>
      selectOkfContext(versionId, body, signal),
  };
});

const { KnowledgeView } = await import("./KnowledgeView");
const { KnowledgeDocument } = await import("./KnowledgeDocument");
const { ObjectKnowledge } = await import("./ObjectKnowledge");

const VIEW = "sources/source-aaaa/schemas/schema-bbbb/views/view-0123456789abcdef0123456789abcdef.md";
const TABLE = "sources/source-aaaa/schemas/schema-bbbb/tables/table-fedcba9876543210fedcba9876543210.md";

function publication(overrides: Partial<OkfPublicationRead> = {}): OkfPublicationRead {
  return {
    publication_id: "pub-2",
    sequence: 2,
    trigger: "SOURCE_CHANGE",
    captured_at: "2026-09-18T01:00:00Z",
    is_current: true,
    bundle_content_digest: "d".repeat(64),
    content_snapshot_digest: "e".repeat(64),
    document_count: 14,
    rendered_count: 8,
    carried_count: 6,
    valid: true,
    changes: {
      added: [],
      changed: ["log.md", VIEW],
      removed: [],
      changed_subjects: 1,
      marked_subjects: 1,
      full_render: false,
    },
    ...overrides,
  };
}

function bundle(): OkfBundleRead {
  return {
    okf_version: "0.2",
    spec_revision: "0b87c52c6ef999286c745e19998fdfcd03d5dbee",
    spec_conformance: "SELF_CHECKED_AGAINST_PINNED_SPEC_CLAUSES",
    profile: "atlas-okf-export/2",
    content_snapshot_digest: "e".repeat(64),
    bundle_content_digest: "d".repeat(64),
    scope_digest: "f".repeat(64),
    document_count: 4,
    valid: true,
    findings: [],
    files: [
      { path: "index.md", sha256: "1".repeat(64), bytes: 10 },
      { path: "log.md", sha256: "2".repeat(64), bytes: 10 },
      { path: TABLE, sha256: "3".repeat(64), bytes: 10 },
      { path: VIEW, sha256: "4".repeat(64), bytes: 10 },
    ],
    manifest: {
      counts: { tables: 1, views: 1, routines: 0, concepts: 0, tools: 0, sources: 1 },
      source_objects: [{ key: "fedcba9876543210fedcba9876543210", qualified_name: "bank.sales.orders" }],
    },
    publication: publication(),
    validated_at: "2026-09-18T01:05:00Z",
  };
}

function doc(path: string, content: string, rendered = 1): OkfDocumentRead {
  return {
    publication_id: "pub-2",
    publication_sequence: 2,
    path,
    sha256: "9".repeat(64),
    bytes: content.length,
    rendered_in_sequence: rendered,
    subject_key: null,
    content,
  };
}

const INDEX = `---\nokf_version: '0.2'\n---\n\n# Bundle\n\n* Sources in scope: 1.\n* [bank.sales.orders_v](/${VIEW}) - reads\n`;
const VIEW_DOC =
  `---\ntype: Atlas View\nstatus: draft\ngenerated:\n  by: process:atlas-okf-export\n  at: '2026-09-02T00:00:00+00:00'\n---\n\n` +
  `# Purpose\n\nNot established. <img src=x onerror=alert(1)> stays text.\n\n` +
  `# Dependencies\n\n* [bank.sales.orders](/${TABLE}) - reads\n* [outside](https://example.invalid/x) - never a link\n\n` +
  `# Schema\n\n| Column | Type | Description |\n|---|---|---|\n| \`order_id\` | \`uuid\` | _not established_ |\n`;

beforeEach(() => {
  fetchOkfBundle.mockReset().mockResolvedValue(bundle());
  fetchOkfPublications.mockReset().mockResolvedValue({
    context_product_version_id: "ver-1",
    items: [
      publication(),
      publication({
        publication_id: "pub-1",
        sequence: 1,
        trigger: "INITIAL",
        is_current: false,
        changes: { added: [], changed: [], removed: [], changed_subjects: 0, marked_subjects: 0, full_render: true },
      }),
    ],
  });
  fetchOkfDocument.mockReset().mockImplementation(async (_version, path) =>
    path === VIEW ? doc(VIEW, VIEW_DOC, 2) : path === TABLE ? doc(TABLE, "# Purpose\n\nOne row per order.\n") : doc(path, INDEX),
  );
  fetchObjectKnowledge.mockReset();
  downloadOkfBundle.mockReset().mockResolvedValue(undefined);
  selectOkfContext.mockReset();
  fetchSourceOkfBundle.mockReset().mockResolvedValue({
    ...bundle(),
    manifest: {
      scope: { kind: "DATASOURCE", datasource_id: "ds-1" },
      counts: { tables: 1, views: 1, routines: 0, schemas: 1, concepts: 0, tools: 0, sources: 1 },
      source_objects: [{ key: "fedcba9876543210fedcba9876543210", qualified_name: "bank.sales.orders" }],
    },
  });
  fetchSourceOkfDocument.mockReset().mockImplementation(async (_source, path) =>
    path === VIEW ? doc(VIEW, VIEW_DOC, 2) : doc(path, INDEX),
  );
  fetchSourceOkfPublications.mockReset().mockResolvedValue({ datasource_id: "ds-1", items: [publication()] });
  downloadSourceOkfBundle.mockReset().mockResolvedValue(undefined);
  selectSourceOkfContext.mockReset();
});

function context(overrides: Partial<OkfContextRead> = {}): OkfContextRead {
  return {
    context_product_version_id: "ver-1",
    product_key: "revenue_context",
    product_version: 2,
    publication: publication(),
    status: "MATCHED",
    question_terms: ["order", "identifier"],
    documents: [
      {
        citation: "K1",
        path: TABLE,
        sha256: "3".repeat(64),
        type: "Atlas Table",
        title: "bank.sales.orders",
        status: "stable",
        description: null,
        hop: 0,
        score: 4.2,
        matched_terms: ["order"],
        linked_from: null,
        approved_statements: ["purpose"],
        derived_statements: ["columns"],
        sections: [
          { anchor: "purpose", heading: "Purpose", text: "One row per order." },
          { anchor: "schema", heading: "Schema", text: "| x |", rows_shown: 1, rows_total: 40 },
        ],
      },
      {
        citation: "K2",
        path: VIEW,
        sha256: "4".repeat(64),
        type: "Atlas View",
        title: "bank.sales.orders_v",
        status: "draft",
        description: null,
        hop: 1,
        score: 0,
        matched_terms: [],
        linked_from: TABLE,
        approved_statements: [],
        derived_statements: ["columns"],
        sections: [{ anchor: "purpose", heading: "Purpose", text: "Not established." }],
      },
    ],
    omitted: [],
    omitted_count: 2,
    ambiguous: [],
    max_chars: 16000,
    used_chars: 812,
    guidance: "",
    markdown: "",
    ...overrides,
  };
}

describe("What an agent reads", () => {
  it("previews the selection from the publication on screen, with citations and receipts", async () => {
    const user = userEvent.setup();
    selectOkfContext.mockResolvedValue(context());
    render(<KnowledgeView versionId="ver-1" title="Revenue context · v2" onClose={() => {}} />);
    const preview = await screen.findByRole("region", { name: "What an agent reads" });
    await user.type(within(preview).getByLabelText("Question"), "order identifier");
    await user.click(within(preview).getByRole("button", { name: "Preview" }));
    // Pinned to the manifest's publication, like every other read in the view.
    expect(selectOkfContext).toHaveBeenCalledWith(
      "ver-1",
      { question: "order identifier", publication_id: "pub-2" },
      undefined,
    );
    expect(await within(preview).findByText("[K1]")).toBeInTheDocument();
    expect(within(preview).getByText("linked from K1")).toBeInTheDocument();
    expect(within(preview).getByText(/Schema \(1 of 40 rows\)/)).toBeInTheDocument();
    expect(within(preview).getByText(/812 of 16,000 characters; 2 section\(s\) left out/)).toBeInTheDocument();
    // A cited document opens in the reader, pinned the same way.
    await user.click(within(preview).getByRole("button", { name: "bank.sales.orders_v" }));
    await waitFor(() => expect(fetchOkfDocument).toHaveBeenLastCalledWith("ver-1", VIEW, "pub-2", expect.anything()));
  });

  it("says plainly when the bundle holds nothing on the question", async () => {
    const user = userEvent.setup();
    selectOkfContext.mockResolvedValue(context({ status: "NO_MATCH", documents: [], used_chars: 0 }));
    render(<KnowledgeView versionId="ver-1" title="Revenue context · v2" onClose={() => {}} />);
    const preview = await screen.findByRole("region", { name: "What an agent reads" });
    await user.type(within(preview).getByLabelText("Question"), "weather in Paris");
    await user.click(within(preview).getByRole("button", { name: "Preview" }));
    expect(await within(preview).findByText(/Nothing in this bundle matches the question/)).toBeInTheDocument();
    expect(within(preview).queryByText("[K1]")).toBeNull();
  });

  it("shows a refusal as a sentence, not a stack", async () => {
    const user = userEvent.setup();
    selectOkfContext.mockRejectedValue(new Error("Not permitted"));
    render(<KnowledgeView versionId="ver-1" title="Revenue context · v2" onClose={() => {}} />);
    const preview = await screen.findByRole("region", { name: "What an agent reads" });
    await user.type(within(preview).getByLabelText("Question"), "orders");
    await user.click(within(preview).getByRole("button", { name: "Preview" }));
    expect(await within(preview).findByRole("alert")).toHaveTextContent("Not permitted");
  });
});

describe("KnowledgeView", () => {
  it("pins every document read and the download to the publication its manifest described", async () => {
    const user = userEvent.setup();
    render(<KnowledgeView versionId="ver-1" title="Revenue context · v2" onClose={() => {}} />);
    expect(await screen.findByText("bank.sales.orders_v")).toBeInTheDocument();
    expect(fetchOkfDocument).toHaveBeenCalledWith("ver-1", "index.md", "pub-2", expect.anything());

    await user.click(screen.getByRole("button", { name: "Download bundle" }));
    expect(downloadOkfBundle).toHaveBeenCalledWith("ver-1", "pub-2");
  });

  it("opens a bundle link in place, and leaves anything outside the bundle inert", async () => {
    const user = userEvent.setup();
    render(<KnowledgeView versionId="ver-1" title="Revenue context · v2" onClose={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "bank.sales.orders_v" }));
    await waitFor(() => expect(fetchOkfDocument).toHaveBeenLastCalledWith("ver-1", VIEW, "pub-2", expect.anything()));
    const reader = screen.getByRole("region", { name: "Document" });
    expect(await within(reader).findByText(/changed in publication 2/)).toBeInTheDocument();
    // The external target is text, not a link and not a button.
    expect(within(reader).getByText("outside").tagName).toBe("SPAN");
    expect(within(reader).queryByRole("link")).toBeNull();
    // A raw tag in the document is characters on screen, never an element.
    expect(within(reader).getByText(/<img src=x onerror=alert\(1\)> stays text/)).toBeInTheDocument();
    expect(reader.querySelector("img")).toBeNull();
    // The table the view reads opens in place too.
    await user.click(within(reader).getByRole("button", { name: "bank.sales.orders" }));
    await waitFor(() => expect(fetchOkfDocument).toHaveBeenLastCalledWith("ver-1", TABLE, "pub-2", expect.anything()));
    expect(await within(reader).findByText(/unchanged since publication 1/)).toBeInTheDocument();
  });

  it("names documents from the manifest rather than by their digests", async () => {
    render(<KnowledgeView versionId="ver-1" title="Revenue context · v2" onClose={() => {}} />);
    const nav = await screen.findByRole("navigation", { name: "Bundle documents" });
    expect(within(nav).getByRole("button", { name: "bank.sales.orders" })).toBeInTheDocument();
    // A document the manifest does not name keeps a short, unambiguous key.
    expect(within(nav).getByRole("button", { name: "view 01234567" })).toBeInTheDocument();
  });

  it("shows coverage and what each publication changed and carried", async () => {
    render(<KnowledgeView versionId="ver-1" title="Revenue context · v2" onClose={() => {}} />);
    const coverage = await screen.findByLabelText("Coverage");
    expect(coverage).toHaveTextContent("14 documents (8 rendered, 6 carried unchanged)");
    const changes = await screen.findByRole("region", { name: "Version changes" });
    expect(within(changes).getByText(/2 changed, 0 added, 0 removed; 8 rendered, 6 carried unchanged/)).toBeInTheDocument();
    expect(within(changes).getByText("14 documents first published.")).toBeInTheDocument();
    expect(within(changes).getByText("current")).toBeInTheDocument();
  });

  it("refuses to offer a download the server would refuse", async () => {
    fetchOkfBundle.mockResolvedValue({ ...bundle(), valid: false, findings: ["EXTERNAL_LINK:x:y"] });
    render(<KnowledgeView versionId="ver-1" title="Revenue context · v2" onClose={() => {}} />);
    expect(await screen.findByText("1 policy finding(s)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download bundle" })).toBeDisabled();
  });
});

describe("KnowledgeDocument", () => {
  it("renders headings, lists, tables and the frontmatter's type and content date", () => {
    render(<KnowledgeDocument content={VIEW_DOC} />);
    expect(screen.getByText("Atlas View")).toBeInTheDocument();
    expect(screen.getByText("content as of 2026-09-02T00:00:00+00:00")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Purpose" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("order_id");
    // `_emphasis_` renders as emphasis, and an identifier's underscore is left alone.
    expect(screen.getByText("not established").tagName).toBe("EM");
    // With no navigation handler, even a bundle link is plain text.
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("resolves an index's relative and directory links against the document's own path", async () => {
    const user = userEvent.setup();
    const opened: string[] = [];
    render(
      <KnowledgeDocument
        path="sources/source-aaaa/schemas/schema-bbbb/index.md"
        content={"# Tables\n\n* [orders](tables/table-x.md) - one\n* [up](../../index.md) - two\n* [dir](views/) - three\n"}
        onNavigate={(path) => opened.push(path)}
      />,
    );
    await user.click(screen.getByRole("button", { name: "orders" }));
    await user.click(screen.getByRole("button", { name: "up" }));
    await user.click(screen.getByRole("button", { name: "dir" }));
    expect(opened).toEqual([
      "sources/source-aaaa/schemas/schema-bbbb/tables/table-x.md",
      "sources/source-aaaa/index.md",
      "sources/source-aaaa/schemas/schema-bbbb/views/index.md",
    ]);
  });

  it("never stalls on a line no block claims", () => {
    render(<KnowledgeDocument content={"[^loose] marker without a definition\n\nnext"} />);
    expect(screen.getByText(/marker without a definition/)).toBeInTheDocument();
    expect(screen.getByText("next")).toBeInTheDocument();
  });
});

describe("ObjectKnowledge", () => {
  it("issues no request until it is opened", async () => {
    const user = userEvent.setup();
    fetchObjectKnowledge.mockResolvedValue({ table_id: "t-1", items: [] });
    render(<ObjectKnowledge tableId="t-1" />);
    expect(fetchObjectKnowledge).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /Knowledge/ }));
    expect(fetchObjectKnowledge).toHaveBeenCalledWith("t-1", expect.anything());
    expect(
      await screen.findByText("No published knowledge bundle you may read includes this object."),
    ).toBeInTheDocument();
  });

  it("shows the object's document, its coverage and which publication last changed it", async () => {
    const user = userEvent.setup();
    fetchObjectKnowledge.mockResolvedValue({
      table_id: "t-1",
      items: [
        {
          context_product_version_id: "ver-1",
          product_key: "revenue_context",
          product_version: 2,
          product_name: "Revenue context",
          publication: publication(),
          document: doc(VIEW, VIEW_DOC, 2),
          coverage: { description_state: "NONE", definition_digest: "abcdef0123456789", definition_capture_version: null },
        },
      ],
    });
    render(<ObjectKnowledge tableId="t-1" />);
    await user.click(screen.getByRole("button", { name: /Knowledge/ }));
    expect(await screen.findByText("Revenue context · revenue_context v2")).toBeInTheDocument();
    expect(screen.getByText("changed in this publication")).toBeInTheDocument();
    expect(screen.getByText("definition abcdef012345")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Download bundle" }));
    expect(downloadOkfBundle).toHaveBeenCalledWith("ver-1", "pub-2");
  });
});

/* R11-OKF02 source bundles: the same view over one datasource's bundle. What
   these hold in place: the view reads ONLY the source routes (never a product
   route), pins every read and the download to the manifest's publication as it
   does for a product, says what a source bundle is and is not, and shows the
   server's refusal as a sentence rather than an empty bundle. */
describe("KnowledgeView over a source bundle", () => {
  it("reads the datasource's own routes, pinned to the publication on screen", async () => {
    const user = userEvent.setup();
    render(<KnowledgeView datasourceId="ds-1" title="warehouse · source bundle" onClose={() => {}} />);
    expect(await screen.findByText("bank.sales.orders_v")).toBeInTheDocument();
    expect(fetchSourceOkfBundle).toHaveBeenCalledWith("ds-1", expect.anything());
    expect(fetchSourceOkfPublications).toHaveBeenCalledWith("ds-1", expect.anything());
    expect(fetchSourceOkfDocument).toHaveBeenCalledWith("ds-1", "index.md", "pub-2", expect.anything());
    // Nothing is read through a product's routes.
    expect(fetchOkfBundle).not.toHaveBeenCalled();
    expect(fetchOkfDocument).not.toHaveBeenCalled();
    expect(fetchOkfPublications).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "bank.sales.orders_v" }));
    await waitFor(() =>
      expect(fetchSourceOkfDocument).toHaveBeenLastCalledWith("ds-1", VIEW, "pub-2", expect.anything()),
    );
    await user.click(screen.getByRole("button", { name: "Download bundle" }));
    expect(downloadSourceOkfBundle).toHaveBeenCalledWith("ds-1", "pub-2");
    expect(downloadOkfBundle).not.toHaveBeenCalled();
  });

  it("says what a source bundle holds and counts schemas, not concepts or tools", async () => {
    render(<KnowledgeView datasourceId="ds-1" title="warehouse · source bundle" onClose={() => {}} />);
    const coverage = await screen.findByLabelText("Coverage");
    expect(coverage).toHaveTextContent("1 schemas");
    expect(coverage).not.toHaveTextContent("concepts");
    expect(coverage).not.toHaveTextContent("tools");
    expect(screen.getByText(/Business concepts and tools are selected in a context product/)).toBeInTheDocument();
  });

  it("asks a question of the source bundle, pinned to the publication on screen", async () => {
    const user = userEvent.setup();
    const { context_product_version_id: _v, product_key: _k, product_version: _n, ...selection } = context();
    selectSourceOkfContext.mockResolvedValue({ ...selection, datasource_id: "ds-1", datasource_name: "warehouse" });
    render(<KnowledgeView datasourceId="ds-1" title="warehouse · source bundle" onClose={() => {}} />);
    const preview = await screen.findByRole("region", { name: "What an agent reads" });
    await user.type(within(preview).getByLabelText("Question"), "order identifier");
    await user.click(within(preview).getByRole("button", { name: "Preview" }));
    expect(selectSourceOkfContext).toHaveBeenCalledWith(
      "ds-1",
      { question: "order identifier", publication_id: "pub-2" },
      undefined,
    );
    expect(selectOkfContext).not.toHaveBeenCalled();
    expect(await within(preview).findByText("[K1]")).toBeInTheDocument();
  });

  it("shows a refused datasource as a refusal, never as an empty bundle", async () => {
    fetchSourceOkfBundle.mockRejectedValue(new ApiError(403, "NO_BINDING_FOR_DATASOURCE"));
    render(<KnowledgeView datasourceId="ds-1" title="warehouse · source bundle" onClose={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("You are not permitted to read this bundle.");
    expect(screen.queryByLabelText("Coverage")).toBeNull();
    expect(fetchSourceOkfDocument).not.toHaveBeenCalled();
  });
});
